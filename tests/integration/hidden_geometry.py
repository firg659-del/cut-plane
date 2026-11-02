# SPDX-License-Identifier: GPL-3.0-or-later
"""Hidden geometry is never cut or cleared, in the preview and in Execute.

    blender --background --factory-startup --python-exit-code 1 \
            --python tests/integration/hidden_geometry.py

1.1.0 passed hidden elements to the bisect and the Clear in standard mode,
so a part the user hid to protect it was split, and with Clear its cleared
side was deleted - invisibly, since hidden faces are not drawn. Native
Bisect cannot reach hidden geometry (it is selection-scoped).

Cases (plane z = 0.1, normal +Z):
  island    a hidden UV sphere beside a visible cube in ONE mesh: the sphere
            must survive untouched; the cube must match native Bisect.
  connected a UV sphere with its +X half hidden: no hidden face may be
            deleted or split, no visible element may survive above the
            plane, and the preview must equal the commit.
  split     the same, no Clear: hidden faces are not split.
  control   nothing hidden: preview and commit match native exactly (the
            fast path is unchanged).
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
Z = 0.1


def check(name, condition, detail=''):
    print(f"[{'ok' if condition else 'FAIL'}] {name}" + (f' ({detail})' if detail else ''),
          flush=True)
    if not condition:
        FAILURES.append(name)


def build(kind):
    cutting.restore_live_cut(ctx)
    if ctx.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    if kind == 'island':
        bpy.ops.mesh.primitive_cube_add(size=2)
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.subdivide(number_cuts=2)
        bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8, location=(3, 0, 0))
        hide_if = lambda c: c.x > 1.5          # noqa: E731
    else:
        bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8)
        bpy.ops.object.mode_set(mode='EDIT')
        hide_if = (lambda c: c.x > 0.3) if kind != 'control' else (lambda c: False)  # noqa: E731
    # Pin the select mode: mesh.hide hides by the CURRENT mode (vertex mode
    # also hides every face touching a selected vertex). 1.1.0's preview
    # rebuild zeroed the edit mesh's select mode, which made identical builds
    # hide different face counts - see the select-mode check in main().
    bpy.ops.mesh.select_mode(type='FACE')
    bm = bmesh.from_edit_mesh(ctx.active_object.data)
    for f in bm.faces:
        f.select_set(hide_if(f.calc_center_median()))
    bmesh.update_edit_mesh(ctx.active_object.data)
    if any(f.select for f in bm.faces):
        bpy.ops.mesh.hide(unselected=False)
    bpy.ops.mesh.select_all(action='DESELECT')
    bpy.ops.mesh.select_mode(type='VERT')
    return ctx.active_object


def state(obj):
    bm = bmesh.from_edit_mesh(obj.data)
    hidden = [f for f in bm.faces if f.hide]
    visible_verts = sorted(tuple(round(c, 4) for c in v.co) for v in bm.verts if not v.hide)
    return {
        'hidden_faces': len(hidden),
        'hidden_area': round(sum(f.calc_area() for f in hidden), 5),
        'visible_verts': visible_verts,
        'visible_faces': sum(1 for f in bm.faces if not f.hide),
        'visible_above': sum(1 for v in bm.verts if not v.hide and v.co.z > Z + 1e-4),
    }


def options(outer, fill):
    sc = ctx.window_manager.cut_plane
    sc['cut_clear_inner'], sc['cut_clear_outer'], sc['cut_use_fill'] = False, outer, fill
    sc['cut_selected_only'] = False
    sc['cut_select_loop'] = False
    sc['mirror_x'] = sc['mirror_y'] = sc['mirror_z'] = False
    sc['center_offset'] = (0.0, 0.0, Z)
    sc['x_offset'] = sc['y_offset'] = sc['z_offset'] = 0.0


def run(kind, outer, fill):
    obj = build(kind)
    before = state(obj)
    bpy.ops.mesh.select_all(action='SELECT')            # native: visible geometry only
    bpy.ops.mesh.bisect(plane_co=(0, 0, Z), plane_no=(0, 0, 1), use_fill=fill, clear_outer=outer)
    native = state(obj)

    obj = build(kind)
    check(f'{kind}: builds are identical (preview run)', state(obj) == before)
    options(outer, fill)
    cutting.update_live_preview(ctx)
    preview = state(obj)
    cutting.restore_live_cut(ctx)
    restored = state(obj)
    check(f'{kind}: the preview rebuild keeps the select mode',
          set(bmesh.from_edit_mesh(obj.data).select_mode) == {'VERT'},
          str(set(bmesh.from_edit_mesh(obj.data).select_mode)))

    obj = build(kind)
    check(f'{kind}: builds are identical (commit run)', state(obj) == before)
    options(outer, fill)
    status = bpy.ops.view3d.cut_plane_cut('EXEC_DEFAULT', plane_co=(0, 0, Z), plane_no=(0, 0, 1),
                                          clear_outer=outer, use_fill=fill)
    commit = state(obj)
    return before, native, preview, restored, commit, status


def main():
    cut_plane.register()
    try:
        for fill in (False, True):
            tag = ' + Fill' if fill else ''
            before, native, preview, restored, commit, status = run('island', True, fill)
            check(f'island{tag}: oracle keeps the hidden sphere', native['hidden_faces'] == before['hidden_faces'])
            for label, got in (('preview', preview), ('commit', commit)):
                check(f'island{tag}: {label} keeps the hidden sphere untouched',
                      (got['hidden_faces'], got['hidden_area']) == (before['hidden_faces'], before['hidden_area']),
                      f"hidden faces {before['hidden_faces']} -> {got['hidden_faces']}")
                check(f'island{tag}: {label} cuts the visible cube like native',
                      (got['visible_verts'], got['visible_faces']) == (native['visible_verts'], native['visible_faces']))
            check(f'island{tag}: leaving the tool restores the original', restored == before)

            before, native, preview, restored, commit, status = run('connected', True, fill)
            for label, got in (('preview', preview), ('commit', commit)):
                check(f'connected{tag}: {label} deletes no hidden face',
                      got['hidden_faces'] == before['hidden_faces']
                      and abs(got['hidden_area'] - before['hidden_area']) < 1e-4,
                      f"hidden faces {before['hidden_faces']} -> {got['hidden_faces']}")
                check(f'connected{tag}: {label} leaves nothing visible above the plane',
                      got['visible_above'] == 0, f"{got['visible_above']} visible verts above")
            check(f'connected{tag}: preview equals commit',
                  {k: v for k, v in preview.items()} == {k: v for k, v in commit.items()})
            check(f'connected{tag}: leaving the tool restores the original', restored == before)

        before, native, preview, restored, commit, status = run('split', False, False)
        for label, got in (('preview', preview), ('commit', commit)):
            check(f'split: {label} does not split hidden faces',
                  got['hidden_faces'] == before['hidden_faces'], f"{before['hidden_faces']} -> {got['hidden_faces']}")

        for fill in (False, True):
            tag = ' + Fill' if fill else ''
            before, native, preview, restored, commit, status = run('control', True, fill)
            for label, got in (('preview', preview), ('commit', commit)):
                check(f'control (nothing hidden){tag}: {label} matches native',
                      (got['visible_verts'], got['visible_faces']) == (native['visible_verts'], native['visible_faces']))
    finally:
        cutting.restore_live_cut(ctx)
        if ctx.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        cut_plane.unregister()
    if FAILURES:
        print(f'HIDDEN GEOMETRY: {len(FAILURES)} FAILED', flush=True)
        os._exit(1)
    print('HIDDEN GEOMETRY: OK', flush=True)
    os._exit(0)


main()
