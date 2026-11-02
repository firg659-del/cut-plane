# SPDX-License-Identifier: GPL-3.0-or-later
"""The Edit Mode cut in its other modes equals native Bisect.

    blender --background --factory-startup --python-exit-code 1 \\
            --python tests/integration/edit_mode_modes.py

On the hard bench meshes:
  * transformed objects - rotated, non-uniformly and negatively scaled,
    moved - cut at a WORLD plane like native mesh.bisect;
  * Selected Only - native on the selection without a Clear; with a Clear
    the tool deletes face by face (native deletes verts and punches holes in
    unselected neighbours), so there: preview == Cut and nothing unselected
    is touched;
  * Mirror X / X+Y / Z (Origin anchor) - native Bisect at every mirrored
    plane in turn, and each mirrored plane is the true reflection in the
    object's own space;
  * several objects in one edit session - each cut like native.
Every case also checks preview == Execute Cut.
"""
import os
import sys
from unittest.mock import patch

import bmesh
import bpy
from mathutils import Euler, Matrix, Vector
from mathutils.kdtree import KDTree

ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

import cut_plane  # noqa: E402
from cut_plane import cutting  # noqa: E402
from cut_plane.cutting import preview as preview_module  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bench_meshes import (bowtie, holed_grid, multi_material, ngon_cylinder,  # noqa: E402
                          suzanne, template)

FAILURES = []
ctx = bpy.context
MESHES = (suzanne, ngon_cylinder, holed_grid, bowtie, multi_material)
PLANES = (
    ((0.05, -0.03, 0.11), (0.3, 0.2, 1.0)),
    ((0.2, 0.1, -0.1), (1.0, -0.4, 0.2)),
)
OPTIONS = ((False, False, False), (False, True, False), (True, False, False),
           (False, True, True))
TRANSFORM = Matrix.LocRotScale(Vector((0.4, -0.3, 0.2)), Euler((0.4, 0.2, 0.7)),
                               Vector((1.3, -0.8, 0.6)))


def check(name, condition, detail=''):
    print(f"[{'ok' if condition else 'FAIL'}] {name}" + (f' ({detail})' if detail else ''),
          flush=True)
    if not condition:
        FAILURES.append(name)


class Points:
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
            near = [i for _c, i, _d in tree.find_range(point, 1e-4) if i not in taken]
            if not near:
                return False
            taken.add(near[0])
        return True


def fingerprint(obj):
    bm = bmesh.from_edit_mesh(obj.data)
    conflicts = sum(1 for e in bm.edges if len(e.link_loops) == 2
                    and e.link_loops[0].vert == e.link_loops[1].vert)
    return (Points(v.co for v in bm.verts), len(bm.faces),
            tuple(sorted(len(f.verts) for f in bm.faces)), conflicts)


def differs(a, b):
    return [n for n, x, y in zip(('positions', 'faces', 'face sizes', 'winding'), a, b)
            if x != y]


def scene(objects, select=None):
    """objects: [(mesh_fn, matrix)]. All in one edit session, the first active."""
    cutting.restore_live_cut(ctx)
    if ctx.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    made = []
    for i, entry in enumerate(objects):
        mesh_fn, matrix = entry[:2]
        ob = bpy.data.objects.new(f'Bench{i}', template(mesh_fn).copy())
        ob.data.use_fake_user = False
        ctx.scene.collection.objects.link(ob)
        if len(entry) > 2:
            # A parent: an object's own transform cannot hold shear, only a
            # rotated child under a non-uniformly scaled parent has it.
            parent = bpy.data.objects.new(f'Parent{i}', None)
            ctx.scene.collection.objects.link(parent)
            parent.matrix_world = entry[2]
            ob.parent = parent
            ob.matrix_basis = matrix
        else:
            ob.matrix_world = matrix
        ob.select_set(True)
        made.append(ob)
    ctx.view_layer.objects.active = made[0]
    ctx.view_layer.update()
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_mode(type='FACE')
    bpy.ops.mesh.select_all(action='SELECT' if select is None else 'DESELECT')
    if select is not None:
        for ob in made:
            bm = bmesh.from_edit_mesh(ob.data)
            for f in bm.faces:
                if select(f.calc_center_median()):
                    f.select_set(True)
            bmesh.update_edit_mesh(ob.data)
    return made


def tool(inner, outer, fill, selected_only=False, mirror=(False, False, False)):
    sc = ctx.window_manager.cut_plane
    sc['cut_clear_inner'], sc['cut_clear_outer'], sc['cut_use_fill'] = inner, outer, fill
    sc['cut_selected_only'] = selected_only
    sc['cut_select_loop'] = False
    sc['mirror_x'], sc['mirror_y'], sc['mirror_z'] = mirror


def run(objects, plane, options, selected_only=False, mirror=(False, False, False),
        select=None, native_planes=None):
    """(native, preview, cut) fingerprints per object; native None when
    there is no native oracle for this case."""
    co, no = Vector(plane[0]), Vector(plane[1]).normalized()
    inner, outer, fill = options
    filled = fill and (inner or outer)
    made = scene(objects, select)
    native = None
    if native_planes is not False:
        tool(inner, outer, fill, selected_only, mirror)
        planes = native_planes(co, no) if native_planes else [(co, no)]
        if not selected_only:
            bpy.ops.mesh.select_all(action='SELECT')
        for p_co, p_no in planes:
            bpy.ops.mesh.bisect(plane_co=p_co, plane_no=p_no, use_fill=filled,
                                clear_inner=inner, clear_outer=outer)
            if not selected_only:
                bpy.ops.mesh.select_all(action='SELECT')
        native = [fingerprint(ob) for ob in made]
    made = scene(objects, select)
    tool(inner, outer, fill, selected_only, mirror)
    with patch.object(preview_module, 'get_plane_world', return_value=(co, no)):
        cutting.update_live_preview(ctx)
    shown = [fingerprint(ob) for ob in made]
    made = scene(objects, select)
    tool(inner, outer, fill, selected_only, mirror)
    status = bpy.ops.view3d.cut_plane_cut(
        'EXEC_DEFAULT', plane_co=co, plane_no=no, clear_inner=inner, clear_outer=outer,
        use_fill=fill, selected_only=selected_only, select_loop=False,
        mirror_x=mirror[0], mirror_y=mirror[1], mirror_z=mirror[2], plane_anchor='ORIGIN')
    cut = [fingerprint(ob) for ob in made] if status == {'FINISHED'} else shown
    return native, shown, cut


def problems_of(native, shown, cut):
    out = []
    for i, (s, c) in enumerate(zip(shown, cut)):
        if differs(s, c):
            out.append(f'object {i}: preview and Cut differ in {differs(s, c)}')
        if native is not None and differs(c, native[i]):
            out.append(f'object {i}: Cut differs from native in {differs(c, native[i])} '
                       f'({c[1]} vs {native[i][1]} faces)')
    return out


def section(name, cases):
    bad = []
    for label, case in cases:
        try:
            bad += [f'{label}: {p}' for p in case()]
        except Exception as error:
            bad.append(f'{label}: {type(error).__name__}: {error}'[:140])
    check(f'{name} ({len(cases)} cases)', not bad, '; '.join(bad[:3]) +
          (f' (+{len(bad) - 3} more)' if len(bad) > 3 else ''))


def tag(options):
    return ''.join(f for f, on in zip('IOF', options) if on) or '-'


def main():
    cut_plane.register()
    # Set once, up front: the setter's update callback starts a live preview,
    # which must never sit under a native Bisect oracle.
    ctx.window_manager.cut_plane.plane_anchor = 'ORIGIN'
    cutting.restore_live_cut(ctx)
    try:
        cases = [(f'{m.__name__} p{p} {tag(o)}',
                  lambda m=m, pl=pl, o=o: problems_of(*run([(m, TRANSFORM)], pl, o)))
                 for m in MESHES for p, pl in enumerate(PLANES) for o in OPTIONS]
        section('transformed object (rotated, scaled -x, moved) equals native', cases)

        def selected(c):
            return c.x > 0.05

        def selected_only(m, pl, o):
            native_planes = None if not (o[0] or o[1]) else False
            native, shown, cut = run([(m, Matrix())], pl, o, selected_only=True,
                                     select=selected, native_planes=native_planes)
            out = problems_of(native, shown, cut)
            if o[0] or o[1]:
                # Nothing unselected may be deleted. A border face may gain a
                # vertex where a selected edge was split (native does the
                # same), so each one's original corners must still be the
                # corners of a face.
                def corners(f):
                    return {tuple(round(c, 4) for c in v.co) for v in f.verts}
                made = scene([(m, Matrix())], selected)
                bm = bmesh.from_edit_mesh(made[0].data)
                before = [corners(f) for f in bm.faces if not f.select]
                tool(*o, selected_only=True)
                co, no = Vector(pl[0]), Vector(pl[1]).normalized()
                bpy.ops.view3d.cut_plane_cut('EXEC_DEFAULT', plane_co=co, plane_no=no,
                                             clear_inner=o[0], clear_outer=o[1], use_fill=o[2],
                                             selected_only=True, select_loop=False)
                bm = bmesh.from_edit_mesh(made[0].data)
                after = [corners(f) for f in bm.faces]
                gone = sum(1 for c in before if not any(c <= a for a in after))
                if gone:
                    out.append(f'{gone} unselected faces deleted by a Selected Only Clear')
            return out

        cases = [(f'{m.__name__} p{p} {tag(o)}',
                  lambda m=m, pl=pl, o=o: selected_only(m, pl, o))
                 for m in MESHES for p, pl in enumerate(PLANES) for o in OPTIONS[:3]]
        section('Selected Only', cases)

        def mirrored(m, pl, o, axes_flags, matrix=None, parent=None):
            axes = tuple(i for i, on in enumerate(axes_flags) if on)

            def planes(co, no):
                sc = ctx.window_manager.cut_plane
                return cutting._mirrored_planes_world(ctx, co, no, axes, sc)
            entry = (m, matrix or TRANSFORM) + ((parent,) if parent is not None else ())
            native, shown, cut = run([entry], pl, o, mirror=axes_flags,
                                     native_planes=planes)
            out = problems_of(native, shown, cut)
            # Each mirrored plane is the reflection in the object's own space.
            ob = ctx.edit_object
            inv = ob.matrix_world.inverted()
            co, no = Vector(pl[0]), Vector(pl[1]).normalized()
            l_co, l_no = inv @ co, (ob.matrix_world.to_3x3().transposed() @ no).normalized()
            got = [(inv @ c, (ob.matrix_world.to_3x3().transposed() @ n).normalized())
                   for c, n in cutting._mirrored_planes_world(
                       ctx, co, no, axes, ctx.window_manager.cut_plane)]
            want = [(l_co.copy(), l_no.copy())]
            for axis in axes:
                flip = []
                for c, n in want:
                    c2, n2 = c.copy(), n.copy()
                    c2[axis], n2[axis] = -c2[axis], -n2[axis]
                    flip.append((c2, n2))
                want += flip
            for wc, wn in want:
                if not any(abs((gc - wc).dot(wn)) < 1e-4 and abs(abs(gn.dot(wn)) - 1) < 1e-4
                           for gc, gn in got):
                    out.append(f'mirrored plane through {tuple(round(x, 3) for x in wc)} missing')
                    break
            return out

        cases = [(f'{m.__name__} {"".join("XYZ"[i] for i, on in enumerate(ax) if on)} {tag(o)}',
                  lambda m=m, o=o, ax=ax: mirrored(m, PLANES[0], o, ax))
                 for m in MESHES for o in (OPTIONS[1], OPTIONS[3])
                 for ax in ((True, False, False), (True, True, False), (False, False, True))]
        section('Mirror (native Bisect at every mirrored plane)', cases)

        # A child rotated under a non-uniformly scaled parent: its matrix is
        # SHEARED, and only a reflection in local space is a local mirror.
        scaled_parent = Matrix.Diagonal((2.0, 1.0, 1.0, 1.0))
        rotated_child = Matrix.Rotation(0.6, 4, 'Z')
        cases = [(f'{m.__name__} {"".join("XYZ"[i] for i, on in enumerate(ax) if on)}',
                  lambda m=m, ax=ax: mirrored(m, PLANES[0], OPTIONS[1], ax,
                                              rotated_child, scaled_parent))
                 for m in (suzanne, ngon_cylinder)
                 for ax in ((True, False, False), (False, True, False))]
        section('Mirror on a sheared object mirrors in the object own space', cases)

        second = Matrix.LocRotScale(Vector((2.2, 0.3, -0.1)), Euler((0.0, 0.5, 0.2)),
                                    Vector((0.7, 1.1, 0.9)))
        cases = [(f'{tag(o)} p{p}',
                  lambda pl=pl, o=o: problems_of(*run(
                      [(suzanne, TRANSFORM), (ngon_cylinder, second)], pl, o)))
                 for p, pl in enumerate(PLANES) for o in OPTIONS]
        section('two objects in one edit session, each like native', cases)
    finally:
        cutting.restore_live_cut(ctx)
        if ctx.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        cut_plane.unregister()
    if FAILURES:
        print(f'EDIT MODE MODES: {len(FAILURES)} FAILED', flush=True)
        os._exit(1)
    print('EDIT MODE MODES: OK', flush=True)
    os._exit(0)


main()
