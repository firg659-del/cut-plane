# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless test: Cut Selected Only restricts the cut to the selection.

Runs inside Blender:

    blender --background --factory-startup --python tests/integration/cut_selected_only.py

Contract (native mesh.bisect semantics, opt-in via cut_plane.cut_selected_only):
selection is the cut's INPUT domain - unselected geometry is never cut,
cleared, or filled; empty selection cuts nothing (explicit CANCELLED, no
silent whole-mesh fallback); the loop-select OUTPUT is suppressed while on
(the rewritten selection would become the next cut's domain); the live
preview honors the same domain and leaves unselected islands untouched.

Fixture: one object, TWO separate cube islands (A at x=-2, B at x=+2),
plane z=0 through both. Cutting A with Clear Outer must leave B's 8 verts
and 6 faces byte-identical while A loses its top half.

Exit code 0 on success, 1 with the failed checks listed.
"""
import os
import sys

import bmesh
import bpy
from mathutils import Matrix

ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

import cut_plane  # noqa: E402
from cut_plane import cutting  # noqa: E402
from cut_plane.state import pose_history  # noqa: E402

cut_plane.register()

failures = []


def check(name, cond, detail=""):
    print(f"[{'ok  ' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


def fresh_two_islands():
    """One mesh object: island A (cube at x=-2), island B (cube at x=+2)."""
    if bpy.context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    cutting.restore_live_cut(bpy.context)
    cutting._reset_live_state()
    for o in [o for o in bpy.data.objects if o.name.startswith("islands")]:
        bpy.data.objects.remove(o, do_unlink=True)
    # Single-object contract: the factory-startup Cube must NOT ride into
    # the edit session (the cut applies to every edit mesh since the
    # multi-object support - that path has its own suite).
    bpy.ops.object.select_all(action='DESELECT')
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=2.0, matrix=Matrix.Translation((-2, 0, 0)))
    bmesh.ops.create_cube(bm, size=2.0, matrix=Matrix.Translation((2, 0, 0)))
    mesh = bpy.data.meshes.new("islands")
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new("islands", mesh)
    bpy.context.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')

    sc = bpy.context.window_manager.cut_plane
    sc.x_offset = sc.y_offset = sc.z_offset = 0.0
    sc.center_offset = (0.0, 0.0, 0.0)  # plane at origin, normal +Z -> z=0
    sc.rotation_matrix_flat = [v for row in Matrix.Identity(4) for v in row]
    sc.cut_clear_inner = False
    sc.cut_clear_outer = True
    sc.cut_use_fill = False
    sc.cut_select_loop = True   # deliberately ON: must be suppressed
    sc.cut_selected_only = True
    return obj


def run_cut(**overrides):
    """Execute Cut with explicit operator props (what invoke() snapshots from
    the scene - headless bpy.ops skips invoke, so pass them directly)."""
    props = dict(plane_co=(0.0, 0.0, 0.0), plane_no=(0.0, 0.0, 1.0),
                 clear_inner=False, clear_outer=True, use_fill=False,
                 selected_only=True, select_loop=True)
    props.update(overrides)
    return bpy.ops.view3d.cut_plane_cut(**props)


def select_island_a(obj):
    bm = bmesh.from_edit_mesh(obj.data)
    for v in bm.verts:
        v.select = v.co.x < 0
    for e in bm.edges:
        e.select = all(v.co.x < 0 for v in e.verts)
    for f in bm.faces:
        f.select = all(v.co.x < 0 for v in f.verts)
    bmesh.update_edit_mesh(obj.data, destructive=False)


def island_b_stats(obj):
    """(verts, faces, selected_verts) of island B (x > 0)."""
    bm = bmesh.from_edit_mesh(obj.data)
    verts = [v for v in bm.verts if v.co.x > 0]
    faces = [f for f in bm.faces if all(v.co.x > 0 for v in f.verts)]
    return len(verts), len(faces), sum(1 for v in verts if v.select)


def totals(obj):
    bm = bmesh.from_edit_mesh(obj.data)
    return len(bm.verts), len(bm.faces)


# --- 1. Execute Cut: selected island cut+cleared, unselected untouched -----
obj = fresh_two_islands()
select_island_a(obj)
res = run_cut()
bv, bf, _ = island_b_stats(obj)
tv, tf = totals(obj)
check("Execute finished", res == {'FINISHED'}, str(res))
check("island B untouched (8 verts, 6 faces)", (bv, bf) == (8, 6), f"{bv}v {bf}f")
# A: cube bisected at z=0 + clear_outer -> 8 verts (4 original bottom + 4 loop),
# 5 faces (4 lower sides + bottom). Totals: 8+8 verts, 5+6 faces.
check("island A cleared above the plane", (tv, tf) == (16, 11), f"{tv}v {tf}f")

# --- 2. loop-select output suppressed (selection stays = the domain) -------
bm = bmesh.from_edit_mesh(obj.data)
a_all_selected = all(v.select for v in bm.verts if v.co.x < 0)
b_none_selected = not any(v.select for v in bm.verts if v.co.x > 0)
check("selection kept as the input domain (no loop rewrite)",
      a_all_selected and b_none_selected)

# --- 3. Fill caps only the domain's cross-section --------------------------
obj = fresh_two_islands()
bpy.context.window_manager.cut_plane.cut_use_fill = True
select_island_a(obj)
run_cut(use_fill=True)
bm = bmesh.from_edit_mesh(obj.data)
caps = [f for f in bm.faces if all(abs(v.co.z) < 1e-4 for v in f.verts)]
caps_on_b = [f for f in caps if all(v.co.x > 0 for v in f.verts)]
check("Fill caps island A only (1 cap, none on B)",
      len(caps) == 1 and not caps_on_b, f"{len(caps)} caps, {len(caps_on_b)} on B")

# --- 4. empty selection: explicit no-op ------------------------------------
obj = fresh_two_islands()
bm = bmesh.from_edit_mesh(obj.data)
for v in bm.verts:
    v.select = False
for e in bm.edges:
    e.select = False
for f in bm.faces:
    f.select = False
bmesh.update_edit_mesh(obj.data, destructive=False)
before = totals(obj)
res = run_cut()
check("empty selection -> CANCELLED, mesh unchanged",
      res == {'CANCELLED'} and totals(obj) == before, f"{res} {totals(obj)}")

# --- 5. live preview honors the domain --------------------------------------
obj = fresh_two_islands()
select_island_a(obj)
cutting.update_live_preview(bpy.context)
bv, bf, _ = island_b_stats(obj)
check("live preview leaves island B untouched", (bv, bf) == (8, 6), f"{bv}v {bf}f")
inv = cutting.check_invariants()
check("cache invariants clean after domain preview", not inv, str(inv))

# --- 6. domain SURVIVES plane moves (the deselects-while-moving bug) --------
# Every move past the first rebuilds from the snapshot; the domain selection
# must ride the snapshot or it evaporates mid-drag.
bpy.context.window_manager.cut_plane.z_offset = 0.25  # move the plane (2nd apply -> rebuild)
cutting.update_live_preview(bpy.context)
bm = bmesh.from_edit_mesh(obj.data)
a_verts = [v for v in bm.verts if v.co.x < 0]
a_still_selected = all(v.select for v in a_verts)
cut_at_new_plane = any(abs(v.co.z - 0.25) < 1e-4 for v in a_verts)
bv, bf, _ = island_b_stats(obj)
check("domain survives the plane move (A selected, cut at new plane, B intact)",
      a_still_selected and cut_at_new_plane and (bv, bf) == (8, 6),
      f"sel={a_still_selected} newcut={cut_at_new_plane} B={bv}v{bf}f")
bpy.context.window_manager.cut_plane.z_offset = -0.25  # and a third move
cutting.update_live_preview(bpy.context)
bm = bmesh.from_edit_mesh(obj.data)
a_verts = [v for v in bm.verts if v.co.x < 0]
check("domain survives repeated moves",
      all(v.select for v in a_verts)
      and any(abs(v.co.z + 0.25) < 1e-4 for v in a_verts))

# --- 6b. GROWING the selection on the PREVIEW mesh grows the domain ---------
# (the stale-domain-after-reselect bug: selections made while a domain cut is
# previewed must be remapped onto the baseline, not reverted by the rebuild)
bm = bmesh.from_edit_mesh(obj.data)
for v in bm.verts:
    if v.co.x > 0:
        v.select = True
for e in bm.edges:
    if all(v.co.x > 0 for v in e.verts):
        e.select = True
for f in bm.faces:
    if all(v.co.x > 0 for v in f.verts):
        f.select = True
bmesh.update_edit_mesh(obj.data, destructive=False)
bpy.context.window_manager.cut_plane.z_offset = 0.0  # next move triggers the remap
cutting.update_live_preview(bpy.context)
bm = bmesh.from_edit_mesh(obj.data)
a_verts = [v for v in bm.verts if v.co.x < 0]
b_verts = [v for v in bm.verts if v.co.x > 0]
check("selection grown mid-preview: both islands cut at the new plane",
      any(abs(v.co.z) < 1e-4 for v in a_verts)
      and any(abs(v.co.z) < 1e-4 for v in b_verts)
      and all(v.select for v in b_verts))

# --- 6c. DESELECTING everything mid-preview clears domain and cut -----------
bm = bmesh.from_edit_mesh(obj.data)
for elem in list(bm.verts) + list(bm.edges) + list(bm.faces):
    elem.select = False
bmesh.update_edit_mesh(obj.data, destructive=False)
bpy.context.window_manager.cut_plane.z_offset = 0.1
cutting.update_live_preview(bpy.context)
check("deselect-all mid-preview restores the uncut baseline",
      totals(obj) == (16, 12), str(totals(obj)))
inv = cutting.check_invariants()
check("invariants clean after selection remaps", not inv, str(inv))

# --- 7. STANDARD mode never deselects (smooth-experience rule) --------------
# The loop-select is ADDITIVE now: the user's selection survives previews and
# commits, and deselections made mid-preview stick (same adopt/remap machinery
# as Selected-Only, enabled for both modes).
obj = fresh_two_islands()
sc = bpy.context.window_manager.cut_plane
sc.cut_selected_only = False
sc.cut_clear_outer = False  # pure split: all original verts survive
sc.z_offset = 0.0
select_island_a(obj)                        # user selection at baseline
cutting.update_live_preview(bpy.context)    # whole-mesh split + loop ADDED
bm = bmesh.from_edit_mesh(obj.data)
a_corners = [v for v in bm.verts if v.co.x < 0 and abs(v.co.z) > 0.9]
loop_sel = [v for v in bm.verts if abs(v.co.z) < 1e-4 and v.select]
check("standard preview keeps the user's selection and ADDS the loop",
      all(v.select for v in a_corners) and loop_sel,
      f"corners={sum(v.select for v in a_corners)}/{len(a_corners)} "
      f"loop={len(loop_sel)}")
# The split must be selection-preserving: BOTH halves of every cut face of
# the selected island stay selected (bisect doesn't copy flags to the new
# half - repair_split_selection fixes that), while the unselected island's
# halves stay unselected.
a_faces = [f for f in bm.faces if all(v.co.x < 0 for v in f.verts)]
b_faces = [f for f in bm.faces if all(v.co.x > 0 for v in f.verts)]
check("split halves inherit selection (no half-deselected band)",
      all(f.select for f in a_faces) and not any(f.select for f in b_faces),
      f"A {sum(f.select for f in a_faces)}/{len(a_faces)} sel, "
      f"B {sum(f.select for f in b_faces)}/{len(b_faces)} sel")

bm = bmesh.from_edit_mesh(obj.data)         # user deselects island A entirely
for v in bm.verts:
    if v.co.x < 0:
        v.select = False
for e in bm.edges:
    if any(v.co.x < 0 for v in e.verts):
        e.select = False
for f in bm.faces:
    if any(v.co.x < 0 for v in f.verts):
        f.select = False
bmesh.update_edit_mesh(obj.data, destructive=False)
sc.z_offset = 0.3                            # next move: remap + rebuild
cutting.update_live_preview(bpy.context)
bm = bmesh.from_edit_mesh(obj.data)
a_corners = [v for v in bm.verts if v.co.x < 0 and abs(v.co.z) > 0.9]
new_loop = [v for v in bm.verts if abs(v.co.z - 0.3) < 1e-4 and v.select]
check("deselection made mid-preview STICKS across plane moves",
      not any(v.select for v in a_corners) and new_loop,
      f"corners_sel={sum(v.select for v in a_corners)} loop={len(new_loop)}")

# --- 8. Clear must be FACE-GRANULAR: unselected neighbors survive -----------
# The torus report: bisect's own clear deletes cleared-side VERTS, killing
# every unselected neighbor face sharing the domain's boundary verts. Fixture:
# one connected vertical grid, only the middle face column selected.
if bpy.context.mode != 'OBJECT':
    bpy.ops.object.mode_set(mode='OBJECT')
cutting.restore_live_cut(bpy.context)
cutting._reset_live_state()
bm0 = bmesh.new()
bmesh.ops.create_grid(bm0, x_segments=4, y_segments=4, size=1.5,
                      matrix=Matrix.Rotation(1.5707963, 4, 'X'))
mesh = bpy.data.meshes.new("grid")
bm0.to_mesh(mesh)
bm0.free()
obj = bpy.data.objects.new("grid", mesh)
bpy.context.collection.objects.link(obj)
bpy.context.view_layer.objects.active = obj
obj.select_set(True)
bpy.ops.object.mode_set(mode='EDIT')

bm = bmesh.from_edit_mesh(obj.data)
for elem in list(bm.verts) + list(bm.edges) + list(bm.faces):
    elem.select = False
column = [f for f in bm.faces if abs(f.calc_center_median().x) < 0.4]
for f in column:
    f.select = True
    for v in f.verts:
        v.select = True
    for e in f.edges:
        e.select = True
unsel_before = [f for f in bm.faces if not f.select]
n_unsel_before = len(unsel_before)
area_unsel_before = sum(f.calc_area() for f in unsel_before)
bmesh.update_edit_mesh(obj.data, destructive=False)

res = run_cut()  # clear_outer=True, plane z=0
bm = bmesh.from_edit_mesh(obj.data)
unsel_after = [f for f in bm.faces if not f.select]
area_unsel_after = sum(f.calc_area() for f in unsel_after)
sel_above = [f for f in bm.faces
             if f.select and f.calc_center_median().z > 1e-4]
sel_below = [f for f in bm.faces
             if f.select and f.calc_center_median().z < -1e-4]
check("clear is face-granular: every unselected neighbor face survives",
      res == {'FINISHED'} and len(unsel_after) == n_unsel_before
      and abs(area_unsel_after - area_unsel_before) < 1e-4,
      f"{n_unsel_before}->{len(unsel_after)} faces, "
      f"area {area_unsel_before:.3f}->{area_unsel_after:.3f}")
check("selected column cleared above the plane, kept below",
      not sel_above and sel_below,
      f"{len(sel_above)} above, {len(sel_below)} below")

# --- 9. count-collision must never bake the preview (cube + Clear + Fill) ---
# A mid-prism Clear+Fill cut reproduces the baseline's EXACT counts (cube:
# 8v/12e/6f -> 8v/12e/6f), which fooled the old count-based rebuild skip into
# cutting the already-cut mesh on every move and skipping the exit restore -
# preview cuts baked in without Execute (the user's fill/clear-inner report).
if bpy.context.mode != 'OBJECT':
    bpy.ops.object.mode_set(mode='OBJECT')
cutting.restore_live_cut(bpy.context)
cutting._reset_live_state()
bm0 = bmesh.new()
bmesh.ops.create_cube(bm0, size=2.0)
mesh = bpy.data.meshes.new("collide")
bm0.to_mesh(mesh)
bm0.free()
obj = bpy.data.objects.new("collide", mesh)
bpy.context.collection.objects.link(obj)
bpy.context.view_layer.objects.active = obj
obj.select_set(True)
bpy.ops.object.mode_set(mode='EDIT')
sc = bpy.context.window_manager.cut_plane
sc.x_offset = sc.y_offset = sc.z_offset = 0.0
sc.center_offset = (0.0, 0.0, 0.0)
sc.rotation_matrix_flat = [v for row in Matrix.Identity(4) for v in row]
sc.cut_selected_only = False
sc.cut_clear_inner = True
sc.cut_clear_outer = False
sc.cut_use_fill = True

cutting.update_live_preview(bpy.context)  # cut at z=0: counts == baseline!
bm = bmesh.from_edit_mesh(obj.data)
collided = (len(bm.verts), len(bm.edges), len(bm.faces)) == (8, 12, 6)
check("fixture reproduces the count collision", collided,
      f"{len(bm.verts)}v {len(bm.edges)}e {len(bm.faces)}f")

sc.z_offset = -0.5
cutting.update_live_preview(bpy.context)  # must re-cut the ORIGINAL cube
bm = bmesh.from_edit_mesh(obj.data)
min_z = min(v.co.z for v in bm.verts)
check("moving the plane re-cuts from the baseline (no compounding)",
      abs(min_z + 0.5) < 1e-4, f"min z = {min_z} (compounded cut leaves 0.0)")

cutting.restore_live_cut(bpy.context)  # tool exit must bring everything back
bm = bmesh.from_edit_mesh(obj.data)
min_z = min(v.co.z for v in bm.verts)
check("exit restore reverts the cleared side despite the collision",
      abs(min_z + 1.0) < 1e-4 and len(bm.faces) == 6,
      f"min z = {min_z}, {len(bm.faces)} faces")

# --- 10. F9 adjustments WRITE BACK to the toolbar (redo <-> tool binding) ---
# The operator props are snapshots of the tool settings; without write-back
# an F9 change silently diverged from the header checkboxes and the preview.
sc.cut_use_fill = False
sc.cut_clear_inner = False
sc.cut_clear_outer = True
sc.cut_selected_only = True
sc.cut_select_loop = True
run_cut(use_fill=True, clear_inner=True, clear_outer=False,
        selected_only=False, select_loop=False)  # simulate F9 re-run
check("redo adjustments write back to the tool settings",
      sc.cut_use_fill and sc.cut_clear_inner and not sc.cut_clear_outer
      and not sc.cut_selected_only and not sc.cut_select_loop,
      f"fill={sc.cut_use_fill} in={sc.cut_clear_inner} out={sc.cut_clear_outer} "
      f"selonly={sc.cut_selected_only} loop={sc.cut_select_loop}")

# --- 11. Clear must not deselect faces the cut line crosses (torus report) --
# bisect_plane reuses the original face for an ARBITRARY half; with a Clear
# the selected sibling can die inside the bisect, leaving the kept half as a
# new unselected face with nothing for the two-sided repair to compare
# (~40% of a selected band lost, "sometimes"). The snapshot-BVH one-sided
# repair recovers the source's selection. Also: hidden-side preservation is
# Selected-Only-scoped now - standard mode must not accumulate stale
# invisible selections (the primitive spawns fully selected).
if bpy.context.mode != 'OBJECT':
    bpy.ops.object.mode_set(mode='OBJECT')
cutting.restore_live_cut(bpy.context)
cutting._reset_live_state()
bpy.ops.mesh.primitive_torus_add(major_radius=1.0, minor_radius=0.25,
                                 major_segments=24, minor_segments=8)
obj = bpy.context.active_object
bpy.ops.object.mode_set(mode='EDIT')
sc = bpy.context.window_manager.cut_plane
sc.x_offset = sc.y_offset = 0.0
sc.z_offset = 0.1
sc.center_offset = (0.0, 0.0, 0.0)
sc.rotation_matrix_flat = [v for row in Matrix.Identity(4) for v in row]
sc.cut_selected_only = False
sc.cut_clear_inner = False
sc.cut_clear_outer = True
sc.cut_use_fill = False
sc.cut_select_loop = True

bm = bmesh.from_edit_mesh(obj.data)
for elem in list(bm.verts) + list(bm.edges) + list(bm.faces):
    elem.select = False
bmesh.update_edit_mesh(obj.data, destructive=False)
cutting.update_live_preview(bpy.context)  # preview cut+clear at z=0.1

bm = bmesh.from_edit_mesh(obj.data)  # select the visible band along the line
picked = [f for f in bm.faces if any(abs(v.co.z - 0.1) < 1e-4 for v in f.verts)]
for f in picked:
    f.select_set(True)
bmesh.update_edit_mesh(obj.data, destructive=False)
bvh = cutting._get_or_build_snapshot_bvh(cutting._session_for(obj))
src_ids = set()
for f in picked:
    hit = bvh.find_nearest(f.calc_center_median())
    if hit is not None and hit[2] is not None:
        src_ids.add(hit[2])

sc.z_offset = 0.14
cutting.update_live_preview(bpy.context)  # remap + rebuild + re-cut + clear

snap = cutting._session_for(obj).live_snapshot
snap_sel = {p.index for p in snap.polygons if p.select}
bm = bmesh.from_edit_mesh(obj.data)
bvh = cutting._get_or_build_snapshot_bvh(cutting._session_for(obj))
live_srcs = set()
for f in bm.faces:
    if f.select:
        hit = bvh.find_nearest(f.calc_center_median())
        if hit is not None and hit[2] is not None:
            live_srcs.add(hit[2])
lost = src_ids - live_srcs
check("Clear keeps every line-crossed selected face selected (one-sided repair)",
      not lost and len(src_ids) > 10,
      f"{len(lost)} of {len(src_ids)} sources lost")
check("standard mode accumulates no stale hidden-side selections",
      snap_sel == src_ids,
      f"snapshot {len(snap_sel)} vs intended {len(src_ids)}")

# --- the operator must never destroy geometry and report CANCELLED --------
# `bm` from bmesh.from_edit_mesh IS the live edit mesh, so a mirrored plane
# that clears the whole selection is already real when the NEXT plane finds
# nothing selected. A mid-loop `return 'EMPTY'` there jumped the
# count-changed guard: execute() said "nothing selected to cut" + CANCELLED
# *after* wiping the mesh - so Blender pushed no undo step and
# pose_history.mark_mesh_step never ran (the tool-scoped Ctrl+Z, which
# shadows global undo, never learned a mesh step happened).


def fresh_cube(name="emptycube"):
    if bpy.context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    cutting.restore_live_cut(bpy.context)
    cutting._reset_live_state()
    for o in [o for o in bpy.data.objects if o.name.startswith(name)]:
        bpy.data.objects.remove(o, do_unlink=True)
    bpy.ops.object.select_all(action='DESELECT')
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=2.0)
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(ob)
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    return ob


def counts(ob):
    b = bmesh.from_edit_mesh(ob.data)
    return (len(b.verts), len(b.edges), len(b.faces))


ob = fresh_cube()
before = counts(ob)
token_before = bpy.context.scene.get(pose_history.TOKEN_PROP, 0)
# Mirror X + Clear Inner + a plane past the mesh: plane 1 clears the whole
# selection, plane 2 then finds an empty domain.
res = bpy.ops.view3d.cut_plane_cut(
    'EXEC_DEFAULT', plane_co=(5.0, 0.0, 0.0), plane_no=(1.0, 0.0, 0.0),
    clear_inner=True, selected_only=True, mirror_x=True)
after = counts(ob)
check("a mirrored Clear that empties the selection does NOT report CANCELLED",
      res != {'CANCELLED'} or after == before,
      f"{res} and {before} -> {after}")
check("...it reports FINISHED, so Blender pushes an undo step",
      res == {'FINISHED'}, str(res))
check("...and advances the mesh-step counter for the tool-scoped Ctrl+Z",
      bpy.context.scene.get(pose_history.TOKEN_PROP, 0) > token_before,
      f"token {token_before} -> {bpy.context.scene.get(pose_history.TOKEN_PROP, 0)}")

# The legitimate EMPTY must survive the fix: nothing selected at all means
# nothing cut, nothing changed, and an honest CANCELLED.
ob = fresh_cube()
bpy.ops.mesh.select_all(action='DESELECT')
before = counts(ob)
res = bpy.ops.view3d.cut_plane_cut(
    'EXEC_DEFAULT', plane_co=(0.0, 0.0, 0.0), plane_no=(0.0, 0.0, 1.0),
    selected_only=True, mirror_x=True)
check("an empty selection still CANCELs (native semantics, nothing touched)",
      res == {'CANCELLED'} and counts(ob) == before,
      f"{res} and {before} -> {counts(ob)}")

# --- the remap must judge the mesh against the state that PRODUCED it ------
# _sync_baseline used to hand the selection remap the INCOMING plane/clear
# flags, but the bmesh it reads is still the PREVIOUS apply's result - and
# the dirty check guarantees those two states DIFFER whenever the remap
# runs. The hidden() guard (which preserves flags on the cleared side, the
# geometry the preview cannot show) then misfired on everything between the
# two states and wrote the loss into the snapshot - the baseline every
# rebuild restores. Permanent, silent, and in Selected-Only mode the
# selection IS the cut domain. Measured: one Clear-checkbox untick
# deselected 256 of 384 faces. The remap now reads cache.written_state.

sc = bpy.context.window_manager.cut_plane


def erosion_cube(name):
    ob = fresh_cube(name)
    for _ in range(3):
        bpy.ops.mesh.subdivide()
    bpy.ops.mesh.select_all(action='SELECT')
    return ob


def domain(ob):
    """The Selected-Only cut domain = the snapshot's stored selection."""
    c = cutting._session_for(ob)
    return sum(1 for p in c.live_snapshot.polygons if p.select)


def deselect_one(ob):
    bm = bmesh.from_edit_mesh(ob.data)
    next(f for f in bm.faces if f.select).select_set(False)
    bmesh.update_edit_mesh(ob.data)


# TRIGGER 1: untick Clear with the plane unmoved (the worst case - one click)
ob = erosion_cube("erosion")
sc.cut_selected_only = True
sc.cut_clear_outer = True
sc.x_offset = sc.y_offset = 0.0
sc.z_offset = -0.5
cutting.update_live_preview(bpy.context)
total = domain(ob)
deselect_one(ob)
sc.cut_clear_outer = False
cutting.update_live_preview(bpy.context)
check("EROSION: unticking Clear after one deselect keeps the domain",
      domain(ob) == total - 1, f"{domain(ob)} of {total} (correct {total - 1})")

# TRIGGER 2: a one-refresh plane jump (the tool's own Ctrl+Z path)
sc.cut_clear_outer = True
cutting.update_live_preview(bpy.context)
deselect_one(ob)
sc.z_offset = 0.5          # jumps in a single refresh, like pose undo
cutting.update_live_preview(bpy.context)
check("EROSION: a one-refresh plane jump after a deselect keeps the domain",
      domain(ob) == total - 2, f"{domain(ob)} of {total} (correct {total - 2})")

# TRIGGER 3: mirrored Clear - EVERY plane of the producing apply shields its
# own cleared side, not just the primary one.
ob = erosion_cube("erosionmir")
sc.cut_clear_outer = False
sc.cut_clear_inner = True
sc.mirror_z = True         # planes at z=-0.5 AND z=+0.5, each clearing inward
sc.z_offset = -0.5
cutting.update_live_preview(bpy.context)
total = domain(ob)
deselect_one(ob)
sc.cut_clear_inner = False
cutting.update_live_preview(bpy.context)
check("EROSION: the MIRRORED plane's cleared side is preserved too",
      domain(ob) == total - 1, f"{domain(ob)} of {total} (correct {total - 1})")

sc.mirror_z = False
sc.cut_clear_inner = False
sc.cut_selected_only = False
cutting.restore_live_cut(bpy.context)

# --- the header warning must read the BASELINE, like the engine does -------
# Execute Cut restores the baseline before bisecting, so its Selected-Only
# domain is the SNAPSHOT's selection. The warning used to read the live
# mesh's total_vert_sel - which the destructive preview zeroes when a Clear
# deletes the selected side - so the header said "Nothing selected to cut"
# about a cut that was about to succeed.
from cut_plane.ui.tool import CutPlaneTool  # noqa: E402

ob = fresh_cube("warncube")
sc.cut_selected_only = True
sc.cut_clear_outer = True
sc.x_offset = sc.y_offset = 0.0
sc.z_offset = -1.5          # whole cube on the cleared side
cutting.update_live_preview(bpy.context)

check("the preview really zeroed the LIVE selection (fixture is honest)",
      ob.data.total_vert_sel == 0, str(ob.data.total_vert_sel))
check("...while the baseline still holds the full domain",
      cutting.baseline_selection_counts(ob)[0] > 0,
      str(cutting.baseline_selection_counts(ob)))
warning = CutPlaneTool._edge_case_warning(bpy.context, sc)
check("WARNING FIX: the header does NOT claim 'Nothing selected to cut'",
      warning != "Nothing selected to cut", repr(warning))

# ...and a genuinely empty domain still warns.
cutting.restore_live_cut(bpy.context)
bpy.ops.mesh.select_all(action='DESELECT')
warning = CutPlaneTool._edge_case_warning(bpy.context, sc)
check("a genuinely empty selection still warns",
      warning == "Nothing selected to cut", repr(warning))

sc.cut_clear_outer = False
sc.cut_selected_only = False
cutting.restore_live_cut(bpy.context)

# --- a selection made MID-PREVIEW is honored by an immediate Execute -------
# Execute restores the baseline before bisecting; the restore now FOLDS any
# pending selection change into the baseline first (_fold_pending_selection).
# Without that, selecting faces and pressing Cut right away reported
# "Selected Only: nothing selected to cut" while the user stared at their
# fully selected mesh - the rebuild had just reverted the selection.
ob = fresh_cube("midsel")
bpy.ops.mesh.select_all(action='DESELECT')
sc.z_offset = 0.0
cutting.update_live_preview(bpy.context)     # a real cut is on screen
bpy.ops.mesh.select_all(action='SELECT')     # user selects NOW, no refresh
f0 = counts(ob)[2]
res = bpy.ops.view3d.cut_plane_cut(
    'EXEC_DEFAULT', plane_co=(0.0, 0.0, 0.3), plane_no=(0.0, 0.0, 1.0),
    selected_only=True)
check("MID-PREVIEW: Execute honors a selection made since the last refresh",
      res == {'FINISHED'}, str(res))
check("...and really cuts it", counts(ob)[2] > 6, f"faces {counts(ob)[2]}")

print(f"\nCUT SELECTED ONLY: {'FAIL - ' + ', '.join(failures) if failures else 'OK'}")
sys.stdout.flush()
os._exit(1 if failures else 0)
