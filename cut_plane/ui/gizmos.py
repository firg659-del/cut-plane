# SPDX-License-Identifier: GPL-3.0-or-later
"""Gizmo classes: the visual plane and the 5-axis gizmo group."""

import traceback
from typing import Tuple

import bpy
from bpy.types import Gizmo, GizmoGroup
from mathutils import Matrix, Vector

from ..lib.constants import (  # noqa: I001
    logger,
    _CACHED_ROTATION_ARROW_X,
    _CACHED_ROTATION_ARROW_Y,
    _CACHED_ROTATION_DIAL_X,
    _CACHED_ROTATION_DIAL_Y,
    ARROW_HIDE_THRESHOLD_MIN,
    ARROW_OFFSET_PIXELS,
    DEBUG,
    GIZMO_ARROW_ALPHA,
    GIZMO_ARROW_LINE_WIDTH,
    GIZMO_CENTER_BUTTON_SCALE,
    GIZMO_DIAL_ALPHA,
    GIZMO_DIAL_ALPHA_HIGHLIGHT,
    GIZMO_DIAL_LINE_WIDTH,
    GIZMO_PLANE_OFFSET_PIXELS,
    GIZMO_PLANE_SCALE,
    GIZMO_TRACKBALL_ALPHA,
    GIZMO_TRACKBALL_ALPHA_HIGHLIGHT,
    GIZMO_TRACKBALL_SCALE,
    GIZMO_VIEW_DIAL_SCALE,
    PARALLEL_EPSILON,
    PLANE_SIZE,
)
from .drawing import draw_plane_at_matrix
from ..operators.utils import any_keyboard_transform_active
from ..lib.math_utils import (
    apply_validated_matrix_to_gizmo,
    calculate_axis_visibility,
    flat_to_matrix,
    get_current_orientation_type,
    get_mesh_origin_world,
    get_pixel_world_size,
    get_plane_pose,
    get_theme_active_object_color,
    get_theme_axis_color,
    get_view_direction_at_position,
    is_plane_tool_active,
    ui_scale_factor,
)


def _apply_default_center():
    """One-shot timer body: default-center a virgin plane on the mesh bbox.

    Deferred from CutPlaneGroup.setup() because setup runs in a draw
    context where property writes are forbidden; timers run outside
    it. ensure_default_center is idempotent (once per edit object), so the
    multiple timers from multi-area setups are harmless.
    """
    from ..state.properties import ensure_default_center
    ensure_default_center(bpy.context)
    return None  # one-shot


class CutPlaneVisual(Gizmo):
    """
    Custom gizmo that renders the orange matrix plane.

    This gizmo is purely visual - no interaction.
    The arrow/scale/rotation gizmos handle user input.
    """
    bl_idname = "VIEW3D_GT_plane_visual"

    def draw(self, context) -> None:
        """Draw the plane visual at gizmo matrix position (includes rotation)."""
        try:
            # matrix_basis already holds position + rotation (set per frame)
            scale_x = getattr(self, 'scale_x', 1.0)
            scale_y = getattr(self, 'scale_y', 1.0)

            draw_plane_at_matrix(
                self.matrix_basis,
                scale=PLANE_SIZE,
                scale_x=scale_x,
                scale_y=scale_y
            )
        except Exception as e:
            sx = getattr(self, 'scale_x', 'N/A')
            sy = getattr(self, 'scale_y', 'N/A')
            logger.error(f"[CUT PLANE] Error in CutPlaneVisual.draw() [scale_x={sx}, scale_y={sy}]: {e}")
            logger.error(f"[CUT PLANE] Traceback: {traceback.format_exc()}")

    def draw_select(self, context, select_id: int) -> None:
        """
        Draw selection hitbox (none - plane is visual only).

        Only the manipulation gizmos should be interactive.
        """
        pass  # No hitbox


class CutPlaneGroup(GizmoGroup):
    """The 5-axis gizmo group: move/scale/rotate handle sets + the plane visual.

    One-directional data flow: handle drags invoke the modal operators, the
    operators write scene properties, and refresh()/draw_prepare() read those
    properties back to place every gizmo - operators never position gizmos
    directly. The mode property picks which handle set is visible; the
    GLOBAL/LOCAL orientation is resolved through the math_utils axis helpers.
    """
    bl_idname = "OBJECT_GGT_plane_3_axis"
    bl_label = "Plane 3-Axis Gizmo"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_options = {'3D', 'PERSISTENT'}

    @classmethod
    def poll(cls, context) -> bool:
        """Show only while OUR workspace tool is the active tool.

        Gating on the active tool (not just Edit Mode) means the gizmo appears
        when the user picks the Cut Plane from the toolbar and disappears when
        they switch to another tool - it does not show on plain Edit Mode entry.
        This matches the live-cut watchdog, which also keys off tool-active.
        """
        if not (context.mode == 'EDIT_MESH'
                and context.edit_object
                and context.edit_object.type == 'MESH'
                and context.edit_object.data):  # ensure mesh data exists
            return False
        # NOTE: keyboard transforms (G/R/S) hide the group via HIDE FLAGS in
        # refresh()/draw_prepare(), deliberately NOT via poll: returning False
        # here makes Blender FREE and re-create the group, which (a) made the
        # reappearance depend on a gizmo-map refresh ("sometimes shows,
        # sometimes not") and (b) re-created the cut plane without its
        # scale_x/scale_y attributes, so it drew at the 1.0 default until the
        # next refresh ("scale reverts"). Hide flags keep the instance alive.
        # Shared with the live-cut watchdog (is_plane_tool_active) so the gizmo
        # and the destructive preview never disagree about tool-active state.
        return is_plane_tool_active(context)

    def _create_operator_arrow(
        self,
        color: Tuple[float, float, float],
        operator_id: str,
        constraint: str,
        draw_style: str = 'NORMAL'
    ) -> Gizmo:
        """
        Create an arrow gizmo bound to an operator with axis constraint.
        """
        gz = self.gizmos.new("GIZMO_GT_arrow_3d")
        gz.draw_style = draw_style
        gz.color = color
        gz.color_highlight = color
        gz.alpha = GIZMO_ARROW_ALPHA
        gz.use_draw_value = True
        gz.line_width = GIZMO_ARROW_LINE_WIDTH
        gz.length = 0.8
        op_props = gz.target_set_operator(operator_id)
        op_props.constraint_axis = constraint
        return gz

    def _create_dial_gizmo(
        self,
        color: Tuple[float, float, float],
        operator_id: str
    ) -> Gizmo:
        """
        Helper to create and configure dial gizmo for rotation.

        Uses operator binding for latency-free rotation matching native feel.
        """
        gz = self.gizmos.new("GIZMO_GT_dial_3d")
        gz.draw_options = {'CLIP'}
        gz.color = color
        gz.color_highlight = color
        gz.alpha = GIZMO_DIAL_ALPHA
        gz.alpha_highlight = GIZMO_DIAL_ALPHA_HIGHLIGHT
        gz.use_draw_value = True
        gz.line_width = GIZMO_DIAL_LINE_WIDTH  # Axis dials use thicker lines (3.0)
        gz.target_set_operator(operator_id)
        return gz

    def _create_plane_handler(
        self,
        color: Tuple[float, float, float],
        operator_id: str,
        constraint: str,
        is_plane_constraint: bool = False
    ) -> Gizmo:
        """
        Create a plane handler gizmo (small square for 2D planar movement/scale).
        """
        gz = self.gizmos.new("GIZMO_GT_primitive_3d")
        gz.draw_style = 'PLANE'
        gz.use_draw_modal = True
        gz.color = color
        gz.color_highlight = color
        gz.alpha = GIZMO_ARROW_ALPHA
        gz.scale_basis = GIZMO_PLANE_SCALE
        op_props = gz.target_set_operator(operator_id)
        op_props.constraint_axis = constraint
        if is_plane_constraint:
            op_props.is_plane_constraint = True
        return gz

    def setup(self, context) -> None:
        """
        Create gizmos: X/Y/Z manipulation gizmos + visual plane.

        Called once when gizmo group is created.
        """
        # Default plane placement: a VIRGIN plane centers on the mesh bbox,
        # once per edit object. setup() only runs when OUR tool is active in
        # an edit session - exactly the right event - but it runs in a draw
        # context where property writes are forbidden, hence the one-shot timer.
        bpy.app.timers.register(_apply_default_center, first_interval=0.0)

        props = context.window_manager.cut_plane
        self.rotation_matrix = flat_to_matrix(props.rotation_matrix_flat)

        # Theme colors, read once
        color_x = get_theme_axis_color('X')
        color_y = get_theme_axis_color('Y')
        color_z = get_theme_axis_color('Z')
        # Accent for neutral elements: ties them visually to the orange
        # plane (this is a plane manipulator, not native transform)
        color_accent = get_theme_active_object_color()

        # Move gizmos (NORMAL arrow style with stems, operator-bound)
        self.gz_arrow_x = self._create_operator_arrow(color_x, "view3d.cut_plane_move_axis", "X")
        self.gz_arrow_y = self._create_operator_arrow(color_y, "view3d.cut_plane_move_axis", "Y")
        self.gz_arrow_z = self._create_operator_arrow(color_z, "view3d.cut_plane_move_axis", "Z")

        # Default highlight alpha, restored after a drag ends (the dragged
        # arrow is made fully transparent - see _draw_prepare_move_mode)
        self._arrow_alpha_highlight = self.gz_arrow_x.alpha_highlight

        # Scale gizmos (BOX arrow style, operator-bound)
        self.gz_scale_x = self._create_operator_arrow(color_x, "view3d.cut_plane_scale_plane", "ONLY_X", draw_style='BOX')
        self.gz_scale_y = self._create_operator_arrow(color_y, "view3d.cut_plane_scale_plane", "ONLY_Y", draw_style='BOX')
        self.gz_scale_z = self._create_operator_arrow(color_z, "view3d.cut_plane_scale_plane", "ONLY_Z", draw_style='BOX')

        # Scale Plane Handlers (XY=Blue/Z-lock, YZ=Red/X-lock, XZ=Green/Y-lock)
        self.gz_scale_plane_xy = self._create_plane_handler(color_z, "view3d.cut_plane_scale_plane", "Z")
        self.gz_scale_plane_yz = self._create_plane_handler(color_x, "view3d.cut_plane_scale_plane", "X")
        self.gz_scale_plane_xz = self._create_plane_handler(color_y, "view3d.cut_plane_scale_plane", "Y")

        # Uniform scale: one ANNULUS like native's, clickable between the
        # inner and outer circles
        self.gz_scale_uniform = self.gizmos.new("GIZMO_GT_primitive_3d")
        self.gz_scale_uniform.draw_style = 'ANNULUS'
        self.gz_scale_uniform.draw_inner = False
        # inner radius = outer * arc_inner_factor (~0.2 / 1.2 here)
        self.gz_scale_uniform.arc_inner_factor = GIZMO_CENTER_BUTTON_SCALE / GIZMO_VIEW_DIAL_SCALE
        self.gz_scale_uniform.color = color_accent
        self.gz_scale_uniform.color_highlight = color_accent
        self.gz_scale_uniform.alpha = GIZMO_ARROW_ALPHA
        self.gz_scale_uniform.alpha_highlight = GIZMO_DIAL_ALPHA_HIGHLIGHT
        self.gz_scale_uniform.line_width = 2.0
        self.gz_scale_uniform.scale_basis = GIZMO_VIEW_DIAL_SCALE
        # Background-select: the big ring hitbox must not steal clicks from
        # the axis handlers
        self.gz_scale_uniform.use_select_background = True
        self.gz_scale_uniform.select_bias = -2.0
        self.gz_scale_uniform.use_draw_modal = True
        op_props = self.gz_scale_uniform.target_set_operator("view3d.cut_plane_scale_plane")
        op_props.constraint_axis = ""  # empty = uniform scale

        # Trackball dial (filled circle, invisible until hovered). Created
        # FIRST: selection walks the gizmo list backward, so first-added =
        # lowest pick priority, matching native ordering.
        self.gz_dial_trackball = self.gizmos.new("GIZMO_GT_dial_3d")
        self.gz_dial_trackball.draw_options = {'FILL'}
        self.gz_dial_trackball.color = (0.8, 0.8, 0.8)
        self.gz_dial_trackball.color_highlight = (0.8, 0.8, 0.8)
        self.gz_dial_trackball.alpha = GIZMO_TRACKBALL_ALPHA
        self.gz_dial_trackball.alpha_highlight = GIZMO_TRACKBALL_ALPHA_HIGHLIGHT
        self.gz_dial_trackball.use_draw_value = True
        self.gz_dial_trackball.line_width = GIZMO_ARROW_LINE_WIDTH
        self.gz_dial_trackball.scale_basis = GIZMO_TRACKBALL_SCALE
        self.gz_dial_trackball.target_set_operator("view3d.cut_plane_trackball")
        # Python spelling of WM_GIZMO_SELECT_BACKGROUND + native's -2.0 bias
        self.gz_dial_trackball.use_select_background = True
        self.gz_dial_trackball.select_bias = -2.0

        # Rotation dial gizmos (axis-constrained) - uses operator binding for native feel
        # Created AFTER trackball so they have higher selection priority
        self.gz_dial_x = self._create_dial_gizmo(
            color_x, "view3d.cut_plane_rotate_x"
        )
        self.gz_dial_y = self._create_dial_gizmo(
            color_y, "view3d.cut_plane_rotate_y"
        )
        self.gz_dial_z = self._create_dial_gizmo(
            color_z, "view3d.cut_plane_rotate_z"
        )

        # No use_grab_cursor on the dials: native wraps the cursor during a
        # rotation (WM_GIZMO_MOVE_CURSOR) but never hides it - the Python
        # flag hides it, so it stays off.

        # View-aligned rotation dial (the outer circle); empty draw_options
        # gives the full unclipped circle
        self.gz_dial_view = self.gizmos.new("GIZMO_GT_dial_3d")
        self.gz_dial_view.draw_options = set()
        self.gz_dial_view.color = color_accent
        self.gz_dial_view.color_highlight = color_accent
        self.gz_dial_view.alpha = GIZMO_DIAL_ALPHA
        self.gz_dial_view.alpha_highlight = GIZMO_DIAL_ALPHA_HIGHLIGHT
        self.gz_dial_view.use_draw_value = True
        self.gz_dial_view.line_width = GIZMO_ARROW_LINE_WIDTH
        self.gz_dial_view.scale_basis = GIZMO_VIEW_DIAL_SCALE
        self.gz_dial_view.target_set_operator("view3d.cut_plane_rotate_view")

        # Center drag button (free move). Native builds this from
        # GIZMO_GT_primitive_3d with an operator binding
        # (transform_gizmo_3d.cc:1415), which the Python API can't do -
        # move_3d with RING_2D is the closest equivalent.
        self.gz_center = self.gizmos.new("GIZMO_GT_move_3d")
        self.gz_center.draw_style = 'RING_2D'
        self.gz_center.color = color_accent
        self.gz_center.color_highlight = color_accent
        self.gz_center.alpha = GIZMO_ARROW_ALPHA
        self.gz_center.alpha_highlight = GIZMO_DIAL_ALPHA_HIGHLIGHT
        self.gz_center.line_width = 2.0  # native GIZMO_AXIS_LINE_WIDTH
        self.gz_center.scale_basis = GIZMO_CENTER_BUTTON_SCALE
        self.gz_center.select_bias = 2.0  # picked before the arrows, like native
        self.gz_center.use_draw_modal = True

        # Explicit empty constraint: operator properties persist per binding,
        # and a leftover axis from a plane handler would constrain the center
        props = self.gz_center.target_set_operator("view3d.cut_plane_move_center")
        props.constraint_axis = ""

        # 2D Plane Handlers (XY=Blue/Z-normal, YZ=Red/X-normal, XZ=Green/Y-normal)
        self.gz_plane_xy = self._create_plane_handler(color_z, "view3d.cut_plane_move_center", "Z", is_plane_constraint=True)
        self.gz_plane_yz = self._create_plane_handler(color_x, "view3d.cut_plane_move_center", "X", is_plane_constraint=True)
        self.gz_plane_xz = self._create_plane_handler(color_y, "view3d.cut_plane_move_center", "Y", is_plane_constraint=True)

        # Visual cut plane
        self.gz_plane = self.gizmos.new(CutPlaneVisual.bl_idname)
        self.gz_plane.use_draw_modal = True  # Keep visible during modal operations (drag)

    def _get_gizmo_properties(self, context) -> dict:
        """One read of the per-frame placement inputs. Positioning itself
        goes through get_plane_pose - only these four are read directly."""
        props = context.window_manager.cut_plane

        return {
            'origin': get_mesh_origin_world(context),
            'mode': props.mode,
            'x_scale': props.x_scale,
            'y_scale': props.y_scale,
        }

    def _hide_all_gizmos(self) -> bool:
        """Hide the whole group (plane included) while a keyboard transform runs.

        Called at the TOP of refresh()/draw_prepare(). Returns True when hidden,
        so the caller can skip its positioning work. Uses hide flags rather than
        poll() so the group instance (and the plane's scale_x/scale_y state)
        survives the transform and reappears on the very next frame after
        confirm - matching native transform-gizmo behavior. The live plane
        outline is drawn by drawing._draw_plane_overlay meanwhile.
        """
        if not any_keyboard_transform_active():
            return False
        if hasattr(self, 'gz_plane'):
            for gz in self.gizmos:
                gz.hide = True
        return True

    def _apply_handles_toggle(self, context) -> None:
        """Hide every interactive handle when Show Gizmo Handles is off.

        Runs LAST in refresh()/draw_prepare(), after the per-mode logic set the
        hide flags, so it wins regardless of mode/visibility math. The plane
        visual stays (it is display, not a handle). Hidden gizmos are also not
        clickable, so this fully switches the tool to keyboard (G/R/S) driving.
        """
        props = getattr(context.window_manager, 'cut_plane', None)
        if props is None or props.show_gizmo_handles or not hasattr(self, 'gz_plane'):
            return
        for gz in self.gizmos:
            if gz is not self.gz_plane:
                gz.hide = True

    def _update_visibility(self, mode: str) -> None:
        """
        Update visibility of gizmo elements based on the active mode.
        
        This is called from both refresh() and draw_prepare() to ensure
        immediate visual updates even if refresh() lags behind.
        """
        if not hasattr(self, 'gz_arrow_x'):
            return  # setup() hasn't run for this group instance yet

        # The plane visual is ALWAYS visible while the group shows. This must
        # be explicit: _hide_all_gizmos() hides it during keyboard transforms,
        # and unlike the handles below nothing else ever recomputes its hide
        # flag - without this line the plane stayed hidden after the first
        # keyboard transform confirmed.
        self.gz_plane.hide = False

        # Move Mode
        is_move = (mode == 'MOVE')
        self.gz_arrow_x.hide = not is_move
        self.gz_arrow_y.hide = not is_move
        self.gz_arrow_z.hide = not is_move
        self.gz_center.hide = not is_move
        
        # Planes are part of Move mode
        self.gz_plane_xy.hide = not is_move
        self.gz_plane_yz.hide = not is_move
        self.gz_plane_xz.hide = not is_move

        # Scale Mode - all 7 scale gizmos
        is_scale = (mode == 'SCALE')
        self.gz_scale_x.hide = not is_scale
        self.gz_scale_y.hide = not is_scale
        self.gz_scale_z.hide = not is_scale
        self.gz_scale_plane_xy.hide = not is_scale
        self.gz_scale_plane_yz.hide = not is_scale
        self.gz_scale_plane_xz.hide = not is_scale
        self.gz_scale_uniform.hide = not is_scale

        # Rotate Mode
        is_rotate = (mode == 'ROTATE')
        self.gz_dial_x.hide = not is_rotate
        self.gz_dial_y.hide = not is_rotate
        self.gz_dial_z.hide = not is_rotate
        self.gz_dial_view.hide = not is_rotate
        self.gz_dial_trackball.hide = not is_rotate

    # NOTE: there is no _update_move_mode. MOVE (like SCALE) positioning lives
    # exclusively in draw_prepare - a refresh()-time version existed but its
    # results were overwritten by draw_prepare before anything ever drew.

    def _update_rotate_mode(self, context) -> None:
        """Position the three axis dials (ROTATE mode).

        Shared by refresh() and _draw_prepare_rotate_mode(). The dial center
        comes from the single plane-pose source (ROTATE collapses to world-space
        offsets inside it, preventing dial drift).
        """
        pose = get_plane_pose(context)
        if pose is None:
            return
        dial_center, rotation_matrix = pose

        # In LOCAL orientation the dials rotate with the gizmo to stay aligned
        # with its local axes; GLOBAL keeps them world-aligned.
        if get_current_orientation_type(context) == 'LOCAL':
            base_matrix = Matrix.Translation(dial_center) @ rotation_matrix
        else:
            base_matrix = Matrix.Translation(dial_center)

        self.gz_dial_x.matrix_basis = base_matrix @ _CACHED_ROTATION_DIAL_X   # faces YZ
        self.gz_dial_y.matrix_basis = base_matrix @ _CACHED_ROTATION_DIAL_Y   # faces XZ
        self.gz_dial_z.matrix_basis = base_matrix                             # faces XY

        # The view/trackball dials are placed in draw_prepare (they track the camera)

    def _place_plane_visual(self, context, props: dict) -> None:
        """Position + scale the orange plane visual (single place, all modes).

        Uses the plane-pose single source; this fixed a latent inconsistency
        where the refresh-path applied raw offsets without the LOCAL-orientation
        transform (masked because draw_prepare always overwrote it).
        """
        pose = get_plane_pose(context)
        if pose is None:
            return
        plane_matrix = Matrix.Translation(pose[0]) @ self.rotation_matrix
        apply_validated_matrix_to_gizmo(
            self.gz_plane, plane_matrix, props['origin'], self.rotation_matrix)

        # Scale is consumed by CutPlaneVisual.draw(); must be refreshed every
        # frame in EVERY mode (a stale/unset value drew the frozen "duplicate"
        # plane during keyboard S, and 1.0-defaults after group re-creation).
        self.gz_plane.scale_x = props['x_scale']
        self.gz_plane.scale_y = props['y_scale']

    def refresh(self, context) -> None:
        """Reposition on data changes (Blender calls this on notifier events)."""
        try:
            if self._hide_all_gizmos():
                return  # keyboard transform running - overlay draws the plane

            props = self._get_gizmo_properties(context)
            mode = props['mode']
            self._update_visibility(mode)

            # Rotation comes from the tool property; every mode needs it
            # (MOVE/SCALE handles orient by it in LOCAL orientation)
            self.rotation_matrix = flat_to_matrix(
                context.window_manager.cut_plane.rotation_matrix_flat)

            # MOVE and SCALE handle positioning exclusively in draw_prepare()
            # (which runs before every draw); only the axis dials are placed here.
            if mode == 'ROTATE':
                self._update_rotate_mode(context)

            self._place_plane_visual(context, props)

            self._apply_handles_toggle(context)  # keyboard-only mode wins last

        except Exception as e:
            mode = props.get('mode', 'UNKNOWN') if 'props' in locals() else 'UNKNOWN'
            logger.error(f"[CUT PLANE] Error in refresh() [mode={mode}]: {e}")
            logger.error(f"[CUT PLANE] Traceback: {traceback.format_exc()}")

    def _draw_prepare_rotate_mode(self, context, view_aligned_matrix: Matrix) -> None:
        """Place ROTATE-mode gizmos for this frame."""
        self._update_rotate_mode(context)

        # Camera-facing dials track the view every frame
        self.gz_dial_view.matrix_basis = view_aligned_matrix
        self.gz_dial_trackball.matrix_basis = view_aligned_matrix

        # While one dial is being dragged, hide its siblings (native behavior)
        runtime = getattr(context.window_manager, 'cut_plane_runtime', None)
        active_dial = runtime.rotation_active_dial if runtime else ""
        if active_dial:
            self.gz_dial_x.hide = (active_dial != 'X')
            self.gz_dial_y.hide = (active_dial != 'Y')
            self.gz_dial_z.hide = (active_dial != 'Z')
            self.gz_dial_view.hide = (active_dial != 'VIEW')
            self.gz_dial_trackball.hide = (active_dial != 'TRACKBALL')
        else:
            self.gz_dial_x.hide = False
            self.gz_dial_y.hide = False
            self.gz_dial_z.hide = False
            self.gz_dial_view.hide = False
            self.gz_dial_trackball.hide = False

        # (plane visual is placed once for all modes in draw_prepare)

    def _draw_prepare_scale_mode(self, context, center: Vector, rv3d,
                                 view_inv_matrix: Matrix, view_z_axis: Vector,
                                 view_aligned_matrix: Matrix) -> None:
        """Update SCALE mode gizmos for drawing.
        """
        rot = self.rotation_matrix
        # x ui_scale: the HANDLES these offsets place are native gizmo types
        # whose sizes the C pipeline scales by UI_SCALE_FAC - raw-pixel
        # offsets next to them drift on any display where the factor != 1
        # (correct-size handles at half the distance on a 2x Mac).
        world_per_px = get_pixel_world_size(context.region, rv3d, center)
        push_scale = ui_scale_factor() * world_per_px
        arrow_push = ARROW_OFFSET_PIXELS * push_scale   # box handlers: outside the ring
        plane_push = GIZMO_PLANE_OFFSET_PIXELS * push_scale

        # Visibility/alpha fading: scale handlers always use plane-local axes
        view_dir = get_view_direction_at_position(rv3d, view_inv_matrix, view_z_axis, center)
        vis = calculate_axis_visibility(view_dir, rot, 'LOCAL')

        # Axis box handlers: pushed outward along their LOCAL axis, oriented by
        # the plane rotation (then the cached per-axis arrow rotation).
        for axis, arrow_rot in (('x', _CACHED_ROTATION_ARROW_X),
                                ('y', _CACHED_ROTATION_ARROW_Y),
                                ('z', None)):
            local_push = Vector((0.0, 0.0, 0.0))
            setattr(local_push, axis, arrow_push)
            gz = getattr(self, f'gz_scale_{axis}')
            matrix = Matrix.Translation(center + rot @ local_push) @ rot
            gz.matrix_basis = matrix @ arrow_rot if arrow_rot else matrix
            gz.alpha = GIZMO_ARROW_ALPHA * vis[f'alpha_arrow_{axis}']
            gz.hide = (vis[f'idot_{axis}'] < ARROW_HIDE_THRESHOLD_MIN)

        # Plane handlers: pushed diagonally in their LOCAL plane, oriented by the
        # plane rotation (then the cached dial rotation to face their plane).
        # A handler hides when its plane is edge-on (its NORMAL's idot is high).
        for name, push_axes, dial_rot, normal_axis in (
                ('xy', ('x', 'y'), None, 'z'),
                ('yz', ('y', 'z'), _CACHED_ROTATION_DIAL_X, 'x'),
                ('xz', ('x', 'z'), _CACHED_ROTATION_DIAL_Y, 'y')):
            local_push = Vector((0.0, 0.0, 0.0))
            for axis in push_axes:
                setattr(local_push, axis, plane_push)
            gz = getattr(self, f'gz_scale_plane_{name}')
            matrix = Matrix.Translation(center + rot @ local_push) @ rot
            gz.matrix_basis = matrix @ dial_rot if dial_rot else matrix
            gz.alpha = GIZMO_ARROW_ALPHA * vis[f'alpha_plane_{name}']
            gz.hide = ((1.0 - vis[f'idot_{normal_axis}']) < ARROW_HIDE_THRESHOLD_MIN)

        # Uniform Scale Handler - view-aligned ANNULUS (donut) for uniform scaling
        view_aligned_uniform = view_aligned_matrix.copy()
        view_aligned_uniform.col[3][:3] = center
        self.gz_scale_uniform.matrix_basis = view_aligned_uniform

    def _draw_prepare_move_mode(self, context, center: Vector, rv3d,
                                view_inv_matrix: Matrix, view_z_axis: Vector,
                                view_aligned_matrix: Matrix) -> None:
        """Update MOVE mode gizmos for drawing.
        """
        rot = self.rotation_matrix
        orientation_type = get_current_orientation_type(context)
        # In LOCAL orientation the handles follow the gizmo rotation; in GLOBAL
        # they stay world-aligned (None = no extra rotation factor).
        eff_rot = rot if orientation_type == 'LOCAL' else None

        # Center button: view-aligned (native uses set_matrix_rotation_from_z_axis)
        view_aligned_button = view_aligned_matrix.copy()
        view_aligned_button.col[3][:3] = center
        self.gz_center.matrix_basis = view_aligned_button

        # View direction at the gizmo position (perspective parallax matters at
        # screen edges), then per-axis visibility/alpha in the active orientation.
        view_dir = get_view_direction_at_position(rv3d, view_inv_matrix, view_z_axis, center)
        vis = calculate_axis_visibility(view_dir, rot, orientation_type)

        # Pixel-size push so arrow stems start at the center circle's edge.
        # x ui_scale: the ring's own size scales with UI_SCALE_FAC (see
        # _draw_prepare_scale_mode) - without the factor the arrows started
        # INSIDE the enlarged ring on a 2x display.
        world_per_px = get_pixel_world_size(context.region, rv3d, center)
        push_scale = ui_scale_factor() * world_per_px
        arrow_push = ARROW_OFFSET_PIXELS * push_scale
        plane_push = GIZMO_PLANE_OFFSET_PIXELS * push_scale

        # Axis arrows: at the FULL gizmo center (operator-bound - nothing "adds
        # back" an excluded axis; excluding one stranded the Z arrow after tool
        # re-entry), pushed outward along their effective axis.
        for axis, arrow_rot in (('x', _CACHED_ROTATION_ARROW_X),
                                ('y', _CACHED_ROTATION_ARROW_Y),
                                ('z', None)):
            gz = getattr(self, f'gz_arrow_{axis}')
            base = center + vis[f'axis_{axis}'] * arrow_push
            matrix = Matrix.Translation(base)
            if eff_rot is not None:
                matrix = matrix @ eff_rot
            gz.matrix_basis = matrix @ arrow_rot if arrow_rot else matrix
            gz.alpha = GIZMO_ARROW_ALPHA * vis[f'alpha_arrow_{axis}']
            gz.hide = (vis[f'idot_{axis}'] < ARROW_HIDE_THRESHOLD_MIN)

        # Make the dragged arrow invisible: Blender draws an interacting
        # arrow from state captured at interaction start - it ignores
        # matrix_basis updates AND the hide flag, so it cannot track
        # precision/typed values and would split from the gizmo. Colors
        # ARE read live at draw time, so zero the alpha channels as well.
        runtime = getattr(context.window_manager, 'cut_plane_runtime', None)
        active_axis = runtime.move_active_axis if runtime else ""
        default_highlight = getattr(self, '_arrow_alpha_highlight', 0.5)
        for axis_id in ('X', 'Y', 'Z'):
            gz_arrow = getattr(self, f'gz_arrow_{axis_id.lower()}')
            if axis_id == active_axis:
                gz_arrow.hide = True
                gz_arrow.alpha = 0.0
                gz_arrow.alpha_highlight = 0.0
            else:
                gz_arrow.alpha_highlight = default_highlight

        # 2D plane handlers: stable translation basis at the center; the
        # diagonal push + facing rotation live in matrix_offset. A handler
        # hides when its plane is edge-on (its NORMAL's idot is high).
        plane_basis_matrix = Matrix.Translation(center)
        for name, push_axes, dial_rot, normal_axis in (
                ('xy', ('x', 'y'), None, 'z'),
                ('yz', ('y', 'z'), _CACHED_ROTATION_DIAL_X, 'x'),
                ('xz', ('x', 'z'), _CACHED_ROTATION_DIAL_Y, 'y')):
            local_push = Vector((0.0, 0.0, 0.0))
            for axis in push_axes:
                setattr(local_push, axis, plane_push)
            gz = getattr(self, f'gz_plane_{name}')
            gz.matrix_basis = plane_basis_matrix
            if eff_rot is not None:
                offset_matrix = Matrix.Translation(eff_rot @ local_push) @ eff_rot
            else:
                offset_matrix = Matrix.Translation(local_push)
            gz.matrix_offset = offset_matrix @ dial_rot if dial_rot else offset_matrix
            gz.alpha = GIZMO_ARROW_ALPHA * vis[f'alpha_plane_{name}']
            gz.hide = ((1.0 - vis[f'idot_{normal_axis}']) < ARROW_HIDE_THRESHOLD_MIN)

    def draw_prepare(self, context) -> None:
        """Pre-draw pass, every frame. Owns the per-mode positioning (see the
        _draw_prepare_*_mode methods): camera-facing elements and modal-drag
        state need refreshing per frame, which refresh() doesn't get."""
        try:
            if not context:
                return
            # The view of the region being drawn, NOT space_data.region_3d
            # (which is the space's own - in a quad view, "the camera
            # region"). This runs per WINDOW region and is mixed with
            # context.region below, so reading the space's view placed every
            # camera-facing element from a foreign view in 3 of a quad view's
            # 4 regions. Every transform modal already reads region_data.
            rv3d = context.region_data
            if rv3d is None or not hasattr(rv3d, 'view_matrix'):
                return

            if self._hide_all_gizmos():
                return  # keyboard transform running - overlay draws the plane

            props = self._get_gizmo_properties(context)
            mode = props['mode']

            # Sync rotation from the tool property EVERY frame: modal
            # operators write it mid-drag (rotation dials, snap
            # align-rotation during moves) and refresh() does not run
            # during modals - without this the plane visual lags until
            # the drag ends
            self.rotation_matrix = flat_to_matrix(
                context.window_manager.cut_plane.rotation_matrix_flat)

            # Ensure visibility is correct immediately (fixes stale gizmo issue)
            # This is critical because refresh() might lag behind tag_redraw()
            self._update_visibility(mode)

            # Shared gizmo center from the single plane-pose source
            pose = get_plane_pose(context)
            if pose is None:
                return
            center = pose[0]

            # View direction toward the camera - rv3d->viewinv[2] in C terms
            try:
                view_inv_matrix = rv3d.view_matrix.inverted()
            except ValueError:
                if DEBUG:
                    logger.warning("[CUT PLANE] View matrix is singular, cannot invert")
                return

            view_z_axis = view_inv_matrix.col[2].xyz
            if view_z_axis.length_squared < PARALLEL_EPSILON:
                if DEBUG:
                    logger.warning("[CUT PLANE] Degenerate view direction")
                return

            # Right-handed basis around the view axis, for camera-facing gizmos
            y_axis = view_z_axis.orthogonal().normalized()
            x_axis = y_axis.cross(view_z_axis).normalized()

            view_aligned_matrix = Matrix.Identity(4)
            view_aligned_matrix.col[0][:3] = x_axis
            view_aligned_matrix.col[1][:3] = y_axis
            view_aligned_matrix.col[2][:3] = view_z_axis
            view_aligned_matrix.col[3][:3] = center

            if mode == 'ROTATE':
                self._draw_prepare_rotate_mode(context, view_aligned_matrix)
            elif mode == 'SCALE':
                self._draw_prepare_scale_mode(
                    context, center, rv3d, view_inv_matrix, view_z_axis, view_aligned_matrix)
            elif mode == 'MOVE':
                self._draw_prepare_move_mode(
                    context, center, rv3d, view_inv_matrix, view_z_axis, view_aligned_matrix)

            # Plane visual: one placement for all modes (position + rotation + scale)
            self._place_plane_visual(context, props)

            self._apply_handles_toggle(context)  # keyboard-only mode wins last

        except Exception as e:
            mode = props.get('mode', 'UNKNOWN') if 'props' in locals() else 'UNKNOWN'
            logger.error(f"[CUT PLANE] Error in draw_prepare() [mode={mode}]: {e}")
            logger.error(f"[CUT PLANE] Traceback: {traceback.format_exc()}")
            # Fallback: show all arrows with full alpha if in MOVE mode
            # (hasattr guard: a setup() failure means no gizmos to un-hide,
            # and raising from an except handler would mask the original)
            if mode == 'MOVE' and hasattr(self, 'gz_arrow_x'):
                self.gz_arrow_x.hide = False
                self.gz_arrow_y.hide = False
                self.gz_arrow_z.hide = False
                self.gz_plane_xy.hide = False
                self.gz_plane_yz.hide = False
                self.gz_plane_xz.hide = False
                self.gz_arrow_x.alpha = GIZMO_ARROW_ALPHA
                self.gz_arrow_y.alpha = GIZMO_ARROW_ALPHA
                self.gz_arrow_z.alpha = GIZMO_ARROW_ALPHA
