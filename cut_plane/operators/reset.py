# SPDX-License-Identifier: GPL-3.0-or-later
"""Reset operator: full reset (button) or per-channel clears (one key each).

The per-channel keys default to Alt+G / Alt+R / Alt+S - the native
clear-transform vocabulary - but they are rebindable tool keymap items, so
nothing here names a key: the panel row and the Keymap editor show the real
chord, and a tooltip restating it could only drift out of date.
"""

import bpy
from bpy.types import Operator
from mathutils import Vector

from .. import cutting
from ..lib.constants import on_internal_error
from ..lib.math_utils import effective_orientation, flat_to_matrix

_IDENTITY_FLAT = (1.0, 0.0, 0.0, 0.0,
                  0.0, 1.0, 0.0, 0.0,
                  0.0, 0.0, 1.0, 0.0,
                  0.0, 0.0, 0.0, 1.0)


class CutPlaneResetOperator(Operator):
    """Reset the plane to the mesh center with identity rotation and scale 1"""
    bl_idname = "view3d.cut_plane_reset"
    bl_label = "Reset Plane"
    # No 'UNDO': the reset writes WM tool state only, so its native undo
    # step was EMPTY - later eaten by the tool-undo fallthrough as one
    # visibly inert Ctrl+Z press. pose_history records the reset instead
    # (one Ctrl+Z restores the pre-reset pose). No 'REGISTER': it stole the
    # adjust-last-operation panel from the last cut. (Default bl_options is
    # {'REGISTER'}, hence the explicit empty set.)
    bl_options = set()

    # Which channels to reset: the header button resets everything; the
    # tool-scoped per-channel keys clear one channel each (set by the keymap
    # item, so a rebind carries its own mode).
    mode: bpy.props.EnumProperty(
        name="Reset",
        description="Which plane channels to reset",
        items=[
            ('ALL', "Everything", "Location, rotation, and scale"),
            ('LOCATION', "Location", "Plane position only (back to the mesh center)"),
            ('ROTATION', "Rotation", "Plane rotation only (position is kept)"),
            ('SCALE', "Scale", "Plane scale only"),
        ],
        default='ALL',
    )

    @classmethod
    def poll(cls, context):
        # Edit-mode only, like every other entry point of this edit-mode
        # tool. No tool-state existence check: registration order guarantees
        # the WM pointer exists whenever this class is registered.
        return context.mode == 'EDIT_MESH'

    @classmethod
    def description(cls, context, properties):
        return {
            'LOCATION': "Reset the plane position to the mesh center",
            'ROTATION': "Reset the plane rotation, keeping its position",
            'SCALE': "Reset the plane scale",
        }.get(properties.mode,
              "Reset the plane to the mesh center with identity rotation and scale 1")

    def execute(self, context):
        from ..state import pose_history
        props = context.window_manager.cut_plane
        # A reset is a pose change like any transform: one Ctrl+Z entry
        pose_before = pose_history.capture_pose(context)
        do_all = self.mode == 'ALL'

        if do_all or self.mode == 'ROTATION':
            # Clearing the rotation must not MOVE the plane: in LOCAL
            # orientation the offsets are interpreted through the rotation,
            # so re-express them in world space first (identity local ==
            # world). Skipped for ALL - position resets anyway.
            if not do_all and effective_orientation(context) == 'LOCAL':
                rotation = flat_to_matrix(props.rotation_matrix_flat)
                world = rotation @ Vector(
                    (props.x_offset, props.y_offset, props.z_offset))
                props.x_offset, props.y_offset, props.z_offset = world
            props.rotation_matrix_flat = _IDENTITY_FLAT

        if do_all or self.mode == 'LOCATION':
            props.x_offset = 0.0
            props.y_offset = 0.0
            props.z_offset = 0.0
            # The DEFAULT placement: the mesh bounding-box center (the origin
            # is often far from the geometry - a plane there intersects
            # nothing, so "Reset" would look like the tool broke). The
            # BASELINE's box, not the evaluated one: the destructive preview
            # shrinks obj.bound_box, so resetting against it chased the
            # surviving half's center on every press (measured: each Alt+G
            # halved the distance to the top face, never reaching center).
            props.center_offset = cutting.baseline_center_offset(
                context.edit_object)

        if do_all or self.mode == 'SCALE':
            props.x_scale = 1.0
            props.y_scale = 1.0
            props.z_scale = 1.0

        # The live preview tracks the plane through EXPLICIT refreshes (scene
        # property writes don't tag the depsgraph, so nothing else notices the
        # plane moved). The transform modals refresh on every mousemove; an
        # operator execute is equally write-safe - without this the preview
        # stays cut at the OLD plane until the next drag (the Alt+G/R stale-
        # preview bug).
        try:
            cutting.update_live_preview(context)
        except Exception:
            on_internal_error()  # DEBUG: surface; prod: never block the reset

        pose_history.push_if_changed(context, pose_before)

        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
        return {'FINISHED'}
