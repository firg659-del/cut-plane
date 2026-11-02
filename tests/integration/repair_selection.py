# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless test: repair_split_selection preserves a cut-through SELECTED edge
but does NOT select the segment between two accumulated cut loops.

    blender --background --factory-startup --python tests/integration/repair_selection.py

A new cut vertex is selected exactly when the edge it split was selected -
read from the UNCUT mesh (cutting.SplitPlan), never from the neighbour verts:
both endpoints of the segment between two accumulated cut loops are selected
cut verts, yet the segment itself never was.

Exit code 0 on success, 1 with the failed checks listed.
"""
import os
import sys

import bmesh
from mathutils import Vector

ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

from cut_plane import cutting  # noqa: E402

failures = []
CO, NO = Vector((1.0, 0.0, 0.0)), Vector((1.0, 0.0, 0.0))


def check(name, cond, detail=""):
    print(f"[{'ok  ' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


def cut_segment(edge_selected):
    """A segment a-b crossing the plane x = 1, its endpoints selected, the
    segment itself selected or not; cut, repaired. Returns the halves'
    selection."""
    bm = bmesh.new()
    a = bm.verts.new((0, 0, 0))
    b = bm.verts.new((2, 0, 0))
    edge = bm.edges.new((a, b))
    a.select = b.select = True
    edge.select = edge_selected       # flag only: both ends stay selected
    bm.select_mode = {'VERT'}
    plan = cutting.split_plan_from_bmesh(bm, CO, NO, 1e-4, face_mode=False)
    res = bmesh.ops.bisect_plane(bm, geom=bm.verts[:] + bm.edges[:], dist=1e-4,
                                 plane_co=CO, plane_no=NO)
    cutting.repair_split_selection(bm, res['geom_cut'], plan)
    halves = [e.select for e in bm.edges]
    bm.free()
    return halves


check("genuine: cut across a SELECTED edge keeps both halves selected",
      cut_segment(True) == [True, True], str(cut_segment(True)))
check("no over-fire: segment between accumulated cut loops stays UNselected",
      not any(cut_segment(False)), str(cut_segment(False)))

print("\nREPAIR SELECTION:", "OK" if not failures else f"FAIL ({len(failures)}): {failures}")
sys.exit(1 if failures else 0)
