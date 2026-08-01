# SPDX-License-Identifier: GPL-3.0-or-later
"""Preview lifecycle: session start, the per-frame apply, restore, and the
post-commit baseline adoption.

_apply_live_cut orchestrates the phases: ensure session -> dirty check ->
sync user changes into the baseline -> rebuild -> build domain -> cut/clear/
fill/select -> write + bookkeeping. Each phase owns its own rules; the
orchestrator owns only their ORDER. Mesh writes -> write-safe contexts only
(transform modals / option callback / depsgraph / timer); never a draw handler.
"""

from typing import Optional

import bmesh
import bpy

from ..lib.constants import logger, on_internal_error
from .invariants import _debug_check
from .plane import (
    _mirror_axes,
    _mirrored_planes_local,
    _plane_key,
    get_plane_world,
    plane_to_object_local,
)
from .selection import (
    _remap_selection_to_baseline,
    apply_cut_selection,
    clear_domain_side,
    repair_split_selection,
    reselect_cut_domain,
)
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
)
from .state import (
    _RECOVERY_TOKEN_PROP,
    CutPreviewCache,
    _edit_mesh_objects,
    _session_for,
    _sessions,
    topology_changed,
)


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
    planes = 1 << len(_mirror_axes(sc))
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


def _build_domain(bm, selected_only: bool, include_verts: bool = True):
    """Preview phase 3: the geometry the bisect operates on.

    Standard mode: the whole mesh. Selected Only: the selection = the cut's
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
        if include_verts:
            return bm.verts[:] + bm.edges[:] + bm.faces[:]
        return bm.edges[:] + bm.faces[:]
    geom = ([v for v in bm.verts if v.select]
            + [e for e in bm.edges if e.select]
            + [f for f in bm.faces if f.select])
    return geom or None


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
           mirror_axes)
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

    if not _live_rebuild_from_snapshot(cache, bm):
        _reset_live_state(cache)  # snapshot vanished; don't flush a half-built bmesh
        return

    co_l, no_l = plane_to_object_local(obj, plane_co, plane_no)
    nl = no_l.length
    if nl < 1e-9:
        bmesh.update_edit_mesh(obj.data, destructive=True)
        _record_written_counts(cache, bm, me=obj.data)
        cache.written_state = None  # rebuild restored the baseline, no cut
        return
    no_l = no_l / nl
    # The mirror toggles expand the one plane into its reflections; each
    # runs the full bisect/clear/fill phase in sequence - identical to
    # cutting at every plane by hand, so the semantics need no new rules.
    planes_l = _mirrored_planes_local(co_l, no_l, mirror_axes)

    pre_counts = (len(bm.verts), len(bm.edges), len(bm.faces))
    any_cut = False
    any_fill = False
    for p_co, p_no in planes_l:
        # Verts only needed in the bisect geom when a Clear must delete a
        # side; a plain split derives them from the cut edges (cheaper list).
        # Rebuilt per plane: a bisect invalidates prior element lists.
        geom = _build_domain(bm, selected_only,
                             include_verts=(clear_inner or clear_outer))
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
            # Selected Only clears via clear_domain_side (face-granular)
            # instead of the bisect's own vert-level clear, which would also
            # kill unselected neighbor faces sharing the domain's boundary.
            res = bmesh.ops.bisect_plane(
                bm, geom=geom, dist=1e-5, plane_co=p_co, plane_no=p_no,
                clear_inner=clear_inner and not selected_only,
                clear_outer=clear_outer and not selected_only)
        except Exception:
            # Recover the clean mesh first (never leave a half-bisected
            # preview), then surface under DEBUG.
            _live_rebuild_from_snapshot(cache, bm)
            bmesh.update_edit_mesh(obj.data, destructive=True)
            _record_written_counts(cache, bm, me=obj.data)
            on_internal_error()
            return
        if selected_only and (clear_inner or clear_outer):
            clear_domain_side(bm, res.get('geom', ()), p_co, p_no,
                              clear_inner, clear_outer)

        geom_cut = res.get('geom_cut', ())
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
                    fill_res = bmesh.ops.triangle_fill(
                        bm, edges=cut_edges, use_dissolve=True, normal=p_no)
                    fill_geom = fill_res.get('geom', ())
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
            # Splits must not half-deselect a selected region along the loop.
            # With a Clear active, the kept half may be the NEW face (its
            # selected sibling died inside the bisect) - the snapshot
            # recovers the source's selection.
            needs_snapshot = clear_inner or clear_outer
            repair_split_selection(
                geom_cut,
                bvh=_get_or_build_snapshot_bvh(cache) if needs_snapshot else None,
                snap_polygons=(cache.live_snapshot.polygons
                               if needs_snapshot and _snapshot_valid(cache) else None))
            apply_cut_selection(
                geom_cut, select=(sc is None or sc.cut_select_loop))

        any_cut = any_cut or bool(geom_cut)
        any_fill = any_fill or bool(fill_geom)

    # Exact baseline-ness: this apply left the mesh untouched only if no
    # plane produced a cut, nothing was filled, and no clear removed
    # anything. The count compare is WITHIN this apply (same mesh moments
    # apart), so the baseline-vs-preview count collision cannot fool it.
    cache.mesh_is_baseline = (
        not any_cut and not any_fill
        and (len(bm.verts), len(bm.edges), len(bm.faces)) == pre_counts)

    bmesh.update_edit_mesh(obj.data, destructive=True)
    _record_written_counts(cache, bm, me=obj.data)
    cache.live_key = key
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
                    pass
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
                    pass
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


def update_live_preview(context) -> None:
    """Refresh the live-cut preview at the current plane / clear side.

    Write-safe and dirty-checked; only call from write-safe contexts (transform
    modals, option callback, depsgraph) - never a draw handler. Cuts immediately
    on every call, so the preview tracks the plane with no pacing.
    """
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
                        sc.cut_use_fill, plane[0], plane[1])
    _debug_check('update_live_preview')


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
                              sc.cut_use_fill, _mirror_axes(sc))
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
