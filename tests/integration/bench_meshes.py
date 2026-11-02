# SPDX-License-Identifier: GPL-3.0-or-later
"""Geometrically hard meshes shared by the Edit Mode benches.

Open boundaries, n-gons, a hole through the mesh, a holed grid, concave
faces, high-valence poles, loose parts, a non-manifold edge and several
materials - each carrying UVs and two materials. Each is built once and
copied: Blender's own primitives do not emit faces in the same order on
every call, so a rebuilt mesh is not the same mesh.
"""
import math

import bmesh
import bpy

ctx = bpy.context


def _active():
    return ctx.active_object


def suzanne():
    bpy.ops.mesh.primitive_monkey_add()
    return _active()


def ngon_cylinder():
    bpy.ops.mesh.primitive_cylinder_add(vertices=12, end_fill_type='NGON')
    return _active()


def torus():
    bpy.ops.mesh.primitive_torus_add(major_segments=24, minor_segments=10)
    return _active()


def holed_grid():
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=12, y_subdivisions=12, size=2)
    ob = _active()
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    for v in bm.verts:
        v.co.z = 0.3 * math.sin(v.co.x * 2.0) + 0.15 * v.co.y
    bm.faces.ensure_lookup_table()
    bmesh.ops.delete(bm, geom=[bm.faces[i] for i in (14, 15, 40, 41, 77, 100)], context='FACES')
    bm.to_mesh(ob.data)
    bm.free()
    return ob


def _from_pydata(name, verts, faces):
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    me.update()
    ob = bpy.data.objects.new(name, me)
    ctx.scene.collection.objects.link(ob)
    return ob


def concave_prism():
    outline = [(0, 0), (2, 0), (2, 0.6), (0.6, 0.6), (0.6, 2), (0, 2)]
    verts = [(x - 1, y - 1, z) for z in (-0.5, 0.5) for x, y in outline]
    n = len(outline)
    faces = [tuple(range(n))[::-1], tuple(range(n, 2 * n))]
    faces += [(i, (i + 1) % n, n + (i + 1) % n, n + i) for i in range(n)]
    return _from_pydata('Prism', verts, faces)


def dense_sphere():
    bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=24)
    return _active()


def loose_parts():
    bpy.ops.mesh.primitive_cube_add()
    ob = _active()
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    a = bm.verts.new((1.6, 0, 0.1))
    b = bm.verts.new((1.6, 0, -0.9))
    bm.edges.new((a, b))
    bm.verts.new((0, 1.7, 0.3))
    bm.to_mesh(ob.data)
    bm.free()
    return ob


def bowtie():
    """Two cubes sharing one edge: a non-manifold edge with four faces."""
    verts, faces = [], []
    q = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    for ox, oy in ((0, 0), (1, 1)):
        base = len(verts)
        verts += [(ox + x, oy + y, z - 0.5) for z in (0, 1) for y in (0, 1) for x in (0, 1)]
        faces += [tuple(base + i for i in f) for f in q]
    ob = _from_pydata('Bowtie', verts, faces)
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=1e-5)
    bm.to_mesh(ob.data)
    bm.free()
    return ob


def multi_material():
    bpy.ops.mesh.primitive_cube_add()
    ob = _active()
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=2, use_grid_fill=True)
    for i, e in enumerate(bm.edges):
        e.seam = i % 7 == 0
        e.smooth = i % 5 != 0
    bm.to_mesh(ob.data)
    bm.free()
    return ob


MESHES = (suzanne, ngon_cylinder, torus, holed_grid, concave_prism, dense_sphere,
          loose_parts, bowtie, multi_material)
_TEMPLATES = {}


def template(mesh_fn):
    if mesh_fn not in _TEMPLATES:
        if ctx.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        ob = mesh_fn()
        me = ob.data
        if not me.uv_layers:
            uv = me.uv_layers.new(name='UVMap')
            for loop in me.loops:
                co = me.vertices[loop.vertex_index].co
                uv.data[loop.index].uv = (co.x * 0.5 + 0.5, co.y * 0.5 + co.z * 0.25)
        for name in ('BenchA', 'BenchB'):
            me.materials.append(bpy.data.materials.get(name) or bpy.data.materials.new(name))
        for poly in me.polygons:
            poly.material_index = poly.index % 2
        me.use_fake_user = True
        bpy.data.objects.remove(ob, do_unlink=True)
        _TEMPLATES[mesh_fn] = me
    return _TEMPLATES[mesh_fn]
