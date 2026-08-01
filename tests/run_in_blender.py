# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the full test suite under Blender's bundled Python.

The math/snapping tests need real ``mathutils`` (and the round-trip ones a real
linear-algebra backend), which only exists inside Blender. This launches pytest
against the whole tests/ folder using whatever interpreter is running it.

Usage (one-time: make sure pytest is in Blender's Python):
    "<blender>/<ver>/python/bin/python(.exe)" -m ensurepip
    "<blender>/<ver>/python/bin/python(.exe)" -m pip install pytest

Then either run headless through Blender:
    blender --background --python tests/run_in_blender.py
or directly with Blender's Python (faster, no .blend loaded):
    "<blender>/<ver>/python/bin/python(.exe)" -m pytest tests -q
"""
import pathlib
import sys

try:
    import pytest
except ImportError:
    sys.stderr.write(
        "pytest is not installed in this Python.\n"
        "Install it into Blender's Python first - see the header of this file.\n")
    raise SystemExit(1)

_TESTS_DIR = pathlib.Path(__file__).resolve().parent
raise SystemExit(pytest.main([str(_TESTS_DIR), "-q"]))
