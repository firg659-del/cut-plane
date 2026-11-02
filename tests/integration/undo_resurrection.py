# SPDX-License-Identifier: GPL-3.0-or-later
"""An undo never brings back a cut that was never executed.

    blender --background --factory-startup --python-exit-code 1 \
            --python tests/integration/undo_resurrection.py

The preview lives in the edit mesh, so an undo step pushed while it shows -
a click-select, Tab out - stores the preview. In 1.1.0 a later Ctrl+Z that
landed on such a step made the un-executed cut real: the next session took
it for the original, and geometry a Clear had hidden was gone for good.

Flows (the review's; plane A z 0.5, plane B z -0.5, Clear Outer; a clean
default cube is 6 faces, top z 1.0):
  main  preview A, select, preview B, Execute at B, Ctrl+Z
  sel2  preview A, select, deselect, Ctrl+Z
  exit  preview A, select, leave the tool, a native edit, Ctrl+Z
  tab   preview A, Tab out, an object move, Ctrl+Z
  kept  preview A, shade smooth on top of it, select, Ctrl+Z: that step is
        the user's own edit, so it stays as they saw it (WYSIWYG)

Headless: the undo system needs an initial push; timers do not fire, so the
purge timer (which repairs first) and the deferred re-apply are run by hand,
exactly as one timer turn would.
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
CLEAN = (6, 1.0)
NAME = 'Cube'


def check(name, condition, detail=''):
    print(f"[{'ok' if condition else 'FAIL'}] {name}" + (f' ({detail})' if detail else ''),
          flush=True)
    if not condition:
        FAILURES.append(name)


def obj():
    return bpy.data.objects[NAME]  # undo replaces the Python objects


def geo():
    ob = obj()
    if ob.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(ob.data)
        return len(bm.faces), round(max(v.co.z for v in bm.verts), 3)
    return len(ob.data.polygons), round(max(v.co.z for v in ob.data.vertices), 3)


def markers():
    ob = obj()
    if ob.mode == 'EDIT':
        names = bmesh.from_edit_mesh(ob.data).faces.layers.int.keys()
    else:
        names = [a.name for a in ob.data.attributes]
    return [n for n in names if n.startswith('.cutplane_pv_')]


def plane(z):
    sc = ctx.window_manager.cut_plane
    sc['center_offset'] = (0.0, 0.0, float(z))
    sc['x_offset'] = sc['y_offset'] = sc['z_offset'] = 0.0


def preview(z):
    plane(z)
    cutting.update_live_preview(ctx)


def timers():
    """One timer turn after the undo: the purge (it repairs first), then the
    deferred re-apply the undo handler scheduled while the tool is active."""
    for fn in (cutting._purge_orphan_snapshots, cutting._deferred_sync):
        if bpy.app.timers.is_registered(fn):
            bpy.app.timers.unregister(fn)
            fn()


def start():
    cutting.restore_live_cut(ctx)
    if ctx.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    bpy.ops.mesh.primitive_cube_add(size=2)
    ctx.active_object.name = NAME
    bpy.ops.ed.undo_push(message='start')
    bpy.ops.object.editmode_toggle('EXEC_DEFAULT', True)
    bpy.ops.mesh.select_all('EXEC_DEFAULT', True, action='DESELECT')
    sc = ctx.window_manager.cut_plane
    sc['cut_clear_inner'], sc['cut_clear_outer'], sc['cut_use_fill'] = False, True, False
    sc['cut_selected_only'] = False
    sc['mirror_x'] = sc['mirror_y'] = sc['mirror_z'] = False
    cutting.state._last_tool_active = True    # the watchdog's cache (headless never draws)


def select(action):
    bpy.ops.mesh.select_all('EXEC_DEFAULT', True, action=action)


def leave_and_inspect(label):
    """Look at the baseline (plane off the mesh), then leave the tool and Edit Mode."""
    preview(5.0)
    check(f'{label}: with the plane off the mesh the full cube shows', geo() == CLEAN, str(geo()))
    cutting.restore_live_cut(ctx)
    bpy.ops.object.editmode_toggle('EXEC_DEFAULT', True)
    check(f'{label}: the mesh left in Object Mode is the full cube', geo() == CLEAN, str(geo()))
    check(f'{label}: no marker is left behind', not markers(), str(markers()))


def main():
    cut_plane.register()
    ctx.preferences.edit.use_global_undo = True
    try:
        start()
        preview(0.5)
        check('fixture: the preview writes a marker', len(markers()) == 1, str(markers()))
        select('SELECT')                     # an undo step holding preview A
        preview(-0.5)
        status = bpy.ops.view3d.cut_plane_cut('EXEC_DEFAULT', True, plane_co=(0, 0, -0.5),
                                              plane_no=(0, 0, 1), clear_outer=True)
        check('fixture: Execute at B', status == {'FINISHED'} and geo() == (5, -0.5), str(geo()))
        check('...and the committed cut carries no marker', not markers(), str(markers()))
        bpy.ops.ed.undo()
        timers()
        check('main: Ctrl+Z of the cut does not bring back preview A',
              geo() in (CLEAN, (5, -0.5)), str(geo()))
        leave_and_inspect('main')

        start()
        preview(0.5)
        select('SELECT')
        select('DESELECT')
        bpy.ops.ed.undo()
        timers()
        leave_and_inspect('sel2')

        start()
        preview(0.5)
        select('SELECT')
        cutting.restore_live_cut(ctx)        # the user leaves the tool
        cutting.state._last_tool_active = False
        bpy.ops.transform.translate('EXEC_DEFAULT', True, value=(0.25, 0, 0))
        bpy.ops.ed.undo()
        timers()
        check('exit: undoing the edit lands on the clean cube', geo() == CLEAN, str(geo()))
        check('exit: no marker is left behind', not markers(), str(markers()))

        start()
        preview(0.5)
        bpy.ops.object.editmode_toggle('EXEC_DEFAULT', True)   # Tab out
        check('fixture: Tab out restores the clean cube', geo() == CLEAN, str(geo()))
        cutting.state._last_tool_active = False
        obj().location.x = 2.0
        bpy.ops.ed.undo_push(message='Move')
        bpy.ops.ed.undo()
        timers()
        check('tab: undoing an object move keeps the clean cube', geo() == CLEAN, str(geo()))
        check('tab: no marker is left behind', not markers(), str(markers()))

        start()
        preview(0.5)
        select('SELECT')
        bpy.ops.mesh.faces_shade_smooth('EXEC_DEFAULT', True)  # the user's edit, on the preview
        select('DESELECT')
        cutting.state._last_tool_active = False   # no re-apply: look at the step itself
        bpy.ops.ed.undo()                    # back onto the shade-smooth step
        timers()
        bm = bmesh.from_edit_mesh(obj().data)
        check('kept: a step the user edited on top of the preview stays as they saw it',
              geo() == (5, 0.5) and all(f.smooth for f in bm.faces), str(geo()))
        check('kept: ...and loses the marker', not markers(), str(markers()))
    finally:
        cutting.restore_live_cut(ctx)
        if ctx.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        cut_plane.unregister()
    if FAILURES:
        print(f'UNDO RESURRECTION: {len(FAILURES)} FAILED', flush=True)
        os._exit(1)
    print('UNDO RESURRECTION: OK', flush=True)
    os._exit(0)


main()
