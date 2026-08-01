# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless test: the watchdog asks ONE global tool-active question.

    blender --background --factory-startup --python-exit-code 1 \
            --python tests/integration/watchdog_windows.py

Contract: the live-cut watchdog is a POST_VIEW draw handler - it runs once
per 3D viewport per frame - but it drives ONE global mesh state. Its
tool-active input must therefore be the ANY-WINDOW answer
(_tool_active_any_window, built on workspace.tools which needs no context),
never the drawing region's own.

The bug this locks: the watchdog asked the per-context is_plane_tool_active.
With two windows on different workspaces (Layout with Cut Plane active +
any second window on UV Editing/Modeling/... whose edit tool is Tweak), the
Cut Plane viewport scheduled 'apply' and the other scheduled 'restore' -
the mesh bisected and restored every frame, forever, CPU pinned.

Headless notes: draw handlers never fire and workspace.tools is never
populated in --background, so the GLOBAL answer is monkeypatched (its API
was verified live on 4.5 and 5.1) and the watchdog body is called directly.
A per-context alternator stands in for the two windows' disagreeing
answers: the fixed watchdog must be deaf to it. Timers do not self-fire
headless either, so servicing unregisters + calls _deferred_sync by hand -
exactly what one timer tick does.

Exit code 0 on success, 1 with the failed checks listed.
"""
import inspect
import os
import sys

import bmesh
import bpy

ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

import cut_plane  # noqa: E402
from cut_plane import cutting  # noqa: E402

cut_plane.register()

failures = []


def check(name, cond, detail=""):
    print(f"[{'ok  ' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


# A cube in Edit Mode with Clear Inner on, so cut vs uncut is unmistakable.
bpy.ops.object.select_all(action='DESELECT')
bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
obj = bpy.context.active_object
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.mesh.select_all(action='SELECT')
sc = bpy.context.window_manager.cut_plane
sc.cut_clear_inner = True
# The option's own update callback just applied a preview - restore it so
# the FIRST watchdog frame below is the one that starts the session.
cutting.restore_live_cut(bpy.context)

UNCUT = 6


def faces():
    return len(bmesh.from_edit_mesh(obj.data).faces)


def service():
    """One timer tick: fire the deferred sync exactly once."""
    if bpy.app.timers.is_registered(cutting._deferred_sync):
        bpy.app.timers.unregister(cutting._deferred_sync)
    if cutting._op_pending:
        cutting._deferred_sync()


def frame():
    """One watchdog pass + its timer tick; returns the action it scheduled."""
    before = cutting._pending_action
    cutting._live_cut_watchdog()
    action = cutting._pending_action if cutting._op_pending else before
    service()
    return action


# --- structural: the watchdog consults the any-window answer ---------------
src = inspect.getsource(cutting._live_cut_watchdog)
check("the watchdog asks _tool_active_any_window, not the drawing region",
      "_tool_active_any_window" in src and "is_plane_tool_active" not in src)

# --- the two-window scenario ------------------------------------------------
# Global truth: the tool IS active (in some window). Per-viewport truth: an
# alternator that disagrees on every other call - the exact input pattern
# two windows produce. The fixed watchdog must never consult it.
_alt = {"n": 0}


def alternating_per_context(_ctx):
    _alt["n"] += 1
    return _alt["n"] % 2 == 1


# Patch the OWNING module (cutting.watchdog): the façade re-export is a
# separate binding, and _live_cut_watchdog resolves the name in its own module.
cutting.watchdog._tool_active_any_window = lambda: True
# Bait for a MUTANT that reintroduces per-context detection: plant the
# alternator where such a watchdog would resolve the name - its OWN module
# namespace (a facade-only plant went inert with the package split).
cutting.watchdog.is_plane_tool_active = alternating_per_context
cutting.is_plane_tool_active = alternating_per_context

check("first frame applies the preview", frame() == 'apply')
check("...the mesh is now really cut", faces() != UNCUT, f"{faces()} faces")
check("...one session exists", len(cutting._sessions) == 1)

actions = [frame() for _ in range(8)]
check("REGRESSION: 8 alternating-viewport frames schedule NOTHING",
      all(a is None for a in actions), str(actions))
check("...the mesh stayed cut the whole time (no flicker)",
      faces() != UNCUT, f"{faces()} faces")
check("...and the session survived", len(cutting._sessions) == 1)

# --- tool active NOWHERE -> exactly one restore, then quiet ------------------
cutting.watchdog._tool_active_any_window = lambda: False
check("tool inactive everywhere restores once", frame() == 'restore')
check("...the mesh is back to the baseline", faces() == UNCUT, f"{faces()} faces")
check("...sessions ended", len(cutting._sessions) == 0)
actions = [frame() for _ in range(4)]
check("...and later frames stay quiet", all(a is None for a in actions),
      str(actions))

print(f"\nWATCHDOG WINDOWS: {'FAIL - ' + ', '.join(failures) if failures else 'OK'}")
sys.stdout.flush()
os._exit(1 if failures else 0)
