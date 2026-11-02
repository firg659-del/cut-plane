# SPDX-License-Identifier: GPL-3.0-or-later
"""Move operators: axis-constrained arrows and the free-move center button."""

from typing import Optional

import bpy
from bpy.types import Operator
from bpy_extras import view3d_utils
from mathutils import Matrix, Vector

from ..lib.constants import (  # noqa: I001
    AXIS_VECTOR_MAP,
    AXIS_Z,
    MOVE_PRECISION_FAC,
    PARALLEL_EPSILON,
)
from ..lib.math_utils import (
    flat_to_matrix,
    get_constraint_axis_world,
    get_current_orientation_type,
    get_mesh_origin_world,
    get_pixel_world_size,
    get_plane_pose,
)
from ..state.properties import get_runtime
from .. import cutting
from .numeric_input import MultiNumericInput, NumericInput
from .snapping import (
    resolve_translation_snap,
    restore_align_rotation,
    set_snap_marker,
)
from .modal_base import CutPlaneModalMixin
from .utils import next_kb_constraint

# Free (movable) axes per locked constraint axis for plane handlers
_PLANE_FREE_AXES = {'Z': ('X', 'Y'), 'X': ('Y', 'Z'), 'Y': ('X', 'Z')}

# Tracking radius as a multiple of the view distance to the drag anchor:
# beyond this the ray is grazing its constraint (plane horizon / rail
# vanishing point) and the tracked point is racing toward infinity - the
# sample is rejected and the drag freezes at its last value instead.
_TRACK_RANGE = 100.0


def _ray_line_closest_point(ray_origin: Vector, ray_dir: Vector,
                            line_point: Vector, line_dir: Vector) -> Optional[Vector]:
    """Closest point on a line to a ray, or None when the sample is unusable
    (parallel ray, vanishing point crossed, or a grazing blowup). Callers
    keep their last value on None, so the drag freezes instead of teleporting.

    THE function behind the native "rail" feel: the plane rides the point on
    the axis nearest the mouse ray, so it tracks the cursor 1:1. Shared by the
    arrow-handle drags and the keyboard G->X/Y/Z axis constraint.
    """
    w0 = ray_origin - line_point
    a = ray_dir.dot(ray_dir)      # 1 if normalized
    b = ray_dir.dot(line_dir)
    c = line_dir.dot(line_dir)    # 1 if normalized
    d = ray_dir.dot(w0)
    e = line_dir.dot(w0)

    denom = a * c - b * b
    if abs(denom) < PARALLEL_EPSILON:
        # Exactly parallel: no unique closest point. (The old fallback
        # projected the RAY ORIGIN onto the rail - i.e. teleported the
        # plane back to the camera's shadow on the axis.)
        return None

    # Closest-approach parameter ON THE RAY (Ericson two-line form). Once
    # the mouse crosses the axis's VANISHING POINT the ray diverges from
    # the rail and this goes negative: the "closest point" then flips to
    # the far side (measured: +5000 -> -5000 in one event - the plane
    # racing back through the camera at super speed). Freeze instead.
    s_ray = (b * e - c * d) / denom
    if s_ray <= 0.0:
        return None

    # Line parameter of the closest point (Ericson, Real-Time Collision
    # Detection: t = (a*f - b*c)/denom in his notation). HISTORY: this used to
    # be (b*d - a*e), the NEGATED parameter - it returned the point MIRRORED
    # through line_point. The arrow operator silently compensated with a
    # second inversion (delta = init - current), so the bug was invisible
    # until the keyboard constraint consumed the function without the hack
    # and moved opposite to the mouse. Sign-locked by unit tests.
    t_line = (a * e - b * d) / denom

    # Grazing guard: just BEFORE the vanishing point the rail point races
    # to infinity while the ray is still measurably non-parallel. Track
    # only within a sane radius of the anchor, scaled by the view distance
    # so zoomed-out drags keep their reach.
    if abs(t_line) > _TRACK_RANGE * max(w0.length, 1.0):
        return None
    return line_point + line_dir * t_line


# Bottom status-bar key legends (shown while the modal runs) as
# ((key spec, ...), label) entries - rendered as native key chords by
# modal_base._legend_draw (spec = event type, optional 'shift+' prefix).
# Arrow/plane handles are already axis/plane-constrained, so they omit the
# X/Y/Z hints the free (center / keyboard-G) move offers.
_LEGEND_MOVE_AXIS = (
    # Mouse first, key last in both: the wide Esc icon must sit LAST, where
    # the template sizes it (see modal_base._WIDE_KEYS).
    (('LEFTMOUSE', 'RET'), "Confirm"),
    (('RIGHTMOUSE', 'ESC'), "Cancel"),
    (('LEFT_SHIFT',), "Precision"),
    (('LEFT_CTRL',), "Snap"),
    # [0] stands in for the digit keys - any of them starts numeric type-in
    # (native has no numeric-input hint, so there is no convention to copy).
    (('ZERO',), "Type value"),
)
_LEGEND_MOVE_FREE = (
    (('LEFTMOUSE', 'RET'), "Confirm"),
    (('RIGHTMOUSE', 'ESC'), "Cancel"),
    (('X', 'Y', 'Z'), "Axis"),
    # Shift only on the first key: draws the merged [Shift][X][Y][Z] chord,
    # matching how native merges the three Shift+axis items.
    (('shift+X', 'Y', 'Z'), "Plane"),
    (('LEFT_SHIFT',), "Precision"),
    (('LEFT_CTRL',), "Snap"),
    (('ZERO',), "Type value"),
)


class CutPlaneMoveAxisOperator(CutPlaneModalMixin, Operator):
    """Move the plane along an axis."""
    bl_idname = "view3d.cut_plane_move_axis"
    bl_label = "Move Plane Axis"
    bl_description = """Slide the cut plane along this arrow's axis
Shift — precision, Ctrl — snap, or type an exact distance"""
    # No 'UNDO': moving the cut plane is TOOL STATE (like the 3D cursor), not
    # mesh data, so it must not create an undo step - that keeps Ctrl+Z stepping
    # through actual cuts instead of plane nudges.
    # No 'REGISTER': it put this modal in the Adjust Last Operation panel,
    # where touching ANY field triggers undo-then-re-execute - and since the
    # move pushed no undo step, the undo pops the PREVIOUS step (the last
    # cut!) - measured live 2026-07-11. Without REGISTER the panel stays on
    # the cut operator, whose F9 is safe (see cut.py bl_options).
    bl_options = {'BLOCKING'}
    _status_legend = _LEGEND_MOVE_AXIS

    constraint_axis: bpy.props.StringProperty(
        name="Constraint Axis",
        description="Axis for constrained movement (X, Y, or Z)",
        default="X"
    )

    def _get_axis_vector_world(self, context) -> Vector:
        """
        Get the axis vector in world space based on orientation mode.
        """
        props = context.window_manager.cut_plane
        orientation_type = get_current_orientation_type(context)
        rotation_matrix = flat_to_matrix(props.rotation_matrix_flat)

        local_axis = AXIS_VECTOR_MAP.get(self.constraint_axis, AXIS_Z)
        if orientation_type == 'LOCAL':
            return (rotation_matrix @ local_axis).normalized()
        return local_axis.copy()

    require_runtime = True

    def setup(self, context, event):
        props = context.window_manager.cut_plane
        runtime = get_runtime(context)
        if runtime is None:
            return {'CANCELLED'}

        orientation_type = get_current_orientation_type(context)

        # Drag anchor + rotation from the single plane-pose source; the matrix
        # and its inverse are cached for reuse in modal() (performance, and
        # mid-drag stability - modal never re-reads the live props for these).
        pose = get_plane_pose(context)
        if pose is None:
            return {'CANCELLED'}
        self.init_pos_world, self._cached_rotation_matrix = pose
        try:
            self._cached_rotation_matrix_inv = self._cached_rotation_matrix.inverted()
        except ValueError:
            # Matrix is singular - use identity as fallback
            self._cached_rotation_matrix_inv = Matrix.Identity(4)

        self.axis_vector = self._get_axis_vector_world(context)

        region = context.region
        rv3d = context.region_data
        mouse_coord = (event.mouse_region_x, event.mouse_region_y)
        ray_origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, mouse_coord)
        ray_dir = view3d_utils.region_2d_to_vector_3d(region, rv3d, mouse_coord)
        self.init_point_on_axis = _ray_line_closest_point(
            ray_origin, ray_dir, self.init_pos_world, self.axis_vector
        )
        if self.init_point_on_axis is None:
            return {'CANCELLED'}

        self.init_x_offset = props.x_offset
        self.init_y_offset = props.y_offset
        self.init_z_offset = props.z_offset

        # Precision mode tracking (prevents jumps when toggling Shift)
        self.precision_offset = 0.0
        self._prev_total_dist = 0.0

        self.orientation_type = orientation_type

        # Numeric type-in (scene length units, e.g. "1.5" or "25cm")
        self.num_input = NumericInput(allow_units=True)

        # Hide this arrow while dragging (see _draw_prepare_move_mode):
        # Blender draws the interacting arrow from state captured at drag
        # start, so it cannot track precision/typed values
        runtime.move_active_axis = self.constraint_axis
        return None

    def apply_numeric(self, context, props) -> None:
        """Apply movement from the typed value (distance along the axis)."""
        # Typed input owns the transform: drop snap visuals and alignment
        # (the mousemove path that normally clears them is skipped)
        set_snap_marker(context, None)
        restore_align_rotation(props, self)

        distance = self.num_input.parse(context, 'LENGTH')
        delta_world = self.axis_vector * (distance if distance is not None else 0.0)

        if self.orientation_type == 'LOCAL':
            delta_local = self._cached_rotation_matrix_inv @ delta_world
            props.x_offset = self.init_x_offset + delta_local.x
            props.y_offset = self.init_y_offset + delta_local.y
            props.z_offset = self.init_z_offset + delta_local.z
        else:  # GLOBAL
            props.x_offset = self.init_x_offset + delta_world.x
            props.y_offset = self.init_y_offset + delta_world.y
            props.z_offset = self.init_z_offset + delta_world.z

        cutting.update_live_preview(context)  # live cut preview
        if context.area:
            if self.num_input.active:
                context.area.header_text_set(f"Move {self.constraint_axis}: [{self.num_input.display()}]")
            else:
                # Buffer emptied via backspace - mouse takes over on next move
                context.area.header_text_set(None)

    def constraint_visual_lines(self):
        """Axis line for the draw layer: the arrow drag IS an axis constraint,
        so it shows the same native line as keyboard G + X/Y/Z."""
        if getattr(self, 'axis_vector', None) is None:
            return []
        return [(self.constraint_axis, self.init_pos_world, self.axis_vector)]

    def handle_modal_event(self, context, event, props, runtime):
        # Mouse movement (skipped while numeric input is active)
        if event.type == 'MOUSEMOVE' and not self.num_input.active:
            region = context.region
            rv3d = context.region_data
            mouse_coord = (event.mouse_region_x, event.mouse_region_y)
            ray_origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, mouse_coord)
            ray_dir = view3d_utils.region_2d_to_vector_3d(region, rv3d, mouse_coord)
            current_point_on_axis = _ray_line_closest_point(
                ray_origin, ray_dir, self.init_pos_world, self.axis_vector
            )

            if current_point_on_axis is not None:
                # Movement delta along the rail (both points come from the
                # now-sign-correct _ray_line_closest_point, so this is the
                # plain forward difference - the old "(init - current) with
                # ADDITION" double-inversion hack is gone)
                delta_world = current_point_on_axis - self.init_point_on_axis

                # Precision mode with offset accumulation (native algorithm,
                # same as rotate/scale): pressing Shift mid-drag continues
                # from the current position at 10% speed instead of
                # rescaling the total delta (which jumped back)
                total_dist = delta_world.dot(self.axis_vector)
                frame_delta = total_dist - self._prev_total_dist
                self._prev_total_dist = total_dist

                if event.shift:
                    self.precision_offset += frame_delta * (1.0 - MOVE_PRECISION_FAC)

                display_dist = total_dist - self.precision_offset

                # Snapping via the shared resolver (geometry beats
                # increment/grid; marker and align-rotation handled there)
                delta_world = resolve_translation_snap(
                    context, event, self, props,
                    anchor=self.init_pos_world,
                    raw_delta=self.axis_vector * display_dist,
                    axis=self.axis_vector)

                if self.orientation_type == 'LOCAL':
                    # world -> local for storage (cached inverse from invoke)
                    delta_local = self._cached_rotation_matrix_inv @ delta_world
                    props.x_offset = self.init_x_offset + delta_local.x
                    props.y_offset = self.init_y_offset + delta_local.y
                    props.z_offset = self.init_z_offset + delta_local.z
                else:  # GLOBAL: delta is already world-space
                    props.x_offset = self.init_x_offset + delta_world.x
                    props.y_offset = self.init_y_offset + delta_world.y
                    props.z_offset = self.init_z_offset + delta_world.z

                distance_moved = delta_world.length
                if delta_world.dot(self.axis_vector) < 0:
                    distance_moved = -distance_moved
                shift_text = " (Precision)" if event.shift else ""
                if context.area:
                    context.area.header_text_set(f"Move {self.constraint_axis}: {distance_moved:.2f}m{shift_text}")

            cutting.update_live_preview(context)  # live cut preview
            if context.area:
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        return None

    def on_finish(self, context, props, runtime):
        # Bake the final offset into center_offset and reset axis offsets
        # This prevents the gizmo arrows from teleporting back
        local_offset = Vector((props.x_offset, props.y_offset, props.z_offset))

        if self.orientation_type == 'LOCAL':
            # Transform local offset to world space (use cached matrix)
            world_offset = self._cached_rotation_matrix @ local_offset
        else:
            # Already in world space
            world_offset = local_offset

        # Add the offset to center_offset (bake the transform)
        current_center = Vector(props.center_offset)
        new_center = current_center + world_offset
        props.center_offset = (new_center.x, new_center.y, new_center.z)

        # Reset axis offsets to zero (neutral position)
        props.x_offset = 0.0
        props.y_offset = 0.0
        props.z_offset = 0.0
        runtime.move_active_axis = ""  # Restore arrow visibility
        set_snap_marker(context, None)

    def on_cancel(self, context, props, runtime):
        # Restore initial values (also the force-cancel body - the old
        # force path was missing restore_align_rotation; unified now)
        props.x_offset = self.init_x_offset
        props.y_offset = self.init_y_offset
        props.z_offset = self.init_z_offset
        if runtime is not None:
            runtime.move_active_axis = ""  # Restore arrow visibility
        set_snap_marker(context, None)
        restore_align_rotation(props, self)


class CutPlaneMoveCenterOperator(CutPlaneModalMixin, Operator):
    """Move the plane freely (unconstrained)."""
    # Projects the mouse onto a view-aligned plane at the gizmo's depth, so
    # dragging stays 1:1 in perspective instead of drifting ("floaty").
    bl_idname = "view3d.cut_plane_move_center"
    bl_label = "Move Plane Center"
    bl_description = """Move the cut plane freely at its current depth (G)
X/Y/Z — axis constraint, Shift — precision, Ctrl — snap, or type a distance"""
    # Tool state, not undoable; not REGISTERed either (see
    # CutPlaneMoveAxisOperator - the adjust panel popped the last cut)
    bl_options = {'BLOCKING'}

    def status_legend(self):
        # This operator serves BOTH the free move (center button / keyboard G)
        # and the constrained square/axis handle drags. The X/Y/Z retarget
        # keys only exist for the free move (the key handler is gated on
        # `not self.constraint_axis`), so constrained drags must not
        # advertise them - they get the hint-less legend.
        return _LEGEND_MOVE_AXIS if self.constraint_axis else _LEGEND_MOVE_FREE

    constraint_axis: bpy.props.StringProperty(
        name="Constraint Axis",
        description="Axis normal to the movement plane (X, Y, Z) or empty for free move",
        default=""
    )

    is_plane_constraint: bpy.props.BoolProperty(
        name="Is Plane Constraint",
        description="True for 2D plane movement (squares), False for 1D axis movement (arrows)",
        default=False
    )

    def _get_constraint_vector(self, context) -> Vector:
        """World-space vector for constraint_axis in the active orientation
        (fallback Z). Used as the constrained-plane normal for ray-plane
        tracking; the edge-on fallback projects view movement onto it."""
        props = context.window_manager.cut_plane
        orientation_type = get_current_orientation_type(context)
        gizmo_rotation_matrix = flat_to_matrix(props.rotation_matrix_flat)

        local_axis = AXIS_VECTOR_MAP.get(self.constraint_axis, AXIS_Z)  # Fallback to Z
        return get_constraint_axis_world(
            orientation_type,
            local_axis,
            gizmo_rotation_matrix
        )

    def setup(self, context, event):
        self.init_mouse_x = event.mouse_region_x
        self.init_mouse_y = event.mouse_region_y

        props = context.window_manager.cut_plane
        self.init_center_offset = Vector(props.center_offset).copy()

        # Drag anchor + rotation from the single plane-pose source; cached for
        # reuse in modal() (performance + mid-drag stability).
        pose = get_plane_pose(context)
        if pose is None:
            return {'CANCELLED'}
        self.start_pos_world, self._cached_rotation_matrix = pose
        self._cached_orientation_type = get_current_orientation_type(context)

        if self.constraint_axis:
            self.plane_normal = self._get_constraint_vector(context)
            # Ray-plane intersection for stable cursor tracking (shared helper;
            # None when the view is edge-on - the modal falls back to projection)
            self.prev_intersect = self._mouse_plane_intersect(
                context, event, self.plane_normal)

        self.accumulated_delta = Vector((0.0, 0.0, 0.0))
        self.prev_mouse_x = self.init_mouse_x
        self.prev_mouse_y = self.init_mouse_y

        # Free-move precision (Shift) as native does it: a VIRTUAL cursor
        # that moves at 10% while Shift is held, and every tracking mode
        # maps that cursor. Scaling the result in world space instead
        # evaluated the plane lock's ray at the REAL cursor - at a grazing
        # view that mapping races toward the horizon, so a Shift+Z drag ran
        # ~2x ahead of native and kept accelerating (measured at 6 deg).
        self._vm_prev = Vector((self.init_mouse_x, self.init_mouse_y))
        self._vm_offset = Vector((0.0, 0.0))

        # Numeric type-in: one value per free axis, Tab switches between
        # them (native multi-value input). Plane handlers have 2 free
        # axes, the free-move center button has all 3.
        if self.constraint_axis and self.is_plane_constraint:
            self._free_axes = _PLANE_FREE_AXES.get(self.constraint_axis, ('X', 'Y'))
        elif self.constraint_axis:
            self._free_axes = (self.constraint_axis,)
        else:
            self._free_axes = ('X', 'Y', 'Z')
        self.num_input = MultiNumericInput(len(self._free_axes), allow_units=True)

        # Keyboard X/Y/Z constraint (free move only): (kind, axis, space) or None
        self._kb_constraint = None
        self._kb_ref = None                       # tracking reference at engage
        self._kb_base = Vector((0.0, 0.0, 0.0))   # carried-over delta at engage
        self._kb_switched = False                 # constraint changed this event
        return None

    # --- keyboard X/Y/Z constraints (native G -> X/Y/Z, free move only) ------

    def _kb_axis_world(self, axis_id: str, space: str) -> Vector:
        """The constraint axis in world space, in the chosen GLOBAL/LOCAL space."""
        return get_constraint_axis_world(
            space, AXIS_VECTOR_MAP[axis_id], self._cached_rotation_matrix)

    def _set_kb_constraint(self, context, kind: str, axis: str) -> None:
        """Engage / cycle / clear the keyboard constraint (native semantics)."""
        self.adopt_kb_constraint(
            context, None, next_kb_constraint(self._kb_constraint, kind, axis))

    def kb_constraint_state(self):
        return getattr(self, '_kb_constraint', None)

    def adopt_kb_constraint(self, context, event, constraint) -> None:
        """Engage *constraint* ((kind, axis, space) or None): the shared tail
        of the X/Y/Z key cycling, and the receiving end of a mid-modal
        transform switch (native G-X -> R -> G keeps the X constraint).

        Rebuilds the numeric type-in for the new free axes (typed values then
        mean distances along the constraint's axes, in its space).
        """
        self._kb_constraint = constraint
        if constraint is None:
            self._free_axes = ('X', 'Y', 'Z')
        elif constraint[0] == 'AXIS':
            self._free_axes = (constraint[1],)
        else:  # PLANE: the two axes the plane spans
            self._free_axes = _PLANE_FREE_AXES.get(constraint[1], ('X', 'Y'))
        self.num_input = MultiNumericInput(len(self._free_axes), allow_units=True)
        # New tracking basis: re-reference on the next apply (jump-onto-axis
        # semantics) and resync the precision tracker (no spurious spike).
        self._kb_ref = None
        self._kb_switched = True

    def _kb_header_suffix(self) -> str:
        """Native-style constraint readout for the header ('' when free)."""
        if self._kb_constraint is None:
            return ""
        kind, axis, space = self._kb_constraint
        space_txt = "local " if space == 'LOCAL' else "global "
        if kind == 'AXIS':
            return f" along {space_txt}{axis}"
        return f" locking {space_txt}{axis}"

    def constraint_visual_lines(self):
        """Axis lines for the draw layer: [(axis_id, anchor, world_direction)].

        Keyboard constraint: one line along the movement axis (AXIS) or the
        two in-plane axes (PLANE). Gizmo SQUARE-handle drags are plane
        constraints by construction, so they show their two in-plane axis
        lines the same way (native square handles do too). Free move: none.
        """
        if getattr(self, '_kb_constraint', None) is not None:
            kind, axis, space = self._kb_constraint
            axes = (axis,) if kind == 'AXIS' else _PLANE_FREE_AXES.get(axis, ())
            return [(a, self.start_pos_world, self._kb_axis_world(a, space))
                    for a in axes]
        if self.constraint_axis and getattr(self, '_free_axes', None):
            # Handle-invoked constrained drag: the handle's own axes, in the
            # tool orientation the handle was built for.
            return [(a, self.start_pos_world,
                     self._kb_axis_world(a, self._cached_orientation_type))
                    for a in self._free_axes]
        return []

    def _free_screen_delta(self, context, mouse) -> Optional[Vector]:
        """Unconstrained free-move delta: screen-space reprojection (the plane
        stays exactly under the cursor in perspective). None = skip frame."""
        start_pos = self.start_pos_world
        screen_pos = view3d_utils.location_3d_to_region_2d(
            context.region, context.region_data, start_pos)
        if screen_pos is None:
            return None  # anchor behind the camera
        new_screen = (screen_pos[0] + mouse[0] - self.init_mouse_x,
                      screen_pos[1] + mouse[1] - self.init_mouse_y)
        new_pos_world = view3d_utils.region_2d_to_location_3d(
            context.region, context.region_data, new_screen, start_pos)
        if not new_pos_world:
            return None
        return new_pos_world - start_pos

    def _mouse_ray(self, context, event, mouse=None):
        """(origin, direction) of the mouse ray for the current event, or
        through `mouse` (region pixels) - the free move's virtual cursor."""
        if mouse is None:
            mouse = (event.mouse_region_x, event.mouse_region_y)
        return (view3d_utils.region_2d_to_origin_3d(context.region, context.region_data, mouse),
                view3d_utils.region_2d_to_vector_3d(context.region, context.region_data, mouse))

    def _mouse_plane_intersect(self, context, event, normal: Vector,
                               mouse=None) -> Optional[Vector]:
        """Mouse-ray intersection with the constraint plane through the drag
        anchor, or None when the ray is (near-)parallel to the plane.

        THE plane-tracking math - shared by the square plane handlers and the
        keyboard Shift+X/Y/Z plane lock (one implementation, two entry points).
        """
        ray_origin, ray_dir = self._mouse_ray(context, event, mouse)
        denom = ray_dir.dot(normal)
        if abs(denom) < PARALLEL_EPSILON:
            return None
        t = (self.start_pos_world - ray_origin).dot(normal) / denom
        if t <= 0.0:
            # Horizon crossed: the "hit" is behind the ray origin, on the
            # opposite side of the world (measured: one tilt past the
            # horizon flips t from +50k to -50k - a 100k-unit reversed
            # teleport). The projection fallback takes over instead.
            return None
        hit = ray_origin + t * ray_dir
        # Grazing guard: approaching the horizon the hit races to infinity
        # while |denom| is still orders of magnitude above the parallel
        # epsilon. Track only within a sane radius of the anchor, scaled by
        # the view distance so zoomed-out drags keep their reach.
        view_dist = max((self.start_pos_world - ray_origin).length, 1.0)
        if (hit - self.start_pos_world).length > _TRACK_RANGE * view_dist:
            return None
        return hit

    def _kb_axis_delta(self, context, event, axis_vec: Vector, mouse) -> Optional[Vector]:
        """Rail movement for the keyboard AXIS constraint - the SAME math as
        the arrow handles: the plane rides the point on the axis closest to
        the mouse ray, so it tracks the cursor 1:1 (native G->X feel).

        On engage (_kb_ref None): the delta applied so far jumps onto the axis
        (its projection becomes the base) and the current ray sets the
        reference, so subsequent motion is relative - no teleport under the
        cursor, exactly like native.
        """
        ray_origin, ray_dir = self._mouse_ray(context, event, mouse)
        current = _ray_line_closest_point(
            ray_origin, ray_dir, self.start_pos_world, axis_vec)
        if current is None:
            return None
        if self._kb_ref is None:
            self._kb_ref = current
            self._kb_base = axis_vec * self.accumulated_delta.dot(axis_vec)
        return self._kb_base + (current - self._kb_ref)

    def _kb_plane_delta(self, context, event, normal: Vector, mouse) -> Optional[Vector]:
        """Ray-plane movement for the keyboard PLANE lock - the same math as
        the square plane handlers: the cursor's intersection with the
        constraint plane drives the motion. Engage semantics as the axis case."""
        current = self._mouse_plane_intersect(context, event, normal, mouse)
        if current is None:
            return None  # view edge-on to the plane - skip frame
        if self._kb_ref is None:
            self._kb_ref = current
            self._kb_base = (self.accumulated_delta
                             - normal * self.accumulated_delta.dot(normal))
        return self._kb_base + (current - self._kb_ref)

    def _apply_free_move(self, context, event, props) -> None:
        """One free-move step: constraint-appropriate cursor tracking + snap.

        Unconstrained: screen-space reprojection. Keyboard AXIS constraint:
        ray-line rail (identical to the arrow handles). Keyboard PLANE lock:
        ray-plane intersection (identical to the square handles). The X/Y/Z
        key handler calls this with the KEY event so engaging applies at once.
        """
        # Precision: advance the virtual cursor at 10% while Shift is held
        # (see setup). Toggling Shift never jumps - the offset only grows
        # by the motion made while it was down.
        real = Vector((event.mouse_region_x, event.mouse_region_y))
        if event.shift:
            self._vm_offset += (real - self._vm_prev) * (1.0 - MOVE_PRECISION_FAC)
        self._vm_prev = real
        mouse = real - self._vm_offset

        axis_vec = plane_normal = None
        if self._kb_constraint is not None:
            kind, axis_id, space = self._kb_constraint
            if kind == 'AXIS':
                axis_vec = self._kb_axis_world(axis_id, space)
                raw_delta = self._kb_axis_delta(context, event, axis_vec, mouse)
            else:
                plane_normal = self._kb_axis_world(axis_id, space)
                raw_delta = self._kb_plane_delta(context, event, plane_normal, mouse)
        else:
            raw_delta = self._free_screen_delta(context, mouse)
        if raw_delta is None:
            return  # degenerate view for this tracking mode - skip frame
        self._kb_switched = False

        # Guarantee the constraint against float drift in the tracking.
        if axis_vec is not None:
            raw_delta = axis_vec * raw_delta.dot(axis_vec)
        elif plane_normal is not None:
            raw_delta = raw_delta - plane_normal * raw_delta.dot(plane_normal)

        applied_delta = resolve_translation_snap(
            context, event, self, props,
            anchor=self.start_pos_world, raw_delta=raw_delta,
            axis=axis_vec, plane_normal=plane_normal)

        self.accumulated_delta = applied_delta
        props.center_offset = self.init_center_offset + applied_delta

        if context.area:
            if self._kb_constraint is not None:
                context.area.header_text_set(f"Move{self._kb_header_suffix()}")
            context.area.tag_redraw()

    def apply_numeric(self, context, props) -> None:
        """Apply movement from the typed values along the free axes.

        Values are interpreted in the active orientation space (GLOBAL:
        world axes, LOCAL: gizmo-local axes), matching the plane the
        handler drags in.
        """
        # Typed input owns the transform: drop snap visuals and alignment
        # (the mousemove path that normally clears them is skipped)
        set_snap_marker(context, None)
        restore_align_rotation(props, self)

        values = self.num_input.values(context, 'LENGTH')
        # Axis basis: the keyboard constraint's space when engaged (typed values
        # are distances along ITS axes), else the tool orientation as before.
        if self._kb_constraint is not None:
            basis_space = self._kb_constraint[2]
        else:
            basis_space = self._cached_orientation_type
        world_delta = Vector((0.0, 0.0, 0.0))
        for axis_id, value in zip(self._free_axes, values, strict=False):
            world_delta += self._kb_axis_world(axis_id, basis_space) * value

        # Keep mouse tracking in sync so dragging resumes cleanly from
        # the typed position if the buffers are emptied again
        self.accumulated_delta = world_delta
        self.prev_intersect = None

        props.center_offset = self.init_center_offset + world_delta

        cutting.update_live_preview(context)  # live cut preview
        if context.area:
            if self.num_input.active:
                label = "".join(self._free_axes)
                context.area.header_text_set(
                    f"Move {label}: {self.num_input.display()} (Tab switches)"
                )
            else:
                # Buffers emptied via backspace - mouse takes over again
                context.area.header_text_set(None)

    def handle_modal_event(self, context, event, props, runtime):
        # Keyboard axis constraint (native G -> X/Y/Z): free move only - the
        # gizmo plane/axis handlers are already constrained by construction.
        # Shift+key locks the PLANE the axis is normal to; repeat cycles
        # global -> local -> off. Applied immediately (native jump-onto-axis).
        if (event.value == 'PRESS' and event.type in {'X', 'Y', 'Z'}
                and not self.num_input.active and not self.constraint_axis):
            self._set_kb_constraint(context, 'PLANE' if event.shift else 'AXIS', event.type)
            if context.area and self._kb_constraint is None:
                context.area.header_text_set(None)
            self._apply_free_move(context, event, props)
            cutting.update_live_preview(context)
            return {'RUNNING_MODAL'}

        if event.type == 'MOUSEMOVE' and not self.num_input.active:
            dx = event.mouse_region_x - self.prev_mouse_x
            dy = event.mouse_region_y - self.prev_mouse_y
            self.prev_mouse_x = event.mouse_region_x
            self.prev_mouse_y = event.mouse_region_y

            rv3d = context.region_data
            try:
                view_inv = rv3d.view_matrix.inverted()
            except ValueError:
                # Singular view matrix (degenerate view) - skip this frame.
                # prev_mouse is already updated, so the next move resumes cleanly.
                return {'RUNNING_MODAL'}
            cam_right = view_inv.col[0].xyz.normalized()
            cam_up = view_inv.col[1].xyz.normalized()

            # Pixel size at the gizmo drives mouse sensitivity
            origin = get_mesh_origin_world(context)
            props = context.window_manager.cut_plane
            local_offset = Vector((props.x_offset, props.y_offset, props.z_offset))
            if self._cached_orientation_type == 'LOCAL':
                world_offset = self._cached_rotation_matrix @ local_offset
            else:
                world_offset = local_offset

            current_pos = origin + world_offset + Vector(props.center_offset)
            px_size = get_pixel_world_size(context.region, rv3d, current_pos)

            if event.shift:
                px_size *= 0.1

            delta_vec_world = Vector((0.0, 0.0, 0.0))

            if self.constraint_axis in {'X', 'Y', 'Z'}:
                if self.is_plane_constraint:
                    # --- PLANE MODE (squares): ray-plane intersection tracking
                    current_intersect = self._mouse_plane_intersect(
                        context, event, self.plane_normal)
                    if current_intersect is not None:
                        if self.prev_intersect is not None:
                            delta_vec_world = current_intersect - self.prev_intersect
                            # Precision mode: scale the incremental delta
                            # (jump-free by construction)
                            if event.shift:
                                delta_vec_world *= MOVE_PRECISION_FAC

                        self.prev_intersect = current_intersect
                    else:
                        # Fallback: ray parallel to plane (use vector projection)
                        mouse_move_view = (cam_right * dx + cam_up * dy) * px_size
                        normal_component = mouse_move_view.dot(self.plane_normal)
                        delta_vec_world = mouse_move_view - (self.plane_normal * normal_component)

                else:
                    # --- AXIS MODE: the plane rides the point on the rail
                    # closest to the mouse ray - ABSOLUTE tracking, the same
                    # math as the arrow operator and the keyboard G->X path.
                    # The old incremental mapping scaled every mouse delta by
                    # the pixel size AT THE PLANE'S CURRENT DEPTH: receding
                    # made each step bigger, and once far away one leftward
                    # event yanked the plane back thousands of units. Ray
                    # math has no per-event gain to compound: increments
                    # telescope to (current - first), path-independent.
                    axis_vec = get_constraint_axis_world(
                        self._cached_orientation_type,
                        AXIS_VECTOR_MAP[self.constraint_axis],
                        self._cached_rotation_matrix
                    )
                    ray_origin, ray_dir = self._mouse_ray(context, event)
                    current = _ray_line_closest_point(
                        ray_origin, ray_dir, self.start_pos_world, axis_vec)
                    if current is not None:
                        if self.prev_intersect is not None:
                            delta_vec_world = current - self.prev_intersect
                            # Precision mode: scale the incremental delta
                            # (jump-free by construction, like plane mode)
                            if event.shift:
                                delta_vec_world *= MOVE_PRECISION_FAC
                        self.prev_intersect = current
                    # None = unusable sample (vanishing point / parallel):
                    # keep the last value - the drag freezes, never teleports

            else:
                # --- FREE MOVE (center handle / keyboard G): applies itself
                self._apply_free_move(context, event, props)

            # AXIS and PLANE modes apply here (free move applied itself above)
            if self.constraint_axis:
                # Raw tracking stays unsnapped; snap is applied on top so
                # leaving snap returns to the mouse position (native feel)
                self.accumulated_delta += delta_vec_world
                applied_delta = resolve_translation_snap(
                    context, event, self, props,
                    anchor=self.start_pos_world,
                    raw_delta=self.accumulated_delta.copy(),
                    plane_normal=self.plane_normal)

                new_offset = self.init_center_offset + applied_delta
                props.center_offset = new_offset

                if context.area:
                    context.area.tag_redraw()

            cutting.update_live_preview(context)  # live cut preview
            return {'RUNNING_MODAL'}
        return None

    def on_finish(self, context, props, runtime):
        set_snap_marker(context, None)

    def on_cancel(self, context, props, runtime):
        props.center_offset = self.init_center_offset
        set_snap_marker(context, None)
        restore_align_rotation(props, self)
