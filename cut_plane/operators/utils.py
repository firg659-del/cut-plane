# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared modal-operator utilities (re-entry guard)."""

import sys

_IS_MACOS = sys.platform == 'darwin'


def ctrl_like(event) -> bool:
    """Ctrl held - or Cmd on macOS, where it substitutes for Ctrl.

    Blender's manual (Platform Specific Keys): "The Cmd key can be used
    instead of Ctrl on macOS for all but a few exceptions." Native code gets
    that substitution in the C event layer; our modal event reads must apply
    it themselves (measured on an M2: Cmd did nothing where Ctrl worked).
    Gated to macOS so the Windows/Linux OS key keeps meaning nothing here.
    """
    return event.ctrl or (_IS_MACOS and event.oskey)


def prevent_reentry(invoke_method):
    """
    Decorator that prevents operator re-entry using a class-level _active_instance guard.

    Wraps invoke() to check if another instance is already running,
    sets _active_instance on entry, and clears it on exception or any
    non-RUNNING_MODAL result (early CANCELLED must not leave the guard set,
    or the operator would be permanently locked out).
    Modal operators must still clear _active_instance on FINISHED/CANCELLED.
    """
    def wrapper(self, context, event):
        if getattr(self.__class__, '_active_instance', None) is not None:
            return {'CANCELLED'}
        self.__class__._active_instance = self
        try:
            result = invoke_method(self, context, event)
        except Exception:
            release_operator(self)
            raise
        if 'RUNNING_MODAL' not in result:
            release_operator(self)
        return result
    return wrapper


def release_on_error(modal_method):
    """
    Decorator for modal(): release the re-entry guard if modal raises.

    Blender cancels a modal operator whose modal() raised, but does NOT
    call cancel() in that path - without this wrapper the exception
    would leave _active_instance set and permanently lock the operator
    out until re-registration. The exception still propagates so the
    traceback reaches the console.
    """
    def wrapper(self, context, event):
        try:
            return modal_method(self, context, event)
        except Exception:
            release_operator(self)
            raise
    return wrapper


def release_operator(operator) -> None:
    """Clear _active_instance on the operator's class (call on FINISHED/CANCELLED)."""
    operator.__class__._active_instance = None


def next_kb_constraint(current, kind: str, axis: str):
    """Native-style constraint cycling for the keyboard X/Y/Z keys.

    A constraint is ``(kind, axis, space)`` - kind 'AXIS' (move along / rotate
    around the axis) or 'PLANE' (move in the plane the axis is normal to,
    Shift+key; translation only), space 'GLOBAL' or 'LOCAL'. Pressing the same
    key cycles native-style: global -> local -> off; a different key (or the
    same key with/without Shift) switches straight to that constraint in
    GLOBAL. Pure function - unit-tested. Shared by the move and rotate modals.
    """
    if current is not None and current[0] == kind and current[1] == axis:
        if current[2] == 'GLOBAL':
            return (kind, axis, 'LOCAL')
        return None  # third press clears the constraint
    return (kind, axis, 'GLOBAL')


def any_keyboard_transform_active() -> bool:
    """True while a KEYBOARD-invoked plane transform (G / R / S) is running.

    Single source for the keyboard-transform visual policy:
      * gizmos.CutPlaneGroup.poll() returns False while this is True, hiding
        the ENTIRE gizmo group - the native pattern (native transform gizmos
        hide themselves while transforming). Without this the group keeps
        drawing mid-modal with stale per-mode state (e.g. MOVE mode's
        draw_prepare never updates the plane's scale, so a keyboard S showed a
        frozen "original" plane plus the live one - the duplicate-plane bug).
      * drawing's POST_VIEW callback draws ONLY the live plane outline - no
        handles, no dials, no rotation arc, no snap marker.

    Gizmo-HANDLE drags run the same operators but keep the classic visuals:
    each invoke() records whether it was key-invoked (_keyboard_invoked), and
    this reads that flag off the running instance. The instance is tracked by
    the same _active_instance guard prevent_reentry maintains - the guard lives
    on each CONCRETE class, so the rotate base class is checked together with
    its subclasses.
    """
    return active_keyboard_transform() is not None


def active_transform():
    """The RUNNING plane-transform operator instance (any invocation), or None.

    Lets the draw layer read constraint state off the live modal (e.g. the
    constraint axis lines) without any extra flag plumbing - the instance is
    the single source of its own state. At most one can run at a time (they
    are all BLOCKING modals guarded by prevent_reentry).

    Lazy imports: this module is imported by the operator modules it lists,
    so the imports resolve at call time (draw handlers / poll - long after
    everything is loaded) to avoid the cycle.
    """
    from .move import CutPlaneMoveAxisOperator, CutPlaneMoveCenterOperator
    from .rotate import CutPlaneRotateBaseOperator, CutPlaneTrackballOperator
    from .scale import CutPlaneScalePlaneOperator
    classes = [
        CutPlaneMoveAxisOperator, CutPlaneMoveCenterOperator,
        CutPlaneTrackballOperator, CutPlaneScalePlaneOperator,
        CutPlaneRotateBaseOperator, *CutPlaneRotateBaseOperator.__subclasses__(),
    ]
    for cls in classes:
        inst = getattr(cls, '_active_instance', None)
        if inst is not None:
            return inst
    return None


def active_keyboard_transform():
    """The RUNNING KEYBOARD-invoked transform operator instance, or None.

    The keyboard/handle distinction drives the visual policy only (hide the
    gizmo group, overlay plane) - constraint lines draw for both kinds.
    """
    inst = active_transform()
    if inst is not None and getattr(inst, '_keyboard_invoked', False):
        return inst
    return None
