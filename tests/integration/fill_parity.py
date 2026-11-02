# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless fill-parity test: the addon's Fill must match NATIVE Bisect 1:1.

Runs inside Blender (needs real bmesh + the native operator + the addon):

    blender --background --factory-startup --python tests/integration/fill_parity.py

Contract under test: Execute Cut with Fill caps the cut cross-section
exactly like bpy.ops.mesh.bisect(use_fill=True), on canonical manifold AND
non-manifold geometry, with and without Clear Outer. History: the addon used
edgenet_fill, which silently skipped interior membranes and non-manifold
cross-sections - the 2026-07-05 parity fix.

THE ADDON SIDE DRIVES THE REAL OPERATOR (bpy.ops.view3d.cut_plane_cut).
The previous version inlined the bisect+fill calls it EXPECTED the addon to
make - which tested Blender against Blender: mutating BOTH production call
sites back to edgenet_fill (the exact regression this suite exists to
prevent) left it fully green, along with the other 16 suites. A parity suite
that never calls the addon proves nothing about the addon.

Exit code 0 on full parity, 1 with a per-case report otherwise.
"""
import os
import sys

import bmesh
import bpy
from mathutils import Matrix, Vector

ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

import cut_plane  # noqa: E402
from cut_plane import cutting  # noqa: E402

cut_plane.register()

EPS = 1e-4
PLANE_CO = Vector((0.0, 0.0, 0.0))
PLANE_NO = Vector((0.0, 0.0, 1.0))


# --- geometry cases (built into a fresh bmesh) ------------------------------

def case_cube(bm):
    bmesh.ops.create_cube(bm, size=2.0)


def case_open_tube(bm):
    bmesh.ops.create_cone(bm, cap_ends=False, segments=16,
                          radius1=1.0, radius2=1.0, depth=2.0)


def case_flat_sheet(bm):
    # vertical sheet crossing z=0: OPEN cross-section chain - unfillable by
    # native too (the parity answer here is "both produce 0")
    bmesh.ops.create_grid(bm, x_segments=4, y_segments=4, size=1.0,
                          matrix=Matrix.Rotation(1.5707963, 4, 'X'))


def case_open_box(bm):
    bmesh.ops.create_cube(bm, size=2.0)
    top = [f for f in bm.faces if all(v.co.z > 0.9 for v in f.verts)]
    bmesh.ops.delete(bm, geom=top, context='FACES')


def case_interior_wall(bm):
    # two cubes sharing a kept face = non-manifold interior wall; the cut
    # cross-section is a loop with a chord (edgenet_fill capped 1 of 2 regions)
    bmesh.ops.create_cube(bm, size=2.0, matrix=Matrix.Translation((-1, 0, 0)))
    bmesh.ops.create_cube(bm, size=2.0, matrix=Matrix.Translation((1, 0, 0)))
    bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=1e-5)


def case_edge_joined_cubes(bm):
    # two cubes sharing one edge (4 faces meet at it)
    bmesh.ops.create_cube(bm, size=2.0, matrix=Matrix.Translation((-1, -1, 0)))
    bmesh.ops.create_cube(bm, size=2.0, matrix=Matrix.Translation((1, 1, 0)))
    bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=1e-5)


def case_wire_edges(bm):
    bmesh.ops.create_cube(bm, size=2.0)
    v1 = bm.verts.new((3, 0, -1))
    v2 = bm.verts.new((3, 0, 1))
    v3 = bm.verts.new((3, 1, 1))
    bm.edges.new((v1, v2))
    bm.edges.new((v2, v3))


CASES = [
    ("manifold cube", case_cube),
    ("open tube", case_open_tube),
    ("flat sheet (open chain)", case_flat_sheet),
    ("open box (no top)", case_open_box),
    ("interior wall (2 cubes)", case_interior_wall),
    ("edge-joined cubes", case_edge_joined_cubes),
    ("cube + wire edges", case_wire_edges),
]


def _cap_face_count(faces):
    # No case starts with a face lying IN z=0, so every all-verts-at-z=0
    # face after the cut is a fill product.
    return sum(1 for f in faces if all(abs(v.co.z) < EPS for v in f.verts))


def _spawn(build, name):
    """A fresh, SOLE-selected object for one case - a leftover selected
    object would ride into the edit session and be cut too (both operators
    apply to every edit mesh)."""
    if bpy.context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.select_all(action='DESELECT')
    bm = bmesh.new()
    build(bm)
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    return obj, mesh


def _count_and_drop(obj, mesh):
    bpy.ops.object.mode_set(mode='OBJECT')
    cutting.restore_live_cut(bpy.context)  # drop the committed-cut session
    bm = bmesh.new()
    bm.from_mesh(mesh)
    n = _cap_face_count(bm.faces)
    bm.free()
    bpy.data.objects.remove(obj, do_unlink=True)
    return n


def addon_fill_caps(build, clear_outer, name):
    """Cap faces produced by the REAL Execute Cut operator."""
    obj, mesh = _spawn(build, name)
    bpy.ops.view3d.cut_plane_cut(
        'EXEC_DEFAULT', plane_co=PLANE_CO, plane_no=PLANE_NO,
        use_fill=True, clear_inner=False, clear_outer=clear_outer)
    return _count_and_drop(obj, mesh)


def native_fill_caps(build, clear_outer, name):
    """Cap faces in the result of NATIVE mesh.bisect with use_fill."""
    obj, mesh = _spawn(build, name)
    bpy.ops.mesh.bisect(plane_co=PLANE_CO, plane_no=PLANE_NO, use_fill=True,
                        clear_inner=False, clear_outer=clear_outer)
    return _count_and_drop(obj, mesh)


def main():
    failures = []
    for clear_outer in (False, True):
        for name, build in CASES:
            uniq = f"{name.replace(' ', '_')}_{clear_outer}"
            ours = addon_fill_caps(build, clear_outer, "a_" + uniq)
            # With both sides kept native lays a cap INSIDE the solid; Cut
            # Plane fills only with a Clear (cutting.fill_applies): 0 caps.
            native = native_fill_caps(build, clear_outer, "n_" + uniq) if clear_outer else 0
            tag = "clear" if clear_outer else "keep "
            status = "ok" if ours == native else "FAIL"
            print(f"[{tag}] {name:27} addon={ours} native={native}  {status}")
            if ours != native:
                failures.append((tag, name, ours, native))
    if failures:
        print(f"\nFILL PARITY: {len(failures)} case(s) diverge from native")
        sys.stdout.flush()
        os._exit(1)
    print("\nFILL PARITY: OK (addon fill == native Bisect fill in all cases)")
    sys.stdout.flush()
    os._exit(0)  # skip Blender's flaky GPU teardown


main()
