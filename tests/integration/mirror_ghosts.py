# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless test: the mirror ghosts show exactly where the mirrored cuts land.

    blender --background --factory-startup --python-exit-code 1 \
            --python tests/integration/mirror_ghosts.py

Contract: the ghost planes are a PROMISE about where geometry will be cut.
The cut runs per object (cutting._edit_mesh_objects), and every object
mirrors about the SAME anchor - resolved on the active object, the one the
plane is placed from - so a multi-object session produces ONE set of
mirrored cuts that every object is cut at. (It used to be per object, which
made "symmetric" cuts symmetric about nothing in particular: measured, two
cubes gave mirrors at z=-4.5 and z=3.5 for one plane.)

The bug this locks: _draw_mirror_ghosts read context.edit_object - the ACTIVE
object alone. With two cubes in one session the ghosts showed the active
cube's mirror (floating in empty space where nothing is cut) and NO ghost
where the other cube really was cut. The ghosts lied in both directions.

draw_plane_at_matrix is spied on rather than issuing GPU calls: --background
has no GPU context, and the matrices ARE the thing under test.

Exit code 0 on success, 1 with the failed checks listed.
"""
import os
import sys

import bpy
from math import radians

from mathutils import Matrix

ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

import cut_plane  # noqa: E402
from cut_plane import cutting  # noqa: E402
from cut_plane.ui import drawing  # noqa: E402

cut_plane.register()

failures = []


def check(name, cond, detail=""):
    print(f"[{'ok  ' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


# Two cubes, far apart, in ONE edit session. A at the origin (active), B at x=8.
bpy.ops.object.select_all(action='DESELECT')
bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
cube_a = bpy.context.active_object
cube_a.name = "CubeA"
bpy.ops.mesh.primitive_cube_add(location=(8, 0, 0))
cube_b = bpy.context.active_object
cube_b.name = "CubeB"
cube_a.select_set(True)
cube_b.select_set(True)
bpy.context.view_layer.objects.active = cube_a
bpy.ops.object.mode_set(mode='EDIT')

sc = bpy.context.window_manager.cut_plane
sc.mirror_x = True
sc.show_mirror_planes = True
sc.x_offset, sc.y_offset, sc.z_offset = 8.5, 0.0, 0.0
# Face the plane along X. The plane's normal is its local Z, so at the default
# rotation Mirror X reflects it onto ITSELF and correctly produces no cut and
# no ghost - this suite used to fabricate an X normal for its expectation while
# the renderer used the real pose, and only agreed because neither side deduped.
_rot = Matrix.Rotation(radians(90.0), 4, 'Y')
sc.rotation_matrix_flat = [v for row in _rot for v in row]

check("both cubes are in the edit session (the cut applies to both)",
      len(cutting._edit_mesh_objects(bpy.context)) == 2,
      str([o.name for o in cutting._edit_mesh_objects(bpy.context)]))


def ghost_x_positions():
    """The X world position of every ghost plane the draw pass would emit."""
    seen = []
    real = drawing.draw_plane_at_matrix

    def spy(matrix, *a, **kw):
        seen.append(round(matrix.translation.x, 3))

    drawing.draw_plane_at_matrix = spy
    try:
        drawing._draw_mirror_ghosts(bpy.context)
    finally:
        drawing.draw_plane_at_matrix = real
    return sorted(seen)


def mirrored_cut_x_positions():
    """Where the mirrored cuts REALLY land - the cut's own derivation, from
    the REAL plane pose, which is the promise the ghosts must match."""
    co_w, no_w = cutting.get_plane_world(bpy.context)
    axes = cutting._mirror_axes(sc)
    return sorted(round(c.x, 3) for c, _ in cutting._mirrored_planes_world(
        bpy.context, co_w, no_w, axes, sc)[1:])

ghosts = ghost_x_positions()
cuts = mirrored_cut_x_positions()

check("ONE shared ghost set for the session, not one per object",
      len(ghosts) == 1, f"ghosts at x={ghosts}")
check("REGRESSION: the ghosts sit exactly where the mirrored cuts land",
      ghosts == cuts, f"ghosts {ghosts} vs cuts {cuts}")
check("REGRESSION: no ghost is drawn twice (they used to overdraw)",
      len(ghosts) == len(set(round(g, 3) for g in ghosts)),
      f"ghosts at x={ghosts}")
check("...at the active object's anchor (plane x=8.5 -> mirror x=-8.5)",
      all(abs(g + 8.5) < 0.01 for g in ghosts), f"ghosts at x={ghosts}")

# The eye toggle and the axis toggles still gate the whole pass.
sc.show_mirror_planes = False
check("the eye toggle still hides every ghost",
      not drawing._mirror_ghosts_wanted(bpy.context))
sc.show_mirror_planes = True
sc.mirror_x = sc.mirror_y = sc.mirror_z = False
check("no mirror axis means no ghosts", ghost_x_positions() == [])

# The ghosts dedupe exactly as the cut does, so the count follows the number
# of DISTINCT cuts, not the number of toggles. An X-facing plane is reflected
# onto itself by Mirror Y and Mirror Z, so all three axes still make one cut.
sc.mirror_x = sc.mirror_y = sc.mirror_z = True
check("X+Y+Z on an axis-aligned plane: the perpendicular mirrors add nothing",
      len(ghost_x_positions()) == 1, str(len(ghost_x_positions())))

# Only an OBLIQUE plane reaches the full 8 (7 ghosts + the real one), and it
# is still ONE set for the session - it used to be one set per object.
_obl = Matrix.Rotation(radians(35.0), 4, 'Y') @ Matrix.Rotation(radians(25.0), 4, 'X')
sc.rotation_matrix_flat = [v for row in _obl for v in row]
sc.y_offset, sc.z_offset = 0.7, 0.4          # off-centre on every axis too
check("X+Y+Z on an OBLIQUE plane draws all 7, once for the session",
      len(ghost_x_positions()) == 7, str(len(ghost_x_positions())))

print(f"\nMIRROR GHOSTS: {'FAIL - ' + ', '.join(failures) if failures else 'OK'}")
sys.stdout.flush()
os._exit(1 if failures else 0)
