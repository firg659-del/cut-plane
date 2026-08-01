# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless test: typed values follow the scene unit settings, native-style.

    blender --background --factory-startup --python tests/integration/numeric_units.py

Bare numbers mean the scene's DISPLAY unit (a cm scene: "2" = 2 cm),
explicit suffixes always win, and results are converted from display space
to internal units via the scene's Unit Scale (display = internal *
scale_length). Rotation and unitless categories are unaffected.

Exit code 0 on success, 1 with the failed checks listed.
"""
import math
import os
import sys

import bpy

ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

from cut_plane.operators.numeric_input import parse_value  # noqa: E402

failures = []


def check(name, got, want, tol=1e-6):
    ok = got is not None and abs(got - want) < tol
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}  (got={got}, want={want})")
    if not ok:
        failures.append(name)


us = bpy.context.scene.unit_settings
ctx = bpy.context

# --- metric, meters display (the default) -----------------------------------
us.system = 'METRIC'
us.scale_length = 1.0
us.length_unit = 'METERS'
check("m display: bare number is meters", parse_value(ctx, 'LENGTH', "2"), 2.0)
check("m display: explicit cm", parse_value(ctx, 'LENGTH', "2cm"), 0.02)

# --- metric, centimeters display ---------------------------------------------
us.length_unit = 'CENTIMETERS'
check("cm display: bare number is centimeters",
      parse_value(ctx, 'LENGTH', "2"), 0.02)
check("cm display: explicit m still wins", parse_value(ctx, 'LENGTH', "2m"), 2.0)
check("cm display: expression mixes units",
      parse_value(ctx, 'LENGTH', "2cm+3mm"), 0.023)

# --- unit scale (display = internal * scale_length) --------------------------
us.length_unit = 'METERS'
us.scale_length = 0.01  # 1 internal unit displays as 1 cm
check("unit scale: typed 2m lands as 200 internal units",
      parse_value(ctx, 'LENGTH', "2"), 200.0, tol=1e-4)
us.scale_length = 1.0

# --- imperial -----------------------------------------------------------------
us.system = 'IMPERIAL'
us.length_unit = 'FEET'
check("ft display: bare number is feet",
      parse_value(ctx, 'LENGTH', "1"), 0.3048, tol=1e-4)
check("ft display: explicit inches",
      parse_value(ctx, 'LENGTH', '3"'), 0.0762, tol=1e-4)

# --- units off: plain Blender units -------------------------------------------
us.system = 'NONE'
check("units off: bare number is internal units",
      parse_value(ctx, 'LENGTH', "2"), 2.0)

# --- rotation / unitless unaffected by length settings ------------------------
us.system = 'METRIC'
us.length_unit = 'CENTIMETERS'
us.scale_length = 0.01
check("rotation: plain numbers stay degrees",
      parse_value(ctx, 'ROTATION', "45"), math.radians(45.0))
check("unitless: scale factors untouched", parse_value(ctx, 'NONE', "1.5"), 1.5)

print("\nNUMERIC UNITS:",
      "OK" if not failures else f"FAIL ({len(failures)}): {failures}")
sys.exit(1 if failures else 0)
