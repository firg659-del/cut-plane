# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless test: ONE anchor for placement and for mirroring.

    blender --background --factory-startup --python tests/integration/plane_anchor.py

They used to disagree. A fresh plane centred on the BOUNDING BOX (origins are
routinely nowhere near the geometry, and a plane there intersects nothing),
while Mirror reflected about the OBJECT ORIGIN. On any mesh where those differ
the mirrored cut was wrong by exactly 2 x (bbox centre - origin): measured on a
cube whose origin sat 3 below its geometry, Mirror Z put the twin at z=-3,
five units clear of the mesh, so it cut empty space and one plane did all the
work.

plane_anchor now feeds both. BBOX is the default, so placement is unchanged.

Exit code 0 on success, 1 with the failed checks listed.
"""
import os
import sys

import bmesh
import bpy
from mathutils import Vector

ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

import cut_plane  # noqa: E402
from cut_plane import cutting  # noqa: E402
from cut_plane.lib import constants, math_utils  # noqa: E402

constants.DEBUG = True
cut_plane.register()

failures = []


def check(name, cond, detail=""):
    print(f"[{'ok  ' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


def offset_cube():
    """Origin at world 0, geometry sitting 3 above it - the imported-mesh shape."""
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete()
    bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
    o = bpy.context.object
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.transform.translate(value=(0, 0, 3))
    bpy.ops.mesh.select_all(action='DESELECT')
    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.mode_set(mode='EDIT')
    return o


obj = offset_cube()
bpy.context.scene.cursor.location = (0.0, 0.0, 10.0)

# --- 1. the anchor resolves where it should --------------------------------
check("BBOX anchor is the bounding-box centre",
      abs(math_utils.anchor_world(obj, 'BBOX').z - 3.0) < 1e-5,
      str(math_utils.anchor_world(obj, 'BBOX')))
check("ORIGIN anchor is the object origin",
      abs(math_utils.anchor_world(obj, 'ORIGIN').z) < 1e-5,
      str(math_utils.anchor_world(obj, 'ORIGIN')))
check("CURSOR anchor is the 3D cursor",
      abs(math_utils.anchor_world(obj, 'CURSOR').z - 10.0) < 1e-5,
      str(math_utils.anchor_world(obj, 'CURSOR')))

# --- 2. placement is UNCHANGED: BBOX is the default ------------------------
check("REGRESSION: the default anchor still centres on the bounding box",
      (math_utils.anchor_center_offset(obj, 'BBOX')
       - math_utils.default_center_offset(obj)).length < 1e-6)
props = bpy.context.window_manager.cut_plane
check("...and the property defaults to BBOX", props.plane_anchor == 'BBOX',
      props.plane_anchor)

# --- 3. the mirror follows the anchor --------------------------------------
props.x_offset = props.y_offset = props.z_offset = 0.0
props.center_offset = math_utils.anchor_center_offset(obj, 'BBOX')
props.z_offset = 0.5                      # plane at world z = 3.5
props.mirror_x = props.mirror_y = False
props.mirror_z = True
plane = cutting.get_plane_world(bpy.context)
lo = min((obj.matrix_world @ Vector(c)).z for c in obj.bound_box)
hi = max((obj.matrix_world @ Vector(c)).z for c in obj.bound_box)


def mirrored_world_z(anchor):
    props.plane_anchor = anchor
    co_l, no_l = cutting.plane_to_object_local(obj, plane[0], plane[1])
    piv = cutting._mirror_pivot_local(obj, props)
    return sorted(round((obj.matrix_world @ c).z, 3)
                  for c, _ in cutting._mirrored_planes_local(
                      co_l, no_l.normalized(), (2,), piv))


zs_bbox = mirrored_world_z('BBOX')
check("BBOX anchor mirrors about the GEOMETRY (2p - x = 2.5 and 3.5)",
      zs_bbox == [2.5, 3.5], str(zs_bbox))
check("...and both cuts land inside the mesh",
      all(lo <= z <= hi for z in zs_bbox), f"mesh spans {lo}..{hi}")

zs_origin = mirrored_world_z('ORIGIN')
check("REGRESSION: ORIGIN anchor still mirrors about the origin (-3.5, 3.5)",
      zs_origin == [-3.5, 3.5], str(zs_origin))
check("...which is the old behaviour: the twin misses the mesh entirely",
      not (lo <= zs_origin[0] <= hi), f"mesh spans {lo}..{hi}")

zs_cursor = mirrored_world_z('CURSOR')
check("CURSOR anchor mirrors about the cursor (2*10 - 3.5 = 16.5)",
      zs_cursor == [3.5, 16.5], str(zs_cursor))

# --- 4. the cut itself honours it ------------------------------------------
props.plane_anchor = 'BBOX'
# Drop the preview first, or `before` reads the previewed mesh and the check
# proves nothing.
cutting.restore_live_cut(bpy.context)
before = len(bmesh.from_edit_mesh(obj.data).faces)
bpy.ops.view3d.cut_plane_cut(plane_co=plane[0], plane_no=plane[1],
                             mirror_z=True, plane_anchor='BBOX')
after = len(bmesh.from_edit_mesh(obj.data).faces)
check("Execute Cut with a BBOX anchor makes BOTH cuts",
      before == 6 and after == 14, f"{before} -> {after} faces")

# --- 5. the cached anchor centre must follow the snapshot -------------------
# Its readers are hot - the dirty key every frame, the header warning every
# redraw - so it is cached on the session. Uncached, a 40k-vert mesh measured
# 483 ms/frame and 182 ms per REDRAW. A stale cache would mirror about where
# the mesh used to be.
cache = cutting._session_for(obj)
check("the anchor centre is cached on the live session",
      cache is not None and cache.snapshot_bbox_center is not None,
      "cache=%s" % (cache is not None,))
cached = cache.snapshot_bbox_center.copy()
# A committed cut takes a FRESH snapshot, so the cache must be dropped and
# recomputed off the new geometry.
bpy.ops.view3d.cut_plane_cut(plane_co=plane[0], plane_no=plane[1],
                             clear_outer=True, plane_anchor='BBOX')
cache = cutting._session_for(obj)
recomputed = cutting._mirror_pivot_local(obj, props)
check("...and it is recomputed after a cut changes the geometry",
      recomputed is not None and (recomputed - cached).length > 1e-4,
      "was %s now %s" % (tuple(round(v, 3) for v in cached),
                         tuple(round(v, 3) for v in recomputed)))

props.mirror_z = False
props.plane_anchor = 'BBOX' 
props.z_offset = 0.0
cutting.restore_live_cut(bpy.context)
bpy.ops.object.mode_set(mode='OBJECT')

print("\nPLANE ANCHOR:", "OK" if not failures else f"FAIL ({len(failures)}): {failures}")
sys.exit(1 if failures else 0)
