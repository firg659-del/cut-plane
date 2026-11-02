# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the numeric type-in state machine (operators/numeric_input.py).

Pure string/state logic - no Blender required (conftest stubs ``bpy``). The
offline ``bpy.utils.units.to_value`` stub raises, so :func:`parse_value` exercises
its ``float()`` fallback; unit-string / expression *evaluation* is Blender's job
and is intentionally not asserted here. What we test is the part we wrote: buffer
edits, sign/inverse toggles, mode transitions, backspace stages, and paste.
"""
from types import SimpleNamespace

import pytest

from cut_plane.operators.numeric_input import (
    MultiNumericInput,
    NumericInput,
    parse_value,
)


# --- helpers ---------------------------------------------------------------

def ev(*, value="PRESS", ctrl=False, type="NONE", unicode=""):
    """Minimal stand-in for a Blender modal event."""
    return SimpleNamespace(value=value, ctrl=ctrl, type=type, unicode=unicode)


def ctx():
    """Fake context exposing only what parse_value reads."""
    return SimpleNamespace(
        scene=SimpleNamespace(unit_settings=SimpleNamespace(system="METRIC")))


def type_str(ni, text):
    """Feed each character of *text* to a NumericInput as keypresses."""
    for ch in text:
        ni.handle_event(ev(unicode=ch))


# --- parse_value -----------------------------------------------------------

def test_parse_value_empty_is_none():
    assert parse_value(ctx(), "NONE", "") is None


def test_parse_value_plain_number_via_fallback():
    assert parse_value(ctx(), "NONE", "5") == 5.0
    assert parse_value(ctx(), "NONE", "-3.5") == -3.5


def test_parse_value_garbage_is_none():
    assert parse_value(ctx(), "NONE", "abc") is None


# --- NumericInput: basic typing -------------------------------------------

def test_starts_inactive():
    assert NumericInput().active is False


def test_non_press_events_ignored():
    ni = NumericInput()
    assert ni.handle_event(ev(value="RELEASE", unicode="5")) is False
    assert ni.buffer == ""


def test_digits_and_decimal_build_buffer():
    ni = NumericInput()
    type_str(ni, "12.5")
    assert ni.buffer == "12.5"
    assert ni.active is True
    assert ni.parse(ctx()) == 12.5


def test_comma_is_a_decimal_point_in_simple_mode():
    # Native numinput accepts the comma (European keyboards); it must build
    # the same buffer as a period and NOT flip into expression mode.
    ni = NumericInput()
    type_str(ni, "0,5")
    assert ni.buffer == "0.5"
    assert ni.expression is False
    assert ni.parse(ctx()) == 0.5


def test_sign_toggles_both_ways():
    ni = NumericInput()
    type_str(ni, "5")
    ni.handle_event(ev(unicode="-"))
    assert ni.buffer == "-5"
    assert ni.parse(ctx()) == -5.0
    ni.handle_event(ev(unicode="-"))
    assert ni.buffer == "5"


def test_inverse_toggle_and_parse():
    ni = NumericInput()
    type_str(ni, "4")
    ni.handle_event(ev(unicode="/"))
    assert ni.invert is True
    assert ni.parse(ctx()) == pytest.approx(0.25)
    assert ni.display() == "1/(4)"


def test_inverse_of_zero_is_none():
    ni = NumericInput()
    type_str(ni, "0")
    ni.handle_event(ev(unicode="/"))
    assert ni.parse(ctx()) is None  # 1/0 guarded


# --- NumericInput: units ---------------------------------------------------

def test_units_rejected_without_allow_units():
    ni = NumericInput(allow_units=False)
    type_str(ni, "5")
    assert ni.handle_event(ev(unicode="c")) is False
    assert ni.buffer == "5"


def test_units_accepted_with_allow_units():
    ni = NumericInput(allow_units=True)
    type_str(ni, "25")
    assert ni.handle_event(ev(unicode="c")) is True
    ni.handle_event(ev(unicode="m"))
    assert ni.buffer == "25cm"


def test_axis_keys_and_space_are_not_unit_suffixes():
    # 'G 2 X' must reach the modal as an X constraint and Space as confirm
    # (native simple mode takes digits only); real unit letters still work.
    ni = NumericInput(allow_units=True)
    type_str(ni, "2")
    for ch in ("x", "X", "y", "Z", " "):
        assert ni.handle_event(ev(unicode=ch)) is False
    assert ni.buffer == "2"
    assert ni.handle_event(ev(unicode="c")) is True


def test_period_keys_insert_a_point_in_every_mode():
    # Comma-decimal layouts: the numpad key types ','. Native forces '.'.
    ni = NumericInput()
    ni.handle_event(ev(unicode="="))
    type_str(ni, "1*3")
    ni.handle_event(ev(type="NUMPAD_PERIOD", unicode=","))
    type_str(ni, "5")
    assert ni.buffer == "1*3.5"
    simple = NumericInput()
    type_str(simple, "2")
    simple.handle_event(ev(type="PERIOD", unicode=","))
    assert simple.buffer == "2."


def test_rotation_inverse_is_taken_in_degrees(monkeypatch):
    # Native inverts the degrees typed: "4 /" is 0.25 degrees, not
    # 1/(4 degrees in radians) = 820.7 degrees. Radians scenes invert plainly.
    import math

    from cut_plane.operators import numeric_input
    monkeypatch.setattr(numeric_input, "parse_value", lambda c, cat, text: math.radians(4.0))
    ni = NumericInput()
    type_str(ni, "4")
    ni.handle_event(ev(unicode="/"))

    def scene(rotation):
        return SimpleNamespace(scene=SimpleNamespace(unit_settings=SimpleNamespace(
            system="METRIC", system_rotation=rotation)))
    assert ni.parse(scene("DEGREES"), "ROTATION") == pytest.approx(math.radians(0.25))
    assert ni.parse(scene("RADIANS"), "ROTATION") == pytest.approx(1.0 / math.radians(4.0))
    assert ni.parse(scene("DEGREES"), "LENGTH") == pytest.approx(1.0 / math.radians(4.0))


def test_unit_letter_cannot_lead():
    ni = NumericInput(allow_units=True)
    # No digits yet -> leading letter is rejected (native: units only as suffix)
    assert ni.handle_event(ev(unicode="c")) is False
    assert ni.buffer == ""


# --- NumericInput: expression mode ----------------------------------------

def test_equal_enters_expression_without_appending():
    ni = NumericInput()
    ni.handle_event(ev(unicode="="))
    assert ni.expression is True
    assert ni.buffer == ""
    assert ni.display() == "=0"


def test_star_enters_expression_and_appends():
    ni = NumericInput()
    type_str(ni, "3")
    ni.handle_event(ev(unicode="*"))
    assert ni.expression is True
    assert ni.buffer == "3*"


def test_expression_accepts_letters_for_functions():
    ni = NumericInput()
    ni.handle_event(ev(unicode="="))
    type_str(ni, "sqrt(2)")
    assert ni.buffer == "sqrt(2)"


# --- NumericInput: backspace stages ---------------------------------------

def test_backspace_removes_then_reaches_reset_stage():
    ni = NumericInput()
    type_str(ni, "5")
    assert ni.handle_event(ev(type="BACK_SPACE")) is True
    assert ni.buffer == ""
    assert ni.reset_stage is True
    assert ni.active is True  # still owns the transform at reset stage


def test_second_backspace_hands_back_to_mouse():
    ni = NumericInput()
    type_str(ni, "5")
    ni.handle_event(ev(type="BACK_SPACE"))   # -> reset stage
    assert ni.handle_event(ev(type="BACK_SPACE")) is True  # cancels editing
    assert ni.active is False


def test_backspace_on_empty_is_noop():
    ni = NumericInput()
    assert ni.handle_event(ev(type="BACK_SPACE")) is False


def test_ctrl_backspace_clears_everything():
    ni = NumericInput()
    type_str(ni, "12")
    ni.handle_event(ev(unicode="/"))  # invert on
    assert ni.handle_event(ev(ctrl=True, type="BACK_SPACE")) is True
    assert ni.buffer == ""
    assert ni.invert is False
    assert ni.reset_stage is True


# --- NumericInput: ctrl shortcuts -----------------------------------------

def test_ctrl_minus_and_slash_toggle(monkeypatch):
    ni = NumericInput()
    type_str(ni, "8")
    ni.handle_event(ev(ctrl=True, type="MINUS"))
    assert ni.buffer == "-8"
    ni.handle_event(ev(ctrl=True, type="SLASH"))
    assert ni.invert is True


def test_ctrl_equal_leaves_expression_mode():
    ni = NumericInput()
    ni.handle_event(ev(unicode="="))           # enter expression
    assert ni.expression is True
    assert ni.handle_event(ev(ctrl=True, type="EQUAL")) is True
    assert ni.expression is False


# Background Blender has no clipboard: inside it these three can only fail.
_NO_CLIPBOARD = pytest.mark.skipif(
    getattr(getattr(__import__("bpy"), "app", None), "background", False),
    reason="background Blender has no clipboard")


@_NO_CLIPBOARD
def test_ctrl_c_copies_buffer():
    import bpy  # the conftest stub
    ni = NumericInput()
    type_str(ni, "42")
    assert ni.handle_event(ev(ctrl=True, type="C")) is True
    assert bpy.context.window_manager.clipboard == "42"


@_NO_CLIPBOARD
def test_ctrl_v_pastes_and_detects_expression():
    import bpy
    bpy.context.window_manager.clipboard = "sqrt(2)"
    ni = NumericInput()
    assert ni.handle_event(ev(ctrl=True, type="V")) is True
    assert ni.buffer == "sqrt(2)"
    assert ni.expression is True


@_NO_CLIPBOARD
def test_ctrl_v_plain_number_stays_simple():
    import bpy
    bpy.context.window_manager.clipboard = "123"
    ni = NumericInput()
    ni.handle_event(ev(ctrl=True, type="V"))
    assert ni.buffer == "123"
    assert ni.expression is False


# --- display ---------------------------------------------------------------

def test_display_empty_shows_zero():
    assert NumericInput().display() == "0"


# --- MultiNumericInput -----------------------------------------------------

def test_multi_starts_with_n_buffers():
    multi = MultiNumericInput(3)
    assert len(multi.inputs) == 3
    assert multi.idx == 0
    assert multi.active is False


def test_multi_tab_advances_and_wraps():
    multi = MultiNumericInput(2)
    multi.inputs[0].handle_event(ev(unicode="1"))  # make it active
    assert multi.handle_event(ev(type="TAB")) is True
    assert multi.idx == 1
    multi.handle_event(ev(type="TAB"))
    assert multi.idx == 0  # wrapped


def test_multi_ctrl_tab_goes_back():
    multi = MultiNumericInput(3)
    multi.inputs[0].handle_event(ev(unicode="1"))
    multi.handle_event(ev(ctrl=True, type="TAB"))
    assert multi.idx == 2  # -1 wraps to last


def test_multi_tab_inactive_is_noop():
    multi = MultiNumericInput(2)
    assert multi.handle_event(ev(type="TAB")) is False
    assert multi.idx == 0


def test_multi_values_pad_empty_with_zero():
    multi = MultiNumericInput(3)
    multi.inputs[0].handle_event(ev(unicode="2"))
    multi.inputs[2].handle_event(ev(unicode="7"))
    assert multi.values(ctx()) == [2.0, 0.0, 7.0]


def test_multi_display_brackets_active():
    multi = MultiNumericInput(2)
    multi.inputs[0].handle_event(ev(unicode="5"))
    assert multi.display() == "[5]  0"
