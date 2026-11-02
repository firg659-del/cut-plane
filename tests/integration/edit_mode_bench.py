# SPDX-License-Identifier: GPL-3.0-or-later
"""The Edit Mode cut equals native Bisect on geometrically hard meshes.

    blender --background --factory-startup --python-exit-code 1 \
            --python tests/integration/edit_mode_bench.py

Nine awkward meshes - open boundaries, n-gons, a hole through the mesh, a
holed grid, concave faces, high-valence poles, loose parts, a non-manifold
edge and several materials - each carrying UVs, two materials and seams.
Three planes each (one lying exactly on vertices), every Clear / Fill
combination. For each, the live preview and Execute Cut must be identical, and equal
bpy.ops.mesh.bisect with the same settings - positions, faces and their
sizes, materials and UVs - and leaving the tool must restore the original.
Fill caps are the one exception: where a cap's neighbours disagree,
native's material pick follows element order, so caps follow a rule
instead (the most common neighbouring material, ties to the lowest slot).

Each mesh is built once and copied: Blender's own primitives do not emit
faces in the same order on every call, so a rebuilt mesh is not the same
mesh. Values are compared within a tolerance, not by rounding: modifiers
and bisect are not bit-repeatable across runs.
"""
import os
import sys
from unittest.mock import patch

import bmesh
import bpy
from mathutils import Vector
from mathutils.kdtree import KDTree

ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

import cut_plane  # noqa: E402
from cut_plane import cutting  # noqa: E402
from cut_plane.cutting import preview as preview_module  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bench_meshes import MESHES, template  # noqa: E402

FAILURES = []
ctx = bpy.context


def check(name, condition, detail=''):
    print(f"[{'ok' if condition else 'FAIL'}] {name}" + (f' ({detail})' if detail else ''),
          flush=True)
    if not condition:
        FAILURES.append(name)


# --- planes and options ----------------------------------------------------

PLANES = (
    ((0.05, -0.03, 0.11), (0.3, 0.2, 1.0)),
    ((0.0, 0.0, 0.0), (0.0, 0.0, 1.0)),          # exactly on vertices
    ((0.2, 0.1, -0.1), (1.0, -0.4, 0.2)),
)
# (clear_inner, clear_outer, fill)
OPTIONS = ((False, False, False), (False, True, False), (True, False, False),
           (False, True, True), (True, False, True), (False, False, True))


# --- comparison ------------------------------------------------------------

class Points:
    """An order-free point set compared within a tolerance."""
    TOLERANCE = 1e-4

    def __init__(self, points):
        self.points = [tuple(p) + (0.0,) * (3 - len(p)) for p in points]

    def __eq__(self, other):
        if len(self.points) != len(other.points):
            return False
        if not self.points:
            return True
        tree = KDTree(len(other.points))
        for index, point in enumerate(other.points):
            tree.insert(point, index)
        tree.balance()
        taken = set()
        for point in self.points:
            near = [i for _co, i, _d in tree.find_range(point, self.TOLERANCE) if i not in taken]
            if not near:
                return False
            taken.add(near[0])
        return True


FIELDS = ('positions', 'faces', 'face sizes', 'materials', 'uvs', 'winding')


def _on_plane(face, co, no):
    return all(abs((v.co - co).dot(no)) < 1e-4 for v in face.verts)


def fingerprint(obj, plane=None):
    """Order-free result. With a plane, caps (faces lying in it) are left
    out of materials and UVs: when a cap's neighbours disagree, native
    Bisect's pick follows element order, Cut Plane's follows a rule."""
    bm = bmesh.from_edit_mesh(obj.data)
    uv = bm.loops.layers.uv.active
    faces = [f for f in bm.faces if plane is None or not _on_plane(f, *plane)]
    uvs = [loop[uv].uv for f in faces for loop in f.loops] if uv else []
    # Edges whose two faces run the same way: winding that disagrees.
    conflicts = 0
    for e in bm.edges:
        if len(e.link_loops) == 2:
            a, b = e.link_loops
            conflicts += a.vert == b.vert
    return (Points(v.co for v in bm.verts), len(bm.faces),
            tuple(sorted(len(f.verts) for f in bm.faces)),
            tuple(sorted(f.material_index for f in faces)), Points(uvs), conflicts)


def caps(obj, plane):
    """Fill caps by what can be seen: their vertices, total area and
    materials. Not their triangles: a cap with a hole (a tube cut through
    its vertex ring) is triangulated by Blender's fill in an order the
    add-on cannot control, so the diagonals - and the UVs interpolated
    across them - may differ while the cap looks the same."""
    bm = bmesh.from_edit_mesh(obj.data)
    faces = [f for f in bm.faces if _on_plane(f, *plane)]
    return (Points({v.co.copy().freeze() for f in faces for v in f.verts}),
            round(sum(f.calc_area() for f in faces), 5),
            tuple(sorted({f.material_index for f in faces})))


def cap_rule_breaks(obj, plane):
    """Caps whose material is not the most common among their neighbours
    (ties: the lowest slot)."""
    bm = bmesh.from_edit_mesh(obj.data)
    breaks = 0
    for face in bm.faces:
        if not _on_plane(face, *plane):
            continue
        counts = {}
        neighbours = {other for edge in face.edges for other in edge.link_faces
                      if other is not face and not _on_plane(other, *plane)}
        for other in neighbours:
            counts[other.material_index] = counts.get(other.material_index, 0) + 1
        if counts and face.material_index != min(counts, key=lambda m: (-counts[m], m)):
            breaks += 1
    return breaks


def differs(a, b):
    return [name for name, x, y in zip(FIELDS, a, b) if x != y]


# --- one comparison --------------------------------------------------------

def spawn(mesh_fn):
    cutting.restore_live_cut(ctx)
    if ctx.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    ob = bpy.data.objects.new('Bench', template(mesh_fn).copy())
    ob.data.use_fake_user = False
    ctx.scene.collection.objects.link(ob)
    ctx.view_layer.objects.active = ob
    ob.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    return ob


def tool_options(inner, outer, fill):
    sc = ctx.window_manager.cut_plane
    sc['cut_clear_inner'], sc['cut_clear_outer'], sc['cut_use_fill'] = inner, outer, fill
    sc['cut_selected_only'] = False
    sc['cut_select_loop'] = False
    sc['mirror_x'] = sc['mirror_y'] = sc['mirror_z'] = False


def compare(mesh_fn, plane, options):
    """Problems for one mesh, plane and option set (empty = parity)."""
    co, no = Vector(plane[0]), Vector(plane[1]).normalized()
    inner, outer, fill = options
    problems = []

    plane_local = (co, no)
    ob = spawn(mesh_fn)
    original = fingerprint(ob)
    # Fill needs a Clear (cutting.fill_applies): with both sides kept the
    # oracle is native Bisect WITHOUT its internal cap face.
    filled = fill and (inner or outer)
    bpy.ops.mesh.bisect(plane_co=co, plane_no=no, use_fill=filled,
                        clear_inner=inner, clear_outer=outer)
    native = fingerprint(ob, plane_local)

    ob = spawn(mesh_fn)
    tool_options(inner, outer, fill)
    with patch.object(preview_module, 'get_plane_world', return_value=(co, no)):
        cutting.update_live_preview(ctx)
    shown, shown_caps = fingerprint(ob, plane_local), caps(ob, plane_local)
    cutting.restore_live_cut(ctx)
    restored = fingerprint(ob)

    ob = spawn(mesh_fn)
    tool_options(inner, outer, fill)
    status = bpy.ops.view3d.cut_plane_cut('EXEC_DEFAULT', plane_co=co, plane_no=no,
                                          clear_inner=inner, clear_outer=outer, use_fill=fill)
    committed = fingerprint(ob, plane_local) if status == {'FINISHED'} else native
    committed_caps = caps(ob, plane_local) if status == {'FINISHED'} else shown_caps
    breaks = cap_rule_breaks(ob, plane_local) if filled and status == {'FINISHED'} else 0

    if differs(shown, committed):
        problems.append(f'preview and Cut differ in {differs(shown, committed)}')
    if shown_caps != committed_caps:
        problems.append('preview and Cut caps differ (vertices, area or materials)')
    for label, got in (('preview', shown), ('cut', committed)):
        bad = differs(got, native)
        if bad:
            problems.append(f'{label} differs from native in {bad} '
                            f'({got[1]} vs {native[1]} faces)')
    if breaks:
        problems.append(f'{breaks} caps break the material rule')
    if differs(restored, original):
        problems.append(f'leaving the tool did not restore {differs(restored, original)}')
    return problems


def main():
    cut_plane.register()
    try:
        for mesh_fn in MESHES:
            bad = []
            for p, plane in enumerate(PLANES):
                for options in OPTIONS:
                    tag = ''.join(f for f, on in zip(('I', 'O', 'F'), options) if on) or '-'
                    try:
                        problems = compare(mesh_fn, plane, options)
                    except Exception as error:
                        problems = [f'{type(error).__name__}: {error}'[:120]]
                    bad += [f'plane {p} {tag}: {x}' for x in problems]
            check(f'{mesh_fn.__name__}: preview and Cut equal native Bisect '
                  f'({len(PLANES) * len(OPTIONS)} cases)', not bad, '; '.join(bad[:4]))
    finally:
        cutting.restore_live_cut(ctx)
        if ctx.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        cut_plane.unregister()
    if FAILURES:
        print(f'EDIT MODE BENCH: {len(FAILURES)} FAILED', flush=True)
        os._exit(1)
    print('EDIT MODE BENCH: OK', flush=True)
    os._exit(0)


main()
