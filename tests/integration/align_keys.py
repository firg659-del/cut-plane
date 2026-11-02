# SPDX-License-Identifier: GPL-3.0-or-later
"""Shift+X / Shift+Y / Shift+Z lay the plane in a world plane.

    blender --background --factory-startup --python-exit-code 1 \\
            --python tests/integration/align_keys.py

Named like a native transform's plane keys - Shift + the axis LEFT OUT:
Shift+Z = XY (normal +Z, what Alt+R also gives), Shift+Y = XZ, Shift+X = YZ.
The plane keeps its position in both orientations (in LOCAL the offsets are
read through the rotation), the live preview follows, one tool Ctrl+Z
restores the pose before, and the keys are rebindable panel rows.
"""
import math
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
from cut_plane.lib.math_utils import get_plane_pose  # noqa: E402
from cut_plane.ui import tool  # noqa: E402

FAILURES = []
ctx = bpy.context


def check(name, condition, detail=''):
    print(f"[{'ok' if condition else 'FAIL'}] {name}" + (f' ({detail})' if detail else ''),
          flush=True)
    if not condition:
        FAILURES.append(name)


def close(a, b, tol=1e-5):
    return (Vector(a) - Vector(b)).length < tol


def plane():
    co, no = cutting.get_plane_world(ctx)
    return co.copy(), no.copy()


def top_z(obj):
    return round(max(v.co.z for v in bmesh.from_edit_mesh(obj.data).verts), 4)


def max_x(obj):
    return round(max(v.co.x for v in bmesh.from_edit_mesh(obj.data).verts), 4)


def main():
    cut_plane.register()
    try:
        for ob in list(bpy.data.objects):
            bpy.data.objects.remove(ob, do_unlink=True)
        bpy.ops.mesh.primitive_cube_add(size=2)
        obj = ctx.active_object
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.subdivide(number_cuts=3)
        props = ctx.window_manager.cut_plane
        props['cut_clear_inner'], props['cut_clear_outer'], props['cut_use_fill'] = \
            False, True, False

        # --- each key lays the plane in its world plane ---------------------
        for orientation in ('GLOBAL', 'LOCAL'):
            props.orientation = orientation
            bpy.ops.view3d.cut_plane_reset()
            # A tilted plane away from the centre: something to align FROM.
            props.rotation_matrix_flat = [v for row in (
                Matrix.Rotation(math.radians(30), 4, 'X')
                @ Matrix.Rotation(math.radians(20), 4, 'Z')) for v in row]
            props.x_offset, props.y_offset, props.z_offset = 0.3, -0.2, 0.25
            for axis, normal, name in (('Z', (0, 0, 1), 'XY'), ('Y', (0, 1, 0), 'XZ'),
                                       ('X', (1, 0, 0), 'YZ')):
                before_co, _no = plane()
                bpy.ops.view3d.cut_plane_align(axis=axis)
                co, no = plane()
                check(f'{orientation}: Shift+{axis} lays the plane in world {name}',
                      close(no, normal), f'normal {tuple(round(c, 4) for c in no)}')
                check(f'{orientation}: ...and keeps its position', close(co, before_co),
                      f'{tuple(before_co)} -> {tuple(co)}')

        # --- the preview follows, and Clear Outer drops the positive side ---
        props.orientation = 'GLOBAL'
        bpy.ops.view3d.cut_plane_reset()
        props.x_offset, props.y_offset, props.z_offset = 0.25, 0.0, 0.25
        bpy.ops.view3d.cut_plane_align(axis='Z')
        check('XY with Clear Outer keeps what is below the plane', top_z(obj) == 0.25,
              str(top_z(obj)))
        bpy.ops.view3d.cut_plane_align(axis='X')
        check('YZ with Clear Outer keeps what is behind it on X', max_x(obj) == 0.25,
              str(max_x(obj)))
        check('...and the XY cut is gone (the cube is whole in Z)', top_z(obj) == 1.0,
              str(top_z(obj)))
        # Alt+R gives the same plane as Shift+Z: XY is the reset rotation.
        bpy.ops.view3d.cut_plane_reset(mode='ROTATION')
        _co, no_reset = plane()
        check('Alt+R and Shift+Z agree (XY, normal +Z)', close(no_reset, (0, 0, 1)))

        # --- one tool Ctrl+Z restores the pose before -------------------------
        bpy.ops.view3d.cut_plane_align(axis='Y')
        pose_y = get_plane_pose(ctx)[1].copy()
        bpy.ops.view3d.cut_plane_align(axis='X')
        bpy.ops.view3d.cut_plane_undo()
        check('Ctrl+Z after an align restores the previous plane',
              get_plane_pose(ctx)[1] == pose_y)

        # --- the keys -------------------------------------------------------
        items = {i[1]["type"]: dict(i[2]["properties"])["axis"]
                 for i in tool._TOOL_KEYMAP_BASE if i[0] == "view3d.cut_plane_align"
                 and i[1].get("shift") and not i[1].get("ctrl") and not i[1].get("alt")}
        check('Shift+X / Shift+Y / Shift+Z name the axis left out',
              items == {'X': 'X', 'Y': 'Y', 'Z': 'Z'}, str(items))
        from cut_plane.state.preferences import _KEY_ROWS, _binding_warning, _row_item
        km = tool.tool_keymap_editable()
        rows = [(label, _row_item(km, idname, mode)) for label, idname, mode in _KEY_ROWS]
        found = {label: (kmi.type, kmi.shift) for label, kmi in rows
                 if label.startswith('Align') and kmi is not None}
        check('the panel owns one row per align key',
              found == {'Align XY': ('Z', True), 'Align XZ': ('Y', True),
                        'Align YZ': ('X', True)}, str(found))
        check('...and the shipped keys raise no conflict', _binding_warning(rows) == '',
              _binding_warning(rows))
    finally:
        cutting.restore_live_cut(ctx)
        if ctx.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        cut_plane.unregister()
    if FAILURES:
        print(f'ALIGN KEYS: {len(FAILURES)} FAILED', flush=True)
        os._exit(1)
    print('ALIGN KEYS: OK', flush=True)
    os._exit(0)


main()
