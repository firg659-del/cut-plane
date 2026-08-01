# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless test: crash/autosave recovery restores the uncut mesh.

    blender --background --factory-startup --python tests/integration/crash_recovery.py

Autosave writes the .blend WITHOUT firing save_pre (verified interactively
2026-07-10), so a crash file contains the preview cut baked into the mesh
AND the snapshot datablock (fake_user). The self-heal: snapshots carry the
target mesh name as an ID property, and _recover_orphan_snapshots (scheduled
by load_post only) restores the uncut original from them before purging.

The autosave is simulated exactly: detach the addon's save handlers, save,
reattach, reload. The recovery timer body is called directly (timers don't
tick in background mode). Also locks the negative: the plain purge (the
in-session undo/redo path) must never geometry-restore.

Exit code 0 on success, 1 with the failed checks listed.
"""
import os
import sys

import bmesh
import bpy

ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

import cut_plane  # noqa: E402
from cut_plane import cutting  # noqa: E402
from cut_plane.lib import constants  # noqa: E402

constants.DEBUG = True
cut_plane.register()

failures = []


def check(name, cond, detail=""):
    print(f"[{'ok  ' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


def mesh_faces(me):
    if me.is_editmode:
        return len(bmesh.from_edit_mesh(me).faces)
    return len(me.polygons)


PATH = os.path.join(bpy.app.tempdir, "cut_plane_crash_recovery_test.blend")

# --- a mesh with an ACTIVE preview cut (clear = visibly missing geometry) ---
bpy.ops.mesh.primitive_cube_add(size=2)
obj = bpy.context.active_object
mesh_name = obj.data.name
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.mesh.subdivide(number_cuts=1)
bpy.ops.mesh.select_all(action='DESELECT')

baseline = mesh_faces(obj.data)
sc = bpy.context.window_manager.cut_plane
sc.cut_clear_outer = True  # update callback applies the preview immediately
cut_faces = mesh_faces(obj.data)
session = cutting._session_for(obj)
check("preview cut applied (clear removed a side)",
      session is not None and 0 < cut_faces != baseline,
      f"{baseline} -> {cut_faces}")
check("snapshot stamped with its target mesh",
      session.live_snapshot.get(cutting._RECOVERY_TARGET_PROP) == mesh_name)

# --- simulate the autosave: a save that skips the addon's handlers ----------
bpy.app.handlers.save_pre.remove(cutting._on_save_pre)
bpy.app.handlers.save_post.remove(cutting._on_save_post)
try:
    bpy.ops.wm.save_as_mainfile(filepath=PATH)
finally:
    bpy.app.handlers.save_pre.append(cutting._on_save_pre)
    bpy.app.handlers.save_post.append(cutting._on_save_post)

# --- crash recovery: reload the file ----------------------------------------
bpy.ops.wm.open_mainfile(filepath=PATH)  # fires _on_load_post (cache reset)
me = bpy.data.meshes.get(mesh_name)
check("crash file has the preview cut BAKED", me is not None
      and mesh_faces(me) == cut_faces, f"{mesh_faces(me) if me else '?'}")
check("crash file carries the snapshot", cutting.count_snapshot_datablocks() == 1)

cutting._recover_orphan_snapshots()  # the load_post timer body (no ticks headless)
check("recovery restored the UNCUT mesh", mesh_faces(me) == baseline,
      f"{mesh_faces(me)} vs baseline {baseline}")
check("snapshot reclaimed after recovery", cutting.count_snapshot_datablocks() == 0)
check("cache invariants clean", not cutting.check_invariants(),
      str(cutting.check_invariants()))

# --- negative: the in-session purge must NEVER geometry-restore -------------
fake = bpy.data.meshes.new(cutting._SNAPSHOT_NAME)  # empty mesh, stamped
fake[cutting._RECOVERY_TARGET_PROP] = mesh_name
fake.use_fake_user = True
before = mesh_faces(me)
cutting._purge_orphan_snapshots()
check("plain purge reclaims the snapshot", cutting.count_snapshot_datablocks() == 0)
check("plain purge never touches the target mesh", mesh_faces(me) == before)

# --- control: a NORMAL save (handlers on) produces a clean, snapshot-free file
sc = bpy.context.window_manager.cut_plane
sc.cut_clear_outer = True
if bpy.context.mode != 'EDIT_MESH':  # re-enter edit mode on the loaded object
    ob = next(o for o in bpy.data.objects if o.data is me)
    bpy.context.view_layer.objects.active = ob
    bpy.ops.object.mode_set(mode='EDIT')
cutting.update_live_preview(bpy.context)
check("preview re-applied for the control save", bool(cutting._sessions))
bpy.ops.wm.save_as_mainfile(filepath=PATH)  # save_pre restores, save_post re-applies
bpy.ops.wm.open_mainfile(filepath=PATH)
me = bpy.data.meshes.get(mesh_name)
check("normally saved file is uncut", mesh_faces(me) == baseline,
      f"{mesh_faces(me)} vs {baseline}")
check("normally saved file carries no snapshot",
      cutting.count_snapshot_datablocks() == 0)

# --- recovery identity is the TOKEN, not the mesh NAME ----------------------
# Mesh names are unique but neither stable nor owned. The old name-only stamp
# made recovery (a) silently MISS after one ordinary rename - the baked
# preview cut became permanent and the purge destroyed the only copy of the
# original - and (b) OVERWRITE AN UNRELATED MESH that later took the freed
# name, under a "Recovered" success log. A per-snapshot token stamped on
# BOTH datablocks survives any rename and cannot be reused.


def autosave(path):
    """A save that skips the addon's handlers - exactly what autosave does."""
    bpy.app.handlers.save_pre.remove(cutting._on_save_pre)
    bpy.app.handlers.save_post.remove(cutting._on_save_post)
    try:
        bpy.ops.wm.save_as_mainfile(filepath=path)
    finally:
        bpy.app.handlers.save_pre.append(cutting._on_save_pre)
        bpy.app.handlers.save_post.append(cutting._on_save_post)


def clean_scene():
    if bpy.context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    cutting.restore_live_cut(bpy.context)
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)


# SCENARIO 1: the working mesh is RENAMED during the preview.
clean_scene()
bpy.ops.mesh.primitive_cube_add(size=2)
a = bpy.context.active_object
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.mesh.subdivide(number_cuts=1)
bpy.ops.mesh.select_all(action='DESELECT')
base_a = mesh_faces(a.data)
sc = bpy.context.window_manager.cut_plane
sc.cut_clear_outer = True
cutting.update_live_preview(bpy.context)
cut_a = mesh_faces(a.data)
check("fixture: preview cut applied", 0 < cut_a != base_a,
      f"{base_a} -> {cut_a}")
tok = a.data.get(cutting._RECOVERY_TOKEN_PROP)
check("snapshot and target share ONE recovery token",
      tok is not None and cutting._session_for(a).live_snapshot.get(
          cutting._RECOVERY_TOKEN_PROP) == tok)

a.data.name = "Body_final"          # ordinary mid-modelling rename
autosave(PATH)
bpy.ops.wm.open_mainfile(filepath=PATH)
me_a = bpy.data.meshes.get("Body_final")
check("crash file: the cut is baked under the NEW name",
      me_a is not None and mesh_faces(me_a) == cut_a)
cutting._recover_orphan_snapshots()
check("RENAME: recovery still finds the renamed mesh (token pairing)",
      mesh_faces(me_a) == base_a, f"{mesh_faces(me_a)} vs baseline {base_a}")
check("...snapshot reclaimed", cutting.count_snapshot_datablocks() == 0)
check("...token consumed off the recovered mesh",
      me_a.get(cutting._RECOVERY_TOKEN_PROP) is None)

# SCENARIO 2: the freed name is TAKEN by an unrelated mesh.
clean_scene()
bpy.ops.mesh.primitive_cube_add(size=2)
b = bpy.context.active_object
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.mesh.subdivide(number_cuts=2)
bpy.ops.object.mode_set(mode='OBJECT')
b.data.name = "VictimMesh"
b_faces = mesh_faces(b.data)

bpy.ops.mesh.primitive_cube_add(size=2)
a = bpy.context.active_object
a.data.name = "WorkMesh"
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.mesh.subdivide(number_cuts=1)
bpy.ops.mesh.select_all(action='DESELECT')
base_a = mesh_faces(a.data)
cutting.update_live_preview(bpy.context)
cut_a = mesh_faces(a.data)
check("fixture: preview cut applied to the working mesh",
      0 < cut_a != base_a, f"{base_a} -> {cut_a}")

a.data.name = "WorkMesh_old"        # frees the stamped name...
b.data.name = "WorkMesh"            # ...which the victim now holds
autosave(PATH)
bpy.ops.wm.open_mainfile(filepath=PATH)
me_a = bpy.data.meshes.get("WorkMesh_old")
me_b = bpy.data.meshes.get("WorkMesh")
cutting._recover_orphan_snapshots()
check("NAME REUSE: recovery restores the REAL target",
      me_a is not None and mesh_faces(me_a) == base_a,
      f"{mesh_faces(me_a) if me_a else '?'} vs baseline {base_a}")
check("NAME REUSE: the unrelated mesh holding the stamped name is UNTOUCHED",
      me_b is not None and mesh_faces(me_b) == b_faces,
      f"{mesh_faces(me_b) if me_b else '?'} vs {b_faces}")

# --- token-less legacy snapshots NEVER restore (name-write path removed) ----
# Only pre-release dev builds wrote snapshots without the token pair; no
# shipped build did. The legacy fallback restored by NAME - one rename +
# one reuse away from overwriting an unrelated mesh - so the branch is
# gone: a token-less snapshot restores nothing and is purged. The fixture
# snapshot is EMPTY, so if the old path ever returns, the restore would
# visibly wipe the target and fail the face-count check.
clean_scene()
bpy.ops.mesh.primitive_cube_add(size=2)
legacy_target = bpy.context.active_object.data
legacy_target.name = "LegacyMesh"
legacy_faces = mesh_faces(legacy_target)
legacy_snap = bpy.data.meshes.new(cutting._SNAPSHOT_NAME)
legacy_snap[cutting._RECOVERY_TARGET_PROP] = "LegacyMesh"  # name stamp, NO token
legacy_snap.use_fake_user = True
cutting._recover_orphan_snapshots()
check("LEGACY: a token-less snapshot never restores by name",
      mesh_faces(legacy_target) == legacy_faces,
      f"{mesh_faces(legacy_target)} vs {legacy_faces}")
check("LEGACY: ...and it is purged as an owned leftover",
      cutting.count_snapshot_datablocks() == 0)

# The purge owns only what it stamped: a USER mesh renamed to the snapshot
# prefix must survive (prefix-only matching deleted it AND its objects).
clean_scene()
precious = bpy.data.meshes.new(cutting._SNAPSHOT_NAME + "_precious")
holder = bpy.data.objects.new("Precious", precious)
bpy.context.collection.objects.link(holder)
cutting._purge_orphan_snapshots()
check("PURGE GUARD: a user mesh named with the snapshot prefix survives",
      bpy.data.meshes.get(cutting._SNAPSHOT_NAME + "_precious") is not None
      and bpy.data.objects.get("Precious") is not None)

for p in (PATH, PATH + "1"):
    if os.path.exists(p):
        os.remove(p)

print("\nCRASH RECOVERY:", "OK" if not failures else f"FAIL ({len(failures)}): {failures}")
sys.exit(1 if failures else 0)
