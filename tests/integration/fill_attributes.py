# SPDX-License-Identifier: GPL-3.0-or-later
"""Fill caps must match native Bisect in ORIENTATION and ATTRIBUTES.

    blender --background --factory-startup --python-exit-code 1 \
            --python tests/integration/fill_attributes.py

fill_parity.py compares cap face COUNTS only, which is how 1.1.0 shipped a
Clear Inner cap facing INTO the kept solid (inconsistent winding along the
rim) and caps that lost the neighbouring material, smoothing and UVs: the
preview and Execute Cut called triangle_fill but never face_attribute_fill,
the step native mesh.bisect runs right after it. This suite compares, per
case and per Clear setting, the preview, the commit and native Bisect on:
cap normal direction, material index, smooth flag, UVs, and the number of
manifold edges whose two faces disagree on winding.
"""
import os
import sys

import bmesh
import bpy

ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

import cut_plane  # noqa: E402
from cut_plane import cutting  # noqa: E402

FAILURES = []
ctx = bpy.context


def check(name, condition, detail=''):
    print(f"[{'ok' if condition else 'FAIL'}] {name}" + (f' ({detail})' if detail else ''),
          flush=True)
    if not condition:
        FAILURES.append(name)


def build(kind):
    """A closed solid on material slot 1, smooth shaded, with a non-trivial
    UV map - so a cap that copies nothing shows up as slot 0 / flat / (0,0)."""
    cutting.restore_live_cut(ctx)
    if ctx.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    if kind == 'cube':
        bpy.ops.mesh.primitive_cube_add(size=2)
    elif kind == 'cylinder':
        bpy.ops.mesh.primitive_cylinder_add(vertices=12, radius=1, depth=2)
    else:
        bpy.ops.mesh.primitive_uv_sphere_add(segments=12, ring_count=8, radius=1)
    obj = ctx.active_object
    me = obj.data
    me.materials.append(bpy.data.materials.new('A'))
    me.materials.append(bpy.data.materials.new('B'))
    for p in me.polygons:
        p.material_index = 1
        p.use_smooth = True
    uv = me.uv_layers.active or me.uv_layers.new()
    for loop in me.loops:
        co = me.vertices[loop.vertex_index].co
        uv.data[loop.index].uv = (0.25 + co.x * 0.2, 0.75 + co.y * 0.2)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='DESELECT')
    return obj


def describe(obj):
    """(caps, winding_conflicts) of the edit mesh; caps = sorted
    (normal z sign, material, smooth, rounded UV set) for faces on z=0."""
    bm = bmesh.from_edit_mesh(obj.data)
    bm.normal_update()
    uv = bm.loops.layers.uv.active
    caps = []
    for f in bm.faces:
        if all(abs(v.co.z) < 1e-5 for v in f.verts):
            uvs = tuple(sorted({(round(lp[uv].uv.x, 4), round(lp[uv].uv.y, 4))
                                for lp in f.loops}))
            caps.append((round(f.normal.z), f.material_index, f.smooth, uvs))
    conflicts = 0
    for e in bm.edges:
        if len(e.link_faces) == 2:
            a, b = (next(lp for lp in f.loops if lp.edge == e) for f in e.link_faces)
            if a.vert == b.vert:        # both faces walk the edge the same way
                conflicts += 1
    return sorted(caps), conflicts


def options(inner, outer):
    sc = ctx.window_manager.cut_plane
    sc['cut_clear_inner'], sc['cut_clear_outer'], sc['cut_use_fill'] = inner, outer, True
    sc['cut_selected_only'] = False
    sc['cut_select_loop'] = False
    sc['mirror_x'] = sc['mirror_y'] = sc['mirror_z'] = False
    sc['center_offset'] = (0.0, 0.0, 0.0)          # real pose: plane at the origin
    sc['x_offset'] = sc['y_offset'] = sc['z_offset'] = 0.0


def native(kind, inner, outer):
    obj = build(kind)
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.bisect(plane_co=(0, 0, 0), plane_no=(0, 0, 1), use_fill=True,
                        clear_inner=inner, clear_outer=outer)
    return describe(obj)


def preview(kind, inner, outer):
    obj = build(kind)
    options(inner, outer)
    cutting.update_live_preview(ctx)
    result = describe(obj)
    cutting.restore_live_cut(ctx)
    return result


def commit(kind, inner, outer):
    obj = build(kind)
    options(inner, outer)
    status = bpy.ops.view3d.cut_plane_cut('EXEC_DEFAULT', plane_co=(0, 0, 0), plane_no=(0, 0, 1),
                                          clear_inner=inner, clear_outer=outer, use_fill=True)
    return describe(obj), status


def main():
    cut_plane.register()
    try:
        for kind in ('cube', 'cylinder', 'sphere'):
            for inner, outer in ((True, False), (False, True), (False, False)):
                side = 'Clear Inner' if inner else ('Clear Outer' if outer else 'no Clear')
                ref = native(kind, inner, outer)
                got_preview = preview(kind, inner, outer)
                got_commit, status = commit(kind, inner, outer)
                check(f'{kind} {side}: native Bisect produced caps (oracle sanity)', bool(ref[0]))
                if not (inner or outer):
                    # Without a Clear native lays a membrane INSIDE the solid,
                    # with an undefined normal (either sign across runs,
                    # measured). Cut Plane fills only with a Clear
                    # (cutting.fill_applies): no cap at all, in both.
                    check(f'{kind} {side}: Fill adds no internal cap (preview, commit)',
                          not got_preview[0] and not got_commit[0] and status == {'FINISHED'},
                          f'preview {len(got_preview[0])} commit {len(got_commit[0])} caps')
                    continue
                check(f'{kind} {side}: preview caps match native (normal, material, smooth, UVs)',
                      got_preview[0] == ref[0], f'native {ref[0][:2]} preview {got_preview[0][:2]}')
                check(f'{kind} {side}: commit caps match native', got_commit[0] == ref[0],
                      f'native {ref[0][:2]} commit {got_commit[0][:2]} ({status})')
                check(f'{kind} {side}: commit winding consistent like native',
                      got_commit[1] == ref[1], f'conflicting edges native {ref[1]} commit {got_commit[1]}')
    finally:
        cutting.restore_live_cut(ctx)
        if ctx.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        cut_plane.unregister()
    if FAILURES:
        print(f'FILL ATTRIBUTES: {len(FAILURES)} FAILED', flush=True)
        os._exit(1)
    print('FILL ATTRIBUTES: OK', flush=True)
    os._exit(0)


main()
