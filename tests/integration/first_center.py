# SPDX-License-Identifier: GPL-3.0-or-later
"""A fresh plane centres on the ORIGINAL mesh, even when the preview cut it first.

    blender --background --factory-startup --python-exit-code 1 \
            --python tests/integration/first_center.py

On first activation the watchdog's preview runs before the one-shot
centering timer (traced in the GUI on 4.5, 5.1 and 5.2). With a Clear on,
1.1.0 then measured the evaluated box - already the cut half - so a default
cube got its plane at z -0.5 instead of its centre (5.1 and 5.2; 4.5's box
lagged a turn). The centering now measures the session snapshot, like Reset.

Headless, view_layer.update() stands in for the turn that brings the
evaluated box up to date; a CONTROL check proves the box really is the cut
half when the centering runs.
"""
import os
import sys

import bpy
from mathutils import Matrix

ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

import cut_plane  # noqa: E402
from cut_plane import cutting  # noqa: E402
from cut_plane.state import properties  # noqa: E402

FAILURES = []
ctx = bpy.context


def check(name, condition, detail=''):
    print(f"[{'ok' if condition else 'FAIL'}] {name}" + (f' ({detail})' if detail else ''),
          flush=True)
    if not condition:
        FAILURES.append(name)


def first_activation(lift, clear_inner, clear_outer):
    """A cube whose mesh is lifted by `lift`, a fresh plane at its origin,
    the first preview applied, then the one-shot centering."""
    cutting.restore_live_cut(ctx)
    if ctx.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    bpy.ops.mesh.primitive_cube_add(size=2)
    obj = ctx.active_object
    obj.data.transform(Matrix.Translation((0, 0, lift)))
    bpy.ops.object.mode_set(mode='EDIT')
    sc = ctx.window_manager.cut_plane
    sc['cut_clear_inner'], sc['cut_clear_outer'], sc['cut_use_fill'] = clear_inner, clear_outer, True
    sc['mirror_x'] = sc['mirror_y'] = sc['mirror_z'] = False
    sc['center_offset'] = (0.0, 0.0, 0.0)
    sc['x_offset'] = sc['y_offset'] = sc['z_offset'] = 0.0
    properties.get_runtime(ctx).centered_edit_uid = 0
    cutting.update_live_preview(ctx)          # the watchdog's first apply
    ctx.view_layer.update()                   # the next turn's evaluation
    box = sorted({round(c[2], 3) for c in obj.bound_box})
    properties.ensure_default_center(ctx)     # the gizmo setup's timer
    return box, round(sc.center_offset[2], 4)


def main():
    cut_plane.register()
    try:
        for lift, inner, outer, label in ((0.0, False, True, 'default cube, Clear Outer'),
                                          (0.0, True, False, 'default cube, Clear Inner'),
                                          (0.5, False, True, 'lifted mesh, Clear Outer')):
            box, z = first_activation(lift, inner, outer)
            cut_box = [lift - 1.0, 0.0] if outer else [0.0, lift + 1.0]
            if bpy.app.version >= (5, 0, 0):
                check(f'CONTROL {label}: the evaluated box is the cut half when the centering runs',
                      box == cut_box, str(box))
            else:
                # Measured on 4.5.11, here and in the GUI: the edit-mode box
                # lags, so the trap never springs there.
                print(f'[info] 4.5: the evaluated box still reads {box}')
            check(f'{label}: the plane centres on the original ({lift})', z == lift, f'z {z}')
    finally:
        cutting.restore_live_cut(ctx)
        if ctx.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        cut_plane.unregister()
    if FAILURES:
        print(f'FIRST CENTER: {len(FAILURES)} FAILED', flush=True)
        os._exit(1)
    print('FIRST CENTER: OK', flush=True)
    os._exit(0)


main()
