# SPDX-License-Identifier: GPL-3.0-or-later
"""Selection semantics around a cut: baseline remap, face-granular Clear,
domain reselection, split repair, and the loop-select rule.

These are the pieces both cut paths share (live preview + Execute Cut), so
their behavior can never drift between the preview and the commit.
"""

import bmesh
from mathutils import Vector
from mathutils.kdtree import KDTree

from .snap_support import _get_or_build_snapshot_bvh
from .snapshot import _snapshot_bounds


def _remap_selection_to_baseline(cache, bm, planes=None,
                                 clear_inner=False, clear_outer=False,
                                 selected_only=False) -> None:
    """The user changed the selection ON THE PREVIEW mesh - its topology
    differs from the baseline, so flags can't be copied 1:1. Map the
    selection back by GEOMETRY and write it into the snapshot, so the
    imminent rebuild restores the mesh WITH the new selection instead of
    reverting to the old one (the stale-domain-after-reselect bug; in
    standard mode, the never-lose-a-deselection rule).

    planes/clear_inner/clear_outer describe the state the mesh in `bm` was
    PRODUCED with (cache.written_state) - the caller must never pass the
    incoming refresh's state, which is what silently eroded the domain.
    planes is the full object-local list of the producing apply, mirrors
    included: every one of them cleared a side, so every one shields its
    side.

    Faces: every preview face lies on its source baseline face's surface
    (split halves included - their verts sit on the original edges), so the
    snapshot BVH's nearest-face lookup recovers the source; fill caps sit in
    the mesh interior, far from the baseline surface, and the distance gate
    rejects them. Verts: matched by position (KDTree); cut-loop verts match
    nothing - correct, they are cut artifacts, their faces carry the intent.
    Edges: selected when both verts end up selected. Face selections flush
    down to their verts/edges, mirroring edit-mode flushing.

    CLEARED-side geometry is not representable in the preview - the user
    cannot have touched it - so its stored flags are PRESERVED rather than
    overwritten (without this, the domain would erode as the plane sweeps
    across it with a Clear on). EXCEPTION: a completely empty live selection
    is unambiguous deselect-all intent, so then the hidden flags are cleared
    too. Runs only on a detected selection change (never per-mousemove).
    """
    snap = cache.live_snapshot
    bvh = _get_or_build_snapshot_bvh(cache)
    if snap is None or bvh is None or not snap.vertices:
        return

    # Hidden-side preservation guards the SELECTED-ONLY domain against
    # erosion while the plane sweeps with a Clear on. It is deliberately
    # OFF in standard mode: there the selection is not the cut's input, and
    # preserving flags the user can never see or clear accumulates stale
    # invisible selections (e.g. a primitive's spawn-selected state
    # resurfacing when Clear is toggled off). Deselect-all intent is judged
    # on what MAPS to the baseline: cut artifacts (loop verts/edges, our
    # loop-select output) map to nothing, so a loop-only selection counts
    # as EMPTY. (preserve_hidden is assigned after the mapping below.)

    def hidden(co: Vector) -> bool:
        if not preserve_hidden or not planes \
                or not (clear_inner or clear_outer):
            return False
        for p_co, p_no in planes:
            d = (co - p_co).dot(p_no)
            if (clear_inner and d < -1e-5) or (clear_outer and d > 1e-5):
                return True
        return False

    # Distance gate relative to mesh size (absolute epsilons break on
    # miniature/huge meshes): a hit farther than this is not "on" a face.
    lo, hi = _snapshot_bounds(snap)
    eps = max((hi - lo).length * 1e-3, 1e-6)

    sel_faces = set()
    for f in bm.faces:
        if f.select:
            hit = bvh.find_nearest(f.calc_center_median())
            if hit is not None and hit[2] is not None and hit[3] <= eps:
                sel_faces.add(hit[2])

    kd = KDTree(len(snap.vertices))
    for i, v in enumerate(snap.vertices):
        kd.insert(v.co, i)
    kd.balance()
    sel_verts = set()
    for v in bm.verts:
        if v.select:
            _co, i, dist = kd.find(v.co)
            if i is not None and dist <= eps:
                sel_verts.add(i)

    preserve_hidden = selected_only and bool(sel_faces or sel_verts)

    n_faces = 0
    for p in snap.polygons:
        if hidden(Vector(p.center)):
            if p.select:  # preserved cleared-side selection
                sel_verts.update(p.vertices)
                n_faces += 1
            continue
        chosen = p.index in sel_faces
        p.select = chosen
        if chosen:
            sel_verts.update(p.vertices)
            n_faces += 1
    for i, v in enumerate(snap.vertices):
        if v.select and hidden(Vector(v.co)):
            sel_verts.add(i)  # preserved cleared-side selection
    n_edges = 0
    for e in snap.edges:
        e.select = (e.vertices[0] in sel_verts and e.vertices[1] in sel_verts)
        n_edges += e.select
    for i, v in enumerate(snap.vertices):
        v.select = i in sel_verts
    # The comparison basis must track what the snapshot now stores.
    cache.snapshot_sel_counts = (len(sel_verts), n_edges, n_faces)


def clear_domain_side(bm, res_geom, plane_co, plane_no,
                      clear_inner: bool, clear_outer: bool,
                      eps: float = 1e-5) -> None:
    """FACE-GRANULAR Clear for Selected-Only cuts.

    bisect_plane's own clear flags delete cleared-side VERTS, and deleting a
    vert kills EVERY face using it - including UNSELECTED neighbor faces that
    share the domain's boundary verts (clearing a selected patch blew holes
    in the surrounding mesh). So Selected-Only bisects WITHOUT clear flags
    and this deletes only the DOMAIN's own geometry on the cleared side(s):

      1. domain faces there (FACES_ONLY - shared boundary verts/edges live on
         for their unselected neighbors),
      2. domain edges left wire by step 1 (midpoint-classified),
      3. domain verts left fully isolated.

    On-plane elements (|distance| <= eps) are never touched: the cut loop
    survives, like native, and fill caps are created after this runs.
    res_geom is bisect_plane's 'geom' output = the domain incl. the cut's
    new elements; plane co/no in the same (object-local) space the bisect ran.
    """
    def cleared(d: float) -> bool:
        return (clear_inner and d < -eps) or (clear_outer and d > eps)

    def dist(co) -> float:
        return (co - plane_co).dot(plane_no)

    faces, edges, verts = [], [], []
    for elem in res_geom:
        if isinstance(elem, bmesh.types.BMFace):
            faces.append(elem)
        elif isinstance(elem, bmesh.types.BMEdge):
            edges.append(elem)
        else:
            verts.append(elem)

    doomed = [f for f in faces
              if f.is_valid and cleared(dist(f.calc_center_median()))]
    if doomed:
        bmesh.ops.delete(bm, geom=doomed, context='FACES_ONLY')
    doomed = [e for e in edges
              if e.is_valid and not e.link_faces
              and cleared(dist((e.verts[0].co + e.verts[1].co) * 0.5))]
    if doomed:
        bmesh.ops.delete(bm, geom=doomed, context='EDGES')
    doomed = [v for v in verts
              if v.is_valid and not v.link_faces and not v.link_edges
              and cleared(dist(v.co))]
    if doomed:
        bmesh.ops.delete(bm, geom=doomed, context='VERTS')


def reselect_cut_domain(*geom_lists) -> None:
    """Selected Only: keep the whole surviving domain selected after a cut.

    bisect_plane's output 'geom' holds the input geometry PLUS the elements
    the cut created (loop verts, split halves) - without re-selecting those,
    the domain develops unselected holes and stops working as the input for
    the NEXT cut on the same region. Deliberately beyond native (native
    bisect leaves only the loop selected - it's a one-shot operator, this is
    a persistent tool built for repeated cuts). Cleared-side elements are
    freed by the bisect - the is_valid guard skips them. Only ADDS selection,
    so unselected geometry stays exactly as it was.
    """
    for geom in geom_lists:
        for elem in geom:
            if elem.is_valid:
                elem.select = True


def repair_split_selection(geom_cut, bvh=None, snap_polygons=None) -> None:
    """Make face/edge splits SELECTION-PRESERVING along the cut loop.

    bisect_plane does not copy the select flag to the NEW half of a split
    face/edge, so cutting through a selected region left a half-deselected
    band along the loop. Repair by sibling agreement:

      * the two faces sharing a CUT EDGE are halves of ONE source face (a
        cut edge lies inside its source face) - if they disagree, the source
        was selected (an unselected source leaves both halves unselected),
        so select both;
      * a CUT VERT splits one source edge - select it (and the sub-edges)
        when its non-loop neighbors are all selected, i.e. the region around
        the split was selected.

    ONE-SIDED CASE (bvh + snap_polygons given): with Clear Inner/Outer the
    bisect deletes one half INTERNALLY, and it reuses the original face for
    an ARBITRARY side - when the original fell on the cleared side, the
    kept half is a brand-new unselected face with no sibling left to
    compare (the sometimes-deselects-under-Clear bug: ~40% of a selected
    band lost, whichever halves happened to be new). The kept half's center
    lies ON its source face, so the snapshot BVH recovers the source's
    stored selection.

    O(loop length), not O(mesh). Selected-Only mode doesn't need this - the
    whole surviving domain is re-selected by reselect_cut_domain.
    """
    cut_edges = {g for g in geom_cut if isinstance(g, bmesh.types.BMEdge)}
    for e in cut_edges:
        if not e.is_valid:
            continue
        faces = e.link_faces
        if len(faces) == 2 and faces[0].select != faces[1].select:
            faces[0].select_set(True)
            faces[1].select_set(True)
    if bvh is not None and snap_polygons is not None:
        for e in cut_edges:
            if not e.is_valid:
                continue
            faces = e.link_faces
            if len(faces) == 1 and not faces[0].select:
                hit = bvh.find_nearest(faces[0].calc_center_median())
                if (hit is not None and hit[2] is not None
                        and snap_polygons[hit[2]].select):
                    faces[0].select_set(True)
    for g in geom_cut:
        if not isinstance(g, bmesh.types.BMVert) or not g.is_valid:
            continue
        # A cut vert splits a source edge into halves; bisect keeps the source's
        # select flag on ONE half. SIBLING agreement: select the other half (and
        # the vert) only when a half is ALREADY selected - i.e. the source edge
        # was selected. Keying off the halves, NOT off the neighbour verts being
        # selected, is what stops the segment BETWEEN two accumulated cut loops
        # from selecting: its endpoints are selected cut-verts, but the segment
        # itself was never a selected source edge (that was the over-select bug).
        split_edges = [e for e in g.link_edges if e not in cut_edges]
        if any(e.select for e in split_edges):
            g.select_set(True)
            for e in split_edges:
                e.select_set(True)


def apply_cut_selection(geom_cut, *, select: bool) -> None:
    """ADD the freshly cut loop to the selection as EDGES ONLY (shared by
    live preview + Execute). select=False leaves the selection untouched.

    NEVER deselects - the smooth-experience rule, both modes: the tool only
    ever ADDS to the user's selection, and user (de)selections are preserved
    in the baseline (adopted/remapped by _apply_live_cut). Loops therefore
    accumulate across committed cuts by default - the old Keep-All-Cuts
    toggle described exactly this and was removed as redundant; to shed
    selection, just deselect (mid-preview deselections stick now).

    Uses BMEdge.select_set, which selects each cut edge and its two verts but
    never the FACES between cut edges. A plain upward select_flush would
    instead fill in any face whose edges/verts are all selected (verified: it
    selects a fully-bounded face), flooding accumulated cuts with unwanted
    faces.
    """
    if not select:
        return
    selected_any = False
    for g in geom_cut:
        if isinstance(g, bmesh.types.BMEdge):
            g.select_set(True)
            selected_any = True
    if not selected_any:
        # Degenerate cut (plane grazes verts only) - select the cut verts
        for g in geom_cut:
            if isinstance(g, bmesh.types.BMVert):
                g.select_set(True)
