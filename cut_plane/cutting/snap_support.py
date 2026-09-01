# SPDX-License-Identifier: GPL-3.0-or-later
"""Snap support: let snapping target the PRE-CUT original (the snapshot) rather
than the realtime-cut preview, so snap points (especially derived ones like
edge/face centers, and the freshly created cut edges) don't jump as the plane
moves and re-cuts. A BVH of the snapshot is built lazily - only when snapping
actually queries it - and cached for the session; it is dropped whenever the
snapshot is replaced (_free_live_snapshot) or the session resets.
"""

from mathutils import Vector
from mathutils.bvhtree import BVHTree

from ..lib.constants import on_internal_error
from ..lib.math_utils import anchor_center_offset, default_center_offset
from .snapshot import _snapshot_bounds, _snapshot_valid
from .state import _session_for


def _get_or_build_snapshot_bvh(cache):
    """Cached BVHTree of the session's snapshot in object-local space, or None.

    Index alignment: BVHTree.FromPolygons ray_cast returns the polygon's index in
    the source order, which matches snapshot.polygons - so callers can read
    snapshot.polygons[idx] for the hit face. (Verified live.)
    """
    if cache.live_bvh is not None:
        return cache.live_bvh
    if not _snapshot_valid(cache):
        return None
    snap = cache.live_snapshot
    try:
        verts = [v.co.copy() for v in snap.vertices]
        polys = [tuple(p.vertices) for p in snap.polygons]
        cache.live_bvh = BVHTree.FromPolygons(verts, polys) if polys else None
    except Exception:
        on_internal_error()  # DEBUG: surface; prod: snapping falls back to live mesh
        cache.live_bvh = None
    return cache.live_bvh


def get_live_snapshot_mesh(obj):
    """The pre-cut snapshot Mesh for obj (so snapping can read the ORIGINAL
    polygons for candidate extraction), or None when no live cut is active for it."""
    cache = _session_for(obj)
    if cache is None or not _snapshot_valid(cache):
        return None
    return cache.live_snapshot


def baseline_center_offset(obj, anchor: str = 'BBOX') -> Vector:
    """center_offset placing the plane at the BASELINE mesh's bbox center.

    Reset-to-center must aim at the UNCUT geometry. obj.bound_box is the
    EVALUATED box - the destructive preview shrinks it (a Clear deletes a
    side), so resetting against it chased the surviving half's center: each
    Alt+G moved the plane, the preview re-cut, the box halved again -
    measured walking 0 -> 0.75 -> 0.875 -> 0.938 toward the top face,
    never the true center. The snapshot IS the uncut mesh; without a
    session the live box is the baseline and the plain helper serves.
    """
    if anchor != 'BBOX':
        # ORIGIN and CURSOR do not depend on the mesh at all, so the
        # evaluated-vs-baseline distinction below simply does not apply.
        return anchor_center_offset(obj, anchor)
    snap = get_live_snapshot_mesh(obj)
    if snap is None or not snap.vertices:
        return default_center_offset(obj)
    lo, hi = _snapshot_bounds(snap)
    center_world = obj.matrix_world @ ((lo + hi) / 2.0)
    return center_world - obj.matrix_world.translation


def baseline_selection_counts(obj):
    """(verts, edges, faces) selected in the BASELINE the next cut operates
    on, or None when no preview session holds one (the live mesh is then
    itself the baseline).

    This is what UI truth-telling must read: Execute Cut restores the
    baseline before bisecting, so its Selected-Only domain is the SNAPSHOT's
    selection. The live mesh is the destructive preview - with a Clear on it
    has DELETED the selected side, so its total_vert_sel reads 0 exactly
    while the cut would work (the header warned "Nothing selected to cut"
    about a cut that was about to succeed). O(1): the counts are cached at
    snapshot time and kept current by the selection remap.
    """
    cache = _session_for(obj)
    if cache is None or not _snapshot_valid(cache):
        return None
    return cache.snapshot_sel_counts


def raycast_original(obj, ray_origin: Vector, ray_dir: Vector):
    """Ray-cast obj's pre-cut snapshot in WORLD space.

    Returns (location, normal, face_index) in world space, or None when there is
    no active snapshot for obj or the ray misses. Snapping uses this so targets
    track the stable original geometry instead of the realtime cut (whose new
    edges and shifting face centers otherwise make snapping jump).
    """
    cache = _session_for(obj)
    if cache is None:
        return None
    bvh = _get_or_build_snapshot_bvh(cache)
    if bvh is None:
        return None
    mw = obj.matrix_world
    try:
        inv = mw.inverted()
    except ValueError:
        return None
    o_local = inv @ ray_origin
    d_local = (inv.to_3x3() @ ray_dir).normalized()
    loc, nrm, idx, _dist = bvh.ray_cast(o_local, d_local)
    if idx is None:
        return None
    return mw @ loc, (mw.to_3x3() @ nrm).normalized(), idx
