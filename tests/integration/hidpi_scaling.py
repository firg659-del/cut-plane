# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless test: the HiDPI scaling contract (Retina / UI-scale parity).

    blender --background --factory-startup --python-exit-code 1 \
            --python tests/integration/hidpi_scaling.py

Contract (full diagnosis 2026-07; measured on Windows + fetched Blender
source): Blender scales element SIZES/LAYOUT by U.scale_factor
(system.ui_scale) and line WEIGHTS / marker RADII by U.pixelsize
(system.pixel_size); region and mouse coordinates are PHYSICAL pixels.
Additionally, macOS's Metal backend stores plain line widths and never
applies them - Python batch lines rasterize as hairlines - so every wide
line must go through the POLYLINE shader with an explicit lineWidth uniform.

What that means for this addon:
  * gizmo offsets / arc radius: x ui_scale_factor()   (layout class)
  * snap marker radius / line uniforms: x pixel_size_factor()  (weight class)
  * NO gpu.state.line_width_set anywhere (Metal ignores it; and the C
    front-end multiplies it by U.pixelsize, so adding the factor there
    would double-scale on OpenGL/Vulkan)
  * interaction geometry (snap reach, grid floor, deadzone, trackball
    sensitivity): logical, so x (or /) ui_scale_factor()
  * native gizmo scale_basis values: NEVER scaled by us (the C pipeline
    already applies UI_SCALE_FAC x gizmo_size)

Draw callbacks cannot execute headless (no GPU context), so this suite
locks the helpers' behavior plus the structure of every fix site - each
check fails if its factor is removed (mutation-tested).

Exit code 0 on success, 1 with the failed checks listed.
"""
import inspect
import os
import sys

import bpy

ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

import cut_plane  # noqa: E402
from cut_plane.lib import math_utils  # noqa: E402
from cut_plane.ui import drawing, gizmos  # noqa: E402
from cut_plane.operators import rotate, snapping  # noqa: E402

cut_plane.register()

failures = []


def check(name, cond, detail=""):
    print(f"[{'ok  ' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


def src(fn):
    return inspect.getsource(fn)


# --- the helpers and their headless guards ---------------------------------
# system.ui_scale reads 0.0 in --background (no window, no DPI machinery);
# an unguarded read would zero every offset in this very battery.
check("headless fixture: system.ui_scale really is 0.0 here",
      bpy.context.preferences.system.ui_scale == 0.0,
      str(bpy.context.preferences.system.ui_scale))
check("ui_scale_factor guards the headless 0.0 to 1.0",
      math_utils.ui_scale_factor() == 1.0)
check("pixel_size_factor is sane headless",
      math_utils.pixel_size_factor() >= 1.0,
      str(math_utils.pixel_size_factor()))

# --- no plain line widths anywhere (the Metal hairline + double-scale trap) -
import pathlib  # noqa: E402

offenders = []
for f in pathlib.Path(ADDON, "cut_plane").rglob("*.py"):
    for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
        if "line_width_set(" in line and not line.strip().startswith("#"):
            offenders.append(f"{f.name}:{n}")
check("ZERO gpu.state.line_width_set calls remain (POLYLINE-only lines)",
      not offenders, str(offenders))

# --- layout class: offsets and radii x ui_scale ----------------------------
s = src(gizmos.CutPlaneGroup._draw_prepare_scale_mode)
check("SCALE-mode gizmo pushes scale with ui_scale",
      "ui_scale_factor()" in s and "ARROW_OFFSET_PIXELS" in s)
s = src(gizmos.CutPlaneGroup._draw_prepare_move_mode)
check("MOVE-mode gizmo pushes scale with ui_scale",
      "ui_scale_factor()" in s and "ARROW_OFFSET_PIXELS" in s)
s = src(drawing._draw_arc_3d)
check("rotation-arc radius scales with ui_scale",
      "ui_scale_factor()" in s and "ARC_RADIUS_PIXELS" in s)
s = src(drawing._draw_active_rotation_arc)
check("snap-tick radius scales with ui_scale",
      "ui_scale_factor()" in s and "ARC_RADIUS_PIXELS" in s)

# --- weight class: marker radius and line uniforms x pixel_size ------------
s = src(drawing._draw_snap_marker_3d)
check("snap-marker radius scales with pixel_size (native ED_view3d_pixel_size)",
      "vertex_size * pixel_size_factor()" in s)
check("snap-marker outlines go through POLYLINE with a closed LINE_STRIP",
      "get_polyline_shader" in s and "LINE_STRIP" in s
      and "lineWidth" in s)
s = src(drawing.draw_plane_at_matrix)
check("plane outline: POLYLINE with lineWidth x pixel_size",
      "get_polyline_shader" in s
      and "PLANE_OUTLINE_WIDTH * pixel_size_factor()" in s)
s = src(drawing._draw_helpline_3d)
check("helplines: POLYLINE with lineWidth x pixel_size",
      "get_polyline_shader" in s and "pixel_size_factor()" in s)
s = src(drawing._draw_snap_ticks_3d)
check("snap ticks: POLYLINE at exactly pixel_size (the old double-scale is gone)",
      "get_polyline_shader" in s
      and 'uniform_float("lineWidth", pixel_size_factor())' in s)

# --- interaction geometry: logical sizes -----------------------------------
s = src(snapping.snap_to_geometry)
check("snap capture radius scales with ui_scale",
      "SNAP_PIXEL_RADIUS * ui_scale_factor()" in s)
s = src(snapping.get_move_increment)
check("grid-step pixel floor scales with ui_scale",
      "GRID_STEP_MIN_PIXELS * ui_scale_factor()" in s)
s = src(rotate.CutPlaneRotateBaseOperator._deadzone_2d_sq)
check("rotation deadzone scales with ui_scale SQUARED",
      "f * f" in s)
_rotate_src = pathlib.Path(ADDON, "cut_plane", "operators",
                           "rotate.py").read_text(encoding="utf-8")
check("...and every comparison site uses the method, not the raw constant",
      _rotate_src.count("self._deadzone_2d_sq()") >= 4
      and "< self.DISTANCE_THRESHOLD_2D_SQ" not in _rotate_src
      and ">= self.DISTANCE_THRESHOLD_2D_SQ" not in _rotate_src,
      f"{_rotate_src.count('self._deadzone_2d_sq()')} sites")
s = src(rotate.CutPlaneTrackballOperator.handle_modal_event)
check("trackball sensitivity normalizes by ui_scale",
      "TRACKBALL_SENSITIVITY / ui_scale_factor()" in s)

# --- must-NOT-scale list stays untouched -----------------------------------
gs = pathlib.Path(gizmos.__file__).read_text(encoding="utf-8")  # layout-proof
check("native gizmo scale_basis values are NOT multiplied by any factor",
      "scale_basis = GIZMO_PLANE_SCALE" in gs
      and "scale_basis * ui_scale" not in gs
      and "scale_basis * pixel_size" not in gs)
ms = src(drawing._draw_constraint_lines_impl) if hasattr(drawing, "_draw_constraint_lines_impl") \
    else pathlib.Path(drawing.__file__).read_text(encoding="utf-8")  # layout-proof
check("the constraint line (the reference implementation) is unchanged",
      "_CONSTRAINT_LINE_WIDTH * context.preferences.system.pixel_size" in ms)

# the drag-sensitivity cancellation must stay factor-free (units cancel)
mv = pathlib.Path(ADDON, "cut_plane", "operators", "move.py").read_text(encoding="utf-8")
check("move drag sensitivity stays unscaled (world-per-px x px cancels)",
      "ui_scale_factor" not in mv)

print(f"\nHIDPI SCALING: {'FAIL - ' + ', '.join(failures) if failures else 'OK'}")
sys.stdout.flush()
os._exit(1 if failures else 0)
