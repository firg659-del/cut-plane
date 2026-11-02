# SPDX-License-Identifier: GPL-3.0-or-later
"""Snapshot lifecycle: take, validate, rebuild-from, and drop the original.

Mesh writes -> write-safe contexts only (transform modals / option callback /
depsgraph / timer); never a draw handler.
"""

import array
import hashlib
import uuid
from typing import Optional

import bmesh
import bpy
from mathutils import Vector

from ..lib.constants import logger, on_internal_error
from . import state
from .clip import free_scratch
from .state import (
    _RECOVERY_TARGET_PROP,
    _RECOVERY_TOKEN_PROP,
    _SNAPSHOT_NAME,
    _sessions,
)

# View state, not content: mesh and UV selection, visibility (the
# hidden-geometry rules read it off the snapshot, at snapshot time), and
# the tool's own preview marker.
_FP_SKIP = ('.select_', '.hide_', '.vs.', '.es.', '.uv_select', state._MARKER_PREFIX)
# data_type -> (foreach property, array typecode, values per element)
_FP_LAYOUT = {
    'FLOAT': ('value', 'f', 1), 'INT': ('value', 'i', 1), 'INT8': ('value', 'i', 1),
    'BOOLEAN': ('value', 'b', 1), 'FLOAT2': ('vector', 'f', 2),
    'FLOAT_VECTOR': ('vector', 'f', 3), 'FLOAT_COLOR': ('color', 'f', 4),
    'BYTE_COLOR': ('color', 'f', 4), 'INT16_2D': ('value', 'i', 2),
    'INT32_2D': ('value', 'i', 2), 'QUATERNION': ('value', 'f', 4),
    'FLOAT4X4': ('value', 'f', 16),
}
# Zero users, so never saved. Persistent on purpose: removing a datablock
# rebuilds the depsgraph relations - measured 150 ms per fingerprint in a
# 3000-object scene, against 10 ms for a kept one.
_FP_MESH_NAME = ".cut_plane_fingerprint"
_fp_mesh = None


def _fingerprint_mesh():
    global _fp_mesh
    try:
        if _fp_mesh is not None and _fp_mesh.users == 0:
            return _fp_mesh
    except ReferenceError:
        pass  # freed by undo, a file load or a purge
    # An Object Mode undo can bring back the copy it captured: reuse it
    # rather than pile up .001, .002 ...
    found = bpy.data.meshes.get(_FP_MESH_NAME)
    _fp_mesh = found if found is not None and found.users == 0 else bpy.data.meshes.new(_FP_MESH_NAME)
    return _fp_mesh


def free_fingerprint_mesh() -> None:
    global _fp_mesh
    me, _fp_mesh = _fp_mesh, None
    try:
        if me is not None and me.users == 0:
            bpy.data.meshes.remove(me)
    except Exception:
        pass  # already freed, or bpy.data is restricted (teardown must not raise)


def _hash_mesh(me) -> bytes:
    """Digest of a Mesh's content: winding and every attribute (positions,
    UVs, seams, sharp and smooth flags, materials, creases, colors, custom
    normals) except view state (_FP_SKIP). Vertex-group weights are not
    attributes and are not covered."""
    h = hashlib.blake2b(digest_size=16)
    h.update(repr((len(me.vertices), len(me.edges), len(me.loops), len(me.polygons))).encode())
    starts = array.array('i', bytes(4 * len(me.polygons)))
    me.polygons.foreach_get('loop_start', starts)
    h.update(starts)
    for attr in sorted(me.attributes, key=lambda a: (a.domain, a.name)):
        if attr.name.startswith(_FP_SKIP):
            continue
        h.update(f'{attr.domain}/{attr.data_type}/{attr.name}'.encode())
        layout = _FP_LAYOUT.get(attr.data_type)
        if layout is None:
            continue  # e.g. STRING: its name and type still count
        key, code, width = layout
        buf = array.array(code, bytes(array.array(code).itemsize * len(attr.data) * width))
        attr.data.foreach_get(key, buf)
        h.update(buf)
    return h.digest()


def content_fingerprint(bm) -> bytes:
    """Digest of an edit BMesh's content (see _hash_mesh).

    Counts alone missed every edit that keeps them - flip normals, shade
    smooth, material assign, a menu move, UV unwrap, seams - so the next
    plane move rebuilt from the snapshot and silently reverted the user's
    work. A full BMesh -> Mesh conversion costs 36-72% of a preview apply
    (measured), so it runs only where the user can have edited: never per
    frame while a plane transform owns the input (see state._input_owned).
    """
    me = _fingerprint_mesh()
    bm.to_mesh(me)
    try:
        return _hash_mesh(me)
    finally:
        me.clear_geometry()  # keep the scratch empty between calls


def fingerprint_or_none(source) -> Optional[bytes]:
    """content_fingerprint of a BMesh, or _hash_mesh of a Mesh; None when
    it fails. A failure must fall back to the counts, never skip a restore:
    the restore paths treat an exception as "keep the mesh", which would
    leave the preview baked in."""
    try:
        if isinstance(source, bmesh.types.BMesh):
            return content_fingerprint(source)
        return _hash_mesh(source)
    except Exception:
        on_internal_error()  # DEBUG: surface; prod: counts only
        return None


def _free_live_snapshot(cache) -> None:
    snap = cache.live_snapshot
    cache.live_snapshot = None
    cache.live_bvh = None  # snapshot gone -> its BVH is stale
    cache.snapshot_bbox_center = None  # ...and so is its bbox centre
    # ...and its arrays: a new snapshot can reuse the freed one's address
    # with the same counts, so no stamp of ours could tell them apart.
    cache.clip_arrays = None
    cache.split_arrays = None
    cache.clip_frames = {}
    free_scratch(cache)
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
    """Remember the mesh as WE wrote it (foreign-edit detection): its counts
    and its content fingerprint - deferred to end_owned_input while a plane
    transform owns the input.

    Passing the edit-mesh datablock additionally records the selection
    counts (the selection-change detector, both modes - O(1) via the
    maintained counters)."""
    cache.expected_counts = (len(bm.verts), len(bm.edges), len(bm.faces))
    cache.expected_sel_counts = _selection_counts(me) if me is not None else None
    if state._input_owned:
        cache.content_fp = None
        cache.fp_stale = True
    else:
        cache.content_fp = fingerprint_or_none(bm)
        cache.fp_stale = False
        if cache.content_fp is not None and cache.token and not cache.mesh_is_baseline:
            _remember_written(cache.token, cache.content_fp)


_WRITTEN_PER_TOKEN = 64
_WRITTEN_TOKENS = 256


def _remember_written(token, fp) -> None:
    """A preview state the tool wrote - the only kind repair_marked_mesh
    may replace. Bounded: an undo step older than this is kept as it is."""
    states = state._written_fps.pop(token, None) or []
    if fp not in states:
        states.append(fp)
    state._written_fps[token] = states[-_WRITTEN_PER_TOKEN:]  # re-inserted: newest last
    while len(state._written_fps) > _WRITTEN_TOKENS:
        del state._written_fps[next(iter(state._written_fps))]


def mark_preview(cache, bm, me) -> None:
    """Tag the edit mesh as holding this session's uncommitted preview -
    undo steps store the tag with it (see state._MARKER_PREFIX)."""
    name = state._MARKER_PREFIX + cache.token
    if bm.faces.layers.int.get(name) is None:
        bm.faces.layers.int.new(name)
    state._marked_mesh_uids.add(me.session_uid)


def strip_markers(bm) -> bool:
    """Drop preview markers from a BMesh; True when there were any."""
    names = [n for n in bm.faces.layers.int.keys() if n.startswith(state._MARKER_PREFIX)]
    for name in names:
        bm.faces.layers.int.remove(bm.faces.layers.int[name])
    return bool(names)


def strip_mesh_markers(me) -> None:
    """Drop preview markers from a Mesh outside Edit Mode."""
    for name in [a.name for a in me.attributes if a.name.startswith(state._MARKER_PREFIX)]:
        me.attributes.remove(me.attributes[name])


def _selection_changed_since_write(cache, me) -> bool:
    """True when the user changed the selection after our last write."""
    exp = cache.expected_sel_counts
    return exp is not None and _selection_counts(me) != exp


def _foreign_edit_happened(cache, bm) -> bool:
    """True when the edit mesh differs from what we last wrote - the user
    edited it during the live preview: extrude/knife/delete change the
    counts; flip normals, shade smooth, a material, a move keep them and
    only the content fingerprint tells. While a plane transform owns the
    input nobody else can edit, so only the counts are read there."""
    exp = cache.expected_counts
    if exp is not None and (len(bm.verts), len(bm.edges), len(bm.faces)) != exp:
        return True
    if state._input_owned or cache.content_fp is None:
        return False
    fp = fingerprint_or_none(bm)
    return fp is not None and fp != cache.content_fp


def _live_take_snapshot(cache, obj) -> bool:
    """Store the current edit geometry as a Mesh datablock (the original)."""
    try:
        live = bmesh.from_edit_mesh(obj.data)
        # A marker on the mesh being snapshotted is obsolete: this becomes
        # the baseline - real geometry (an adopted user edit), not a preview.
        if strip_markers(live):
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=True)
        src = live.copy()
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
    cache.token = token
    cache.snapshot_bbox_center = None  # recomputed lazily off the new snapshot
    cache.snapshot_has_hidden = any(
        name in snap.attributes for name in ('.hide_vert', '.hide_edge', '.hide_poly'))
    cache.live_object_uid = obj.session_uid  # rename-proof identity
    # Selection counts AS SNAPSHOTTED (the copy carries the flags) - the O(1)
    # comparison basis for the selection-adoption check.
    cache.snapshot_sel_counts = _selection_counts(obj.data)
    # By construction the live mesh IS the snapshot right now (every caller
    # snapshots the current mesh: session start, rebase, adoption, commit).
    cache.mesh_is_baseline = True
    cache.written_state = None  # baseline mesh: no cut, no cleared side
    return True


def repair_marked_mesh(me) -> Optional[str]:
    """Outside a session, a mesh carrying a preview marker came back
    through undo/redo with an uncommitted cut in it.

    When its content is exactly a preview state the tool wrote, it is
    replaced by that preview's snapshot - the undo step that brought the
    marker back brought the snapshot back into bpy.data too (verified on
    4.5, 5.1 and 5.2 for a click-select, Tab out and Execute) - so the cut
    is gone again. Otherwise the user edited on top of the preview, or the
    snapshot is gone: the mesh stays as it is (WYSIWYG, like any foreign
    edit) and only loses the marker. Returns 'repaired', 'kept' or None.
    """
    if me.session_uid not in state._marked_mesh_uids:
        return None
    bm = bmesh.from_edit_mesh(me) if me.is_editmode else None
    if bm is not None:
        names = [n for n in bm.faces.layers.int.keys() if n.startswith(state._MARKER_PREFIX)]
    else:
        names = [a.name for a in me.attributes if a.name.startswith(state._MARKER_PREFIX)]
    if not names:
        return None
    token = names[0][len(state._MARKER_PREFIX):]
    snap = next((m for m in bpy.data.meshes
                 if m.name.startswith(_SNAPSHOT_NAME) and m.get(_RECOVERY_TOKEN_PROP) == token),
                None)
    fp = fingerprint_or_none(bm if bm is not None else me)
    written = fp is not None and fp in state._written_fps.get(token, ())
    if written and snap is not None:
        if bm is not None:
            replace_bmesh(bm, snap)  # the marker goes with the rest
            bmesh.update_edit_mesh(me, destructive=True)
        else:
            tmp = bmesh.new()
            tmp.from_mesh(snap)
            tmp.to_mesh(me)
            tmp.free()
            me.update()
        outcome = 'repaired'
    else:
        if written:
            logger.warning(f"[CUT PLANE] An undo brought back an un-executed preview cut "
                           f"on '{me.name}' and its original is gone - it could NOT be reverted")
        if bm is not None:
            strip_markers(bm)
            bmesh.update_edit_mesh(me, destructive=True)
        else:
            strip_mesh_markers(me)
        outcome = 'kept'
    if _RECOVERY_TOKEN_PROP in me.keys():
        del me[_RECOVERY_TOKEN_PROP]  # the step carried the session's stamp
    return outcome


def _selection_matches_snapshot(cache, me) -> bool:
    """True when the live selection has the same per-type counts as the
    selection stored in the snapshot (cached at snapshot time - O(1)). A
    count heuristic (like the foreign-edit check): an equal-count different
    selection slips through, but every practical select/deselect changes a
    count."""
    stored = cache.snapshot_sel_counts
    return stored is None or _selection_counts(me) == stored


def replace_bmesh(bm, mesh) -> None:
    """Replace an EDIT bmesh's contents with `mesh`, keeping its select mode.

    bm.clear() zeroes the whole BMesh struct - select_mode included - and
    from_mesh does not set it back, so 1.1.0 left the user's edit mesh with
    an EMPTY select mode after every preview rebuild, still empty after the
    tool was left. Every edit-bmesh rebuild goes through here."""
    select_mode = set(bm.select_mode)
    bm.clear()
    bm.from_mesh(mesh)
    if select_mode:
        bm.select_mode = select_mode


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
        replace_bmesh(bm, cache.live_snapshot)
    except Exception:
        on_internal_error()  # DEBUG: surface; prod: report failure to the caller
        return False
    cache.mesh_is_baseline = True
    return True


def _snapshot_bounds(snap):
    """(lo, hi) local-space bounding corners of a snapshot mesh.

    Caller guarantees snap.vertices is non-empty."""
    # foreach_get fills the whole coordinate buffer in one call; the loop
    # it replaces cost ~6 RNA reads per vertex. Values are the mesh's own
    # float32, so the bounds are bit-identical.
    buf = array.array('f', [0.0]) * (len(snap.vertices) * 3)
    snap.vertices.foreach_get("co", buf)
    lo = Vector((min(buf[0::3]), min(buf[1::3]), min(buf[2::3])))
    hi = Vector((max(buf[0::3]), max(buf[1::3]), max(buf[2::3])))
    return lo, hi
