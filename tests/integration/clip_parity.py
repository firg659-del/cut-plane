# SPDX-License-Identifier: GPL-3.0-or-later
"""The preview's fast cut (cutting/clip.py) leaves the mesh bisect_plane does.

    blender --background --factory-startup --python-exit-code 1 \\
            --python tests/integration/clip_parity.py

Every case cuts one snapshot both ways - clip_cut, and rebuild +
bmesh.ops.bisect_plane over the whole mesh (the exact preview path) - and
compares the two meshes element by element, keyed by position: every
attribute value (UVs, colours, creases, seams, materials, selection),
the winding of every face, and the 'geom_cut' output. Cases that only the
exact path can produce must be refused BEFORE the bmesh is touched.
"""
import math
import os
import sys
from unittest.mock import patch

import bmesh
import bpy
from mathutils import Matrix, Vector

ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

from cut_plane.cutting import clip  # noqa: E402
from cut_plane.cutting.snapshot import replace_bmesh  # noqa: E402
from cut_plane.lib.constants import BISECT_DIST  # noqa: E402

FAILURES = []
ctx = bpy.context


def check(name, condition, detail=''):
    print(f"[{'ok' if condition else 'FAIL'}] {name}" + (f' ({detail})' if detail else ''),
          flush=True)
    if not condition:
        FAILURES.append(name)


class Cache:
    clip_scratch = None


def vkey(co):
    return tuple(round(c, 4) + 0.0 for c in co)


def describe(me):
    """{attribute: {element key: value}} plus the element key sets."""
    vk = [vkey(v.co) for v in me.vertices]
    ek = [frozenset((vk[a], vk[b])) for a, b in (e.vertices for e in me.edges)]
    fk, ck = [], []
    for p in me.polygons:
        ring = [vk[i] for i in p.vertices]
        r = ring.index(min(ring))
        fk.append(tuple(ring[r:] + ring[:r]))       # winding kept
        ck += [(fk[-1], vk[i]) for i in p.vertices]
    keys = {'POINT': vk, 'EDGE': ek, 'FACE': fk, 'CORNER': ck}
    out = {'#verts': sorted(vk), '#edges': sorted(map(sorted, ek)), '#faces': sorted(fk)}
    for a in me.attributes:
        if a.name in ('position', '.edge_verts', '.corner_vert', '.corner_edge'):
            continue
        if a.data_type in ('FLOAT2', 'FLOAT_VECTOR'):
            vals = [tuple(round(x, 4) + 0.0 for x in d.vector) for d in a.data]
        elif a.data_type == 'FLOAT_COLOR':
            vals = [tuple(round(x, 4) + 0.0 for x in d.color) for d in a.data]
        elif a.data_type == 'FLOAT':
            vals = [round(d.value, 4) + 0.0 for d in a.data]
        else:
            vals = [d.value if not hasattr(d.value, '__len__') else tuple(d.value)
                    for d in a.data]
        out[a.name] = dict(zip(keys[a.domain], vals))
    return out


def as_mesh(bm):
    me = bpy.data.meshes.new('cmp')
    bm.to_mesh(me)
    return me


def compare(name, snap, obj, co, no, ci, co_, mode=frozenset({'VERT'})):
    co, no = Vector(co), Vector(no).normalized()
    exact = bmesh.new()
    exact.select_mode = set(mode)
    replace_bmesh(exact, snap)               # the exact preview's rebuild
    geom = exact.verts[:] + exact.edges[:] + exact.faces[:]
    res = bmesh.ops.bisect_plane(exact, geom=geom, dist=BISECT_DIST, plane_co=co,
                                 plane_no=no, clear_inner=ci, clear_outer=co_)
    want_cut = sorted(repr(vkey(g.co)) if isinstance(g, bmesh.types.BMVert)
                      else repr(sorted(vkey(v.co) for v in g.verts))
                      for g in res['geom_cut'] if g.is_valid)

    arr = clip.clip_arrays(obj, snap)
    a = describe(as_mesh(exact))
    runs = [('', lambda cache, bm: clip.clip_cut(cache, bm, snap, arr, co, no,
                                                 BISECT_DIST, ci, co_))]
    if not (ci and co_):
        # The drag-time path: the snapshot sorted along the normal (the
        # negated one for Clear Inner), the plane one offset along it.
        frame = clip.clip_frame(snap, arr, -no if ci else no)
        runs.append((' [sorted frame]', lambda cache, bm: clip.frame_cut(
            cache, bm, arr, frame, co, BISECT_DIST, ci or co_)))
    for tag, run in runs:
        fast = bmesh.new()
        fast.select_mode = set(mode)
        cache = Cache()
        cut = run(cache, fast)
        if cut is None:
            check(f'{name}{tag}: the fast path takes it', False, 'refused')
            continue
        got_cut = sorted(repr(vkey(g.co)) if isinstance(g, bmesh.types.BMVert)
                         else repr(sorted(vkey(v.co) for v in g.verts))
                         for g in cut if g.is_valid)
        b = describe(as_mesh(fast))
        diffs = [k for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
        if diffs:
            k = diffs[0]
            if isinstance(a.get(k), dict) and isinstance(b.get(k), dict):
                bad = [e for e in set(a[k]) | set(b[k]) if a[k].get(e) != b[k].get(e)][:2]
                detail = f'{k}: ' + '; '.join(f'{e}: {a[k].get(e)} vs {b[k].get(e)}'
                                              for e in bad)
            else:
                detail = f'{k}: {str(a.get(k))[:120]} vs {str(b.get(k))[:120]}'
            check(f'{name}{tag}: same mesh as bisect_plane', False, detail)
        else:
            check(f'{name}{tag}: same mesh as bisect_plane', True,
                  f'{len(fast.verts)}v {len(fast.faces)}f')
        check(f'{name}{tag}: same geom_cut', want_cut == got_cut,
              f'{len(want_cut)} vs {len(got_cut)}')
        fast.free()
        clip.free_scratch(cache)
    exact.free()


def object_from(build):
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    build()
    obj = ctx.active_object
    return obj, obj.data.copy()


def monkey():
    bpy.ops.mesh.primitive_monkey_add()
    m = ctx.active_object.modifiers.new('S', 'SUBSURF')
    m.levels = 2
    bpy.ops.object.modifier_apply(modifier='S')
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='DESELECT')
    bpy.ops.mesh.select_random(ratio=0.4, seed=3)
    bpy.ops.object.mode_set(mode='OBJECT')


def rich_cube():
    """Every kind of layer a cut must carry: two UV maps (the second
    active), a colour, creases, seams, sharp edges, materials, smoothing,
    a partial selection - and on-plane vertex rows at z = 0, x = 0."""
    bpy.ops.mesh.primitive_cube_add(size=2)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.subdivide(number_cuts=3)
    bpy.ops.object.mode_set(mode='OBJECT')
    me = ctx.active_object.data
    me.uv_layers.new(name='Second')
    for i, d in enumerate(me.uv_layers['Second'].data):
        d.uv = (math.sin(i), math.cos(i * 0.7))
    me.uv_layers.active = me.uv_layers['Second']
    col = me.color_attributes.new('Tint', 'FLOAT_COLOR', 'CORNER')
    for i, d in enumerate(col.data):
        d.color = (i % 7 / 7, i % 3 / 3, 0.5, 1.0)
    me.color_attributes.active_color = col
    crease = me.attributes.new('crease_edge', 'FLOAT', 'EDGE')
    for i, d in enumerate(crease.data):
        d.value = (i % 5) / 5
    for i, e in enumerate(me.edges):
        e.use_seam = i % 4 == 0
        e.use_edge_sharp = i % 6 == 0
    ctx.active_object.data.materials.append(bpy.data.materials.new('A'))
    ctx.active_object.data.materials.append(bpy.data.materials.new('B'))
    for i, p in enumerate(me.polygons):
        p.material_index = i % 2
        p.use_smooth = i % 3 == 0
    # A selection as Edit Mode leaves it (flushed), like every snapshot's.
    bm = bmesh.new()
    bm.from_mesh(me)
    for f in bm.faces:
        f.select_set(f.index % 5 < 2)
    bm.to_mesh(me)
    bm.free()
    me.attributes.new('weight', 'FLOAT', 'POINT')
    me.attributes.new('flag', 'INT8', 'FACE')
    me.attributes.new('ids', 'INT', 'EDGE')
    for i, d in enumerate(me.attributes['ids'].data):
        d.value = i * 7


def loose_bits():
    """An upright grid plus loose verts and wire edges on both sides of
    the plane, one wire edge across it and one lying in it."""
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=6, y_subdivisions=6, size=2)
    bm = bmesh.new()
    me = ctx.active_object.data
    me.transform(Matrix.Rotation(math.pi / 2, 4, 'X'))
    bm.from_mesh(me)
    pts = [(0.3, 0.0, 0.5), (0.3, 0.0, -0.5), (-0.4, 0.2, 0.7), (-0.4, 0.2, -0.7),
           (0.6, 0.1, 0.0), (0.8, 0.1, 0.0)]
    vs = [bm.verts.new(p) for p in pts]
    bm.verts.new((0.0, 0.5, 0.9))                   # loose, cleared by Outer
    bm.verts.new((0.0, 0.5, -0.9))                  # loose, cleared by Inner
    bm.edges.new((vs[0], vs[1]))                    # wire across the plane
    bm.edges.new((vs[2], vs[3]))
    bm.edges.new((vs[4], vs[5]))                    # wire IN the plane
    bm.to_mesh(me)
    bm.free()


def ngon_disc():
    """Convex n-gon caps (a cylinder) and triangles."""
    bpy.ops.mesh.primitive_cylinder_add(vertices=24)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.quads_convert_to_tris()
    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.mesh.primitive_cylinder_add(vertices=12, location=(3, 0, 0))
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.join()


def l_shape():
    """One concave n-gon the plane crosses twice."""
    bm = bmesh.new()
    pts = [(0, 0, 0), (3, 0, 0), (3, 1, 0), (1, 1, 0), (1, 3, 0), (0, 3, 0)]
    bm.faces.new([bm.verts.new(p) for p in pts])
    me = bpy.data.meshes.new('L')
    bm.to_mesh(me)
    bm.free()
    obj = bpy.data.objects.new('L', me)
    ctx.collection.objects.link(obj)
    ctx.view_layer.objects.active = obj


def caps(obj):
    return sorted(len(f.verts) for f in bmesh.from_edit_mesh(obj.data).faces)[-2:]


def fill_tie_drag():
    """A Fill cap on a tie follows memory layout (see preview._apply_live_cut):
    a drag may show the fast cut's cap, but its end re-cuts exactly - the
    preview at rest is the cap Cut makes."""
    import cut_plane
    from cut_plane import cutting
    from cut_plane.cutting import preview as pv
    cut_plane.register()
    try:
        for ob in list(bpy.data.objects):
            bpy.data.objects.remove(ob, do_unlink=True)
        bpy.ops.mesh.primitive_torus_add(major_segments=24, minor_segments=10)
        obj = ctx.active_object
        bpy.ops.object.mode_set(mode='EDIT')
        sc = ctx.window_manager.cut_plane
        sc['cut_clear_inner'], sc['cut_clear_outer'], sc['cut_use_fill'] = False, True, True
        plane = [Vector((0, 0, 0.3)), Vector((0, 0, 1))]
        with patch.object(pv, 'get_plane_world', side_effect=lambda c: tuple(plane)):
            cutting.update_live_preview(ctx)          # session start
            plane[0] = Vector((0, 0, 0))              # exactly on a vertex ring
            cutting.update_live_preview(ctx)
            at_rest = caps(obj)
            check('at rest a Fill preview is never the fast cut',
                  not _session(obj).fill_tied)
            cutting.begin_owned_input()
            plane[0] = Vector((0, 0, 0.3))
            cutting.update_live_preview(ctx)
            plane[0] = Vector((0, 0, 0))
            cutting.update_live_preview(ctx)
            check('mid-drag the fast cut serves Fill', _session(obj).fill_tied)
            cutting.end_owned_input()
            check('the end of the drag re-cuts exactly', not _session(obj).fill_tied)
            check('...to the cap a cut at rest makes', caps(obj) == at_rest,
                  f'{caps(obj)} vs {at_rest}')
    finally:
        cutting.restore_live_cut(ctx)
        if ctx.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        cut_plane.unregister()


def edit_state(obj):
    """The live edit mesh, described like describe() does a Mesh."""
    bm = bmesh.from_edit_mesh(obj.data)
    me = bpy.data.meshes.new('state')
    bm.to_mesh(me)
    out = describe(me)
    bpy.data.meshes.remove(me)
    return out


def drag_matches_rest():
    """Mid-drag the cut goes through the snapshot sorted along the normal
    (clip.ClipFrame) with the selection flushes trimmed: a translate drag
    ending at a plane must leave exactly what a cut at rest there leaves -
    mesh and selection, every select mode, every Clear."""
    import cut_plane
    from cut_plane import cutting
    from cut_plane.cutting import preview as pv
    cut_plane.register()
    try:
        for mode in ('VERT', 'EDGE', 'FACE'):
            for ci, co_ in ((False, False), (False, True), (True, False)):
                for ob in list(bpy.data.objects):
                    bpy.data.objects.remove(ob, do_unlink=True)
                monkey()
                obj = ctx.active_object
                bpy.ops.object.mode_set(mode='EDIT')
                bpy.ops.mesh.select_mode(type=mode)
                sc = ctx.window_manager.cut_plane
                sc['cut_clear_inner'], sc['cut_clear_outer'] = ci, co_
                sc['cut_use_fill'] = False
                no = Vector((0.2, 0.1, 1.0)).normalized()
                plane = [Vector((0, 0, 0.3)), no]
                name = f'drag {mode} ci={ci} co={co_}'
                with patch.object(pv, 'get_plane_world', side_effect=lambda c: tuple(plane)):
                    cutting.update_live_preview(ctx)           # session start
                    plane[0] = Vector((0, 0, 0.07))
                    cutting.update_live_preview(ctx)
                    at_rest = edit_state(obj)
                    cutting.begin_owned_input()
                    for z in (0.2, 0.12, 0.07):                # a translate
                        plane[0] = Vector((0, 0, z))
                        cutting.update_live_preview(ctx)
                    check(f'{name}: a sorted frame served the drag',
                          bool(_session(obj).clip_frames))
                    got = edit_state(obj)
                    diffs = [k for k in sorted(set(got) | set(at_rest))
                             if got.get(k) != at_rest.get(k)]
                    check(f'{name}: same mesh and selection as at rest', not diffs,
                          ', '.join(diffs[:3]))
                    cutting.end_owned_input()
                cutting.restore_live_cut(ctx)
                bpy.ops.object.mode_set(mode='OBJECT')
    finally:
        cutting.restore_live_cut(ctx)
        if ctx.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        cut_plane.unregister()


def _session(obj):
    from cut_plane.cutting.state import _session_for
    return _session_for(obj)


def main():
    tilt = (0.2, 0.1, 1.0)
    clears = [(False, False), (False, True), (True, False), (True, True)]

    obj, snap = object_from(monkey)
    for ci, co_ in clears:
        compare(f'monkey tilted ci={ci} co={co_}', snap, obj, (0, 0, 0.07), tilt, ci, co_)
    compare('monkey, plane misses it', snap, obj, (0, 0, 5.0), (0, 0, 1), False, True)
    compare('monkey, plane misses it (keep side)', snap, obj, (0, 0, 5.0), (0, 0, 1), True, False)

    obj, snap = object_from(monkey)
    for mode in ({'EDGE'}, {'FACE'}):
        for ci, co_ in clears:
            compare(f'monkey in {min(mode)} mode ci={ci} co={co_}', snap, obj,
                    (0, 0, 0.07), tilt, ci, co_, mode)

    obj, snap = object_from(rich_cube)
    for ci, co_ in clears:
        compare(f'rich cube in FACE mode, diagonal ci={ci} co={co_}', snap, obj,
                (0, 0, 0), (1, -1, 0), ci, co_, {'FACE'})
        compare(f'rich cube on a vertex row ci={ci} co={co_}', snap, obj,
                (0, 0, 0), (0, 0, 1), ci, co_)
        compare(f'rich cube between rows ci={ci} co={co_}', snap, obj,
                (0.25, 0, 0), (1, 0, 0), ci, co_)
        compare(f'rich cube diagonal through corners ci={ci} co={co_}', snap, obj,
                (0, 0, 0), (1, -1, 0), ci, co_)
    arr = clip.clip_arrays(obj, snap)
    check('the active UV map is recorded', arr is not None and arr.uv_active == 'Second')

    obj, snap = object_from(loose_bits)
    for ci, co_ in clears:
        compare(f'loose verts and wire edges ci={ci} co={co_}', snap, obj,
                (0, 0, 0), (0, 0, 1), ci, co_)
        compare(f'grid edge-on, plane through a vertex column ci={ci} co={co_}', snap, obj,
                (0, 0, 0), (1, 0, 0), ci, co_)

    obj, snap = object_from(ngon_disc)
    for ci, co_ in clears:
        compare(f'n-gon caps and triangles ci={ci} co={co_}', snap, obj,
                (0, 0, 0.1), (0.3, 0.2, 1.0), ci, co_)
        compare(f'n-gon caps cut across ci={ci} co={co_}', snap, obj,
                (0.1, 0, 0), (1, 0.1, 0), ci, co_)

    # --- refused: only the exact path can do these -----------------------
    obj, snap = object_from(l_shape)
    bm = bmesh.new()
    bm.from_mesh(snap)
    before = len(bm.faces)
    bm.faces.ensure_lookup_table()
    cache = Cache()
    # x + y = 2.5 crosses both arms of the L: four plane points on one face.
    cut = clip.clip_cut(cache, bm, snap, clip.clip_arrays(obj, snap),
                        Vector((1.25, 1.25, 0)), Vector((1, 1, 0)).normalized(),
                        BISECT_DIST, False, True)
    check('a concave n-gon crossed twice is refused', cut is None)
    frame = clip.clip_frame(snap, clip.clip_arrays(obj, snap),
                            Vector((1, 1, 0)).normalized())
    check('...by the sorted frame too',
          clip.frame_cut(cache, bm, clip.clip_arrays(obj, snap), frame,
                         Vector((1.25, 1.25, 0)), BISECT_DIST, True) is None)
    check('...before the bmesh is touched', len(bm.faces) == before and bm.faces[0].is_valid)
    clip.free_scratch(cache)
    bm.free()
    compare('the same n-gon crossed once', snap, obj, (2.0, 0, 0), (1, 0, 0), False, True)

    obj, snap = object_from(rich_cube)
    snap.color_attributes.new('Bytes', 'BYTE_COLOR', 'POINT')
    check('a byte colour (lossy round trip) refuses the session',
          clip.clip_arrays(obj, snap) is None)
    obj, snap = object_from(rich_cube)
    obj.vertex_groups.new(name='G')
    check('vertex groups refuse the session', clip.clip_arrays(obj, snap) is None)
    obj, snap = object_from(rich_cube)
    obj.shape_key_add(name='Basis')
    snap = obj.data.copy()
    check('shape keys refuse the session', clip.clip_arrays(obj, snap) is None)

    fill_tie_drag()
    drag_matches_rest()

    leftover = [m.name for m in bpy.data.meshes if m.name.startswith(clip.SCRATCH_NAME)]
    check('no scratch mesh is left behind', not leftover, str(leftover))
    if FAILURES:
        print(f'CLIP PARITY: {len(FAILURES)} FAILED', flush=True)
        os._exit(1)
    print('CLIP PARITY: OK', flush=True)
    os._exit(0)


main()
