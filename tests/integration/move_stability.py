# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless test: move-gizmo tracking is stable at any distance.

Runs inside Blender:

    blender --background --factory-startup --python tests/integration/move_stability.py

The field bug this locks: dragging a move ARROW far away accelerated
exponentially (each mouse delta was scaled by the pixel size at the
plane's CURRENT depth - gain compounding), and once far, one leftward
event yanked the plane back thousands of units. The PLANE squares had a
separate defect: the ray-plane intersection had no t>0 or range guard,
so approaching the constraint plane's horizon the hit raced to +infinity
and one tilt past it teleported to the opposite side (measured +-50k).

Contracts:
  1. ARROW rail tracking is ABSOLUTE (ray-line closest point): the final
     position depends only on the final mouse ray, not the event count or
     path - no gain to compound, drags are exactly reversible.
  2. PLANE tracking rejects horizon-crossed hits (t <= 0) and grazing
     blowups (hit beyond _PLANE_TRACK_RANGE x view distance) - the caller
     then uses its bounded projection fallback instead of teleporting.
  3. Structure: the arrow modal branch actually uses the rail math (a
     revert to the incremental px-size mapping fails this suite).

Exit code 0 on success, 1 with the failed checks listed.
"""
import inspect
import math
import os
import sys
import types

from mathutils import Vector

ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

from cut_plane.operators import move as move_mod  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"[{'ok  ' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


def ray_for(px: float, cam=Vector((0.0, 0.0, 5.0)), px_scale=0.001):
    """A mouse ray: camera looking -Z, horizontal pixel offset px."""
    d = Vector((px * px_scale, 0.0, -1.0)).normalized()
    return cam, d


# --- 1. ARROW rail: the closest-point mapping is sane ----------------------
# NOTE on method: absolute tracking makes runaway impossible BY SHAPE (the
# summed increments of any pure function of the mouse ray telescope to
# f(end) - f(start), so no property-based check here can distinguish good
# from bad absolute math - the old bug was STATEFUL gain, which this
# function cannot express). The mutation lock for the arrow branch is
# therefore the structural check in section 3; these checks pin the
# mapping's geometry.
axis = Vector((1.0, 0.0, 0.0))
anchor = Vector((0.0, 0.0, 0.0))


def rail_point(px):
    o, d = ray_for(px)
    return move_mod._ray_line_closest_point(o, d, anchor, axis)


check("straight-down ray lands on the anchor",
      (rail_point(0.0) - anchor).length < 1e-9)
seq = [rail_point(px).x for px in (0.0, 250.0, 500.0, 1000.0, 2000.0)]
check("rail position is monotone in the mouse position",
      all(a < b for a, b in zip(seq, seq[1:])), str([round(v, 2) for v in seq]))
check("rail stays on the rail (no off-axis leakage)",
      all(abs(rail_point(px).y) < 1e-9 and abs(rail_point(px).z) < 1e-9
          for px in (250.0, 2000.0)))


def rail_tilt(dz):
    """Rail point for a ray tilting by dz toward/past the vanishing point."""
    L = math.sqrt(1.0 + dz * dz)
    return move_mod._ray_line_closest_point(
        Vector((0.0, 0.0, 5.0)), Vector((1.0 / L, 0.0, dz / L)), anchor, axis)


# THE FIELD BUG: past the axis's vanishing point the closest point used to
# flip from +5000 to -5000 in one event - the plane racing back through
# the camera. All pathological samples must be rejected now.
check("sane tilt still tracks (rail x = 50)",
      rail_tilt(-0.1) is not None and abs(rail_tilt(-0.1).x - 50.0) < 1e-3)
check("grazing blowup before the vanishing point is rejected",
      rail_tilt(-0.001) is None)  # rail point would be +5000
check("VANISHING POINT crossed: diverging ray is rejected (the collapse)",
      rail_tilt(+0.001) is None and rail_tilt(+0.1) is None)
check("exactly parallel ray is rejected (old fallback teleported to the "
      "camera's shadow on the rail)",
      move_mod._ray_line_closest_point(
          Vector((0.0, 3.0, 5.0)), axis, anchor, axis) is None)

# --- 2. PLANE squares: horizon and grazing guards ---------------------------
shim = types.SimpleNamespace(start_pos_world=anchor)
_ray = [None]
shim._mouse_ray = lambda ctx, ev: _ray[0]
intersect = move_mod.CutPlaneMoveCenterOperator._mouse_plane_intersect
normal = Vector((0.0, 0.0, 1.0))  # constraint plane z=0, camera at z=5


def hit_for(dz):
    L = math.sqrt(1.0 + dz * dz)
    _ray[0] = (Vector((0.0, 0.0, 5.0)), Vector((1.0 / L, 0.0, dz / L)))
    return intersect(shim, None, None, normal)


h = hit_for(-0.1)  # sane downward tilt: t = 50, hit ~50 units out
check("a sane ray still intersects (t=50 case)",
      h is not None and abs(h.x - 50.0) < 1e-3, str(h))
check("grazing blowup is rejected (hit would be ~5000 units out)",
      hit_for(-0.001) is None)
check("horizon-crossed ray is rejected (t would be negative)",
      hit_for(+0.001) is None)
check("STEEPLY past-horizon ray is rejected (bogus hit lies WITHIN the "
      "range guard - only the t>0 gate catches it)",
      hit_for(+0.5) is None)
check("hard-parallel ray is rejected (epsilon guard still works)",
      hit_for(0.0) is None)

# --- 3. structure: the arrow branch uses the rail math ----------------------
src = inspect.getsource(move_mod.CutPlaneMoveAxisOperator.handle_modal_event)
check("arrow modal branch calls the rail math",
      "_ray_line_closest_point(" in src)
guard_src = inspect.getsource(move_mod.CutPlaneMoveCenterOperator._mouse_plane_intersect)
check("plane intersect carries the horizon guard",
      "t <= 0.0" in guard_src and "_TRACK_RANGE" in guard_src)
rail_src = inspect.getsource(move_mod._ray_line_closest_point)
check("rail math carries the vanishing-point gate",
      "s_ray" in rail_src and "_TRACK_RANGE" in rail_src)

print(f"\nMOVE STABILITY: {'FAIL - ' + ', '.join(failures) if failures else 'OK'}")
sys.stdout.flush()
os._exit(1 if failures else 0)
