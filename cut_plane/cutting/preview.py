# SPDX-License-Identifier: GPL-3.0-or-later
"""Preview lifecycle: session start, the per-frame apply, restore, and the
post-commit baseline adoption.

_apply_live_cut orchestrates the phases: ensure session -> dirty check ->
sync user changes into the baseline -> rebuild -> build domain -> cut/clear/
fill/select -> write + bookkeeping. Each phase owns its own rules; the
orchestrator owns only their ORDER. Mesh writes -> write-safe contexts only
(transform modals / option callback / depsgraph / timer); never a draw handler.
"""

import time
from typing import Optional

import bmesh
import bpy

from ..lib.constants import BISECT_DIST, logger, on_internal_error
from . import dialogs, state
from .invariants import _debug_check
from .plane import (
    _mirror_axes,
    _mirrored_planes_world,
    _mirror_pivot_key,
    _plane_key,
    fill_applies,
    get_plane_world,
    plane_to_object_local,
)
from .selection import (
    _remap_selection_to_baseline,
    apply_cut_selection,
    face_select_mode,
    split_plan_from_bmesh,
    split_plan_from_mesh,
    clear_domain_side,
    repair_split_selection,
    reselect_cut_domain,
)
from .clip import clip_arrays, clip_cut, clip_frame, frame_cut, frame_split_plan
from .snap_support import _get_or_build_snapshot_bvh
from .snapshot import (
    _foreign_edit_happened,
    _live_rebuild_from_snapshot,
    _live_take_snapshot,
    _record_written_counts,
    _reset_live_state,
    _resolve_live_object,
    _selection_changed_since_write,
    _selection_matches_snapshot,
    _snapshot_valid,
    fingerprint_or_none,
    mark_preview,
    repair_marked_mesh,
    strip_markers,
    strip_mesh_markers,
)
from .state import (
    _RECOVERY_TOKEN_PROP,
    CutPreviewCache,
    _edit_mesh_objects,
    _session_for,
    _sessions,
    topology_changed,
)


def fill_cut(bm, cut_edges, plane_no):
    """Cap a cut cross-section exactly like native mesh.bisect: triangle_fill
    with dissolve, THEN face_attribute_fill (editmesh_bisect.cc runs both).

    The second step is what 1.1.0 missed. Without it every cap was wound
    along the plane normal, so with Clear Inner it faced INTO the kept solid
    (its rim edges disagreeing with the neighbours' winding), and every cap
    landed on material slot 0, flat, with zeroed UVs. face_attribute_fill
    orients each new face from its neighbours and copies their material,
    smooth flag and corner data. Shared by the live preview and Execute Cut
    so the two can never drift apart again. Returns the fill's output geom.
    """
    sides = {f for e in cut_edges for f in e.link_faces}
    fill_res = bmesh.ops.triangle_fill(
        bm, edges=cut_edges, use_dissolve=True, normal=plane_no)
    geom = fill_res.get('geom', ())
    faces = [f for f in geom if isinstance(f, bmesh.types.BMFace)]
    if faces:
        bmesh.ops.face_attribute_fill(bm, faces=faces, use_normals=True, use_data=True)
        # use_dissolve merges the fill's triangles into n-gons it does not
        # report: find the caps from the cut edges instead.
        caps = {f for f in faces if f.is_valid}
        caps.update(f for e in cut_edges if e.is_valid for f in e.link_faces
                    if f not in sides)
        _settle_cap_attributes(sorted(caps, key=_rounded_center),
                               face_mode=face_select_mode(bm))
    return geom


def _rounded_center(face):
    return tuple(round(c, 5) for c in face.calc_center_median())


def _settle_cap_attributes(caps, face_mode=True):
    """Make each cap's material, smooth flag and corner data independent of
    element order.

    face_attribute_fill copies them from the first neighbour it meets, so
    with neighbours of different materials (alternating slots around a
    rim) the live preview and Execute Cut - which reach the fill with their
    elements in a different order - gave the same cap different materials
    and UVs. Native Bisect's own pick is just as arbitrary. Here a cap
    takes the most common material among its neighbours (ties: the lowest
    slot) and copies the rest from one of those neighbours, chosen by
    geometry, so preview and Cut always agree - and agree with native
    wherever the neighbours do.
    """
    cap_set = set(caps)
    for cap in caps:
        neighbours = {other for edge in cap.edges for other in edge.link_faces
                      if other not in cap_set}
        # Selected only when what surrounds it is: every neighbour (Face
        # mode) or every rim vertex (Vertex/Edge mode, where verts rule).
        want = (bool(neighbours) and all(f.select for f in neighbours)
                if face_mode else all(v.select for v in cap.verts))
        if cap.select != want:
            # Deselecting a BMFace deselects its verts and edges too, which
            # took a shared rim vertex from the cap next to it: keep theirs.
            rim = [(v, v.select) for v in cap.verts] + [(e, e.select) for e in cap.edges]
            cap.select = want
            for elem, was in rim:
                if was:
                    elem.select = True
        if not neighbours:
            continue
        counts = {}
        for face in neighbours:
            counts[face.material_index] = counts.get(face.material_index, 0) + 1
        material = min(counts, key=lambda m: (-counts[m], m))
        sources = sorted((f for f in neighbours if f.material_index == material),
                         key=_rounded_center)
        cap.material_index = material
        cap.smooth = sources[0].smooth
        for loop in cap.loops:
            # The same vertex's corner in the first source face that has it,
            # else in any neighbour around that vertex (by geometry).
            around = [f for f in loop.vert.link_faces if f not in cap_set]
            ordered = [f for f in sources if f in around] or sorted(around, key=_rounded_center)
            if not ordered:
                continue
            corner = next(lp for lp in ordered[0].loops if lp.vert == loop.vert)
            loop.copy_from(corner)


def _plane_count(sc) -> int:
    """How many planes a re-cut really bisects: the mirrored set after its
    dedup - a mirror that maps the plane onto itself adds no cut, and used
    to halve the density budget anyway ('mesh too dense' on one plane)."""
    axes = _mirror_axes(sc)
    if not axes:
        return 1
    try:
        context = bpy.context
        plane = get_plane_world(context)
        if plane is not None:
            return max(1, len(_mirrored_planes_world(context, plane[0], plane[1],
                                                     axes, sc)))
    except Exception:
        pass        # no usable context: the toggles' upper bound
    return 1 << len(axes)


def live_preview_blocker(obj) -> Optional[str]:
    """Why the live preview cannot run for obj, or None when it can.

    Single source shared by the per-apply guard and the tool-header
    warning (tool.py), so the UI never disagrees with the engine:
      * shape keys - the rebuild round-trip silently corrupts key data
      * density cap - a per-move bisect above the preferences face limit
        would stall the viewport (peeked cheaply, before any snapshot
        copy). Mirror planes multiply the per-move bisect cost, so they
        shrink the budget the same way: 4 planes = 1/4 the cap.
    Execute Cut works in every case (one-shot, native bisect, uncapped).
    """
    if obj is None or obj.type != 'MESH':
        return None
    if getattr(obj.data, 'shape_keys', None) is not None:
        return "shape keys"
    from ..state.preferences import preview_face_cap
    wms = bpy.data.window_managers
    sc = getattr(wms[0], 'cut_plane', None) if wms else None
    planes = _plane_count(sc)
    cache = _session_for(obj)
    if cache is not None and _snapshot_valid(cache):
        # The next apply rebuilds from the BASELINE and bisects that - so
        # the budget question is about the snapshot, not the live mesh
        # (which is the PREVIOUS cut's output: splits and fill caps add
        # faces). Measuring the output made a baseline just under the cap
        # block itself on alternate refreshes: cut -> over cap -> restore ->
        # under cap -> re-cut -> ...
        n_faces = len(cache.live_snapshot.polygons)
    else:
        n_faces = len(bmesh.from_edit_mesh(obj.data).faces)
    if n_faces > preview_face_cap() // planes:
        return "mesh too dense"
    return None


# One warning per (object, reason): the session guard runs on every plane
# move, so an un-deduped log would spam the console per mousemove. A SET,
# not a single slot: in multi-object edit several meshes can be blocked at
# once and a slot would alternate-spam between them.
_preview_block_warned = set()


def _warn_preview_blocked(obj, blocker: str) -> None:
    key = (obj.session_uid, blocker)
    if key not in _preview_block_warned:
        _preview_block_warned.add(key)
        logger.warning(
            f"[CUT PLANE] Live preview disabled for '{obj.name}': {blocker} "
            f"(Execute Cut still works)")


def _ensure_live_session(obj):
    """Preview phase 1: a live session with a VALID snapshot for obj.

    Starts the session on first contact (the BLOCKER gate runs in
    _apply_live_cut, before this - per apply, not per start, so a session
    that outgrows the budget mid-flight ends instead of stalling). Drops a
    session whose snapshot was freed by undo/reload - the watchdog
    re-snapshots the current mesh next frame. Returns the session cache, or
    None = no preview for this object this frame.
    """
    cache = _session_for(obj)
    if cache is None:
        try:
            # Undo/redo may have brought back a preview cut: never adopt it
            # as the baseline (the post-undo purge normally got there first).
            repair_marked_mesh(obj.data)
        except Exception:
            on_internal_error()  # DEBUG: surface; prod: the session still starts
        cache = CutPreviewCache()
        if not _live_take_snapshot(cache, obj):
            return None
        # Preview runs for this object again - re-arm its warnings
        _preview_block_warned.difference_update(
            {k for k in _preview_block_warned if k[0] == obj.session_uid})
        cache.live_active = True
        cache.live_key = None
        _sessions[obj.session_uid] = cache
    if not _snapshot_valid(cache):
        _reset_live_state(cache)
        return None
    return cache


def _fold_pending_selection(cache, obj, bm, selected_only=None) -> None:
    """Write a PENDING live selection change into the snapshot baseline.

    The shared half of _sync_baseline's remap branch, also run by
    _restore_session: the baseline is what a restore rebuilds and what
    Execute Cut bisects, so a selection made since the last preview write
    must reach it BEFORE either happens. Without this, selecting faces and
    immediately pressing Cut reported "nothing selected to cut" while the
    user stared at their selection (the restore reverted it first), and
    leaving the tool reverted the selection the same way.
    """
    if not _selection_changed_since_write(cache, obj.data):
        return
    if selected_only is None:
        wms = bpy.data.window_managers
        sc = getattr(wms[0], 'cut_plane', None) if wms else None
        selected_only = sc is not None and sc.cut_selected_only
    planes, prev_ci, prev_co = (cache.written_state or (None, False, False))
    _remap_selection_to_baseline(cache, bm, planes, prev_ci, prev_co,
                                 selected_only=selected_only)


def selection_change_pending(obj) -> bool:
    """True when the live selection differs from the last preview write -
    i.e. the baseline is stale until the next refresh/restore folds it.
    O(1) count compare; UI truth-telling reads the LIVE selection then."""
    cache = _session_for(obj)
    return (cache is not None and _snapshot_valid(cache)
            and _selection_changed_since_write(cache, obj.data))


def _sync_baseline(cache, obj, bm, selected_only=False) -> bool:
    """Preview phase 2: fold USER CHANGES into the baseline pre-rebuild.

    Foreign-edit rebase: the user edited the mesh during the preview
    (extrude, knife, delete, ...). NEVER discard their work by rebuilding
    from the stale snapshot - adopt the CURRENT mesh (their edit + the
    preview they were looking at, WYSIWYG) as the new baseline.

    Selection changes must reach the baseline too, in BOTH modes, or the
    next rebuild wipes them: in Selected-Only they are the cut domain, in
    standard mode they are simply the user's selection (the smooth-
    experience rule - the tool never loses a (de)selection). Two
    topologies, two mechanisms: at baseline topology adopt by
    re-snapshotting; at preview topology remap by geometry.

    The remap reads cache.written_state - the planes and clear flags the
    mesh in `bm` was PRODUCED with - deliberately NOT the incoming
    refresh's state (this function no longer even receives it). `bm` still
    holds the PREVIOUS apply's result here (_live_rebuild_from_snapshot
    runs after), and the dirty check guarantees the incoming key DIFFERS
    from the one that produced it - so judging its geometry against the
    incoming plane/flags misfired the hidden() guard on everything between
    the two states and wrote the loss into the snapshot, permanently
    eroding the Selected-Only domain (one Clear-checkbox untick deselected
    256 of 384 faces; the tool's own Ctrl+Z plane-jump did the same).
    False = session reset, bail.
    """
    if _foreign_edit_happened(cache, bm):
        if not _live_take_snapshot(cache, obj):
            _reset_live_state(cache)
            return False
        return True
    if cache.mesh_is_baseline:
        if not _selection_matches_snapshot(cache, obj.data):
            if not _live_take_snapshot(cache, obj):
                _reset_live_state(cache)
                return False
    else:
        _fold_pending_selection(cache, obj, bm, selected_only=selected_only)
    return True


def bmesh_has_hidden(bm) -> bool:
    """True when any element of bm is hidden. A hidden face does not imply a
    hidden vert or edge (shared ones stay visible), so all three are asked.
    O(n) worst case - for one-shot paths; the per-frame preview reads its
    snapshot's cached answer instead (CutPreviewCache.snapshot_has_hidden)."""
    return (any(f.hide for f in bm.faces) or any(e.hide for e in bm.edges)
            or any(v.hide for v in bm.verts))


def _build_domain(bm, selected_only: bool, include_verts: bool = True,
                  visible_only: bool = False):
    """Preview phase 3: the geometry the bisect operates on.

    Standard mode: the whole VISIBLE mesh. Hidden geometry is never cut or
    cleared - native Bisect cannot reach it (it is selection-scoped and
    hidden elements cannot be selected), and hiding a part to protect it is
    the ordinary Blender habit. 1.1.0 passed hidden elements too, so a
    Clear silently deleted a hidden part on the cleared side, invisible in
    the preview. visible_only=False keeps the plain-slice fast path for the
    usual mesh with nothing hidden (the filter costs ~65% more per plane at
    the 300k-face cap). Selected Only: the selection = the cut's
    INPUT domain (native mesh.bisect semantics) - the rebuild just restored
    the BASELINE selection, so the domain is stable across plane moves for
    the whole session; bisect_plane touches only the geom it is given, so
    unselected geometry is never cut, cleared, or filled. Returns None for
    an empty Selected-Only domain: nothing selected = nothing cut
    (explicit; no silent whole-mesh fallback).

    include_verts: for a plain SPLIT (no Clear) edges+faces is the MINIMAL
    sufficient geom - bisect_plane derives the new cut verts from the edges it
    splits, so the whole vert list is redundant. Dropping it is byte-identical
    output but skips building ~1/3 of the per-move geom list (~15% off each
    re-cut on dense meshes), with zero behavior change. A CLEAR deletes the
    verts on one side, so it MUST see them - the caller passes include_verts
    True whenever Clear Inner/Outer is on.
    """
    if not selected_only:
        if visible_only:
            geom = [e for e in bm.edges if not e.hide] + [f for f in bm.faces if not f.hide]
            if include_verts:
                geom = [v for v in bm.verts if not v.hide] + geom
            return geom
        if include_verts:
            return bm.verts[:] + bm.edges[:] + bm.faces[:]
        return bm.edges[:] + bm.faces[:]
    geom = ([v for v in bm.verts if v.select]
            + [e for e in bm.edges if e.select]
            + [f for f in bm.faces if f.select])
    return geom or None


def _clip_arrays(cache, obj):
    """The fast cut's arrays for this session's snapshot, or None when the
    exact path must serve it (see cutting/clip.py). Built once per
    snapshot (~40 ms at 126k faces) and dropped with it."""
    arr = cache.clip_arrays
    if arr is None:
        try:
            arr = clip_arrays(obj, cache.live_snapshot) or False
        except Exception:
            on_internal_error()  # DEBUG: surface; prod: the exact path serves it
            arr = False
        cache.clip_arrays = arr
    return arr or None


def _drag_frame(cache, snap, arr, p_no, clear_inner, clear_outer):
    """Mid-drag, once the plane's normal repeats (a translate keeps it): the
    snapshot sorted along that normal (clip.ClipFrame), through which the
    cut costs what the cut touches, not the whole mesh. Clear Inner is
    Clear Outer of the negated normal. None: cut without one."""
    if not state._input_owned or (clear_inner and clear_outer):
        return None
    no = -p_no if clear_inner else p_no
    key = tuple(no)
    frame = cache.clip_frames.get(key)
    if frame is None and key == cache.clip_last_normal:
        if len(cache.clip_frames) >= 2:
            cache.clip_frames = {}
        frame = cache.clip_frames[key] = clip_frame(snap, arr, no)
    cache.clip_last_normal = key
    return frame


def _apply_live_cut(context, obj, clear_inner, clear_outer, use_fill,
                    plane_co, plane_no) -> None:
    """Snapshot once, then restore + real-bisect at the current plane.

    Neither clear flag splits (keeps both sides); either removes that side.
    use_fill caps the cut cross-section, so the preview matches what Execute
    commits. The new cut loop is selected (when Select Cut Loop is on) so it
    reads like a fresh bisect.

    Orchestrates the phases: ensure session -> dirty check -> sync user
    changes into the baseline -> rebuild -> build domain -> cut/clear/fill/
    select -> write + bookkeeping. Each phase owns its own rules; this
    function owns only their ORDER.
    """
    try:
        blocker = live_preview_blocker(obj)
    except Exception:
        on_internal_error()  # DEBUG: surface; prod: skip the preview this frame
        return
    if blocker is not None:
        _warn_preview_blocked(obj, blocker)
        cache = _session_for(obj)
        if cache is not None:
            # The session outgrew the budget mid-flight (e.g. a mirror
            # toggle multiplied the plane count on a near-cap mesh): end it
            # and RESTORE the original, so the engine never keeps previewing
            # what the UI says it refuses.
            _restore_session(cache)
        return

    cache = _ensure_live_session(obj)
    if cache is None:
        return

    sc = getattr(context.window_manager, 'cut_plane', None)
    mirror_axes = _mirror_axes(sc)
    key = (_plane_key(plane_co, plane_no), clear_inner, clear_outer, use_fill,
           mirror_axes, _mirror_pivot_key(obj, sc))
    if key == cache.live_key:
        return

    try:
        bm = bmesh.from_edit_mesh(obj.data)
    except Exception:
        on_internal_error()  # DEBUG: surface; prod: skip the preview this frame
        return

    selected_only = sc is not None and sc.cut_selected_only

    if not _sync_baseline(cache, obj, bm, selected_only=selected_only):
        return

    # Hidden geometry is outside the domain (see _build_domain). The
    # bisect's own vert-level clear would still delete a hidden face that
    # shares a vertex with the cleared visible part, so with anything hidden
    # the preview clears face by face - exactly what Execute Cut does - and
    # the two stay identical. Nothing hidden keeps the fast native clear.
    hidden = cache.snapshot_has_hidden
    face_granular = selected_only or hidden
    co_l, no_l = plane_to_object_local(obj, plane_co, plane_no)
    nl = no_l.length
    # The fast cut (cutting/clip.py) builds the FIRST plane's result
    # straight from the snapshot, so the full rebuild is skipped with it.
    # Its mesh is the exact one - but a Fill cap is not decided by the mesh
    # alone: on a tie (a plane exactly through a symmetric vertex ring)
    # triangle_fill's dissolve follows the elements' memory layout, which
    # differs (measured on a torus: caps of 10 + 42 corners exact, 9 + 43
    # fast - and 9 + 43 from the exact mesh too once compacted). Cut runs
    # the exact path, so with Fill the fast cut serves only mid-drag, and
    # the drag's end re-cuts exactly (end_owned_input).
    fast = (None if face_granular or nl < 1e-9 or not _snapshot_valid(cache)
            or (use_fill and not state._input_owned)
            else _clip_arrays(cache, obj))
    fast_used = False
    if fast is None and not _live_rebuild_from_snapshot(cache, bm):
        _reset_live_state(cache)  # snapshot vanished; don't flush a half-built bmesh
        return

    if nl < 1e-9:
        bmesh.update_edit_mesh(obj.data, destructive=True)
        _record_written_counts(cache, bm, me=obj.data)
        cache.written_state = None  # rebuild restored the baseline, no cut
        return
    no_l = no_l / nl
    # The mirror toggles expand the one plane into its reflections. They are
    # derived in WORLD space from the ACTIVE object's frame, so the whole
    # session is cut at the SAME set - exactly as it is cut at the same real
    # plane - and each object just converts them into its own space. Derived
    # per object, "Mirror X" meant a different world direction for every
    # differently rotated object.
    planes_l = []
    for c_w, n_w in _mirrored_planes_world(context, plane_co, plane_no,
                                           mirror_axes, sc):
        c_l, n_l = plane_to_object_local(obj, c_w, n_w)
        if n_l.length_squared < 1e-18:
            continue                      # degenerate for THIS object - skip
        planes_l.append((c_l, n_l.normalized()))
    if not planes_l:
        planes_l = [(co_l, no_l)]

    # The baseline's counts: the rebuilt bmesh's, or the snapshot's when
    # the fast cut skipped the rebuild.
    snap = cache.live_snapshot
    pre_counts = (len(snap.vertices), len(snap.edges), len(snap.polygons))
    any_cut = False
    any_fill = False
    face_mode = face_select_mode(bm)
    # Select Cut Loop flushes after the repair in Vertex/Edge mode, and the
    # repair after the fast cut: only the last of those flushes counts there
    # (a flush derives edges and faces from verts, or faces from edges).
    loop_flushes = sc is None or sc.cut_select_loop
    for plane_index, (p_co, p_no) in enumerate(planes_l):
        plan = None
        frame = (_drag_frame(cache, snap, fast, p_no, clear_inner, clear_outer)
                 if plane_index == 0 and fast is not None else None)
        if not selected_only:
            # The selection the bisect is about to lose, from the uncut mesh:
            # the snapshot for the first plane (the rebuilt bmesh IS it), the
            # bmesh itself for a later mirrored one.
            if frame is not None:
                plan = frame_split_plan(frame, p_co, BISECT_DIST, face_mode)
            elif plane_index == 0 and _snapshot_valid(cache):
                snap = cache.live_snapshot
                # The arrays belong to ONE snapshot: a foreign edit re-takes it.
                stamp = (snap.as_pointer(), len(snap.vertices), len(snap.edges))
                cached = getattr(cache, 'split_arrays', None)
                plan, arrays = split_plan_from_mesh(
                    snap, p_co, p_no, BISECT_DIST, face_mode,
                    arrays=cached[1] if cached and cached[0] == stamp else None)
                cache.split_arrays = (stamp, arrays)
            else:
                plan = split_plan_from_bmesh(bm, p_co, p_no, BISECT_DIST, face_mode)
        res = None
        if plane_index == 0 and fast is not None:
            cache.mesh_is_baseline = False      # about to be rewritten
            try:
                marker = state._MARKER_PREFIX + cache.token
                if frame is not None:
                    cut = frame_cut(cache, bm, fast, frame, p_co, BISECT_DIST,
                                    clear_inner or clear_outer, flush=face_mode,
                                    marker=marker)
                else:
                    cut = clip_cut(cache, bm, snap, fast, p_co, p_no, BISECT_DIST,
                                   clear_inner, clear_outer, flush=face_mode,
                                   marker=marker)
            except Exception:
                on_internal_error()  # DEBUG: surface; prod: the exact path below
                cut = None
            if cut is not None:
                res = {'geom_cut': cut}
                fast_used = True
            elif not _live_rebuild_from_snapshot(cache, bm):
                # Refused (or failed): the exact path needs the rebuild the
                # fast one skipped.
                _reset_live_state(cache)
                return
        if res is None:
            # Verts only needed in the bisect geom when a Clear must delete a
            # side; a plain split derives them from the cut edges (cheaper list).
            # Rebuilt per plane: a bisect invalidates prior element lists.
            geom = _build_domain(bm, selected_only,
                                 include_verts=(clear_inner or clear_outer),
                                 visible_only=hidden)
            if geom is None:
                # Empty Selected-Only domain - the rebuild already restored the
                # uncut mesh; just flush and remember the key.
                bmesh.update_edit_mesh(obj.data, destructive=True)
                _record_written_counts(cache, bm, me=obj.data)
                cache.live_key = key
                cache.written_state = None  # mesh is the baseline, no cut
                return

            # The mesh is about to be mutated - drop baseline-ness NOW so a
            # failure mid-bisect can never masquerade as "still the baseline"
            # (the recovery rebuild must actually rewrite). Recomputed at the end.
            cache.mesh_is_baseline = False
            try:
                # Selected Only (and any mesh with hidden parts) clears via
                # clear_domain_side (face-granular) instead of the bisect's own
                # vert-level clear, which would also kill faces OUTSIDE the
                # domain (unselected or hidden) sharing its boundary verts.
                res = bmesh.ops.bisect_plane(
                    bm, geom=geom, dist=BISECT_DIST, plane_co=p_co, plane_no=p_no,
                    clear_inner=clear_inner and not face_granular,
                    clear_outer=clear_outer and not face_granular)
            except Exception:
                # Recover the clean mesh first (never leave a half-bisected
                # preview), then surface under DEBUG.
                _live_rebuild_from_snapshot(cache, bm)
                bmesh.update_edit_mesh(obj.data, destructive=True)
                _record_written_counts(cache, bm, me=obj.data)
                on_internal_error()
                return
            if face_granular and (clear_inner or clear_outer):
                clear_domain_side(bm, res.get('geom', ()), p_co, p_no,
                                  clear_inner, clear_outer)

        geom_cut = res.get('geom_cut', ())
        if not selected_only:
            # Before the fill, as Execute does: the caps are no split halves.
            # With a Clear active, the kept half may be the NEW face (its
            # selected sibling died inside the bisect) - the snapshot
            # recovers the source's selection.
            needs_snapshot = clear_inner or clear_outer
            repair_split_selection(
                bm, geom_cut, plan,
                bvh=_get_or_build_snapshot_bvh(cache) if needs_snapshot else None,
                snap_polygons=(cache.live_snapshot.polygons
                               if needs_snapshot and _snapshot_valid(cache) else None),
                flush=not loop_flushes)
        fill_geom = ()
        if use_fill:
            # Preview honesty: Execute caps the cross-section when Fill is
            # on, so the preview must too (it used to stay hollow - preview
            # lied). triangle_fill(use_dissolve) is NATIVE Bisect's fill
            # (mesh_bisect.cc); edgenet_fill only capped simple boundary
            # loops - it skipped interior membranes (no Clear) and
            # non-manifold cross-sections entirely (verified against
            # bpy.ops.mesh.bisect, 14/14 parity cases).
            cut_edges = [g for g in geom_cut
                         if isinstance(g, bmesh.types.BMEdge)]
            if cut_edges:
                try:
                    fill_geom = fill_cut(bm, cut_edges, p_no)
                except Exception:
                    on_internal_error()  # DEBUG: surface; prod: hollow beats none

        if selected_only:
            # Selection is the INPUT domain: never rewrite it with the
            # loop-select OUTPUT (it would become the next rebuild's domain
            # and shrink every subsequent cut to the loop). Instead keep the
            # surviving domain fully selected, including the cut's new
            # elements - which also feeds the NEXT mirrored plane's domain.
            reselect_cut_domain(res.get('geom', ()), geom_cut, fill_geom)
        else:
            apply_cut_selection(
                geom_cut, select=(sc is None or sc.cut_select_loop), bm=bm)

        any_cut = any_cut or bool(geom_cut)
        any_fill = any_fill or bool(fill_geom)

    # Exact baseline-ness: this apply left the mesh untouched only if no
    # plane produced a cut, nothing was filled, and no clear removed
    # anything. The count compare is WITHIN this apply (same mesh moments
    # apart), so the baseline-vs-preview count collision cannot fool it.
    cache.mesh_is_baseline = (
        not any_cut and not any_fill
        and (len(bm.verts), len(bm.edges), len(bm.faces)) == pre_counts)
    if not cache.mesh_is_baseline:
        mark_preview(cache, bm, obj.data)  # undo steps store it (see state)
    elif fast_used:
        # The fast cut creates the marker on the way in; a plane off the
        # mesh leaves the baseline, which must carry none.
        strip_markers(bm)

    bmesh.update_edit_mesh(obj.data, destructive=True)
    _record_written_counts(cache, bm, me=obj.data)
    cache.live_key = key
    cache.fill_tied = fast_used and use_fill
    # What the mesh NOW holds, for the next selection remap: the full local
    # plane list (each mirrored plane cleared its own side) + clear flags.
    # Copies, not references: the caller's vectors get mutated per-frame.
    cache.written_state = (
        tuple((p_co.copy(), p_no.copy()) for p_co, p_no in planes_l),
        clear_inner, clear_outer)


def restore_live_cut(context) -> None:
    """Restore the original meshes and end EVERY live-cut session (any mode).

    If a snapshot was freed (undo/reload) we can't restore from it - just drop
    that session cleanly rather than crash; the mesh keeps whatever state it's in.
    `context` is unused - kept so every preview entry point shares the
    update_live_preview / adopt_committed_baseline signature.
    """
    cancel_paced()      # a paced cut must not re-apply after the restore
    for cache in list(_sessions.values()):
        _restore_session(cache)
    _debug_check('restore_live_cut')


def _restore_session(cache) -> None:
    """Restore ONE session's original mesh, then drop the session."""
    obj = _resolve_live_object(cache)  # by session_uid - survives renames
    if obj is not None and obj.type == 'MESH' and _snapshot_valid(cache):
        # obj.mode covers every object in a multi-object edit session, not
        # just the active one (the old check compared against edit_object).
        if obj.mode == 'EDIT':
            try:
                bm = bmesh.from_edit_mesh(obj.data)
                if _foreign_edit_happened(cache, bm):
                    # The user edited during the preview; restoring the stale
                    # snapshot would discard their work. Keep the mesh exactly
                    # as they see it (WYSIWYG) and just drop the session.
                    if strip_markers(bm):
                        bmesh.update_edit_mesh(obj.data, destructive=True)
                else:
                    # A selection made since the last preview write lives
                    # only on the LIVE mesh - fold it into the baseline
                    # first, or the rebuild reverts it (Execute Cut restores
                    # through here, so an un-folded selection made it report
                    # "nothing selected to cut" at a fully selected mesh).
                    _fold_pending_selection(cache, obj, bm)
                    if _live_rebuild_from_snapshot(cache, bm):
                        bmesh.update_edit_mesh(obj.data, destructive=True)
            except Exception:
                # Teardown path: log (visible even outside DEBUG) but never let a
                # restore hiccup abort the exit; the mesh keeps its current state.
                logger.exception("[CUT PLANE] Live-cut restore (edit mode) failed")
        else:  # object mode: overwrite the mesh datablock from the snapshot
            try:
                me = obj.data
                counts = (len(me.vertices), len(me.edges), len(me.polygons))
                if cache.expected_counts is not None and counts != cache.expected_counts:
                    # Foreign edit happened before leaving edit mode - keep the
                    # user's mesh as-is (WYSIWYG), same rule as the edit branch.
                    strip_mesh_markers(me)
                elif cache.content_fp is not None and fingerprint_or_none(me) not in (
                        None, cache.content_fp):
                    strip_mesh_markers(me)  # ...and one that kept the counts
                elif cache.mesh_is_baseline:
                    pass  # no preview delta - a rewrite would only clobber
                    # live-only state (selection); same rule as the edit branch
                else:
                    tmp = bmesh.new()
                    tmp.from_mesh(cache.live_snapshot)
                    tmp.to_mesh(obj.data)
                    tmp.free()
                    obj.data.update()
            except Exception:
                logger.exception("[CUT PLANE] Live-cut restore (object mode) failed")
    # Session over: the recovery token has no snapshot to pair with anymore.
    # Popping it keeps saved files clean (a new session stamps a fresh one).
    try:
        if (obj is not None and obj.type == 'MESH'
                and _RECOVERY_TOKEN_PROP in obj.data.keys()):
            del obj.data[_RECOVERY_TOKEN_PROP]
    except Exception:
        pass  # obj.data may be freed mid-teardown (undo/reload) - stamp is cosmetic
    _reset_live_state(cache)


# Drag pacing for dense meshes. A re-cut costs O(whole mesh) - every bmesh
# operator allocates per-element flags for the whole mesh (~0.4 s per op at
# 500k faces, measured) - so cutting on every mouse move froze the drag,
# gizmo included. While a transform owns the input and the last re-cut took
# longer than PACE_BUDGET_MS, a move only records the plane: the cut runs
# once the pointer rests PACE_REST_S, at most every other cut's length while
# it keeps moving (half the time stays with the UI), and always when the
# drag ends (end_owned_input).
PACE_BUDGET_MS = 50.0
PACE_REST_S = 0.12
_last_cost_ms = 0.0
_last_cut_end = 0.0
_paced_request = 0.0
_paced_window = None


def update_live_preview(context) -> None:
    """Re-cut at the current plane - paced during a drag on a dense mesh
    (see PACE_BUDGET_MS), immediately otherwise."""
    if state._input_owned and _last_cost_ms > PACE_BUDGET_MS:
        # Every other cut's length, as above: the rest delay is for a
        # resting pointer only. Flooring this at PACE_REST_S held a moving
        # drag to 1 / (0.12 s + cut) - ~6 cuts a second even once a cut
        # took 55 ms.
        gap = _last_cost_ms / 1000.0
        if time.perf_counter() - _last_cut_end < gap:
            _arm_paced(context)
            return
    _timed_update(context)


def _timed_update(context) -> None:
    global _last_cost_ms, _last_cut_end
    sessions = len(_sessions)
    begin = time.perf_counter()
    _update_now(context)
    end = time.perf_counter()
    cost = (end - begin) * 1000.0
    # A no-op (unchanged plane) says nothing, and neither does a cut that
    # started a session: its one-time snapshot made a light mesh look slow.
    if cost > 1.0 and len(_sessions) <= sessions:
        _last_cost_ms, _last_cut_end = cost, end


def _arm_paced(context) -> None:
    global _paced_request, _paced_window
    _paced_request = time.perf_counter()
    _paced_window = getattr(context, 'window', None)
    if not bpy.app.timers.is_registered(_paced_tick):
        bpy.app.timers.register(_paced_tick, first_interval=PACE_REST_S)


def _paced_tick():
    """The pointer rested: cut at the plane it rested on."""
    wait = PACE_REST_S - (time.perf_counter() - _paced_request)
    if wait > 0.005:
        return wait
    try:
        window = _paced_window
        ctx = bpy.context
        if window is not None and any(w == window for w in ctx.window_manager.windows):
            with ctx.temp_override(window=window):
                _timed_update(bpy.context)
        else:
            _timed_update(ctx)
        _tag_view3d_redraw()
    except Exception:
        on_internal_error()  # DEBUG: surface; prod: the drag's end cuts anyway
    return None


def flush_paced(context=None) -> None:
    """Apply a paced cut now (the drag ended): nothing may stay behind."""
    if bpy.app.timers.is_registered(_paced_tick):
        bpy.app.timers.unregister(_paced_tick)
        try:
            _timed_update(context if context is not None else bpy.context)
            _tag_view3d_redraw()
        except Exception:
            on_internal_error()


def cancel_paced() -> None:
    if bpy.app.timers.is_registered(_paced_tick):
        bpy.app.timers.unregister(_paced_tick)


def _tag_view3d_redraw() -> None:
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _update_now(context) -> None:
    """Refresh the live-cut preview at the current plane / clear side.

    Write-safe and dirty-checked; only call from write-safe contexts (transform
    modals, option callback, depsgraph) - never a draw handler. Cuts immediately
    on every call, so the preview tracks the plane with no pacing.
    """
    if dialogs.operator_file_browser_open():
        # An export would write the preview into its file: show the
        # original until the browser closes (see dialogs).
        pause_for_dialog(context)
        return
    sc = getattr(context.window_manager, 'cut_plane', None)
    if sc is None:
        return
    plane = get_plane_world(context)
    if plane is None:
        return
    # EVERY mesh in the multi-object edit session gets its own preview
    # session (per-object snapshot/baseline; a blocked object skips itself)
    for obj in _edit_mesh_objects(context):
        _apply_live_cut(context, obj, sc.cut_clear_inner, sc.cut_clear_outer,
                        fill_applies(sc.cut_use_fill, sc.cut_clear_inner,
                                     sc.cut_clear_outer), plane[0], plane[1])
    _debug_check('update_live_preview')


def sessions_armed() -> bool:
    """True when every session sits at the ARMED baseline Execute Cut leaves
    (mesh == baseline, key armed at the committed plane - see
    adopt_committed_baseline). Such a state comes back by re-adoption: a
    plain re-apply re-bisects the committed mesh at the plane the cut
    already occupies, and with Fill lays a second coincident cap."""
    return bool(_sessions) and all(
        c.mesh_is_baseline and c.live_key is not None for c in _sessions.values())


def pause_for_dialog(context) -> None:
    """Restore the original while an operator's file browser is open, and
    remember how resume_after_dialog brings the preview back."""
    if _sessions:
        state._dialog_resume = (
            ('readopt', {uid: c.live_key for uid, c in _sessions.items()})
            if sessions_armed() else ('apply', None))
        restore_live_cut(context)
    elif state._dialog_resume is None:
        state._dialog_resume = ('apply', None)  # a preview was asked for meanwhile
    from .watchdog import ensure_dialog_watch
    ensure_dialog_watch()  # it resumes once the browser closes


def resume_after_dialog(context) -> None:
    """The browser closed: bring back what pause_for_dialog stopped.

    An armed baseline is re-adopted with its pre-pause keys, so a plane or
    option change made meanwhile still previews and an unchanged one stays
    a no-op. Skipped when the tool or Edit Mode was left meanwhile, or when
    something already started a new preview - adopting that mesh as the
    baseline would bake its uncommitted cut.
    """
    resume, state._dialog_resume = state._dialog_resume, None
    if (resume is None or _sessions or not state._last_tool_active
            or getattr(context, 'mode', None) != 'EDIT_MESH'):
        return
    kind, keys = resume
    if kind == 'readopt':
        adopt_committed_baseline(context)
        for uid, cache in _sessions.items():
            cache.live_key = keys.get(uid)
    update_live_preview(context)


def begin_owned_input() -> None:
    """A plane transform takes the input. Fold a user edit made since our
    last write into the baseline now - one fingerprint per session - then
    trust the per-frame writes until end_owned_input: the transform modals
    consume every event, so nothing else can edit the mesh meanwhile."""
    adopted = []
    for cache in list(_sessions.values()):
        obj = _resolve_live_object(cache)
        if (obj is None or obj.type != 'MESH' or obj.mode != 'EDIT'
                or not _snapshot_valid(cache)):
            continue
        try:
            if _foreign_edit_happened(cache, bmesh.from_edit_mesh(obj.data)):
                # WYSIWYG, as _sync_baseline: what the user sees becomes the
                # baseline. live_key stays - it still produced what shows.
                if _live_take_snapshot(cache, obj):
                    adopted.append((cache, obj))
                else:
                    _reset_live_state(cache)
        except Exception:
            on_internal_error()  # DEBUG: surface; prod: counts still guard
    state._input_owned = True
    for cache in _sessions.values():
        cache.clip_frames, cache.clip_last_normal = {}, None
    for cache, obj in adopted:
        _record_written_counts(cache, bmesh.from_edit_mesh(obj.data), me=obj.data)


def end_owned_input() -> None:
    """The transform let go of the input: fingerprint what it left, so the
    next check tells a user edit from the tool's own writes."""
    if not state._input_owned:
        return
    flush_paced()       # the drag's last plane, before fingerprinting it
    state._input_owned = False
    for cache in _sessions.values():
        cache.clip_frames, cache.clip_last_normal = {}, None
    # A Fill cap the fast cut made mid-drag: re-cut exactly, so the preview
    # at rest is what Cut makes (see _apply_live_cut).
    tied = [c for c in _sessions.values() if c.fill_tied]
    if tied:
        for cache in tied:
            cache.live_key = None
        try:
            _timed_update(bpy.context)
            _tag_view3d_redraw()
        except Exception:
            on_internal_error()  # DEBUG: surface; prod: the next move re-cuts
    for cache in list(_sessions.values()):
        if not cache.fp_stale:
            continue
        cache.fp_stale = False
        obj = _resolve_live_object(cache)
        try:
            if obj is not None and obj.type == 'MESH' and obj.mode == 'EDIT':
                _record_written_counts(cache, bmesh.from_edit_mesh(obj.data), me=obj.data)
        except Exception:
            cache.content_fp = None  # counts only until the next write
            on_internal_error()


def refresh_live_preview(context) -> None:
    """Re-apply the preview at the current plane even if the plane/clear key is
    unchanged - used when a selection-role option (Select Cut Loop / Selected
    Only) is toggled, since those don't move the plane."""
    for cache in _sessions.values():
        cache.live_key = None
    update_live_preview(context)


def adopt_committed_baseline(context, executed_plane=None) -> None:
    """After Execute Cut: make the committed mesh the live baseline WITHOUT
    laying a fresh preview on top, so the cut stays a single clean undo step.

    Called by the Cut operator instead of topology_changed(). The watchdog would
    otherwise see no live session right after the commit and re-bisect the
    committed mesh next frame - a destructive edit with NO undo step of its own.
    That floating mutation is exactly what forced a second Ctrl+Z to undo a cut
    and made Clear Outer show a "committed loop that should have been a preview".

    Instead we snapshot the just-committed mesh and mark the session active AT THE
    CURRENT PLANE KEY, so the watchdog sees no transition and leaves the mesh as
    the clean committed cut (== the operator's undo step). The next plane move
    re-previews from this baseline. No mesh write happens here, so there is no
    1-frame flash.

    Pairs with the undo/redo handler: an undo drops this baseline (the snapshot
    would otherwise be stale) so the cut never reappears on the next plane move.
    """
    objects = _edit_mesh_objects(context)
    if getattr(context, 'mode', None) != 'EDIT_MESH' or not objects:
        topology_changed()  # fall back to plain behavior
        return
    sc = getattr(context.window_manager, 'cut_plane', None)
    plane = get_plane_world(context)
    for obj in objects:
        # Shape-keyed meshes never run the live preview (the rebuild
        # round-trip corrupts key data), so don't start a session here either.
        if getattr(obj.data, 'shape_keys', None) is not None:
            cache = _session_for(obj)
            if cache is not None:
                cache.live_key = None  # per-object topology_changed fallback
            continue
        cache = _session_for(obj) or CutPreviewCache()
        if not _live_take_snapshot(cache, obj):
            _reset_live_state(cache)
            continue
        cache.live_active = True
        _sessions[obj.session_uid] = cache
        try:
            _record_written_counts(cache, bmesh.from_edit_mesh(obj.data),
                                   me=obj.data)
        except Exception:
            cache.expected_counts = None
        if plane is not None and sc is not None:
            # Match the key update_live_preview will compute next, so the
            # preview stays a no-op until the plane or an option changes.
            # SAME SHAPE as _apply_live_cut's key - incl. the mirror axes.
            cache.live_key = (_plane_key(plane[0], plane[1]),
                              sc.cut_clear_inner, sc.cut_clear_outer,
                              fill_applies(sc.cut_use_fill, sc.cut_clear_inner,
                                           sc.cut_clear_outer), _mirror_axes(sc),
                              _mirror_pivot_key(obj, sc))
            if (executed_plane is not None
                    and _plane_key(*executed_plane) != _plane_key(plane[0], plane[1])):
                # F9 re-run with the gizmo plane moved since the cut: the cut
                # (correctly) re-ran at its STORED plane, but a key built from
                # the CURRENT pose would suppress the preview under the user's
                # plane - the plane looked dead while the adjusted cut sat
                # elsewhere. Leave the key dirty so the next refresh (the
                # undo-post 'apply' timer, or the next move) re-previews here.
                cache.live_key = None
        else:
            cache.live_key = None
    _debug_check('adopt_committed_baseline')
