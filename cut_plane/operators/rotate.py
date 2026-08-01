# SPDX-License-Identifier: GPL-3.0-or-later
"""Rotation operators: axis dials, view dial, and trackball."""

import math
from typing import Optional

import bpy
from bpy.types import Operator
from bpy_extras import view3d_utils
from mathutils import Matrix, Vector

from ..lib.constants import (  # noqa: I001
    logger,
    ANGLE_WRAP_THRESHOLD_RADIANS,
    AXIS_VECTOR_MAP,
    AXIS_X,
    AXIS_Y,
    AXIS_Z,
    DEBUG,
    GIZMO_VIEW_DIAL_SCALE,
    ROTATION_PRECISION_FAC,
    TRACKBALL_PRECISION_FAC,
    TRACKBALL_SENSITIVITY,
)
from ..lib.math_utils import (
    build_dial_basis,
    flat_to_matrix,
    get_current_orientation_type,
    get_plane_pose,
    get_rotation_axis_world,
    get_theme_active_object_color,
    get_theme_axis_color,
    matrix_to_flat,
    ui_scale_factor,
)
from ..state.properties import get_runtime
from .. import cutting
from .numeric_input import MultiNumericInput, NumericInput
from .snapping import get_angle_increment, snap_enabled
from .modal_base import CutPlaneModalMixin
from .utils import next_kb_constraint, release_operator


# Bottom status-bar key legends (shown while the modal runs) as
# ((key spec, ...), label) entries - rendered as native key chords by
# modal_base._legend_draw (spec = event type, optional 'shift+' prefix).
# Fixed-axis dials and trackball are already constrained; only keyboard R
# (view rotate) offers the X/Y/Z retarget and the R->trackball switch.
_LEGEND_ROTATE_DIAL = (
    # Mouse first, key last in both: the wide Esc icon must sit LAST, where
    # the template sizes it (see modal_base._WIDE_KEYS).
    (('LEFTMOUSE', 'RET'), "Confirm"),
    (('RIGHTMOUSE', 'ESC'), "Cancel"),
    (('LEFT_SHIFT',), "Precision"),
    (('LEFT_CTRL',), "Snap"),
    # [0] stands in for the digit keys - any of them starts numeric type-in.
    (('ZERO',), "Type angle"),
)
# The trackball entry is spliced in live (status_legend) from the rotate
# binding - it is the only legend key a user can rebind, and a literal 'R'
# here told them to press R while E was the real key. Every other legend
# key IS hardcoded in the modal, so those literals are honest.
_LEGEND_ROTATE_VIEW_HEAD = (
    (('LEFTMOUSE', 'RET'), "Confirm"),
    (('RIGHTMOUSE', 'ESC'), "Cancel"),
    (('X', 'Y', 'Z'), "Axis"),
)
_LEGEND_ROTATE_VIEW_TAIL = (
    (('LEFT_SHIFT',), "Precision"),
    (('LEFT_CTRL',), "Snap"),
    (('ZERO',), "Type angle"),
)
_LEGEND_TRACKBALL = (
    (('LEFTMOUSE', 'RET'), "Confirm"),
    (('RIGHTMOUSE', 'ESC'), "Cancel"),
    (('LEFT_SHIFT',), "Precision"),
    (('ZERO',), "Type angle"),
)


class CutPlaneRotateBaseOperator(CutPlaneModalMixin, Operator):
    """
    Base class for rotation operators with native-like algorithm.

    Uses ray-plane intersection and signed angle calculation for precise,
    latency-free rotation that matches Blender's native dial gizmo feel.

    Subclasses set axis_id + local_axis, or override _setup_axis() /
    _get_axis_color() for computed axes (see CutPlaneRotateViewOperator).
    """
    # Tool state, not undoable: rotating the cut plane must not create an undo
    # step, so Ctrl+Z steps through cuts not plane nudges.
    # Not REGISTERed either: the Adjust Last Operation panel it claimed pops
    # the previous undo step (the last cut) on any field touch - see
    # move.CutPlaneMoveAxisOperator.
    bl_options = {'BLOCKING'}

    def status_legend(self):
        # X/Y/Z retarget and the trackball toggle only exist for a keyboard
        # rotate (view axis).
        if not (self._keyboard_invoked and self.axis_id == 'VIEW'):
            return _LEGEND_ROTATE_DIAL
        from ..ui.tool import chord_label, transform_chords
        chord = transform_chords()[1]
        if chord is None:      # rotate unbound/disabled: no key to advertise
            return _LEGEND_ROTATE_VIEW_HEAD + _LEGEND_ROTATE_VIEW_TAIL
        return (_LEGEND_ROTATE_VIEW_HEAD
                + (((chord_label(chord, human=False),), "Trackball"),)
                + _LEGEND_ROTATE_VIEW_TAIL)

    # Distance threshold for zero-vector protection (matches BLI_dial_2d)
    DISTANCE_THRESHOLD_SQ = 0.0001 * 0.0001

    # 2D screen-space distance threshold (pixels squared) - native uses ~5 pixels
    DISTANCE_THRESHOLD_2D_SQ = 25.0  # 5 LOGICAL pixels squared

    def _deadzone_2d_sq(self) -> float:
        """The deadzone in PHYSICAL pixels squared: region/mouse coordinates
        are physical pixels, so the 5-logical-px threshold scales with
        ui_scale - squared, because it gates a squared distance. Without it
        the deadzone halves on a 2x display."""
        f = ui_scale_factor()
        return self.DISTANCE_THRESHOLD_2D_SQ * f * f

    # Axis configuration (override in subclasses for standard axis operators)
    axis_id: str = ''           # 'X', 'Y', 'Z', 'VIEW', 'TRACKBALL'
    local_axis: Vector = None   # AXIS_X, AXIS_Y, AXIS_Z, or None for computed

    def _setup_axis(self, context):
        """Set axis_vector and dial_plane_normal. Override for non-standard axes."""
        if self.local_axis is not None:
            self.axis_vector = self._get_rotation_axis_world(context, self.local_axis)
            self.dial_plane_normal = self.axis_vector

    def _get_axis_color(self):
        """Return RGB color tuple for arc visualization."""
        if self.axis_id in ('X', 'Y', 'Z'):
            return get_theme_axis_color(self.axis_id)
        return (1.0, 1.0, 1.0)  # White default

    def _get_dial_id(self):
        """Return dial identifier for visibility control."""
        return self.axis_id

    def _get_rotation_axis_world(self, context, local_axis: Vector) -> Vector:
        """
        Get rotation axis in world space based on orientation setting.
        """
        orientation_type = get_current_orientation_type(context)
        props = context.window_manager.cut_plane
        gizmo_rotation_matrix = flat_to_matrix(props.rotation_matrix_flat)
        world_axis = get_rotation_axis_world(
            orientation_type,
            local_axis,
            gizmo_rotation_matrix
        )

        if DEBUG:
            logger.debug(f"[ROTATE OPERATOR] orientation_type: {orientation_type}")
            logger.debug(f"[ROTATE OPERATOR] local_axis: {local_axis}, world_axis: {world_axis}")

        return world_axis

    require_runtime = True

    def setup(self, context, event):
        runtime = get_runtime(context)
        if runtime is None:
            return {'CANCELLED'}

        self.init_mouse = Vector((event.mouse_region_x, event.mouse_region_y))

        # Orientation decides intrinsic (LOCAL) vs extrinsic (GLOBAL) rotation
        orientation_type = get_current_orientation_type(context)
        self.orientation_type = orientation_type

        # Dial center + rotation from the single plane-pose source (ROTATE mode
        # collapses to world-space offsets inside it, preventing dial drift).
        pose = get_plane_pose(context)
        if pose is None:
            return {'CANCELLED'}
        self.dial_center, self.init_rotation_matrix = pose

        # Subclass must set self.axis_vector and self.dial_plane_normal
        self._setup_axis(context)

        # Keyboard axis constraint (keyboard R only): (kind, axis, space) or None,
        # plus the angle already applied when the axis was switched (so the
        # rotation carries over to the new axis, native-style).
        self._kb_axis = None
        self._kb_angle_base = 0.0
        self._last_applied_angle = 0.0
        # Native rotation input is a 2D screen-space dial (BLI_dial_2d) for ANY
        # axis; while a keyboard constraint is engaged we force that input model
        # (the 3D dial-plane projection goes nonlinear for oblique axes).
        self._kb_screen_input = False
        self._kb_input_sign = 1.0

        # Angle-tracking reference for the current axis (shared by invoke and
        # the keyboard axis retarget)
        self._init_rotation_reference(context, runtime, self.init_mouse)

        # Numeric type-in (plain numbers are degrees; 'd'/'r' suffixes
        # and expressions like 45/2 work via the ROTATION unit category)
        self.num_input = NumericInput(allow_units=True)
        return None

    def _init_rotation_reference(self, context, runtime, mouse: Vector) -> None:
        """(Re)initialize all angle-tracking state for the CURRENT axis, with
        *mouse* as the zero-angle reference.

        Extracted from invoke so the keyboard X/Y/Z axis retarget can restart
        tracking mid-modal with identical semantics (3D plane projection with
        native 2D edge-on fallback, wrap-around, precision, arc runtime).
        Requires self.axis_vector / self.dial_plane_normal / self.dial_center.
        """
        self.init_mouse = mouse.copy()

        # Native typed-angle convention: positive input rotates clockwise
        # on screen. Native flips the rotation axis away from the viewer
        # (transform_mode_rotate.cc); the equivalent for typed values is
        # negating the angle when the axis points toward the viewer.
        self._typed_sign = 1.0
        rv3d = context.region_data
        if rv3d is not None:
            try:
                toward_viewer = rv3d.view_matrix.inverted().col[2].xyz
                if self.axis_vector.dot(toward_viewer) > 0.0:
                    self._typed_sign = -1.0
            except ValueError:
                pass  # Singular view matrix - keep default sign

        # Create dial plane (ax + by + cz + d = 0)
        self.dial_plane_d = -self.dial_plane_normal.dot(self.dial_center)

        # Project the reference mouse onto the dial plane
        self.init_point_on_plane = self._project_mouse_to_plane(context, mouse)

        # Initialize 2D fallback variables (used when view is edge-on)
        self.dial_center_2d = self._get_dial_center_2d(context)
        self.prev_mouse = mouse.copy()

        # Determine if we need 2D fallback mode. A keyboard axis constraint
        # FORCES the 2D screen-dial input - that is native's input model for
        # constrained rotation (the mouse angle around the projected pivot),
        # and the 3D plane projection feels nonlinear for oblique axes.
        if self.init_point_on_plane is None or getattr(self, '_kb_screen_input', False):
            # 2D screen-space rotation mode (matches native BLI_dial_2d input)
            self.use_2d_fallback = True
            init_dir_2d = mouse - self.dial_center_2d
            if init_dir_2d.length_squared < self._deadzone_2d_sq():
                # Too close to center in 2D - use arbitrary initial direction
                self.init_2d_direction = Vector((1.0, 0.0))
            else:
                self.init_2d_direction = init_dir_2d.normalized()
        else:
            # Normal 3D mode - check distance from center
            init_rel = self.init_point_on_plane - self.dial_center
            if init_rel.length_squared < self.DISTANCE_THRESHOLD_SQ:
                # Too close to center in 3D - switch to 2D fallback
                self.use_2d_fallback = True
                init_dir_2d = mouse - self.dial_center_2d
                if init_dir_2d.length_squared < self._deadzone_2d_sq():
                    self.init_2d_direction = Vector((1.0, 0.0))
                else:
                    self.init_2d_direction = init_dir_2d.normalized()
            else:
                # Normal 3D mode; init_2d_direction is assigned on every
                # transition INTO 2D fallback, so nothing to seed here
                self.use_2d_fallback = False

        # Reset wrap-around / precision tracking for the new reference
        self.prev_angle = 0.0
        self.rotations = 0
        self.last_valid_angle = 0.0
        self.precision_offset = 0.0
        self._prev_total_angle = 0.0

        # Setup arc drawing via runtime properties
        runtime.rotation_active = True
        runtime.rotation_arc_center = self.dial_center
        runtime.rotation_arc_axis = self.axis_vector

        # Arc start angle from the reference mouse position (native behavior);
        # must use the same basis as _draw_arc_3d (shared build_dial_basis)
        if not self.use_2d_fallback:
            init_rel = self.init_point_on_plane - self.dial_center
            ref_direction = build_dial_basis(self.axis_vector)[0]
            if init_rel.length_squared < self.DISTANCE_THRESHOLD_SQ:
                init_rel_normalized = ref_direction
            else:
                init_rel_normalized = init_rel.normalized()
            runtime.rotation_arc_start_angle = self._angle_signed_on_axis(
                ref_direction, init_rel_normalized, self.axis_vector)
        else:
            # 2D fallback: screen-space angle from atan2
            runtime.rotation_arc_start_angle = math.atan2(
                self.init_2d_direction.y, self.init_2d_direction.x)

        runtime.rotation_arc_current_angle = 0.0
        runtime.rotation_arc_color = self._get_axis_color()
        runtime.rotation_arc_scale = self._get_dial_scale()  # 1.0 axis, 1.2 view
        runtime.rotation_active_dial = self._get_dial_id()  # Hide other dials

        # Store start angle for absolute snap calculation
        self.start_angle = runtime.rotation_arc_start_angle

    def _retarget_kb_axis(self, context, runtime, axis: str, mouse: Vector) -> None:
        """Cycle the keyboard rotation-axis constraint and retarget the dial.

        Native R -> X/Y/Z semantics: the SAME rotation machinery keeps running,
        only self.axis_vector / dial_plane_normal change (this operator family
        is per-axis by construction - the X/Y/Z dials are just fixed
        parameterizations of it). The rotation applied so far carries over as
        _kb_angle_base, so the plane jumps to the same angle around the new
        axis, then the mouse continues from zero at the current position.
        Cycling off returns to the operator's own axis (VIEW).
        """
        self._kb_axis = next_kb_constraint(self._kb_axis, 'AXIS', axis)
        self._apply_kb_axis(context, runtime, mouse)

    def _apply_kb_axis(self, context, runtime, mouse: Vector) -> None:
        """Retarget the dial to self._kb_axis (None = the operator's own
        axis). Shared by the X/Y/Z key cycling and the mid-modal switch
        adopt."""
        self._kb_angle_base = self._last_applied_angle
        # Constrained -> native 2D screen-dial input (must be set BEFORE the
        # reference init, which picks the input model)
        self._kb_screen_input = self._kb_axis is not None

        if self._kb_axis is None:
            self._setup_axis(context)  # back to the operator's own axis
        else:
            _kind, axis_id, space = self._kb_axis
            # LOCAL uses the gizmo rotation at drag START (the constraint must
            # not drift while this very modal rotates the matrix)
            self.axis_vector = get_rotation_axis_world(
                space, AXIS_VECTOR_MAP[axis_id], self.init_rotation_matrix)
            self.dial_plane_normal = self.axis_vector

        self._init_rotation_reference(context, runtime, mouse)
        # Native sign rule for screen input: the screen-swept angle applies
        # around the axis oriented AWAY from the viewer, so the geometry
        # follows the mouse direction whichever way the axis points.
        # _typed_sign (recomputed above for the new axis) encodes exactly that.
        self._kb_input_sign = self._typed_sign if self._kb_axis is not None else 1.0
        # Constrained: tint the arc with the axis color (like the dials);
        # cleared: back to the operator's own color.
        if self._kb_axis is not None:
            runtime.rotation_arc_color = get_theme_axis_color(self._kb_axis[1])

    def kb_constraint_state(self):
        return getattr(self, '_kb_axis', None)

    def adopt_kb_constraint(self, context, event, constraint) -> None:
        """Mid-modal switch hand-over: start rotating under the G/S
        constraint (native G-X -> R rotates around X). Keyboard view-rotate
        only - the dials are fixed-axis by construction. A PLANE constraint
        becomes rotation around its normal axis, the native equivalence."""
        if self.axis_id != 'VIEW':
            return
        runtime = get_runtime(context)
        if runtime is None:
            return
        _kind, axis, space = constraint
        self._kb_axis = ('AXIS', axis, space)
        self._apply_kb_axis(
            context, runtime,
            Vector((event.mouse_region_x, event.mouse_region_y)))

    def _kb_header_suffix(self) -> str:
        """Native-style constraint readout for the header ('' when free)."""
        if getattr(self, '_kb_axis', None) is None:
            return ""
        _kind, axis, space = self._kb_axis
        return f" along {'local ' if space == 'LOCAL' else 'global '}{axis}"

    def constraint_visual_lines(self):
        """Constraint axis line for the draw layer (see drawing.draw_constraint_
        lines_px): one line through the dial center along the rotation axis.

        Keyboard R + X/Y/Z shows the constrained axis; an X/Y/Z DIAL handle
        drag is the same constraint by construction, so it shows its own axis
        line too (native dial drags do). View dial / trackball: none.
        """
        if getattr(self, 'dial_center', None) is None:
            return []
        if getattr(self, '_kb_axis', None) is not None:
            return [(self._kb_axis[1], self.dial_center, self.axis_vector)]
        if self.axis_id in ('X', 'Y', 'Z'):
            return [(self.axis_id, self.dial_center, self.axis_vector)]
        return []

    def _get_dial_scale(self) -> float:
        """Return dial scale for arc radius (1.0 for axis, 1.2 for view)."""
        return 1.0  # Default for axis-constrained dials

    def _get_dial_center_2d(self, context) -> Vector:
        """
        Project dial center to 2D screen coordinates.

        Used for 2D screen-space rotation fallback when view is edge-on to dial plane.
        """
        region = context.region
        rv3d = context.region_data
        center_2d = view3d_utils.location_3d_to_region_2d(
            region, rv3d, self.dial_center
        )
        return Vector(center_2d) if center_2d else self.init_mouse

    def _calculate_2d_angle(self, mouse_pos: Vector) -> float:
        """
        Calculate rotation angle using 2D screen-space math (BLI_dial_2d equivalent).

        Used as fallback when 3D plane projection fails (edge-on view).
        Implements the same algorithm as Blender's BLI_dial_angle():
        - Uses atan2(sin, cos) for robust angle calculation
        - Returns last valid angle if mouse is too close to center
        - Handles wrap-around for full rotation tracking
        """
        current_direction = mouse_pos - self.dial_center_2d

        # Distance threshold (native uses ~5 pixels) - prevent jitter near center
        if current_direction.length_squared < self._deadzone_2d_sq():
            return self.last_valid_angle

        current_direction.normalize()

        # 2D angle calculation using atan2 (like BLI_dial_2d)
        cos_val = current_direction.dot(self.init_2d_direction)
        # 2D cross product: a.x * b.y - a.y * b.x
        sin_val = current_direction.x * self.init_2d_direction.y - current_direction.y * self.init_2d_direction.x
        angle = math.atan2(sin_val, cos_val)

        self.last_valid_angle = angle
        return angle

    def _project_mouse_to_plane(self, context, mouse_pos) -> Optional[Vector]:
        """
        Ray-plane intersection with tolerance check.
        """
        region = context.region
        rv3d = context.region_data

        ray_origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, mouse_pos)
        ray_direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, mouse_pos)

        # Tolerance check (ray nearly parallel to plane)
        denom = self.dial_plane_normal.dot(ray_direction)
        if abs(denom) < 1e-5:  # ~0.001 degree threshold
            return None

        t = -(self.dial_plane_normal.dot(ray_origin) + self.dial_plane_d) / denom
        return ray_origin + t * ray_direction

    def _angle_signed_on_axis(self, v1: Vector, v2: Vector, axis: Vector) -> float:
        """
        Calculate signed angle between vectors around axis.

        Replicates native angle_signed_on_axis_v3v3_v3() from BLI_math.
        """
        # Project onto plane perpendicular to axis
        v1_proj = v1 - v1.dot(axis) * axis
        v2_proj = v2 - v2.dot(axis) * axis

        # Zero-vector protection
        len1_sq = v1_proj.length_squared
        len2_sq = v2_proj.length_squared
        if len1_sq < self.DISTANCE_THRESHOLD_SQ or len2_sq < self.DISTANCE_THRESHOLD_SQ:
            return self.last_valid_angle

        v1_proj /= math.sqrt(len1_sq)
        v2_proj /= math.sqrt(len2_sq)

        # Clamp for numerical stability
        cos_angle = max(-1.0, min(1.0, v1_proj.dot(v2_proj)))
        angle = math.acos(cos_angle)

        # Determine sign using cross product
        cross = v1_proj.cross(v2_proj)
        if cross.dot(axis) < 0:
            angle = -angle

        self.last_valid_angle = angle
        return angle

    def _detect_wrap_around(self, delta: float) -> None:
        """
        Detect and handle ±180° wrap-around for full rotation tracking.
        """
        if (delta * self.prev_angle < 0) and (abs(self.prev_angle) > ANGLE_WRAP_THRESHOLD_RADIANS):
            if self.prev_angle < 0:
                self.rotations -= 1
            else:
                self.rotations += 1

    @staticmethod
    def _clamp_large_rotation(angle: float) -> float:
        """
        Clamp rotation angle to prevent matrix instability from extreme values.

        Native Blender limits rotation to 1001 turns max (transform_mode_rotate.cc:211-221).
        This prevents iterative stepping from becoming too slow and matrix corruption.
        """
        angle_max = math.pi * 2000.0  # ~12,566 radians (1001 complete turns)

        if abs(angle) > angle_max:
            # Wrap angle while preserving sign and staying within limit
            angle_sign = -1.0 if angle < 0.0 else 1.0
            angle = angle_sign * (math.fmod(abs(angle), 2.0 * math.pi) + angle_max)

        return angle

    @staticmethod
    def _stabilize_matrix_deferred():
        """
        Stabilize the rotation matrix (timer body - no context captured).

        Runs in a timer, where bpy.context may be limited; the tool state
        lives on the WindowManager singleton, reachable through bpy.data
        without any context at all.
        """
        wms = bpy.data.window_managers
        props = getattr(wms[0], 'cut_plane', None) if wms else None
        if props is None:
            return None  # Properties not available (addon unregistered)

        try:
            # Orthonormalize the rotation to remove accumulated drift
            rot_3x3 = flat_to_matrix(props.rotation_matrix_flat).to_3x3()
            rot_3x3.normalize()
            props.rotation_matrix_flat = matrix_to_flat(rot_3x3.to_4x4())

            if DEBUG:
                logger.debug("[MATRIX STABILIZATION] Matrix normalized")
        except Exception as e:
            if DEBUG:
                logger.error(f"[MATRIX STABILIZATION] Error during stabilization: {e}")

        return None  # Stop timer (run once)

    def handle_modal_event(self, context, event, props, runtime):
        # Native R R: pressing the rotate key again during a keyboard
        # view-rotate switches to trackball. Restore the initial rotation
        # first (native restarts the rotation from scratch on switch), then
        # hand over. Matched as a full chord, so the user's rebind drives it
        # and a modified press (Alt+R) does not.
        from ..ui.tool import chord_matches, transform_chords
        if (chord_matches(transform_chords()[1], event) and event.value == 'PRESS'
                and not self.num_input.active
                and getattr(self, '_keyboard_invoked', False)
                and self.axis_id == 'VIEW'):
            props.rotation_matrix_flat = matrix_to_flat(self.init_rotation_matrix)
            runtime.rotation_active = False
            runtime.rotation_active_dial = ""
            runtime.rotation_snap_active = False
            # Same rule as the Esc branch and modal_base's step 2b hand-over:
            # re-cut NOW so the destructive preview snaps back onto the
            # restored pose. Without this the plane drew at its start
            # orientation while the mesh stayed bisected at the abandoned
            # angle - and it stayed that way until the next transform.
            cutting.update_live_preview(context)
            if context.area:
                context.area.header_text_set(None)
            # The successor sets its own legend on start; clear ours NOW so a
            # failed hand-over (setup CANCELLED, e.g. singular view matrix)
            # cannot pin a stale legend in the status bar.
            self._status_text_set(context, None)
            release_operator(self)
            bpy.ops.view3d.cut_plane_trackball('INVOKE_DEFAULT')
            return {'FINISHED'}

        # Keyboard axis constraint (native R -> X/Y/Z): keyboard view-rotate
        # only - the dials are fixed parameterizations of this same machinery.
        # Cycling global -> local -> off (back to the view axis). The rotation
        # applied so far carries over to the new axis immediately.
        if (event.value == 'PRESS' and event.type in {'X', 'Y', 'Z'}
                and not self.num_input.active
                and getattr(self, '_keyboard_invoked', False)
                and self.axis_id == 'VIEW'):
            mouse = Vector((event.mouse_region_x, event.mouse_region_y))
            self._retarget_kb_axis(context, runtime, event.type, mouse)
            # Re-apply the carried-over angle around the new axis at once
            carried = self._clamp_large_rotation(self._kb_angle_base)
            rotation = Matrix.Rotation(carried, 4, self.axis_vector)
            props.rotation_matrix_flat = matrix_to_flat(rotation @ self.init_rotation_matrix)
            self._last_applied_angle = carried
            runtime.rotation_arc_current_angle = carried
            cutting.update_live_preview(context)
            if context.area:
                context.area.header_text_set(
                    f"Rotate {self._header_label()}: "
                    f"{math.degrees(carried) * self._typed_sign:.2f}°"
                    f"{self._kb_header_suffix()}")
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        # Effective snap state (native: magnet XOR Ctrl, gated by Affect
        # Rotate). Only write the property on actual change - scene
        # property writes trigger depsgraph updates per event
        snap_on = snap_enabled(context, event, 'snap_affect_rotate')
        snap_state_changed = (snap_on != runtime.rotation_snap_active)
        if snap_state_changed:
            runtime.rotation_snap_active = snap_on

        # Recalculate on MOUSEMOVE or when the snap state changes
        # Skipped while numeric input is active (typed value owns the transform)
        if (event.type == 'MOUSEMOVE' or snap_state_changed) and not self.num_input.active:
            mouse_pos = Vector((event.mouse_region_x, event.mouse_region_y))

            if self.use_2d_fallback:
                # 2D screen-space rotation (edge-on fallback, and ALWAYS while a
                # keyboard axis constraint is engaged - the native input model).
                # _kb_input_sign maps the screen sweep to the axis so the
                # geometry follows the mouse (1.0 outside keyboard constraints).
                delta = self._calculate_2d_angle(mouse_pos) * self._kb_input_sign
            else:
                # Normal 3D plane projection
                current_point = self._project_mouse_to_plane(context, mouse_pos)
                if current_point is None:
                    # Dynamic switch to 2D if 3D projection fails mid-drag
                    # This happens when user rotates view while dragging
                    self.use_2d_fallback = True
                    self.dial_center_2d = self._get_dial_center_2d(context)
                    prev_dir_2d = self.prev_mouse - self.dial_center_2d
                    if prev_dir_2d.length_squared >= self._deadzone_2d_sq():
                        self.init_2d_direction = prev_dir_2d.normalized()
                    else:
                        self.init_2d_direction = Vector((1.0, 0.0))
                    # Reset rotation tracking for smooth transition
                    self.rotations = 0
                    self.prev_angle = 0.0
                    delta = self._calculate_2d_angle(mouse_pos) * self._kb_input_sign
                else:
                    init_rel = self.init_point_on_plane - self.dial_center
                    current_rel = current_point - self.dial_center
                    delta = self._angle_signed_on_axis(init_rel, current_rel, self.axis_vector)

            # Track previous mouse for dynamic fallback switching
            self.prev_mouse = mouse_pos.copy()

            # Detect wrap-around for full rotation tracking
            self._detect_wrap_around(delta)
            self.prev_angle = delta

            # Total angle with full rotations accumulated
            total_angle = delta + (2 * math.pi * self.rotations)

            # Precision mode with offset accumulation (native algorithm - no jumps)
            # Track delta from previous frame
            angle_delta_frame = total_angle - self._prev_total_angle
            self._prev_total_angle = total_angle

            if event.shift:
                # Accumulate offset to subtract (keeps 5% of precision movement)
                self.precision_offset += angle_delta_frame * (1.0 - ROTATION_PRECISION_FAC)

            display_angle = total_angle - self.precision_offset

            # Angle carried over from before a keyboard axis switch (0.0 when
            # no switch happened) - the tracked delta continues on top of it.
            display_angle += self._kb_angle_base

            # Snap mode (native): snap the ROTATION AMOUNT itself to clean
            # multiples of the increment (5°, 10°, 15°, ... - finer step with
            # Shift), like native Ctrl-rotate. NOT the absolute dial angle -
            # that offset every snapped value by the click angle (e.g.
            # "1.552 + 15°"). The tick marks are drawn rotated by the start
            # angle (see _draw_snap_ticks_3d) so the arc end still lands
            # exactly on a tick.
            if snap_on:
                snap = get_angle_increment(context, event.shift)
                runtime.rotation_snap_increment = snap  # Drives tick drawing
                display_angle = round(display_angle / snap) * snap

            # Clamp angle to prevent matrix corruption (native: max 1001 turns)
            clamped_angle = self._clamp_large_rotation(display_angle)

            # Extrinsic (pre-multiply) for BOTH orientations, like native:
            # LOCAL comes from transforming the axis to world space, not from
            # the multiplication order
            rotation = Matrix.Rotation(clamped_angle, 4, self.axis_vector)
            new_matrix = rotation @ self.init_rotation_matrix

            if DEBUG:
                if self.orientation_type == 'LOCAL':
                    logger.debug(f"[ROTATE APPLY] LOCAL extrinsic: angle={clamped_angle:.3f}, axis transformed to world")
                else:
                    logger.debug(f"[ROTATE APPLY] GLOBAL extrinsic: angle={clamped_angle:.3f}, world axis")

            props.rotation_matrix_flat = matrix_to_flat(new_matrix)
            self._last_applied_angle = clamped_angle  # keyboard axis-switch carry-over

            # Update arc drawing (use clamped angle for visual consistency)
            runtime.rotation_arc_current_angle = clamped_angle

            if context.area:
                # Display in the typed-value convention (positive = clockwise
                # on screen) so the readout matches what typing would produce
                display_deg = math.degrees(clamped_angle) * self._typed_sign
                shift_text = " (Precision)" if event.shift else ""
                context.area.header_text_set(
                    f"Rotate {self._header_label()}: {display_deg:.2f}°"
                    f"{shift_text}{self._kb_header_suffix()}"
                )
                context.area.tag_redraw()

            cutting.update_live_preview(context)  # live cut preview
            return {'RUNNING_MODAL'}
        return None

    def on_finish(self, context, props, runtime):
        runtime.rotation_active = False
        runtime.rotation_active_dial = ""  # Restore dial visibility
        runtime.rotation_snap_active = False  # Clear snap visualization

    def after_finish(self, context):
        # Deferred matrix stabilization prevents drift; runs AFTER the modal
        # exits / the guard is released to avoid context write errors.
        # Nothing captured: the timer body reads the WM state via bpy.data.
        bpy.app.timers.register(
            CutPlaneRotateBaseOperator._stabilize_matrix_deferred,
            first_interval=0.01
        )

    def on_cancel(self, context, props, runtime):
        # Restore initial state (also the force-cancel body)
        props.rotation_matrix_flat = matrix_to_flat(self.init_rotation_matrix)
        if runtime is not None:
            runtime.rotation_active = False
            runtime.rotation_active_dial = ""  # Restore dial visibility
            runtime.rotation_snap_active = False  # Clear snap visualization

    def _header_label(self) -> str:
        """Human-readable axis label for header text."""
        return 'View' if self.axis_id == 'VIEW' else self.axis_id

    def apply_numeric(self, context, props) -> None:
        """Apply rotation from the typed value.

        The ROTATION unit category parses plain numbers as degrees and
        returns radians, with 'd'/'r' suffixes and expressions supported.
        """
        runtime = get_runtime(context)
        value = self.num_input.parse(context, 'ROTATION')
        if value is not None:
            angle = self._clamp_large_rotation(value * self._typed_sign)
        else:
            angle = 0.0
        rotation = Matrix.Rotation(angle, 4, self.axis_vector)
        props.rotation_matrix_flat = matrix_to_flat(rotation @ self.init_rotation_matrix)
        if runtime is not None:
            runtime.rotation_arc_current_angle = angle
        cutting.update_live_preview(context)  # live cut preview
        if context.area:
            if self.num_input.active:
                context.area.header_text_set(
                    f"Rotate {self._header_label()}: [{self.num_input.display()}]"
                )
            else:
                # Buffer emptied via backspace - mouse takes over on next move
                context.area.header_text_set(None)


class CutPlaneRotateXOperator(CutPlaneRotateBaseOperator):
    """Rotate plane around X axis."""
    bl_idname = "view3d.cut_plane_rotate_x"
    bl_label = "Rotate Plane X"
    bl_description = """Rotate the cut plane around the dial's X axis
Shift — precision, Ctrl — angle snap, or type an exact angle"""
    axis_id = 'X'
    local_axis = AXIS_X


class CutPlaneRotateYOperator(CutPlaneRotateBaseOperator):
    """Rotate plane around Y axis."""
    bl_idname = "view3d.cut_plane_rotate_y"
    bl_label = "Rotate Plane Y"
    bl_description = """Rotate the cut plane around the dial's Y axis
Shift — precision, Ctrl — angle snap, or type an exact angle"""
    axis_id = 'Y'
    local_axis = AXIS_Y


class CutPlaneRotateZOperator(CutPlaneRotateBaseOperator):
    """Rotate plane around Z axis."""
    bl_idname = "view3d.cut_plane_rotate_z"
    bl_label = "Rotate Plane Z"
    bl_description = """Rotate the cut plane around the dial's Z axis
Shift — precision, Ctrl — angle snap, or type an exact angle"""
    axis_id = 'Z'
    local_axis = AXIS_Z


class CutPlaneRotateViewOperator(CutPlaneRotateBaseOperator):
    """Rotate plane around view Z axis (screen-space rotation)."""
    bl_idname = "view3d.cut_plane_rotate_view"
    bl_label = "Rotate Plane View"
    bl_description = """Rotate the cut plane around the view axis (R)
X/Y/Z — constrain to an axis, Shift — precision, Ctrl — angle snap, or type an angle"""
    axis_id = 'VIEW'
    def _get_axis_color(self):
        # Accent color ties the view dial's arc helplines to the plane
        # (the dial itself is accent-tinted too - see CutPlaneGroup.setup)
        return get_theme_active_object_color()

    def _setup_axis(self, context):
        # View-aligned: rotate around camera's view direction (computed dynamically)
        rv3d = context.region_data
        try:
            view_inv = rv3d.view_matrix.inverted()
            self.axis_vector = view_inv.col[2].xyz.normalized()
        except ValueError:
            # Fallback to Z-axis if view matrix is singular
            self.axis_vector = AXIS_Z.copy()
        self.dial_plane_normal = self.axis_vector

    def _get_dial_scale(self):
        return GIZMO_VIEW_DIAL_SCALE  # 1.2 - larger arc for view dial


class CutPlaneTrackballOperator(CutPlaneModalMixin, Operator):
    """Trackball-rotate the plane."""
    # Native trackball math (transform_mode_trackball.cc):
    # axis = cam_right * (-dy) + cam_up * dx; angle = axis.length.
    bl_idname = "view3d.cut_plane_trackball"
    bl_label = "Trackball Rotate Plane"
    bl_description = "Free-rotate the cut plane by dragging (trackball)"
    # Tool state, not undoable; not REGISTERed (see CutPlaneRotateBaseOperator)
    bl_options = {'BLOCKING'}
    _status_legend = _LEGEND_TRACKBALL  # overrides the base's dial/view legend

    require_runtime = True

    def setup(self, context, event):
        runtime = get_runtime(context)
        if runtime is None:
            return {'CANCELLED'}

        self.init_mouse = Vector((event.mouse_region_x, event.mouse_region_y))
        self.prev_mouse = self.init_mouse.copy()

        # Dial center + rotation from the single plane-pose source (ROTATE mode
        # collapses to world-space offsets inside it).
        pose = get_plane_pose(context)
        if pose is None:
            return {'CANCELLED'}
        self.dial_center, self.init_rotation_matrix = pose

        # Cache view axes at invoke time (don't change mid-drag)
        rv3d = context.region_data
        try:
            view_inv = rv3d.view_matrix.inverted()
        except ValueError:
            # Matrix is singular - cannot proceed with trackball rotation
            return {'CANCELLED'}
        self.cam_right = view_inv.col[0].xyz.normalized()
        self.cam_up = view_inv.col[1].xyz.normalized()

        # Accumulated rotation matrix (updated incrementally each frame)
        self.accumulated_rotation = Matrix.Identity(4)

        # Numeric type-in: two angles in degrees, like native R R -
        # value 1 rotates around the view's horizontal axis, value 2
        # around its vertical axis, Tab switches between them
        self.num_input = MultiNumericInput(2, allow_units=True)
        self._phi = [0.0, 0.0]  # Running angle totals for the header

        # Hide other dials during trackball rotation
        runtime.rotation_active_dial = 'TRACKBALL'
        return None

    def apply_numeric(self, context, props) -> None:
        """Apply rotation from the typed values (degrees).

        Combines the two angles exactly like native applyTrackballValue():
        axis = cam_right * phi0 + cam_up * phi1, angle = |axis|.
        """
        # ROTATION category returns radians (plain numbers are degrees)
        phi = self.num_input.values(context, 'ROTATION')

        axis = self.cam_right * phi[0] + self.cam_up * phi[1]
        angle = axis.length
        if angle > 1e-8:
            rotation = Matrix.Rotation(angle, 4, axis.normalized())
        else:
            rotation = Matrix.Identity(4)

        # Replace the accumulated rotation and totals so mouse control
        # resumes from the typed state if the buffers are emptied
        self.accumulated_rotation = rotation
        self._phi = phi

        props.rotation_matrix_flat = matrix_to_flat(rotation @ self.init_rotation_matrix)

        cutting.update_live_preview(context)  # live cut preview
        if context.area:
            if self.num_input.active:
                context.area.header_text_set(
                    f"Trackball: {self.num_input.display()} (Tab switches)"
                )
            else:
                # Buffers emptied via backspace - mouse takes over again
                context.area.header_text_set(None)

    def handle_modal_event(self, context, event, props, runtime):
        # Native R R (return leg): pressing the rotate key during a keyboard
        # trackball switches back to the view-axis rotate, restarting from
        # the initial rotation - so it toggles between the rotation styles.
        from ..ui.tool import chord_matches, transform_chords
        if (chord_matches(transform_chords()[1], event) and event.value == 'PRESS'
                and not self.num_input.active
                and getattr(self, '_keyboard_invoked', False)):
            props.rotation_matrix_flat = matrix_to_flat(self.init_rotation_matrix)
            runtime.rotation_active_dial = ""
            # Re-cut onto the restored pose before handing back (see the
            # view-rotate -> trackball leg).
            cutting.update_live_preview(context)
            if context.area:
                context.area.header_text_set(None)
            # Clear before hand-over: a failed successor invoke must not pin
            # a stale legend (see the view-rotate -> trackball leg).
            self._status_text_set(context, None)
            release_operator(self)
            bpy.ops.view3d.cut_plane_rotate_view('INVOKE_DEFAULT')
            return {'FINISHED'}

        if event.type == 'MOUSEMOVE':
            mouse_pos = Vector((event.mouse_region_x, event.mouse_region_y))

            # Calculate delta from PREVIOUS frame (incremental).
            # / ui_scale: mouse deltas are PHYSICAL pixels, so a fixed
            # radians-per-pixel made the same wrist motion rotate twice as
            # fast on a 2x display - normalize to logical distance.
            sens = TRACKBALL_SENSITIVITY / ui_scale_factor()
            delta_x = (mouse_pos.x - self.prev_mouse.x) * sens
            delta_y = (mouse_pos.y - self.prev_mouse.y) * sens
            self.prev_mouse = mouse_pos.copy()

            # Typed angles own the rotation while numeric input is active;
            # prev_mouse keeps tracking above so control resumes without a
            # jump if the buffers are emptied
            if self.num_input.active:
                return {'RUNNING_MODAL'}

            if event.shift:
                delta_x *= TRACKBALL_PRECISION_FAC
                delta_y *= TRACKBALL_PRECISION_FAC

            # Native trackball algorithm (from transform_mode_trackball.cc):
            # Native inverts Y: phi[0] = init_y - curr_y = -delta_y
            # Native keeps X: phi[1] = curr_x - init_x = +delta_x
            # axis = cam_right * phi[0] + cam_up * phi[1]
            axis = self.cam_right * (-delta_y) + self.cam_up * delta_x
            angle = axis.length

            # Running totals for the header readout (approximate for long
            # curved drags - incremental accumulation is not separable
            # into two fixed-axis angles, but close enough as a guide for
            # what typing the same values would produce)
            self._phi[0] += -delta_y
            self._phi[1] += delta_x

            if angle > 0.0001:
                axis.normalize()
                frame_rotation = Matrix.Rotation(angle, 4, axis)
                # Pre-multiply: accumulate in global space
                self.accumulated_rotation = frame_rotation @ self.accumulated_rotation
                new_matrix = self.accumulated_rotation @ self.init_rotation_matrix
                props.rotation_matrix_flat = matrix_to_flat(new_matrix)

            if context.area:
                shift_text = " (Precision)" if event.shift else ""
                context.area.header_text_set(
                    f"Trackball: {math.degrees(self._phi[0]):.2f}°  "
                    f"{math.degrees(self._phi[1]):.2f}°{shift_text}"
                )
                context.area.tag_redraw()

            cutting.update_live_preview(context)  # live cut preview
            return {'RUNNING_MODAL'}
        return None

    def on_finish(self, context, props, runtime):
        # No snap flag to clear: trackball has no snapping, and every path
        # INTO trackball (invoke, R-R hand-over) already cleared it.
        runtime.rotation_active_dial = ""  # Restore dial visibility

    def on_cancel(self, context, props, runtime):
        # Restore initial state (also the force-cancel body)
        props.rotation_matrix_flat = matrix_to_flat(self.init_rotation_matrix)
        if runtime is not None:
            runtime.rotation_active_dial = ""  # Restore dial visibility
