# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless soak / fuzz test for the live-cut state machine.

Runs inside Blender (it needs the real bpy / bmesh / operators):

    blender --background --python tests/integration/soak.py
    blender --background --python tests/integration/soak.py -- 5000 12345
                                                               (iters)  (seed)

It registers the addon with DEBUG on (so swallowed errors and invariant
violations surface), builds a mesh in Edit Mode, then fires thousands of
randomized operations - cut, move/rotate the plane, toggle clear/fill/select
options, reset, switch mode/orientation, simulate undo/redo + save round-trips,
restore, mutate the selection. After EVERY operation it asserts:

  * no exception escaped the operation,
  * cutting.check_invariants() is empty (cache stays consistent), and
  * after simulating the deferred purge timer, <= 1 snapshot datablock survives
    (the leak guard).

Modal drags and real Ctrl+Z can't be driven headlessly, so the soak drives the
same functions the modals/watchdog call (update_live_preview, _on_undo_redo_post,
restore_live_cut, ...). The seed is printed so any failure is reproducible.
"""
import math
import os
import random
import sys
import traceback

import bmesh
import bpy
from mathutils import Matrix

ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

import cut_plane  # noqa: E402  (must follow the sys.path insert above)
from cut_plane import cutting  # noqa: E402
from cut_plane.lib import constants  # noqa: E402

# --- args (after the Blender "--" separator) ------------------------------
_args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
ITERS = int(_args[0]) if len(_args) > 0 else 2000
SEED = int(_args[1]) if len(_args) > 1 else random.randrange(1 << 30)
FACE_CAP = 6000  # rebuild the mesh if a clear/cut path lets it grow past this

rng = random.Random(SEED)
violations = []  # (iteration, op_name, kind, detail)


def _record(i, op, kind, detail):
    violations.append((i, op, kind, detail))


# --- scene setup ----------------------------------------------------------

def _new_mesh():
    """Fresh subdivided cube in Edit Mode (so cuts add visible geometry)."""
    cutting.restore_live_cut(bpy.context)
    cutting._reset_live_state()
    cutting._purge_orphan_snapshots()
    if bpy.context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for ob in list(bpy.data.objects):
        if ob.type == 'MESH':
            bpy.data.objects.remove(ob, do_unlink=True)
    bpy.ops.mesh.primitive_cube_add(size=2)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.subdivide(number_cuts=3)
    bpy.ops.mesh.select_all(action='DESELECT')


def _face_count():
    obj = bpy.context.edit_object
    if obj is None or bpy.context.mode != 'EDIT_MESH':
        return 0
    return len(bmesh.from_edit_mesh(obj.data).faces)


# --- operations (each: ctx, sc, rng) --------------------------------------

def op_move_plane(ctx, sc, rng):
    sc.center_offset = (rng.uniform(-1.6, 1.6), rng.uniform(-1.6, 1.6), rng.uniform(-1.6, 1.6))
    cutting.update_live_preview(ctx)


def op_rotate_plane(ctx, sc, rng):
    m = Matrix.Rotation(rng.uniform(0.0, math.pi), 4, rng.choice(['X', 'Y', 'Z']))
    sc.rotation_matrix_flat = tuple(m[r][c] for r in range(4) for c in range(4))
    cutting.update_live_preview(ctx)


def op_toggle_clear(ctx, sc, rng):
    sc.cut_clear_inner = rng.random() < 0.4   # update callback -> update_live_preview
    sc.cut_clear_outer = rng.random() < 0.4


def op_toggle_options(ctx, sc, rng):
    sc.cut_use_fill = rng.random() < 0.5
    sc.cut_select_loop = rng.random() < 0.85
    # Selected Only exercises the domain-restricted bisect paths, including
    # empty/degenerate domains
    sc.cut_selected_only = rng.random() < 0.3


def op_cut(ctx, sc, rng):
    bpy.ops.view3d.cut_plane_cut()


def op_reset(ctx, sc, rng):
    bpy.ops.view3d.cut_plane_reset()


def op_mode(ctx, sc, rng):
    sc.mode = rng.choice(['MOVE', 'SCALE', 'ROTATE'])


def op_orientation(ctx, sc, rng):
    sc.orientation = rng.choice(['GLOBAL', 'LOCAL'])


def op_sim_undo(ctx, sc, rng):
    cutting._on_undo_redo_post()      # pointer-only reset + (maybe) schedule re-apply
    cutting.update_live_preview(ctx)  # what the watchdog re-apply does next frame


def op_save_cycle(ctx, sc, rng):
    cutting._on_save_pre()
    cutting._on_save_post()


def op_restore(ctx, sc, rng):
    cutting.restore_live_cut(ctx)     # tool/edit-mode exit


def op_mutate_selection(ctx, sc, rng):
    # Random selection churn: exercises the selection adopt/remap machinery
    # (both modes preserve user (de)selections in the baseline now)
    obj = ctx.edit_object
    bm = bmesh.from_edit_mesh(obj.data)
    for v in bm.verts:
        if rng.random() < 0.05:
            v.select = not v.select
    bmesh.update_edit_mesh(obj.data, destructive=False)


OPS = [
    op_move_plane, op_rotate_plane, op_toggle_clear, op_toggle_options,
    op_cut, op_cut, op_cut,          # weight cuts higher
    op_reset, op_mode, op_orientation,
    op_sim_undo, op_sim_undo, op_save_cycle, op_restore, op_mutate_selection,
]


# --- run ------------------------------------------------------------------

def main():
    constants.DEBUG = True            # surface swallowed errors + invariant asserts
    try:
        cut_plane.register()
    except ValueError:
        # Already registered: the addon is ENABLED in the user's prefs and
        # auto-loaded at startup (the dev junction installs the working tree
        # as a real addon). Re-register for a clean, DEBUG-armed state.
        cut_plane.unregister()
        cut_plane.register()
    _new_mesh()

    print(f"\n[SOAK] seed={SEED} iters={ITERS}")
    ctx = bpy.context
    for i in range(ITERS):
        sc = ctx.window_manager.cut_plane
        op = rng.choice(OPS)
        try:
            op(ctx, sc, rng)
        except Exception:
            _record(i, op.__name__, "EXCEPTION", traceback.format_exc().strip().splitlines()[-1])
            _new_mesh()
            continue

        probs = cutting.check_invariants()
        if probs:
            _record(i, op.__name__, "INVARIANT", probs)

        cutting._purge_orphan_snapshots()        # simulate the deferred purge timer
        nsnap = cutting.count_snapshot_datablocks()
        if nsnap > 1:
            _record(i, op.__name__, "LEAK", f"{nsnap} snapshot datablocks")

        if _face_count() > FACE_CAP or _face_count() == 0:
            _new_mesh()

    # final teardown sanity
    cutting.restore_live_cut(ctx)
    cutting._reset_live_state()
    cutting._purge_orphan_snapshots()
    leftover = cutting.count_snapshot_datablocks()
    if leftover != 0:
        _record(ITERS, "teardown", "LEAK", f"{leftover} snapshot datablocks after cleanup")

    try:
        cut_plane.unregister()
    except Exception:
        pass

    # --- report ---
    print(f"[SOAK] completed {ITERS} ops, {len(violations)} violation(s)")
    for i, op, kind, detail in violations[:40]:
        print(f"   [{i:5d}] {kind:9s} {op}: {detail}")
    if len(violations) > 40:
        print(f"   ... +{len(violations) - 40} more")
    if violations:
        print(f"[SOAK] FAIL (reproduce with: -- {ITERS} {SEED})")
        return 1
    print("[SOAK] PASS - no exceptions, invariant violations, or snapshot leaks")
    return 0


if __name__ == "__main__":
    code = main()
    # os._exit avoids Blender's flaky headless GPU-teardown crash masking our code
    sys.stdout.flush()
    os._exit(code)
