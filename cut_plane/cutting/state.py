# SPDX-License-Identifier: GPL-3.0-or-later
"""Session state: the per-object cache, the registry, and the shared latches.

Everything mutable that the live-cut subsystem shares lives HERE, in one
module, so every other module has a single import target for it. Two access
rules keep cross-module state truthful:

  * containers (_sessions, and the cache objects inside it) are only ever
    mutated IN PLACE, so ``from .state import _sessions`` aliases stay live;
  * SCALARS (_op_pending, _pending_action, _last_tool_active) are reassigned,
    so writers and readers in other modules must use ``state.<name>``
    attribute access - a ``from``-import would freeze the import-time value.
"""

from typing import Optional


class CutPreviewCache:
    """Per-OBJECT live-cut session state (one instance per edit mesh).

    Big data never lives in properties. The snapshot is a real Mesh datablock
    (the untouched original) so the live bisect can always be undone visually.
    Multi-object edit mode runs one session per mesh - all of them found in
    the module-level _sessions registry, keyed by object session_uid.
    """

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.live_active: bool = False
        self.live_snapshot = None              # bpy Mesh datablock (the original)
        # Local-space bbox centre of that snapshot, for a BBOX anchor. Cached
        # because its readers are HOT - the preview dirty key every frame and
        # the header warning every redraw - while the snapshot it measures
        # only changes on session start, rebase, or a committed cut. Measured
        # uncached on a 40k-vert mesh: 483 ms/frame preview and 182 ms per
        # redraw. Dropped wherever live_snapshot is.
        self.snapshot_bbox_center = None
        self.live_bvh = None                   # BVHTree of the snapshot (for snapping)
        # Object identity by session_uid, NOT name: renaming the object mid-preview
        # must not orphan the session (a name lookup silently baked the preview cut).
        self.live_object_uid: Optional[int] = None
        self.live_key: Optional[tuple] = None  # plane pose + options (dirty check)
        # (verts, edges, faces) as WE last wrote them. A mismatch means the user
        # edited the mesh during the preview - rebase instead of discarding it.
        self.expected_counts: Optional[tuple] = None
        # Per-type SELECTION counts as we last wrote them (and as stored in
        # the snapshot). A mismatch means the user changed the selection -
        # adopt or remap it into the baseline instead of letting a rebuild
        # wipe it (in Selected-Only mode it is also the cut domain).
        self.expected_sel_counts: Optional[tuple] = None
        self.snapshot_sel_counts: Optional[tuple] = None
        # True while the live mesh is EXACTLY the baseline (set on snapshot,
        # recomputed at every preview write). EXPLICIT KNOWLEDGE, never a
        # count comparison: a mid-prism Clear+Fill cut reproduces the
        # baseline's exact counts (verified on a cube: 8/12/6 -> 8/12/6),
        # which made a count-based rebuild skip COMPOUND cuts and bake the
        # preview permanently.
        self.mesh_is_baseline: bool = False
        # The state the CURRENT preview mesh was actually produced with:
        # (planes, clear_inner, clear_outer), planes = the object-LOCAL
        # (co, no) list of the last apply, mirrors included. None while the
        # mesh is the baseline. The selection remap classifies THIS mesh's
        # geometry, so it must be judged against THIS state - live_key is
        # the wrong source (it holds the INCOMING key, is world-space, and
        # is deliberately wiped by refresh_live_preview to force re-applies).
        # Feeding the remap the incoming state instead made its hidden()
        # guard misjudge the previously-cleared side as visible-and-
        # deselected, permanently eroding the Selected-Only domain
        # (measured: unticking Clear Outer deselected 256 of 384 faces).
        self.written_state: Optional[tuple] = None


# The live sessions: object session_uid -> CutPreviewCache. A session in the
# registry IS active (sessions are registered only after their first snapshot
# succeeds and dropped on reset). The watchdog's pending-action latch is
# GLOBAL (one decision covers the whole edit-mode session set) - see below.
_sessions = {}

# Watchdog latch (was on the singleton cache): a deferred apply/restore is
# scheduled / which action the watchdog decided from its valid-region context.
_op_pending = False
_pending_action: Optional[str] = None

# Last tool-active state seen by the watchdog (which runs in a valid-region
# context). The undo/redo handler may be region-less, so it reads this cached
# flag to gate its blink-mitigation re-apply instead of re-deriving tool-active.
_last_tool_active = False


def _session_for(obj):
    """The live session for obj, or None. Rename-proof (session_uid key)."""
    if obj is None:
        return None
    return _sessions.get(obj.session_uid)


def _edit_mesh_objects(context):
    """Every mesh in the multi-object edit session (the preview and the cut
    both apply to all of them); falls back to the active edit object in
    contexts that lack the derived member (timers)."""
    objs = [ob for ob in getattr(context, 'objects_in_mode_unique_data', ())
            if ob.type == 'MESH']
    if objs:
        return objs
    obj = getattr(context, 'edit_object', None)
    return [obj] if obj is not None and obj.type == 'MESH' else []

# Name of the snapshot Mesh datablock (Blender may suffix .001 etc. for
# uniqueness, so matching is by startswith). Single source for creation + purge.
_SNAPSHOT_NAME = "__cut_plane_cut_snapshot__"

# ID property stamped on every snapshot: the NAME of the mesh datablock it was
# taken from. ID properties persist in .blend files, which is the whole point -
# an autosave/crash file carries the snapshot (see handlers), and the stamp is
# what lets the recovery find the mesh to restore into.
_RECOVERY_TARGET_PROP = "cut_plane_recovery_target"
# The rename-proof pairing: one fresh token per snapshot, stamped on BOTH the
# snapshot and its target mesh. Mesh NAMES are unique but neither stable nor
# owned - the name stamp alone made crash recovery (a) miss after an ordinary
# rename (the baked preview cut became permanent, the snapshot holding the
# original was purged, all silently) and (b) OVERWRITE AN UNRELATED MESH when
# the freed name was later taken by another datablock, while logging success.
# ID properties survive renames and file round-trips; tokens cannot collide.
_RECOVERY_TOKEN_PROP = "cut_plane_recovery_token"


def topology_changed() -> None:
    """Invalidate every session's plane key so the next preview re-applies.

    Fallback used by adopt_committed_baseline outside edit mode."""
    for cache in _sessions.values():
        cache.live_key = None
