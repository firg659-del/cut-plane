# SPDX-License-Identifier: GPL-3.0-or-later
"""Tool-active watchdog: keep the live cut in sync with the tool being active.

Nothing fires on plain tool activation/deactivation (the gizmo modals only run
while dragging), so a cheap POST_VIEW handler (no drawing) watches the active
tool and, via a write-safe one-shot timer, APPLIES the preview as soon as the
tool is activated (so the cut shows without needing to move first) and
RESTORES the original when the tool / edit mode is left. While the tool is
active, an every-turn timer also pauses the preview for as long as an
operator's file browser is open (see dialogs).

The shared latch scalars live in state (state._op_pending & co) and are
accessed as attributes - see state's module docstring for the access rules.
"""

import bpy

from ..lib import handler_store
from ..lib.constants import logger, on_internal_error
from . import dialogs, state
from .preview import pause_for_dialog, restore_live_cut, resume_after_dialog, update_live_preview
from .snapshot import free_fingerprint_mesh
from .state import _sessions


def _deferred_sync():
    """One-shot, write-safe timer: run the apply/restore the watchdog decided.

    CRITICAL: this must NOT re-derive the decision - it just executes
    _pending_action, which the watchdog set from a POST_VIEW draw context.
    (Historically because per-context tool detection false-negatives in a
    region-less timer; the any-window check would survive here, but one
    decision point stays one decision point.) A single dispatcher still lets
    the watchdog self-heal a stuck _op_pending latch via timers.is_registered.
    """
    state._op_pending = False
    action = state._pending_action
    state._pending_action = None
    try:
        ctx = bpy.context
        if action == 'apply':
            # First activation in a scene: seed the tool's own snap settings
            # from the scene's current snap (once), then it's standalone.
            try:
                from ..operators import snapping
                snapping.seed_tool_snap_if_needed(ctx)
            except Exception:
                # Non-critical: log but still show the preview.
                logger.exception("[CUT PLANE] Seeding tool snap failed")
            update_live_preview(ctx)        # tool just active -> show preview
        elif action == 'restore':
            restore_live_cut(ctx)           # tool/edit-mode left -> restore
    except Exception:
        # Timer context: can't re-raise (would just disable the timer); log so
        # an apply/restore bug is visible instead of silently dropped.
        logger.exception("[CUT PLANE] Deferred live-cut sync failed")
    return None  # one-shot timer


def _mirror_pivot_moved(ctx) -> bool:
    """True when the mirrors' anchor no longer matches what the preview was
    cut with: Shift+right-click or Shift+S moves the 3D cursor without any
    operator of ours running, so with Anchor = Cursor the mirrored cut kept
    showing the old location while the ghosts and Execute used the new one.
    Cheap: one key compare per redraw, mirrors only."""
    from .plane import _mirror_axes, _mirror_pivot_key
    from .state import _session_for
    sc = getattr(ctx.window_manager, 'cut_plane', None)
    if sc is None or not _mirror_axes(sc):
        return False
    cache = _session_for(ctx.edit_object)
    key = getattr(cache, 'live_key', None)
    if not key:
        return False
    return key[-1] != _mirror_pivot_key(ctx.edit_object, sc)


def _tool_active_any_window() -> bool:
    """True when Cut Plane is the EDIT_MESH tool of ANY open window's
    workspace - the GLOBAL question the watchdog must ask.

    The watchdog runs per 3D viewport, but it drives ONE global mesh state.
    Asking the per-context is_plane_tool_active made two windows on
    different workspaces fight: the Cut Plane window scheduled 'apply', the
    Tweak window scheduled 'restore', and the mesh bisected/restored every
    frame with the CPU pinned (measured). workspace.tools is readable
    without any region or context override (verified live on 4.5 and 5.1),
    so every viewport now computes the same answer: the preview shows while
    the tool is active anywhere, and restores only when it is active
    nowhere. (Headless has no populated workspace.tools - this reads False
    there, same as the per-context detection always did.)
    """
    try:
        from ..lib.constants import TOOL_IDNAME
        for win in bpy.context.window_manager.windows:
            ref = win.workspace.tools.from_space_view3d_mode(
                'EDIT_MESH', create=False)
            if ref is not None and ref.idname == TOOL_IDNAME:
                return True
    except Exception:
        pass  # tool system unreadable - fail safe to "not active"
    return False


def _live_cut_watchdog() -> None:
    """POST_VIEW: apply the preview on tool activation, restore it on exit.

    Runs with a valid 3D region; the apply/restore decision is made now and
    handed to the deferred timer (which fires region-less and cannot redo
    it). The tool-active question is deliberately the ANY-WINDOW one, never
    this region's own - see _tool_active_any_window.
    """
    if state._op_pending:
        # Self-heal: if the scheduled sync was never serviced (e.g. no timer
        # tick on this redraw path), the latch would otherwise wedge the
        # watchdog off forever. Only keep waiting while the timer is live.
        try:
            if bpy.app.timers.is_registered(_deferred_sync):
                return
        except Exception:
            return
        state._op_pending = False
    try:
        ctx = bpy.context
        active = (getattr(ctx, 'mode', None) == 'EDIT_MESH'
                  and ctx.edit_object is not None
                  and ctx.edit_object.type == 'MESH'
                  and _tool_active_any_window())
    except Exception:
        return

    # Cache for the region-less undo/redo handler (see handlers._on_undo_redo_post).
    state._last_tool_active = active
    if active:
        ensure_dialog_watch()
        if dialogs.operator_file_browser_open():
            return  # the browser watch pauses and resumes the preview

    if active and not _sessions:
        action = 'apply'                    # tool just active -> show preview
    elif _sessions and not active:
        action = 'restore'                  # tool/edit-mode left -> restore
    elif active and _mirror_pivot_moved(ctx):
        action = 'apply'                    # the mirrors' anchor moved
    else:
        return  # preview already matches tool state; nothing to do

    state._pending_action = action
    state._op_pending = True
    try:
        bpy.app.timers.register(_deferred_sync, first_interval=0.0)
    except Exception:
        state._op_pending = False
        state._pending_action = None


def _dialog_watch():
    """Every event-loop turn while the tool is active in Edit Mode: pause the
    preview while an operator's file browser is open (see dialogs).

    App timers run after input handling and before drawing, so a browser
    opened this turn is noticed - and the original restored - before its
    first frame: no input can reach it while the cut shows. The POST_VIEW
    watchdog cannot do this; the viewport need not redraw while a browser
    window has focus. Returning 0.0 re-runs next turn without adding an
    event or a redraw, so Blender keeps its idle sleep.
    """
    try:
        ctx = bpy.context
        if dialogs.operator_file_browser_open():
            if _sessions:
                pause_for_dialog(ctx)
            return 0.0
        if state._dialog_resume is not None:
            resume_after_dialog(ctx)
    except Exception:
        # A timer cannot re-raise (it would just stop); log, then stop
        # rather than log every turn - the watchdog restarts it next redraw.
        logger.exception("[CUT PLANE] File-browser watch failed")
        dialogs.forget_browsers()
        return None
    if _sessions or state._dialog_resume is not None or state._last_tool_active:
        return 0.0
    dialogs.forget_browsers()
    return None


def ensure_dialog_watch() -> None:
    """Start the browser watch unless it is running (idempotent, cheap)."""
    try:
        if not bpy.app.timers.is_registered(_dialog_watch):
            bpy.app.timers.register(_dialog_watch, first_interval=0.0)
    except Exception:
        on_internal_error()


_WATCHDOG_KEY = "__cut_plane_cut_watchdog__"


def register_live_cut_watchdog() -> None:
    """Register the tool-active watchdog (idempotent across dev reloads).

    The handle goes through handler_store: a driver_namespace-only store is
    wiped by every file load, which orphaned this handler (it kept firing on
    every redraw with no way to remove it) and stacked another on re-enable.
    """
    handler_store.add(_WATCHDOG_KEY, bpy.types.SpaceView3D.draw_handler_add,
                      _live_cut_watchdog, (), 'WINDOW', 'POST_VIEW')


def unregister_live_cut_watchdog() -> None:
    """Remove the watchdog, drop any pending sync timer, restore the live cut,
    and reset the cache."""
    handler_store.remove(_WATCHDOG_KEY)
    from .preview import cancel_paced
    cancel_paced()
    # Drop pending timers so they can't fire into a torn-down addon.
    for timer in (_deferred_sync, _dialog_watch):
        try:
            if bpy.app.timers.is_registered(timer):
                bpy.app.timers.unregister(timer)
        except Exception:
            pass  # the timer may fire and self-remove between the check and here
    state._op_pending = False
    state._pending_action = None
    state._dialog_resume = None
    state._input_owned = False
    dialogs.forget_browsers()
    try:
        restore_live_cut(bpy.context)
    except Exception:
        logger.exception("[CUT PLANE] Live-cut restore during unregister failed")
    _sessions.clear()
    state._marked_mesh_uids.clear()
    state._written_fps.clear()
    free_fingerprint_mesh()
