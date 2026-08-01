# SPDX-License-Identifier: GPL-3.0-or-later
"""Self-instrumentation: consistency checks on the live-cut cache. These turn
state-machine regressions into concrete violations instead of mysterious
visual glitches. check_invariants() is pure (returns a list); _debug_check()
raises under DEBUG so interactive dev crashes loudly; the headless soak test
(tests/integration/soak.py) calls check_invariants() after every operation.
Call only at STABLE points - never mid-_live_take_snapshot, where two snapshot
datablocks briefly coexist (the orphan count lives in the soak, not here, since
it's sensitive to the deferred purge timer).
"""

import bpy

from ..lib.constants import DEBUG
from . import state
from .snapshot import _snapshot_valid
from .state import _SNAPSHOT_NAME, _sessions


def check_invariants() -> list:
    """Return a list of live-cut cache inconsistencies (empty == healthy)."""
    problems = []
    if state._pending_action not in (None, 'apply', 'restore'):
        problems.append(f"unexpected _pending_action {state._pending_action!r}")
    for uid, c in _sessions.items():
        tag = f"session {uid}: "
        if not c.live_active:
            problems.append(tag + "registered session not live_active")
        if c.live_snapshot is None:
            problems.append(tag + "session without a snapshot")
        elif not _snapshot_valid(c):
            problems.append(tag + "snapshot datablock is invalid")
        if c.live_object_uid != uid:
            problems.append(tag + "registry key != live_object_uid")
        # Baseline/selection bookkeeping coherence (the count-collision and
        # selection-wipe bug class): these fields only mean something while a
        # snapshot exists, and they travel together.
        if c.mesh_is_baseline and c.live_snapshot is None:
            problems.append(tag + "mesh_is_baseline without a snapshot")
        if c.snapshot_sel_counts is not None and c.live_snapshot is None:
            problems.append(tag + "snapshot_sel_counts without a snapshot")
        if c.expected_sel_counts is not None and c.expected_counts is None:
            problems.append(tag + "expected_sel_counts without expected_counts")
    return problems


def count_snapshot_datablocks() -> int:
    """How many snapshot Mesh datablocks exist (for leak detection in tests)."""
    return sum(1 for m in bpy.data.meshes if m.name.startswith(_SNAPSHOT_NAME))


def _debug_check(where: str) -> None:
    """Under DEBUG, raise on any cache-invariant violation (else no-op)."""
    if not DEBUG:
        return
    problems = check_invariants()
    if problems:
        raise AssertionError(f"[CUT PLANE] invariant(s) at {where}: {problems}")
