# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless test: multi-object edit mode - cut AND live preview.

    blender --background --factory-startup --python tests/integration/multi_object_cut.py

Native edit-mode semantics: the cut applies to EVERY mesh in the
multi-object edit session (like native Bisect), deduped by mesh data so a
linked duplicate is cut once. The live preview runs ONE SESSION PER MESH
(per-object snapshot/baseline in cutting._sessions), so every edit mesh
previews the cut, and restore returns every mesh to its original.

Exit code 0 on success, 1 with the failed checks listed.
"""
import os
import sys

import bmesh
import bpy
from mathutils import Vector

ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

import cut_plane  # noqa: E402
from cut_plane import cutting  # noqa: E402
from cut_plane.lib import constants  # noqa: E402

constants.DEBUG = True
cut_plane.register()

failures = []


def check(name, cond, detail=""):
    print(f"[{'ok  ' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


def faces(obj):
    return len(bmesh.from_edit_mesh(obj.data).faces)


def make_cube(name, location):
    bpy.ops.mesh.primitive_cube_add(size=2, location=location)
    obj = bpy.context.active_object
    obj.name = name
    return obj


def enter_multi_edit(*objs):
    if bpy.context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.select_all(action='DESELECT')
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[-1]
    bpy.ops.object.mode_set(mode='EDIT')


# Two separated cubes, both straddling the world Z=0 plane
a = make_cube("A", (0.0, 0.0, 0.0))
b = make_cube("B", (5.0, 0.0, 0.0))

# --- 0. live preview runs a session per mesh ---------------------------------
enter_multi_edit(a, b)
bpy.ops.mesh.select_all(action='DESELECT')
sc = bpy.context.window_manager.cut_plane
sc.center_offset = (0.0, 0.0, 0.0)
sc.x_offset = sc.y_offset = sc.z_offset = 0.0
cutting.update_live_preview(bpy.context)
check("one session per edit mesh", len(cutting._sessions) == 2,
      f"{len(cutting._sessions)} sessions")
check("preview split on the active mesh", faces(b) > 6, f"faces={faces(b)}")
check("preview split on the NON-active mesh too", faces(a) > 6,
      f"faces={faces(a)}")
check("two snapshots (one per session)",
      cutting.count_snapshot_datablocks() == 2)
cutting.restore_live_cut(bpy.context)
check("restore returns BOTH meshes to the original",
      faces(a) == 6 and faces(b) == 6, f"{faces(a)}, {faces(b)}")
check("restore drops every session", not cutting._sessions)

# --- 1. plain cut hits every edit mesh --------------------------------------
result = bpy.ops.view3d.cut_plane_cut(plane_co=(0, 0, 0), plane_no=(0, 0, 1))
check("multi-object cut FINISHED", result == {'FINISHED'})
check("active mesh cut", faces(b) > 6, f"faces={faces(b)}")
check("NON-active mesh cut too", faces(a) > 6, f"faces={faces(a)}")

# --- 2. Selected Only cuts only the objects with a selected domain ----------
bpy.ops.object.mode_set(mode='OBJECT')
enter_multi_edit(a, b)
bpy.ops.mesh.select_all(action='DESELECT')
fa0, fb0 = faces(a), faces(b)
# Select everything on A only (B keeps an empty domain)
bm_a = bmesh.from_edit_mesh(a.data)
for v in bm_a.verts:
    v.select_set(True)
bm_a.select_flush(True)
bmesh.update_edit_mesh(a.data, destructive=False)
result = bpy.ops.view3d.cut_plane_cut(
    plane_co=(0, 0, 0.5), plane_no=(0, 0, 1), selected_only=True)
check("selected-only multi cut FINISHED", result == {'FINISHED'})
check("selected object cut", faces(a) > fa0, f"{fa0} -> {faces(a)}")
check("empty-domain object untouched", faces(b) == fb0, f"{fb0} -> {faces(b)}")

# --- 3. nothing selected anywhere -> clean cancel ----------------------------
bpy.ops.mesh.select_all(action='DESELECT')
fa0, fb0 = faces(a), faces(b)
result = bpy.ops.view3d.cut_plane_cut(
    plane_co=(0, 0, -0.5), plane_no=(0, 0, 1), selected_only=True)
check("nothing selected -> CANCELLED", result == {'CANCELLED'})
check("no mesh changed on cancel", faces(a) == fa0 and faces(b) == fb0)

# --- 4. plane missing every mesh -> clean cancel -----------------------------
result = bpy.ops.view3d.cut_plane_cut(plane_co=(0, 0, 50), plane_no=(0, 0, 1))
check("plane misses all -> CANCELLED", result == {'CANCELLED'})
check("no mesh changed on miss", faces(a) == fa0 and faces(b) == fb0)

# --- 5. linked duplicates (shared mesh data) are cut ONCE --------------------
bpy.ops.object.mode_set(mode='OBJECT')
c = make_cube("C", (0.0, 5.0, 0.0))
bpy.ops.object.select_all(action='DESELECT')
c.select_set(True)
bpy.context.view_layer.objects.active = c
bpy.ops.object.duplicate(linked=True)
d = bpy.context.active_object
d.location = (0.0, 8.0, 0.0)
enter_multi_edit(c, d)
bpy.ops.mesh.select_all(action='DESELECT')
fc0 = faces(c)
result = bpy.ops.view3d.cut_plane_cut(plane_co=(0, 5, 0), plane_no=(0, 0, 1))
check("linked-duplicate cut FINISHED", result == {'FINISHED'})
# One straight cut through a cube adds exactly 4 side faces (6 -> 10 with
# the split); a double cut would add 8. Same shared data for both objects.
check("shared data cut exactly once", faces(c) == fc0 + 4,
      f"{fc0} -> {faces(c)}")

check("cache invariants clean", not cutting.check_invariants(),
      str(cutting.check_invariants()))

# --- a zero-scale-axis object must not break the whole session -------------
# matrix_world.inverted() raises ValueError on such an object; unguarded in
# plane_to_object_local it blew up the live preview, Execute Cut AND the
# tool's Ctrl+Z the moment any object in the edit session had a zeroed
# scale axis. The helper now returns a zero normal - the SKIP signal - so
# the degenerate object is skipped and every healthy one still cuts.
if bpy.context.mode != 'OBJECT':
    bpy.ops.object.mode_set(mode='OBJECT')
cutting.restore_live_cut(bpy.context)
z = make_cube("Zflat", (5.0, 0.0, 0.0))
z.scale = (1.0, 1.0, 0.0)                 # a real user state (flattened)
h = make_cube("Healthy", (0.0, 0.0, 0.0))
enter_multi_edit(z, h)
bpy.ops.mesh.select_all(action='SELECT')

co_l, no_l = cutting.plane_to_object_local(z, Vector((0, 0, 0)), Vector((0, 0, 1)))
check("zero-scale object resolves to the zero-normal SKIP signal, no raise",
      no_l.length_squared < 1e-12, str(no_l))

hf0, zf0 = faces(h), faces(z)
cutting.update_live_preview(bpy.context)  # used to raise ValueError here
check("live preview survives a zero-scaled object in the session",
      faces(h) != hf0, f"healthy {hf0} -> {faces(h)}")
check("...which is skipped untouched", faces(z) == zf0)

res = bpy.ops.view3d.cut_plane_cut(
    'EXEC_DEFAULT', plane_co=(0.0, 0.0, 0.0), plane_no=(0.0, 0.0, 1.0))
check("Execute Cut still FINISHES with a zero-scaled object present",
      res == {'FINISHED'}, str(res))
check("...the healthy object is really cut", faces(h) > hf0,
      f"{hf0} -> {faces(h)}")

print("\nMULTI OBJECT CUT:",
      "OK" if not failures else f"FAIL ({len(failures)}): {failures}")
sys.exit(1 if failures else 0)
