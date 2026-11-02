# SPDX-License-Identifier: GPL-3.0-or-later
"""Addon preferences: per-INSTALL settings (Preferences > Add-ons > this addon).

Deliberately small. Preferences hold machine/user-level knobs saved in the
userprefs - session tool state (cut options, plane pose, snapping) stays on
window_manager.cut_plane, out of undo's reach. What qualifies:

  * Debug Mode - the discoverable face of the CUT_PLANE_DEBUG env var
    (loud internal errors + verbose logging for bug reports)
  * Live Preview Face Limit - the density cap above which the per-move
    live bisect is skipped; machine-dependent by nature

The Shortcuts block is NOT a preference. It draws the tool keymap's REAL
KeyMapItems with Blender's own full_event hotkey button - click a row, press
a chord, done - so this panel and Preferences > Keymap edit the same objects
and can never disagree. Earlier versions stored the keys as addon properties
and regenerated the keymap from them; that made the addon a second writer on
a keymap Blender already owns, which is the whole phantom-diff story in
tool.py. full_event also checks RNA_struct_is_a(&RNA_KeyMapItem), so the
native key button cannot be driven from addon properties at all - a
shortcut UI has to edit real KeyMapItems to get it.

The addon also runs un-enabled via dev_load.py, where AddonPreferences are
not reachable - every consumer goes through get_prefs() and falls back to
the constants defaults.
"""

import logging

import bpy
from bpy.types import AddonPreferences

from ..lib.constants import CUT_PREVIEW_MAX_FACES, logger

# (label, idname, mode) - the bindings worth surfacing here, in panel order.
# mode disambiguates items sharing one idname by their keymap-set property:
# the three resets ('mode') and the three aligns ('axis'). Everything omitted (the four
# box-select drags, the NUMPAD_ENTER cut alias) stays editable in
# Preferences > Keymap, which the signpost below names.
_KEY_ROWS = (
    ("Move", "view3d.cut_plane_move_center", None),
    ("Rotate", "view3d.cut_plane_rotate_view", None),
    ("Scale", "view3d.cut_plane_scale_plane", None),
    ("Reset Location", "view3d.cut_plane_reset", 'LOCATION'),
    ("Reset Rotation", "view3d.cut_plane_reset", 'ROTATION'),
    ("Reset Scale", "view3d.cut_plane_reset", 'SCALE'),
    ("Align XY", "view3d.cut_plane_align", 'Z'),
    ("Align XZ", "view3d.cut_plane_align", 'Y'),
    ("Align YZ", "view3d.cut_plane_align", 'X'),
    ("Cut", "view3d.cut_plane_cut", None),
    ("Undo", "view3d.cut_plane_undo", None),
    ("Redo", "view3d.cut_plane_redo", None),
)


def _shortcuts():
    """Cheat sheet: (keys, what) for keys that are NOT keymap items.

    ONE rule, mechanically applied: nothing that appears in the keymap
    appears here. Those live in the rows above, which cannot go stale
    because they ARE the bindings. What is left is the modal event loop's
    own vocabulary - hardcoded, matching Blender's Transform Modal Map
    item for item, and not rebindable (an addon keyconfig cannot carry a
    modal keymap). The trackball row is the one live-resolved token,
    because the rotate key is rebindable.
    """
    from ..ui.tool import chord_label, transform_chords
    rotate_chord = transform_chords()[1]
    rows = [
        ("X / Y / Z", "Axis constraint (again: local, again: off)"),
        ("Shift+X / Y / Z", "Lock a plane"),
    ]
    if rotate_chord is not None:
        # The FULL chord, via the same formatter the status bar uses: with
        # Rotate on Shift+R, "R again" named a key that does nothing.
        rows.append((f"{chord_label(rotate_chord)} again", "Trackball rotation"))
    rows += [
        ("Type numbers", "Exact input (units, expressions, Tab)"),
        # Space confirms EXCEPT while typing a value, where it is a unit
        # separator ("25 cm") - Enter always confirms.
        ("Enter / Space / Click", "Confirm"),
        ("Esc / Right Mouse", "Cancel"),
    ]
    return tuple(rows)


# The addon's identity in Blender's registries is the ROOT package name
# ("bl_ext.<repo>.cut_plane" when installed as an extension), NOT this
# state/ subpackage's own __package__. Both the AddonPreferences bl_idname
# and the preferences.addons[] lookup key must use it - with the subpackage
# name the lookup silently missed on a formally enabled install and every
# preference fell back to defaults (caught by the fresh-sandbox gate; the
# dev_load/test path never has a formal enable, so the battery cannot see it).
_ADDON_ROOT = __package__.rsplit(".", 1)[0]


def get_prefs():
    """This addon's preferences, or None when not formally enabled
    (dev_load.py sessions, headless) - callers fall back to defaults."""
    try:
        return bpy.context.preferences.addons[_ADDON_ROOT].preferences
    except (KeyError, AttributeError, TypeError):
        return None


def preview_face_cap() -> int:
    """The live-preview density cap: preferences override, constants default."""
    prefs = get_prefs()
    return prefs.preview_face_limit if prefs is not None else CUT_PREVIEW_MAX_FACES


def _update_debug(self, _context) -> None:
    """Apply Debug Mode live: constants.DEBUG drives on_internal_error's
    raise-vs-swallow and _debug_check's invariant asserts; the logger level
    drives visibility. (Modules that bound DEBUG at import only gate extra
    debug prints - those pick the change up on the next addon reload.)"""
    from ..lib import constants
    constants.DEBUG = self.debug_mode
    logger.setLevel(logging.DEBUG if self.debug_mode else logging.INFO)
    logger.info(f"[CUT PLANE] Debug mode {'ON' if self.debug_mode else 'off'}")


def _row_item(km, idname, mode):
    """The KeyMapItem a _KEY_ROWS entry points at, or None if unbound.

    Scanned in REVERSED order because Blender builds the user copy by
    inserting each template item at the front, so the live keymap is the
    exact reverse of _TOOL_KEYMAP (measured, and locked by a test). Reading
    it back in template order is what makes the Cut row own RET rather than
    its NUMPAD_ENTER alias - a plain first-match silently owned the numpad
    key, so rebinding "Cut" left Enter still cutting. A rebind does not move
    an item, so the row keeps owning the same one afterwards.
    """
    for kmi in reversed(km.keymap_items):
        if kmi.idname != idname:
            continue
        if mode is None:
            return kmi
        props = kmi.properties
        if mode in (getattr(props, 'mode', None), getattr(props, 'axis', None)):
            return kmi
    return None


def _binding_warning(rows):
    """A real conflict against the addon's OWN hardcoded keys, or "".

    Checks three things only, all of them self-consistency - never a scan of
    the user's keymap. A native-shadowing warning would fire on nearly every
    letter (20 of 26 collide with something in Edit Mesh) and teach the user
    to ignore it; shadowing while the tool is active IS how tool keymaps
    work.

    A DISABLED binding is skipped throughout: it cannot fire, so it cannot
    conflict - it used to produce a phantom "Move and Rotate share R" for a
    Rotate the user had switched off.
    """
    from ..ui.tool import _MOD_ATTRS

    def mods(kmi):
        # -1 is KM_ANY ("either state"); 1 is "must be held".
        return tuple(getattr(kmi, m, 0) for m in _MOD_ATTRS)

    chords = []
    for label, kmi in rows:
        if kmi is None or not kmi.active:
            continue
        if kmi.map_type != 'KEYBOARD':
            if kmi.type in {'LEFTMOUSE', 'RIGHTMOUSE'}:
                return f"{label} on a mouse button breaks select and confirm"
            continue
        # X/Y/Z are consumed by the axis-constraint handler inside a running
        # transform, before the mid-modal switch table ever sees them. Only a
        # chord that can fire with NO modifier held is affected - and KM_ANY
        # (-1) can, which a plain truth test missed (it reads as "held").
        if (label in {"Move", "Rotate", "Scale"} and kmi.type in {'X', 'Y', 'Z'}
                and all(m != 1 for m in mods(kmi))):
            return f"X / Y / Z is the axis constraint - {label} will not work"
        chords.append((label, (kmi.type,) + mods(kmi)))
    for i, (label_a, chord_a) in enumerate(chords):
        for label_b, chord_b in chords[i + 1:]:
            if chord_a == chord_b:
                return f"{label_a} and {label_b} share {chord_a[0]}"
    return ""


class CutPlaneAddonPreferences(AddonPreferences):
    """Rendered in Preferences > Add-ons when installed as a regular addon."""
    bl_idname = _ADDON_ROOT  # the addon's module name, not this subpackage

    debug_mode: bpy.props.BoolProperty(
        name="Debug Mode",
        description="Verbose logging, and internal errors raise loudly "
                    "instead of being swallowed - turn on when reporting a "
                    "bug (equivalent to the CUT_PLANE_DEBUG environment "
                    "variable; some extra debug prints need an addon reload)",
        default=False,
        update=_update_debug,
    )
    preview_face_limit: bpy.props.IntProperty(
        name="Live Preview Face Limit",
        description="Above this face count the destructive live preview is "
                    "skipped (a per-mousemove re-bisect of a very dense mesh "
                    "would stall the viewport); Execute Cut always works. "
                    "Lower it on slow machines, raise it on fast ones",
        default=CUT_PREVIEW_MAX_FACES,
        min=1000,
        soft_max=2_000_000,
    )

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        layout.prop(self, "debug_mode")
        layout.prop(self, "preview_face_limit")

        layout.separator()
        self._draw_keys(layout)

        layout.separator()
        col = layout.column(align=True)
        col.label(text="While a transform is running")
        for keys, what in _shortcuts():
            row = col.split(factor=0.4)
            row.label(text="    " + keys)
            row.label(text=what)

    @staticmethod
    def _draw_keys(layout):
        """The tool's real bindings, one native hotkey button per row."""
        from ..ui.tool import tool_keymap_editable
        km = tool_keymap_editable()
        if km is None:
            layout.label(text="Shortcuts unavailable in this session",
                         icon='INFO')
            return

        col = layout.column()
        col.use_property_split = False
        # preferences.keyitem_restore reads context.keymap.
        col.context_pointer_set("keymap", km)

        rows = [(label, _row_item(km, idname, mode))
                for label, idname, mode in _KEY_ROWS]
        for label, kmi in rows:
            split = col.split(factor=0.4)
            split.label(text=label)
            sub = split.row(align=True)
            if kmi is None:
                sub.label(text="unbound")
                continue
            # The enable checkbox, exactly as Blender's own keymap editor
            # draws it (rna_keymap_ui.py:143). Not decoration: an item can
            # be switched OFF in Preferences > Keymap, and without this the
            # row rendered a live-looking key that did nothing. Left
            # undimmed so it is always clickable back on.
            sub.prop(kmi, "active", text="", emboss=False)
            keyrow = sub.row(align=True)
            keyrow.active = kmi.active   # grey the chord when it cannot fire
            keyrow.prop(kmi, "type", text="", full_event=True)
            # Blender never ships an editable kmi without its restore arrow
            # (rna_keymap_ui.py:178); without it a user who breaks a binding
            # here would have to leave for another tab to undo it.
            if (not kmi.is_user_defined) and kmi.is_user_modified:
                keyrow.operator("preferences.keyitem_restore", text="",
                                icon='BACK').item_id = kmi.id
            else:
                keyrow.label(text="", icon='BLANK1')

        warning = _binding_warning(rows)
        if warning:
            col.label(text=warning, icon='ERROR')

        col.separator()
        col.label(text="All bindings incl. box select: Preferences > Keymap >")
        col.label(text='"3D View Tool: Edit Mesh, Cut Plane"')
