# SPDX-License-Identifier: GPL-3.0-or-later
"""Tool-local snapping for the gizmo's modal operators.

The gizmo has its OWN snap settings (cut_plane.snap_*), independent of
Blender's global snap - configuring one never affects the other. They mirror
native snapping semantics (and the popover looks native), seeded once from the
scene's snap on first tool activation (seed_tool_snap_if_needed):

- ``snap_use`` (magnet) inverted by holding Ctrl, gated per transform type by
  the Affect toggles (``snap_affect_move`` / ``_rotate`` / ``_scale``)
- ``snap_elements`` selects targets. Supported for the plane center:
  INCREMENT, GRID, VERTEX, EDGE, FACE, EDGE_MIDPOINT, FACE_MIDPOINT
- ``snap_angle_increment`` (+``_precision``) drive dial snapping

Not applicable to a single virtual point and therefore not offered:
VOLUME (object depth placement), EDGE_PERPENDICULAR (needs a reference
geometry point), FACE_PROJECT / FACE_NEAREST (individual-element
projection modes), and the Snap Base options (the base is always the
plane center).
"""

import math

from bpy_extras import view3d_utils
from mathutils import Vector

from .. import cutting
from ..lib.constants import AXIS_Z
from ..lib.math_utils import get_pixel_world_size, matrix_to_flat, ui_scale_factor
from .utils import ctrl_like

# Screen-space search radius for vertex/edge candidates, in pixels
# (native uses a comparable pixel threshold for its snap search)
SNAP_PIXEL_RADIUS = 25.0

# A grid step is scaled by powers of 10 until it spans at least this many
# pixels - approximates how the viewport grid subdivides with zoom
# (native ED_view3d_grid_view_scale)
GRID_STEP_MIN_PIXELS = 10.0

# snap_elements entries that need a geometry query under the cursor
GEOMETRY_ELEMENTS = frozenset({'VERTEX', 'EDGE', 'EDGE_MIDPOINT', 'FACE', 'FACE_MIDPOINT'})

# Snap targets the gizmo supports (used when seeding the tool's snap from the scene)
_SUPPORTED_SNAP_ELEMENTS = frozenset(
    {'INCREMENT', 'GRID', 'VERTEX', 'EDGE', 'FACE', 'EDGE_MIDPOINT', 'FACE_MIDPOINT'})

# Screen-distance bias so vertices win over edges at similar distance,
# and edges over edge midpoints (matches native candidate preference)
_VERTEX_BIAS = 0.85
_EDGE_MIDPOINT_BIAS = 0.9


def snap_enabled(context, event, affect_attr: str) -> bool:
    """Effective snap state with native semantics, from the TOOL-LOCAL settings.

    The tool's own magnet (cut_plane.snap_use), gated by its per-mode Affect
    checkbox (affect_attr is a cut_plane attr: snap_affect_move/_rotate/_scale),
    inverted by holding Ctrl (or Cmd on macOS - ctrl_like) - so it enables
    ad-hoc snapping when the magnet is off, and temporarily disables it when
    on. Independent of the scene snap.
    """
    props = context.window_manager.cut_plane
    magnet = props.snap_use and getattr(props, affect_attr, True)
    return magnet != ctrl_like(event)


def get_angle_increment(context, precision: bool) -> float:
    """Rotation snap step from the tool-local settings (radians)."""
    props = context.window_manager.cut_plane
    if precision:
        return props.snap_angle_increment_precision
    return props.snap_angle_increment


def seed_tool_snap_if_needed(context) -> None:
    """Copy the scene's current snap settings into the tool's own - once.

    Called on the tool's first activation in a scene (from the cut watchdog, a
    write-safe context). Makes the switch to tool-local snapping seamless; after
    seeding, snap_seeded latches True and the tool's snap is fully standalone.
    """
    props = getattr(context.window_manager, 'cut_plane', None)
    if props is None or props.snap_seeded:
        return
    ts = context.scene.tool_settings
    try:
        props.snap_use = ts.use_snap
        seeded = set(ts.snap_elements) & _SUPPORTED_SNAP_ELEMENTS
        props.snap_elements = seeded or {'INCREMENT'}
        props.snap_affect_move = ts.use_snap_translate
        props.snap_affect_rotate = ts.use_snap_rotate
        props.snap_affect_scale = ts.use_snap_scale
        props.snap_angle_increment = ts.snap_angle_increment_3d
        props.snap_angle_increment_precision = ts.snap_angle_increment_3d_precision
        props.snap_align_rotation = ts.use_snap_align_rotation
    except Exception:
        # Future Blender may rename snap tool_settings - seeding is a
        # best-effort convenience and must never block tool activation;
        # the tool's own defaults cover whatever didn't copy.
        pass
    props.snap_seeded = True


def get_move_increment(context, position: Vector, precision: bool) -> float:
    """Zoom-adaptive translation step matching the visible viewport grid.

    Approximates native ED_view3d_grid_view_scale: the base grid scale is
    multiplied/divided by powers of 10 until a step spans a sensible
    number of pixels at the gizmo's position. Shift gives a 10x finer step.
    """
    step = 1.0
    overlay = getattr(context.space_data, 'overlay', None)
    if overlay is not None:
        step = overlay.grid_scale_unit or overlay.grid_scale or 1.0

    px = get_pixel_world_size(context.region, context.region_data, position)
    # x ui_scale: the "at least N pixels on screen" floor is a LOGICAL size
    # (region pixels are physical) - see snap_to_geometry.
    min_px = GRID_STEP_MIN_PIXELS * ui_scale_factor()
    if px > 0.0:
        for _ in range(12):
            if step / px >= min_px:
                break
            step *= 10.0
        for _ in range(12):
            if step / px < min_px * 10.0:
                break
            step /= 10.0

    if precision:
        step /= 10.0
    return step


def round_vector_to_step(vec: Vector, step: float) -> Vector:
    """Round each component to the nearest multiple of step."""
    if step <= 0.0:
        return vec.copy()
    return Vector((
        round(vec.x / step) * step,
        round(vec.y / step) * step,
        round(vec.z / step) * step,
    ))


def snap_to_geometry(context, mouse_pos, elements):
    """Find the best snap point near the mouse among enabled elements.

    Ray-casts under the cursor, then evaluates candidates from the hit
    face: vertices, edge points, edge midpoints, and the face center
    within a pixel radius; the face surface point acts as the fallback
    (cursor is always on it) - matching native priority.

    While the live-cut preview is active, the EDITED object is ray-cast
    against its pre-cut snapshot (a cached BVH of the original geometry)
    instead of the realtime-cut mesh, so derived targets (edge/face
    centers, the new cut edges) stay put instead of jumping as the plane
    re-cuts - and original features stay snappable even on a side that
    Clear Inner/Outer removed. Other objects still snap to the live scene.
    """
    region = context.region
    rv3d = context.region_data
    if region is None or rv3d is None:
        return None, None, None

    # Region pixels are PHYSICAL pixels: without x ui_scale the capture
    # radius halves in logical terms on a 2x display (native scales its
    # snap threshold with the UI factor) - Mac users got half the reach.
    snap_radius = SNAP_PIXEL_RADIUS * ui_scale_factor()

    depsgraph = context.evaluated_depsgraph_get()
    edit_obj = context.edit_object
    # Snapshot Mesh of the edited object while a live cut is active (else None).
    snap_mesh = cutting.get_live_snapshot_mesh(edit_obj) if edit_obj is not None else None

    def cast(px, py):
        """Ray-cast at a pixel -> (hit, loc, normal, face_index, obj, matrix, mesh).

        `mesh` is the snapshot Mesh when the edited object resolved through its
        pre-cut BVH (candidates are read from it); None means use obj's evaluated
        mesh. The edited object's realtime cut is never used as a snap source -
        the stable snapshot stands in for it.
        """
        o = view3d_utils.region_2d_to_origin_3d(region, rv3d, (px, py))
        d = view3d_utils.region_2d_to_vector_3d(region, rv3d, (px, py))
        if snap_mesh is not None:
            r = cutting.raycast_original(edit_obj, o, d)
            if r is not None:
                loc, nrm, fidx = r
                return True, loc, nrm, fidx, edit_obj, edit_obj.matrix_world, snap_mesh
        h, loc, nrm, fidx, ob, mx = context.scene.ray_cast(depsgraph, o, d)
        if snap_mesh is not None and ob is not None and ob == edit_obj:
            # Hit the edited object's realtime cut - ignore it; its stable
            # original was already tried via the snapshot BVH above.
            return False, None, None, -1, None, None, None
        return h, loc, nrm, fidx, ob, mx, None

    hit, hit_loc, hit_normal, face_index, obj, matrix, src_mesh = cast(mouse_pos[0], mouse_pos[1])
    surface_hit = hit

    if not hit:
        # Near-silhouette search: native snaps to vertices/edges within
        # the pixel radius even when the cursor is just OFF the surface
        # (think corner vertices). Probe a small ring of rays around the
        # cursor to find the nearby face; candidates are still validated
        # against the true mouse position below.
        for radius in (snap_radius * 0.5, snap_radius):
            for i in range(8):
                ang = (math.pi / 4.0) * i
                px = mouse_pos[0] + math.cos(ang) * radius
                py = mouse_pos[1] + math.sin(ang) * radius
                hit, hit_loc, hit_normal, face_index, obj, matrix, src_mesh = cast(px, py)
                if hit:
                    break
            if hit:
                break

    if not hit or obj is None:
        return None, None, None
    if src_mesh is None and obj.type != 'MESH':
        return None, None, None
    hit_normal = Vector(hit_normal)

    mouse = Vector(mouse_pos)
    best = None  # (biased_dist_px, location, element)

    def consider(point_world, element, bias=1.0):
        nonlocal best
        p2d = view3d_utils.location_3d_to_region_2d(region, rv3d, point_world)
        if p2d is None:
            return
        d = (Vector(p2d) - mouse).length * bias
        if d <= snap_radius and (best is None or d < best[0]):
            best = (d, Vector(point_world), element)

    want_vert = 'VERTEX' in elements
    want_edge = 'EDGE' in elements
    want_edge_mid = 'EDGE_MIDPOINT' in elements
    want_face_mid = 'FACE_MIDPOINT' in elements

    if want_vert or want_edge or want_edge_mid or want_face_mid:
        verts_world = []
        face_center = None
        if src_mesh is not None:
            # Snapshot Mesh (persistent datablock) - read its polygons directly.
            me = src_mesh
            if 0 <= face_index < len(me.polygons):
                poly = me.polygons[face_index]
                verts_world = [matrix @ me.vertices[i].co for i in poly.vertices]
                face_center = matrix @ poly.center
        else:
            # NOTE: to_mesh() (a copy), NOT ob_eval.data - in Edit Mode the
            # evaluated object's .data can be an empty BMesh wrapper with no
            # vertex/polygon arrays, while to_mesh() is always populated.
            ob_eval = obj.evaluated_get(depsgraph)
            me = ob_eval.to_mesh()
            try:
                if me is not None and 0 <= face_index < len(me.polygons):
                    poly = me.polygons[face_index]
                    verts_world = [matrix @ me.vertices[i].co for i in poly.vertices]
                    face_center = matrix @ poly.center
            finally:
                ob_eval.to_mesh_clear()

        if verts_world:
            if want_vert:
                for v in verts_world:
                    consider(v, 'VERTEX', bias=_VERTEX_BIAS)
            if want_edge or want_edge_mid:
                count = len(verts_world)
                for i in range(count):
                    a = verts_world[i]
                    b = verts_world[(i + 1) % count]
                    if want_edge_mid:
                        consider((a + b) * 0.5, 'EDGE_MIDPOINT', bias=_EDGE_MIDPOINT_BIAS)
                    if want_edge:
                        ab = b - a
                        len_sq = ab.length_squared
                        if len_sq > 1e-12:
                            t = max(0.0, min(1.0, (hit_loc - a).dot(ab) / len_sq))
                            consider(a + ab * t, 'EDGE')
            if want_face_mid and face_center is not None:
                consider(face_center, 'FACE_MIDPOINT')

    if best is not None:
        return best[1], best[2], hit_normal
    if surface_hit and 'FACE' in elements:
        # Surface fallback only when the cursor is actually on the face
        return Vector(hit_loc), 'FACE', hit_normal
    return None, None, None


def align_rotation_to_normal(init_rotation_matrix, normal: Vector):
    """Minimal rotation aligning the plane's +Z to the target normal.

    Mirrors native use_snap_align_rotation: the rotation between the
    plane's current normal (from the rotation at drag start) and the
    snap target normal, pre-multiplied so no extra twist is introduced.
    """
    current = (init_rotation_matrix.to_3x3() @ AXIS_Z).normalized()
    target = normal.normalized()
    quat = current.rotation_difference(target)
    return quat.to_matrix().to_4x4() @ init_rotation_matrix


def apply_align_rotation(context, props, operator, normal) -> None:
    """Align the plane to the snap normal when the tool's
    Align Rotation to Target setting (cut_plane.snap_align_rotation) is enabled.

    The operator must expose _cached_rotation_matrix (rotation at drag
    start). While unsnapped (normal None) or with the setting off, any
    previously applied alignment is reverted - like native, alignment
    only holds while actually snapped.
    """
    if normal is None or not props.snap_align_rotation:
        restore_align_rotation(props, operator)
        return
    aligned = align_rotation_to_normal(operator._cached_rotation_matrix, normal)
    props.rotation_matrix_flat = matrix_to_flat(aligned)
    operator._snap_align_applied = True


def restore_align_rotation(props, operator) -> None:
    """Revert the rotation to the drag-start state after leaving a snap."""
    if getattr(operator, '_snap_align_applied', False):
        props.rotation_matrix_flat = matrix_to_flat(operator._cached_rotation_matrix)
        operator._snap_align_applied = False


def set_snap_marker(context, location, element: str = "") -> None:
    """Update the snap marker runtime state (None hides the marker).

    The element type drives the marker shape (native indicator shapes).
    Lives on the WindowManager runtime group - never saved, no depsgraph
    updates triggered by the per-mousemove writes.
    """
    runtime = getattr(context.window_manager, 'cut_plane_runtime', None)
    if runtime is None:
        return
    if location is None:
        if runtime.snap_marker_active:
            runtime.snap_marker_active = False
    else:
        runtime.snap_marker_active = True
        runtime.snap_marker_location = location
        if runtime.snap_marker_element != element:
            runtime.snap_marker_element = element


def resolve_translation_snap(context, event, operator, props, anchor, raw_delta,
                             axis=None, plane_normal=None):
    """Resolve snapping for one translation mousemove.

    Single entry point shared by the axis arrows, plane handlers, and
    the free center move - handles target priority (geometry under the
    cursor beats increment/grid), the snap marker, and the
    align-rotation state. Raw drag tracking stays unsnapped in the
    caller, so leaving a snap returns to the mouse position (native).
    """
    if not snap_enabled(context, event, 'snap_affect_move'):
        set_snap_marker(context, None)
        restore_align_rotation(props, operator)
        return raw_delta

    # The ENUM_FLAG popover lets the user untoggle every target; an empty set
    # would make an enabled magnet silently do nothing - fall back to INCREMENT.
    elements = context.window_manager.cut_plane.snap_elements or {'INCREMENT'}
    delta = raw_delta
    marker = None
    marker_element = ""
    marker_normal = None

    if elements & GEOMETRY_ELEMENTS:
        snap_loc, snap_el, snap_normal = snap_to_geometry(
            context, (event.mouse_region_x, event.mouse_region_y), elements)
        if snap_loc is not None:
            # The marker stays on the geometry point; movement gets the
            # constrained projection of it (native constrained snapping)
            offset = snap_loc - anchor
            if axis is not None:
                delta = axis * offset.dot(axis)
            elif plane_normal is not None:
                delta = offset - plane_normal * offset.dot(plane_normal)
            else:
                delta = offset
            marker = snap_loc
            marker_element = snap_el
            marker_normal = snap_normal

    if marker is None and elements & {'INCREMENT', 'GRID'}:
        step = get_move_increment(context, anchor, event.shift)
        if axis is not None:
            # 1D: scalar steps along the axis (GRID rounds the absolute
            # world position, then projects back onto the axis)
            dist = delta.dot(axis)
            if 'GRID' in elements:
                candidate = round_vector_to_step(anchor + axis * dist, step)
                dist = (candidate - anchor).dot(axis)
            else:
                dist = round(dist / step) * step
            delta = axis * dist
        else:
            if 'GRID' in elements:
                delta = round_vector_to_step(anchor + delta, step) - anchor
            else:
                delta = round_vector_to_step(delta, step)
            if plane_normal is not None:
                # Keep the movement in the constraint plane
                delta = delta - plane_normal * delta.dot(plane_normal)

    set_snap_marker(context, marker, marker_element)
    apply_align_rotation(context, props, operator, marker_normal)
    return delta
