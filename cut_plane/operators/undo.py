# SPDX-License-Identifier: GPL-3.0-or-later
"""Tool-scoped Ctrl+Z / Ctrl+Shift+Z: plane-pose undo, then native undo.

Bound in the TOOL keymap only (tool.py), so these exist solely while Cut
Plane is the active tool - everywhere else native undo is untouched and a
cut undoes in one press. Inside the tool, Ctrl+Z walks back through plane
moves first, then hands mesh steps (cuts, edits, option toggles) to native
ed.undo - the ordering logic lives in pose_history (token alignment).
"""

import bpy
from bpy.types import Operator

from .. import cutting
from ..state import pose_history


def _refresh_preview(context) -> None:
    """Snap the cut line onto the restored pose (pose fields have no update
    callbacks) and redraw."""
    cutting.update_live_preview(context)
    if context.area:
        context.area.tag_redraw()


class CutPlaneUndoOperator(Operator):
    """Undo the last plane move, then the last cut (tool-scoped Ctrl+Z)"""
    bl_idname = "view3d.cut_plane_undo"
    bl_label = "Undo (Cut Plane)"
    bl_description = ("Undo the newest change: plane moves first, then "
                      "cuts and mesh edits (native undo)")
    # INTERNAL: not searchable - it only makes sense on the tool's Ctrl+Z.
    # No 'UNDO'/'REGISTER': this operator MANAGES undo, it must never
    # push steps or claim the adjust panel itself.
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def execute(self, context):
        if pose_history.undo(context):
            _refresh_preview(context)
            return {'FINISHED'}
        # Newest history is a mesh step - native undo owns it. The existing
        # undo_post handler resets the preview sessions as usual.
        if bpy.ops.ed.undo.poll():
            return bpy.ops.ed.undo()
        return {'CANCELLED'}


class CutPlaneRedoOperator(Operator):
    """Redo the last undone plane move, or hand over to native redo"""
    bl_idname = "view3d.cut_plane_redo"
    bl_label = "Redo (Cut Plane)"
    bl_description = ("Redo the last undone change: plane moves when the "
                      "timeline lines up, otherwise native redo")
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def execute(self, context):
        if pose_history.redo(context):
            _refresh_preview(context)
            return {'FINISHED'}
        if bpy.ops.ed.redo.poll():
            return bpy.ops.ed.redo()
        return {'CANCELLED'}
