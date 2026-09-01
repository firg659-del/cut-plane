# SPDX-License-Identifier: GPL-3.0-or-later
"""Realtime cut preview via live bisect.

The preview *is* the cut. While the cut plane tool is active, the mesh is
bisected for real along the gizmo plane, so the preview is genuine Blender-
rendered geometry - exact shading, wireframe, and selection, with no
approximation. Clear None keeps both sides (split only, the cut loop shows
where the plane is); Inner/Outer remove a side. A snapshot of the original mesh
is kept so it is restored on every exit: changing the plane re-cuts from the
snapshot; leaving edit mode, switching away from the tool, Execute, saving the
file, and addon unload all restore the original.

This replaced an earlier non-destructive approach (a GPU contour line + a
clipped "band" + per-element hide). That was accurate but did its geometry
maths in Python every frame; the live bisect runs in Blender's C core, so it is
both exact and faster on dense meshes. The trade-off is that it is destructive
while active (a plane move re-cuts from the snapshot) and undo during the
preview is unreliable - acceptable for a positioning preview that is always
restored on exit.

Package layout (one module per concern, dependency-ordered):

    state        the per-object cache, session registry, shared latches
    plane        plane pose, local-space transform, mirror expansion, keys
    snapshot     take / validate / rebuild-from / drop the original mesh
    invariants   self-instrumentation over the session state
    snap_support snapshot BVH + baseline queries for snapping and the UI
    selection    remap / clear / reselect / repair / loop-select semantics
    preview      the per-frame apply orchestrator + restore + adoption
    watchdog     tool-active POST_VIEW watchdog + deferred sync timer
    handlers     save/load/undo/redo handlers, orphan purge, crash recovery

This __init__ is the subsystem's single import surface: every name callers
(and tests) used on the old one-file module is re-exported here, so
``from . import cutting`` works exactly as before the split.
"""

from . import (  # noqa: F401
    handlers,
    invariants,
    plane,
    preview,
    selection,
    snap_support,
    snapshot,
    state,
    watchdog,
)
from .handlers import (  # noqa: F401
    _SESSION_HANDLERS,
    _on_load_post,
    _on_save_post,
    _on_save_pre,
    _on_undo_redo_post,
    _purge_orphan_snapshots,
    _recover_orphan_snapshots,
    _schedule_purge,
    _schedule_recovery,
    register_cut_session_handlers,
    unregister_cut_session_handlers,
)
from .invariants import (  # noqa: F401
    _debug_check,
    check_invariants,
    count_snapshot_datablocks,
)
from .plane import (  # noqa: F401
    _coincident_plane_key,
    _mirror_axes,
    _mirror_pivot_key,
    _mirror_pivot_local,
    _mirrored_planes_local,
    _mirror_reflection_world,
    _mirror_world_frame,
    _mirrored_planes_world,
    _plane_key,
    get_plane_world,
    plane_to_object_local,
)
from .preview import (  # noqa: F401
    _apply_live_cut,
    _build_domain,
    _ensure_live_session,
    _fold_pending_selection,
    _preview_block_warned,
    _restore_session,
    _sync_baseline,
    _warn_preview_blocked,
    adopt_committed_baseline,
    live_preview_blocker,
    refresh_live_preview,
    restore_live_cut,
    selection_change_pending,
    update_live_preview,
)
from .selection import (  # noqa: F401
    _remap_selection_to_baseline,
    apply_cut_selection,
    clear_domain_side,
    repair_split_selection,
    reselect_cut_domain,
)
from .snap_support import (  # noqa: F401
    _get_or_build_snapshot_bvh,
    baseline_center_offset,
    baseline_selection_counts,
    get_live_snapshot_mesh,
    raycast_original,
)
from .snapshot import (  # noqa: F401
    _foreign_edit_happened,
    _free_live_snapshot,
    _live_rebuild_from_snapshot,
    _live_take_snapshot,
    _record_written_counts,
    _reset_live_state,
    _resolve_live_object,
    _selection_changed_since_write,
    _selection_counts,
    _selection_matches_snapshot,
    _snapshot_bounds,
    _snapshot_valid,
)
from .state import (  # noqa: F401
    _RECOVERY_TARGET_PROP,
    _RECOVERY_TOKEN_PROP,
    _SNAPSHOT_NAME,
    CutPreviewCache,
    _edit_mesh_objects,
    _session_for,
    _sessions,
    topology_changed,
)
from .watchdog import (  # noqa: F401
    _WATCHDOG_KEY,
    _deferred_sync,
    _live_cut_watchdog,
    _tool_active_any_window,
    register_live_cut_watchdog,
    unregister_live_cut_watchdog,
)

# The five REASSIGNED scalars cannot be re-exported by name: a `from`-import
# would bind this module to their import-time values while the owning module
# rebinds its own attribute (state's module docstring documents the rule).
# PEP 562 module __getattr__ forwards reads to the live attribute instead, so
# `cutting._op_pending` stays truthful. Writers must go through the owning
# module (`cutting.state._op_pending = ...`) - which is also what tests that
# monkeypatch internals target directly.
_FORWARDED_SCALARS = {
    "_op_pending": state,
    "_pending_action": state,
    "_last_tool_active": state,
    "_restore_after_save": handlers,
    "_readopt_after_save": handlers,
}


def __getattr__(name):
    owner = _FORWARDED_SCALARS.get(name)
    if owner is not None:
        return getattr(owner, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
