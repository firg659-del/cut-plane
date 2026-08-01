# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless test: repair_split_selection preserves a cut-through SELECTED edge
but does NOT select the segment between two accumulated cut loops.

    blender --background --factory-startup --python tests/integration/repair_selection.py

The vertex branch of repair_split_selection must key off SIBLING agreement (was
the source edge selected -> keep both halves), not off neighbour verts being
selected. Otherwise cutting a new line between two already-selected cut loops
selects the in-between edge, because both its endpoints are selected cut-verts.

Exit code 0 on success, 1 with the failed checks listed.
"""
import os
import sys

import bmesh

ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

from cut_plane import cutting  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"[{'ok  ' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


def genuine_case():
    """A SELECTED source edge is cut across -> both halves must stay selected."""
    bm = bmesh.new()
    a = bm.verts.new((0, 0, 0))
    b = bm.verts.new((2, 0, 0))
    c = bm.verts.new((1, 0, 0))
    d = bm.verts.new((1, 1, 0))
    e1 = bm.edges.new((a, c))
    e2 = bm.edges.new((c, b))
    loop = bm.edges.new((c, d))
    # Source edge was selected: bisect kept ONE half's flag (e1), endpoints selected.
    a.select_set(True)
    b.select_set(True)
    e1.select_set(True)
    cutting.repair_split_selection([c, loop])  # geom_cut = cut vert + cut-loop edge
    both = e1.select and e2.select
    bm.free()
    return both


def overfire_case():
    """The segment BETWEEN two accumulated cut loops (endpoints selected, edge
    NOT) is cut across -> the two halves must NOT become selected."""
    bm = bmesh.new()
    gA = bm.verts.new((0, 0, 0))
    gB = bm.verts.new((2, 0, 0))
    gC = bm.verts.new((1, 0, 0))
    gD = bm.verts.new((1, 1, 0))
    sa = bm.edges.new((gA, gC))
    sc = bm.edges.new((gC, gB))
    loop = bm.edges.new((gC, gD))
    # Accumulated cut loops selected their VERTS (gA, gB) but not this segment.
    gA.select_set(True)
    gB.select_set(True)
    cutting.repair_split_selection([gC, loop])
    leaked = sa.select or sc.select
    bm.free()
    return leaked


check("genuine: cut across a SELECTED edge keeps both halves selected", genuine_case())
check("no over-fire: segment between accumulated cut loops stays UNselected",
      not overfire_case())

print("\nREPAIR SELECTION:", "OK" if not failures else f"FAIL ({len(failures)}): {failures}")
sys.exit(1 if failures else 0)
