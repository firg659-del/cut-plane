# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the pure snapping helpers (operators/snapping.py).

Needs real ``mathutils``; skipped on a bare interpreter (run under Blender).
Only the geometry-free helpers are covered here - the cursor ray-cast paths
need a live viewport and are exercised by hand.
"""
import pytest

pytest.importorskip("mathutils")

from mathutils import Vector  # noqa: E402

from cut_plane.operators.snapping import round_vector_to_step  # noqa: E402


def test_round_vector_to_step_rounds_each_component():
    out = round_vector_to_step(Vector((1.2, 2.7, -0.4)), 0.5)
    assert out.x == pytest.approx(1.0)
    assert out.y == pytest.approx(2.5)
    assert out.z == pytest.approx(-0.5)


def test_round_vector_to_step_unit_step():
    out = round_vector_to_step(Vector((1.49, 1.51, -2.5)), 1.0)
    assert out.x == pytest.approx(1.0)
    assert out.y == pytest.approx(2.0)


def test_round_vector_to_step_non_positive_returns_copy():
    vec = Vector((1.3, 2.6, 3.9))
    out = round_vector_to_step(vec, 0.0)
    assert out == vec
    assert out is not vec  # a copy, not the same object
