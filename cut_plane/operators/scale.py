# SPDX-License-Identifier: GPL-3.0-or-later
"""Scale operator: plane, single-axis, and uniform scaling."""

import bpy
from bpy.types import Operator
from bpy_extras import view3d_utils
from mathutils import Vector

from ..lib.constants import (
    AXIS_VECTOR_MAP,
    SCALE_PRECISION_FAC,
    SCALE_SNAP_INCREMENT,
    SCALE_ZERO_EPSILON,
)
from ..lib.math_utils import get_plane_pose
from .modal_base import CutPlaneModalMixin
from .numeric_input import NumericInput
from .snapping import snap_enabled

# Header labels per constraint_axis (locked axis -> scaled plane)
_SCALE_HEADER_LABELS = {
    "": "Scale",
    "Z": "Scale XY",
    "X": "Scale YZ",
    "Y": "Scale XZ",
    "ONLY_X": "Scale X",
    "ONLY_Y": "Scale Y",
    "ONLY_Z": "Scale Z",
}

# Constraint axis lines per constraint_axis: the axes being SCALED (native
# scale handles show the line(s) of the scaled axes; uniform scale shows none)
_SCALE_LINE_AXES = {
    "": (),
    "Z": ('X', 'Y'),
    "X": ('Y', 'Z'),
    "Y": ('X', 'Z'),
    "ONLY_X": ('X',),
    "ONLY_Y": ('Y',),
    "ONLY_Z": ('Z',),
}


# Bottom status-bar key legend (shown while the modal runs) as
# ((key spec, ...), label) entries - rendered as native key chords by
# modal_base._legend_draw (spec = event type, optional 'shift+' prefix).
_LEGEND_SCALE = (
    # Mouse first, key last in both: the wide Esc icon must sit LAST, where
    # the template sizes it (see modal_base._WIDE_KEYS).
    (('LEFTMOUSE', 'RET'), "Confirm"),
    (('RIGHTMOUSE', 'ESC'), "Cancel"),
    (('LEFT_SHIFT',), "Precision"),
    (('LEFT_CTRL',), "Snap"),
    # [0] stands in for the digit keys - any of them starts numeric type-in.
    (('ZERO',), "Type factor"),
)


class CutPlaneScalePlaneOperator(CutPlaneModalMixin, Operator):
    """Scale the plane."""
    # constraint_axis: "" uniform, "Z" scales XY, "X" scales YZ, "Y" scales XZ.
    bl_idname = "view3d.cut_plane_scale_plane"
    bl_label = "Scale Plane"
    bl_description = """Scale the cut plane (S; corner handles scale per-axis)
Shift — precision, Ctrl — snap, or type an exact factor"""
    # Tool state, not undoable: scaling the cut plane must not create an undo
    # step, so Ctrl+Z steps through cuts not plane nudges.
    # Not REGISTERed either: the Adjust Last Operation panel it claimed pops
    # the previous undo step (the last cut) on any field touch - see
    # move.CutPlaneMoveAxisOperator.
    bl_options = {'BLOCKING'}
    _status_legend = _LEGEND_SCALE

    constraint_axis: bpy.props.StringProperty(
        name="Constraint Axis",
        description="Axis to lock (leave empty for uniform scale)",
        default=""
    )

    def setup(self, context, event):
        props = context.window_manager.cut_plane

        # All three initial scales (any constraint may be numeric-typed)
        self.init_x_scale = props.x_scale
        self.init_y_scale = props.y_scale
        self.init_z_scale = props.z_scale

        # Gizmo center from the single plane-pose source, projected to 2D.
        # The 3D pose is kept for the constraint axis lines (the scale axes
        # are PLANE-LOCAL by definition, so the lines follow pose rotation).
        pose = get_plane_pose(context)
        if pose is None:
            return {'CANCELLED'}
        self._center_3d, self._plane_rotation = pose

        region = context.region
        rv3d = context.region_data
        center_2d = view3d_utils.location_3d_to_region_2d(
            region, rv3d, self._center_3d
        )
        if center_2d is None:
            return {'CANCELLED'}

        self.center_2d = Vector(center_2d)
        self.init_mouse = Vector((event.mouse_region_x, event.mouse_region_y))

        # Native: store initial distance from center
        init_vec = self.init_mouse - self.center_2d
        self.init_distance = max(init_vec.length, 1.0)

        # Precision mode tracking (prevents jumps when toggling Shift)
        self.precision_offset = 0.0
        self.prev_raw_ratio = 1.0

        # Numeric type-in (absolute scale factor)
        self.num_input = NumericInput()
        return None

    def _apply_scale_ratio(self, props, scale_ratio: float) -> None:
        """Apply ratio to the axes selected by constraint_axis.

        Allows negative scale (geometry flipping), prevents zero singularity.
        """
        def apply_scale(init_val, ratio):
            new_val = init_val * ratio
            if abs(new_val) < SCALE_ZERO_EPSILON:
                new_val = SCALE_ZERO_EPSILON if new_val >= 0 else -SCALE_ZERO_EPSILON
            return new_val

        if self.constraint_axis == "":
            # Uniform scale (all 3)
            props.x_scale = apply_scale(self.init_x_scale, scale_ratio)
            props.y_scale = apply_scale(self.init_y_scale, scale_ratio)
            props.z_scale = apply_scale(self.init_z_scale, scale_ratio)
        elif self.constraint_axis == "Z":
            # XY plane (lock Z)
            props.x_scale = apply_scale(self.init_x_scale, scale_ratio)
            props.y_scale = apply_scale(self.init_y_scale, scale_ratio)
        elif self.constraint_axis == "X":
            # YZ plane (lock X)
            props.y_scale = apply_scale(self.init_y_scale, scale_ratio)
            props.z_scale = apply_scale(self.init_z_scale, scale_ratio)
        elif self.constraint_axis == "Y":
            # XZ plane (lock Y)
            props.x_scale = apply_scale(self.init_x_scale, scale_ratio)
            props.z_scale = apply_scale(self.init_z_scale, scale_ratio)
        elif self.constraint_axis == "ONLY_X":
            props.x_scale = apply_scale(self.init_x_scale, scale_ratio)
        elif self.constraint_axis == "ONLY_Y":
            props.y_scale = apply_scale(self.init_y_scale, scale_ratio)
        elif self.constraint_axis == "ONLY_Z":
            props.z_scale = apply_scale(self.init_z_scale, scale_ratio)

    def constraint_visual_lines(self):
        """Axis lines for the draw layer: the axes being scaled, in the
        plane's local space (scale is plane-local by definition). Uniform
        scale (center handle / keyboard S) shows none - native does not
        draw a line for unconstrained resize either."""
        if getattr(self, '_plane_rotation', None) is None:
            return []
        return [(a, self._center_3d,
                 (self._plane_rotation @ AXIS_VECTOR_MAP[a]).normalized())
                for a in _SCALE_LINE_AXES.get(self.constraint_axis, ())]

    def kb_constraint_state(self):
        # The tuple a switch handed us, verbatim - scale has no X/Y/Z keys of
        # its own, so this is the only way a keyboard scale is constrained,
        # and G-X -> S -> R must keep the original axis AND space.
        return getattr(self, '_kb_constraint', None)

    def adopt_kb_constraint(self, context, event, constraint) -> None:
        """Mid-modal switch hand-over, in scale's constraint vocabulary:
        AXIS X scales that axis only (ONLY_X); PLANE X locks X and scales
        the YZ plane (native G-shift+X -> S scales the same plane). Space
        is moot here - the scale axes are plane-local by definition."""
        self._kb_constraint = constraint
        _kind, axis, _space = constraint
        self.constraint_axis = f"ONLY_{axis}" if _kind == 'AXIS' else axis

    def apply_numeric(self, context, props):
        value = self.num_input.parse(context, 'NONE')
        self._apply_scale_ratio(props, value if value is not None else 1.0)
        if context.area:
            if self.num_input.active:
                label = _SCALE_HEADER_LABELS.get(self.constraint_axis, "Scale")
                context.area.header_text_set(f"{label}: [{self.num_input.display()}]")
            else:
                # Buffer emptied via backspace - mouse takes over on next move
                context.area.header_text_set(None)

    def handle_modal_event(self, context, event, props, runtime):
        if event.type == 'MOUSEMOVE' and not self.num_input.active:
            current_mouse = Vector((event.mouse_region_x, event.mouse_region_y))
            current_vec = current_mouse - self.center_2d
            current_distance = current_vec.length

            # Native algorithm: distance ratio
            raw_ratio = current_distance / self.init_distance
            # Clamp ratio to prevent extreme values when clicking near center
            raw_ratio = max(-1000.0, min(raw_ratio, 1000.0))

            # Detect direction flip for negative scale
            init_dir = (self.init_mouse - self.center_2d).normalized()
            current_dir = current_vec.normalized() if current_vec.length > 0.1 else init_dir
            if init_dir.dot(current_dir) < 0:
                raw_ratio = -raw_ratio

            # Precision mode with accumulated offset (no jumps on Shift toggle)
            delta_ratio = raw_ratio - self.prev_raw_ratio
            self.prev_raw_ratio = raw_ratio

            if event.shift:
                self.precision_offset += delta_ratio * (1.0 - SCALE_PRECISION_FAC)

            scale_ratio = raw_ratio - self.precision_offset

            # Snapping (native: magnet XOR Ctrl, Affect Scale) - 10%
            # increments, 1% with Shift (native hardcodes these steps)
            if snap_enabled(context, event, 'snap_affect_scale'):
                step = SCALE_SNAP_INCREMENT * (0.1 if event.shift else 1.0)
                scale_ratio = round(scale_ratio / step) * step

            self._apply_scale_ratio(props, scale_ratio)

            if context.area:
                label = _SCALE_HEADER_LABELS.get(self.constraint_axis, "Scale")
                shift_text = " (Precision)" if event.shift else ""
                context.area.header_text_set(f"{label}: {scale_ratio:.4f}{shift_text}")
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        return None

    def on_cancel(self, context, props, runtime):
        props.x_scale = self.init_x_scale
        props.y_scale = self.init_y_scale
        props.z_scale = self.init_z_scale
