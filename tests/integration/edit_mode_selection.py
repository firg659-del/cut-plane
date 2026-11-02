# SPDX-License-Identifier: GPL-3.0-or-later
"""The selection survives a cut exactly as the user made it.

    blender --background --factory-startup --python-exit-code 1 \\
            --python tests/integration/edit_mode_selection.py

On the hard bench meshes, three planes (one lying exactly on vertices),
Clear / Fill options, Face and Vertex select mode, Select Cut Loop on and
off. The contract:

  1. preview and Execute Cut select exactly the same faces, edges, verts;
  2. every kept piece of a face is selected exactly when its original was;
  3. a Fill cap is selected only when every face around it is;
  4. in Face select mode nothing is selected outside the selected faces
     (plus the cut loop when Select Cut Loop is on);
  5. leaving the tool restores the original selection exactly;
  6. a click during the preview, a plane move and leaving the tool leave
     exactly the original selection plus the clicked face.
"""
import os
import sys
from unittest.mock import patch

import bmesh
import bpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree
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
PLANES = (
    ((0.05, -0.03, 0.11), (0.3, 0.2, 1.0)),
    ((0.0, 0.0, 0.0), (0.0, 0.0, 1.0)),          # exactly on vertices
    ((0.2, 0.1, -0.1), (1.0, -0.4, 0.2)),
)
OPTIONS = ((False, False, False), (False, True, False), (True, False, False),
           (False, True, True))
TOL = 1e-4
verts_sel = []


def check(name, condition, detail=''):
    print(f"[{'ok' if condition else 'FAIL'}] {name}" + (f' ({detail})' if detail else ''),
          flush=True)
    if not condition:
        FAILURES.append(name)


class Points:
    """An order-free point set compared within a tolerance."""

    def __init__(self, points):
        self.points = [tuple(p) for p in points]

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
            near = [i for _co, i, _d in tree.find_range(point, TOL) if i not in taken]
            if not near:
                return False
            taken.add(near[0])
        return True

    def __len__(self):
        return len(self.points)


def selection(obj, plane=None):
    """Selected faces (caps as their selected AREA: Blender may triangulate
    a cap with a hole differently each time), edges and verts."""
    bm = bmesh.from_edit_mesh(obj.data)
    faces = [f for f in bm.faces if f.select]
    if plane is not None:
        caps = [f for f in faces if on_plane(f, *plane)]
        faces = [f for f in faces if not on_plane(f, *plane)]
        faces.append(None)
        cap_area = round(sum(f.calc_area() for f in caps), 4)
    centers = [f.calc_center_median() if f is not None else Vector((cap_area, 9e9, 0))
               for f in faces] if plane is not None else [f.calc_center_median() for f in faces]
    edges = [e for e in bm.edges if e.select]
    if plane is not None:
        # Edges inside a cap follow its triangulation: count those.
        flat = [e for e in edges if on_plane(e, *plane)]
        edges = [e for e in edges if not on_plane(e, *plane)]
    mids = [(e.verts[0].co + e.verts[1].co) / 2 for e in edges]
    if plane is not None:
        mids.append(Vector((len(flat), 9e9, 1)))
    return (Points(centers),
            Points(mids),
            Points(v.co for v in bm.verts if v.select))


def pattern(index, center):
    """Which faces the user selected: half the mesh, or a checker."""
    if index == 0:
        return center.x > 0.05
    return (int((center.x + 5) * 3) + int((center.y + 5) * 3) + int((center.z + 5) * 3)) % 2 == 0


def spawn(mesh_fn, select_index, mode):
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
    bpy.ops.mesh.select_mode(type=mode)
    bpy.ops.mesh.select_all(action='DESELECT')
    bm = bmesh.from_edit_mesh(ob.data)
    for f in bm.faces:
        if pattern(select_index, f.calc_center_median()):
            f.select_set(True)
    bm.select_flush_mode()
    bmesh.update_edit_mesh(ob.data)
    return ob


def tool_options(inner, outer, fill, loop):
    sc = ctx.window_manager.cut_plane
    sc['cut_clear_inner'], sc['cut_clear_outer'], sc['cut_use_fill'] = inner, outer, fill
    sc['cut_selected_only'] = False
    sc['cut_select_loop'] = loop
    sc['mirror_x'] = sc['mirror_y'] = sc['mirror_z'] = False


def on_plane(face, co, no):
    return all(abs((v.co - co).dot(no)) < TOL for v in face.verts)


def contract_breaks(obj, original, co, no, mode, loop):
    """Contract items 2-4 on the mesh as it is now; `original` is the
    uncut mesh's (BVH, face selection flags)."""
    bvh, selected, verts = original
    bm = bmesh.from_edit_mesh(obj.data)
    breaks = []
    pieces = caps = stray = lost = 0
    for f in bm.faces:
        if on_plane(f, co, no):
            around = {o for e in f.edges for o in e.link_faces
                      if o is not f and not on_plane(o, co, no)}
            # Face mode: selected when every neighbour is; Vertex mode: when
            # every rim vertex is (the verts rule there).
            want = (all(o.select for o in around) if mode == 'FACE'
                    else all(v.select for v in f.verts))
            if around and f.select != want:
                caps += 1
            continue
        if mode != 'FACE':
            continue    # in Vertex mode a face's selection follows its verts
        hit = bvh.find_nearest(f.calc_center_median())
        if hit[2] is not None and hit[3] < 1e-3 and f.select != selected[hit[2]]:
            pieces += 1
    if mode == 'VERT':
        # Every original vertex that survives keeps its selection.
        for v in bm.verts:
            if abs((v.co - co).dot(no)) <= 2e-4:
                continue    # on the cut: new, or an original the plane touches
            near = verts.find_range(v.co, TOL)
            if len(near) != 1:
                continue    # no original here, or coincident ones (Suzanne)
            _co, i, d = near[0]
            if v.select != verts_sel[i]:
                lost += 1
                where = (tuple(round(c, 4) for c in v.co), round((v.co - co).dot(no), 5),
                         verts_sel[i], len(v.link_edges))
    if lost:
        breaks.append(f'{lost} original verts changed selection (e.g. {where})')
    if mode == 'FACE':
        in_faces = {e for f in bm.faces if f.select for e in f.edges}
        unselected = sum(1 for e in in_faces if not e.select)
        if unselected:
            breaks.append(f'{unselected} edges of selected faces left unselected')
        for e in bm.edges:
            if e.select and e not in in_faces:
                cut_loop = abs((e.verts[0].co - co).dot(no)) < TOL and \
                    abs((e.verts[1].co - co).dot(no)) < TOL
                if not (loop and cut_loop):
                    stray += 1
    if pieces:
        breaks.append(f'{pieces} kept pieces disagree with their original face')
    if caps:
        breaks.append(f'{caps} caps disagree with the faces around them')
    if stray:
        breaks.append(f'{stray} edges selected outside the selected faces')
    return breaks


def compare(mesh_fn, plane, options, select_index, mode, loop):
    co, no = Vector(plane[0]), Vector(plane[1]).normalized()
    inner, outer, fill = options
    problems = []

    ob = spawn(mesh_fn, select_index, mode)
    original_sel = selection(ob)
    me = ob.data
    bm = bmesh.from_edit_mesh(me)
    bvh = BVHTree.FromBMesh(bm)
    kd = KDTree(len(bm.verts))
    for v in bm.verts:
        kd.insert(v.co, v.index)
    kd.balance()
    global verts_sel
    verts_sel = [v.select for v in bm.verts]
    original = (bvh, [f.select for f in bm.faces], kd)
    tool_options(inner, outer, fill, loop)
    with patch.object(preview_module, 'get_plane_world', return_value=(co, no)):
        cutting.update_live_preview(ctx)
    shown = selection(ob, (co, no))
    problems += ['preview: ' + b for b in contract_breaks(ob, original, co, no, mode, loop)]
    cutting.restore_live_cut(ctx)
    if selection(ob) != original_sel:
        problems.append('leaving the tool did not restore the selection')

    ob = spawn(mesh_fn, select_index, mode)
    tool_options(inner, outer, fill, loop)
    status = bpy.ops.view3d.cut_plane_cut('EXEC_DEFAULT', plane_co=co, plane_no=no,
                                          clear_inner=inner, clear_outer=outer, use_fill=fill,
                                          select_loop=loop)
    if status == {'FINISHED'}:
        committed = selection(ob, (co, no))
        problems += ['cut: ' + b for b in contract_breaks(ob, original, co, no, mode, loop)]
        names = ('faces', 'edges', 'verts')
        bad = [n for n, a, b in zip(names, shown, committed) if a != b]
        if bad:
            problems.append(f'preview and Cut select different {bad} '
                            f'({[len(x) for x in shown]} vs {[len(x) for x in committed]})')
    return problems


def click_during_preview(mesh_fn):
    """Item 6: select one more kept face mid-preview, move the plane, leave."""
    co, no = Vector((0.05, -0.03, 0.11)), Vector((0.3, 0.2, 1.0)).normalized()
    ob = spawn(mesh_fn, 0, 'FACE')
    bm = bmesh.from_edit_mesh(ob.data)
    before = {tuple(round(c, 4) for c in f.calc_center_median()) for f in bm.faces if f.select}
    tool_options(False, True, False, True)
    plane = [co, no]
    with patch.object(preview_module, 'get_plane_world', side_effect=lambda c: tuple(plane)):
        cutting.update_live_preview(ctx)
        bm = bmesh.from_edit_mesh(ob.data)
        # an unselected face well below the plane, kept whole by the cut
        candidates = [f for f in bm.faces if not f.select
                      and max((v.co - co).dot(no) for v in f.verts) < -0.05]
        if not candidates:
            return []
        clicked = min(candidates, key=lambda f: f.calc_center_median().x)
        target = tuple(round(c, 4) for c in clicked.calc_center_median())
        clicked.select_set(True)
        bmesh.update_edit_mesh(ob.data)
        plane[0] = co + no * 0.03
        cutting.update_live_preview(ctx)
    cutting.restore_live_cut(ctx)
    bm = bmesh.from_edit_mesh(ob.data)
    after = {tuple(round(c, 4) for c in f.calc_center_median()) for f in bm.faces if f.select}
    in_faces = {v for f in bm.faces if f.select for v in f.verts}
    stray = [v for v in bm.verts if v.select and v not in in_faces]
    problems = []
    if after != before | {target}:
        missing = sorted((before | {target}) - after)
        where = [round((Vector(c) - co).dot(no), 3) for c in missing[:5]]
        problems.append(f'faces after leaving: {len(after)}, expected {len(before) + 1} '
                        f'(missing {len(missing)} at plane distances {where}, '
                        f'extra {len(after - before - {target})})')
    if stray:
        problems.append(f'{len(stray)} stray verts selected after leaving')
    return problems


def main():
    cut_plane.register()
    try:
        for mesh_fn in MESHES:
            bad = []
            cases = 0
            for p, plane in enumerate(PLANES):
                for options in OPTIONS:
                    for select_index, mode in ((0, 'FACE'), (1, 'VERT')):
                        for loop in (False, True):
                            cases += 1
                            tag = (''.join(f for f, on in zip('IOF', options) if on) or '-') + \
                                f' {mode[0]}{"L" if loop else ""}'
                            try:
                                problems = compare(mesh_fn, plane, options, select_index, mode, loop)
                            except Exception as error:
                                problems = [f'{type(error).__name__}: {error}'[:120]]
                            bad += [f'plane {p} {tag}: {x}' for x in problems]
            check(f'{mesh_fn.__name__}: selection contract ({cases} cases)', not bad,
                  '; '.join(bad[:3]) + (f' (+{len(bad) - 3} more)' if len(bad) > 3 else ''))
            try:
                problems = click_during_preview(mesh_fn)
            except Exception as error:
                problems = [f'{type(error).__name__}: {error}'[:120]]
            check(f'{mesh_fn.__name__}: a click during the preview is kept, nothing else',
                  not problems, '; '.join(problems))
    finally:
        cutting.restore_live_cut(ctx)
        if ctx.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        cut_plane.unregister()
    if FAILURES:
        print(f'EDIT MODE SELECTION: {len(FAILURES)} FAILED', flush=True)
        os._exit(1)
    print('EDIT MODE SELECTION: OK', flush=True)
    os._exit(0)


main()
