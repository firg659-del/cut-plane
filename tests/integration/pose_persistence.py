# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless test: WM tool-state persistence across .blend save/load.

    blender --background --factory-startup --python tests/integration/pose_persistence.py

The tool state lives on the WindowManager (undo-proof - see redo_pose.py),
which Blender does not save into .blend files. persistence.py bridges that:
save_pre stamps the state into a scene ID property, load_post restores it,
and a file WITHOUT a stamp resets everything to defaults (a foreign file
must never inherit the previous file's plane).

Exit code 0 on success, 1 with the failed checks listed.
"""
import os
import sys
import tempfile

import bpy

ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

import cut_plane  # noqa: E402
from cut_plane import cutting  # noqa: E402
from cut_plane.lib import constants  # noqa: E402
from cut_plane.state import persistence  # noqa: E402

constants.DEBUG = True
cut_plane.register()

failures = []


def check(name, cond, detail=""):
    print(f"[{'ok  ' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


def props():
    return bpy.context.window_manager.cut_plane


def center():
    return tuple(round(v, 3) for v in props().center_offset)


# --- 1. restores skip update callbacks (no preview re-entry mid-load) ------
calls = []
orig = cutting.update_live_preview
cutting.update_live_preview = lambda ctx: calls.append(1)
try:
    persistence._apply_state(props(), {"cut_use_fill": True,
                                       "cut_selected_only": True})
finally:
    cutting.update_live_preview = orig
check("restore skips the option update callbacks", not calls,
      f"{len(calls)} preview calls")
check("...but the values landed",
      props().cut_use_fill and props().cut_selected_only)
persistence._apply_state(props(), persistence._defaults_state(props()))
check("defaults state resets what it changed",
      not props().cut_use_fill and not props().cut_selected_only)

# --- 1b. default state stamps NOTHING (file hygiene) ------------------------
# A user who never posed the plane must save .blend files carrying no trace
# of this addon, and resetting the pose must UN-stamp the file on its next
# save. (The is-default comparison is float32-tolerant: RNA floats are
# single precision, so exact equality against Python-double defaults would
# never hold for non-representable values.)
path0 = os.path.join(tempfile.mkdtemp(prefix="cut_plane_clean_"), "clean.blend")
bpy.ops.wm.save_mainfile(filepath=path0)
check("default state writes NO scene stamp",
      bpy.context.scene.get(persistence._STATE_PROP) is None)
props().x_offset = 2.0
bpy.ops.wm.save_mainfile(filepath=path0)
check("posed state DOES stamp the scene",
      bpy.context.scene.get(persistence._STATE_PROP) is not None)
props().x_offset = 0.0
bpy.ops.wm.save_mainfile(filepath=path0)
check("reset pose UN-stamps the file on the next save",
      bpy.context.scene.get(persistence._STATE_PROP) is None)

# --- 2. save/load round-trip ------------------------------------------------
# Distinctive values across every property kind: vector, float, enum,
# enum-flag, bool. Rotation stays identity so the orientation flip's basis
# change is a no-op and the offsets survive verbatim.
p = props()
p.center_offset = (1.25, -2.5, 0.75)
p.x_offset = 3.0
p.mode = 'SCALE'
p.orientation = 'LOCAL'
p.cut_use_fill = True
p.snap_elements = {'VERTEX', 'EDGE'}
p.snap_angle_increment = 0.123

path = os.path.join(tempfile.mkdtemp(prefix="cut_plane_persist_"), "state.blend")
bpy.ops.wm.save_mainfile(filepath=path)
check("save_pre stamped the scene",
      bpy.context.scene.get(persistence._STATE_PROP) is not None)

# Trash the live state, then load the file back - load_post must restore
p.center_offset = (9.0, 9.0, 9.0)
p.x_offset = 0.0
p.mode = 'MOVE'
p.orientation = 'GLOBAL'
p.cut_use_fill = False
p.snap_elements = {'INCREMENT'}
bpy.ops.wm.open_mainfile(filepath=path)

p = props()
check("vector restored", center() == (1.25, -2.5, 0.75), f"center={center()}")
check("float restored", round(p.x_offset, 3) == 3.0, f"x_offset={p.x_offset}")
check("enum restored", p.mode == 'SCALE' and p.orientation == 'LOCAL',
      f"mode={p.mode} orientation={p.orientation}")
check("enum-flag set restored", p.snap_elements == {'VERTEX', 'EDGE'},
      f"snap_elements={p.snap_elements}")
check("bool restored", p.cut_use_fill is True)
check("float knob restored", abs(p.snap_angle_increment - 0.123) < 1e-6)

runtime = bpy.context.window_manager.cut_plane_runtime
check("runtime trackers reset on load",
      runtime.last_orientation == '' and runtime.centered_edit_uid == 0,
      f"last_orientation={runtime.last_orientation!r}")

# --- 3. stampless file -> defaults (no cross-file leakage) ------------------
bpy.ops.wm.read_homefile(use_empty=True)
p = props()
check("fresh file resets the pose", center() == (0.0, 0.0, 0.0)
      and p.x_offset == 0.0 and p.mode == 'MOVE' and p.orientation == 'GLOBAL',
      f"center={center()} mode={p.mode}")
check("fresh file resets options + snap",
      not p.cut_use_fill and p.snap_elements == {'INCREMENT'})

# --- 4. corrupt stamp -> defaults, not a crash -------------------------------
p.center_offset = (5.0, 5.0, 5.0)
bpy.context.scene[persistence._STATE_PROP] = "{this is not json"
persistence._load_tool_state()
check("corrupt stamp degrades to defaults", center() == (0.0, 0.0, 0.0),
      f"center={center()}")

check("cache invariants clean", not cutting.check_invariants(),
      str(cutting.check_invariants()))

# --- a stamp field that cannot be applied must RESET, not leak -------------
# A stamp is external data: hand-edited, or written by a different addon
# version whose enum identifiers have since been renamed. _load_tool_state
# seeds every field with its default precisely so an unsupplied field resets
# rather than inheriting the previously open file's value - but _apply_state
# silently SKIPPED a present-but-invalid value, which kept the live (i.e.
# previous file's) value instead. Missing keys always reset correctly, so the
# defect only showed with a present-but-invalid one.
import json  # noqa: E402


def stamp_with(overrides):
    """Write a valid stamp into the scene, then corrupt chosen fields."""
    state = json.loads(persistence._serialize(props()))
    state.update(overrides)
    bpy.context.scene[persistence._STATE_PROP] = json.dumps(state)


def default_of(ident):
    return persistence._default_for(props().bl_rna.properties[ident])


# Establish a "previous file" state that differs from the defaults.
p = props()
p.orientation = 'LOCAL'
p.mode = 'SCALE'
p.x_offset = 7.5
prev = (p.orientation, p.mode, p.x_offset)
check("the previous-file state differs from the defaults (fixture is valid)",
      prev != (default_of('orientation'), default_of('mode'),
               default_of('x_offset')),
      str(prev))

# A stamp whose enum identifiers no longer exist and whose float is garbage.
stamp_with({"orientation": "DEFAULT_OLD", "mode": "MOVE_OLD",
            "x_offset": "not-a-number"})
persistence._load_tool_state()

p = props()
check("REGRESSION: an invalid enum resets to its default, not the last file's",
      p.orientation == default_of('orientation'),
      f"got {p.orientation!r}, previous file had {prev[0]!r}")
check("REGRESSION: a second invalid enum resets too",
      p.mode == default_of('mode'), f"got {p.mode!r}, previous had {prev[1]!r}")
check("REGRESSION: an invalid float resets to its default",
      abs(p.x_offset - default_of('x_offset')) < 1e-6,
      f"got {p.x_offset}, previous had {prev[2]}")

# A VALID stamp must still restore faithfully - the reset must not overreach.
p.orientation = 'LOCAL'
p.mode = 'SCALE'
p.x_offset = 3.25
stamp_with({})
p.orientation = 'GLOBAL'
p.mode = 'MOVE'
p.x_offset = 0.0
persistence._load_tool_state()
p = props()
check("a VALID stamp still restores every field (no over-eager reset)",
      (p.orientation, p.mode, round(p.x_offset, 4)) == ('LOCAL', 'SCALE', 3.25),
      f"{(p.orientation, p.mode, round(p.x_offset, 4))}")

print("\nPOSE PERSISTENCE:",
      "OK" if not failures else f"FAIL ({len(failures)}): {failures}")
sys.exit(1 if failures else 0)
