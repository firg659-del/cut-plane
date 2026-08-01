# SPDX-License-Identifier: GPL-3.0-or-later
"""Math and Blender-context helpers shared across the Cut Plane addon."""

from typing import Optional, Tuple

import bpy
from bpy_extras import view3d_utils
from mathutils import Matrix, Vector

from .constants import (  # noqa: I001
    logger,
    ARROW_HIDE_THRESHOLD_MAX,
    ARROW_HIDE_THRESHOLD_MIN,
    AXIS_X,
    AXIS_Y,
    AXIS_Z,
    DEBUG,
    GIZMO_ARROW_COLOR_X,
    GIZMO_ARROW_COLOR_Y,
    GIZMO_ARROW_COLOR_Z,
    PARALLEL_EPSILON,
    TOOL_IDNAME,
)


def get_mesh_origin_world(context) -> Vector:
    """World position of the edit-mesh origin, or (0,0,0) when there's none."""
    if context.edit_object and context.edit_object.type == 'MESH':
        return context.edit_object.matrix_world.translation.copy()
    return Vector((0, 0, 0))


# Fallback axis colors when theme is unavailable
_FALLBACK_AXIS_COLORS = {
    'X': GIZMO_ARROW_COLOR_X,
    'Y': GIZMO_ARROW_COLOR_Y,
    'Z': GIZMO_ARROW_COLOR_Z,
}


def get_theme_axis_color(axis: str) -> Tuple[float, float, float]:
    """RGB of the theme's X/Y/Z axis color (hardcoded fallback if unavailable)."""
    try:
        theme = bpy.context.preferences.themes[0]
        ui = theme.user_interface
        theme_attrs = {'X': 'axis_x', 'Y': 'axis_y', 'Z': 'axis_z'}
        if axis in theme_attrs:
            return tuple(getattr(ui, theme_attrs[axis]))
        return (1.0, 1.0, 1.0)
    except (AttributeError, IndexError):
        return _FALLBACK_AXIS_COLORS.get(axis, (1.0, 1.0, 1.0))


def build_dial_basis(axis: Vector) -> Tuple[Vector, Vector]:
    """
    Build the orthonormal (x_axis, y_axis) basis of a dial plane.

    SINGLE SOURCE OF TRUTH: the arc drawing and the rotation operators'
    start-angle math must use the exact same basis, or the drawn arc
    drifts against the actual rotation. Previously duplicated in three
    places that had to be kept in sync by hand.
    """
    z_axis = axis.normalized()
    temp = AXIS_Z if abs(z_axis.z) < 0.9 else AXIS_X
    x_axis = z_axis.cross(temp).normalized()
    y_axis = z_axis.cross(x_axis).normalized()
    return x_axis, y_axis


def get_theme_active_object_color() -> Tuple[float, float, float]:
    """
    Get the active-object accent color from the current theme.

    Used to tint the gizmo's neutral elements (center button, view dial,
    uniform scale ring) so they read as belonging to the orange plane
    rather than to Blender's native transform gizmo.
    """
    try:
        return tuple(bpy.context.preferences.themes[0].view_3d.object_active)[:3]
    except (AttributeError, IndexError):
        return (1.0, 0.6, 0.0)  # Matches PLANE_COLOR_OUTLINE


def matrix_to_flat(matrix: Matrix) -> tuple:
    """
    Convert 4x4 Matrix to 16-element tuple (row-major order).

    Used to store Matrix in scene properties for operator↔gizmo communication.
    """
    return tuple(matrix[row][col] for row in range(4) for col in range(4))


def flat_to_matrix(flat) -> Matrix:
    """
    Convert 16-element sequence to 4x4 Matrix.

    Inverse of matrix_to_flat().
    """
    return Matrix((
        (flat[0], flat[1], flat[2], flat[3]),
        (flat[4], flat[5], flat[6], flat[7]),
        (flat[8], flat[9], flat[10], flat[11]),
        (flat[12], flat[13], flat[14], flat[15])
    ))


def get_current_orientation_type(context) -> str:
    """
    Get the gizmo orientation ('GLOBAL' or 'LOCAL').

    Standalone: read only the tool's own Orientation option
    (window_manager.cut_plane.orientation). The gizmo never follows the
    scene's transform orientation dropdown - configuring one never affects
    the other. This is the single funnel every orientation decision in the
    codebase goes through.
    """
    props = getattr(context.window_manager, 'cut_plane', None)
    if props is not None and props.orientation == 'LOCAL':
        return 'LOCAL'
    return 'GLOBAL'


def orientation_with_rotate_override(orientation_type: str, mode: str) -> str:
    """Collapse the orientation to GLOBAL while in ROTATE mode.

    ROTATE always positions the plane/gizmos in world space: a local offset
    would drift as the local->world transform changes mid-rotation. Every site
    that converts offsets to/from world must funnel through this rule, or the
    cut plane, the gizmos, and the plane visual disagree about where the plane
    is. This function is the single source of that invariant.
    """
    return 'GLOBAL' if mode == 'ROTATE' else orientation_type


def effective_orientation(context) -> str:
    """Effective gizmo orientation ('GLOBAL'/'LOCAL'), honoring the ROTATE rule.

    Context convenience wrapper over :func:`orientation_with_rotate_override`
    and :func:`get_current_orientation_type` - the funnel for positioning code.
    """
    props = getattr(context.window_manager, 'cut_plane', None)
    mode = props.mode if props is not None else 'MOVE'
    return orientation_with_rotate_override(get_current_orientation_type(context), mode)


def get_plane_pose(context) -> Optional[Tuple[Vector, Matrix]]:
    """World-space pose of the cut plane: (position, rotation matrix), or None.

    THE single source of the plane's placement formula:

        position = mesh origin + orientation-aware offset + center offset
        rotation = the gizmo rotation matrix (4x4, from rotation_matrix_flat)

    where the offset honors :func:`effective_orientation` (LOCAL offsets are
    rotated into world space; ROTATE mode collapses to GLOBAL). Every consumer
    of "where is the plane" - the cut plane, the plane visual, the gizmo
    handles' shared center, the transform operators' drag anchors - must go
    through here; the formula used to be inlined at ~20 call sites and every
    positioning fix meant a multi-file sweep.

    Returns None when the cut_plane property group is unavailable. NOTE: modal
    operators should call this at INVOKE time only - mid-drag they work from
    values cached at invoke (live re-reads would fight properties the modal
    itself is writing, e.g. snap align-rotation).
    """
    sc = getattr(context.window_manager, 'cut_plane', None)
    if sc is None:
        return None
    rotation = flat_to_matrix(sc.rotation_matrix_flat)
    local_offset = Vector((sc.x_offset, sc.y_offset, sc.z_offset))
    world_offset = transform_offset_to_world(
        local_offset, effective_orientation(context), rotation)
    position = get_mesh_origin_world(context) + world_offset + Vector(sc.center_offset)
    return position, rotation


def default_center_offset(obj) -> Vector:
    """The center_offset that places a fresh plane at the mesh BBOX CENTER.

    The plane pose formula anchors at the OBJECT ORIGIN (stable: mesh edits
    and cuts never move it mid-session), but origins are routinely far from
    the geometry (imported models, origin-at-world-center meshes) - a plane
    defaulting there intersects nothing and the live preview shows nothing.
    The default PLACEMENT therefore centers on the bounding box:

        center_offset = world bbox center - world origin

    Returns (0,0,0) for non-mesh/missing objects. Uses obj.bound_box (the
    last evaluated box - current at edit-session start, O(1) vs iterating
    vertices).
    """
    if obj is None or obj.type != 'MESH':
        return Vector((0.0, 0.0, 0.0))
    corners = [Vector(c) for c in obj.bound_box]
    center_local = sum(corners, Vector()) / 8.0
    center_world = obj.matrix_world @ center_local
    return center_world - obj.matrix_world.translation


def is_plane_tool_active(context) -> bool:
    """True when our Cut Plane workspace tool is the active tool.

    Single source for the gizmo group's poll() and the live-cut watchdog, which
    must agree on this exact condition (one shows the gizmo, the other drives
    the destructive preview).

    NOTE: tool_active_from_context() returns a FALSE NEGATIVE in region-less
    contexts (timers, headless execution) - it needs a valid 3D region. Callers
    that may run region-less must not trust a False result; the watchdog only
    queries this from a POST_VIEW draw context, where a region exists.
    """
    try:
        from bl_ui.space_toolsystem_common import ToolSelectPanelHelper
        tool = ToolSelectPanelHelper.tool_active_from_context(context)
        return tool is not None and tool.idname == TOOL_IDNAME
    except Exception:
        # Tool system not queryable - fail safe to "not active" so the gizmo
        # never appears unbidden and the preview never starts unexpectedly.
        return False


def calculate_alpha_fade(value: float, min_threshold: float = ARROW_HIDE_THRESHOLD_MIN,
                         max_threshold: float = ARROW_HIDE_THRESHOLD_MAX) -> float:
    """
    Calculate alpha fade factor using Hermite smoothstep interpolation.

    Used for smooth arrow/plane handler visibility transitions based on view angle.
    Matches native Blender gizmo behavior for axis-aligned view hiding.
    """
    if value < min_threshold:
        return 0.0
    if value > max_threshold:
        return 1.0
    # Hermite smoothstep (3t² - 2t³)
    t = (value - min_threshold) / (max_threshold - min_threshold)
    return t * t * (3.0 - 2.0 * t)


def get_view_direction_at_position(rv3d, view_inv_matrix: Matrix, view_z_axis: Vector,
                                    position: Vector) -> Vector:
    """
    Calculate view direction for a specific 3D position.

    Handles parallax in perspective view where view direction varies by screen position.
    In orthographic view, direction is constant.
    """
    if rv3d.is_perspective:
        # Perspective: View direction varies with position (parallax effect)
        camera_pos = view_inv_matrix.col[3].xyz
        return (camera_pos - position).normalized()
    # Orthographic: View direction is constant across entire view
    return view_z_axis


def ui_scale_factor() -> float:
    """Blender's UI scale (U.scale_factor) - the factor native code applies
    to element SIZES and LAYOUT (gizmo scale_final, widget units).

    Anything we lay out in raw region pixels (which are PHYSICAL framebuffer
    pixels on every platform) must multiply by this, or it sits at the wrong
    distance next to natively-scaled gizmo handles the moment the factor
    isn't 1.0 (Retina Macs: 2.0; Windows at Resolution Scale 1.5: 1.5 -
    measured). GUARD: system.ui_scale reads 0.0 in --background (the DPI
    machinery needs a real window), which would zero every offset in
    headless tests - fall back to 1.0.
    """
    try:
        s = bpy.context.preferences.system.ui_scale
        return s if s > 0.0 else 1.0
    except Exception:
        return 1.0


def pixel_size_factor() -> float:
    """Blender's DPI backing factor (U.pixelsize) - the factor native code
    applies to line WEIGHTS and marker RADII (2.0 on a Retina backing
    store, 1.0 on a standard display).

    Note the trap this exists to document: gpu.state.line_width_set is
    ALREADY multiplied by this factor in Blender's C front-end (measured:
    request 2.0 at pixel_size 2.0, read back 4.0) - so it must only be
    applied to POLYLINE shader lineWidth uniforms and to radii, never
    stacked onto line_width_set.
    """
    try:
        p = bpy.context.preferences.system.pixel_size
        return p if p > 0.0 else 1.0
    except Exception:
        return 1.0


def get_pixel_world_size(region, rv3d, location) -> float:
    """World-space size of one pixel at *location*.

    Empirical: project the point and a point one unit above it (view-up, so
    aspect ratio / FOV quirks cancel out) and take 1 / the pixel distance.
    Robust across perspective/ortho/camera views; 0.001 on any degenerate
    projection, capped at 10 so a camera sitting on the point can't blow
    up gizmo sizing.
    """
    if not region or not rv3d:
        return 0.001

    p1_2d = view3d_utils.location_3d_to_region_2d(region, rv3d, location)
    if p1_2d is None:
        return 0.001  # behind the camera

    try:
        view_inv = rv3d.view_matrix.inverted()
    except ValueError:
        return 0.001
    cam_up = view_inv.col[1].xyz.normalized()

    p2_2d = view3d_utils.location_3d_to_region_2d(region, rv3d, location + cam_up)
    if p2_2d is None:
        return 0.001

    dist_pix = (Vector(p2_2d) - Vector(p1_2d)).length
    if dist_pix < PARALLEL_EPSILON:
        return 0.001

    return min(1.0 / dist_pix, 10.0)


def transform_offset_to_world(
    local_offset: Vector,
    orientation_type: str,
    rotation_matrix: Matrix
) -> Vector:
    """
    Transform local offset to world space based on orientation mode.

    Core helper that eliminates the repeated LOCAL/GLOBAL branching pattern.
    Used by gizmo positioning and operator initialization.
    """
    if orientation_type == 'LOCAL':
        return rotation_matrix @ local_offset
    return local_offset.copy()


def apply_validated_matrix_to_gizmo(
    gizmo,
    primary_matrix: Matrix,
    origin: Vector,
    rotation_matrix: Matrix
) -> None:
    """
    Apply matrix to gizmo with validation and fallback chain.

    Handles the common pattern of validating a matrix before applying,
    with fallback to simpler matrices if validation fails.
    """
    if primary_matrix.is_valid:
        gizmo.matrix_basis = primary_matrix
    else:
        if DEBUG:
            logger.warning("[CUT PLANE] Warning: Invalid primary matrix, using fallback")

        fallback_matrix = Matrix.Translation(origin) @ rotation_matrix
        if fallback_matrix.is_valid:
            gizmo.matrix_basis = fallback_matrix
        else:
            gizmo.matrix_basis = Matrix.Translation(origin)
            if DEBUG:
                logger.error("[CUT PLANE] CRITICAL: Both primary and fallback matrices invalid!")


def calculate_axis_visibility(
    view_dir: Vector,
    rotation_matrix: Matrix,
    orientation_type: str
) -> dict:
    """
    Calculate visibility metrics for all axes based on view direction.

    Computes idot values and alpha fades for arrows and planes,
    eliminating duplicated visibility calculation code.
    """
    # Determine axis vectors based on orientation
    if orientation_type == 'LOCAL':
        axis_x = (rotation_matrix @ AXIS_X).normalized()
        axis_y = (rotation_matrix @ AXIS_Y).normalized()
        axis_z = (rotation_matrix @ AXIS_Z).normalized()
    else:
        axis_x = AXIS_X.copy()
        axis_y = AXIS_Y.copy()
        axis_z = AXIS_Z.copy()

    # Calculate inverted dot products
    idot_x = 1.0 - abs(view_dir.dot(axis_x))
    idot_y = 1.0 - abs(view_dir.dot(axis_y))
    idot_z = 1.0 - abs(view_dir.dot(axis_z))

    return {
        'idot_x': idot_x,
        'idot_y': idot_y,
        'idot_z': idot_z,
        'alpha_arrow_x': calculate_alpha_fade(idot_x),
        'alpha_arrow_y': calculate_alpha_fade(idot_y),
        'alpha_arrow_z': calculate_alpha_fade(idot_z),
        'alpha_plane_xy': calculate_alpha_fade(1.0 - idot_z),
        'alpha_plane_yz': calculate_alpha_fade(1.0 - idot_x),
        'alpha_plane_xz': calculate_alpha_fade(1.0 - idot_y),
        'axis_x': axis_x,
        'axis_y': axis_y,
        'axis_z': axis_z,
    }


def get_constraint_axis_world(orientation_type: str, local_axis: Vector,
                               gizmo_rotation_matrix: Matrix) -> Vector:
    """World-space constraint axis for move/rotate/scale.

    orientation_type is binary ('GLOBAL'/'LOCAL' - the only values the
    orientation funnel produces); LOCAL rotates the axis through the gizmo
    rotation (3x3 only: rotation without the translation part).
    """
    if orientation_type == 'LOCAL':
        return (gizmo_rotation_matrix.to_3x3() @ local_axis).normalized()
    return local_axis.copy()


# Alias: rotation axis transformation is identical to constraint axis transformation
get_rotation_axis_world = get_constraint_axis_world
