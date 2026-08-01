# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless anchor-stability test: the plane must NEVER jump on orientation,
mode, object-switch, or first-tick events.

Runs inside Blender:

    blender --background --factory-startup --python tests/integration/anchor_stability.py

Contract under test (properties.handle_orientation_or_mode_change +
ensure_default_center):

  1. STABILITY ACROSS OBJECT SWITCH: position the plane while editing A,
     switch to editing B, flip orientation -> the plane must not move.
     (The old axis_world_position cache teleported it to A's old world
     position - the 2026-07-05 anchor bug.)
  2. FIRST-TICK PRIMING: the handler's first run of a session must record
     state, not convert offsets (the old code converted against a default
     (0,0,0) cache -> plane teleported to the WORLD origin).
  3. CHANGE OF BASIS: GLOBAL<->LOCAL flips with a real rotation must keep
     the plane world-position identical (offsets are converted in place).
  4. DEFAULT PLACEMENT: a virgin plane centers on the mesh bbox (not the
     origin); once positioned, object switches never re-center it; Reset
     re-centers explicitly.

Exit code 0 on success, 1 with the failed checks listed.
"""
import math
import os
import sys

import bpy
from mathutils import Matrix, Vector

ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

import cut_plane  # noqa: E402
from cut_plane.lib import math_utils  # noqa: E402
from cut_plane.state import properties  # noqa: E402

cut_plane.register()

failures = []


def check(name, cond, detail=""):
    print(f"[{'ok  ' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


def vec_close(a, b, eps=1e-4):
    return (Vector(a) - Vector(b)).length < eps


def make_cube(name, loc):
    if bpy.context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.mesh.primitive_cube_add(location=loc)
    obj = bpy.context.active_object
    obj.name = name
    return obj


def edit(obj):
    if bpy.context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')


def pose_pos():
    pose = math_utils.get_plane_pose(bpy.context)
    return None if pose is None else Vector(pose[0])


def tick():
    """What a depsgraph update would do for the offset bookkeeping."""
    properties.handle_orientation_or_mode_change(bpy.context.scene)


props = bpy.context.window_manager.cut_plane

a = make_cube("A", (0, 0, 0))
b = make_cube("B", (10, 0, 0))

# --- 2. first-tick priming must not move a plane on an off-origin object ---
edit(b)
before = pose_pos()
tick()  # first handler run of the session (trackers empty)
check("first tick does not teleport the plane", vec_close(pose_pos(), before),
      f"{tuple(before)} -> {tuple(pose_pos())}")

# --- 1. stability across object switch + orientation flip ------------------
edit(a)
tick()
props.x_offset = 2.0  # user positions the plane on A -> world (2, 0, 0)
edit(b)
before = pose_pos()  # rides at B.origin + offset = (12, 0, 0)
check("plane rides origin-relative after object switch",
      vec_close(before, (12, 0, 0)), f"{tuple(before)}")
props.orientation = 'LOCAL'
tick()
check("orientation flip after object switch does not move the plane",
      vec_close(pose_pos(), before), f"{tuple(before)} -> {tuple(pose_pos())}")

# --- 3. change of basis with a real rotation --------------------------------
rot = Matrix.Rotation(math.radians(90.0), 4, 'Z')
props.rotation_matrix_flat = [v for row in rot for v in row]
props.orientation = 'GLOBAL'
tick()
props.x_offset, props.y_offset, props.z_offset = 3.0, 0.0, 0.0
before = pose_pos()
props.orientation = 'LOCAL'
tick()
ok_fwd = vec_close(pose_pos(), before)
check("GLOBAL->LOCAL basis change keeps the plane still", ok_fwd,
      f"{tuple(before)} -> {tuple(pose_pos())}")
props.orientation = 'GLOBAL'
tick()
check("LOCAL->GLOBAL basis change keeps the plane still",
      vec_close(pose_pos(), before), f"{tuple(before)} -> {tuple(pose_pos())}")
props.orientation = 'LOCAL'
tick()
before = pose_pos()
props.mode = 'ROTATE'  # effective orientation collapses LOCAL -> GLOBAL
tick()
check("ROTATE-mode collapse keeps the plane still",
      vec_close(pose_pos(), before), f"{tuple(before)} -> {tuple(pose_pos())}")
props.mode = 'MOVE'
tick()

# --- 4. default placement: virgin plane centers on the mesh bbox -----------
# fresh state
props.x_offset = props.y_offset = props.z_offset = 0.0
props.center_offset = (0.0, 0.0, 0.0)
props.orientation = 'GLOBAL'
props.rotation_matrix_flat = [v for row in Matrix.Identity(4) for v in row]
tick()
runtime = properties.get_runtime(bpy.context)
runtime.centered_edit_uid = 0

c = make_cube("C", (0, 0, 0))
c.data.transform(Matrix.Translation((0, 0, 5)))  # mesh center at z=5, origin at 0
edit(c)
properties.ensure_default_center(bpy.context)
check("virgin plane defaults to the mesh bbox center",
      vec_close(pose_pos(), (0, 0, 5)), f"{tuple(pose_pos())}")

props.center_offset = (1.0, 0.0, 5.0)  # the user repositions the plane
edit(b)
properties.ensure_default_center(bpy.context)  # object switch: NOT virgin
check("positioned plane is never re-centered on object switch",
      vec_close(props.center_offset, (1.0, 0.0, 5.0)),
      f"{tuple(props.center_offset)}")

edit(c)
bpy.ops.view3d.cut_plane_reset()
check("Reset re-centers on the mesh bbox",
      vec_close(pose_pos(), (0, 0, 5)), f"{tuple(pose_pos())}")

# --- 5. per-channel reset: Alt+R must clear rotation WITHOUT moving --------
props.orientation = 'LOCAL'
tick()
rot = Matrix.Rotation(math.radians(37.0), 4, 'Z')
props.rotation_matrix_flat = [v for row in rot for v in row]
props.x_offset, props.y_offset, props.z_offset = 1.0, 2.0, 0.5  # LOCAL space
before = pose_pos()
bpy.ops.view3d.cut_plane_reset(mode='ROTATION')
check("rotation clear keeps the plane still (LOCAL offsets re-based)",
      vec_close(pose_pos(), before), f"{tuple(before)} -> {tuple(pose_pos())}")
check("rotation clear actually cleared the rotation",
      vec_close(props.rotation_matrix_flat[:3], (1.0, 0.0, 0.0)))
bpy.ops.view3d.cut_plane_reset(mode='SCALE')
check("scale clear leaves position untouched",
      vec_close(pose_pos(), before), f"{tuple(before)} -> {tuple(pose_pos())}")
bpy.ops.view3d.cut_plane_reset(mode='LOCATION')
check("location clear returns to the mesh bbox center",
      vec_close(pose_pos(), (0, 0, 5)), f"{tuple(pose_pos())}")

# --- 6. Alt+G is IDEMPOTENT under a destructive Clear preview --------------
# The reset used to center on obj.bound_box - the EVALUATED box, which the
# preview's Clear shrinks. Each press then chased the surviving half's
# center: the plane moved, the preview re-cut, the box halved again
# (measured walking 0 -> 0.75 -> 0.875 -> 0.938 toward the top face). The
# reset now reads the BASELINE (the uncut snapshot): the first press lands
# on the true center and every further press is a no-op.
from cut_plane import cutting as _cutting  # noqa: E402

props.orientation = 'GLOBAL'
edit(c)
bpy.ops.mesh.select_all(action='SELECT')
props.cut_clear_inner = True             # preview deletes a side
props.x_offset = props.y_offset = 0.0
props.z_offset = 0.5                     # off-center: the box will shrink
_cutting.update_live_preview(bpy.context)
bpy.context.view_layer.update()          # the eval between real clicks

bpy.ops.view3d.cut_plane_reset(mode='LOCATION')
bpy.context.view_layer.update()
first = pose_pos()
check("ALT+G: first press lands on the TRUE (baseline) center",
      vec_close(first, (0, 0, 5)), f"{tuple(first)}")
for _press in range(2):
    bpy.ops.view3d.cut_plane_reset(mode='LOCATION')
    bpy.context.view_layer.update()
check("ALT+G: repeated presses do not move the plane (idempotent)",
      vec_close(pose_pos(), first), f"{tuple(first)} -> {tuple(pose_pos())}")
props.cut_clear_inner = False
_cutting.restore_live_cut(bpy.context)

print(f"\nANCHOR STABILITY: {'FAIL - ' + ', '.join(failures) if failures else 'OK'}")
sys.stdout.flush()
os._exit(1 if failures else 0)
