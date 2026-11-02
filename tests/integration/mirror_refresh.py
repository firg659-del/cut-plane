# SPDX-License-Identifier: GPL-3.0-or-later
"""Mirror previews follow a moved anchor; the density budget counts planes.

    blender --background --factory-startup --python-exit-code 1 \\
            --python tests/integration/mirror_refresh.py

  * Anchor = Cursor with a mirror on: moving the 3D cursor (Shift+right
    click, Shift+S) runs none of our operators, so the mirrored preview kept
    the old cut. The watchdog now notices the moved anchor.
  * A mirror that maps the plane onto itself adds no cut, yet halved the
    preview's face budget: a mesh within budget for its one real plane lost
    its preview as 'mesh too dense'.
"""
import os
import sys
from unittest.mock import patch

import bmesh
import bpy
from mathutils import Vector

ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

import cut_plane  # noqa: E402
from cut_plane import cutting  # noqa: E402
from cut_plane.cutting import preview as preview_module, watchdog  # noqa: E402

FAILURES = []
ctx = bpy.context


def check(name, condition, detail=''):
    print(f"[{'ok' if condition else 'FAIL'}] {name}" + (f' ({detail})' if detail else ''),
          flush=True)
    if not condition:
        FAILURES.append(name)


def edit_cube():
    cutting.restore_live_cut(ctx)
    if ctx.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    bpy.ops.mesh.primitive_cube_add(size=2)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.subdivide(number_cuts=3)
    return ctx.active_object


def verts(obj):
    return sorted(tuple(round(c, 3) for c in v.co) for v in bmesh.from_edit_mesh(obj.data).verts)


def main():
    cut_plane.register()
    sc = ctx.window_manager.cut_plane
    try:
        # --- the cursor anchor --------------------------------------------
        obj = edit_cube()
        sc['cut_clear_inner'], sc['cut_clear_outer'], sc['cut_use_fill'] = False, True, False
        sc['mirror_x'] = True
        sc.plane_anchor = 'CURSOR'
        ctx.scene.cursor.location = (0.2, 0.0, 0.0)
        plane = (Vector((0.6, 0.0, 0.0)), Vector((1.0, 0.0, 0.0)))
        with patch.object(preview_module, 'get_plane_world', return_value=plane), \
                patch.object(cutting, 'get_plane_world', return_value=plane):
            cutting.update_live_preview(ctx)
            before = verts(obj)
            check('an untouched anchor asks for nothing', not watchdog._mirror_pivot_moved(ctx))
            ctx.scene.cursor.location = (-0.3, 0.0, 0.0)    # Shift+right-click
            check('a moved cursor is noticed by the watchdog',
                  watchdog._mirror_pivot_moved(ctx))
            cutting.update_live_preview(ctx)               # what it schedules
            check('...and the mirrored cut follows it', verts(obj) != before)
            check('...after which nothing more is asked', not watchdog._mirror_pivot_moved(ctx))
        sc['mirror_x'] = False
        check('no mirror: the anchor is never watched', not watchdog._mirror_pivot_moved(ctx))
        sc.plane_anchor = 'BBOX'

        # --- the density budget ------------------------------------------
        obj = edit_cube()
        faces = len(bmesh.from_edit_mesh(obj.data).faces)
        sc['mirror_x'], sc['mirror_y'], sc['mirror_z'] = False, False, True
        # A plane through the origin with an X normal: Mirror Z maps it onto
        # itself, so one plane is cut.
        plane = (Vector((0.0, 0.0, 0.0)), Vector((1.0, 0.0, 0.0)))
        with patch.object(preview_module, 'get_plane_world', return_value=plane), \
                patch('cut_plane.state.preferences.preview_face_cap', return_value=faces + 1):
            check('a self-reflecting mirror keeps the one-plane budget',
                  cutting.live_preview_blocker(obj) is None,
                  str(cutting.live_preview_blocker(obj)))
        plane = (Vector((0.0, 0.0, 0.3)), Vector((0.0, 0.0, 1.0)))   # Mirror Z: 2 planes
        with patch.object(preview_module, 'get_plane_world', return_value=plane), \
                patch('cut_plane.state.preferences.preview_face_cap', return_value=faces + 1):
            check('...a real second plane still halves it',
                  cutting.live_preview_blocker(obj) == 'mesh too dense',
                  str(cutting.live_preview_blocker(obj)))
        sc['mirror_z'] = False
    finally:
        cutting.restore_live_cut(ctx)
        if ctx.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        cut_plane.unregister()
    if FAILURES:
        print(f'MIRROR REFRESH: {len(FAILURES)} FAILED', flush=True)
        os._exit(1)
    print('MIRROR REFRESH: OK', flush=True)
    os._exit(0)


main()
