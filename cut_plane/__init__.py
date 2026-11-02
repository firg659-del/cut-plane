# SPDX-License-Identifier: GPL-3.0-or-later
# Cut Plane
# Author: Firg <firgcg@gmail.com>
#
# Edit Mode bisect tool driven by an interactive gizmo plane: move, rotate
# and scale the plane (gizmos or G/R/S), watch the cut result live on the
# mesh, press Enter to commit. The plane defaults to the mesh bbox center.

import bpy

from . import cutting
from .state import persistence
from .ui import drawing
from .lib.constants import DEBUG, logger
from .ui.gizmos import CutPlaneGroup, CutPlaneVisual
from .operators.cut import CutPlaneCutOperator
from .operators.modal_base import legend_keymap_cleanup
from .operators.move import CutPlaneMoveAxisOperator, CutPlaneMoveCenterOperator
from .operators.rotate import (
    CutPlaneRotateViewOperator,
    CutPlaneRotateXOperator,
    CutPlaneRotateYOperator,
    CutPlaneRotateZOperator,
    CutPlaneTrackballOperator,
)
from .operators.reset import CutPlaneAlignOperator, CutPlaneResetOperator
from .operators.scale import CutPlaneScalePlaneOperator
from .operators.undo import CutPlaneRedoOperator, CutPlaneUndoOperator
from .state.properties import (
    CutPlaneProperties,
    CutPlaneRuntimeProperties,
    handle_orientation_or_mode_change,
)
from .state.preferences import CutPlaneAddonPreferences
from .ui.tool import (
    CUT_PLANE_PT_cut_options,
    CUT_PLANE_PT_snapping,
    CutPlaneActivateOperator,
    CutPlaneTool,
    _ensure_tool_unregistered,
    register_menu,
    unregister_menu,
)

# No bl_info: blender_manifest.toml is the single metadata source (the addon
# ships as a Blender EXTENSION; legacy addon installs are not supported,
# and a duplicated version string here would only drift).

# Registered in order; CutPlaneProperties is handled separately because the
# WindowManager pointer must exist before CutPlaneGroup.setup() can run.
_classes = (
    CutPlaneVisual,
    CutPlaneRotateXOperator,
    CutPlaneRotateYOperator,
    CutPlaneRotateZOperator,
    CutPlaneRotateViewOperator,
    CutPlaneTrackballOperator,
    CutPlaneMoveAxisOperator,
    CutPlaneMoveCenterOperator,
    CutPlaneScalePlaneOperator,
    CutPlaneResetOperator,
    CutPlaneAlignOperator,
    CutPlaneCutOperator,
    CutPlaneUndoOperator,
    CutPlaneRedoOperator,
    CutPlaneActivateOperator,
    CutPlaneGroup,
    CUT_PLANE_PT_snapping,
    CUT_PLANE_PT_cut_options,
    CutPlaneAddonPreferences,
)


def register():
    """Register addon classes and properties."""
    # 4.5 LTS is the support floor (4.2 LTS reached end of life 2026-07;
    # tested on 4.5 LTS and 5.x). Extensions enforce blender_version_min,
    # but dev_load.py sideloads bypass the manifest - hence the hard check.
    if bpy.app.version < (4, 5, 0):
        raise RuntimeError("Cut Plane requires Blender 4.5 or newer")

    registered = []
    try:
        bpy.utils.register_class(CutPlaneProperties)
        registered.append(CutPlaneProperties)
        # WindowManager, NOT Scene: undo never touches WM data, so neither
        # Ctrl+Z nor F9's undo half can move the plane (the pose is tool
        # state). persistence.py handles .blend save/load for this group.
        bpy.types.WindowManager.cut_plane = bpy.props.PointerProperty(
            type=CutPlaneProperties)

        # Transient state: WindowManager-level, never saved, no depsgraph
        bpy.utils.register_class(CutPlaneRuntimeProperties)
        registered.append(CutPlaneRuntimeProperties)
        bpy.types.WindowManager.cut_plane_runtime = bpy.props.PointerProperty(
            type=CutPlaneRuntimeProperties)

        for cls in _classes:
            bpy.utils.register_class(cls)
            registered.append(cls)
    except Exception:
        # Roll back partial registration so a failed register() leaves
        # Blender in a clean state and register() can be retried.
        if hasattr(bpy.types.WindowManager, "cut_plane"):
            del bpy.types.WindowManager.cut_plane
        if hasattr(bpy.types.WindowManager, "cut_plane_runtime"):
            del bpy.types.WindowManager.cut_plane_runtime
        for cls in reversed(registered):
            try:
                bpy.utils.unregister_class(cls)
            except Exception:
                pass
        raise

    # Register workspace tool (idempotent across dev reloads).
    # bpy.utils.unregister_tool uses cls._bl_tool, which only exists on the
    # original class object - a fresh import creates a new class without it,
    # so any stale toolbar entry must be removed by idname before registering.
    # The unregister MUST precede register_tool: _km_action_simple only
    # populates a keymap `if km is None`, so registering over a surviving
    # addon keymap would silently NOT rebuild it. bl_keymap is the frozen
    # _TOOL_KEYMAP constant - nothing to resolve, no re-registration.
    _ensure_tool_unregistered(CutPlaneTool)
    bpy.utils.register_tool(CutPlaneTool, separator=True, group=False)

    # Materialize the USER keyconfig's copy of the tool keymap: register_tool
    # writes the default/addon configs, and the user copy (which is what
    # actually fires, what the preferences panel edits, and what
    # transform_chords reads) only appears once the keyconfig is rebuilt.
    # Exactly ONE call, here - it rebuilds the whole user keyconfig and
    # dangles Python keymap references, so it never belongs on a hot path.
    try:
        bpy.context.window_manager.keyconfigs.update()
    except Exception:
        logger.exception("[CUT PLANE] keyconfigs.update() failed")

    # Mesh-menu entry (F3-searchable, user-bindable) - the native way a
    # toolbar tool without a default key exposes itself, like Bisect.
    register_menu()

    # Reset the status-legend display keymap: a dev reload starts with an
    # empty item cache, so a keymap surviving from the previous session
    # would otherwise accumulate duplicate items. No-op at startup
    # (restricted context) - the keymap is built lazily on first use anyway.
    legend_keymap_cleanup()

    # Register arc draw handler for rotation visualization
    drawing.register_arc_draw_handler()

    # Register the live-cut watchdog (restores the preview when the tool is left)
    cutting.register_live_cut_watchdog()

    # Keep the destructive live-cut preview out of saved files and reset the
    # session on file load / undo / redo (persistent handlers, deduped by __name__).
    cutting.register_cut_session_handlers()

    # Tool state lives on the WindowManager (undo-proof) - stamp it into the
    # scene on save and restore/reset it on load.
    persistence.register_persistence_handlers()

    # Register orientation/mode change handler.
    # Identity-based `in` check breaks across dev reloads (fresh function
    # object each time), so remove any stale handler with the same __name__
    # - scoped to OUR module path, so a foreign add-on's handler that
    # happens to share the name can never be removed.
    _handler_name = handle_orientation_or_mode_change.__name__
    bpy.app.handlers.depsgraph_update_post[:] = [
        h for h in bpy.app.handlers.depsgraph_update_post
        if not (getattr(h, "__name__", None) == _handler_name
                and getattr(h, "__module__", "").startswith(__package__))
    ]
    bpy.app.handlers.depsgraph_update_post.append(handle_orientation_or_mode_change)

    if DEBUG:
        logger.debug("[CUT PLANE] Registered successfully")


def unregister():
    """Unregister addon classes and cleanup."""
    # Unregister orientation/mode change handler FIRST.
    # Remove by __name__ + our module path (stale reload copies match too;
    # a foreign add-on's same-named handler never does).
    bpy.app.handlers.depsgraph_update_post[:] = [
        h for h in bpy.app.handlers.depsgraph_update_post
        if not (getattr(h, "__name__", None) == handle_orientation_or_mode_change.__name__
                and getattr(h, "__module__", "").startswith(__package__))
    ]

    drawing.unregister_arc_draw_handler()
    drawing.cleanup_gpu_resources()
    cutting.unregister_live_cut_watchdog()
    cutting.unregister_cut_session_handlers()
    persistence.unregister_persistence_handlers()
    legend_keymap_cleanup()
    # Final sweep: anything still in the handler store (a handle the
    # specific unregisters above missed, e.g. after a partial register)
    # is removed here - a lost handle can otherwise never be removed.
    from .lib import handler_store
    handler_store.clear_all()

    try:
        unregister_menu()
    except Exception:
        pass  # Menu may already be gone (partial register / reload)

    try:
        bpy.utils.unregister_tool(CutPlaneTool)
    except Exception:
        pass  # Tool may not be registered or in use

    for cls in reversed(_classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass  # Keep going so one failure doesn't leak the rest

    # Remove PropertyGroups AFTER gizmo classes are unregistered
    # (prevents access to deleted properties during gizmo cleanup)
    if hasattr(bpy.types.WindowManager, "cut_plane"):
        del bpy.types.WindowManager.cut_plane
    if hasattr(bpy.types.WindowManager, "cut_plane_runtime"):
        del bpy.types.WindowManager.cut_plane_runtime

    try:
        bpy.utils.unregister_class(CutPlaneRuntimeProperties)
    except Exception:
        pass
    try:
        bpy.utils.unregister_class(CutPlaneProperties)
    except Exception:
        pass

    if DEBUG:
        logger.debug("[CUT PLANE] Unregistered successfully")
