# SPDX-License-Identifier: GPL-3.0-or-later
"""Tool-scoped undo for the plane pose (Ctrl+Z while OUR tool is active).

The pose is deliberately OUTSIDE Blender's undo (WindowManager tool state -
that is what killed the F9 teleport). This module
gives it an undo history anyway, without ever letting native undo manage
it: a plain Python stack of pose snapshots, popped by our own Ctrl+Z
operator, which is bound in the TOOL keymap - outside the tool, native
Ctrl+Z is untouched, so a cut always undoes in ONE press there.

ORDERING against mesh steps (the hard part): the scene carries a MESH-STEP
COUNTER - advanced by our cut (mark_mesh_step) right before its undo push,
so every undo step snapshots how many tracked mesh steps preceded it, and
native undo/redo move the counter for us. Each pose entry records the
counter at push time. The interleave rule falls out:

  * entry.mesh_count >= live counter -> no tracked mesh step separates the
    entry from the current mesh state; the pose change is the next event
    in reverse chronology - pop it. (STRICTLY greater happens after native
    undo ran outside our operator: the mesh rewound past the entry while
    the pose didn't - the entry is then the newest un-reverted event.)
  * entry.mesh_count <  live counter -> a cut is newer; fall through to
    native ed.undo, which also rolls the counter back.

The v1 of this compared exact token values and trimmed on mismatch - in
multi-cut sessions undoing a cut restores the PREVIOUS cut's snapshot,
skipping over the pose tokens between the two cuts, so every plane move
was mis-trimmed as "erased future" and Ctrl+Z only ever walked cuts
(user-caught regression). Counting mesh steps instead of events makes the
comparison survive that skip.

Counter writes are plain ID-property assignments: they never push undo
steps themselves, they just ride inside whatever step comes next.

Accepted limits (documented, not bugs): foreign edit ops (an extrude done
while our tool is active) don't advance the counter, so around those the
order is approximate - everything still undoes; pose entries from an
overwritten mesh future are dropped when a NEW cut lands (undo branching);
the counter is per-scene while the pose is global, so switching scenes
mid-history can misalign (falls through to native undo - never corrupts);
users who rebound Ctrl+Z simply keep native behavior.
"""

from ..lib.constants import DEBUG, logger

# Scene ID property carrying the mesh-step counter (see module docstring)
TOKEN_PROP = "cut_plane_undo_token"

# Pose fields, in snapshot order. Orientation/mode are excluded on purpose:
# flipping them CONVERTS the offsets to keep the plane still - they are a
# view of the pose, not part of it.
_POSE_FIELDS = ("x_offset", "y_offset", "z_offset", "center_offset",
                "rotation_matrix_flat", "x_scale", "y_scale", "z_scale")

_MAX_ENTRIES = 64

# Entries: (pose_before, pose_after, mesh_count)
_undo_stack = []
_redo_stack = []


def _token(scene) -> int:
    return scene.get(TOKEN_PROP, 0)


def capture_pose(context):
    """The current pose as a plain tuple, or None pre-registration."""
    props = getattr(context.window_manager, 'cut_plane', None)
    if props is None:
        return None
    return tuple(
        tuple(v) if f in ("center_offset", "rotation_matrix_flat") else v
        for f, v in ((f, getattr(props, f)) for f in _POSE_FIELDS))


def _apply_pose(context, pose) -> None:
    """Write a snapshot back. Pose fields have no update callbacks; the
    caller refreshes the preview (operator context = write-safe)."""
    props = getattr(context.window_manager, 'cut_plane', None)
    if props is None:
        return
    for field, value in zip(_POSE_FIELDS, pose):
        setattr(props, field, value)


def push_if_changed(context, pose_before) -> None:
    """Record a completed pose change (transform confirm / Reset).

    No-op when nothing changed (e.g. a click without a drag). The entry
    remembers the current mesh-step count - that is its place in the
    interleaved chronology. Any new change invalidates the redo branch.
    """
    if pose_before is None:
        return
    pose_after = capture_pose(context)
    if pose_after is None or pose_after == pose_before:
        return
    mesh_count = _token(context.scene)
    _undo_stack.append((pose_before, pose_after, mesh_count))
    del _undo_stack[:-_MAX_ENTRIES]
    _redo_stack.clear()
    if DEBUG:
        logger.debug(f"[POSE UNDO] push @ mesh step {mesh_count}, "
                     f"{len(_undo_stack)} entries")


def mark_mesh_step(context) -> None:
    """Advance the mesh-step counter - called by OUR cut operator on
    success, right before Blender pushes its undo step, so that step
    snapshots a count higher than every pending pose entry (a cut after a
    move must undo FIRST). Also the undo-branch point: pose entries from a
    mesh future this new step overwrites are dropped, and redo dies like
    native redo does."""
    scene = context.scene
    count = _token(scene)
    _undo_stack[:] = [e for e in _undo_stack if e[2] <= count]
    _redo_stack.clear()
    scene[TOKEN_PROP] = count + 1


def undo(context) -> bool:
    """Pop the newest pose change unless a tracked mesh step is newer.

    True = a pose was restored (the caller is done); False = a cut is the
    newest history - the caller falls through to native undo (which rolls
    the counter back itself via the step snapshots).
    """
    cur = _token(context.scene)
    if not _undo_stack or _undo_stack[-1][2] < cur:
        return False
    entry = _undo_stack.pop()
    _redo_stack.append(entry)
    _apply_pose(context, entry[0])
    return True


def redo(context) -> bool:
    """Re-apply the last pose undo, unless a mesh redo must come first
    (the entry's moment lies after the current mesh state)."""
    cur = _token(context.scene)
    if not _redo_stack or _redo_stack[-1][2] > cur:
        return False
    entry = _redo_stack.pop()
    _undo_stack.append(entry)
    _apply_pose(context, entry[1])
    return True


def clear() -> None:
    """Drop all history (file load / addon reload - snapshots would point
    at another file's timeline)."""
    _undo_stack.clear()
    _redo_stack.clear()
