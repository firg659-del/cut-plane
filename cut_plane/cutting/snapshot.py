# SPDX-License-Identifier: GPL-3.0-or-later
"""Snapshot lifecycle: take, validate, rebuild-from, and drop the original.

Mesh writes -> write-safe contexts only (transform modals / option callback /
depsgraph / timer); never a draw handler.
"""

import uuid

import bmesh
import bpy
from mathutils import Vector

from ..lib.constants import on_internal_error
from .state import (
    _RECOVERY_TARGET_PROP,
    _RECOVERY_TOKEN_PROP,
    _SNAPSHOT_NAME,
    _sessions,
)


def _free_live_snapshot(cache) -> None:
    snap = cache.live_snapshot
    cache.live_snapshot = None
    cache.live_bvh = None  # snapshot gone -> its BVH is stale
    if snap is not None:
        try:
            bpy.data.meshes.remove(snap)
        except Exception:
            pass  # datablock may already be freed (undo/reload) - teardown must not raise


def _snapshot_valid(cache) -> bool:
    """True if the session's snapshot Mesh datablock is still alive.

    Undo / file reload can free the datablock out from under us, leaving the
    cached Python pointer dangling; accessing any RNA property on a freed
    datablock raises, which is the reliable validity test.
    """
    snap = cache.live_snapshot
    if snap is None:
        return False
    try:
        return snap.users >= 0
    except Exception:
        return False


def _reset_live_state(cache=None) -> None:
    """Drop live-cut session(s) without touching the mesh.

    cache=None drops EVERY session (tests); a specific
    cache drops just that object's session. Frees the snapshot datablock and
    removes the session from the registry.
    """
    targets = [cache] if cache is not None else list(_sessions.values())
    for c in targets:
        _free_live_snapshot(c)
        c.reset()
        for uid, s in list(_sessions.items()):
            if s is c:
                del _sessions[uid]


def _resolve_live_object(cache):
    """The session's object, found by session_uid (rename-proof), or None."""
    uid = cache.live_object_uid
    if uid is None:
        return None
    for ob in bpy.data.objects:
        if ob.session_uid == uid:
            return ob
    return None


def _selection_counts(me) -> tuple:
    """Per-type selection counts of an EDIT mesh, O(1): Blender maintains
    these counters on the edit bmesh (totvertsel & co), kept correct by
    BM_elem_select_set - which every select write in this codebase goes
    through (python .select assignment and select_set both call it)."""
    return (me.total_vert_sel, me.total_edge_sel, me.total_face_sel)


def _record_written_counts(cache, bm, me=None) -> None:
    """Remember the mesh counts as WE wrote them (foreign-edit detection).

    Passing the edit-mesh datablock additionally records the selection
    counts (the selection-change detector, both modes - O(1) via the
    maintained counters)."""
    cache.expected_counts = (len(bm.verts), len(bm.edges), len(bm.faces))
    cache.expected_sel_counts = _selection_counts(me) if me is not None else None


def _selection_changed_since_write(cache, me) -> bool:
    """True when the user changed the selection after our last write."""
    exp = cache.expected_sel_counts
    return exp is not None and _selection_counts(me) != exp


def _foreign_edit_happened(cache, bm) -> bool:
    """True when the edit mesh's topology differs from what we last wrote -
    i.e. the user edited (extrude/knife/delete/...) during the live preview."""
    exp = cache.expected_counts
    return exp is not None and (len(bm.verts), len(bm.edges), len(bm.faces)) != exp


def _live_take_snapshot(cache, obj) -> bool:
    """Store the current edit geometry as a Mesh datablock (the original)."""
    try:
        src = bmesh.from_edit_mesh(obj.data).copy()
    except Exception:
        on_internal_error()  # DEBUG: surface; prod: skip the snapshot, no preview
        return False
    snap = bpy.data.meshes.new(_SNAPSHOT_NAME)
    src.to_mesh(snap)
    src.free()
    snap.use_fake_user = True  # keep it from being orphan-purged mid-session
    # Crash-recovery stamp: which mesh this snapshot restores into. Autosave
    # writes files WITHOUT firing save_pre (verified 2026-07-10), so a crash
    # file contains the baked preview cut AND this snapshot - the stamp lets
    # load-time recovery undo the bake (see handlers._recover_orphan_snapshots).
    # The NAME is display/legacy only; the TOKEN is the identity (see
    # _RECOVERY_TOKEN_PROP for why a name must never be trusted).
    snap[_RECOVERY_TARGET_PROP] = obj.data.name
    token = uuid.uuid4().hex
    snap[_RECOVERY_TOKEN_PROP] = token
    obj.data[_RECOVERY_TOKEN_PROP] = token
    _free_live_snapshot(cache)  # drop any stale snapshot first
    cache.live_snapshot = snap
    cache.live_object_uid = obj.session_uid  # rename-proof identity
    # Selection counts AS SNAPSHOTTED (the copy carries the flags) - the O(1)
    # comparison basis for the selection-adoption check.
    cache.snapshot_sel_counts = _selection_counts(obj.data)
    # By construction the live mesh IS the snapshot right now (every caller
    # snapshots the current mesh: session start, rebase, adoption, commit).
    cache.mesh_is_baseline = True
    cache.written_state = None  # baseline mesh: no cut, no cleared side
    return True


def _selection_matches_snapshot(cache, me) -> bool:
    """True when the live selection has the same per-type counts as the
    selection stored in the snapshot (cached at snapshot time - O(1)). A
    count heuristic (like the foreign-edit check): an equal-count different
    selection slips through, but every practical select/deselect changes a
    count."""
    stored = cache.snapshot_sel_counts
    return stored is None or _selection_counts(me) == stored


def _live_rebuild_from_snapshot(cache, bm) -> bool:
    """Replace the edit bmesh contents with the snapshot.

    Validates the snapshot BEFORE clearing the bmesh, so a freed snapshot can
    never leave the user's geometry emptied.

    SKIPS the rewrite while the mesh IS the baseline (mesh_is_baseline -
    EXPLICIT knowledge set by the write sites, deliberately not a count
    comparison, see CutPreviewCache): rewriting then would only clobber
    LIVE-ONLY state - crucially the selection the user just made, which is
    the Selected-Only cut domain (the select-then-cut flow died to exactly
    this rewrite).
    """
    if not _snapshot_valid(cache):
        return False
    if cache.mesh_is_baseline:
        return True
    try:
        bm.clear()
        bm.from_mesh(cache.live_snapshot)
    except Exception:
        on_internal_error()  # DEBUG: surface; prod: report failure to the caller
        return False
    cache.mesh_is_baseline = True
    return True


def _snapshot_bounds(snap):
    """(lo, hi) local-space bounding corners of a snapshot mesh.

    Caller guarantees snap.vertices is non-empty."""
    lo = Vector(snap.vertices[0].co)
    hi = lo.copy()
    for v in snap.vertices:
        for i in range(3):
            lo[i] = min(lo[i], v.co[i])
            hi[i] = max(hi[i], v.co[i])
    return lo, hi
