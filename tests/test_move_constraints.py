# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the keyboard-constraint cycling state machine (G -> X/Y/Z).

Importing operators.move pulls in mathutils (via cutting), so this runs under
Blender's Python and skips on a bare interpreter, like the math suite.
"""
import pytest

pytest.importorskip("mathutils")

from mathutils import Matrix, Vector  # noqa: E402

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


def test_closest_point_parallel_ray_is_unusable():
    # Ray parallel to the line: no unique closest point. The old fallback
    # projected the ray origin, which teleported the plane; callers now keep
    # their last value on None (this suite only runs inside Blender, so the
    # stale expectation went unnoticed).
    p = _ray_line_closest_point(
        Vector((4.0, 3.0, 0.0)), Vector((1.0, 0.0, 0.0)),
        Vector((0.0, 0.0, 0.0)), Vector((1.0, 0.0, 0.0)))
    assert p is None


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


# --- Shift precision drives a VIRTUAL cursor (native) ----------------------
# Native Blender applies Precision to the mouse: the cursor it maps advances
# at 10% while Shift is held. The move used to scale the result in world
# space instead and map the REAL cursor - at a grazing view the plane lock's
# ray-plane mapping races toward the horizon, so a Shift+Z drag ran ~2x ahead
# of native and kept accelerating (reported: "the plane runs away").

def _grazing_ray(mouse):
    """A perspective camera 6 degrees above the XY plane, looking at the
    origin; 0.002 rad per pixel around the region centre (500, 300)."""
    import math
    eye = Vector((0.0, -10.0, 10.0 * math.tan(math.radians(6.0))))
    forward = (-eye).normalized()
    right = forward.cross(Vector((0.0, 0.0, 1.0))).normalized()
    up = right.cross(forward)
    d = forward + right * ((mouse[0] - 500) * 0.002) + up * ((mouse[1] - 300) * 0.002)
    return eye, d.normalized()


def _plane_hit(mouse):
    eye, d = _grazing_ray(mouse)
    return eye + d * (-eye.z / d.z)


def _drive_plane_lock(monkeypatch):
    from types import SimpleNamespace

    from cut_plane.operators import move

    monkeypatch.setattr(move, 'resolve_translation_snap',
                        lambda context, event, op, props, anchor, raw_delta,
                        axis=None, plane_normal=None: raw_delta)
    methods = {k: v for k, v in vars(move.CutPlaneMoveCenterOperator).items()
               if callable(v) and not k.startswith('__')}
    methods['_mouse_ray'] = lambda self, context, event, mouse=None: _grazing_ray(
        mouse if mouse is not None else (event.mouse_region_x, event.mouse_region_y))
    op = type('FakeMove', (), methods)()
    op.init_mouse_x, op.init_mouse_y = 500, 300
    op.start_pos_world = Vector((0.0, 0.0, 0.0))
    op.init_center_offset = Vector((0.0, 0.0, 0.0))
    op.accumulated_delta = Vector((0.0, 0.0, 0.0))
    op._cached_rotation_matrix = Matrix.Identity(4)
    op._kb_constraint = ('PLANE', 'Z', 'GLOBAL')      # Shift+Z: lock the XY plane
    op._kb_ref, op._kb_base, op._kb_switched = None, Vector((0.0, 0.0, 0.0)), True
    op._vm_prev, op._vm_offset = Vector((500.0, 300.0)), Vector((0.0, 0.0))
    props = SimpleNamespace(center_offset=None)
    ctx = SimpleNamespace(area=None, region=None, region_data=None)

    def ev(x, y, shift):
        return SimpleNamespace(mouse_region_x=x, mouse_region_y=y, shift=shift)

    op._apply_free_move(ctx, ev(500, 300, True), props)   # the Shift+Z press
    for i in range(1, 6):                                  # Shift still held
        op._apply_free_move(ctx, ev(500 + 20 * i, 300 + 6 * i, True), props)
    for i in range(1, 6):                                  # Shift released
        op._apply_free_move(ctx, ev(600 + 20 * i, 330 + 6 * i, False), props)
    return Vector(props.center_offset)


def test_shift_precision_maps_a_virtual_cursor(monkeypatch):
    from cut_plane.lib.constants import MOVE_PRECISION_FAC
    got = _drive_plane_lock(monkeypatch)
    f = MOVE_PRECISION_FAC
    virtual = (500 + 100 * f + 100, 300 + 30 * f + 30)
    native = _plane_hit(virtual) - _plane_hit((500, 300))
    assert (got - native).length < 1e-4 * max(native.length, 1.0)
    # ...and the old world-space rule lands clearly elsewhere at this view,
    # so this test can tell the two apart.
    real = _plane_hit((700, 360)) - _plane_hit((500, 300))
    held = _plane_hit((600, 330)) - _plane_hit((500, 300))
    old = real - held * (1.0 - f)
    assert (old - native).length > 0.1 * native.length
