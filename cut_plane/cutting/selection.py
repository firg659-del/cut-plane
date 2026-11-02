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


def face_select_mode(bm) -> bool:
    """Face flags are the selection's truth only in pure Face select mode."""
    mode = bm.select_mode
    return 'VERT' not in mode and 'EDGE' not in mode


class SplitPlan:
    """What the selection repair must know about the UNCUT mesh, gathered
    before a bisect (the bisect itself loses it - in Vertex select mode its
    own flush drops every split face's selection, measured):

      points    where the plane crosses each SELECTED edge - the new cut
                vertex there must be selected (KDTree, or None);
      existing  edges that already lay ON the plane, as position pairs - the
                bisect reports them in geom_cut, but they split no face, so
                the sibling rule must leave their faces alone;
      face_mode Face select mode: face flags are the truth; otherwise the
                verts are, and Blender's select-mode flush derives the rest.
    """
    __slots__ = ('points', 'existing', 'face_mode', 'tol')

    def __init__(self, points, existing, face_mode, tol):
        self.points, self.existing, self.face_mode, self.tol = points, existing, face_mode, tol


def _edge_key(a, b):
    a, b = tuple(float(c) for c in a), tuple(float(c) for c in b)
    return (a, b) if a <= b else (b, a)


def _plan(crossings, existing, face_mode, scale):
    points = None
    if crossings:
        points = KDTree(len(crossings))
        for i, co in enumerate(crossings):
            points.insert(co, i)
        points.balance()
    return SplitPlan(points, existing, face_mode, max(scale, 1.0) * 1e-5)


def split_plan_from_mesh(me, plane_co, plane_no, dist, face_mode, arrays=None):
    """SplitPlan from a Mesh in sync with the bmesh about to be cut (the
    preview's snapshot, Execute's synced edit mesh) - vectorised: the
    per-move preview cannot afford a Python walk over every edge.
    `arrays` caches (positions, edge vertex pairs) for an unchanging mesh."""
    import numpy as np
    if arrays is None:
        pos = np.empty(len(me.vertices) * 3, np.float32)
        me.vertices.foreach_get('co', pos)
        # intc, not int32: on Windows int32's buffer format is 'l', which
        # foreach_get walks item by item - 100x slower (see clip._LAYOUT).
        ev = np.empty(len(me.edges) * 2, np.intc)
        me.edges.foreach_get('vertices', ev)
        arrays = (pos.reshape(-1, 3).astype(np.float64), ev.reshape(-1, 2))
    pos, ev = arrays
    # The '.select_edge' attribute reads as one array; MeshEdge.select goes
    # through RNA per edge (41 ms at 250k edges, measured). The attribute is
    # missing when nothing is selected - and on a mesh just synced from Edit
    # Mode (Execute's), whose flags only RNA reads: fall back there.
    sel = np.zeros(len(me.edges), bool)
    attr = me.attributes.get('.select_edge')
    if attr is not None:
        attr.data.foreach_get('value', sel)
    else:
        me.edges.foreach_get('select', sel)
    no = np.array(plane_no, np.float64)
    d = pos @ no - float(np.dot(np.array(plane_co, np.float64), no))
    a, b = d[ev[:, 0]], d[ev[:, 1]]
    crossing = ((a > dist) & (b < -dist)) | ((a < -dist) & (b > dist))
    pick = crossing & sel
    t = (a[pick] / (a[pick] - b[pick]))[:, None]
    pa, pb = pos[ev[pick, 0]], pos[ev[pick, 1]]
    crossings = (pa + t * (pb - pa)).tolist()
    flat = (np.abs(a) <= dist) & (np.abs(b) <= dist)
    p32 = pos.astype(np.float32)
    existing = {_edge_key(p32[i], p32[j]) for i, j in ev[flat].tolist()}
    scale = float(np.abs(pos).max()) if len(pos) else 1.0
    return _plan(crossings, existing, face_mode, scale), arrays


def split_plan_from_bmesh(bm, plane_co, plane_no, dist, face_mode):
    """SplitPlan straight from a bmesh (a later mirrored plane, where no
    Mesh mirrors the bmesh any more). O(edges) in Python - mirrors only."""
    def side(v):
        return (v.co - plane_co).dot(plane_no)

    crossings, existing, scale = [], set(), 1.0
    for e in bm.edges:
        a, b = side(e.verts[0]), side(e.verts[1])
        if abs(a) <= dist and abs(b) <= dist:
            existing.add(_edge_key(e.verts[0].co, e.verts[1].co))
        elif e.select and ((a > dist and b < -dist) or (a < -dist and b > dist)):
            co_a, co_b = e.verts[0].co, e.verts[1].co
            crossings.append(co_a + (co_b - co_a) * (a / (a - b)))
            scale = max(scale, co_a.length)
    return _plan(crossings, existing, face_mode, scale)


def _inside_point(face):
    """A point ON the face, whatever its shape: a hair inside the middle of
    its first edge. The median centre lies outside a concave face (an
    L-shaped n-gon), and any interior point of a twisted quad lies off the
    baseline's triangulation if that split the quad along the other
    diagonal (up to 3x the lookup tolerance on Suzanne) - in both cases the
    lookup missed and a selection made during the preview was lost. The
    edges are common to every triangulation, so next to one the surface is
    the same whichever diagonal was chosen."""
    loop = face.loops[0]
    a, b = loop.vert.co, loop.link_loop_next.vert.co
    edge = b - a
    inward = face.normal.cross(edge)      # left of a counter-clockwise edge
    if inward.length_squared == 0.0:
        return face.calc_center_median()
    return (a + b) * 0.5 + inward.normalized() * (edge.length * 0.01)


def _source_face(face, bvh, snap, eps, exact):
    """Index of the baseline face a preview face came from (itself, or the
    face it is a split half of), or None. Several baseline faces can lie
    within reach - coincident or touching parts (Suzanne's eyes) - and the
    nearest one is then a coin toss: the one sharing the most vertex
    positions wins, since a face's original corners never move."""
    point = _inside_point(face)
    near = bvh.find_nearest_range(point, eps)
    if not near:
        return None
    if len(near) == 1:
        return near[0][2]
    corners = [v.co for v in face.verts]

    def shared(index):
        cos = [snap.vertices[i].co for i in snap.polygons[index].vertices]
        return sum(1 for c in corners if any((c - o).length <= exact for o in cos))

    return max(near, key=lambda hit: (shared(hit[2]), -hit[3]))[2]


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
        return preserve_hidden and hidden_raw(co)

    def hidden_raw(co: Vector) -> bool:
        if not planes or not (clear_inner or clear_outer):
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

    face_mode = face_select_mode(bm)
    exact = max((hi - lo).length * 1e-6, 1e-7)
    sel_faces = set()
    for f in bm.faces:
        if f.select:
            source = _source_face(f, bvh, snap, eps, exact)
            if source is not None:
                sel_faces.add(source)

    sel_verts = set()
    if not face_mode:
        # An original vertex is where it always was; a cut-loop vertex only
        # NEAR one (dense mesh, plane on a vertex row) must not stand in for
        # it - it left a stray column selected after leaving the tool.
        kd = KDTree(len(snap.vertices))
        for i, v in enumerate(snap.vertices):
            kd.insert(v.co, i)
        kd.balance()
        for v in bm.verts:
            if v.select:
                _co, i, dist = kd.find(v.co)
                if i is not None and dist <= exact:
                    sel_verts.add(i)

    # What the preview hides (a Clear's side) the user cannot have changed:
    # keep it while they ADDED to or trimmed the selection they could see
    # (Shift+click, Ctrl+click: at least half of it is still selected) -
    # not after a replace (plain click) or Deselect All, which drop most of
    # it. Folds can merge several edits, so the intent is judged on the
    # outcome, not on one click.
    if selected_only:
        preserve_hidden = bool(sel_faces or sel_verts)
    else:
        visible_before = [p.index for p in snap.polygons
                          if p.select and not hidden_raw(Vector(p.center))]
        kept = sum(1 for i in visible_before if i in sel_faces)
        preserve_hidden = not visible_before or kept * 2 >= len(visible_before)

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
    if face_mode:
        # Face mode: edges come from the selected faces only - "both verts
        # selected" also picked edges no selected face owns.
        face_edges = {k for p in snap.polygons if p.select for k in p.edge_keys}
    for e in snap.edges:
        if face_mode:
            e.select = e.key in face_edges
        else:
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
    # Edges of the cleared faces: after the delete, any of them still used
    # only by HIDDEN faces (outside a visible-only domain) is re-hidden
    # below, so a protected hidden part stays whole AND hidden instead of
    # leaving stray visible boundary edges floating on the cleared side.
    border = {e for f in doomed for e in f.edges}
    if doomed:
        bmesh.ops.delete(bm, geom=doomed, context='FACES_ONLY')
    doomed = [e for e in edges
              if e.is_valid and not e.link_faces
              and cleared(dist((e.verts[0].co + e.verts[1].co) * 0.5))]
    # context='EDGES' also deletes every vertex left without edges - which
    # took the ON-PLANE vertex of a plane that only touches a vertex (a
    # snapped cube corner, a sphere pole), against the rule above. Execute
    # then selected the freed vertex and raised, leaving the edit mesh
    # EMPTY; native Bisect and the preview keep that vertex. Edges touching
    # an on-plane vertex are removed one by one (edge only, verts live on),
    # the rest in bulk; step 3 collects the isolated cleared verts.
    touching = [e for e in doomed
                if any(abs(dist(v.co)) <= eps for v in e.verts)]
    if touching:
        keep = set(touching)
        doomed = [e for e in doomed if e not in keep]
    if doomed:
        bmesh.ops.delete(bm, geom=doomed, context='EDGES')
    for e in touching:
        if e.is_valid:
            bm.edges.remove(e)
    doomed = [v for v in verts
              if v.is_valid and not v.link_faces and not v.link_edges
              and cleared(dist(v.co))]
    if doomed:
        bmesh.ops.delete(bm, geom=doomed, context='VERTS')
    _rehide_orphaned_border(border)


def _rehide_orphaned_border(edges) -> None:
    """Blender's hide invariant around a face-granular clear: an element used
    only by hidden faces is itself hidden (and unselected). Without it the
    hidden part's former border with the cleared visible faces stays visible.
    Touches only the given edges and their verts - O(cleared border)."""
    verts = set()
    for e in edges:
        if not e.is_valid or e.hide:
            continue
        faces = e.link_faces
        if faces and all(f.hide for f in faces):
            e.select = False
            e.hide = True
            verts.update(e.verts)
    for v in verts:
        if v.is_valid and not v.hide and v.link_edges and all(e.hide for e in v.link_edges):
            v.select = False
            v.hide = True


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


def repair_split_selection(bm, geom_cut, plan, bvh=None, snap_polygons=None,
                           flush: bool = True) -> None:
    """Keep the user's selection across the split (shared by both paths).

    bisect_plane leaves the new halves of split faces and edges unselected,
    and in Vertex select mode its flush then drops the selection of every
    split face. The selection is rebuilt from the uncut mesh (`plan`):

      * a NEW cut vertex is selected exactly when the edge it split was;
      * Face mode: the two faces on a new cut edge are halves of ONE source
        face, so if they disagree the source was selected - select both.
        With a Clear the bisect may delete the selected half and keep a new,
        unselected one: then the snapshot BVH (bvh + snap_polygons) recovers
        the source face's flag. Edges that already lay on the plane split no
        face and are skipped (they once selected the whole ring below a
        selected hemisphere);
      * Vertex/Edge mode: the verts are the truth - Blender's select-mode
        flush derives edges and faces, as for any edit.

    O(loop length) plus one flush - flush=False leaves that to a caller
    that flushes again before anything reads the edge and face flags.
    """
    cut_edges = {g for g in geom_cut if isinstance(g, bmesh.types.BMEdge)}
    if plan.points is not None:
        for g in geom_cut:
            if not isinstance(g, bmesh.types.BMVert) or not g.is_valid:
                continue
            _co, index, dist = plan.points.find(g.co)
            if index is None or dist > plan.tol:
                continue
            g.select = True
            for e in g.link_edges:
                if e not in cut_edges:
                    e.select = True
    if not plan.face_mode:
        if flush:
            bm.select_flush_mode()
        return
    new_edges = [e for e in cut_edges if e.is_valid
                 and _edge_key(e.verts[0].co, e.verts[1].co) not in plan.existing]
    for e in new_edges:
        faces = e.link_faces
        if len(faces) == 2 and faces[0].select != faces[1].select:
            faces[0].select_set(True)
            faces[1].select_set(True)
    if bvh is not None and snap_polygons is not None:
        for e in new_edges:
            faces = e.link_faces
            if len(faces) == 1 and not faces[0].select:
                hit = bvh.find_nearest(faces[0].calc_center_median())
                if (hit is not None and hit[2] is not None
                        and snap_polygons[hit[2]].select):
                    faces[0].select_set(True)
    # A selected face along the cut selects its new edges too: when the
    # bisect keeps the ORIGINAL face for the selected half, nothing above
    # touched it, and its new cut edge stayed unselected.
    for e in cut_edges:
        if e.is_valid:
            for f in e.link_faces:
                if f.select:
                    f.select_set(True)


def apply_cut_selection(geom_cut, *, select: bool, bm=None) -> None:
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
    # A Clear that runs after the bisect (Execute, Selected Only, meshes
    # with hidden parts) can free elements of geom_cut - never touch those.
    selected_any = False
    for g in geom_cut:
        if isinstance(g, bmesh.types.BMEdge) and g.is_valid:
            g.select_set(True)
            selected_any = True
    if not selected_any:
        # Degenerate cut (plane grazes verts only) - select the cut verts
        for g in geom_cut:
            if isinstance(g, bmesh.types.BMVert) and g.is_valid:
                g.select_set(True)
    if bm is not None and not face_select_mode(bm):
        # Vertex/Edge mode: a face whose verts are all selected IS selected
        # there (a fill cap ringed by the loop) - Blender's own flush.
        bm.select_flush_mode()
