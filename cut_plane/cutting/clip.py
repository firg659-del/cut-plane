# SPDX-License-Identifier: GPL-3.0-or-later
"""Preview fast path: the snapshot rebuilt already cut by one plane.

The exact re-cut rebuilds the whole edit bmesh from the snapshot, builds a
Python list of every element, and runs bmesh.ops.bisect_plane - and every
bmesh operator allocates tool flags for the WHOLE mesh before it reads its
input (~110 ms at 126k faces, measured, even with an empty geom). Together
~220 ms per move at 126k faces: the drag stayed fluid (pacing) but the cut
trailed it.

Here numpy classifies the snapshot against the plane, a scratch Mesh
receives only what can survive (with a Clear, the cleared side is never
loaded at all), one from_mesh loads it, and only the edges and faces the
plane crosses are split in Python - with the primitives bisect_plane itself
uses (BM_edge_split at the plane's factor from the edge's first vert, then
BM_face_split between the two plane points), so the mesh is the same mesh.

Exact or nothing: whatever this path cannot reproduce returns None BEFORE
the bmesh is touched, and the caller runs the exact path:
  * a face the plane crosses more than once (concave n-gons) - bisect
    pairs those points along the plane, this path does not;
  * data a scratch Mesh would not carry - shape keys, vertex groups, an
    attribute type outside _LAYOUT (byte colours: their float round trip
    is not guaranteed exact). The preview mesh can become real: a foreign
    edit adopts it as the new baseline, so a dropped layer would be lost.
"""
from typing import Optional

import bmesh
import bpy

# attribute data_type -> (foreach key, values per element, numpy dtype name).
# 'intc', never 'int32': on Windows numpy's int32 reports the buffer format
# 'l', which foreach_set does not take as a raw buffer - it falls back to a
# per-item sequence walk, 100x slower (27.6 ms vs 0.2 ms for 600k values,
# measured). intc is C int, format 'i', on every platform.
_LAYOUT = {
    'FLOAT': ('value', 1, 'float32'),
    'INT': ('value', 1, 'intc'),
    'INT8': ('value', 1, 'int8'),
    'BOOLEAN': ('value', 1, 'bool'),
    'FLOAT2': ('vector', 2, 'float32'),
    'FLOAT_VECTOR': ('vector', 3, 'float32'),
    'FLOAT_COLOR': ('color', 4, 'float32'),
    'INT32_2D': ('value', 2, 'intc'),
    'QUATERNION': ('value', 4, 'float32'),
}
# Written by the topology arrays themselves.
_TOPOLOGY = frozenset(('position', '.edge_verts', '.corner_vert', '.corner_edge'))
# The selection remap writes these INTO the live snapshot between moves, so
# they are read per cut - never cached with the static arrays.
_SELECTION = frozenset(('.select_vert', '.select_edge', '.select_poly'))
_DOMAINS = ('POINT', 'EDGE', 'FACE', 'CORNER')

SCRATCH_NAME = "__cut_plane_clip_scratch__"


class ClipArrays:
    """The snapshot as flat arrays - static while the snapshot is."""
    __slots__ = ('pos', 'ev', 'starts', 'totals', 'lv', 'le', 'face_of_loop',
                 'attrs', 'uv_active', 'uv_render', 'color_active', 'color_default')


def _read(np, data, key, width, dtype, count):
    buf = np.empty(count * width, dtype)
    data.foreach_get(key, buf)
    return buf.reshape(count, width) if width > 1 else buf


def clip_arrays(obj, snap) -> Optional[ClipArrays]:
    """ClipArrays for `snap`, or None when a scratch Mesh could not carry
    everything it holds (see the module docstring) - the exact path then
    serves the whole session."""
    import numpy as np
    if snap.shape_keys is not None or len(obj.vertex_groups):
        return None
    nv, ne, nf, nl = (len(snap.vertices), len(snap.edges),
                      len(snap.polygons), len(snap.loops))
    attrs = []
    for a in snap.attributes:
        if a.name in _TOPOLOGY:
            continue
        if a.data_type not in _LAYOUT or a.domain not in _DOMAINS:
            return None
        if a.name in _SELECTION:
            continue
        key, width, dtype = _LAYOUT[a.data_type]
        attrs.append((a.name, a.data_type, a.domain,
                      _read(np, a.data, key, width, dtype, len(a.data))))
    arr = ClipArrays()
    pos = _read(np, snap.vertices, 'co', 3, 'float32', nv)
    arr.pos = pos
    arr.ev = _read(np, snap.edges, 'vertices', 2, 'intc', ne)
    arr.starts = _read(np, snap.polygons, 'loop_start', 1, 'intc', nf)
    arr.lv = _read(np, snap.loops, 'vertex_index', 1, 'intc', nl)
    arr.le = _read(np, snap.loops, 'edge_index', 1, 'intc', nl)
    arr.totals = np.diff(np.append(arr.starts, nl)).astype(np.intc)
    arr.face_of_loop = np.repeat(np.arange(nf), arr.totals)
    arr.attrs = attrs
    uv = snap.uv_layers
    arr.uv_active = uv.active.name if uv.active is not None else None
    arr.uv_render = next((u.name for u in uv if u.active_render), None)
    ca = snap.color_attributes
    arr.color_active = ca.active_color_name or None
    arr.color_default = ca.default_color_name or None
    return arr


def _scratch(cache):
    """The session's scratch Mesh, emptied. Reused across moves: removing a
    datablock tags the depsgraph relations for a rebuild, every move."""
    me = getattr(cache, 'clip_scratch', None)
    try:
        if me is not None:
            me.clear_geometry()
            return me
    except ReferenceError:
        pass                      # removed behind our back (file load, undo)
    me = bpy.data.meshes.new(SCRATCH_NAME)
    cache.clip_scratch = me
    return me


def free_scratch(cache) -> None:
    me = getattr(cache, 'clip_scratch', None)
    cache.clip_scratch = None
    if me is None:
        return
    try:
        bpy.data.meshes.remove(me)
    except (ReferenceError, RuntimeError):
        pass                      # already gone


def _closed_selection(np, snap, arr, nv, ne, nf) -> list:
    """The snapshot's selection as it is NOW (the remap writes it into the
    snapshot between moves), closed downward over the WHOLE mesh: faces
    select their edges, edges their verts.

    from_mesh applies face selection with the flushing setter, so a full
    rebuild selects every corner of a selected face. The scratch holds only
    part of the mesh: a vert on the plane whose selected face lies on the
    cleared side (never loaded) lost its selection - and the trailing
    flush then deselected the on-plane edge through it."""
    def read(name, count):
        a = snap.attributes.get(name)
        return (_read(np, a.data, 'value', 1, 'bool', count) if a is not None
                else np.zeros(count, bool))
    sel_f = read('.select_poly', nf)
    sel_e = read('.select_edge', ne)
    sel_v = read('.select_vert', nv)
    if nf and sel_f.any():
        sel_e[arr.le[sel_f[arr.face_of_loop]]] = True
    if ne and sel_e.any():
        sel_v[arr.ev[sel_e].ravel()] = True
    out = []
    for name, domain, sel in (('.select_vert', 'POINT', sel_v),
                              ('.select_edge', 'EDGE', sel_e),
                              ('.select_poly', 'FACE', sel_f)):
        if sel.any():
            out.append((name, 'BOOLEAN', domain, sel))
    return out


class _Load:
    """One planned cut: the scratch mesh's arrays and what to split, all in
    the scratch mesh's own indices."""
    __slots__ = ('pos', 'ev', 'lv', 'le', 'totals', 'attrs', 'split_e', 'fac',
                 'split_f', 'pairs', 'flat_e', 'doomed', 'on_plane')


def _split_pairs(np, tot, lv, le, on0, cross, vmap, crossing):
    """Each split face's two plane points, in the order its verts run once
    its edges are split (what bisect's face split sees): an on-plane
    corner as its loaded vert index, a split edge as -(k + 1) for the k-th
    split edge. Faces in split order; `tot` their sizes, the rest one entry
    per loop of them, in face order. A loop's vert and the edge after it
    cannot both be plane points, so loop order is already vert order."""
    point = on0 | cross
    ids = np.where(on0, vmap(lv), -(np.searchsorted(crossing, le) + 1))
    return ids[point].reshape(-1, 2)


def _plane32(np, plane_co, plane_no):
    """bisect_plane's own plane, float32: (n, -n.co). A vert's distance is
    dot(n, v) + plane[3], summed x, y, z in that order."""
    n = np.array(plane_no, np.float32)
    w = np.float32(-(n.astype(np.float64) @ np.array(plane_co, np.float64)))
    return n, w


def _project(pos, n):
    return (pos[:, 0] * n[0] + pos[:, 1] * n[1]) + pos[:, 2] * n[2]


def clip_cut(cache, bm, snap, arr: ClipArrays, plane_co, plane_no, dist,
             clear_inner: bool, clear_outer: bool, flush: bool = True, marker=None):
    """Rebuild the edit bmesh `bm` as `snap` cut by one plane (local space,
    unit normal), exactly as rebuild + bisect_plane(clear_inner,
    clear_outer) over the whole mesh would leave it.

    Returns bisect_plane's 'geom_cut': the verts and edges ON the plane,
    new and existing - or None, before touching bm, when only the exact
    path can produce this cut. flush=False skips bisect's trailing
    selection flush, for a caller that flushes again before anything
    reads the edge and face flags. `marker`: the name of an int face layer
    to create on the way in (the preview's tag, see snapshot.mark_preview):
    in the scratch mesh it is one array; added to the loaded bmesh it
    reallocates every face (2 ms a move at 126k faces, measured).
    """
    import numpy as np
    load = _plan_masked(np, snap, arr, plane_co, plane_no, np.float32(dist),
                        clear_inner, clear_outer)
    return None if load is None else _load_and_split(cache, bm, arr, load, flush, marker)


def _plan_masked(np, snap, arr, plane_co, plane_no, dist, clear_inner, clear_outer):
    nv, ne, nf = len(arr.pos), len(arr.ev), len(arr.starts)
    pos, ev, lv, le, starts = arr.pos, arr.ev, arr.lv, arr.le, arr.starts
    n, w = _plane32(np, plane_co, plane_no)
    d = _project(pos, n) + w
    side = np.zeros(nv, np.int8)
    side[d > dist] = 1
    side[d < -dist] = -1

    crossing_e = side[ev[:, 0]] * side[ev[:, 1]] == -1
    flat_e = (side[ev[:, 0]] == 0) & (side[ev[:, 1]] == 0)
    # A face's plane points: its on-plane corners plus its split edges.
    # bisect splits a face between exactly two of them unless they are
    # adjacent (an on-plane edge already joins them) - with or without a
    # split edge (a quad whose diagonal corners touch the plane is split
    # along that diagonal). More than two: bisect pairs them along the
    # plane (concave n-gons, a face lying in the plane) - exact path.
    if nf:
        points = np.add.reduceat((side[lv] == 0).astype(np.intc) + crossing_e[le], starts)
        if np.any(points > 2):
            return None
        has_flat = np.add.reduceat(flat_e[le].astype(np.intc), starts) > 0
        split_f = (points == 2) & ~has_flat
    else:
        split_f = np.zeros(0, bool)

    doomed_v = np.zeros(nv, bool)
    if clear_outer:
        doomed_v |= side == 1
    if clear_inner:
        doomed_v |= side == -1
    # Load what can survive the cut, plus what the splits need: every
    # split face whole (its cleared corners die after the split).
    if nf:
        f_doomed = np.maximum.reduceat(doomed_v[lv].astype(np.int8), starts) > 0
        load_f = ~f_doomed | split_f
    else:
        load_f = np.zeros(0, bool)
    load_l = load_f[arr.face_of_loop]
    load_e = ~(doomed_v[ev[:, 0]] | doomed_v[ev[:, 1]]) | crossing_e
    load_e[le[load_l]] = True
    load_v = ~doomed_v
    load_v[ev[load_e].ravel()] = True
    vmap = (np.cumsum(load_v) - 1).astype(np.intc)
    emap = (np.cumsum(load_e) - 1).astype(np.intc)
    fmap = (np.cumsum(load_f) - 1).astype(np.intc)

    masks = {'POINT': load_v, 'EDGE': load_e, 'FACE': load_f, 'CORNER': load_l}
    load = _Load()
    load.pos = pos[load_v]
    load.ev = vmap[ev[load_e]]
    load.lv = vmap[lv[load_l]]
    load.le = emap[le[load_l]]
    load.totals = arr.totals[load_f]
    load.attrs = [(name, data_type, domain, buf[masks[domain]])
                  for name, data_type, domain, buf
                  in arr.attrs + _closed_selection(np, snap, arr, nv, ne, nf)]
    load.split_e = emap[crossing_e]
    load.fac = d[ev[crossing_e, 0]] / (d[ev[crossing_e, 0]] - d[ev[crossing_e, 1]])
    load.split_f = fmap[split_f]
    split_idx = np.nonzero(split_f)[0]
    sl = _ranges(np, starts[split_idx], arr.totals[split_idx])
    load.pairs = _split_pairs(np, arr.totals[split_idx], lv[sl], le[sl], side[lv[sl]] == 0,
                              crossing_e[le[sl]], lambda i: vmap[i],
                              np.nonzero(crossing_e)[0])
    load.flat_e = emap[flat_e]
    load.doomed = vmap[load_v & doomed_v]
    load.on_plane = side[load_v] == 0
    return load


class ClipFrame:
    """The snapshot sorted along one plane normal, for a drag that only
    MOVES the plane (its normal fixed - a translate, a numeric offset).

    Verts in order of their projection on the normal, edges and faces in
    order of their largest corner projection, every array and attribute
    permuted to match, once per drag. A plane is then one offset, and
    everything a Clear keeps is a PREFIX of each array - copied whole,
    with no per-element work - plus the few faces the plane crosses.
    The per-move cut becomes proportional to the cut, not to the mesh.
    Built only while a transform owns the input, so the selection it
    carries cannot change under it."""
    __slots__ = ('n', 'proj', 'pos', 'ev', 'emax', 'emin', 'fmax', 'fmin',
                 'starts', 'totals', 'lv', 'le', 'attrs', 'sel_edge', 'scale')


def clip_frame(snap, arr: ClipArrays, plane_no) -> ClipFrame:
    import numpy as np
    nv, ne, nf, nl = len(arr.pos), len(arr.ev), len(arr.starts), len(arr.lv)
    fr = ClipFrame()
    fr.n = np.array(plane_no, np.float32)
    proj = _project(arr.pos, fr.n)
    vo = np.argsort(proj, kind='stable')
    vinv = np.empty(nv, np.intp)
    vinv[vo] = np.arange(nv)
    fr.proj = proj[vo]
    fr.pos = arr.pos[vo]

    ev0 = vinv[arr.ev]
    pa, pb = fr.proj[ev0[:, 0]], fr.proj[ev0[:, 1]]
    emax = np.maximum(pa, pb)
    eo = np.argsort(emax, kind='stable')
    einv = np.empty(ne, np.intp)
    einv[eo] = np.arange(ne)
    fr.ev = ev0[eo].astype(np.intc)
    fr.emax = emax[eo]
    fr.emin = np.minimum(pa, pb)[eo]

    if nf:
        lp = fr.proj[vinv[arr.lv]]
        fmax = np.maximum.reduceat(lp, arr.starts)
        fmin = np.minimum.reduceat(lp, arr.starts)
        fo = np.argsort(fmax, kind='stable')
    else:
        fmax = fmin = np.zeros(0, np.float32)
        fo = np.zeros(0, np.intp)
    totals = arr.totals[fo]
    starts = np.cumsum(totals) - totals
    lperm = np.repeat(arr.starts[fo] - starts, totals) + np.arange(nl)
    fr.totals = totals.astype(np.intc)
    fr.starts = starts.astype(np.intc)
    fr.fmax, fr.fmin = fmax[fo], fmin[fo]
    fr.lv = vinv[arr.lv[lperm]].astype(np.intc)
    fr.le = einv[arr.le[lperm]].astype(np.intc)
    perm = {'POINT': vo, 'EDGE': eo, 'FACE': fo, 'CORNER': lperm}
    fr.attrs = [(name, data_type, domain, buf[perm[domain]])
                for name, data_type, domain, buf
                in arr.attrs + _closed_selection(np, snap, arr, nv, ne, nf)]
    fr.sel_edge = next((buf for name, _t, _d, buf in fr.attrs
                        if name == '.select_edge'), None)
    fr.scale = float(np.abs(arr.pos).max()) if nv else 1.0
    return fr


def frame_split_plan(fr: ClipFrame, plane_co, dist, face_mode):
    """split_plan_from_mesh through a ClipFrame: the crossing and on-plane
    edges come straight from the sorted arrays instead of a pass over every
    edge. The distances are the cut's own float32 ones, so the plan agrees
    with the cut even for an edge within a hair of the threshold."""
    import numpy as np
    from .selection import _edge_key, _plan
    D = np.float32(dist)
    w = np.float32(-(fr.n.astype(np.float64) @ np.array(plane_co, np.float64)))
    d = fr.proj + w
    ed_max, ed_min = fr.emax + w, fr.emin + w
    hi = int(np.searchsorted(ed_max, D, 'right'))
    crossing = hi + np.nonzero(ed_min[hi:] < -D)[0]
    crossings = []
    if fr.sel_edge is not None and len(crossing):
        pick = crossing[fr.sel_edge[crossing]]
        a, b = fr.ev[pick, 0], fr.ev[pick, 1]
        da, db = d[a].astype(np.float64), d[b].astype(np.float64)
        pa, pb = fr.pos[a].astype(np.float64), fr.pos[b].astype(np.float64)
        crossings = (pa + (da / (da - db))[:, None] * (pb - pa)).tolist()
    lo = int(np.searchsorted(ed_max, -D, 'left'))
    flat = lo + np.nonzero(ed_min[lo:hi] >= -D)[0]
    existing = {_edge_key(fr.pos[i], fr.pos[j]) for i, j in fr.ev[flat].tolist()}
    return _plan(crossings, existing, face_mode, fr.scale)


def _ranges(np, starts, totals):
    """The loop indices of faces given by their starts and sizes."""
    first = np.cumsum(totals) - totals
    return np.repeat(starts - first, totals) + np.arange(int(totals.sum()))


def frame_cut(cache, bm, arr: ClipArrays, fr: ClipFrame, plane_co, dist, clear: bool,
              flush: bool = True, marker=None):
    """clip_cut through a ClipFrame: the plane is fr.n through plane_co,
    and `clear` drops its POSITIVE side (Clear Outer; Clear Inner is the
    same cut through the frame of the negated normal - bisect's float32
    distances simply change sign). Same result and refusals as clip_cut."""
    import numpy as np
    D = np.float32(dist)
    nv, ne, nf, nl = len(fr.pos), len(fr.ev), len(fr.starts), len(fr.lv)
    w = np.float32(-(fr.n.astype(np.float64) @ np.array(plane_co, np.float64)))
    d = fr.proj + w                  # adding w keeps the order: still sorted
    ed_max, ed_min = fr.emax + w, fr.emin + w
    fd_max, fd_min = fr.fmax + w, fr.fmin + w

    def side(values):
        return (values > D).astype(np.int8) - (values < -D)

    # The faces the plane can touch: corners on both sides of -D..D.
    lo = int(np.searchsorted(fd_max, -D, 'left'))
    cand = lo + np.nonzero(fd_min[lo:] <= D)[0]
    split = np.zeros(0, np.intp)
    if len(cand):
        tot = fr.totals[cand]
        cl = _ranges(np, fr.starts[cand], tot)
        first = (np.cumsum(tot) - tot)
        sv = side(d[fr.lv[cl]])
        ce = fr.ev[fr.le[cl]]
        s0, s1 = side(d[ce[:, 0]]), side(d[ce[:, 1]])
        points = np.add.reduceat((sv == 0).astype(np.intc) + (s0 * s1 == -1), first)
        if np.any(points > 2):
            return None
        has_flat = np.add.reduceat(((s0 == 0) & (s1 == 0)).astype(np.intc), first) > 0
        split = cand[(points == 2) & ~has_flat]
    hi = int(np.searchsorted(ed_max, D, 'right'))
    crossing = hi + np.nonzero(ed_min[hi:] < -D)[0]
    lo_e = int(np.searchsorted(ed_max, -D, 'left'))
    flat = lo_e + np.nonzero(ed_min[lo_e:hi] >= -D)[0]

    if clear:
        kv = int(np.searchsorted(d, D, 'right'))
        ke, kf = hi, int(np.searchsorted(fd_max, D, 'right'))
        xf = split[split >= kf]
        xl = _ranges(np, fr.starts[xf], fr.totals[xf])
        xe = fr.le[xl]
        xe = np.unique(np.concatenate((crossing, xe[xe >= ke])))
        xv = np.unique(fr.ev[xe].ravel())
        xv = xv[xv >= kv]
    else:
        kv, ke, kf = nv, ne, nf
        xf = xl = xe = xv = np.zeros(0, np.intp)
    kl = int(fr.starts[kf]) if kf < nf else nl

    def vmap(i):
        return np.where(i < kv, i, kv + np.searchsorted(xv, i)).astype(np.intc)

    def emap(i):
        return np.where(i < ke, i, ke + np.searchsorted(xe, i)).astype(np.intc)

    load = _Load()
    load.pos = np.concatenate((fr.pos[:kv], fr.pos[xv]))
    load.ev = np.concatenate((fr.ev[:ke], vmap(fr.ev[xe])))
    load.lv = np.concatenate((fr.lv[:kl], vmap(fr.lv[xl])))
    load.le = np.concatenate((fr.le[:kl], emap(fr.le[xl])))
    load.totals = np.concatenate((fr.totals[:kf], fr.totals[xf]))
    pick = {'POINT': (kv, xv), 'EDGE': (ke, xe), 'FACE': (kf, xf), 'CORNER': (kl, xl)}
    load.attrs = []
    for name, data_type, domain, buf in fr.attrs:
        k, extra = pick[domain]
        load.attrs.append((name, data_type, domain,
                           np.concatenate((buf[:k], buf[extra]))))
    load.split_e = emap(crossing)
    d0, d1 = d[fr.ev[crossing, 0]], d[fr.ev[crossing, 1]]
    load.fac = d0 / (d0 - d1)
    load.split_f = np.where(split < kf, split, kf + np.searchsorted(xf, split))
    load.flat_e = flat                       # on the plane: inside the prefix
    load.doomed = np.arange(kv, kv + len(xv))
    load.on_plane = np.concatenate((np.abs(d[:kv]) <= D, np.zeros(len(xv), bool)))
    sl = _ranges(np, fr.starts[split], fr.totals[split])
    slv, sle = fr.lv[sl], fr.le[sl]
    se = fr.ev[sle]
    load.pairs = _split_pairs(np, fr.totals[split], slv, sle, np.abs(d[slv]) <= D,
                              side(d[se[:, 0]]) * side(d[se[:, 1]]) == -1, vmap, crossing)
    return _load_and_split(cache, bm, arr, load, flush, marker)


def _load_and_split(cache, bm, arr, load, flush=True, marker=None):
    """Write a planned cut into the scratch mesh, load it into bm, and split
    what the plane crosses. Returns bisect_plane's 'geom_cut'."""
    import numpy as np
    me = _scratch(cache)
    me.vertices.add(len(load.pos))
    me.edges.add(len(load.ev))
    me.loops.add(len(load.lv))
    me.polygons.add(len(load.totals))
    # An empty domain has no layer to write (everything cleared).
    if len(me.vertices):
        me.attributes['position'].data.foreach_set('vector', load.pos.ravel())
    if len(me.edges):
        me.attributes['.edge_verts'].data.foreach_set(
            'value', load.ev.astype(np.intc, copy=False).ravel())
    if len(me.polygons):
        me.attributes['.corner_vert'].data.foreach_set(
            'value', load.lv.astype(np.intc, copy=False))
        me.attributes['.corner_edge'].data.foreach_set(
            'value', load.le.astype(np.intc, copy=False))
        me.polygons.foreach_set(
            'loop_start', (np.cumsum(load.totals) - load.totals).astype(np.intc))
    # vertices/edges/polygons.add() make SELECTED elements: a layer the
    # snapshot lacks (nothing selected) must not survive on the scratch.
    wanted = {a[0] for a in load.attrs} | _TOPOLOGY | {marker}
    for name in [a.name for a in me.attributes if a.name not in wanted]:
        me.attributes.remove(me.attributes[name])
    if marker and me.attributes.get(marker) is None:
        me.attributes.new(marker, 'INT', 'FACE')
    for name, data_type, domain, buf in load.attrs:
        target = me.attributes.get(name) or me.attributes.new(name, data_type, domain)
        if len(buf):              # the layer exists even with nothing in it
            target.data.foreach_set(_LAYOUT[data_type][0], buf.ravel())
    if arr.uv_active and arr.uv_active in me.uv_layers:
        me.uv_layers.active = me.uv_layers[arr.uv_active]
    if arr.uv_render and arr.uv_render in me.uv_layers:
        me.uv_layers[arr.uv_render].active_render = True
    if arr.color_active:
        me.color_attributes.active_color_name = arr.color_active
    if arr.color_default:
        me.color_attributes.default_color_name = arr.color_default
    me.update()

    # Indices into the LOADED mesh, taken before anything is added.
    split_e = load.split_e.tolist()
    fac = load.fac.tolist()
    split_f = load.split_f.tolist()
    pairs = load.pairs.tolist()
    on_plane_e = load.flat_e.tolist()
    doomed = load.doomed.tolist()

    select_mode = set(bm.select_mode)
    bm.clear()
    bm.from_mesh(me)
    if select_mode:
        bm.select_mode = select_mode
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    verts, edges, faces = bm.verts, bm.edges, bm.faces
    split_edges = [edges[i] for i in split_e]
    split_faces = [faces[i] for i in split_f]
    cut_edges = [edges[i] for i in on_plane_e]
    doomed_verts = [verts[i] for i in doomed]
    # A pair's loaded verts, resolved while the lookup table is valid.
    pair_verts = [(verts[a] if a >= 0 else a, verts[b] if b >= 0 else b)
                  for a, b in pairs]

    # The very C calls bisect makes (BM_edge_split from e->v1, BM_face_split
    # with no example edge), so the flags they copy or leave are bisect's.
    edge_split = bmesh.utils.edge_split
    new_verts = [edge_split(e, e.verts[0], t)[1] for e, t in zip(split_edges, fac)]
    face_split = bmesh.utils.face_split
    for f, (a, b) in zip(split_faces, pair_verts):
        if isinstance(a, int):
            a = new_verts[-a - 1]
        if isinstance(b, int):
            b = new_verts[-b - 1]
        cut_edges.append(face_split(f, a, b)[1].edge)
    for v in doomed_verts:
        bm.verts.remove(v)
    # The Python bmesh.ops wrapper flushes the selection after EVERY
    # operator (in Vertex mode it selects an edge between two selected
    # verts even after a no-op bisect, measured) - so the exact path's
    # selection is bisect's plus this flush.
    if flush:
        bm.select_flush_mode()
    # bisect's geom_cut: the new verts, every edge lying in the plane, and
    # those edges' verts - an on-plane vert with no on-plane edge (a loose
    # one, a corner merely touching) is not part of it.
    seen = set(new_verts)
    cut_verts = list(new_verts)
    for e in cut_edges:
        for v in e.verts:
            if v not in seen:
                seen.add(v)
                cut_verts.append(v)
    return cut_verts + cut_edges
