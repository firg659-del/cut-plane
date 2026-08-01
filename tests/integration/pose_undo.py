# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless test: tool-scoped pose undo (Ctrl+Z walks plane moves AND cuts).

    blender --background --factory-startup --python tests/integration/pose_undo.py

The pose stays OUTSIDE native undo (WM tool state - the F9 fix); Ctrl+Z is
shadowed in the TOOL keymap by view3d.cut_plane_undo, which pops our own
pose history when no tracked mesh step is newer, else falls through to
ed.undo. Ordering rides a scene MESH-STEP COUNTER: our cut advances it,
mesh undo steps snapshot it (see pose_history.py - v1 compared exact
tokens and mis-trimmed every move in multi-cut sessions, user-caught).

Native ed.undo itself is undrivable headlessly (known limit) - the counter
rollbacks it performs are simulated by writing the scene counter directly;
everything else runs the real operators.

Exit code 0 on success, 1 with the failed checks listed.
"""
import os
import sys

import bpy

ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

import cut_plane  # noqa: E402
from cut_plane import cutting  # noqa: E402
from cut_plane.lib import constants  # noqa: E402
from cut_plane.state import pose_history  # noqa: E402
from cut_plane.ui.tool import _TOOL_KEYMAP  # noqa: E402

constants.DEBUG = True
cut_plane.register()

failures = []


def check(name, cond, detail=""):
    print(f"[{'ok  ' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


def center():
    return tuple(round(v, 3)
                 for v in bpy.context.window_manager.cut_plane.center_offset)


def counter():
    return bpy.context.scene.get(pose_history.TOKEN_PROP, 0)


def set_counter(v):
    """Simulate native undo/redo restoring the counter from a step snapshot
    (ed.undo is undrivable headlessly)."""
    bpy.context.scene[pose_history.TOKEN_PROP] = v


def simulate_move(to):
    """A confirmed transform: capture-before, write, push (the exact calls
    the modal confirm branch makes)."""
    before = pose_history.capture_pose(bpy.context)
    bpy.context.window_manager.cut_plane.center_offset = to
    pose_history.push_if_changed(bpy.context, before)


def fresh():
    pose_history.clear()
    set_counter(0)
    bpy.context.window_manager.cut_plane.center_offset = (0.0, 0.0, 0.0)


bpy.ops.mesh.primitive_cube_add(size=2)
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.mesh.select_all(action='DESELECT')
sc = bpy.context.window_manager.cut_plane

# --- 1. keymap contract ------------------------------------------------------
km = _TOOL_KEYMAP
idnames = [e[0] for e in km]
check("tool keymap binds Ctrl+Z", "view3d.cut_plane_undo" in idnames)
check("tool keymap binds Ctrl+Shift+Z", "view3d.cut_plane_redo" in idnames)
undo_kmi = next(e for e in km if e[0] == "view3d.cut_plane_undo")
check("undo entry is ctrl+Z exactly",
      undo_kmi[1].get("ctrl") and undo_kmi[1]["type"] == 'Z'
      and not undo_kmi[1].get("shift"))

# --- 2. pose-only roundtrip ---------------------------------------------------
fresh()
simulate_move((1.0, 0.0, 0.0))
simulate_move((2.0, 0.0, 0.0))
check("pushes never touch the mesh counter", counter() == 0, f"{counter()}")
res = bpy.ops.view3d.cut_plane_undo()
check("undo pops newest move", res == {'FINISHED'} and center() == (1.0, 0.0, 0.0),
      f"center={center()}")
res = bpy.ops.view3d.cut_plane_undo()
check("undo pops first move", res == {'FINISHED'} and center() == (0.0, 0.0, 0.0),
      f"center={center()}")
res = bpy.ops.view3d.cut_plane_redo()
check("redo re-applies the first move",
      res == {'FINISHED'} and center() == (1.0, 0.0, 0.0), f"center={center()}")
simulate_move((5.0, 0.0, 0.0))
check("a new change clears the redo branch",
      not pose_history.redo(bpy.context))

# --- 3. THE multi-cut walk (the user-caught regression) ----------------------
# move -> cut -> move -> cut, then Ctrl+Z x4 must give:
# cut2, move2, cut1, move1 - v1 trimmed the moves and only walked cuts.
fresh()
simulate_move((0.0, 0.0, 0.25))                       # move1  (mc=0)
r = bpy.ops.view3d.cut_plane_cut(plane_co=(0, 0, 0.25), plane_no=(0, 0, 1))
check("cut1 FINISHED", r == {'FINISHED'})
check("cut1 advanced the counter", counter() == 1, f"{counter()}")
simulate_move((0.0, 0.0, 0.5))                        # move2  (mc=1)
r = bpy.ops.view3d.cut_plane_cut(plane_co=(0, 0, 0.5), plane_no=(0, 0, 1))
check("cut2 FINISHED", r == {'FINISHED'})
check("cut2 advanced the counter", counter() == 2, f"{counter()}")

res = bpy.ops.view3d.cut_plane_undo()                 # -> should be cut2
check("press 1 falls through to native (cut2 is newest)",
      center() == (0.0, 0.0, 0.5), f"center={center()} res={res}")
set_counter(1)                                        # native undid cut2
res = bpy.ops.view3d.cut_plane_undo()                 # -> move2
check("press 2 pops move2 (NOT trimmed - the v1 bug)",
      res == {'FINISHED'} and center() == (0.0, 0.0, 0.25), f"center={center()}")
res = bpy.ops.view3d.cut_plane_undo()                 # -> cut1
check("press 3 falls through to native (cut1)",
      center() == (0.0, 0.0, 0.25), f"center={center()}")
set_counter(0)                                        # native undid cut1
res = bpy.ops.view3d.cut_plane_undo()                 # -> move1
check("press 4 pops move1 - full chain walked",
      res == {'FINISHED'} and center() == (0.0, 0.0, 0.0), f"center={center()}")

# redo mirrors the walk: move1 first (mc=0 <= 0), then mesh redo needed
res = bpy.ops.view3d.cut_plane_redo()
check("redo 1 re-applies move1", res == {'FINISHED'}
      and center() == (0.0, 0.0, 0.25), f"center={center()}")
check("redo 2 defers to native mesh redo (move2 is later)",
      not pose_history.redo(bpy.context))
set_counter(1)                                        # native redid cut1
res = bpy.ops.view3d.cut_plane_redo()
check("redo 3 re-applies move2 after the mesh redo",
      res == {'FINISHED'} and center() == (0.0, 0.0, 0.5), f"center={center()}")

# --- 4. external native undo (tool inactive): poses stay walkable -------------
fresh()
simulate_move((1.0, 0.0, 0.0))                        # mc=0
pose_history.mark_mesh_step(bpy.context)              # cut (simulated), mc->1
simulate_move((2.0, 0.0, 0.0))                        # mc=1
set_counter(0)                                        # user undid the cut NATIVELY
res = bpy.ops.view3d.cut_plane_undo()
check("after external undo, newest pose still pops",
      res == {'FINISHED'} and center() == (1.0, 0.0, 0.0), f"center={center()}")
res = bpy.ops.view3d.cut_plane_undo()
check("...and the older pose after it",
      res == {'FINISHED'} and center() == (0.0, 0.0, 0.0), f"center={center()}")

# --- 5. undo branching: a NEW cut drops the overwritten pose future -----------
fresh()
pose_history.mark_mesh_step(bpy.context)              # cut1, counter=1
simulate_move((3.0, 0.0, 0.0))                        # mc=1
set_counter(0)                                        # native undo of cut1
pose_history.mark_mesh_step(bpy.context)              # NEW cut -> branch point
check("branch point drops the overwritten pose entry",
      not pose_history._undo_stack)
check("counter advanced for the new cut", counter() == 1)

# --- 6. Reset is one undoable pose entry (real operator, end to end) ---------
fresh()
sc.center_offset = (4.0, 4.0, 4.0)
bpy.ops.view3d.cut_plane_reset()
res = bpy.ops.view3d.cut_plane_undo()
check("undo restores the pre-Reset pose",
      res == {'FINISHED'} and center() == (4.0, 4.0, 4.0), f"center={center()}")

# --- 7. hygiene ---------------------------------------------------------------
fresh()
before = pose_history.capture_pose(bpy.context)
pose_history.push_if_changed(bpy.context, before)
check("unchanged pose pushes nothing", not pose_history._undo_stack)
for i in range(70):
    simulate_move((float(i + 1), 1.0, 1.0))
check("history capped at 64", len(pose_history._undo_stack) <= 64,
      f"{len(pose_history._undo_stack)} entries")

check("cache invariants clean", not cutting.check_invariants(),
      str(cutting.check_invariants()))

print("\nPOSE UNDO:", "OK" if not failures else f"FAIL ({len(failures)}): {failures}")
sys.exit(1 if failures else 0)
