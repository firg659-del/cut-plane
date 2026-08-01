# SPDX-License-Identifier: GPL-3.0-or-later
"""Session handlers (save / load / undo / redo) + orphan purge & crash recovery.

The live preview is a destructive bisect holding a snapshot Mesh datablock:
  save_pre   restores the original before the .blend is written (so the cut
             isn't baked in / the snapshot isn't orphaned); save_post re-applies.
  load_post  resets the session (the snapshot belonged to the previous file).
  undo/redo  reset the session POINTER-ONLY (mesh edits / datablock removal are
             unsafe inside these handlers), so the watchdog re-snapshots the
             real post-undo mesh next frame and a cut never reappears on the
             next plane move. The abandoned snapshot is reclaimed by a deferred
             write-safe purge (Blender keeps it alive via use_fake_user).
These manage the PREVIEW's lifetime around session events; they are NOT an
undo *implementation* - operator undo stays plain Blender-native.
"""

import bmesh
import bpy

from ..lib.constants import logger, on_internal_error
from . import state
from .invariants import _debug_check
from .preview import adopt_committed_baseline, restore_live_cut, update_live_preview
from .snapshot import _snapshot_valid
from .state import (
    _RECOVERY_TARGET_PROP,
    _RECOVERY_TOKEN_PROP,
    _SNAPSHOT_NAME,
    _sessions,
)
from .watchdog import _deferred_sync


def _purge_orphan_snapshots() -> None:
    """Write-safe timer: remove snapshot datablocks no longer referenced.

    Undo/redo and file load reset the cache pointer-only (removing a datablock is
    unsafe inside those handlers), so the fake-user snapshot Mesh they abandon
    would linger forever. This reclaims every snapshot datablock except the
    current live one. Runs from a timer, where bpy.data edits are safe.
    """
    keep_ptrs = set()
    for cache in _sessions.values():
        if _snapshot_valid(cache):
            try:
                keep_ptrs.add(cache.live_snapshot.as_pointer())
            except Exception:
                pass  # snapshot freed between the validity check and here - skip it
    for me in list(bpy.data.meshes):
        try:
            # Ownership = our recovery stamp, not just the name prefix: every
            # snapshot this addon has ever created carries the stamp, while a
            # user mesh RENAMED to the prefix does not - the prefix-only test
            # deleted such a mesh AND the objects using it (remove() unlinks).
            if (me.name.startswith(_SNAPSHOT_NAME)
                    and me.get(_RECOVERY_TARGET_PROP) is not None
                    and me.as_pointer() not in keep_ptrs):
                bpy.data.meshes.remove(me)
        except Exception:
            on_internal_error()  # DEBUG: surface; prod: skip this one, keep purging
    return None  # one-shot timer


def _schedule_purge() -> None:
    """Schedule the orphan-snapshot purge on a write-safe one-shot timer."""
    try:
        if not bpy.app.timers.is_registered(_purge_orphan_snapshots):
            bpy.app.timers.register(_purge_orphan_snapshots, first_interval=0.0)
    except Exception:
        on_internal_error()


def _recover_orphan_snapshots() -> None:
    """Write-safe timer, LOAD-TIME ONLY: crash-recovery self-heal.

    A snapshot datablock inside a freshly loaded file is the fingerprint of
    a handler-less write (autosave / crash file / quit.blend): every normal
    save restores the mesh and frees the snapshot in save_pre, so saved
    files never contain one. Such a file therefore holds the PREVIEW CUT
    baked into the mesh - and the snapshot IS the uncut original. Restore
    it into its stamped target mesh, discarding the never-executed cut, so
    recovering from a crash lands on the geometry the user actually had.

    Ends with the regular purge. MUST NOT run for in-session purges
    (undo/redo, register): there the mesh legitimately differs from the
    leftover snapshot and a restore would fight the undo system - only
    _on_load_post schedules this.
    """
    for me in list(bpy.data.meshes):
        if not me.name.startswith(_SNAPSHOT_NAME):
            continue
        token = me.get(_RECOVERY_TOKEN_PROP)
        if token:
            # The identity: the mesh carrying the SAME token this snapshot
            # was stamped with. Rename-proof and reuse-proof - matching by
            # NAME restored into whatever mesh happened to hold the stamped
            # name (one rename + one reuse = an unrelated mesh silently
            # overwritten under a "Recovered" success log; measured).
            target = next(
                (m for m in bpy.data.meshes
                 if not m.name.startswith(_SNAPSHOT_NAME)
                 and m.get(_RECOVERY_TOKEN_PROP) == token), None)
            if target is None:
                # The target datablock is genuinely gone (tokens survive
                # renames) - the baked preview cut cannot be undone. Say so:
                # the old silent `continue` + purge destroyed the only copy
                # of the original without a word.
                logger.warning(
                    f"[CUT PLANE] Crash-recovery snapshot '{me.name}' has no "
                    f"surviving target mesh (stamped name was "
                    f"{me.get(_RECOVERY_TARGET_PROP)!r}) - the un-executed "
                    f"preview cut could NOT be reverted")
                continue
        else:
            # Token-less snapshot: only pre-release dev builds ever wrote
            # these (every shipped build stamps the token pair). The legacy
            # NAME stamp cannot be trusted - a freed name may have been
            # reused by an unrelated mesh, and restoring would overwrite it.
            # Never restore by name; the purge below clears the leftover.
            continue
        try:
            if target.is_editmode:  # file was saved (crashed) in Edit Mode
                bm = bmesh.from_edit_mesh(target)
                bm.clear()
                bm.from_mesh(me)
                bmesh.update_edit_mesh(target, destructive=True)
            else:
                tmp = bmesh.new()
                tmp.from_mesh(me)
                tmp.to_mesh(target)
                tmp.free()
                target.update()
            try:
                if _RECOVERY_TOKEN_PROP in target.keys():
                    del target[_RECOVERY_TOKEN_PROP]  # pairing consumed
            except Exception:
                pass  # stamp cleanup is cosmetic - never fail the recovery over it
            logger.warning(
                f"[CUT PLANE] Recovered the uncut mesh '{target.name}' from a "
                f"crash/autosave file (the un-executed preview cut was discarded)")
        except Exception:
            logger.exception(
                f"[CUT PLANE] Crash-recovery restore into '{target.name}' failed")
    _purge_orphan_snapshots()
    return None  # one-shot timer


def _schedule_recovery() -> None:
    """Schedule the crash-recovery restore + purge (load_post only)."""
    try:
        if not bpy.app.timers.is_registered(_recover_orphan_snapshots):
            bpy.app.timers.register(_recover_orphan_snapshots, first_interval=0.0)
    except Exception:
        on_internal_error()


# True between save_pre and save_post iff a preview was active and must be
# re-applied after the write (so we never start a preview that wasn't showing).
_restore_after_save = False
# True when every pre-save session sat at an ARMED baseline - the state
# adopt_committed_baseline establishes after Execute Cut (mesh == baseline,
# live_key armed so the preview is a deliberate no-op at the committed
# plane). save_pre's restore destroys the sessions AND their keys; rebuilding
# via update_live_preview gave fresh sessions with live_key None, whose
# suppression guard can never match - so the preview RE-BISECTED the
# committed mesh at the plane the cut already occupies, and with Fill on
# laid a SECOND coincident cap. One Ctrl+S per extra cap, compounding
# (65 -> 66 -> 67... faces measured), and the next save wrote it to disk.
_readopt_after_save = False


@bpy.app.handlers.persistent
def _on_save_pre(*_args) -> None:
    global _restore_after_save, _readopt_after_save
    _restore_after_save = bool(_sessions)
    _readopt_after_save = bool(_sessions) and all(
        c.mesh_is_baseline and c.live_key is not None
        for c in _sessions.values())
    try:
        restore_live_cut(bpy.context)
    except Exception:
        # Surface: a failure here risks baking the preview cut into the saved file.
        logger.exception("[CUT PLANE] save_pre live-cut restore failed")


@bpy.app.handlers.persistent
def _on_save_post(*_args) -> None:
    global _restore_after_save
    if not _restore_after_save:
        return
    _restore_after_save = False
    try:
        if _readopt_after_save:
            # Reproduce the pre-save state: fresh snapshot of the committed
            # mesh + live_key armed at the current plane/options, exactly
            # what adopt_committed_baseline built after the cut. The plane
            # cannot have moved during a save, so the armed key is current.
            adopt_committed_baseline(bpy.context)
        else:
            update_live_preview(bpy.context)
    except Exception:
        logger.exception("[CUT PLANE] save_post live-cut re-apply failed")


@bpy.app.handlers.persistent
def _on_load_post(*_args) -> None:
    # The cached snapshots point into the previous file's data; drop every
    # session so the watchdog re-establishes previews cleanly for the new file.
    global _restore_after_save, _readopt_after_save
    _restore_after_save = False
    _readopt_after_save = False
    _sessions.clear()
    # Recovery, not a plain purge: a snapshot in a LOADED file means an
    # autosave/crash file with the preview baked in - restore the uncut
    # original from it first (see _recover_orphan_snapshots), then purge.
    _schedule_recovery()


@bpy.app.handlers.persistent
def _on_undo_redo_post(*_args) -> None:
    """Undo/redo restored the mesh to a state the cache knows nothing about.

    Drop the session POINTER-ONLY (mirrors _on_load_post - editing the mesh or
    removing datablocks is unsafe inside an undo/redo handler) so the watchdog
    re-snapshots the real post-undo mesh next frame. Without this, the stale
    snapshot would re-apply an undone cut on the next plane move. The abandoned
    snapshot datablock is reclaimed by the deferred purge. Touches only the
    Python session registry, which Blender's undo does not manage - no conflict.

    BLINK MITIGATION (Clear Inner/Outer): the undo restored the fuller mesh and
    tagged a redraw. If we leave the re-apply to the watchdog, it schedules the
    re-cut from POST_VIEW (during that redraw), so the full mesh renders for one
    frame first - the visible blink. Scheduling the re-apply timer HERE (this
    handler runs BEFORE the redraw) lets it fire sooner, often closing the gap.
    Gated on _last_tool_active - the flag the watchdog caches from its valid-region
    context - so a cut preview is never applied when our tool isn't the active one.
    This handler may itself be region-less, so it must NOT re-derive tool-active;
    when the flag is False we simply fall back to the watchdog (no regression).
    """
    _sessions.clear()
    # The purge is UNCONDITIONAL on purpose: undo can RESURRECT a snapshot
    # datablock into bpy.data from a step where a session existed, while the
    # Python-side registry is already empty - gating on live sessions would
    # leak that snapshot (fake-user, so it would even save). Cheap anyway:
    # the scan is a deferred one-shot timer doing a name-prefix check per
    # mesh, never inside this handler.
    _schedule_purge()
    try:
        ctx = bpy.context
        if (state._last_tool_active
                and getattr(ctx, 'mode', None) == 'EDIT_MESH'
                and ctx.edit_object is not None
                and ctx.edit_object.type == 'MESH'
                and not bpy.app.timers.is_registered(_deferred_sync)):
            state._pending_action = 'apply'
            state._op_pending = True
            bpy.app.timers.register(_deferred_sync, first_interval=0.0)
    except Exception:
        on_internal_error()
    _debug_check('on_undo_redo_post')


_SESSION_HANDLERS = (
    ("save_pre", _on_save_pre),
    ("save_post", _on_save_post),
    ("load_post", _on_load_post),
    ("undo_post", _on_undo_redo_post),
    ("redo_post", _on_undo_redo_post),
)


# Ownership anchor = the addon ROOT package - same rule and rationale as
# state/persistence.py: stale copies from an older layout (these handlers
# lived at cut_plane.cutting pre-split) must still be swept, and the dot
# boundary keeps a foreign "cut_plane_tools" module from ever matching.
_ADDON_ROOT = __package__.rsplit(".", 1)[0]


def _is_ours(handler, name: str) -> bool:
    """True when `handler` is a copy of OUR handler `name`: matched by
    __name__ (reloads create fresh function objects, so identity fails) AND
    by module path - a foreign add-on's handler that happens to share a
    generic name like "_on_save_pre" must never be touched (same rule as
    persistence._is_ours)."""
    mod = getattr(handler, "__module__", "")
    return (getattr(handler, "__name__", None) == name
            and (mod == _ADDON_ROOT or mod.startswith(_ADDON_ROOT + ".")))


def register_cut_session_handlers() -> None:
    """Register the save/load/undo/redo handlers (idempotent; stale reload
    copies are removed first - see _is_ours for the matching rule)."""
    for list_name, fn in _SESSION_HANDLERS:
        handlers = getattr(bpy.app.handlers, list_name)
        handlers[:] = [h for h in handlers if not _is_ours(h, fn.__name__)]
        handlers.append(fn)
    # Clean up snapshot datablocks orphaned by a previous session / dev reload.
    _schedule_purge()


def unregister_cut_session_handlers() -> None:
    """Remove the session handlers (same matching rule clears stale copies
    too) and drop any pending purge/recovery timer."""
    for list_name, fn in _SESSION_HANDLERS:
        handlers = getattr(bpy.app.handlers, list_name)
        handlers[:] = [h for h in handlers if not _is_ours(h, fn.__name__)]
    try:
        if bpy.app.timers.is_registered(_purge_orphan_snapshots):
            bpy.app.timers.unregister(_purge_orphan_snapshots)
        if bpy.app.timers.is_registered(_recover_orphan_snapshots):
            bpy.app.timers.unregister(_recover_orphan_snapshots)
    except Exception:
        on_internal_error()
