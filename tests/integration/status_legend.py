# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless test: the status-bar key legends are drawable.

    blender --background --factory-startup --python tests/integration/status_legend.py

The legends render through a status_text_set DRAW FUNCTION whose entries go
through template_event_from_keymap_item, fed by KeyMapItems materialized
from key specs into a private display-data keymap. A bad key spec only
raises at legend-build time (inside invoke, invisible to imports), so
materialize every spec here against the real keymap API, and prove
status_text_set accepts the function form (and None to clear).

Exit code 0 on success, 1 with the failed checks listed.
"""
import os
import sys

import bpy

ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

import cut_plane.operators.modal_base as mb  # noqa: E402
import cut_plane.operators.move as mv        # noqa: E402
import cut_plane.operators.rotate as rt      # noqa: E402
import cut_plane.operators.scale as sc       # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"[{'ok  ' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


def _rotate_view_legend():
    """The keyboard view-rotate legend as status_legend() actually builds
    it - its trackball entry is spliced in live from the rebindable rotate
    binding, so there is no module constant to read."""
    class _FakeRotateOp:
        _keyboard_invoked = True
        axis_id = 'VIEW'
        status_legend = rt.CutPlaneRotateBaseOperator.status_legend
    return _FakeRotateOp().status_legend()


LEGENDS = {
    "MOVE_AXIS": mv._LEGEND_MOVE_AXIS,
    "MOVE_FREE": mv._LEGEND_MOVE_FREE,
    "ROTATE_DIAL": rt._LEGEND_ROTATE_DIAL,
    "ROTATE_VIEW": _rotate_view_legend(),
    "TRACKBALL": rt._LEGEND_TRACKBALL,
    "SCALE": sc._LEGEND_SCALE,
}

# Every legend is well-formed ((key spec, ...), label) - what _legend_draw
# unpacks (an invalid shape would raise mid-draw otherwise)
for name, legend in LEGENDS.items():
    ok = all(isinstance(keys, tuple) and isinstance(label, str) and label
             for keys, label in legend)
    check(f"{name}: well-formed entries", ok)

# Wide-icon keys (Esc, Ctrl, ...) must be the LAST key of their entry:
# prefix icons draw in fixed one-unit slots, so a wide glyph there overlaps
# the next icon (the template sizes the last key correctly)
for name, legend in LEGENDS.items():
    bad = [spec for keys, _label in legend for spec in keys[:-1]
           if spec.rsplit("+", 1)[-1] in mb._WIDE_KEYS]
    check(f"{name}: no wide key in prefix position", not bad, ", ".join(bad))

# Every key spec must map to real icons for the chord-prefix rendering
# (a typo'd icon name would only raise mid-draw otherwise)
icon_enum = {i.identifier for i in
             bpy.types.UILayout.bl_rna.functions['label']
             .parameters['icon'].enum_items}
for name, legend in LEGENDS.items():
    bad = [icon for keys, _label in legend for spec in keys
           for icon in mb._spec_icons(spec) if icon not in icon_enum]
    check(f"{name}: all spec icons exist", not bad, ", ".join(bad))

# Every key spec must materialize into a KeyMapItem (an unknown event type
# raises TypeError inside keymap_items.new)
kc_available = getattr(bpy.context, 'window_manager', None) is not None \
    and bpy.context.window_manager.keyconfigs.addon is not None
check("addon keyconfig reachable headless", kc_available)
if kc_available:
    for name, legend in LEGENDS.items():
        bad = []
        for keys, _label in legend:
            for spec in keys:
                try:
                    item = mb._legend_keymap_item(spec)
                    if item is None:
                        bad.append(spec)
                except Exception as e:  # noqa: BLE001 - any rejection = bad spec
                    bad.append(f"{spec} ({e})")
        check(f"{name}: all key specs materialize", not bad, ", ".join(bad))

    # Specs are cached: same item back, no duplicate keymap entries
    a = mb._legend_keymap_item('LEFTMOUSE')
    b = mb._legend_keymap_item('LEFTMOUSE')
    check("key spec cache returns the same item", a is b)

    # shift+ prefix lands on the item (drawn as the [Shift][key] chord)
    shifted = mb._legend_keymap_item('shift+X')
    check("shift+ spec sets the shift modifier",
          bool(shifted.shift) and shifted.type == 'X')

# status_text_set accepts the draw-function form (and None to clear)
ws = bpy.data.workspaces[0]
try:
    ws.status_text_set(mb._legend_draw(mv._LEGEND_MOVE_FREE))
    ws.status_text_set(None)
    check("status_text_set accepts draw function + None", True)
except Exception as e:  # noqa: BLE001 - report any API rejection as a failure
    check("status_text_set accepts draw function + None", False, str(e))

# Cleanup removes the display-data keymap and empties the cache
if kc_available:
    mb.legend_keymap_cleanup()
    kc = bpy.context.window_manager.keyconfigs.addon
    check("cleanup removes the legend keymap",
          kc.keymaps.get(mb._LEGEND_KEYMAP_NAME) is None
          and not mb._legend_kmi_cache)

print("\nSTATUS LEGEND:", "OK" if not failures else f"FAIL ({len(failures)}): {failures}")
sys.exit(1 if failures else 0)
