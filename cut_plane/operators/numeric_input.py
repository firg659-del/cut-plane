# SPDX-License-Identifier: GPL-3.0-or-later
"""Native-style numeric type-in for the gizmo's modal operators.

Blender's C ``numinput`` machinery is not exposed to Python, so this is a
reimplementation of its behavior:

Simple mode:
- digits / ``.`` append, Backspace edits, Ctrl-Backspace clears
- ``-`` toggles the sign (anywhere in the input)
- ``/`` toggles inverse (1/x), like native
- unit suffix letters when enabled ("25cm", "45d", "1.5r")
- Backspace on an empty input first resets the value (native "Reset
  Backspace" stage), a second press hands control back to the mouse

Expression mode (native: ``=`` or numpad ``*``):
- entered with ``=`` (keeps what was already typed) or by typing
  ``*`` / ``+`` / ``(``; ``Ctrl-=`` switches back to simple mode
- the whole input is evaluated by Blender's unit parser - the same
  evaluator native uses: ``3*5+1``, ``2cm+3mm``, ``sqrt(2)``, ``pi/4``
- ``Ctrl-Minus`` / ``Ctrl-Slash`` toggle negate/inverse (native: plain
  keys insert literally in this mode)

Ctrl-C copies the input, Ctrl-V pastes (sanitized) - like native.
"""

import bpy

from .utils import ctrl_like

# Characters that switch from simple to expression mode when typed
_EXPRESSION_TRIGGERS = {'=', '*', '+', '('}
# Punctuation accepted inside an expression (letters/digits always pass)
_EXPRESSION_CHARS = {'+', '-', '*', '/', '(', ')', '.', ',', ' ', '%', "'", '"'}
# Maximum characters accepted from a clipboard paste
_PASTE_MAX_CHARS = 64


# Scene length_unit enum -> the unit token bare typed numbers mean (native
# type-in interprets "2" in the scene's DISPLAY unit, not always meters).
# 'ADAPTIVE' maps to "" = the parser's system default.
_LENGTH_REF_UNITS = {
    'KILOMETERS': "km", 'METERS': "m", 'CENTIMETERS': "cm",
    'MILLIMETERS': "mm", 'MICROMETERS': "um",
    'MILES': "mi", 'FEET': "ft", 'INCHES': "in", 'THOU': "thou",
}


def parse_value(context, category: str, text: str):
    """Parse typed text with Blender's unit parser, native-style.

    ``category`` is a bpy.utils.units category: 'LENGTH' (returns Blender
    internal units), 'ROTATION' (returns radians - plain numbers mean
    degrees, 'd'/'r' suffixes work), or 'NONE' (unitless). Full expressions
    are supported ("3*5+1", "2cm+3mm", "sqrt(2)"). Returns None if
    unparseable.

    Lengths follow the SCENE UNIT SETTINGS like native type-in does: a bare
    number means the scene's display unit (cm scenes: "2" = 2 cm), explicit
    suffixes always win ("2m" is 2 meters anywhere), and the result is
    converted from display space to internal units via the scene's Unit
    Scale (display = internal * scale_length). With units disabled
    ('NONE' system) numbers are plain Blender units, as native.
    """
    if not text:
        return None
    try:
        unit_settings = context.scene.unit_settings
        unit_system = unit_settings.system
        ref_unit = ""
        if category == 'LENGTH' and unit_system != 'NONE':
            ref_unit = _LENGTH_REF_UNITS.get(unit_settings.length_unit, "")
        value = bpy.utils.units.to_value(unit_system, category, text,
                                         str_ref_unit=ref_unit)
        if category == 'LENGTH' and unit_system != 'NONE':
            scale = unit_settings.scale_length
            if scale > 1e-9:
                value /= scale
        return value
    except (ValueError, AttributeError):
        try:
            return float(text)
        except ValueError:
            return None


class NumericInput:
    """Single-value numeric type-in state machine.

    Feed every modal event to :meth:`handle_event`; while :attr:`active`
    the typed value owns the transform and mouse movement should be
    ignored (matches native transform behavior).
    """

    def __init__(self, allow_units: bool = False):
        self.buffer = ""
        self.allow_units = allow_units
        self.expression = False   # Native NUM_EDIT_FULL ('=' / numpad '*')
        self.invert = False       # Native '/' inverse toggle (1/x)
        self.reset_stage = False  # Native "Reset Backspace" intermediate stage

    @property
    def active(self) -> bool:
        return bool(self.buffer) or self.expression or self.invert or self.reset_stage

    def parse(self, context, category: str = 'NONE'):
        """Evaluate the buffer for *category*, applying the inverse toggle.

        Returns the value in Blender internal units (meters / radians /
        plain float), or None while empty or unparseable.
        """
        value = parse_value(context, category, self.buffer)
        if value is None:
            return None
        if self.invert:
            if abs(value) < 1e-12:
                return None  # 1/0 - treat as not-yet-valid input
            value = 1.0 / value
        return value

    def display(self) -> str:
        """Header representation with native-like mode markers."""
        text = self.buffer if self.buffer else "0"
        if self.expression:
            text = "=" + text
        if self.invert:
            text = f"1/({text})"
        return text

    def _toggle_sign(self) -> None:
        if self.buffer.startswith('-'):
            self.buffer = self.buffer[1:]
        else:
            self.buffer = '-' + self.buffer

    def _handle_ctrl_event(self, event) -> bool:
        """Ctrl-modified keys (native shortcuts; unicode is unreliable
        with Ctrl held, so these match on event.type)."""
        if event.type == 'BACK_SPACE':
            # Clear the whole value at once
            if self.buffer or self.expression or self.invert:
                self.buffer = ""
                self.expression = False
                self.invert = False
                self.reset_stage = True
                return True
            return False
        if event.type in {'EQUAL', 'NUMPAD_ASTERIX'}:
            # Ctrl-= / Ctrl-NumpadAsterisk: back to simple mode (native).
            # The buffer is kept - evaluation is identical in both modes,
            # only the key behavior changes.
            if self.expression:
                self.expression = False
                return True
            return False
        if event.type in {'MINUS', 'NUMPAD_MINUS'}:
            # Negate shortcut; required form inside expression mode (native)
            self._toggle_sign()
            self.reset_stage = False
            return True
        if event.type in {'SLASH', 'NUMPAD_SLASH'}:
            # Inverse shortcut; required form inside expression mode (native)
            self.invert = not self.invert
            self.reset_stage = False
            return True
        if event.type == 'C':
            # Copy the input string (native Ctrl-C)
            if self.active:
                bpy.context.window_manager.clipboard = self.buffer
                return True
            return False
        if event.type == 'V':
            # Paste (native Ctrl-V), sanitized to expression characters
            text = bpy.context.window_manager.clipboard[:_PASTE_MAX_CHARS]
            filtered = "".join(
                c for c in text
                if c.isdigit() or c.isalpha() or c in _EXPRESSION_CHARS
            )
            if filtered:
                self.buffer += filtered
                if any(not (c.isdigit() or c == '.') for c in filtered):
                    self.expression = True
                self.reset_stage = False
                return True
            return False
        return False

    def handle_event(self, event) -> bool:
        """Consume typing events.

        Returns True if the event changed the input state - the caller
        should re-apply the transform from the typed value and redraw.
        """
        if event.value != 'PRESS':
            return False

        if ctrl_like(event):  # Cmd substitutes for Ctrl on macOS
            return self._handle_ctrl_event(event)

        if event.type == 'BACK_SPACE':
            if self.buffer:
                self.buffer = self.buffer[:-1]
                if not self.buffer:
                    # Native "Reset Backspace": first show the initial
                    # state (value zeroed, input still owns the transform)
                    self.expression = False
                    self.invert = False
                    self.reset_stage = True
                return True
            if self.reset_stage or self.expression or self.invert:
                # Second press: cancel numeric editing, back to the mouse
                self.reset_stage = False
                self.expression = False
                self.invert = False
                return True
            return False

        ch = event.unicode
        if not ch:
            return False

        if self.expression:
            # Full-edit mode: append nearly everything, evaluate later
            if ch.isdigit() or ch.isalpha() or ch in _EXPRESSION_CHARS:
                self.buffer += ch
                self.reset_stage = False
                return True
            return False

        # --- Simple mode ---
        if ch == ',':
            # Native numinput reads the comma as a decimal point (European
            # keyboards); expression mode keeps it literal (unit-parser
            # punctuation), so normalize here only.
            ch = '.'
        if ch.isdigit() or ch == '.':
            self.buffer += ch
            self.reset_stage = False
            return True
        if ch == '-':
            # Toggle sign anywhere in the input (matches native)
            self._toggle_sign()
            self.reset_stage = False
            return True
        if ch == '/':
            # Toggle inverse 1/x (matches native simple mode)
            self.invert = not self.invert
            self.reset_stage = False
            return True
        if ch in _EXPRESSION_TRIGGERS:
            # Switch to expression mode, keeping what was already typed
            # (native: '=' or numpad '*' enters full edit)
            self.expression = True
            if ch != '=':
                self.buffer += ch
            self.reset_stage = False
            return True
        if self.allow_units and self.buffer and (ch.isalpha() or ch in {' ', "'", '"'}):
            # Unit suffix characters ("25cm", "45d") - once typing started
            self.buffer += ch
            return True
        return False


class MultiNumericInput:
    """Multi-value numeric type-in (one buffer per free axis).

    Mirrors native transform's multi-value input: typing edits the
    current value, TAB advances to the next one (wrapping). Used by the
    plane handlers (2 free axes), the free-move center button (3), and
    the trackball (2 angles).
    """

    def __init__(self, count: int, allow_units: bool = False):
        self.inputs = [NumericInput(allow_units=allow_units) for _ in range(count)]
        self.idx = 0

    @property
    def active(self) -> bool:
        return any(n.active for n in self.inputs)

    def handle_event(self, event) -> bool:
        """Consume typing events.

        Returns True if the event changed the input state - the caller
        should re-apply the transform from the typed values and redraw.
        """
        if event.type == 'TAB' and event.value == 'PRESS':
            if self.active:
                # Tab: next component, Ctrl-Tab: previous (native; Cmd
                # substitutes for Ctrl on macOS)
                step = -1 if ctrl_like(event) else 1
                self.idx = (self.idx + step) % len(self.inputs)
                return True
            return False
        return self.inputs[self.idx].handle_event(event)

    def values(self, context, category: str = 'NONE') -> list:
        """Per-buffer parsed values; empty/invalid buffers give 0.0."""
        out = []
        for n in self.inputs:
            value = n.parse(context, category)
            out.append(value if value is not None else 0.0)
        return out

    def display(self) -> str:
        """Header string with the value being edited bracketed."""
        parts = []
        for i, n in enumerate(self.inputs):
            parts.append(f"[{n.display()}]" if i == self.idx else n.display())
        return "  ".join(parts)
