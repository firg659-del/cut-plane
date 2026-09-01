# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless test: warn when an enabled Mirror adds no cut.

    blender --background --factory-startup --python tests/integration/mirror_hint.py

Mirror is CORRECT when it reflects a plane onto itself and produces one cut -
there is no second cut to make. It is just silent about it, and at the DEFAULT
plane pose every combination is in that state (measured: all seven of
X/Y/Z/XY/XZ/YZ/XYZ give exactly one cut). So ticking Mirror does nothing, with
no feedback, which reads as a broken feature.

This rides the existing _edge_case_warning channel - "states where the tool
silently does nothing" - already drawn in the tool header AND the N-panel, so
it adds no UI and changes no behaviour.

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
from cut_plane.lib import constants, math_utils  # noqa: E402
from cut_plane.ui.tool import CutPlaneTool  # noqa: E402

constants.DEBUG = True
cut_plane.register()

failures = []


def check(name, cond, detail=""):
    print(f"[{'ok  ' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete()
bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
obj = bpy.context.object
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.mesh.select_all(action='DESELECT')

props = bpy.context.window_manager.cut_plane
props.x_offset = props.y_offset = props.z_offset = 0.0
props.center_offset = math_utils.default_center_offset(obj)
props.mirror_x = props.mirror_y = props.mirror_z = False
props.cut_selected_only = False


def warning():
    return CutPlaneTool._edge_case_warning(bpy.context, props)


def expect(axes):
    """The message for a set of inert axis names."""
    return "Mirror %s %s no cut here" % (
        ", ".join(axes), "adds" if len(axes) == 1 else "add")


check("no mirror enabled -> no warning", warning() is None, str(warning()))

# --- the default pose: every enabled axis is degenerate, and each is NAMED --
for name, (x, y, z), inert in (
        ("X", (1, 0, 0), ["X"]), ("Y", (0, 1, 0), ["Y"]), ("Z", (0, 0, 1), ["Z"]),
        ("XY", (1, 1, 0), ["X", "Y"]), ("XZ", (1, 0, 1), ["X", "Z"]),
        ("YZ", (0, 1, 1), ["Y", "Z"]), ("XYZ", (1, 1, 1), ["X", "Y", "Z"])):
    props.mirror_x, props.mirror_y, props.mirror_z = bool(x), bool(y), bool(z)
    check(f"default pose, Mirror {name:<3} -> names {'+'.join(inert)}",
          warning() == expect(inert), str(warning()))

# --- move the plane: the mirror ALONG the normal now does something --------
props.z_offset = 0.5
props.mirror_x = props.mirror_y = False
props.mirror_z = True
check("plane moved off centre, Mirror Z -> NO warning (it cuts twice)",
      warning() is None, str(warning()))

# ...but the perpendicular axes still cannot contribute, at any position.
props.mirror_z = False
props.mirror_x = True
check("plane moved, Mirror X on a Z-facing plane -> still names X",
      warning() == expect(["X"]), str(warning()))

# --- the case that matters: SOME axes inert, so the total is not 1 ---------
# Three axes on, an axis-aligned plane moved off centre: you get TWO cuts,
# and the two perpendicular toggles are doing nothing. The total alone would
# have said everything was fine.
props.z_offset = 0.5
props.mirror_x = props.mirror_y = props.mirror_z = True
check("plane moved on Z, all three axes on -> names the two inert ones",
      warning() == "Mirror X, Y add no cut here", str(warning()))
props.mirror_y = False
check("...and with only X and Z on, names just X",
      warning() == "Mirror X adds no cut here", str(warning()))
props.mirror_x = False
props.mirror_y = props.mirror_z = False
props.mirror_z = True
check("...and with only the productive axis on, stays quiet",
      warning() is None, str(warning()))
props.z_offset = 0.0

# --- a more serious warning must still win ---------------------------------
props.mirror_x = props.mirror_y = props.mirror_z = True
props.z_offset = 0.0
props.cut_selected_only = True
w = warning()
check("REGRESSION: 'Nothing selected to cut' outranks the mirror hint",
      w == "Nothing selected to cut", str(w))
props.cut_selected_only = False

props.mirror_x = props.mirror_y = props.mirror_z = False
props.z_offset = 0.0
cutting.restore_live_cut(bpy.context)
bpy.ops.object.mode_set(mode='OBJECT')

print("\nMIRROR HINT:", "OK" if not failures else f"FAIL ({len(failures)}): {failures}")
sys.exit(1 if failures else 0)
