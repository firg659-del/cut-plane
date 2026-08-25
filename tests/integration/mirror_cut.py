# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless test: mirror cutting (X/Y/Z reflections of the plane).

    blender --background --factory-startup --python tests/integration/mirror_cut.py

Mirror-modifier semantics: the one real plane expands into reflections
across the object's LOCAL axes through its origin
(cutting._mirrored_planes_local); each plane runs the full bisect/clear/
fill phase in sequence - identical to cutting at every plane by hand.
Preview and Execute share the expansion; the dirty key includes the mirror
axes so toggles refresh the preview; a plane lying ON a mirror axis dedupes
to a single cut.

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


def fresh_cube():
    if bpy.context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    cutting.restore_live_cut(bpy.context)
    bpy.ops.object.select_all(action='DESELECT')
    for o in [o for o in bpy.data.objects if o.name.startswith("MirrorCube")]:
        bpy.data.objects.remove(o, do_unlink=True)
    bpy.ops.mesh.primitive_cube_add(size=2)
    obj = bpy.context.active_object
    obj.name = "MirrorCube"
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='DESELECT')
    return obj


def faces(obj):
    return len(bmesh.from_edit_mesh(obj.data).faces)


def verts_at(obj, axis, value, eps=1e-4):
    bm = bmesh.from_edit_mesh(obj.data)
    return sum(1 for v in bm.verts if abs(v.co[axis] - value) < eps)


sc = bpy.context.window_manager.cut_plane
sc.mirror_x = sc.mirror_y = sc.mirror_z = False

# --- 1. the pure expansion helper -------------------------------------------
# OBLIQUE on every component. An axis-aligned normal is reflected onto the
# SAME geometric plane by the two mirrors perpendicular to it (sliding an
# infinite plane within itself does not move the cut), so it expands to
# fewer distinct cuts - see mirror_self_reflection.py.
co, no = Vector((0.5, 0.2, 0.3)), Vector((1.0, 0.7, 0.4)).normalized()
check("no axes -> 1 plane", len(cutting._mirrored_planes_local(co, no, ())) == 1)
check("one axis -> 2 planes",
      len(cutting._mirrored_planes_local(co, no, (0,))) == 2)
check("two axes -> 4 planes",
      len(cutting._mirrored_planes_local(co, no, (0, 1))) == 4)
check("three axes -> 8 planes",
      len(cutting._mirrored_planes_local(co, no, (0, 1, 2))) == 8)
# A plane ON the mirror axis reflects onto itself -> deduped to one
on_axis = cutting._mirrored_planes_local(
    Vector((0.0, 0.0, 0.0)), Vector((0.0, 0.0, 1.0)), (0,))
check("on-axis plane dedupes", len(on_axis) == 1, f"{len(on_axis)} planes")

# --- 2. Execute: mirror X doubles the cut symmetrically ----------------------
obj = fresh_cube()
result = bpy.ops.view3d.cut_plane_cut(
    plane_co=(0.5, 0, 0), plane_no=(1, 0, 0), mirror_x=True)
check("mirror-X cut FINISHED", result == {'FINISHED'})
check("both rings cut (6 -> 14 faces)", faces(obj) == 14, f"faces={faces(obj)}")
# Each ring on a cube = 4 verts (one per crossed X-parallel edge)
check("cut is symmetric (same verts at +/-0.5)",
      verts_at(obj, 0, 0.5) == verts_at(obj, 0, -0.5) == 4,
      f"+{verts_at(obj, 0, 0.5)} / -{verts_at(obj, 0, -0.5)}")
check("write-back synced the tool state", sc.mirror_x is True)
sc.mirror_x = False

# --- 3. Clear Outer + Fill with mirror = symmetric capped slab ---------------
obj = fresh_cube()
result = bpy.ops.view3d.cut_plane_cut(
    plane_co=(0.5, 0, 0), plane_no=(1, 0, 0), mirror_x=True,
    clear_outer=True, use_fill=True)
check("mirrored clear+fill FINISHED", result == {'FINISHED'})
bm = bmesh.from_edit_mesh(obj.data)
max_x = max(abs(v.co.x) for v in bm.verts)
check("both outer sides cleared (|x| <= 0.5)", max_x <= 0.5 + 1e-4,
      f"max|x|={max_x:.3f}")
check("slab capped on both ends (6 faces)", faces(obj) == 6,
      f"faces={faces(obj)}")
sc.mirror_x = False

# --- 4. on-axis dedupe live: no double cut -----------------------------------
obj = fresh_cube()
result = bpy.ops.view3d.cut_plane_cut(
    plane_co=(0, 0, 0), plane_no=(0, 0, 1), mirror_x=True)
check("on-axis mirrored cut FINISHED", result == {'FINISHED'})
check("deduped to a single cut (10 faces)", faces(obj) == 10,
      f"faces={faces(obj)}")
sc.mirror_x = False

# --- 5. preview: mirror toggle refreshes via the dirty key -------------------
obj = fresh_cube()
sc.center_offset = (0.0, 0.0, 0.5)
sc.x_offset = sc.y_offset = sc.z_offset = 0.0
cutting.update_live_preview(bpy.context)
check("preview single plane (10 faces)", faces(obj) == 10, f"{faces(obj)}")
sc.mirror_z = True   # update callback re-cuts immediately
check("mirror toggle adds the reflected preview (14 faces)",
      faces(obj) == 14, f"{faces(obj)}")
sc.mirror_z = False
check("toggle off returns to the single preview (10 faces)",
      faces(obj) == 10, f"{faces(obj)}")
check("pose untouched by mirror toggles",
      tuple(round(v, 3) for v in sc.center_offset) == (0.0, 0.0, 0.5))

# --- 6. adopt key matches the apply key (no phantom re-cut post-commit) ------
sc.mirror_z = True
result = bpy.ops.view3d.cut_plane_cut(
    plane_co=(0, 0, 0.5), plane_no=(0, 0, 1), mirror_z=True)
check("mirrored commit FINISHED", result == {'FINISHED'})
committed = faces(obj)
cutting.update_live_preview(bpy.context)  # must be a no-op (key matches)
check("no phantom re-cut after commit (key shapes match)",
      faces(obj) == committed, f"{committed} -> {faces(obj)}")
sc.mirror_z = False

# --- 7. mirror planes shrink the preview density budget ----------------------
from cut_plane.state import preferences  # noqa: E402

obj = fresh_cube()  # 6 faces
orig_cap = preferences.preview_face_cap
preferences.preview_face_cap = lambda: 20  # 1 plane: 6<=20 ok; 8 planes: 20//8=2
try:
    sc.center_offset = (0.0, 0.0, 0.5)
    sc.x_offset = sc.y_offset = sc.z_offset = 0.0
    cutting.update_live_preview(bpy.context)
    check("session runs under the cap", cutting._session_for(obj) is not None)
    sc.mirror_x = sc.mirror_y = sc.mirror_z = True  # budget 20//8 = 2 < 6
    check("mirror toggle over-budget ends the session mid-flight",
          cutting._session_for(obj) is None)
    check("mesh restored on the mid-flight end", faces(obj) == 6,
          f"faces={faces(obj)}")
    check("blocker reports the shrunken budget",
          cutting.live_preview_blocker(obj) == "mesh too dense")
    sc.mirror_x = sc.mirror_y = sc.mirror_z = False
    cutting.update_live_preview(bpy.context)
    check("session resumes when mirrors go off",
          cutting._session_for(obj) is not None)
finally:
    preferences.preview_face_cap = orig_cap
    sc.mirror_x = sc.mirror_y = sc.mirror_z = False

check("cache invariants clean", not cutting.check_invariants(),
      str(cutting.check_invariants()))

print("\nMIRROR CUT:", "OK" if not failures else f"FAIL ({len(failures)}): {failures}")
sys.exit(1 if failures else 0)
