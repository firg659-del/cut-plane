# SPDX-License-Identifier: GPL-3.0-or-later
"""A transform that ends abnormally cleans up exactly like Esc.

    blender --background --factory-startup --python-exit-code 1 \
            --python tests/integration/modal_exits.py

A modal() that raised skipped on_cancel: the plane stayed at its
mid-drag pose with no undo entry, and the rotation arc, hidden handles,
header and legend stayed on screen.
"""
import os
import sys
from types import SimpleNamespace

import bpy

ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

import cut_plane  # noqa: E402
from cut_plane.operators.modal_base import CutPlaneModalMixin  # noqa: E402

FAILURES = []


def check(name, condition, detail=''):
    print(f"[{'ok' if condition else 'FAIL'}] {name}" + (f' ({detail})' if detail else ''),
          flush=True)
    if not condition:
        FAILURES.append(name)


class FakeTransform(CutPlaneModalMixin):
    """The real modal() skeleton with recording hooks."""
    bl_idname = 'view3d.cut_plane_move_center'
    require_runtime = False

    def __init__(self, fail=False):
        self.cancelled = 0
        self.fail = fail
        self.num_input = SimpleNamespace(handle_event=lambda event: False, active=False)

    def on_cancel(self, context, props, runtime):
        self.cancelled += 1

    def handle_modal_event(self, context, event, props, runtime):
        if self.fail:
            raise RuntimeError('injected failure inside a transform')
        return {'RUNNING_MODAL'}


EVENT = SimpleNamespace(type='MOUSEMOVE', value='NOTHING', ctrl=False, shift=False,
                        alt=False, oskey=False, hyper=False)


def main():
    cut_plane.register()
    ctx = bpy.context
    try:
        # 2. modal() raised.
        op = FakeTransform(fail=True)
        type(op)._active_instance = op
        try:
            op.modal(ctx, EVENT)
            raised = False
        except RuntimeError:
            raised = True
        check('an error still reaches the console', raised)
        check('...after on_cancel ran, like Esc', op.cancelled == 1, str(op.cancelled))
        check('...and the operator is released', type(op)._active_instance is None)
    finally:
        cut_plane.unregister()
    if FAILURES:
        print(f'MODAL EXITS: {len(FAILURES)} FAILED', flush=True)
        os._exit(1)
    print('MODAL EXITS: OK', flush=True)
    os._exit(0)


main()
