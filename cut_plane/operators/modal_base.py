# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared scaffolding for the five plane-transform modal operators.

Move (axis + center/plane/free), rotate (dials + trackball), and scale all
follow one modal skeleton; before this mixin each operator carried its own
copy of the guards, the numeric-input dispatch, and the confirm/cancel
tails (~200 duplicated lines, and the force-cancel paths had drifted from
the Esc paths). The mixin owns the skeleton; subclasses own the transform.

EVENT PRECEDENCE (fixed, matches what every modal already did):

  1. numeric type-in - while the user types, the value owns the transform
     (apply_numeric hook)
  2. operator-specific events - constraint keys, mode toggles, MOUSEMOVE
     (handle_modal_event hook; return a result set to consume the event,
     None to fall through - consuming preserves the historical if/elif
     semantics where a processed event never also confirms/cancels)
  2b. transform switching - G/R/S during a KEYBOARD transform restores the
     start pose and hands over to the other transform, native-style
     (native also restarts from scratch on a mid-modal mode switch); the
     active X/Y/Z constraint carries over, also native-style (G-X then R
     rotates around X) - see kb_constraint_state / adopt_kb_constraint
  3. confirm - LMB release or Enter press (on_finish hook, then the shared
     header-clear/redraw/release trailer, then after_finish for
     post-release work)
  4. cancel - RMB or Esc (on_cancel hook + the same trailer)

FORCE-CANCEL (window closed / file load mid-drag) reuses on_cancel, so the
Esc path and the force path can never drift apart again.
"""

import bpy

from ..lib.math_utils import is_plane_tool_active
from .utils import prevent_reentry, release_on_error, release_operator

# Mid-modal transform switching (step 2b): which operator each transform
# binding hands over to, in (move, rotate, scale) order - zipped against
# tool.transform_chords() at event time, so a user's rebind drives the
# switching too. bpy.ops.view3d attribute names; the successor's invoke
# reads the SAME key event, so it stamps itself keyboard-invoked too.
_TRANSFORM_SWITCH_OPS = ("cut_plane_move_center", "cut_plane_rotate_view",
                         "cut_plane_scale_plane")

# --- status-bar key legend ---------------------------------------------------
#
# The legends draw through template_event_from_keymap_item - the template
# Blender's own toolbar prompt uses for its status legend (bl_operators/
# wm.py). It renders a KeyMapItem's key chord with the NATIVE metrics
# (variable-width boxed icons, flush icon-to-label spacing); plain labels
# with 'EVENT_*' icons get fixed one-unit icon slots, so wide keys (Ctrl,
# Esc) overflow and narrow ones leave gaps. The template needs real
# KeyMapItems as DATA, so key specs are materialized into a private keymap
# whose name matches no event context - its items can never fire.

_LEGEND_KEYMAP_NAME = "Cut Plane Status Legend"
# Never registered as an operator; the keymap never activates, so the
# items are pure display data for the template.
_LEGEND_KMI_IDNAME = "cut_plane.legend_key"

_legend_kmi_cache = {}


_LEGEND_MODS = ("ctrl", "shift", "alt", "oskey")


def _legend_keymap_item(spec):
    """KeyMapItem for a key spec, cached: an event type ('X', 'LEFTMOUSE',
    'RET', ...) with optional 'ctrl+'/'shift+'/'alt+'/'oskey+' prefixes
    (the rotate legend builds one from the live, rebindable chord). None
    when no addon keyconfig is reachable (background / restricted context)."""
    kmi = _legend_kmi_cache.get(spec)
    if kmi is not None:
        try:
            kmi.type  # dead-RNA check: a keyconfig reset kills the items
        except ReferenceError:
            _legend_kmi_cache.clear()  # the whole keymap died together
            kmi = None
    if kmi is not None:
        return kmi
    wm = getattr(bpy.context, 'window_manager', None)
    kc = wm.keyconfigs.addon if wm is not None else None
    if kc is None:
        return None
    km = kc.keymaps.get(_LEGEND_KEYMAP_NAME)
    if km is None:
        km = kc.keymaps.new(_LEGEND_KEYMAP_NAME, space_type='EMPTY')
    prefixes = spec.split("+")[:-1]
    kmi = km.keymap_items.new(
        _LEGEND_KMI_IDNAME, spec.rsplit("+", 1)[-1], 'PRESS',
        **{m: (m in prefixes) for m in _LEGEND_MODS})
    _legend_kmi_cache[spec] = kmi
    return kmi


def legend_keymap_cleanup() -> None:
    """Drop the display-data keymap and its item cache.

    Called on unregister, and on register too: a dev reload starts with an
    empty cache, so a keymap surviving from the previous session would
    otherwise accumulate duplicate items.
    """
    _legend_kmi_cache.clear()
    wm = getattr(bpy.context, 'window_manager', None)
    kc = wm.keyconfigs.addon if wm is not None else None
    if kc is None:
        return
    km = kc.keymaps.get(_LEGEND_KEYMAP_NAME)
    if km is not None:
        kc.keymaps.remove(km)


# Event type -> icon for the chord-prefix keys (the ones drawn icon-only).
# Anything absent falls back to 'EVENT_<type>', which covers the letter keys.
_SPEC_ICON = {
    'LEFTMOUSE': 'MOUSE_LMB',
    'MIDDLEMOUSE': 'MOUSE_MMB',
    'RIGHTMOUSE': 'MOUSE_RMB',
    'RET': 'EVENT_RETURN',
    'ESC': 'EVENT_ESC',
    'LEFT_SHIFT': 'EVENT_SHIFT',
    'LEFT_CTRL': 'EVENT_CTRL',
    'ZERO': 'EVENT_ZEROKEY',  # digit-key icons carry a KEY suffix
}

# Keys whose icons are WIDER than one icon unit. Prefix icons draw in plain
# labels with fixed one-unit slots, so a wide glyph there overlaps the next
# icon - wide keys must be the LAST key of an entry, where the template
# sizes the icon correctly. The legends below comply by construction; this
# set is the rule as data, consumed by tests/integration/status_legend.py
# to lock the invariant.
_WIDE_KEYS = {'ESC', 'LEFT_CTRL', 'LEFT_ALT', 'SPACE', 'TAB'}


def _spec_icons(spec):
    """Icon name(s) for a key spec: the modifier icon (if any) + the key."""
    icons = []
    if spec.startswith("shift+"):
        icons.append('EVENT_SHIFT')
    etype = spec.rsplit("+", 1)[-1]
    icons.append(_SPEC_ICON.get(etype, 'EVENT_' + etype))
    return icons


def _legend_draw(legend):
    """Build a status-bar draw callback for a key legend.

    workspace.status_text_set also accepts a FUNCTION drawing into the
    status bar's layout. Per entry: all keys but the last render as plain
    icon-only labels (tight chord - a template call with empty text pads a
    blank label after every icon), and the LAST key renders through
    template_event_from_keymap_item so its label sits flush after the icon
    exactly like native. When the keymap item is unavailable (background
    mode) the last key falls back to an icon-only label + plain text.
    """
    entries = []
    for keys, label in legend:
        prefix = [icon for spec in keys[:-1] for icon in _spec_icons(spec)]
        final = _legend_keymap_item(keys[-1]) if keys else None
        if keys and final is None:
            prefix += _spec_icons(keys[-1])
        entries.append((prefix, final, label))

    def draw(header, context):
        row = header.layout.row(align=True)
        for index, (prefix, final, label) in enumerate(entries):
            if index:
                # Small gap between entries: the labels' own margins provide
                # most of the native spacing already (2.0 read too airy).
                row.separator(factor=0.5)
            for icon in prefix:
                row.label(text="", icon=icon)
            if final is not None:
                row.template_event_from_keymap_item(final, text=label)
            else:
                row.label(text=label)
    return draw


class CutPlaneModalMixin:
    """Template-method base: mix in BEFORE bpy.types.Operator.

    Subclass contract (all hooks except setup/apply_numeric are optional):
      setup(context, event)             invoke-time state capture; return a
                                        result set (e.g. {'CANCELLED'}) to
                                        abort, None to start the modal
      apply_numeric(context, props)     re-apply the transform from the
                                        typed value (self.num_input)
      handle_modal_event(context,       operator-specific keys + MOUSEMOVE;
          event, props, runtime)        result set = consume, None = fall
                                        through to confirm/cancel
      on_finish(context, props,         commit-time state (bake offsets,
          runtime)                      clear runtime flags, drop markers)
      after_finish(context)             work that must run AFTER the guard
                                        is released (e.g. deferred timers)
      on_cancel(context, props,         restore invoke-time state; ALSO the
          runtime)                      force-cancel body, so runtime may
                                        be None there - guard accordingly

    require_runtime: the wm runtime group is fetched and validated before
    any hook runs (operators that draw arcs / hide handles need it).
    """

    require_runtime = False

    @classmethod
    def poll(cls, context):
        """Tool-scoped: these operators pose the Cut Plane gizmo, so they
        are meaningful only while the tool is active on an edit mesh. The
        real entry points (gizmos, the tool keymap) imply that already -
        this keeps the operators honestly greyed out in F3 search and the
        keymap editor too. invoke() keeps its own guards for scripted calls
        that bypass poll."""
        return (context.mode == 'EDIT_MESH'
                and context.edit_object is not None
                and getattr(context.window_manager, 'cut_plane', None) is not None
                and is_plane_tool_active(context))

    # Hand-over parcel for the mid-modal transform switch (step 2b): the
    # predecessor's (kind, axis, space) constraint, alive ONLY around the
    # synchronous successor invoke (set/cleared in a try/finally there).
    # A class attribute because bpy.ops kwargs can't carry a Python tuple.
    _switch_constraint = None

    # --- hooks --------------------------------------------------------------

    def setup(self, context, event):
        raise NotImplementedError

    def apply_numeric(self, context, props) -> None:
        raise NotImplementedError

    def handle_modal_event(self, context, event, props, runtime):
        return None

    def on_finish(self, context, props, runtime) -> None:
        pass

    def after_finish(self, context) -> None:
        pass

    def on_cancel(self, context, props, runtime) -> None:
        pass

    def kb_constraint_state(self):
        """The keyboard X/Y/Z constraint to carry across a mid-modal
        transform switch: a (kind, axis, space) tuple (see
        utils.next_kb_constraint) or None. Overridden by operators with
        constraint machinery; the default transform has none."""
        return None

    def adopt_kb_constraint(self, context, event, constraint) -> None:
        """Receive the predecessor's carried constraint right after setup()
        on a mid-modal switch (step 2b). Native G-X -> R semantics: the
        successor starts already constrained. Default: no constraint
        concept, the carry is dropped (e.g. trackball)."""

    def status_legend(self):
        """Bottom status-bar key legend shown while this modal runs, or None.

        A legend is a tuple of ((key spec, ...), label) entries rendered as
        native key chords (see _legend_draw). Default reads a
        `_status_legend` class attribute so most operators just set one;
        override for a legend that depends on state (e.g. rotate).
        """
        return getattr(self, '_status_legend', None)

    @staticmethod
    def _status_text_set(context, legend) -> None:
        """Show (legend tuple) or clear (None) the status-bar legend, safely."""
        ws = getattr(context, 'workspace', None)
        if ws is not None:
            ws.status_text_set(_legend_draw(legend) if legend else None)

    # --- the skeleton ---------------------------------------------------------

    @prevent_reentry
    def invoke(self, context, event):
        if not hasattr(context.window_manager, 'cut_plane'):
            return {'CANCELLED'}
        # Key-invoked (tool G/R/S) vs gizmo-handle drag: drives the visual
        # policy (keyboard transforms hide the gizmo group, overlay-only) -
        # see operators.utils.any_keyboard_transform_active.
        from ..ui.tool import chord_matches, transform_chords
        self._keyboard_invoked = any(chord_matches(c, event)
                                     for c in transform_chords())
        # Pose-undo bookkeeping: snapshot the pose BEFORE this transform so
        # the confirm branch can record the change (tool-scoped Ctrl+Z).
        from ..state import pose_history
        self._pose_before = pose_history.capture_pose(context)
        result = self.setup(context, event)
        if result is not None:
            return result
        # Mid-modal switch hand-over: engage the predecessor's constraint
        # (parcel set around the synchronous invoke in step 2b)
        carried = CutPlaneModalMixin._switch_constraint
        if carried is not None:
            self.adopt_kb_constraint(context, event, carried)
        context.window_manager.modal_handler_add(self)
        legend = self.status_legend()
        if legend:
            self._status_text_set(context, legend)
        return {'RUNNING_MODAL'}

    @release_on_error
    def modal(self, context, event):
        # The addon's property group can vanish mid-modal (unregister)
        if not hasattr(context.window_manager, 'cut_plane'):
            release_operator(self)
            return {'CANCELLED'}
        props = context.window_manager.cut_plane

        runtime = None
        if self.require_runtime:
            from ..state.properties import get_runtime
            runtime = get_runtime(context)
            if runtime is None:
                release_operator(self)
                return {'CANCELLED'}

        # 1. Numeric type-in: while active, the typed value owns the transform
        if self.num_input.handle_event(event):
            self.apply_numeric(context, props)
            if context.area:
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        # 2. Operator-specific events (constraint keys, toggles, MOUSEMOVE)
        result = self.handle_modal_event(context, event, props, runtime)
        if result is not None:
            return result

        # 2b. Native mid-modal transform switching: G/R/S during a KEYBOARD
        # transform restores the start pose and hands over (native restarts
        # from scratch on switch too). Operator-specific keys won in step 2
        # (R during view-rotate already went to trackball); a same-key press
        # falls through and does nothing. Handle drags are excluded: their
        # confirm is the LMB release, which would instantly confirm the
        # successor. INVOKE_DEFAULT re-reads the current key event, so the
        # successor stamps itself keyboard-invoked as well.
        # Matched as full CHORDS, modifiers included: a bare type compare
        # made this modifier-blind, so Alt+G during a rotate abandoned it
        # into a move, and Shift+R / Ctrl+R during a move (Shift is the live
        # precision modifier, Ctrl the live snap modifier) did the same in
        # reverse. Native ignores all of those.
        from ..ui.tool import chord_matches, transform_chords
        target = None
        if event.value == 'PRESS':
            for chord, op in zip(transform_chords(), _TRANSFORM_SWITCH_OPS):
                if chord_matches(chord, event):
                    target = op
                    break
        if (target is not None
                and getattr(self, '_keyboard_invoked', False)
                and not self.num_input.active):
            if f"view3d.{target}" != self.bl_idname:
                self.on_cancel(context, props, runtime)
                # Same rule as the Esc branch: re-cut now so the preview
                # snaps back onto the restored pose before the hand-over.
                from .. import cutting
                cutting.update_live_preview(context)
                # Clear our legend/header BEFORE invoking: a failed
                # hand-over (setup CANCELLED) must not pin them stale.
                self._status_text_set(context, None)
                if context.area:
                    context.area.header_text_set(None)
                release_operator(self)
                # The active X/Y/Z constraint carries over, native-style
                # (G-X then R rotates around X). The invoke is synchronous,
                # so the parcel lives only inside this try/finally.
                CutPlaneModalMixin._switch_constraint = self.kb_constraint_state()
                try:
                    getattr(bpy.ops.view3d, target)('INVOKE_DEFAULT')
                finally:
                    CutPlaneModalMixin._switch_constraint = None
                return {'FINISHED'}

        # 3. Confirm. SPACE joins the Enter keys, matching the CONFIRM set of
        # Blender's Transform Modal Map. Safe: this BLOCKING modal consumes
        # it long before the 3D view's spacebar_action sees it.
        if ((event.type == 'LEFTMOUSE' and event.value == 'RELEASE')
                or (event.type in {'RET', 'NUMPAD_ENTER', 'SPACE'}
                    and event.value == 'PRESS')):
            self.on_finish(context, props, runtime)
            # A confirmed transform is one pose-undo entry (no-op when the
            # pose didn't actually change - a click without a drag)
            from ..state import pose_history
            pose_history.push_if_changed(context, getattr(self, '_pose_before', None))
            self._status_text_set(context, None)
            if context.area:
                context.area.header_text_set(None)
                context.area.tag_redraw()  # gizmos reappear at once (kbd invoke)
            release_operator(self)
            self.after_finish(context)
            return {'FINISHED'}

        # 4. Cancel
        if event.type in {'RIGHTMOUSE', 'ESC'}:
            self.on_cancel(context, props, runtime)
            # on_cancel restored the plane to its start pose, but tool-property
            # writes don't tag the depsgraph, so the preview would stay frozen at
            # the cancelled pose until the next move. Re-cut now so the cut line
            # snaps back onto the restored plane matrix immediately.
            from .. import cutting
            cutting.update_live_preview(context)
            self._status_text_set(context, None)
            if context.area:
                context.area.header_text_set(None)
                context.area.tag_redraw()
            release_operator(self)
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    def cancel(self, context):
        """Force-cancel cleanup (window closed / file load mid-drag).

        The Esc branch of modal() never runs in this path, so restore via
        the same on_cancel hook and release the re-entry guard. Idempotent;
        runtime may legitimately be gone - hooks guard for None.
        """
        props = getattr(context.window_manager, 'cut_plane', None)
        if props is not None:
            from ..state.properties import get_runtime
            self.on_cancel(context, props, get_runtime(context))
            # Same rule as the Esc/RMB branch: on_cancel restored the plane
            # pose, but tool-property writes don't tag the depsgraph - re-cut
            # so a still-live viewport (e.g. area fullscreen toggled mid-drag)
            # doesn't keep the preview frozen at the cancelled pose.
            from .. import cutting
            cutting.update_live_preview(context)
        self._status_text_set(context, None)
        if getattr(context, 'area', None):
            context.area.header_text_set(None)
        release_operator(self)
