# SPDX-License-Identifier: GPL-3.0-or-later
"""A plane that only TOUCHES vertices, with a Clear, cuts like native Bisect.

    blender --background --factory-startup --python-exit-code 1 \
            --python tests/integration/vertex_touch.py

Vertex snapping makes this ordinary: snap the plane to a cube corner or a
sphere pole, Clear the side holding the mesh, press Enter. In 1.1.0 the
face-granular clear deleted the wire edges with context='EDGES', which also
took the on-plane vertex; selecting the loop then hit the freed vertex,
Execute Cut raised a ReferenceError and left the edit mesh EMPTY (0/0/0).
Native Bisect and the live preview keep that vertex (1/0/0).
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
from cut_plane.lib.math_utils import matrix_to_flat  # noqa: E402

FAILURES = []
ctx = bpy.context


def check(name, condition, detail=''):
    print(f"[{'ok' if condition else 'FAIL'}] {name}" + (f' ({detail})' if detail else ''),
          flush=True)
    if not condition:
        FAILURES.append(name)


def build(kind):
    cutting.restore_live_cut(ctx)
    if ctx.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    if kind == 'sphere':
        bpy.ops.mesh.primitive_uv_sphere_add(segments=12, ring_count=6, radius=1)
    else:
        bpy.ops.mesh.primitive_cube_add(size=2)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='DESELECT')
    return ctx.active_object


def counts(obj):
    bm = bmesh.from_edit_mesh(obj.data)
    return len(bm.verts), len(bm.edges), len(bm.faces)


def pose(co, no):
    sc = ctx.window_manager.cut_plane
    # FLOATS: an int tuple written through the ID property becomes an int
    # array, which the float-vector RNA property silently ignores (the plane
    # then sits at the default centre - measured, it masked this whole test).
    sc['center_offset'] = tuple(float(c) for c in co)
    sc['x_offset'] = sc['y_offset'] = sc['z_offset'] = 0.0
    rot = Vector((0, 0, 1)).rotation_difference(Vector(no)).to_matrix().to_4x4()
    sc['rotation_matrix_flat'] = matrix_to_flat(rot)


CASES = {
    # (mesh, plane point, plane normal): the plane touches only vertices and
    # Clear Inner takes the whole mesh except the touched vertex.
    'cube corner': ('cube', (1, 1, 1), Vector((1, 1, 1)).normalized()),
    'sphere pole': ('sphere', (0, 0, 1), Vector((0, 0, 1))),
    # Control: the plane 0.1 inside the corner makes an ordinary cut.
    'control (plane inside the corner)': ('cube', (0.9, 0.9, 0.9), Vector((1, 1, 1)).normalized()),
}


def main():
    cut_plane.register()
    sc = ctx.window_manager.cut_plane
    try:
        for name, (kind, co, no) in CASES.items():
            for fill in (False, True):
                label = f'{name}{" + Fill" if fill else ""}'
                obj = build(kind)
                bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.mesh.bisect(plane_co=co, plane_no=no, clear_inner=True, use_fill=fill)
                native = counts(obj)

                obj = build(kind)
                sc['cut_clear_inner'], sc['cut_clear_outer'], sc['cut_use_fill'] = True, False, fill
                sc['cut_selected_only'] = False
                sc['cut_select_loop'] = True                 # the default - it hit the freed vertex
                pose(co, no)
                cutting.update_live_preview(ctx)
                preview = counts(obj)
                cutting.restore_live_cut(ctx)

                obj = build(kind)
                try:
                    status = bpy.ops.view3d.cut_plane_cut(
                        'EXEC_DEFAULT', plane_co=co, plane_no=no, clear_inner=True,
                        clear_outer=False, use_fill=fill, select_loop=True)
                    raised = None
                except Exception as error:      # 1.1.0: ReferenceError
                    status, raised = None, error
                commit = counts(obj)
                check(f'{label}: Execute does not raise', raised is None, repr(raised))
                check(f'{label}: commit matches native Bisect', commit == native,
                      f'native {native} commit {commit} ({status})')
                check(f'{label}: preview matches native Bisect', preview == native,
                      f'native {native} preview {preview}')
    finally:
        cutting.restore_live_cut(ctx)
        if ctx.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        cut_plane.unregister()
    if FAILURES:
        print(f'VERTEX TOUCH: {len(FAILURES)} FAILED', flush=True)
        os._exit(1)
    print('VERTEX TOUCH: OK', flush=True)
    os._exit(0)


main()
