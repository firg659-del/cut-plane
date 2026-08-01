# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for math_utils.

These need real ``mathutils`` (Matrix/Vector), which only exists inside
Blender's Python - a fake would defeat the purpose. On a bare interpreter the
whole module is skipped; run it under Blender (see tests/README.md).

The orientation helpers (``orientation_with_rotate_override`` /
``effective_orientation``) are the single source of the "ROTATE positions in
world space" invariant shared by the gizmo group, the depsgraph handler, and the
cut plane - these tests lock that rule down.
"""
from types import SimpleNamespace

import pytest

pytest.importorskip("mathutils")

from mathutils import Matrix, Vector  # noqa: E402

from cut_plane.lib.math_utils import (  # noqa: E402
    build_dial_basis,
    calculate_alpha_fade,
    effective_orientation,
    flat_to_matrix,
    get_current_orientation_type,
    get_plane_pose,
    matrix_to_flat,
    orientation_with_rotate_override,
    transform_offset_to_world,
)


# --- the ROTATE-orientation invariant (2b) --------------------------------

@pytest.mark.parametrize("orientation,mode,expected", [
    ("LOCAL", "ROTATE", "GLOBAL"),   # ROTATE always collapses to world space
    ("GLOBAL", "ROTATE", "GLOBAL"),
    ("LOCAL", "MOVE", "LOCAL"),      # non-ROTATE passes through unchanged
    ("LOCAL", "SCALE", "LOCAL"),
    ("GLOBAL", "MOVE", "GLOBAL"),
])
def test_orientation_with_rotate_override(orientation, mode, expected):
    assert orientation_with_rotate_override(orientation, mode) == expected


def _ctx(orientation="GLOBAL", mode="MOVE"):
    # Tool state lives on the WindowManager (undo-proof), not the Scene
    return SimpleNamespace(
        window_manager=SimpleNamespace(
            cut_plane=SimpleNamespace(orientation=orientation, mode=mode)))


def test_effective_orientation_rotate_forces_global():
    assert effective_orientation(_ctx("LOCAL", "ROTATE")) == "GLOBAL"


def test_effective_orientation_passes_through_outside_rotate():
    assert effective_orientation(_ctx("LOCAL", "MOVE")) == "LOCAL"
    assert effective_orientation(_ctx("GLOBAL", "SCALE")) == "GLOBAL"


def test_get_current_orientation_type():
    assert get_current_orientation_type(_ctx("LOCAL")) == "LOCAL"
    assert get_current_orientation_type(_ctx("GLOBAL")) == "GLOBAL"
    # No cut_plane property at all -> safe default
    assert get_current_orientation_type(
        SimpleNamespace(window_manager=SimpleNamespace())) == "GLOBAL"


# --- plane pose (THE placement formula) ------------------------------------

def _pose_ctx(*, orientation="GLOBAL", mode="MOVE", off=(0.0, 0.0, 0.0),
              center=(0.0, 0.0, 0.0), rot_flat=None):
    identity = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)
    return SimpleNamespace(
        edit_object=None,  # origin falls back to (0,0,0)
        window_manager=SimpleNamespace(cut_plane=SimpleNamespace(
            orientation=orientation, mode=mode,
            x_offset=off[0], y_offset=off[1], z_offset=off[2],
            center_offset=center,
            rotation_matrix_flat=rot_flat or identity)))


def test_plane_pose_none_without_cut_plane():
    assert get_plane_pose(
        SimpleNamespace(window_manager=SimpleNamespace())) is None


def test_plane_pose_global_adds_offsets_directly():
    pos, rot = get_plane_pose(_pose_ctx(off=(1, 2, 3), center=(0.5, 0, 0)))
    assert pos == Vector((1.5, 2.0, 3.0))
    assert rot == Matrix.Identity(4)


def test_plane_pose_local_rotates_offset():
    # 90 deg about Z: local +X offset lands on world +Y
    rot90 = Matrix.Rotation(1.5707963, 4, "Z")
    flat = tuple(rot90[r][c] for r in range(4) for c in range(4))
    pos, _rot = get_plane_pose(
        _pose_ctx(orientation="LOCAL", off=(1, 0, 0), rot_flat=flat))
    assert pos.x == pytest.approx(0.0, abs=1e-6)
    assert pos.y == pytest.approx(1.0, abs=1e-6)


def test_plane_pose_rotate_mode_ignores_local_orientation():
    # ROTATE collapses to GLOBAL: local offset applied unrotated
    rot90 = Matrix.Rotation(1.5707963, 4, "Z")
    flat = tuple(rot90[r][c] for r in range(4) for c in range(4))
    pos, _rot = get_plane_pose(
        _pose_ctx(orientation="LOCAL", mode="ROTATE", off=(1, 0, 0), rot_flat=flat))
    assert pos.x == pytest.approx(1.0)
    assert pos.y == pytest.approx(0.0, abs=1e-6)


# --- alpha fade (pure smoothstep) -----------------------------------------

def test_alpha_fade_below_min_is_zero():
    assert calculate_alpha_fade(0.0) == 0.0


def test_alpha_fade_above_max_is_one():
    assert calculate_alpha_fade(0.2) == 1.0


def test_alpha_fade_midpoint_is_half():
    # Default thresholds 0.02..0.10; midpoint 0.06 -> smoothstep(0.5) == 0.5
    assert calculate_alpha_fade(0.06) == pytest.approx(0.5)


# --- matrix <-> flat round-trip -------------------------------------------

def test_flat_to_matrix_identity():
    flat = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)
    assert flat_to_matrix(flat) == Matrix.Identity(4)


def test_matrix_flat_round_trip():
    flat = tuple(float(i) for i in range(16))
    assert matrix_to_flat(flat_to_matrix(flat)) == pytest.approx(flat)


def test_flat_matrix_round_trip_from_rotation():
    m = Matrix.Rotation(0.7, 4, "Z") @ Matrix.Translation((1.0, 2.0, 3.0))
    back = flat_to_matrix(matrix_to_flat(m))
    for r in range(4):
        for c in range(4):
            assert back[r][c] == pytest.approx(m[r][c])


# --- dial basis ------------------------------------------------------------

@pytest.mark.parametrize("axis", [
    Vector((0.0, 0.0, 1.0)),
    Vector((1.0, 0.0, 0.0)),
    Vector((1.0, 2.0, 3.0)),
])
def test_build_dial_basis_is_orthonormal(axis):
    x_axis, y_axis = build_dial_basis(axis)
    z = axis.normalized()
    assert x_axis.length == pytest.approx(1.0)
    assert y_axis.length == pytest.approx(1.0)
    assert x_axis.dot(y_axis) == pytest.approx(0.0, abs=1e-6)
    assert x_axis.dot(z) == pytest.approx(0.0, abs=1e-6)
    assert y_axis.dot(z) == pytest.approx(0.0, abs=1e-6)


# --- offset transform ------------------------------------------------------

def test_transform_offset_global_is_passthrough_copy():
    offset = Vector((1.0, 2.0, 3.0))
    out = transform_offset_to_world(offset, "GLOBAL", Matrix.Rotation(1.0, 4, "Z"))
    assert out == offset
    assert out is not offset  # returns a copy, never the original


def test_transform_offset_local_applies_rotation():
    offset = Vector((1.0, 0.0, 0.0))
    rot = Matrix.Rotation(1.5707963, 4, "Z")  # ~90 deg about Z: X -> Y
    out = transform_offset_to_world(offset, "LOCAL", rot)
    assert out.x == pytest.approx(0.0, abs=1e-6)
    assert out.y == pytest.approx(1.0, abs=1e-6)
