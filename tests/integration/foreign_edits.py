# SPDX-License-Identifier: GPL-3.0-or-later
"""An edit made while the preview shows is never silently reverted.

    blender --background --factory-startup --python-exit-code 1 \
            --python tests/integration/foreign_edits.py

1.1.0 told a user edit from its own writes by the element counts alone.
Edits that keep them - a menu move, flip normals, shade smooth, a material,
UV unwrap, seams, sharp edges - went unseen: the next plane move rebuilt
from the snapshot and reverted them, and so did leaving the tool, Tab and
a save. A content fingerprint now catches them, and the mesh is kept as
the user sees it, exactly like an extrude always was.

The fingerprint costs most of a preview apply, so it must never run per
frame while a plane transform owns the input: counted below. A view-state
change (selection, hide) must not count as an edit, or every click would
bake the preview into the mesh.
"""
import os
import sys

import bmesh
import bpy

ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

import cut_plane  # noqa: E402
from cut_plane import cutting  # noqa: E402
from cut_plane.cutting import snapshot  # noqa: E402

FAILURES = []
ctx = bpy.context


def check(name, condition, detail=''):
    print(f"[{'ok' if condition else 'FAIL'}] {name}" + (f' ({detail})' if detail else ''),
          flush=True)
    if not condition:
        FAILURES.append(name)


def plane_at(z):
    sc = ctx.window_manager.cut_plane
    sc['center_offset'] = (0.0, 0.0, float(z))
    sc['x_offset'] = sc['y_offset'] = sc['z_offset'] = 0.0


def build():
    """A subdivided cube, two materials, a split preview low at z -0.3 -
    the edits below happen on the top, away from the cut."""
    cutting.restore_live_cut(ctx)
    if ctx.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    bpy.ops.mesh.primitive_cube_add(size=2)
    obj = ctx.active_object
    obj.data.materials.append(bpy.data.materials.new('A'))
    obj.data.materials.append(bpy.data.materials.new('B'))
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.subdivide(number_cuts=1)
    bpy.ops.mesh.select_all(action='DESELECT')
    sc = ctx.window_manager.cut_plane
    sc['cut_clear_inner'] = sc['cut_clear_outer'] = sc['cut_use_fill'] = False
    sc['cut_selected_only'] = False
    sc['cut_select_loop'] = False
    sc['mirror_x'] = sc['mirror_y'] = sc['mirror_z'] = False
    plane_at(-0.3)
    cutting.update_live_preview(ctx)
    return obj


def select_top(obj):
    bpy.ops.mesh.select_mode(type='FACE')
    bm = bmesh.from_edit_mesh(obj.data)
    for f in bm.faces:
        f.select_set(f.calc_center_median().z > 0.99)
    bmesh.update_edit_mesh(obj.data)


def top(obj):
    """What the edits change, read off the top faces."""
    bm = bmesh.from_edit_mesh(obj.data)
    faces = [f for f in bm.faces if f.calc_center_median().z > 0.9]
    uv = bm.loops.layers.uv.active
    edges = {e for f in faces for e in f.edges}
    return {
        'max_z': round(max(v.co.z for v in bm.verts), 4),
        'material': sorted({f.material_index for f in faces}),
        'smooth': sorted({f.smooth for f in faces}),
        'normal_z': sorted({round(f.normal.z, 3) for f in faces}),
        'seam': sorted({e.seam for e in edges}),
        'sharp': sorted({not e.smooth for e in edges}),
        'uv': round(sum(c[uv].uv.x + 3 * c[uv].uv.y for f in faces for c in f.loops), 4),
    }


EDITS = {
    'move (Mesh > Transform > Move)': lambda obj: bpy.ops.transform.translate(value=(0, 0, 0.5)),
    'flip normals': lambda obj: bpy.ops.mesh.flip_normals(),
    'shade smooth': lambda obj: bpy.ops.mesh.faces_shade_smooth(),
    'assign material B': lambda obj: (setattr(obj, 'active_material_index', 1),
                                      bpy.ops.object.material_slot_assign()),
    'UV unwrap': lambda obj: bpy.ops.uv.unwrap(),
    'mark seam': lambda obj: bpy.ops.mesh.mark_seam(),
    'mark sharp': lambda obj: bpy.ops.mesh.mark_sharp(),
}


def edited(name):
    """A preview, the top selected (folded), then the edit."""
    obj = build()
    select_top(obj)
    cutting.update_live_preview(ctx)
    before = top(obj)
    EDITS[name](obj)
    after = top(obj)
    return obj, before, after


def main():
    cut_plane.register()
    try:
        # --- every count-preserving edit survives what used to revert it ---
        for name in EDITS:
            for exit_path in ('plane move', 'leaving the tool', 'Tab', 'save'):
                obj, before, after = edited(name)
                check(f'fixture {name}: the edit changed the mesh and kept its counts',
                      after != before, f'{before} -> {after}')
                if exit_path == 'plane move':
                    plane_at(-0.5)
                    cutting.update_live_preview(ctx)
                elif exit_path == 'leaving the tool':
                    cutting.restore_live_cut(ctx)
                elif exit_path == 'Tab':
                    bpy.ops.object.mode_set(mode='OBJECT')
                    cutting.restore_live_cut(ctx)
                    bpy.ops.object.mode_set(mode='EDIT')
                else:
                    cutting.handlers._on_save_pre()
                    cutting.handlers._on_save_post()
                check(f'{name}: survives {exit_path}', top(obj) == after, f'{after} -> {top(obj)}')

        # --- view state is not an edit -------------------------------------
        obj = build()
        for op in (lambda: bpy.ops.mesh.select_all(action='SELECT'),
                   lambda: bpy.ops.mesh.select_mode(type='EDGE'),
                   lambda: bpy.ops.mesh.hide(unselected=False),
                   lambda: bpy.ops.mesh.reveal(),
                   lambda: bpy.ops.mesh.select_all(action='DESELECT')):
            op()
        cutting.restore_live_cut(ctx)
        bm = bmesh.from_edit_mesh(obj.data)
        check('selection and hide changes do not bake the preview: leaving restores the original',
              len(bm.verts) == 26, f'{len(bm.verts)} verts')
        obj = build()
        bpy.ops.object.mode_set(mode='OBJECT')
        cutting.restore_live_cut(ctx)
        check('a Tab without an edit restores the original', len(obj.data.vertices) == 26,
              f'{len(obj.data.vertices)} verts')

        # --- never per frame while a plane transform owns the input --------
        obj = build()
        calls = {'n': 0}
        real_hash = snapshot._hash_mesh

        def counted(me):
            calls['n'] += 1
            return real_hash(me)

        snapshot._hash_mesh = counted
        try:
            cutting.begin_owned_input()
            for i in range(10):
                plane_at(-0.3 - 0.05 * (i + 1))
                cutting.update_live_preview(ctx)
            during = calls['n']
            cutting.end_owned_input()
            after_end = calls['n']
            plane_at(-0.2)
            cutting.update_live_preview(ctx)
            one_shot = calls['n'] - after_end
        finally:
            snapshot._hash_mesh = real_hash
        check('a 10-frame drag fingerprints once, at its start', during == 1, f'{during} calls')
        check('...and once more when it lets go', after_end - during == 1, f'{after_end - during}')
        check('CONTROL: a one-shot write outside a transform does fingerprint', one_shot >= 1,
              f'{one_shot} calls')

        # --- an edit between two drags is caught when the next one starts --
        obj = build()
        cutting.begin_owned_input()
        plane_at(-0.4)
        cutting.update_live_preview(ctx)
        cutting.end_owned_input()
        select_top(obj)
        bpy.ops.mesh.faces_shade_smooth()
        smoothed = top(obj)
        cutting.begin_owned_input()
        for z in (-0.5, -0.6):
            plane_at(z)
            cutting.update_live_preview(ctx)
        cutting.end_owned_input()
        check('an edit made between two drags survives the second drag', top(obj) == smoothed,
              f'{smoothed} -> {top(obj)}')
    finally:
        cutting.restore_live_cut(ctx)
        if ctx.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        cut_plane.unregister()
    if FAILURES:
        print(f'FOREIGN EDITS: {len(FAILURES)} FAILED', flush=True)
        os._exit(1)
    print('FOREIGN EDITS: OK', flush=True)
    os._exit(0)


main()
