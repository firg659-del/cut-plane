# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless test: edge cases warn instead of silently doing nothing.

    blender --background --factory-startup --python tests/integration/edge_warnings.py

Covers the user-facing edge-case feedback:
  * Execute Cut with a plane that intersects nothing -> WARNING + CANCELLED
    (a FINISHED no-op would push an empty undo step), while a clear that
    actually removes geometry still goes through;
  * the tool-header warning (_edge_case_warning): empty Selected-Only
    domain, and the live-preview blockers;
  * cutting.live_preview_blocker + the once-per-object warning latch:
    shape keys and the density cap refuse the session, and the latch
    re-arms when the preview runs again.

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
from cut_plane.state import preferences  # noqa: E402
from cut_plane.ui.tool import CutPlaneTool  # noqa: E402

constants.DEBUG = True
cut_plane.register()

failures = []


def check(name, cond, detail=""):
    print(f"[{'ok  ' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


def face_count():
    return len(bmesh.from_edit_mesh(bpy.context.edit_object.data).faces)


bpy.ops.mesh.primitive_cube_add(size=2)
obj = bpy.context.active_object
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.mesh.select_all(action='DESELECT')
sc = bpy.context.window_manager.cut_plane

MISS = dict(plane_co=(0.0, 0.0, 50.0), plane_no=(0.0, 0.0, 1.0))  # above the cube

# --- Execute Cut that intersects nothing ------------------------------------
faces = face_count()
result = bpy.ops.view3d.cut_plane_cut(**MISS)
check("missed cut is CANCELLED", result == {'CANCELLED'})
check("missed cut leaves the mesh alone", face_count() == faces)

# clear_outer with the plane above: the outer side is empty too -> still a no-op
result = bpy.ops.view3d.cut_plane_cut(**MISS, clear_outer=True)
check("missed cut + empty clear side is CANCELLED", result == {'CANCELLED'})
check("mesh still untouched", face_count() == faces)

# clear_inner with the plane above removes the whole mesh: a real (if drastic)
# operation - the no-op guard must NOT block it
result = bpy.ops.view3d.cut_plane_cut(**MISS, clear_inner=True)
check("clear that removes geometry still FINISHES", result == {'FINISHED'})
check("clear actually removed the mesh", face_count() == 0)
bpy.ops.ed.undo_push(message="test")  # keep the harness state sane
bpy.ops.mesh.primitive_cube_add(size=2)  # rebuild in edit mode
bpy.ops.mesh.select_all(action='DESELECT')

# missed cut in Selected-Only mode (message branch: "the selection")
bpy.ops.mesh.select_all(action='SELECT')
result = bpy.ops.view3d.cut_plane_cut(**MISS, selected_only=True)
check("missed Selected-Only cut is CANCELLED", result == {'CANCELLED'})
bpy.ops.mesh.select_all(action='DESELECT')

# --- the tool-header warning -------------------------------------------------
sc.cut_selected_only = True
check("header warns on empty Selected-Only domain",
      CutPlaneTool._edge_case_warning(bpy.context, sc) == "Nothing selected to cut")
bpy.ops.mesh.select_all(action='SELECT')
check("header warning clears once something is selected",
      CutPlaneTool._edge_case_warning(bpy.context, sc) is None)
sc.cut_selected_only = False
bpy.ops.mesh.select_all(action='DESELECT')

# --- live-preview blockers ----------------------------------------------------
obj = bpy.context.edit_object

# density cap (monkeypatched preferences knob)
cutting.restore_live_cut(bpy.context)
orig_cap = preferences.preview_face_cap
preferences.preview_face_cap = lambda: 3
try:
    check("blocker reports the density cap",
          cutting.live_preview_blocker(obj) == "mesh too dense")
    check("header shows the density blocker",
          CutPlaneTool._edge_case_warning(bpy.context, sc)
          == "No live preview: mesh too dense")
    cutting.update_live_preview(bpy.context)
    check("session refused above the cap", cutting._session_for(obj) is None)
    check("refusal warned once (latch set)",
          (obj.session_uid, "mesh too dense") in cutting._preview_block_warned)
finally:
    preferences.preview_face_cap = orig_cap

cutting.update_live_preview(bpy.context)
check("session starts again below the cap", cutting._session_for(obj) is not None)
check("warning latch re-armed",
      all(k[0] != obj.session_uid for k in cutting._preview_block_warned))

# --- the snap target can never be emptied (native RNA parity) --------------
# Native ts.snap_elements refuses an empty assignment; our ENUM_FLAG cannot,
# so unticking every target in the popover left the header reading "Mix"
# while the engine silently snapped to INCREMENT. The update callback now
# restores the LAST non-empty set - matching native's refusal exactly
# ("you can't turn the last one off" must not jump to Increment).
sc.snap_elements = {'VERTEX'}
check("a real snap target sticks", sc.snap_elements == {'VERTEX'})
sc.snap_elements = set()          # untick the last row
check("SNAP FIX: emptying the set RESTORES what was there (native refusal)",
      sc.snap_elements == {'VERTEX'}, str(sc.snap_elements))
sc.snap_elements = {'EDGE', 'FACE'}
sc.snap_elements = set()
check("...including a multi-selection", sc.snap_elements == {'EDGE', 'FACE'},
      str(sc.snap_elements))
sc.snap_elements = {'INCREMENT'}

# The popover draws the ONE expanded enum-flag widget native uses - the
# source of both the native look and the click-vs-Shift-click semantics.
import inspect as _inspect  # noqa: E402
from cut_plane.ui.tool import CUT_PLANE_PT_snapping  # noqa: E402

_src = _inspect.getsource(CUT_PLANE_PT_snapping.draw)
check("snap popover uses the native expanded enum widget",
      '"snap_elements", expand=True' in _src and "prop_enum" not in _src)

# --- the cap measures the BASELINE, not the previous cut's output ----------
# A bisect ADDS faces (splits + fill caps), so measuring the live mesh made
# a baseline just under the cap block itself on alternate refreshes:
# cut -> live over cap -> restore -> under cap -> re-cut -> ... The budget
# question is about the snapshot the next rebuild starts from.
cutting.restore_live_cut(bpy.context)
bpy.ops.mesh.select_all(action='SELECT')
for _ in range(3):
    bpy.ops.mesh.subdivide()          # 96-face baseline
BASELINE_FACES = len(bmesh.from_edit_mesh(obj.data).faces)
sc.x_offset = sc.y_offset = 0.0
sc.z_offset = 0.1
cutting.update_live_preview(bpy.context)
LIVE_FACES = len(bmesh.from_edit_mesh(obj.data).faces)
check("fixture: the cut output really exceeds the baseline",
      LIVE_FACES > BASELINE_FACES, f"{BASELINE_FACES} -> {LIVE_FACES}")

# Cap between the two: baseline fits, the previous output does not.
preferences.preview_face_cap = lambda: (BASELINE_FACES + LIVE_FACES) // 2
try:
    check("mid-preview the blocker consults the baseline, not the output",
          cutting.live_preview_blocker(obj) is None,
          str(cutting.live_preview_blocker(obj)))
    sc.z_offset = 0.2                 # forces a re-apply
    cutting.update_live_preview(bpy.context)
    check("CAP FIX: the session survives consecutive refreshes near the cap",
          cutting._session_for(obj) is not None)
    check("...and the preview is still applied (no restore flicker)",
          len(bmesh.from_edit_mesh(obj.data).faces) > BASELINE_FACES)
finally:
    preferences.preview_face_cap = orig_cap
cutting.restore_live_cut(bpy.context)
bpy.ops.mesh.select_all(action='DESELECT')

# shape keys
bpy.ops.object.mode_set(mode='OBJECT')
obj.shape_key_add(name="Basis")
bpy.ops.object.mode_set(mode='EDIT')
check("blocker reports shape keys",
      cutting.live_preview_blocker(obj) == "shape keys")
check("header shows the shape-key blocker",
      CutPlaneTool._edge_case_warning(bpy.context, sc)
      == "No live preview: shape keys")
cutting.update_live_preview(bpy.context)
check("session refused on shape keys", cutting._session_for(obj) is None)
bpy.ops.object.mode_set(mode='OBJECT')
obj.shape_key_clear()
bpy.ops.object.mode_set(mode='EDIT')
check("blocker clears with the shape keys gone",
      cutting.live_preview_blocker(obj) is None)

check("cache invariants clean", not cutting.check_invariants(),
      str(cutting.check_invariants()))

print("\nEDGE WARNINGS:", "OK" if not failures else f"FAIL ({len(failures)}): {failures}")
sys.exit(1 if failures else 0)
