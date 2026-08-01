# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless test: the R-R hand-over re-cuts onto the pose it restores.

    blender --background --factory-startup --python-exit-code 1 \
            --python tests/integration/rotate_handover.py

Contract: the live preview is DESTRUCTIVE - the mesh really is bisected at
the plane. So any code that moves the plane owes the preview a re-cut, or the
orange plane and the cut geometry drift apart on screen.

The bug this locks: both R-R legs (view-rotate <-> trackball) restore the
start rotation before handing over - native restarts a rotation from scratch
on a switch - but neither re-cut. Rotate 40 degrees, press R, confirm without
moving the mouse: the plane drew at 0 degrees while the mesh stayed bisected
at 40, and it stayed wrong until the next transform. The Esc branch and
modal_base's step 2b hand-over both already had the re-cut; these two did not.

handle_modal_event is called directly with a fake operator and event: a modal
cannot be driven headless (no event loop), but the branch is a plain method.
The successor's INVOKE_DEFAULT needs a real 3D context and raises here - it
runs AFTER the re-cut, so the assertion is unaffected.

Exit code 0 on success, 1 with the failed checks listed.
"""
import os
import sys

import bmesh
import bpy
from mathutils import Matrix

ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

import cut_plane  # noqa: E402
from cut_plane import cutting  # noqa: E402
from cut_plane.lib.math_utils import matrix_to_flat  # noqa: E402
from cut_plane.operators.rotate import (  # noqa: E402
    CutPlaneRotateViewOperator,
    CutPlaneTrackballOperator,
)

cut_plane.register()

failures = []


def check(name, cond, detail=""):
    print(f"[{'ok  ' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


class FakeEvent:
    type = 'R'
    value = 'PRESS'
    ctrl = shift = alt = oskey = hyper = False
    mouse_region_x = mouse_region_y = 100


class FakeNumInput:
    active = False


class FakeOp:
    """Enough of a modal operator for the hand-over branch."""
    _active_instance = None

    def __init__(self, axis_id, init_rot):
        self.num_input = FakeNumInput()
        self._keyboard_invoked = True
        self.axis_id = axis_id
        self.init_rotation_matrix = init_rot
        self.prev_mouse = None

    @staticmethod
    def _status_text_set(context, legend):
        pass


# A cube in Edit Mode with a live preview running.
bpy.ops.object.select_all(action='DESELECT')
bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
obj = bpy.context.active_object
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.mesh.select_all(action='SELECT')

props = bpy.context.window_manager.cut_plane
runtime = bpy.context.window_manager.cut_plane_runtime

IDENTITY = Matrix.Identity(4)
ROTATED = Matrix.Rotation(0.7, 4, 'X')      # the "abandoned" 40-degree pose


def run_leg(cls, axis_id):
    """Drive one hand-over leg; return (recut_called, pose_seen_by_recut)."""
    seen = {}
    real = cutting.update_live_preview

    def spy(ctx):
        seen['called'] = True
        # The pose AT THE MOMENT of the re-cut is what the preview will use.
        seen['pose'] = tuple(props.rotation_matrix_flat)
        return real(ctx)

    cutting.update_live_preview = spy
    try:
        props.rotation_matrix_flat = matrix_to_flat(ROTATED)
        op = FakeOp(axis_id, IDENTITY)
        try:
            cls.handle_modal_event(op, bpy.context, FakeEvent(), props, runtime)
        except Exception:
            # The successor's INVOKE_DEFAULT needs a real 3D context; it runs
            # after the re-cut, so this does not affect what we assert.
            pass
    finally:
        cutting.update_live_preview = real
    return seen.get('called', False), seen.get('pose')


identity_flat = tuple(matrix_to_flat(IDENTITY))

called, pose = run_leg(CutPlaneRotateViewOperator, 'VIEW')
check("R->trackball re-cuts the preview on hand-over", called)
check("...and does it AFTER restoring the pose (preview matches the plane)",
      pose == identity_flat, f"re-cut saw {pose}")

called, pose = run_leg(CutPlaneTrackballOperator, 'VIEW')
check("trackball->R re-cuts the preview on hand-over", called)
check("...and does it AFTER restoring the pose",
      pose == identity_flat, f"re-cut saw {pose}")

# The plane and the mesh must agree once the dust settles: with the pose back
# at identity (plane through z=0 of an untouched cube) the preview cut must
# sit at the plane, not at the abandoned angle.
props.rotation_matrix_flat = matrix_to_flat(IDENTITY)
props.x_offset = props.y_offset = props.z_offset = 0.0
cutting.update_live_preview(bpy.context)
bm = bmesh.from_edit_mesh(obj.data)
zs = sorted({round(v.co.z, 4) for v in bm.verts})
check("the live cut lands on the restored plane (a cut loop at z=0)",
      0.0 in zs, str(zs))

print(f"\nROTATE HANDOVER: {'FAIL - ' + ', '.join(failures) if failures else 'OK'}")
sys.stdout.flush()
os._exit(1 if failures else 0)
