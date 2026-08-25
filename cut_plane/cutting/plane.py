# SPDX-License-Identifier: GPL-3.0-or-later
"""Plane parameters (single source of truth, mirrors gizmos.py plane visual)."""

from typing import Optional, Tuple

from mathutils import Vector

from ..lib.constants import AXIS_Z, ZERO_EPSILON
from ..lib.math_utils import get_plane_pose


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


def _mirrored_planes_local(co_l: Vector, no_l: Vector, axes) -> list:
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
        for co, no in list(planes):
            mco, mno = co.copy(), no.copy()
            mco[i] = -mco[i]
            mno[i] = -mno[i]
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
