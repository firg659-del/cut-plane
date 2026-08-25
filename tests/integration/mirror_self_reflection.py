# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless test: a plane that mirrors ONTO ITSELF must cut once, not twice.

    blender --background --factory-startup --python tests/integration/mirror_self_reflection.py

REGRESSION. _plane_key keeps the normal's SIGN on purpose - an opposite-
facing twin is normally a different cut, because its Clear side points the
other way. But when the twin is the SAME geometric plane (the plane lies on
the mirror axis plane AND its normal runs along that axis), the two are not
two cuts: one clears each side, and their union is the whole mesh.

This is the DEFAULT state, not an exotic one - the plane spawns at the mesh
centre with a Z normal, which on a fresh primitive is the object origin. So
Mirror Z + Clear on a new cube emptied the mesh, and the live preview did it
on every mouse move.

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
from cut_plane.lib import constants  # noqa: E402

constants.DEBUG = True
cut_plane.register()

failures = []


def check(name, cond, detail=""):
    print(f"[{'ok  ' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


def counts(obj):
    bm = bmesh.from_edit_mesh(obj.data)
    return len(bm.verts), len(bm.edges), len(bm.faces)


def fresh_cube():
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete()
    bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
    obj = bpy.context.object
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='DESELECT')
    return obj


# --- 1. the expansion helper -------------------------------------------------
# Normal ALONG the mirror axis, plane through the origin: the reflection maps
# the plane onto itself and only reverses its orientation.
co, no = Vector((0.0, 0.0, 0.0)), Vector((0.0, 0.0, 1.0))
self_mirror = cutting._mirrored_planes_local(co, no, (2,))
check("a plane that mirrors onto ITSELF dedupes to one cut",
      len(self_mirror) == 1,
      f"{len(self_mirror)} planes: "
      + "; ".join(f"co={tuple(round(v, 2) for v in c)} "
                  f"no={tuple(round(v, 2) for v in n)}" for c, n in self_mirror))

# The already-covered case must keep working: normal PERPENDICULAR to the
# mirror axis, every reflected component zero.
check("REGRESSION: the perpendicular on-axis case still dedupes",
      len(cutting._mirrored_planes_local(co, no, (0,))) == 1)

# ...and genuinely distinct twins must SURVIVE. Same normal direction, but
# the twin sits at the opposite offset - two real cuts, clears facing out.
off = Vector((0.0, 0.0, 1.0))
pair = cutting._mirrored_planes_local(off, no, (2,))
check("an OFF-axis twin is still a second cut", len(pair) == 2,
      f"{len(pair)} planes")
check("...and it sits on the far side of the mirror",
      abs(pair[1][0].z + 1.0) < 1e-6, f"z={pair[1][0].z}")
check("...still facing the opposite way (its Clear side mirrors too)",
      abs(pair[1][1].z + 1.0) < 1e-6, f"nz={pair[1][1].z}")
# An OBLIQUE normal reflects to eight genuinely different planes, so the
# documented 2/4/8 expansion is unchanged where it is real.
obl_co, obl_no = Vector((0.5, 0.2, 0.3)), Vector((1.0, 0.7, 0.4)).normalized()
check("REGRESSION: an oblique plane still expands 2/4/8",
      (len(cutting._mirrored_planes_local(obl_co, obl_no, (0,))) == 2
       and len(cutting._mirrored_planes_local(obl_co, obl_no, (0, 1))) == 4
       and len(cutting._mirrored_planes_local(obl_co, obl_no, (0, 1, 2))) == 8),
      "%d/%d/%d" % (len(cutting._mirrored_planes_local(obl_co, obl_no, (0,))),
                    len(cutting._mirrored_planes_local(obl_co, obl_no, (0, 1))),
                    len(cutting._mirrored_planes_local(obl_co, obl_no, (0, 1, 2)))))
# A normal PERPENDICULAR to a mirror axis reflects the plane onto itself:
# sliding an infinite plane within itself does not move the cut, so that
# mirror adds no cut - it only used to add a duplicate (and a second Fill).
check("a mirror perpendicular to the normal adds no cut",
      len(cutting._mirrored_planes_local(Vector((0.5, 0.2, 0.3)),
                                         Vector((1.0, 0.0, 0.0)), (1,))) == 1)

# --- 2. Execute Cut must not empty the mesh ---------------------------------
for label, kw in (("Clear Outer", dict(mirror_z=True, clear_outer=True)),
                  ("Clear Inner", dict(mirror_z=True, clear_inner=True))):
    obj = fresh_cube()
    bpy.ops.view3d.cut_plane_cut(plane_co=(0, 0, 0), plane_no=(0, 0, 1), **kw)
    v, e, f = counts(obj)
    check(f"Mirror Z + {label} keeps geometry (was 0 faces: mesh wiped)",
          f > 0, f"verts={v} edges={e} faces={f}")
    bpy.ops.object.mode_set(mode='OBJECT')

# Baseline first, so the expected count is measured rather than guessed.
obj = fresh_cube()
bpy.ops.view3d.cut_plane_cut(plane_co=(0, 0, 0), plane_no=(0, 0, 1),
                             use_fill=True)
base_f = counts(obj)[2]
bpy.ops.object.mode_set(mode='OBJECT')
obj = fresh_cube()
bpy.ops.view3d.cut_plane_cut(plane_co=(0, 0, 0), plane_no=(0, 0, 1),
                             mirror_z=True, use_fill=True)
f = counts(obj)[2]
check("Mirror Z + Fill caps the cross-section ONCE (was capped twice)",
      f == base_f, f"faces={f} vs unmirrored {base_f}")
bpy.ops.object.mode_set(mode='OBJECT')

# --- 3. the live preview runs on every mouse move - it must not wipe either --
obj = fresh_cube()
props = bpy.context.window_manager.cut_plane
props.x_offset = props.y_offset = props.z_offset = 0.0
props.mirror_x = props.mirror_y = False
props.mirror_z = True
props.cut_clear_outer = True
cutting.update_live_preview(bpy.context)
v, e, f = counts(obj)
check("live PREVIEW with Mirror Z + Clear Outer keeps geometry",
      f > 0, f"verts={v} edges={e} faces={f}")
check("...and the session invariants still hold",
      not cutting.check_invariants())

props.cut_clear_outer = False
props.mirror_z = False
cutting.restore_live_cut(bpy.context)
bpy.ops.object.mode_set(mode='OBJECT')

print("\nMIRROR SELF REFLECTION:",
      "OK" if not failures else f"FAIL ({len(failures)}): {failures}")
sys.exit(1 if failures else 0)
