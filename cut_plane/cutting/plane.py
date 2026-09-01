# SPDX-License-Identifier: GPL-3.0-or-later
"""Plane parameters (single source of truth, mirrors gizmos.py plane visual)."""

from typing import Optional, Tuple

import bpy
from mathutils import Matrix, Vector

from ..lib.constants import AXIS_Z, ZERO_EPSILON
from ..lib.math_utils import anchor_world, get_plane_pose
from .snapshot import _snapshot_bounds
from .state import _session_for


def get_plane_world(context) -> Optional[Tuple[Vector, Vector]]:
    """World-space (plane_co, plane_no) of the gizmo's cutting plane.

    The cut plane is the gizmo's local XY plane, so its normal is the gizmo's
    local Z axis. Pose comes from math_utils.get_plane_pose (the single source
    of the plane placement formula). Returns None when there is no valid edit
    mesh / gizmo state.
    """
    obj = context.edit_object
    if obj is None or obj.type != 'MESH':
        return None
    pose = get_plane_pose(context)
    if pose is None:
        return None
    plane_co, rotation_matrix = pose
    plane_no = (rotation_matrix.to_3x3() @ AXIS_Z).normalized()
    if plane_no.length_squared < ZERO_EPSILON:
        return None
    return plane_co, plane_no


def plane_to_object_local(obj, plane_co: Vector, plane_no: Vector) -> Tuple[Vector, Vector]:
    """Transform a world plane into object-local space (for bmesh.ops.bisect_plane).

    Uses the inverse-transpose for the normal so it is correct under any object
    matrix, including non-uniform scale.

    An object with a zero-scale axis has NO world->local mapping
    (matrix_world.inverted() raises ValueError) - returns a ZERO normal
    instead, the "skip this object" signal every caller already handles
    (the cut's 'SKIP' status, the preview's degenerate bail). Unguarded,
    this raised out of the live preview, Execute Cut AND the tool's Ctrl+Z
    the moment any object in the edit session had a zeroed scale axis.
    """
    mw = obj.matrix_world
    try:
        co_local = mw.inverted() @ plane_co
    except ValueError:
        return Vector((0.0, 0.0, 0.0)), Vector((0.0, 0.0, 0.0))
    no_local = mw.to_3x3().transposed() @ plane_no
    return co_local, no_local


def _mirror_axes(sc) -> tuple:
    """The enabled mirror-axis indices (0=X, 1=Y, 2=Z), deterministic order."""
    if sc is None:
        return ()
    return tuple(i for i, on in enumerate(
        (sc.mirror_x, sc.mirror_y, sc.mirror_z)) if on)


def _anchor_object(obj):
    """The object the anchor is measured on: the ACTIVE edit object.

    The plane is PLACED relative to the active object, so the mirrors reflect
    about the active object's anchor too - one reference for the whole edit
    session. Measured before this: with two cubes and the plane at world
    z=4.5, a per-object anchor mirrored A to z=-4.5 and B to z=3.5, so
    "symmetric" cuts were symmetric about nothing in particular. Falls back
    to the object itself when there is no active edit object (timer and
    handler contexts), which is also the single-object case - identical
    either way, since the object IS the active one.
    """
    active = getattr(bpy.context, 'edit_object', None)
    if active is None or getattr(active, 'type', None) != 'MESH':
        return obj
    return active


def _mirror_pivot_local(obj, sc):
    """The anchor in OBJECT-LOCAL space for `obj`, or None for its origin.

    Resolved on the ACTIVE object in WORLD space, then converted into this
    object's space - so every object in the session mirrors about the same
    world point. None is the fast path: it means the reflection is a pure
    sign flip with no translation term, byte-identical to what Mirror always
    did, and it is returned only when that is genuinely correct.

    BBOX measures the SESSION SNAPSHOT, never the mesh in front of you - the
    live cut mutates that one in place, so bounds read off it would shrink as
    the plane sweeps and the mirrors would crawl with the preview. Falls back
    to the origin whenever the anchor cannot be resolved: a wrong mirror is
    worse than the documented default.
    """
    mode = getattr(sc, 'plane_anchor', 'BBOX') if sc is not None else 'BBOX'
    if obj is None:
        return None
    src = _anchor_object(obj)
    if mode == 'ORIGIN':
        if src is obj:
            return None                              # its own origin: sign flip
        world = src.matrix_world.translation
    elif mode == 'BBOX':
        cache = _session_for(src)
        center = cache.snapshot_bbox_center if cache is not None else None
        if center is None:
            me = getattr(cache, 'live_snapshot', None) if cache else None
            if me is None:
                me = getattr(src, 'data', None)      # no session: mesh IS the original
            if me is None or not len(me.vertices):
                return None
            lo, hi = _snapshot_bounds(me)
            center = (lo + hi) * 0.5
            if cache is not None and cache.live_snapshot is not None:
                cache.snapshot_bbox_center = center.copy()
        if src is obj:
            return center.copy()                     # already this object's space
        world = src.matrix_world @ center
    else:
        world = anchor_world(src, mode)
    try:
        return obj.matrix_world.inverted() @ Vector(world)
    except ValueError:
        return None                                  # singular transform


def _mirror_world_frame(context, sc, pivot_local=None):
    """(pivot, [x, y, z]) in WORLD space, taken from the ACTIVE object.

    The real plane is ONE world plane that every object in the session is
    cut at. The mirrored planes are now derived the same way, so they are
    one world set too. Deriving them per object in its own local axes made
    "Mirror X" mean a different world DIRECTION for each differently
    rotated object: measured with the plane at z=2 and Mirror Z, an
    unrotated object mirrored to (0,0,-2) normal (0,0,-1) while one turned
    45 deg about Y mirrored to (-2,0,0) normal (-1,0,0).

    Returns (None, None) when there is no active edit object, which puts
    the caller back on the per-object path - the same thing for a single
    object, since it IS the active one.
    """
    obj = getattr(context, 'edit_object', None)
    if obj is None or getattr(obj, 'type', None) != 'MESH':
        return None, None
    piv_l = pivot_local if pivot_local is not None else _mirror_pivot_local(obj, sc)
    mw = obj.matrix_world
    pivot = mw @ (piv_l if piv_l is not None else Vector((0.0, 0.0, 0.0)))
    rot = mw.to_3x3()
    dirs = []
    for i in range(3):
        axis = Vector((0.0, 0.0, 0.0))
        axis[i] = 1.0
        d = rot @ axis
        if d.length_squared < 1e-12:
            return None, None            # degenerate transform - skip mirroring
        dirs.append(d.normalized())
    return pivot, dirs


def _mirrored_planes_world(context, co_w: Vector, no_w: Vector, axes, sc,
                           pivot_local=None) -> list:
    """The mirrored planes in WORLD space - one set for the whole session."""
    pivot, dirs = _mirror_world_frame(context, sc, pivot_local)
    if dirs is None:
        return [(co_w, no_w)]
    return _mirrored_planes_local(co_w, no_w, axes, pivot, dirs)


def _mirror_reflection_world(pivot: Vector, m: Vector) -> Matrix:
    """World reflection across the plane through `pivot` with normal `m`.

    The ghosts need a full matrix, not a (point, normal): the drawn
    rectangle carries the plane's in-plane orientation too.
    """
    r = Matrix.Identity(4)
    for i in range(3):
        for j in range(3):
            r[i][j] = (1.0 if i == j else 0.0) - 2.0 * m[i] * m[j]
    off = 2.0 * pivot.dot(m)
    for i in range(3):
        r[i][3] = off * m[i]
    return r


def _mirror_pivot_key(obj, sc) -> tuple:
    """Dirty-detection component for the anchor, quantised like _plane_key.

    The mode alone is not enough: moving the 3D cursor, or editing the mesh
    under a BBOX anchor, moves the mirrors without touching the plane or any
    toggle, and the preview has to notice.
    """
    mode = getattr(sc, 'plane_anchor', 'BBOX') if sc is not None else 'BBOX'
    if mode == 'ORIGIN':
        return ('ORIGIN',)
    p = _mirror_pivot_local(obj, sc)
    if p is None:
        return (mode, None)
    return (mode, round(p.x, 5), round(p.y, 5), round(p.z, 5))


def _mirrored_planes_local(co_l: Vector, no_l: Vector, axes, pivot=None,
                           dirs=None) -> list:
    """Every object-local plane a cut expands to under the mirror toggles.

    Reflections across the object's LOCAL axes through its origin - Mirror-
    modifier semantics, and the natural spot in the pipeline: both cut paths
    already converted the world plane to object-local space, where a
    reflection is two sign flips. Each enabled axis doubles the set (X+Y =
    4 planes, X+Y+Z = 8). Deduped by the GEOMETRIC
    plane (_coincident_plane_key): a plane that reflects onto itself must
    cut ONCE. An opposite-facing twin somewhere ELSE is still a different
    cut - its Clear side points the other way, exactly what a mirrored
    Clear must do - and it survives, because it sits at a different
    offset.

    The mirrors are derived from the one real plane at cut time - never
    stored state, so undo/F9/persistence know nothing new.
    """
    planes = [(co_l, no_l)]
    for i in axes:
        m = None if dirs is None else dirs[i]
        for co, no in list(planes):
            if m is None:
                # Coordinate-axis reflection: two sign flips, exact.
                mco, mno = co.copy(), no.copy()
                mco[i] = -mco[i] if pivot is None else (2.0 * pivot[i] - mco[i])
                mno[i] = -mno[i]
            else:
                # Reflection across an ARBITRARY unit direction. For an
                # axis-aligned m this reduces to the sign flips above, term
                # for term, so the common case is unchanged.
                p = pivot if pivot is not None else Vector((0.0, 0.0, 0.0))
                mco = co - 2.0 * (co - p).dot(m) * m
                mno = no - 2.0 * no.dot(m) * m
            planes.append((mco, mno))
    out, seen = [], set()
    for co, no in planes:
        k = _coincident_plane_key(co, no)
        if k not in seen:
            seen.add(k)
            out.append((co, no))
    return out


def _coincident_plane_key(co: Vector, no: Vector) -> tuple:
    """Dedup key for the mirror expansion: the GEOMETRIC plane, orientation
    ignored.

    Deliberately NOT _plane_key, which keeps the normal's sign because the
    preview must re-run when a flip changes which side Clear removes. Here
    the sign must NOT separate: a plane that mirrors onto itself comes back
    as the same geometry with a reversed normal, and cutting it twice makes
    Clear delete BOTH sides - their union is the whole mesh. That is the
    DEFAULT state, not an exotic one: the plane spawns at the mesh centre
    with a Z normal, and on a fresh primitive that is the object origin.

    Genuinely different planes still differ here - the offset d = co.n
    separates them.
    """
    n = no.normalized() if no.length_squared > 0.0 else no.copy()
    d = co.dot(n)
    for c in n:                     # canonical orientation: make the first
        if abs(c) > 1e-9:           # significant component positive
            if c < 0.0:
                n = -n
                d = -d
            break
    return (round(n.x, 5), round(n.y, 5), round(n.z, 5), round(d, 5))


def _plane_key(plane_co: Vector, plane_no: Vector) -> tuple:
    """Dirty-detection key: plane pose (rounded). Topology is the snapshot's."""
    return (
        round(plane_co.x, 5), round(plane_co.y, 5), round(plane_co.z, 5),
        round(plane_no.x, 5), round(plane_no.y, 5), round(plane_no.z, 5),
    )
