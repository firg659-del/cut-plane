# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless test: the F9 (adjust-last-operation) architecture contract.

    blender --background --factory-startup --python tests/integration/redo_pose.py

The design (final, 2026-07-11): the cut operator is REGISTER+UNDO with a
native Bisect-style panel, and that is safe ONLY because the tool state
lives on the WINDOW MANAGER - F9 re-runs as undo-then-execute, and the
undo half rolls back scene data alone. While the pose was a Scene group,
any panel edit teleported a since-moved plane back to its cut-time values
(measured live: center (1.705, 4.583, 0.586) -> (0, 0, 0)). The transform
modals stay un-REGISTERed so the panel keeps targeting the last CUT after
a plane move (a registered modal would steal it, and its "adjust" would
pop the cut's undo step).

This suite locks the structural contract headlessly; the undo half itself
is GUI-only (ed.undo cannot be driven from scripts) and is verified live.

Exit code 0 on success, 1 with the failed checks listed.
"""
import os
import sys

import bpy

ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

import cut_plane  # noqa: E402
from cut_plane import cutting  # noqa: E402
from cut_plane.lib import constants  # noqa: E402
from cut_plane.operators.cut import CutPlaneCutOperator  # noqa: E402
from cut_plane.operators.move import (  # noqa: E402
    CutPlaneMoveAxisOperator, CutPlaneMoveCenterOperator)
from cut_plane.operators.reset import CutPlaneResetOperator  # noqa: E402
from cut_plane.operators.rotate import (  # noqa: E402
    CutPlaneRotateBaseOperator, CutPlaneTrackballOperator)
from cut_plane.operators.scale import CutPlaneScalePlaneOperator  # noqa: E402

constants.DEBUG = True
cut_plane.register()

failures = []


def check(name, cond, detail=""):
    print(f"[{'ok  ' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


def center():
    return tuple(round(v, 3)
                 for v in bpy.context.window_manager.cut_plane.center_offset)


# --- the structural contract ------------------------------------------------
# Tool state on the WindowManager = out of undo's reach. The F9 undo half
# (and plain Ctrl+Z) can therefore never move the plane.
check("tool state lives on the WindowManager",
      hasattr(bpy.types.WindowManager, 'cut_plane'))
check("tool state does NOT live on the Scene (undo would roll it back)",
      not hasattr(bpy.types.Scene, 'cut_plane'))

# The cut owns the adjust panel (Bisect-style), as one clean undo step.
check("cut op: REGISTER+UNDO (native adjust panel)",
      CutPlaneCutOperator.bl_options == {'REGISTER', 'UNDO'})
check("cut op draws its panel from OPERATOR properties",
      'draw' in vars(CutPlaneCutOperator))
# Plane point/normal stay hidden: editing them re-cuts away from the
# stationary gizmo plane, which reads as a bug (user decision 2026-07-12).
# (Operator props live on the runtime RNA type, not the Python class.)
_cut_rna = bpy.ops.view3d.cut_plane_cut.get_rna_type()
for prop_name in ("plane_co", "plane_no"):
    check(f"{prop_name} hidden from the panel",
          _cut_rna.properties[prop_name].is_hidden)

# Transform modals must never claim the panel; Reset neither (and its
# native undo step would be EMPTY - pose_history covers it instead).
for cls in (CutPlaneMoveAxisOperator, CutPlaneMoveCenterOperator,
            CutPlaneRotateBaseOperator, CutPlaneTrackballOperator,
            CutPlaneScalePlaneOperator, CutPlaneResetOperator):
    check(f"{cls.__name__}: not REGISTERed",
          'REGISTER' not in cls.bl_options)
check("Reset pushes no (empty) native undo step",
      'UNDO' not in CutPlaneResetOperator.bl_options)

# --- and execute never touches the plane pose -------------------------------
bpy.ops.mesh.primitive_cube_add(size=2)
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.mesh.subdivide(number_cuts=2)
bpy.ops.mesh.select_all(action='DESELECT')
sc = bpy.context.window_manager.cut_plane

cutting.update_live_preview(bpy.context)
plane0 = cutting.get_plane_world(bpy.context)
co0, no0 = tuple(plane0[0]), tuple(plane0[1])
result = bpy.ops.view3d.cut_plane_cut(plane_co=co0, plane_no=no0)
check("cut at origin FINISHED", result == {'FINISHED'})

MOVED = (1.705, 4.583, 0.586)
sc.center_offset = MOVED
cutting.update_live_preview(bpy.context)
result = bpy.ops.view3d.cut_plane_cut(plane_co=co0, plane_no=no0, use_fill=True)
check("scripted re-run FINISHED", result == {'FINISHED'})
check("scripted re-run leaves the pose alone", center() == MOVED,
      f"center={center()}")

# live option toggles (the header / N-panel path) keep working post-commit
sc.cut_use_fill = True
sc.cut_use_fill = False
check("live option toggles never move the pose", center() == MOVED)

check("cache invariants clean", not cutting.check_invariants(),
      str(cutting.check_invariants()))

# --- F9 redo staleness: execute must refresh matrices ITSELF ---------------
# ED_undo_operator_repeat re-executes immediately after the undo, BEFORE the
# depsgraph re-evaluates - matrix_world reads stale for every object, so the
# world plane converted per object used ONE wrong matrix for all: every mesh
# was cut at the same identical LOCAL plane ("all objects look like
# instances", measured live), and with Clear on, meshes were wiped to zero
# verts. execute() now calls view_layer.update() first. The stale state is
# reproducible headless: assigning ob.location leaves matrix_world
# unevaluated until a depsgraph pass - exactly what execute must survive.
import bmesh  # noqa: E402
from mathutils import Vector  # noqa: E402

if bpy.context.mode != 'OBJECT':
    bpy.ops.object.mode_set(mode='OBJECT')
cutting.restore_live_cut(bpy.context)
for o in [o for o in bpy.data.objects if o.name.startswith("stale")]:
    bpy.data.objects.remove(o, do_unlink=True)
bpy.ops.object.select_all(action='DESELECT')


def spawn(name, loc):
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=2.0)
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    ob.location = loc
    bpy.context.collection.objects.link(ob)
    ob.select_set(True)
    return ob


a = spawn("staleA", (0.0, 0.0, 0.0))
b = spawn("staleB", (0.0, 0.0, 0.0))
bpy.context.view_layer.objects.active = a
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.mesh.select_all(action='SELECT')

# Move B far ABOVE the cut plane WITHOUT any depsgraph pass: its
# matrix_world now lies, exactly like every object's does inside the F9
# redo window. (Along Z, because the plane below is a Z plane - a stale
# matrix must make the plane WRONGLY cut B at a phantom origin.)
b.location = Vector((0.0, 0.0, 9.0))
check("fixture: B's matrix_world really is STALE (still reads origin)",
      abs(b.matrix_world.translation.z) < 1e-6,
      str(tuple(round(c, 2) for c in b.matrix_world.translation)))


def faces(ob):
    return len(bmesh.from_edit_mesh(ob.data).faces)


# DIRECT exec() call, like ED_undo_operator_repeat does - the bpy.ops entry
# machinery evaluates the depsgraph on the way in (measured: it healed the
# stale matrix and made this test pass with the fix REMOVED), so only a
# direct call reproduces the F9 timing where execute itself must refresh.
from cut_plane.operators.cut import CutPlaneCutOperator  # noqa: E402


class _FakeCutOp:
    plane_co = (0.0, 0.0, 0.0)
    plane_no = (0.0, 0.0, 1.0)
    use_fill = False
    clear_inner = False
    clear_outer = False
    selected_only = False
    select_loop = True
    mirror_x = mirror_y = mirror_z = False
    plane_anchor = 'BBOX'
    cursor_world = (0.0, 0.0, 0.0)
    bbox_anchor_world = (0.0, 0.0, 0.0)
    _bisect_object = CutPlaneCutOperator._bisect_object

    class properties:
        """The real Operator.properties interface: nothing passed explicitly,
        so execute() resolves the anchor points itself, like a scripted call."""
        @staticmethod
        def is_property_set(_name):
            return False

    def report(self, *_a):
        pass


res = CutPlaneCutOperator.execute(_FakeCutOp(), bpy.context)
check("cut FINISHES in the stale-matrix state", res == {'FINISHED'}, str(res))
check("F9 STALENESS: A (really at the plane) is cut", faces(a) > 6,
      f"{faces(a)} faces")
check("F9 STALENESS: B (really at z=9, missed) is NOT cut at a phantom "
      "origin", faces(b) == 6, f"{faces(b)} faces")

bpy.ops.object.mode_set(mode='OBJECT')
cutting.restore_live_cut(bpy.context)

print("\nREDO POSE:", "OK" if not failures else f"FAIL ({len(failures)}): {failures}")
sys.exit(1 if failures else 0)
