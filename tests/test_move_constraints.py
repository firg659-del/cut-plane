# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the keyboard-constraint cycling state machine (G -> X/Y/Z).

Importing operators.move pulls in mathutils (via cutting), so this runs under
Blender's Python and skips on a bare interpreter, like the math suite.
"""
import pytest

pytest.importorskip("mathutils")

from mathutils import Vector  # noqa: E402

from cut_plane.operators.move import _ray_line_closest_point  # noqa: E402
from cut_plane.operators.utils import next_kb_constraint  # noqa: E402


# --- ray-line closest point: SIGN-LOCKED -----------------------------------
# The parametric solve historically returned the point MIRRORED through
# line_point (negated parameter); the arrow operator compensated with a second
# inversion, and the keyboard constraint then moved opposite to the mouse.
# These tests pin the true closest point so neither sign bug can return.

def test_closest_point_straight_down_ray():
    # Camera above (5, 0, 10) looking straight down at the X axis:
    # the closest point on the axis is (5, 0, 0) - NOT (-5, 0, 0).
    p = _ray_line_closest_point(
        Vector((5.0, 0.0, 10.0)), Vector((0.0, 0.0, -1.0)),
        Vector((0.0, 0.0, 0.0)), Vector((1.0, 0.0, 0.0)))
    assert p.x == pytest.approx(5.0)
    assert p.y == pytest.approx(0.0)
    assert p.z == pytest.approx(0.0)


def test_closest_point_moves_with_the_ray():
    # Moving the ray +X moves the closest point +X (cursor tracking direction)
    def closest(x):
        return _ray_line_closest_point(
            Vector((x, 2.0, 10.0)), Vector((0.0, -0.1, -1.0)).normalized(),
            Vector((0.0, 0.0, 0.0)), Vector((1.0, 0.0, 0.0)))
    assert closest(3.0).x > closest(1.0).x


def test_closest_point_parallel_ray_projects_origin():
    # Ray parallel to the line: fall back to projecting the ray origin
    p = _ray_line_closest_point(
        Vector((4.0, 3.0, 0.0)), Vector((1.0, 0.0, 0.0)),
        Vector((0.0, 0.0, 0.0)), Vector((1.0, 0.0, 0.0)))
    assert p.x == pytest.approx(4.0)
    assert p.y == pytest.approx(0.0)


def test_first_press_engages_global():
    assert next_kb_constraint(None, 'AXIS', 'X') == ('AXIS', 'X', 'GLOBAL')


def test_second_press_cycles_to_local():
    cur = ('AXIS', 'X', 'GLOBAL')
    assert next_kb_constraint(cur, 'AXIS', 'X') == ('AXIS', 'X', 'LOCAL')


def test_third_press_clears():
    cur = ('AXIS', 'X', 'LOCAL')
    assert next_kb_constraint(cur, 'AXIS', 'X') is None


def test_different_axis_switches_to_global():
    cur = ('AXIS', 'X', 'LOCAL')
    assert next_kb_constraint(cur, 'AXIS', 'Y') == ('AXIS', 'Y', 'GLOBAL')


def test_kind_change_restarts_cycle():
    # X held as axis, then Shift+X (plane lock) -> fresh GLOBAL plane constraint
    cur = ('AXIS', 'X', 'GLOBAL')
    assert next_kb_constraint(cur, 'PLANE', 'X') == ('PLANE', 'X', 'GLOBAL')


def test_plane_cycle_full():
    c = next_kb_constraint(None, 'PLANE', 'Z')
    assert c == ('PLANE', 'Z', 'GLOBAL')
    c = next_kb_constraint(c, 'PLANE', 'Z')
    assert c == ('PLANE', 'Z', 'LOCAL')
    assert next_kb_constraint(c, 'PLANE', 'Z') is None
