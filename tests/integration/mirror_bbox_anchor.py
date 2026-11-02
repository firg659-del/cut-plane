# SPDX-License-Identifier: GPL-3.0-or-later
"""Execute Cut mirrors about the SAME point the preview showed.

    blender --background --factory-startup --python-exit-code 1 \
            --python tests/integration/mirror_bbox_anchor.py

1.1.0 restored the preview first, which ended the session, and only then
resolved the Bounding Box anchor - from obj.data, which in Edit Mode holds
the mesh as of Edit Mode entry. After one cut in the same Edit Mode
session the commit therefore mirrored about a stale centre: the preview
showed cuts at -0.25 and -0.75, Execute cut at -0.25 and +0.25 (outside the
remaining half, so the mirrored cut silently vanished).

Also: a scripted Execute with the Cursor anchor and no cursor_world used
the world origin; it now uses the live 3D cursor, like the preview.
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
from cut_plane.lib.constants import PLANE_ANCHOR_ITEMS  # noqa: E402

FAILURES = []
ctx = bpy.context


def check(name, condition, detail=''):
    print(f"[{'ok' if condition else 'FAIL'}] {name}" + (f' ({detail})' if detail else ''),
          flush=True)
    if not condition:
        FAILURES.append(name)


def anchor_index(name):
    return next(item[4] for item in PLANE_ANCHOR_ITEMS if item[0] == name)


def build(sync_between_cuts):
    """A subdivided cube, first cut (Clear Outer at z=0) committed in the SAME
    Edit Mode session - unless the control re-syncs obj.data first."""
    cutting.restore_live_cut(ctx)
    if ctx.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    bpy.ops.mesh.primitive_cube_add(size=2)
    obj = ctx.active_object
    bpy.ops.object.mode_set(mode='EDIT')
    # Rows every 0.5 (z = -1, -0.5, 0 after the first cut): the mirrored
    # cuts at -0.25 / -0.75 / -0.55 must CREATE rows, so a missing cut shows.
    bpy.ops.mesh.subdivide(number_cuts=3)
    bpy.ops.mesh.select_all(action='DESELECT')
    status = bpy.ops.view3d.cut_plane_cut('EXEC_DEFAULT', plane_co=(0, 0, 0), plane_no=(0, 0, 1),
                                          clear_outer=True)
    assert status == {'FINISHED'}, status
    cutting.restore_live_cut(ctx)
    if sync_between_cuts:
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.mode_set(mode='EDIT')
    return obj


def z_levels(obj):
    return sorted({round(v.co.z, 4) for v in bmesh.from_edit_mesh(obj.data).verts})


def setup_mirror(z):
    sc = ctx.window_manager.cut_plane
    sc['cut_clear_inner'] = sc['cut_clear_outer'] = sc['cut_use_fill'] = False
    sc['cut_selected_only'] = False
    sc['cut_select_loop'] = False
    sc['mirror_x'] = sc['mirror_y'] = False
    sc['mirror_z'] = True
    sc['plane_anchor'] = anchor_index('BBOX')
    sc['center_offset'] = (0.0, 0.0, float(z))
    sc['x_offset'] = sc['y_offset'] = sc['z_offset'] = 0.0


def run(sync, keep_session):
    obj = build(sync)
    setup_mirror(-0.25)
    cutting.update_live_preview(ctx)
    preview = z_levels(obj)
    if not keep_session:                 # scripted call with no live preview
        cutting.restore_live_cut(ctx)
    status = bpy.ops.view3d.cut_plane_cut('EXEC_DEFAULT', plane_co=(0, 0, -0.25), plane_no=(0, 0, 1),
                                          mirror_z=True, plane_anchor='BBOX')
    return preview, z_levels(obj), status


def main():
    cut_plane.register()
    try:
        for sync, keep, label in ((False, True, 'second cut in the same session'),
                                  (False, False, 'second cut, preview already left'),
                                  (True, True, 'CONTROL: obj.data synced between the cuts')):
            preview, commit, status = run(sync, keep)
            check(f'{label}: the preview mirrors about the remaining half (-0.75 and -0.25)',
                  -0.75 in preview and -0.25 in preview, str(preview))
            check(f'{label}: Execute cuts where the preview showed', commit == preview,
                  f'preview {preview} commit {commit} ({status})')

        # Cursor anchor, scripted Execute without cursor_world: the live cursor.
        obj = build(False)
        ctx.scene.cursor.location = (0.0, 0.0, -0.4)
        setup_mirror(-0.25)
        ctx.window_manager.cut_plane['plane_anchor'] = anchor_index('CURSOR')
        cutting.update_live_preview(ctx)
        preview = z_levels(obj)
        status = bpy.ops.view3d.cut_plane_cut('EXEC_DEFAULT', plane_co=(0, 0, -0.25), plane_no=(0, 0, 1),
                                              mirror_z=True, plane_anchor='CURSOR')
        commit = z_levels(obj)
        check('Cursor anchor, scripted Execute without cursor_world: mirrors about the live cursor',
              -0.55 in commit and commit == preview, f'preview {preview} commit {commit} ({status})')
        ctx.scene.cursor.location = (0.0, 0.0, 0.0)
    finally:
        cutting.restore_live_cut(ctx)
        if ctx.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        cut_plane.unregister()
    if FAILURES:
        print(f'MIRROR BBOX ANCHOR: {len(FAILURES)} FAILED', flush=True)
        os._exit(1)
    print('MIRROR BBOX ANCHOR: OK', flush=True)
    os._exit(0)


main()
