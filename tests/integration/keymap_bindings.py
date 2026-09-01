# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless test: the tool keymap is a FROZEN template the user owns.

Runs inside Blender:

    blender --background --factory-startup --python tests/integration/keymap_bindings.py

Contract, and the reason three earlier keymap fixes failed:

  * bl_keymap is a module CONSTANT (_TOOL_KEYMAP), never rebuilt from a
    preference. The addon writes the TEMPLATE, the user writes the
    BINDINGS - so a rebind can never be reverted by us, and there is
    nothing to "reconcile".
  * Everything needing a transform key READS the live keymap
    (transform_chords / chord_matches) rather than
    a cached resolution, so what is displayed cannot disagree with what
    fires.

The predecessor suite (keymap_rcs.py) was 22 green checks that could not
fail: it tested build_tool_keymap as a pure function, asserted an answer it
had itself injected, and read the ADDON keyconfig copy - the one that cannot
be wrong. It stayed green when mutated with the pre-6f02e54 WRONG keymap
name. Every check here goes through the USER copy, which is what fires.

keyconfigs.update() MATERIALIZES that user copy - it exists even in
--background, which is what makes this testable at all (an earlier probe
concluded "impossible" only because it never called update()).

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
from cut_plane.ui import tool  # noqa: E402
from cut_plane.operators import modal_base  # noqa: E402
from cut_plane.operators import rotate as rotate_mod  # noqa: E402
from cut_plane.operators.reset import CutPlaneResetOperator  # noqa: E402

cut_plane.register()

wm = bpy.context.window_manager
failures = []


def check(name, cond, detail=""):
    print(f"[{'ok  ' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


def current_keys():
    """(move, rotate, scale) key TYPES from the live keymap; None if unbound.
    Derived from transform_chords - the payload has no key-only accessor."""
    return tuple(c[0] if c else None for c in tool.transform_chords())


class FakeEvent:
    """Enough of a bpy Event for chord_matches (which reads type + mods)."""

    def __init__(self, type, ctrl=False, shift=False, alt=False, oskey=False,
                 hyper=False):
        self.type = type
        self.ctrl, self.shift, self.alt = ctrl, shift, alt
        self.oskey, self.hyper = oskey, hyper


def user_km():
    """The USER copy, re-fetched every time: keyconfigs.update() dangles held
    keymap references (measured - a stale ref raised from inside an unrelated
    keymap), and an uncaught exception exits Blender 0, i.e. reads as PASS."""
    return wm.keyconfigs.user.keymaps.get(tool._TOOL_KEYMAP_NAME)


def user_item(idname, type=None):
    return next((i for i in user_km().keymap_items
                 if i.idname == idname and (type is None or i.type == type)), None)


def types_of(idname):
    return [i.type for i in user_km().keymap_items if i.idname == idname]


# --- the frozen template ---------------------------------------------------
check("bl_keymap IS the frozen constant (not a rebuilt tuple)",
      tool.CutPlaneTool.bl_keymap is tool._TOOL_KEYMAP)
check("the runtime keymap machinery is gone",
      not hasattr(tool, "build_tool_keymap")
      and not hasattr(tool, "_sync_tool_keymap")
      and not hasattr(tool, "_resolve_transform_keys")
      and not hasattr(tool, "_detect_select_mouse"))
# These describe the keymap's DESIGN, so they read the base template. The
# shipped _TOOL_KEYMAP appends a Cmd twin per Ctrl item on macOS, where two
# of the four box-select drags are Ctrl - so a count taken from the shipped
# tuple is 6 there, not 4. Asserting the shipped tuple made this suite fail
# on macOS for a keymap that is correct (caught the first time CI ran on it).
check("all 4 box-select drags bind LEFTMOUSE (native tool_mouse is LEFTMOUSE "
      "in BOTH select-mouse branches)",
      [i[1]["type"] for i in tool._TOOL_KEYMAP_BASE
       if i[0] == "view3d.select_box"] == ['LEFTMOUSE'] * 4)
check("box-select keeps the native ADD/SUB/AND vocabulary",
      sorted(dict(i[2]["properties"])["mode"] for i in tool._TOOL_KEYMAP_BASE
             if i[0] == "view3d.select_box" and i[2]) == ['ADD', 'AND', 'SUB'])
# ...and the platform rule itself, both ways round.
_ctrl_items = [i for i in tool._TOOL_KEYMAP_BASE if i[1].get("ctrl")]
if sys.platform == 'darwin':
    _twins = [i for i in tool._TOOL_KEYMAP if i[1].get("oskey")]
    check("macOS: every Ctrl binding gets a Cmd twin",
          len(_twins) == len(_ctrl_items) and len(_ctrl_items) > 0,
          f"{len(_twins)} twins for {len(_ctrl_items)} Ctrl items")
    check("macOS: the twins are APPENDED, base order untouched",
          tool._TOOL_KEYMAP[:len(tool._TOOL_KEYMAP_BASE)] == tool._TOOL_KEYMAP_BASE)
else:
    check("non-macOS: the shipped keymap IS the base (no Cmd twins)",
          tool._TOOL_KEYMAP == tool._TOOL_KEYMAP_BASE)
check("ONE literal for the transform defaults (template == fallback)",
      tuple(i[1]["type"] for i in tool._TOOL_KEYMAP
            if i[0] in tool._TRANSFORM_IDNAMES) == tool._DEFAULT_TRANSFORM_KEYS)

# --- the exact name, WITHOUT the fuzzy rescue ------------------------------
# The old "(right name)" check went through the fuzzy fallback, so it passed
# with the very name the user caught as broken. Assert the constant itself.
check("_TOOL_KEYMAP_NAME resolves EXACTLY (no fuzzy rescue)",
      wm.keyconfigs.addon.keymaps.get(tool._TOOL_KEYMAP_NAME) is not None,
      tool._TOOL_KEYMAP_NAME)

# --- register() materializes the USER copy ---------------------------------
check("register() left a populated USER copy (the one that fires)",
      user_km() is not None
      and len(user_km().keymap_items) == len(tool._TOOL_KEYMAP),
      f"{0 if user_km() is None else len(user_km().keymap_items)} items")
check("tool_keymap_live skips the EMPTY default-config copy",
      tool.tool_keymap_live() is not None
      and len(tool.tool_keymap_live().keymap_items) > 0)
check("tool_keymap_editable is the USER copy (the only safe write target)",
      tool.tool_keymap_editable() == user_km())
check("transform keys read G/R/S from the live keymap",
      current_keys() == ('G', 'R', 'S'),
      str(current_keys()))

# --- THE REGRESSION -------------------------------------------------------
# What shipped: _sync_tool_keymap called restore_to_default() on every tool
# activation, so ANY deliberate edit in Preferences > Keymap died on the next
# Ctrl+Z. Rebind two items - one the panel owns, one it does not - then
# re-register the tool exactly as register() does.
user_item("view3d.cut_plane_rotate_view").type = 'E'
user_item("view3d.cut_plane_cut", 'RET').type = 'F'
wm.keyconfigs.update()

check("a rebind of rotate lands in the live keymap",
      types_of("view3d.cut_plane_rotate_view") == ['E'],
      str(types_of("view3d.cut_plane_rotate_view")))
check("...and Blender marks it user-modified",
      user_item("view3d.cut_plane_rotate_view").is_user_modified)
check("the resolver FOLLOWS the rebind (no cache to go stale)",
      current_keys() == ('G', 'E', 'S'),
      str(current_keys()))

tool._ensure_tool_unregistered(tool.CutPlaneTool)
bpy.utils.register_tool(tool.CutPlaneTool, separator=True, group=False)
wm.keyconfigs.update()

check("REGRESSION: the rebind SURVIVES a full tool re-registration",
      types_of("view3d.cut_plane_rotate_view") == ['E'],
      str(types_of("view3d.cut_plane_rotate_view")))
check("REGRESSION: a rebind of a NON-panel binding (Cut) survives too",
      sorted(types_of("view3d.cut_plane_cut")) == ['F', 'NUMPAD_ENTER'],
      str(types_of("view3d.cut_plane_cut")))

# --- chord matching: the modifier-blind switch bug ------------------------
move_chord, rotate_chord, _scale_chord = tool.transform_chords()
check("a chord carries type and modifiers",
      move_chord[0] == 'G' and rotate_chord[0] == 'E',
      f"{move_chord} {rotate_chord}")
check("plain G matches the Move chord",
      tool.chord_matches(move_chord, FakeEvent('G')))
check("Alt+G does NOT match it (it used to abandon a rotate into a move)",
      not tool.chord_matches(move_chord, FakeEvent('G', alt=True)))
check("Shift+G / Ctrl+G do not match (the live precision/snap modifiers)",
      not tool.chord_matches(move_chord, FakeEvent('G', shift=True))
      and not tool.chord_matches(move_chord, FakeEvent('G', ctrl=True)))
check("a wrong type never matches",
      not tool.chord_matches(move_chord, FakeEvent('E')))
check("None (unbound) never matches",
      not tool.chord_matches(None, FakeEvent('G')))

user_item("view3d.cut_plane_move_center").type = 'W'
user_item("view3d.cut_plane_move_center").alt = True
wm.keyconfigs.update()
alt_w = tool.transform_chords()[0]
check("an Alt+W rebind matches Alt+W and NOT bare W",
      tool.chord_matches(alt_w, FakeEvent('W', alt=True))
      and not tool.chord_matches(alt_w, FakeEvent('W')))

user_item("view3d.cut_plane_move_center").alt = -1
wm.keyconfigs.update()
any_mod = tool.transform_chords()[0]
check("KM_ANY (-1) matches both pressed and unpressed",
      tool.chord_matches(any_mod, FakeEvent('W', alt=True))
      and tool.chord_matches(any_mod, FakeEvent('W')))

# 'hyper' exists on KeyMapItem AND Event in 4.5 and 5.1 (measured). Omitting
# it from _MOD_ATTRS made Hyper+G match a bare G - the exact blindness this
# design set out to fix.
check("'hyper' is one of the tracked modifiers",
      "hyper" in tool._MOD_ATTRS, str(tool._MOD_ATTRS))
user_km().restore_to_default()
wm.keyconfigs.update()
user_item("view3d.cut_plane_move_center").hyper = True
wm.keyconfigs.update()
hyper_chord = tool.transform_chords()[0]
check("a Hyper+G binding does NOT match a bare G",
      not tool.chord_matches(hyper_chord, FakeEvent('G'))
      and tool.chord_matches(hyper_chord, FakeEvent('G', hyper=True)),
      str(hyper_chord))
user_km().restore_to_default()
wm.keyconfigs.update()

# --- disabling a binding is a legitimate state ---------------------------
# The PANEL must agree with the read path here. It did not: transform_chords
# filters on kmi.active but _row_item does not, so an item switched off in
# Preferences > Keymap still drew as a live, undimmed hotkey while pressing
# it did nothing. The old suite reached this exact state and only ever
# checked the read side - which is precisely why 33/33 green missed it.
from cut_plane.state.preferences import _binding_warning, _KEY_ROWS, _row_item  # noqa: E402

user_item("view3d.cut_plane_scale_plane").active = False
wm.keyconfigs.update()
check("an unchecked binding reads as None, not a resurrected default",
      current_keys()[2] is None,
      str(current_keys()))
check("...and its chord matches nothing",
      not tool.chord_matches(tool.transform_chords()[2], FakeEvent('S')))


def panel_rows():
    km = tool.tool_keymap_editable()
    return [(label, _row_item(km, idname, mode))
            for label, idname, mode in _KEY_ROWS]


_scale_row = dict(panel_rows())["Scale"]
check("the panel still OWNS a disabled item (so it can be switched back on)",
      _scale_row is not None)
check("...and knows it is inactive, so the row can dim + show the checkbox",
      _scale_row is not None and not _scale_row.active)
# A disabled item cannot fire, so it cannot conflict.
user_item("view3d.cut_plane_move_center").type = 'S'
wm.keyconfigs.update()
check("a DISABLED binding raises no phantom conflict",
      _binding_warning(panel_rows()) == "", _binding_warning(panel_rows()))
user_km().restore_to_default()
wm.keyconfigs.update()

# KM_ANY on a transform still trips the X/Y/Z hazard: it fires with no
# modifier held, so a plain truth test on the modifiers ("-1 is truthy")
# silently suppressed the warning.
_rot = user_item("view3d.cut_plane_rotate_view")
_rot.type = 'X'
_rot.any = True
wm.keyconfigs.update()
check("X/Y/Z hazard is still caught when the chord is KM_ANY",
      "axis constraint" in _binding_warning(panel_rows()),
      _binding_warning(panel_rows()) or "(none)")
user_km().restore_to_default()
wm.keyconfigs.update()

# --- no consumer holds a stale key literal -------------------------------
user_km().restore_to_default()
wm.keyconfigs.update()
user_item("view3d.cut_plane_rotate_view").type = 'E'
wm.keyconfigs.update()


class _FakeRotateOp:
    """Just enough for the legend branch (keyboard view-axis rotate)."""
    _keyboard_invoked = True
    axis_id = 'VIEW'
    status_legend = rotate_mod.CutPlaneRotateBaseOperator.status_legend


legend_keys = [k for spec, _label in _FakeRotateOp().status_legend() for k in spec]
check("the status legend advertises the REBOUND trackball key, not 'R'",
      'E' in legend_keys and 'R' not in legend_keys, str(legend_keys))

# The cheat sheet and the status bar must name the SAME chord. With Rotate on
# Shift+R the sheet said "R again" (a key that does nothing) while the status
# bar correctly said shift+R - the panel/status-bar disagreement the frozen
# template was supposed to make impossible. Both now go through chord_label.
from cut_plane.state.preferences import _shortcuts  # noqa: E402

_rot = user_item("view3d.cut_plane_rotate_view")
_rot.type = 'R'
_rot.shift = True
wm.keyconfigs.update()
_trackball_row = next((keys for keys, what in _shortcuts()
                       if "Trackball" in what), None)
check("the cheat sheet names the FULL rotate chord, modifiers included",
      _trackball_row == "Shift+R again", str(_trackball_row))
_legend_spec = [s for spec, label in _FakeRotateOp().status_legend()
                if label == "Trackball" for s in spec]
check("...and the status bar names the same chord",
      _legend_spec == ['shift+R'], str(_legend_spec))
user_km().restore_to_default()
wm.keyconfigs.update()

user_item("view3d.cut_plane_rotate_view").active = False
wm.keyconfigs.update()
check("with rotate DISABLED the cheat sheet drops the trackball row entirely",
      not any("Trackball" in what for _keys, what in _shortcuts()))
user_km().restore_to_default()
wm.keyconfigs.update()

check("the reset tooltips name no key chord at all",
      not any("Alt+" in CutPlaneResetOperator.description(
          None, type("P", (), {"mode": m})())
          for m in ('LOCATION', 'ROTATION', 'SCALE')))
check("SPACE joins the Enter keys as a confirm (documented, previously not "
      "implemented)",
      "{'RET', 'NUMPAD_ENTER', 'SPACE'}" in inspect.getsource(modal_base))

# The panel curates ONE row per binding, so it must own the RIGHT item where
# an idname has aliases. Blender builds the user copy in reverse template
# order, so a naive first-match gave the Cut row NUMPAD_ENTER and rebinding
# "Cut" left Enter still cutting. This locks the order fact _row_item relies
# on: if Blender ever changes it, this fails instead of the panel silently
# owning the wrong key.

# Item ORDER only - types are rebound above, and a rebind never moves an item.
check("the user copy is built in the exact REVERSE of the template order",
      [i.idname for i in user_km().keymap_items]
      == list(reversed([i[0] for i in tool._TOOL_KEYMAP])))
check("the Cut row owns Enter, not its Numpad-Enter alias",
      _row_item(user_km(), "view3d.cut_plane_cut", None).type == 'RET',
      _row_item(user_km(), "view3d.cut_plane_cut", None).type)
check("every panel row resolves to a real binding",
      all(_row_item(user_km(), idname, mode) is not None
          for _label, idname, mode in _KEY_ROWS))
check("the three Reset rows resolve to DISTINCT items (mode disambiguates)",
      len({_row_item(user_km(), "view3d.cut_plane_reset", m).type
           for m in ('LOCATION', 'ROTATION', 'SCALE')}) == 3)

user_km().restore_to_default()
wm.keyconfigs.update()
check("restore_to_default returns the keymap to the template",
      current_keys() == ('G', 'R', 'S'),
      str(current_keys()))

# --- macOS: a Cmd twin for every Ctrl item ---------------------------------
# Blender's manual: "The Cmd key can be used instead of Ctrl on macOS" - but
# that substitution lives in the C event layer for NATIVE items only.
# Measured on an M2: Cmd+Z did NOT fire the tool's Ctrl+Z undo while Ctrl+Z
# did. _oskey_twins makes the convention explicit; the template stays a
# frozen per-platform constant. The builder is pure, so the mac variant is
# fully testable here on Windows.
import sys as _sys  # noqa: E402

twins = tool._oskey_twins(tool._TOOL_KEYMAP_BASE)
check("every Ctrl item gets exactly one Cmd twin (4: undo/redo/sub/and)",
      sorted(t[0] for t in twins) == sorted(
          i[0] for i in tool._TOOL_KEYMAP_BASE if i[1].get("ctrl")),
      str([t[0] for t in twins]))
check("twins carry oskey and never ctrl",
      all(t[1].get("oskey") and not t[1].get("ctrl") for t in twins))
check("twins preserve type/value/shift and operator properties",
      all(t[1]["type"] == b[1]["type"] and t[1]["value"] == b[1]["value"]
          and t[1].get("shift") == b[1].get("shift") and t[2] == b[2]
          for t, b in zip(twins,
                          [i for i in tool._TOOL_KEYMAP_BASE
                           if i[1].get("ctrl")])))
check("the redo twin keeps Shift (Cmd+Shift+Z)",
      any(t[0] == "view3d.cut_plane_redo" and t[1].get("shift")
          for t in twins))
if _sys.platform == 'darwin':
    check("mac template = base + twins",
          tool._TOOL_KEYMAP == tool._TOOL_KEYMAP_BASE + twins)
else:
    check("non-mac template is exactly the base (no OS-key noise)",
          tool._TOOL_KEYMAP is tool._TOOL_KEYMAP_BASE)

# ctrl_like: the modal-side half of the same convention.
from cut_plane.operators import utils as op_utils  # noqa: E402


class _Evt:
    def __init__(self, ctrl=False, oskey=False):
        self.ctrl, self.oskey = ctrl, oskey


check("ctrl_like: plain event is False", not op_utils.ctrl_like(_Evt()))
check("ctrl_like: ctrl is True everywhere", op_utils.ctrl_like(_Evt(ctrl=True)))
_orig_mac = op_utils._IS_MACOS
try:
    op_utils._IS_MACOS = False
    check("ctrl_like: OS key means nothing off-mac",
          not op_utils.ctrl_like(_Evt(oskey=True)))
    op_utils._IS_MACOS = True
    check("ctrl_like: Cmd substitutes for Ctrl on mac",
          op_utils.ctrl_like(_Evt(oskey=True)))
finally:
    op_utils._IS_MACOS = _orig_mac

# --- the transform modals are tool-scoped and documented --------------------
# poll: gizmo-driver operators must read as disabled outside the tool (F3
# search / keymap editor honesty). Headless never has the tool formally
# active, so poll must be False here for every mixin subclass - which also
# proves the shared mixin poll is inherited by all of them.
from cut_plane.operators.move import (  # noqa: E402
    CutPlaneMoveAxisOperator as _MvAx,
    CutPlaneMoveCenterOperator as _MvCt,
)
from cut_plane.operators.rotate import (  # noqa: E402
    CutPlaneRotateViewOperator as _RotV,
    CutPlaneTrackballOperator as _Trk,
)
from cut_plane.operators.scale import CutPlaneScalePlaneOperator as _Scl  # noqa: E402

_TRANSFORM_OPS = (_MvAx, _MvCt, _RotV, _Trk, _Scl)
_mixin_poll = modal_base.CutPlaneModalMixin.poll.__func__
check("transform modals define poll (via the mixin)",
      all(op.poll.__func__ is _mixin_poll for op in _TRANSFORM_OPS),
      str([op.__name__ for op in _TRANSFORM_OPS
           if op.poll.__func__ is not _mixin_poll]))
check("poll is False while the tool is not active (headless)",
      not any(op.poll(bpy.context) for op in _TRANSFORM_OPS))
_no_desc = [op.__name__ for op in _TRANSFORM_OPS
            if not getattr(op, "bl_description", "").strip()]
check("every transform modal carries a bl_description", not _no_desc,
      str(_no_desc))

print(f"\nKEYMAP BINDINGS: {'FAIL - ' + ', '.join(failures) if failures else 'OK'}")
sys.stdout.flush()
os._exit(1 if failures else 0)
