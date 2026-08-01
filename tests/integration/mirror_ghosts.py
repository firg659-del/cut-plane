# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless test: the mirror ghosts show exactly where the mirrored cuts land.

    blender --background --factory-startup --python-exit-code 1 \
            --python tests/integration/mirror_ghosts.py

Contract: the ghost planes are a PROMISE about where geometry will be cut.
The cut runs per object (cutting._edit_mesh_objects) and each object reflects
the plane across ITS OWN origin, so in a multi-object edit session N objects
at different locations produce N different sets of mirrored cuts.

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
from mathutils import Vector

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
    """Where the mirrored cuts REALLY land, straight from the cut's own
    plane derivation - the promise the ghosts must match."""
    xs = []
    co_w, no_w = Vector((sc.x_offset, sc.y_offset, sc.z_offset)), Vector((1, 0, 0))
    axes = cutting._mirror_axes(sc)
    for obj in cutting._edit_mesh_objects(bpy.context):
        co_l, no_l = cutting.plane_to_object_local(obj, co_w, no_w)
        for p_co, _p_no in cutting._mirrored_planes_local(co_l, no_l, axes)[1:]:
            xs.append(round((obj.matrix_world @ p_co).x, 3))
    return sorted(xs)

ghosts = ghost_x_positions()
cuts = mirrored_cut_x_positions()

check("a ghost is drawn for EVERY object's mirror, not just the active one",
      len(ghosts) == 2, f"ghosts at x={ghosts}")
check("REGRESSION: the ghosts sit exactly where the mirrored cuts land",
      ghosts == cuts, f"ghosts {ghosts} vs cuts {cuts}")
check("...including the non-active object's mirror (the hidden real cut)",
      any(abs(g - 7.5) < 0.01 for g in ghosts), f"ghosts at x={ghosts}")
check("...and the active object's mirror is still shown",
      any(abs(g + 8.5) < 0.01 for g in ghosts), f"ghosts at x={ghosts}")

# The eye toggle and the axis toggles still gate the whole pass.
sc.show_mirror_planes = False
check("the eye toggle still hides every ghost",
      not drawing._mirror_ghosts_wanted(bpy.context))
sc.show_mirror_planes = True
sc.mirror_x = sc.mirror_y = sc.mirror_z = False
check("no mirror axis means no ghosts", ghost_x_positions() == [])

# 8 planes (X+Y+Z) per object, minus the real one, for two objects.
sc.mirror_x = sc.mirror_y = sc.mirror_z = True
check("X+Y+Z draws all 7 ghosts per object, for both objects",
      len(ghost_x_positions()) == 14, str(len(ghost_x_positions())))

print(f"\nMIRROR GHOSTS: {'FAIL - ' + ', '.join(failures) if failures else 'OK'}")
sys.stdout.flush()
os._exit(1 if failures else 0)
