# SPDX-License-Identifier: GPL-3.0-or-later
"""An export never writes the uncommitted preview while its file browser is open.

    blender --background --factory-startup --python-exit-code 1 \
            --python tests/integration/edit_mode_exports.py

1.1.0 had no export guard in Edit Mode: with a Clear Outer + Fill preview
showing, File > Export > STL (and PLY, USD, Alembic, Collada) wrote the
uncommitted cut - a file identical to one of a committed cut. The preview
now pauses while an operator's file browser is open, and comes back the
way it was when the browser closes.

Headless notes: there is no window, so no real browser - the detector is
patched to report one, and the watch timer and the watchdog are called
directly, which is what one timer turn or one redraw does. The real dialog,
including the Export-press race the latch closes, needs a GUI session. The
CONTROL checks prove the instrument can fail: without a browser the export
really writes the preview.
"""
import os
import sys
import tempfile

import bmesh
import bpy

ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

import cut_plane  # noqa: E402
from cut_plane import cutting  # noqa: E402
from cut_plane.cutting import dialogs, handlers, preview, state, watchdog  # noqa: E402

FAILURES = []
ctx = bpy.context
OUT = tempfile.mkdtemp(prefix='cut_plane_exports_')
REAL_DETECTOR = dialogs.operator_file_browser_open
ORIGINAL = (8, 6, 1.0)          # default cube: verts, faces, top z


def check(name, condition, detail=''):
    print(f"[{'ok' if condition else 'FAIL'}] {name}" + (f' ({detail})' if detail else ''),
          flush=True)
    if not condition:
        FAILURES.append(name)


def browser(is_open):
    """Patch the OWNING module: preview and watchdog resolve it there."""
    dialogs.operator_file_browser_open = (lambda: True) if is_open else REAL_DETECTOR


def tick():
    """One turn of the browser watch, as its timer runs it."""
    return watchdog._dialog_watch()


def redraw():
    """One watchdog pass (one viewport redraw); returns the action it scheduled."""
    state._op_pending = False
    state._pending_action = None
    cutting._live_cut_watchdog()
    action = state._pending_action if state._op_pending else None
    if bpy.app.timers.is_registered(cutting._deferred_sync):
        bpy.app.timers.unregister(cutting._deferred_sync)
    if state._op_pending:
        cutting._deferred_sync()
    return action


def stl_z(name):
    path = os.path.join(OUT, name + '.stl')
    bpy.ops.wm.stl_export(filepath=path, ascii_format=True)
    with open(path) as f:
        zs = [float(line.split()[3]) for line in f if line.strip().startswith('vertex')]
    return round(min(zs), 3), round(max(zs), 3)


def ply_z(name):
    path = os.path.join(OUT, name + '.ply')
    bpy.ops.wm.ply_export(filepath=path, ascii_format=True)
    with open(path) as f:
        lines = f.read().splitlines()
    count = next(int(line.split()[2]) for line in lines if line.startswith('element vertex'))
    start = lines.index('end_header') + 1
    zs = [float(line.split()[2]) for line in lines[start:start + count]]
    return round(min(zs), 3), round(max(zs), 3)


def mesh(obj):
    bm = bmesh.from_edit_mesh(obj.data)
    return len(bm.verts), len(bm.faces), round(max(v.co.z for v in bm.verts), 3)


def plane_at(z):
    sc = ctx.window_manager.cut_plane
    sc['center_offset'] = (0.0, 0.0, float(z))
    sc['x_offset'] = sc['y_offset'] = sc['z_offset'] = 0.0


def build(count=1):
    """Default cubes in one Edit Mode session, Clear Outer + Fill at z=0."""
    browser(False)
    cutting.restore_live_cut(ctx)
    state._dialog_resume = None
    if ctx.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    objs = []
    for i in range(count):
        bpy.ops.mesh.primitive_cube_add(size=2, location=(3.0 * i, 0, 0))
        objs.append(ctx.active_object)
    for ob in objs:
        ob.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='DESELECT')
    sc = ctx.window_manager.cut_plane
    sc['cut_clear_inner'], sc['cut_clear_outer'], sc['cut_use_fill'] = False, True, True
    sc['cut_selected_only'] = False
    sc['mirror_x'] = sc['mirror_y'] = sc['mirror_z'] = False
    plane_at(0.0)
    return objs


def execute():
    """Execute Cut at the gizmo plane, as the button does (a different plane
    leaves the baseline unarmed on purpose - see adopt_committed_baseline)."""
    co, no = cutting.get_plane_world(ctx)
    status = bpy.ops.view3d.cut_plane_cut('EXEC_DEFAULT', plane_co=tuple(co), plane_no=tuple(no),
                                          clear_outer=True, use_fill=True)
    assert status == {'FINISHED'}, status


def main():
    cut_plane.register()
    # The watchdog caches this from its draw context; headless never draws.
    state._last_tool_active = True
    watchdog._tool_active_any_window = lambda: True
    try:
        # --- a live preview, then a browser -------------------------------
        obj, = build()
        cutting.update_live_preview(ctx)
        check('fixture: the preview shows the cut', mesh(obj)[2] == 0.0 and bool(cutting._sessions),
              str(mesh(obj)))
        check('CONTROL: with no browser open, STL writes the preview', stl_z('control') == (-1.0, 0.0),
              str(stl_z('control')))
        check('CONTROL: with no browser open, PLY writes the preview', ply_z('control') == (-1.0, 0.0),
              str(ply_z('control')))

        browser(True)
        check('the watch keeps running while a browser is open', tick() == 0.0)
        check('one watch turn restores the original', mesh(obj) == ORIGINAL, str(mesh(obj)))
        check('...and ends the session', not cutting._sessions)
        check('STL exported meanwhile holds the original', stl_z('paused') == (-1.0, 1.0),
              str(stl_z('paused')))
        check('PLY exported meanwhile holds the original', ply_z('paused') == (-1.0, 1.0),
              str(ply_z('paused')))

        plane_at(-0.5)
        cutting.update_live_preview(ctx)            # a plane move or option toggle
        check('nothing re-applies the preview while the browser is open',
              mesh(obj) == ORIGINAL and not cutting._sessions, str(mesh(obj)))
        check('the watchdog schedules nothing while the browser is open', redraw() is None)
        check('...and it keeps the watch running',
              bpy.app.timers.is_registered(watchdog._dialog_watch))

        browser(False)
        tick()
        check('closing the browser brings the preview back, at the moved plane',
              mesh(obj)[2] == -0.5 and bool(cutting._sessions), str(mesh(obj)))
        check('...and the pause is over', state._dialog_resume is None)

        # --- Execute + Fill, then a browser: no second cap ------------------
        objs = build(2)
        execute()
        committed = [mesh(ob) for ob in objs]
        check('fixture: Execute left an armed baseline', preview.sessions_armed())
        browser(True)
        tick()
        check('pausing the armed baseline changes nothing', [mesh(ob) for ob in objs] == committed)
        check('...an export writes the committed cut', stl_z('armed') == (-1.0, 0.0),
              str(stl_z('armed')))
        browser(False)
        tick()
        check('resuming re-adopts it: no second cap on either object',
              [mesh(ob) for ob in objs] == committed and preview.sessions_armed()
              and len(cutting._sessions) == 2, f'{[mesh(ob) for ob in objs]} vs {committed}')
        cutting.restore_live_cut(ctx)
        cutting.update_live_preview(ctx)
        if bpy.app.version < (5, 2, 0):
            check('CONTROL: a plain re-apply at the committed plane lays a second cap',
                  mesh(objs[0])[1] > committed[0][1], f'{mesh(objs[0])} vs {committed[0]}')
        else:
            # Measured on 5.2.2: the re-apply no longer duplicates the cap.
            print(f'[info] 5.2+: a plain re-apply gives {mesh(objs[0])} (committed {committed[0]})')

        obj, = build()
        execute()
        committed = mesh(obj)
        browser(True)
        tick()
        plane_at(-0.5)
        cutting.update_live_preview(ctx)
        browser(False)
        tick()
        check('a plane moved while paused previews from the committed cut on resume',
              mesh(obj)[2] == -0.5, str(mesh(obj)))
        cutting.restore_live_cut(ctx)
        check('...and leaving shows the committed cut again', mesh(obj) == committed, str(mesh(obj)))

        # --- an undo while paused: the armed baseline is gone ---------------
        obj, = build()
        execute()
        browser(True)
        tick()
        check('fixture: paused with a re-adoption pending',
              state._dialog_resume is not None and state._dialog_resume[0] == 'readopt')
        handlers._on_undo_redo_post()
        if bpy.app.timers.is_registered(cutting._deferred_sync):
            bpy.app.timers.unregister(cutting._deferred_sync)
            cutting._deferred_sync()                # its apply meets the open browser
        check('an undo while paused resumes with a fresh preview instead',
              state._dialog_resume == ('apply', None), str(state._dialog_resume))
        check('...and still shows nothing while the browser is open', not cutting._sessions)

        # --- a stale resume never bakes a live preview ----------------------
        obj, = build()
        cutting.update_live_preview(ctx)
        state._dialog_resume = ('readopt', {})
        tick()
        check('a resume with a preview already live is dropped', state._dialog_resume is None)
        cutting.restore_live_cut(ctx)
        check('...and leaving still restores the original', mesh(obj) == ORIGINAL, str(mesh(obj)))
        cutting.update_live_preview(ctx)
        cutting.adopt_committed_baseline(ctx)
        cutting.restore_live_cut(ctx)
        check('CONTROL: adopting a live preview as the baseline would bake the cut',
              mesh(obj) != ORIGINAL, str(mesh(obj)))

        # --- the tool is left while paused -----------------------------------
        obj, = build()
        cutting.update_live_preview(ctx)
        browser(True)
        tick()
        state._last_tool_active = False
        browser(False)
        check('the watch stops once nothing is left to watch', tick() is None)
        check('leaving the tool while paused: the preview does not come back',
              not cutting._sessions and mesh(obj) == ORIGINAL and state._dialog_resume is None,
              str(mesh(obj)))
        state._last_tool_active = True
        check('the watch runs while the tool is active', tick() == 0.0)
    finally:
        browser(False)
        cutting.restore_live_cut(ctx)
        if ctx.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        cut_plane.unregister()
    check('unregister stops the watch', not bpy.app.timers.is_registered(watchdog._dialog_watch))
    if FAILURES:
        print(f'EDIT MODE EXPORTS: {len(FAILURES)} FAILED', flush=True)
        os._exit(1)
    print('EDIT MODE EXPORTS: OK', flush=True)
    os._exit(0)


main()
