# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless test: saving never mutates the committed mesh.

    blender --background --factory-startup --python-exit-code 1 \
            --python tests/integration/save_roundtrip.py

Contract: Ctrl+S is an observation, not an edit. save_pre restores the
baseline (so the destructive preview never reaches disk) and save_post puts
the session back - in EXACTLY the state it destroyed.

The bug this locks: after Execute Cut, adopt_committed_baseline arms
live_key so the preview is a deliberate NO-OP at the committed plane. The
save round-trip destroyed that session and rebuilt it via
update_live_preview - a fresh session with live_key None, whose suppression
guard can never match - so the preview RE-BISECTED the committed mesh at the
plane the cut already occupies. With Fill on that laid a SECOND coincident
cap; a later foreign edit rebased the doubled mesh into the baseline, and
the next save wrote it to disk. One extra cap per save, compounding
(65 -> 66 -> 67... faces measured on 5.1 AND 4.5). save_post now re-ADOPTS
(fresh snapshot + armed key) when every pre-save session sat at an armed
baseline.

A/B structure: two identical user flows, the ONLY difference one save.

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

cut_plane.register()

failures = []


def check(name, cond, detail=""):
    print(f"[{'ok  ' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


# Own temp dir, NOT bpy.app.tempdir: Blender deletes its session temp
# directory on file load (observed on Linux; Windows leaves it behind, so
# the difference only shows cross-platform), and this suite reloads files
# mid-flow - a session-dir path would vanish before the next save.
import tempfile  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="cut_plane_save_roundtrip_")
PATH = os.path.join(_TMP, "save_roundtrip.blend")
OUT = os.path.join(_TMP, "save_roundtrip_out.blend")


def run_flow(saves_after_commit):
    """Cube -> subdivide -> Execute Cut (Fill+Clear) -> N saves -> foreign
    edit -> leave edit mode -> save -> reopen. Returns
    (committed_faces, saved_file_faces, coincident_duplicates)."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_cube_add(size=2)
    obj = bpy.context.active_object
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.subdivide(number_cuts=3)
    bpy.ops.mesh.select_all(action='DESELECT')
    sc = bpy.context.window_manager.cut_plane
    sc.z_offset = 0.17
    sc.cut_use_fill = True
    sc.cut_clear_outer = True

    plane = cutting.get_plane_world(bpy.context)
    bpy.ops.view3d.cut_plane_cut(plane_co=plane[0], plane_no=plane[1],
                                 use_fill=True, clear_outer=True)
    committed = len(bmesh.from_edit_mesh(obj.data).faces)

    for _ in range(saves_after_commit):
        bpy.ops.wm.save_as_mainfile(filepath=PATH)  # the only difference

    # The user keeps modelling: a foreign edit rebases the live baseline,
    # so anything the save smuggled in becomes permanent.
    bm = bmesh.from_edit_mesh(obj.data)
    bmesh.ops.create_vert(bm, co=(9.0, 9.0, 9.0))
    bmesh.update_edit_mesh(obj.data, destructive=True)
    cutting.update_live_preview(bpy.context)

    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.wm.save_as_mainfile(filepath=OUT)
    bpy.ops.wm.open_mainfile(filepath=OUT)
    me = bpy.data.meshes["Cube"]
    seen, dup = set(), 0
    for p in me.polygons:
        key = tuple(round(c, 4) for c in p.center)
        if key in seen:
            dup += 1
        seen.add(key)
    return committed, len(me.polygons), dup


a_committed, a_faces, a_dup = run_flow(0)
b_committed, b_faces, b_dup = run_flow(1)
c_committed, c_faces, c_dup = run_flow(3)

check("fixture: both flows commit the identical cut",
      a_committed == b_committed == c_committed,
      f"{a_committed}/{b_committed}/{c_committed}")
check("the unsaved flow reaches disk with no coincident faces",
      a_dup == 0, str(a_dup))
check("REGRESSION: one save adds NO faces to the committed mesh",
      b_faces == a_faces, f"{a_faces} -> {b_faces}")
check("...and NO coincident duplicate caps", b_dup == a_dup,
      f"{a_dup} -> {b_dup}")
check("REGRESSION: three saves compound nothing either",
      c_faces == a_faces and c_dup == a_dup,
      f"faces {a_faces} -> {c_faces}, dup {a_dup} -> {c_dup}")

# A save MID-PREVIEW must still bring the preview back (the re-adopt path
# must not swallow the ordinary case).
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=2)
obj = bpy.context.active_object
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.mesh.select_all(action='SELECT')
sc = bpy.context.window_manager.cut_plane
sc.cut_clear_inner = True
sc.z_offset = 0.0
cutting.update_live_preview(bpy.context)
faces_preview = len(bmesh.from_edit_mesh(obj.data).faces)
check("fixture: a live preview is applied", faces_preview != 6,
      str(faces_preview))
bpy.ops.wm.save_as_mainfile(filepath=PATH)
check("a mid-preview save brings the preview BACK",
      len(bmesh.from_edit_mesh(obj.data).faces) == faces_preview,
      f"{len(bmesh.from_edit_mesh(obj.data).faces)} vs {faces_preview}")
check("...with a live session", len(cutting._sessions) == 1)

# ...and the saved file itself holds the clean baseline, no snapshots.
bpy.ops.wm.open_mainfile(filepath=PATH)
me = bpy.data.meshes["Cube"]
check("the saved file holds the BASELINE, not the preview cut",
      len(me.polygons) == 6, str(len(me.polygons)))
check("...and zero snapshot datablocks",
      not any(m.name.startswith(cutting._SNAPSHOT_NAME)
              for m in bpy.data.meshes))

for p in (PATH, OUT):
    if os.path.exists(p):
        os.remove(p)

print(f"\nSAVE ROUNDTRIP: {'FAIL - ' + ', '.join(failures) if failures else 'OK'}")
sys.stdout.flush()
os._exit(1 if failures else 0)
