# SPDX-License-Identifier: GPL-3.0-or-later
"""GPU drawing: plane visual, rotation arc, snap ticks, shader cache."""

import math
import traceback
from typing import Optional

import bpy
import gpu
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix, Vector

# TOP-LEVEL, never lazy: a lazy import of a wrong name inside a draw callback
# raises on every frame and is swallowed by the broad except below - that is
# exactly how the mirror ghosts silently never drew for weeks. A wrong name
# here fails loudly at module load instead. (cutting does not import drawing,
# so there is no cycle.)
from .. import cutting
from ..lib import handler_store
from ..operators.utils import active_transform, any_keyboard_transform_active
from ..lib.constants import (  # noqa: I001
    logger,
    ARC_RADIUS_PIXELS,
    MIRROR_GHOST_COLOR,
    PLANE_COLOR_OUTLINE,
    PLANE_OUTLINE_WIDTH,
    PLANE_SIZE,
    TICK_INNER_FACTOR,
    TICK_OUTER_FACTOR,
    ZERO_EPSILON,
)
from ..lib.math_utils import (
    build_dial_basis,
    get_pixel_world_size,
    get_plane_pose,
    get_theme_active_object_color,
    get_theme_axis_color,
    is_plane_tool_active,
    pixel_size_factor,
    ui_scale_factor,
)

_CACHED_UNIFORM_SHADER: Optional[gpu.types.GPUShader] = None
_CACHED_POLYLINE_SHADER: Optional[gpu.types.GPUShader] = None


def get_uniform_color_shader() -> gpu.types.GPUShader:
    """
    Get cached uniform color shader, creating it if necessary.

    Handles GPU context loss (e.g., workspace switch) by recreating shader.
    """
    global _CACHED_UNIFORM_SHADER
    try:
        if _CACHED_UNIFORM_SHADER is None:
            _CACHED_UNIFORM_SHADER = gpu.shader.from_builtin('UNIFORM_COLOR')
        return _CACHED_UNIFORM_SHADER
    except RuntimeError as e:
        # GPU context loss (e.g. workspace switch) invalidates the cache -
        # recreate; anything else re-raises
        error_msg = str(e).lower()
        if "context" in error_msg or "gpu" in error_msg:
            _CACHED_UNIFORM_SHADER = gpu.shader.from_builtin('UNIFORM_COLOR')
            return _CACHED_UNIFORM_SHADER
        raise


def get_polyline_shader() -> gpu.types.GPUShader:
    """Cached POLYLINE_UNIFORM_COLOR shader - the anti-aliased line shader the
    native overlays use (smooth width falloff, no jaggies).

    Same context-loss recovery as get_uniform_color_shader."""
    global _CACHED_POLYLINE_SHADER
    try:
        if _CACHED_POLYLINE_SHADER is None:
            _CACHED_POLYLINE_SHADER = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
        return _CACHED_POLYLINE_SHADER
    except RuntimeError as e:
        error_msg = str(e).lower()
        if "context" in error_msg or "gpu" in error_msg:
            _CACHED_POLYLINE_SHADER = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
            return _CACHED_POLYLINE_SHADER
        raise


def cleanup_gpu_resources() -> None:
    """Clean up GPU resources (shader cache)."""
    global _CACHED_UNIFORM_SHADER, _CACHED_POLYLINE_SHADER
    _CACHED_UNIFORM_SHADER = None
    _CACHED_POLYLINE_SHADER = None


def draw_plane_at_matrix(
    matrix: Matrix,
    scale: float = PLANE_SIZE,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
    color=None,
) -> None:
    """
    Draw orange plane at given transformation matrix.

    Creates GPU batches fresh each frame. This is intentional - the matrix changes
    frequently (position/rotation), so caching would be counterproductive.
    """
    try:
        size_x = scale * scale_x
        size_y = scale * scale_y
        vertices = [
            matrix @ Vector((-size_x, -size_y, 0)),
            matrix @ Vector((size_x, -size_y, 0)),
            matrix @ Vector((size_x, size_y, 0)),
            matrix @ Vector((-size_x, size_y, 0))
        ]

        # POLYLINE, not UNIFORM_COLOR + line_width_set: macOS's Metal backend
        # STORES a plain line width and never applies it - every batch-drawn
        # line rasterizes as a ~1-physical-px hairline there (native lines
        # survive only via an immediate-mode shader swap Python never gets).
        # The polyline shader takes the width as a uniform, on every backend.
        # x pixel_size: line WEIGHTS scale with U.pixelsize natively; the C
        # front-end applies it to line_width_set (which Metal then drops), so
        # the uniform must carry it itself.
        shader = get_polyline_shader()

        # Outline only - the plane is drawn as a wire rectangle (no fill).
        batch_outline = batch_for_shader(
            shader, 'LINES',
            {"pos": vertices},
            indices=[(0, 1), (1, 2), (2, 3), (3, 0)]
        )

        saved_blend = gpu.state.blend_get()
        try:
            gpu.state.blend_set('ALPHA')
            shader.bind()
            viewport = gpu.state.viewport_get()
            shader.uniform_float("viewportSize", (viewport[2], viewport[3]))
            shader.uniform_float(
                "lineWidth", PLANE_OUTLINE_WIDTH * pixel_size_factor())
            shader.uniform_float(
                "color", color if color is not None else PLANE_COLOR_OUTLINE)
            batch_outline.draw(shader)
        finally:
            gpu.state.blend_set(saved_blend)

    except Exception as e:
        logger.error(f"[CUT PLANE] Error drawing plane: {e}")
        logger.error(f"[CUT PLANE] Traceback: {traceback.format_exc()}")


# ============================================================================
# ROTATION ARC DRAWING SYSTEM
# ============================================================================
# Custom arc drawing for operator-based rotation (replaces built-in dial arc
# which doesn't work with target_set_operator() due to DialInteraction bypass)


def draw_rotation_arc_callback():
    """
    GPU draw callback for rotation arc visualization during operator modal.

    Reads arc parameters from the WindowManager runtime group set by the
    rotation operators. Called every frame by the SpaceView3D draw handler.
    """
    try:
        context = bpy.context
        if not context:
            return

        # Transient drag state lives on the WindowManager runtime group
        runtime = getattr(context.window_manager, 'cut_plane_runtime', None)
        if runtime is None:
            return

        rotation_active = runtime.rotation_active
        marker_active = runtime.snap_marker_active
        # Keyboard transforms (tool G/R/S): the gizmo group hides itself
        # (CutPlaneGroup._hide_all_gizmos, via hide flags), so draw the live
        # plane outline here instead. No rotation arc, no snap ticks:
        # keyboard ops show a single clean plane representation (user
        # decision). The snap MARKER is different - it is snap-target
        # feedback (which vertex/edge/face the plane will land on), not a
        # gizmo visual, so it draws for BOTH invocation kinds (native shows
        # the snap indicator during keyboard G too).
        keyboard_transform = any_keyboard_transform_active()
        ghosts = _mirror_ghosts_wanted(context)
        if not (rotation_active or marker_active or keyboard_transform
                or ghosts):
            return
    except Exception:
        # Context may be invalid during some operations - silently exit
        return

    if ghosts:
        # Mirror-plane ghosts show in EVERY state (gizmo idle and mid-drag):
        # the user must see where the mirrored cuts will land at all times.
        _draw_mirror_ghosts(context)
    if keyboard_transform:
        # (constraint axis lines draw in the POST_PIXEL pass - display space,
        # after color management, so their color matches native 1:1)
        _draw_plane_overlay(context)
    elif rotation_active:
        _draw_active_rotation_arc(context, runtime)
    if marker_active:
        _draw_snap_marker_3d(
            context, Vector(runtime.snap_marker_location), runtime.snap_marker_element)


# World-space half-length of a constraint axis line - long enough to leave the
# view in any sane scene, so it reads as infinite like the native line
_CONSTRAINT_LINE_HALF_LENGTH = 10000.0
# Anti-aliased line width in native pixels (multiplied by the UI pixel size);
# matches the visual weight of Blender's own constraint/axis lines
_CONSTRAINT_LINE_WIDTH = 2.0
# View-space near distance the 3D line is clipped to before 2D projection
# (points behind the camera cannot be projected)
_NEAR_CLIP = 0.001


def _visible_segment_2d(context, anchor: Vector, direction: Vector):
    """Project the infinite 3D constraint line to a drawable 2D region segment.

    Near-clips the (long) 3D segment in view space first - endpoints behind
    the camera have no 2D projection - then maps both ends to region pixels.
    Returns (p1, p2) as 2D tuples, or None when the line is entirely behind
    the camera / unprojectable.
    """
    rv3d = context.region_data
    if rv3d is None:
        return None
    view = rv3d.view_matrix
    span = direction * _CONSTRAINT_LINE_HALF_LENGTH
    v1 = view @ (anchor - span)   # view space: camera looks down -Z
    v2 = view @ (anchor + span)

    if rv3d.is_perspective:
        z1, z2 = v1.z, v2.z
        if z1 > -_NEAR_CLIP and z2 > -_NEAR_CLIP:
            return None  # whole line behind the camera
        if z1 > -_NEAR_CLIP or z2 > -_NEAR_CLIP:
            # Clip the behind-camera end to the near plane
            t = (-_NEAR_CLIP - z1) / (z2 - z1)
            clipped = v1.lerp(v2, t)
            if z1 > -_NEAR_CLIP:
                v1 = clipped
            else:
                v2 = clipped

    view_inv = view.inverted()
    p1 = view3d_utils.location_3d_to_region_2d(context.region, rv3d, view_inv @ v1)
    p2 = view3d_utils.location_3d_to_region_2d(context.region, rv3d, view_inv @ v2)
    if p1 is None or p2 is None:
        return None
    return p1, p2


def draw_constraint_lines_px() -> None:
    """POST_PIXEL: native-look constraint axis line(s) during any constrained
    transform - keyboard G/R + X/Y/Z AND gizmo-handle drags (native draws the
    axis line for both: its gizmo arrows/dials invoke the same constrained
    transform, and drawConstraint doesn't care how it was invoked).

    Drawn in the 2D pixel pass ON PURPOSE - two things need it for the 1:1
    native look:
      * COLOR: POST_VIEW draws pass through the viewport's scene color
        management (AgX washes the theme color out - the pale line bug);
        POST_PIXEL is display space, the same stage native overlays draw in,
        so the theme axis color arrives on screen untouched.
      * SMOOTHNESS: POLYLINE_UNIFORM_COLOR is the anti-aliased line shader
        the native overlays use (plain GL lines have no AA - the jaggies).

    Reads the lines off the running modal instance (constraint_visual_lines) -
    the operator owns its constraint state; this layer only renders it.
    """
    try:
        inst = active_transform()
        spec_fn = getattr(inst, 'constraint_visual_lines', None)
        if spec_fn is None:
            return
        specs = spec_fn()
        if not specs:
            return
        context = bpy.context

        shader = get_polyline_shader()
        saved_blend = gpu.state.blend_get()
        try:
            gpu.state.blend_set('ALPHA')
            shader.bind()
            viewport = gpu.state.viewport_get()
            shader.uniform_float("viewportSize", (viewport[2], viewport[3]))
            shader.uniform_float(
                "lineWidth",
                _CONSTRAINT_LINE_WIDTH * context.preferences.system.pixel_size)
            for axis_id, anchor, direction in specs:
                seg = _visible_segment_2d(context, anchor, direction)
                if seg is None:
                    continue
                (x1, y1), (x2, y2) = seg
                batch = batch_for_shader(
                    shader, 'LINES', {"pos": [(x1, y1, 0.0), (x2, y2, 0.0)]})
                shader.uniform_float("color", (*get_theme_axis_color(axis_id), 1.0))
                batch.draw(shader)
        finally:
            gpu.state.blend_set(saved_blend)
    except Exception as e:
        logger.error(f"[CUT PLANE] Error drawing constraint lines: {e}")


def _mirror_ghosts_wanted(context) -> bool:
    """True when mirror-plane ghosts should draw: any mirror axis enabled,
    edit-mesh mode, and OUR tool active (checked here in POST_VIEW, where
    tool detection has a valid region - the watchdog does the same)."""
    try:
        sc = getattr(context.window_manager, 'cut_plane', None)
        if sc is None or not (sc.mirror_x or sc.mirror_y or sc.mirror_z):
            return False
        if not sc.show_mirror_planes:
            return False  # the cuts still mirror; only the visuals are off
        if getattr(context, 'mode', None) != 'EDIT_MESH':
            return False
        return is_plane_tool_active(context)
    except Exception:
        return False


def _draw_mirror_ghosts(context) -> None:
    """Dim outline copies of the plane at its mirrored positions.

    Mirrors reflect across each object's local axes through its origin - the
    same Mirror-modifier semantics the cut uses
    (cutting._mirrored_planes_local) - so the ghosts show exactly where the
    mirrored cuts will land, before the plane even touches the mesh.

    Ghosts are drawn for EVERY object in the edit session, because the cut
    runs per object: each one reflects across its OWN origin, so N objects at
    different locations mean N different sets of mirrored cuts. Drawing only
    the active object's set advertised cuts that never happen and hid cuts
    that do (measured in a two-cube session).
    """
    try:
        sc = getattr(context.window_manager, 'cut_plane', None)
        pose = get_plane_pose(context)
        if sc is None or pose is None:
            return
        axes = [i for i, on in enumerate(
            (sc.mirror_x, sc.mirror_y, sc.mirror_z)) if on]
        if not axes:
            return
        pos, rot = pose
        world = Matrix.Translation(pos) @ rot
        for obj in cutting._edit_mesh_objects(context):
            try:
                inv = obj.matrix_world.inverted()
            except ValueError:
                continue  # singular transform - nothing sane to mirror across
            local = inv @ world
            matrices = [local]
            for i in axes:
                refl = Matrix.Identity(4)
                refl[i][i] = -1.0
                matrices += [refl @ m for m in matrices]
            for m in matrices[1:]:  # [0] is the real plane - the gizmo draws it
                draw_plane_at_matrix(obj.matrix_world @ m, scale=PLANE_SIZE,
                                     scale_x=sc.x_scale, scale_y=sc.y_scale,
                                     color=MIRROR_GHOST_COLOR)
    except Exception as e:
        logger.error(f"[CUT PLANE] Error drawing mirror ghosts: {e}")


def _draw_plane_overlay(context) -> None:
    """The SOLE plane visual during a keyboard transform (G/R/S).

    The gizmo group hides itself while a keyboard transform runs (see
    CutPlaneGroup._hide_all_gizmos), so this outline is the only plane
    representation -
    tracking the live tool properties every frame. Mirrors the gizmo group's
    plane-visual placement (origin + orientation-aware offset + center offset,
    gizmo rotation, x/y scale) so the cut plane reappears on confirm exactly
    where the overlay was.
    """
    try:
        sc = getattr(context.window_manager, 'cut_plane', None)
        pose = get_plane_pose(context)
        if sc is None or pose is None:
            return
        pos, rot = pose
        draw_plane_at_matrix(Matrix.Translation(pos) @ rot,
                             scale=PLANE_SIZE, scale_x=sc.x_scale, scale_y=sc.y_scale)
    except Exception as e:
        logger.error(f"[CUT PLANE] Error drawing plane overlay: {e}")


def _draw_active_rotation_arc(context, runtime) -> None:
    """Draw the rotation arc + snap ticks for the active dial drag."""
    # Arc parameters from the WM runtime group (written by the rotation modals)
    center = Vector(runtime.rotation_arc_center)
    axis = Vector(runtime.rotation_arc_axis)
    start_angle = runtime.rotation_arc_start_angle
    current_angle = runtime.rotation_arc_current_angle
    arc_scale = runtime.rotation_arc_scale  # 1.0 for axis dials, 1.2 for view dial

    # Native uses hardcoded gray for arc fill (dial3d_gizmo.cc:396)
    # The axis color is only used for helplines, not the fill wedge
    fill_color = (0.8, 0.8, 0.8, 0.6)  # Gray with alpha

    # Get axis color for helplines (red/green/blue for X/Y/Z axis, white for view)
    axis_color = tuple(runtime.rotation_arc_color)

    # Zero axis has no dial basis (build_dial_basis would produce NaNs)
    if axis.length_squared < ZERO_EPSILON:
        return

    # The view of the region BEING DRAWN: this handler runs once per WINDOW
    # region, and SpaceView3D.region_3d is the space's own (quad view: "the
    # camera region"), so it names the wrong view in 3 of a quad view's 4
    # regions. context.region_data is the current one - the same source every
    # transform modal already reads.
    rv3d = context.region_data
    if not rv3d:
        return

    # Draw the arc with helplines
    # Note: current_angle is the rotation DELTA, not the absolute end angle
    # So end_angle = start_angle + current_angle (delta)
    _draw_arc_3d(center, axis, start_angle, start_angle + current_angle, fill_color, arc_scale, axis_color)

    # Draw snap tick marks while snapping (matches native dial3d_gizmo.cc)
    if runtime.rotation_snap_active:
        # Shared orthonormal basis (same as _draw_arc_3d and the operators)
        x_axis, y_axis = build_dial_basis(axis)

        # Calculate zoom-independent radius (same as _draw_arc_3d).
        # x ui_scale: the arc is calibrated to touch the dial ring, whose
        # size the gizmo system scales by UI_SCALE_FAC - without the factor
        # the arc reached only halfway to the ring on a 2x display.
        world_per_px = get_pixel_world_size(context.region, rv3d, center)
        radius = ARC_RADIUS_PIXELS * ui_scale_factor() * world_per_px * arc_scale

        # Tick marks at the angle increment, ROTATED by the start angle (like
        # native): the rotation AMOUNT snaps to clean multiples, so the ticks
        # must be measured from where the drag started for the arc end to
        # land exactly on them.
        _draw_snap_ticks_3d(center, x_axis, y_axis, radius,
                            runtime.rotation_snap_increment, start_angle)


# Snap marker appearance - matches native cursor_point_draw()
# (view3d_cursor_snap.cc): screen-aligned shapes per element type,
# radius = 2.5 * theme vertex size in pixels, line width 1.5
SNAP_MARKER_RADIUS_FACTOR = 2.5
SNAP_MARKER_LINE_WIDTH = 1.5
SNAP_MARKER_SEGMENTS = 24

# Unit-space outlines per snap element, verbatim from native:
_MARKER_CIRCLE = tuple(
    (math.cos((2.0 * math.pi / SNAP_MARKER_SEGMENTS) * i),
     math.sin((2.0 * math.pi / SNAP_MARKER_SEGMENTS) * i))
    for i in range(SNAP_MARKER_SEGMENTS)
)
# Vertex: square (native SCE_SNAP_TO_VERTEX = POINT | EDGE_ENDPOINT; a
# mesh vertex resolves to EDGE_ENDPOINT, which draws the square - the
# circle + X cross POINT shape is only used for point objects)
_MARKER_SQUARE = ((-1.0, -1.0), (-1.0, 1.0), (1.0, 1.0), (1.0, -1.0))
# Edge: line loop in crossing order = bowtie/hourglass
_MARKER_BOWTIE = ((-1.0, -1.0), (1.0, 1.0), (-1.0, 1.0), (1.0, -1.0))
# Edge center: triangle
_MARKER_TRIANGLE = ((-1.0, -1.0), (0.0, 0.866), (1.0, -1.0))
# Face center dot (small filled disc instead of a GL point)
_MARKER_DOT = tuple(
    (0.18 * math.cos((2.0 * math.pi / 8) * i),
     0.18 * math.sin((2.0 * math.pi / 8) * i))
    for i in range(8)
)


def _draw_snap_marker_3d(context, location: Vector, element: str) -> None:
    """Draw the native-style snap indicator at the snap point.

    Shapes, sizing, and screen alignment match native cursor_point_draw()
    in view3d_cursor_snap.cc:
    - VERTEX: square (the native edge-endpoint shape mesh vertices use)
    - EDGE: bowtie
    - EDGE_MIDPOINT: triangle
    - FACE_MIDPOINT: circle + center dot
    - FACE (and fallback): circle
    Grid/increment snapping draws nothing, also like native.
    """
    try:
        # region_data, not space_data.region_3d: the view of the region being
        # drawn (see _draw_active_rotation_arc).
        rv3d = context.region_data
        region = context.region
        if rv3d is None or region is None:
            return

        try:
            view_inv = rv3d.view_matrix.inverted()
        except ValueError:
            return
        x_axis = view_inv.col[0].xyz.normalized()
        y_axis = view_inv.col[1].xyz.normalized()

        try:
            vertex_size = bpy.context.preferences.themes[0].view_3d.vertex_size
        except (AttributeError, IndexError):
            vertex_size = 3.0
        # x pixel_size: native's radius formula multiplies ED_view3d_pixel_
        # size (= the no_ui_scale quantity get_pixel_world_size measures,
        # TIMES U.pixelsize) - without it the markers rendered at half
        # logical size on a 2x Retina backing store.
        size = (SNAP_MARKER_RADIUS_FACTOR * vertex_size * pixel_size_factor()
                * get_pixel_world_size(region, rv3d, location))

        def to_world(points):
            return [location + (x_axis * px + y_axis * py) * size for px, py in points]

        # (gpu primitive, unit-space points) per shape part
        if element == 'VERTEX':
            shapes = (('LINE_LOOP', _MARKER_SQUARE),)
        elif element == 'EDGE':
            shapes = (('LINE_LOOP', _MARKER_BOWTIE),)
        elif element == 'EDGE_MIDPOINT':
            shapes = (('LINE_LOOP', _MARKER_TRIANGLE),)
        elif element == 'FACE_MIDPOINT':
            shapes = (('LINE_LOOP', _MARKER_CIRCLE), ('TRI_FAN', _MARKER_DOT))
        else:  # FACE and fallback
            shapes = (('LINE_LOOP', _MARKER_CIRCLE),)

        # Native draws the snap point in the active-object accent orange
        color = (*get_theme_active_object_color(), 1.0)

        # Outlines through POLYLINE (Metal drops plain line widths - see
        # draw_plane_at_matrix); the dot is a fill, no width involved.
        line_shader = get_polyline_shader()
        fill_shader = get_uniform_color_shader()
        saved_blend = gpu.state.blend_get()
        try:
            gpu.state.blend_set('ALPHA')
            viewport = gpu.state.viewport_get()
            for primitive, points in shapes:
                if primitive == 'LINE_LOOP':
                    # POLYLINE has no LINE_LOOP topology - close the loop by
                    # repeating the first vertex in a LINE_STRIP.
                    pts = to_world(points)
                    pts.append(pts[0])
                    line_shader.bind()
                    line_shader.uniform_float(
                        "viewportSize", (viewport[2], viewport[3]))
                    line_shader.uniform_float(
                        "lineWidth",
                        SNAP_MARKER_LINE_WIDTH * pixel_size_factor())
                    line_shader.uniform_float("color", color)
                    batch = batch_for_shader(
                        line_shader, 'LINE_STRIP', {"pos": pts})
                    batch.draw(line_shader)
                else:
                    fill_shader.bind()
                    fill_shader.uniform_float("color", color)
                    batch = batch_for_shader(
                        fill_shader, primitive, {"pos": to_world(points)})
                    batch.draw(fill_shader)
        finally:
            gpu.state.blend_set(saved_blend)

    except Exception as e:
        logger.error(f"[CUT PLANE] Error drawing snap marker: {e}")


def _draw_helpline_3d(center: Vector, x_axis: Vector, y_axis: Vector,
                      angle: float, radius: float, color: tuple, line_width: float) -> None:
    """
    Draw a single helpline from center outward at given angle.

    Matches native Blender dial3d_gizmo.cc:dial_ghostarc_draw_helpline().
    """
    try:
        x = math.cos(angle) * radius
        y = math.sin(angle) * radius
        endpoint = center + x_axis * x + y_axis * y

        # POLYLINE (Metal drops plain line widths - see draw_plane_at_matrix)
        shader = get_polyline_shader()
        batch = batch_for_shader(shader, 'LINES', {"pos": [center, endpoint]})

        shader.bind()
        viewport = gpu.state.viewport_get()
        shader.uniform_float("viewportSize", (viewport[2], viewport[3]))
        shader.uniform_float("lineWidth", line_width * pixel_size_factor())
        shader.uniform_float("color", color)
        batch.draw(shader)

    except Exception as e:
        logger.error(f"[CUT PLANE] Error drawing helpline: {e}")


# Memoized unit-circle tick directions per snap increment (avoids
# recomputing trig every frame; the increment rarely changes)
_SNAP_TICK_CACHE = {}


def _get_snap_tick_directions(snap_increment: float):
    """Unit circle (cos, sin) pairs at multiples of snap_increment."""
    key = round(snap_increment, 6)
    directions = _SNAP_TICK_CACHE.get(key)
    if directions is None:
        if snap_increment > 1e-6:
            count = max(2, min(720, int(round((2.0 * math.pi) / snap_increment))))
        else:
            count = 72
        directions = [
            (math.cos(snap_increment * i), math.sin(snap_increment * i))
            for i in range(count)
        ]
        _SNAP_TICK_CACHE[key] = directions
    return directions


def _draw_snap_ticks_3d(center: Vector, x_axis: Vector, y_axis: Vector,
                        radius: float, snap_increment: float,
                        offset_angle: float = 0.0) -> None:
    """
    Draw snap increment tick marks around the dial.

    Matches native Blender dial3d_gizmo.cc:dial_ghostarc_draw_incremental_angle(),
    including its offset: ticks are measured from *offset_angle* (the drag start),
    because the rotation AMOUNT snaps to clean increments - so tick positions
    must be start + n*increment for the arc end to land on them.
    """
    try:
        # Native ticks start a gap OUTSIDE the dial edge (DIAL_WIDTH * 1.1)
        # and extend outward from there (* 1.1 again)
        vertices = []
        inner_radius = radius * TICK_INNER_FACTOR
        outer_radius = radius * TICK_OUTER_FACTOR

        # Apply the offset by rotating the basis (the per-increment unit-circle
        # directions stay cacheable): a point at angle theta+offset in (x, y)
        # equals a point at theta in this rotated basis.
        cos_o, sin_o = math.cos(offset_angle), math.sin(offset_angle)
        bx = x_axis * cos_o + y_axis * sin_o
        by = -x_axis * sin_o + y_axis * cos_o

        for cos_a, sin_a in _get_snap_tick_directions(snap_increment):
            direction = bx * cos_a + by * sin_a
            vertices.extend([center + direction * outer_radius,
                             center + direction * inner_radius])

        # Native draws these at exactly U.pixelsize, as a POLYLINE uniform
        # (dial3d_gizmo.cc) with NO C-side multiply. The old
        # line_width_set(pixel_size) was a latent DOUBLE-scale: the C
        # front-end multiplies every line_width_set by U.pixelsize again
        # (pixel_size squared physical px at factor 2).
        shader = get_polyline_shader()
        batch = batch_for_shader(shader, 'LINES', {"pos": vertices})

        shader.bind()
        viewport = gpu.state.viewport_get()
        shader.uniform_float("viewportSize", (viewport[2], viewport[3]))
        shader.uniform_float("lineWidth", pixel_size_factor())
        shader.uniform_float("color", (1.0, 1.0, 1.0, 1.0))
        batch.draw(shader)

    except Exception as e:
        logger.error(f"[CUT PLANE] Error drawing snap ticks: {e}")


def _draw_arc_3d(center: Vector, axis: Vector, start_angle: float,
                end_angle: float, color: tuple, scale: float = 1.0,
                helpline_color: tuple = None) -> None:
    """
    Draw a 3D arc around an axis with zoom-independent size.
    """
    angle_diff = end_angle - start_angle
    if abs(angle_diff) < 0.001:
        return

    segments = min(max(8, int(abs(angle_diff) * 20)), 128)

    # Constant screen-size radius (native: ED_view3d_pixel_size_no_ui_scale)
    context = bpy.context
    radius = 1.0
    # region + region_data must describe the SAME region or the screen-size
    # radius is computed from a foreign view (see _draw_active_rotation_arc).
    if context.region and context.region_data:
        # x ui_scale: calibrated against the natively-scaled dial ring (see
        # the snap-tick radius above).
        world_per_px = get_pixel_world_size(context.region, context.region_data,
                                            center)
        radius = (ARC_RADIUS_PIXELS * ui_scale_factor()
                  * world_per_px * scale)

    # Orthonormal basis with the axis as Z - MUST match the rotation
    # operators' start-angle basis (shared build_dial_basis)
    x_axis, y_axis = build_dial_basis(axis)

    vertices = []
    for i in range(segments + 1):
        angle = start_angle + (i / segments) * angle_diff
        x = math.cos(angle) * radius
        y = math.sin(angle) * radius
        vertices.append(center + x_axis * x + y_axis * y)

    try:
        shader = get_uniform_color_shader()

        saved_blend = gpu.state.blend_get()

        try:
            gpu.state.blend_set('ALPHA')

            # Filled wedge only, no outline - an outline would overlap the
            # dial ring (native draws it fill-only too)
            if abs(angle_diff) > 0.05:
                wedge_verts = [center] + vertices
                wedge_color = (color[0], color[1], color[2], color[3] * 0.5)
                indices = [(0, i + 1, i + 2) for i in range(len(vertices) - 1)]

                if indices:
                    shader.bind()
                    batch_wedge = batch_for_shader(
                        shader, 'TRIS',
                        {"pos": wedge_verts},
                        indices=indices
                    )
                    shader.uniform_float("color", wedge_color)
                    batch_wedge.draw(shader)

            # Helplines in the axis color (the wedge stays gray like native):
            # thin at the start angle, thick at the end
            if helpline_color is not None:
                helpline_rgba = (helpline_color[0], helpline_color[1], helpline_color[2], 1.0)
                _draw_helpline_3d(center, x_axis, y_axis, start_angle, radius, helpline_rgba, 1.0)
                _draw_helpline_3d(center, x_axis, y_axis, end_angle, radius, helpline_rgba, 3.0)

        finally:
            gpu.state.blend_set(saved_blend)

    except Exception as e:
        logger.error(f"[CUT PLANE] Error drawing rotation arc: {e}")


# Handles live in handler_store, which keeps a module global AND a
# driver_namespace mirror: driver_namespace ALONE is wiped by every file load,
# which orphaned both handlers (unregister then popped None and removed
# nothing) and stacked a fresh pair on each re-enable.
_ARC_HANDLER_KEY = "__cut_plane_arc_draw_handler__"
_PIXEL_HANDLER_KEY = "__cut_plane_pixel_draw_handler__"


def register_arc_draw_handler():
    """Register the draw handlers (idempotent across re-execs): the POST_VIEW
    3D overlay (arc, ticks, marker, plane outline) and the POST_PIXEL pass
    (constraint axis lines - display space for the native 1:1 color)."""
    for key, callback, stage in (
            (_ARC_HANDLER_KEY, draw_rotation_arc_callback, 'POST_VIEW'),
            (_PIXEL_HANDLER_KEY, draw_constraint_lines_px, 'POST_PIXEL')):
        handler_store.add(key, bpy.types.SpaceView3D.draw_handler_add,
                          callback, (), 'WINDOW', stage)


def unregister_arc_draw_handler():
    """Unregister both draw handlers."""
    for key in (_ARC_HANDLER_KEY, _PIXEL_HANDLER_KEY):
        handler_store.remove(key)
