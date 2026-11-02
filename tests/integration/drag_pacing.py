# SPDX-License-Identifier: GPL-3.0-or-later
"""On a dense mesh a drag stays fluid: the re-cut is paced, never lost.

    blender --background --factory-startup --python-exit-code 1 \\
            --python tests/integration/drag_pacing.py

A re-cut costs O(whole mesh) - each bmesh operator allocates flags for the
whole mesh (~0.4 s at 500k faces) - so cutting on every mouse move froze
the drag, gizmo included. While a transform owns the input and the last
re-cut was slower than PACE_BUDGET_MS, a move only records the plane; the
cut follows when the pointer rests, and always when the drag ends.
"""
import os
import sys
import time
from unittest.mock import patch

import bmesh
import bpy
from mathutils import Vector

ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

import cut_plane  # noqa: E402
from cut_plane import cutting  # noqa: E402
from cut_plane.cutting import preview as pv  # noqa: E402

FAILURES = []
ctx = bpy.context
plane = [Vector((0.0, 0.0, 0.3)), Vector((0.0, 0.0, 1.0))]


def check(name, condition, detail=''):
    print(f"[{'ok' if condition else 'FAIL'}] {name}" + (f' ({detail})' if detail else ''),
          flush=True)
    if not condition:
        FAILURES.append(name)


def top(obj):
    """The highest vertex: where the Clear Outer cut currently is."""
    return round(max(v.co.z for v in bmesh.from_edit_mesh(obj.data).verts), 4)


def scene():
    cutting.restore_live_cut(ctx)
    if ctx.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16)
    bpy.ops.object.mode_set(mode='EDIT')
    sc = ctx.window_manager.cut_plane
    sc['cut_clear_inner'], sc['cut_clear_outer'], sc['cut_use_fill'] = False, True, False
    sc['cut_selected_only'] = False
    sc['mirror_x'] = sc['mirror_y'] = sc['mirror_z'] = False
    pv._last_cost_ms = pv._last_cut_end = 0.0
    return ctx.active_object


def move(z):
    plane[0] = Vector((0.0, 0.0, z))
    cutting.update_live_preview(ctx)


def main():
    cut_plane.register()
    try:
        with patch.object(pv, 'get_plane_world', side_effect=lambda c: tuple(plane)):
            # A "dense" mesh: every re-cut counts as slower than the budget.
            obj = scene()
            with patch.object(pv, 'PACE_BUDGET_MS', -1.0):
                move(0.35)              # starts the session (not timed)
                move(0.3)
                check('cuts outside a drag apply at once', top(obj) == 0.3, str(top(obj)))
                cutting.begin_owned_input()
                move(0.1)
                check('during a drag, a move right after a slow cut is paced',
                      top(obj) == 0.3 and bpy.app.timers.is_registered(pv._paced_tick),
                      str(top(obj)))
                move(-0.2)
                check('...and only the latest plane is kept', top(obj) == 0.3)
                pv._paced_request = time.perf_counter() - 1.0   # the pointer rested
                check('when the pointer rests, the cut catches up',
                      pv._paced_tick() is None and top(obj) == -0.2, str(top(obj)))
                pv._last_cut_end = time.perf_counter()
                move(0.15)
                check('...paced again while it keeps moving', top(obj) == -0.2)
                cutting.end_owned_input()
                check('the end of the drag applies the last plane',
                      top(obj) == 0.15 and not bpy.app.timers.is_registered(pv._paced_tick),
                      str(top(obj)))
                move(0.05)
                check('outside a drag nothing is paced', top(obj) == 0.05, str(top(obj)))

                cutting.begin_owned_input()
                pv._last_cut_end = time.perf_counter()
                move(0.25)
                cutting.restore_live_cut(ctx)
                check('leaving the tool cancels a paced cut',
                      not bpy.app.timers.is_registered(pv._paced_tick))
                cutting.end_owned_input()

            # A light mesh is cut on every move, as before - even right
            # after the cut that started its session: the one-time snapshot
            # must not count as the cost of a cut.
            obj = scene()
            with patch.object(pv, 'PACE_BUDGET_MS', 0.0):
                move(0.3)
            check('a session-starting cut is not timed', pv._last_cost_ms == 0.0,
                  f'{pv._last_cost_ms:.1f} ms')
            cutting.begin_owned_input()
            for z in (0.2, 0.1, 0.0):
                move(z)
                check(f'a light mesh is cut on every move ({z})', top(obj) == z, str(top(obj)))
            cutting.end_owned_input()
    finally:
        cutting.restore_live_cut(ctx)
        if ctx.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        cut_plane.unregister()
    if FAILURES:
        print(f'DRAG PACING: {len(FAILURES)} FAILED', flush=True)
        os._exit(1)
    print('DRAG PACING: OK', flush=True)
    os._exit(0)


main()
