# SPDX-License-Identifier: GPL-3.0-or-later
"""Session state: the per-object cache, the registry, and the shared latches.

Everything mutable that the live-cut subsystem shares lives HERE, in one
module, so every other module has a single import target for it. Two access
rules keep cross-module state truthful:

  * containers (_sessions, and the cache objects inside it) are only ever
    mutated IN PLACE, so ``from .state import _sessions`` aliases stay live;
  * SCALARS (_op_pending, _pending_action, _last_tool_active, _input_owned,
    _dialog_resume) are reassigned, so writers and readers in other modules
    must use ``state.<name>`` attribute access - a ``from``-import would
    freeze the import-time value.
"""

from typing import Optional


class CutPreviewCache:
    """Per-OBJECT live-cut session state (one instance per edit mesh).

    Big data never lives in properties. The snapshot is a real Mesh datablock
    (the untouched original) so the live bisect can always be undone visually.
    Multi-object edit mode runs one session per mesh - all of them found in
    the module-level _sessions registry, keyed by object session_uid.
    """

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.live_active: bool = False
        self.live_snapshot = None              # bpy Mesh datablock (the original)
        self.token: Optional[str] = None       # the snapshot's token (marker name)
        # Local-space bbox centre of that snapshot, for a BBOX anchor. Cached
        # because its readers are HOT - the preview dirty key every frame and
        # the header warning every redraw - while the snapshot it measures
        # only changes on session start, rebase, or a committed cut. Measured
        # uncached on a 40k-vert mesh: 483 ms/frame preview and 182 ms per
        # redraw. Dropped wherever live_snapshot is.
        self.snapshot_bbox_center = None
        # True when the snapshot has any hidden element: the preview then
        # cuts only visible geometry and clears face by face, like Execute.
        # Read off the snapshot's .hide_* attributes, which Blender writes
        # only when something is hidden (verified 4.5 / 5.2) - O(1) per frame.
        self.snapshot_has_hidden: bool = False
        self.live_bvh = None                   # BVHTree of the snapshot (for snapping)
        # The fast cut's flat arrays of THIS snapshot (clip.ClipArrays), False
        # when it cannot be served that way, None until asked; and the
        # (stamp, arrays) the split plan reads. Both dropped with the snapshot.
        self.clip_arrays = None
        self.split_arrays = None
        self.clip_scratch = None               # the fast cut's scratch Mesh
        # Mid-drag only: the snapshot sorted along the plane normal
        # (clip.ClipFrame, keyed by that normal) and the normal of the last
        # move - a frame is built once a normal repeats (a translate).
        self.clip_frames = {}
        self.clip_last_normal = None
        # True while the mesh shows a Fill cap the fast cut made mid-drag:
        # end_owned_input re-cuts it exactly (see preview._apply_live_cut).
        self.fill_tied = False
        # Object identity by session_uid, NOT name: renaming the object mid-preview
        # must not orphan the session (a name lookup silently baked the preview cut).
        self.live_object_uid: Optional[int] = None
        self.live_key: Optional[tuple] = None  # plane pose + options (dirty check)
        # (verts, edges, faces) as WE last wrote them. A mismatch means the user
        # edited the mesh during the preview - rebase instead of discarding it.
        self.expected_counts: Optional[tuple] = None
        # Content fingerprint of the mesh as WE last left it: the foreign-
        # edit check for edits that keep the counts. None = not recorded
        # (counts only); fp_stale = written while a plane transform owned
        # the input, recorded when it lets go (see snapshot.content_fingerprint).
        self.content_fp: Optional[bytes] = None
        self.fp_stale: bool = False
        # Per-type SELECTION counts as we last wrote them (and as stored in
        # the snapshot). A mismatch means the user changed the selection -
        # adopt or remap it into the baseline instead of letting a rebuild
        # wipe it (in Selected-Only mode it is also the cut domain).
        self.expected_sel_counts: Optional[tuple] = None
        self.snapshot_sel_counts: Optional[tuple] = None
        # True while the live mesh is EXACTLY the baseline (set on snapshot,
        # recomputed at every preview write). EXPLICIT KNOWLEDGE, never a
        # count comparison: a mid-prism Clear+Fill cut reproduces the
        # baseline's exact counts (verified on a cube: 8/12/6 -> 8/12/6),
        # which made a count-based rebuild skip COMPOUND cuts and bake the
        # preview permanently.
        self.mesh_is_baseline: bool = False
        # The state the CURRENT preview mesh was actually produced with:
        # (planes, clear_inner, clear_outer), planes = the object-LOCAL
        # (co, no) list of the last apply, mirrors included. None while the
        # mesh is the baseline. The selection remap classifies THIS mesh's
        # geometry, so it must be judged against THIS state - live_key is
        # the wrong source (it holds the INCOMING key, is world-space, and
        # is deliberately wiped by refresh_live_preview to force re-applies).
        # Feeding the remap the incoming state instead made its hidden()
        # guard misjudge the previously-cleared side as visible-and-
        # deselected, permanently eroding the Selected-Only domain
        # (measured: unticking Clear Outer deselected 256 of 384 faces).
        self.written_state: Optional[tuple] = None


# The live sessions: object session_uid -> CutPreviewCache. A session in the
# registry IS active (sessions are registered only after their first snapshot
# succeeds and dropped on reset). The watchdog's pending-action latch is
# GLOBAL (one decision covers the whole edit-mode session set) - see below.
_sessions = {}

# Watchdog latch (was on the singleton cache): a deferred apply/restore is
# scheduled / which action the watchdog decided from its valid-region context.
_op_pending = False
_pending_action: Optional[str] = None

# Last tool-active state seen by the watchdog (which runs in a valid-region
# context). The undo/redo handler may be region-less, so it reads this cached
# flag to gate its blink-mitigation re-apply instead of re-deriving tool-active.
_last_tool_active = False

# Undo steps pushed while a preview shows (a click-select, Tab out) store
# the preview mesh, and undoing back to one used to make the never-executed
# cut real. So every preview write tags the mesh with a face layer named
# _MARKER_PREFIX + its snapshot's token; a mesh that comes back through
# undo/redo carrying one is repaired from that snapshot, which the same
# undo step brings back into bpy.data (see snapshot.repair_marked_mesh).
_MARKER_PREFIX = ".cutplane_pv_"
# Containers, mutated in place: meshes (by session_uid) that ever carried a
# marker, and per token the content fingerprints of the preview states
# written with it - the repair only replaces a mesh that is exactly one of
# them, never one the user edited on top of the preview.
_marked_mesh_uids = set()
_written_fps = {}

# True while one of the plane transforms runs: a blocking modal consumes
# every event, so nothing but the tool can edit the mesh, and the per-frame
# writes skip the content fingerprint (begin/end_owned_input in preview).
_input_owned = False

# Set while the preview is paused for an operator's file browser (see
# dialogs): how to bring it back once the browser closes - ('apply', None),
# or ('readopt', {session_uid: live_key}) for sessions that sat at the armed
# baseline Execute Cut leaves. None = not paused.
_dialog_resume: Optional[tuple] = None


def _session_for(obj):
    """The live session for obj, or None. Rename-proof (session_uid key)."""
    if obj is None:
        return None
    return _sessions.get(obj.session_uid)


def _edit_mesh_objects(context):
    """Every mesh in the multi-object edit session (the preview and the cut
    both apply to all of them); falls back to the active edit object in
    contexts that lack the derived member (timers)."""
    objs = [ob for ob in getattr(context, 'objects_in_mode_unique_data', ())
            if ob.type == 'MESH']
    if objs:
        return objs
    obj = getattr(context, 'edit_object', None)
    return [obj] if obj is not None and obj.type == 'MESH' else []

# Name of the snapshot Mesh datablock (Blender may suffix .001 etc. for
# uniqueness, so matching is by startswith). Single source for creation + purge.
_SNAPSHOT_NAME = "__cut_plane_cut_snapshot__"

# ID property stamped on every snapshot: the NAME of the mesh datablock it was
# taken from. ID properties persist in .blend files, which is the whole point -
# an autosave/crash file carries the snapshot (see handlers), and the stamp is
# what lets the recovery find the mesh to restore into.
_RECOVERY_TARGET_PROP = "cut_plane_recovery_target"
# The rename-proof pairing: one fresh token per snapshot, stamped on BOTH the
# snapshot and its target mesh. Mesh NAMES are unique but neither stable nor
# owned - the name stamp alone made crash recovery (a) miss after an ordinary
# rename (the baked preview cut became permanent, the snapshot holding the
# original was purged, all silently) and (b) OVERWRITE AN UNRELATED MESH when
# the freed name was later taken by another datablock, while logging success.
# ID properties survive renames and file round-trips; tokens cannot collide.
_RECOVERY_TOKEN_PROP = "cut_plane_recovery_token"


def topology_changed() -> None:
    """Invalidate every session's plane key so the next preview re-applies.

    Fallback used by adopt_committed_baseline outside edit mode."""
    for cache in _sessions.values():
        cache.live_key = None
