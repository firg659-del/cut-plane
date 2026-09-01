# SPDX-License-Identifier: GPL-3.0-or-later
"""Execute Cut: bisect the mesh along the gizmo plane."""

import bmesh
import bpy
from bpy.types import Operator
from mathutils import Vector

from ..lib.constants import PLANE_ANCHOR_ITEMS, logger
from .. import cutting


def _op_pivot_local(obj, op):
    """The operator's anchor in object-local space.

    Same as cutting._mirror_pivot_local except CURSOR reads the position
    STORED on the operator, not the live 3D cursor - so F9 and a scripted
    re-run reproduce the cut they replay."""
    if getattr(op, 'plane_anchor', 'BBOX') != 'CURSOR':
        return cutting._mirror_pivot_local(obj, op)
    try:
        return obj.matrix_world.inverted() @ Vector(op.cursor_world)
    except ValueError:
        return None                    # singular transform - skip the object


class CutPlaneCutOperator(Operator):
    """Bisect the mesh along the gizmo plane"""
    bl_idname = "view3d.cut_plane_cut"
    bl_label = "Cut Plane Cut"
    bl_description = "Cut the mesh along the gizmo plane (bisect)"
    # 'REGISTER' + 'UNDO': one clean undo step AND the native adjust-last-
    # operation (F9) panel, like Bisect. The panel is safe ONLY because the
    # tool state (plane pose + options) lives on the WindowManager: an F9
    # edit re-runs the operator as undo-then-execute, and the undo half
    # rolls back scene data alone - while the pose was a Scene group, any
    # panel edit teleported a since-moved plane back to its cut-time values
    # (measured live 2026-07-11). Two hard rules keep it safe:
    #   1. The panel draws OPERATOR properties only. Drawing live tool
    #      properties here hard-crashes Blender on toggle
    #      (ui_apply_but_autokey assumes every button in this panel belongs
    #      to the operator and null-derefs).
    #   2. The transform modals stay un-REGISTERed (see move/rotate/scale),
    #      so this panel keeps targeting the last CUT after a plane move -
    #      a registered modal would steal it and its "adjust" would pop the
    #      cut's undo step instead.
    bl_options = {'REGISTER', 'UNDO'}

    # Captured from the gizmo at invoke (scripted calls may pass their own).
    # HIDDEN and not drawn in the panel (user decision 2026-07-12): editing
    # them re-cuts elsewhere while the gizmo plane - independent tool state -
    # stays put, which reads as a bug. Making the gizmo FOLLOW panel edits
    # would need execute() to write the pose again, and it cannot tell
    # "typed a new point" from "moved the plane, then toggled Fill" - both
    # arrive as plane != pose, and guessing wrong is the v1 teleport.
    plane_co: bpy.props.FloatVectorProperty(
        name="Plane Point", size=3, subtype='TRANSLATION',
        options={'HIDDEN'},
    )
    plane_no: bpy.props.FloatVectorProperty(
        name="Plane Normal", size=3, subtype='DIRECTION', default=(0.0, 0.0, 1.0),
        options={'HIDDEN'},
    )
    # NOTE: execute() must NEVER write the gizmo pose - the user's plane is
    # tool state that stays wherever they put it.
    clear_inner: bpy.props.BoolProperty(
        name="Clear Inner",
        description="Remove geometry on the back side of the plane",
        default=False,
    )
    clear_outer: bpy.props.BoolProperty(
        name="Clear Outer",
        description="Remove geometry on the front side of the plane",
        default=False,
    )
    use_fill: bpy.props.BoolProperty(name="Fill", default=False)
    # Mirror-modifier semantics: also cut at the plane's reflections across
    # the object's local axes (see cutting._mirrored_planes_local).
    mirror_x: bpy.props.BoolProperty(
        name="Mirror X",
        description="Also cut at the plane mirrored across the object's local X axis",
        default=False,
    )
    mirror_y: bpy.props.BoolProperty(
        name="Mirror Y",
        description="Also cut at the plane mirrored across the object's local Y axis",
        default=False,
    )
    mirror_z: bpy.props.BoolProperty(
        name="Mirror Z",
        description="Also cut at the plane mirrored across the object's local Z axis",
        default=False,
    )
    # The CURSOR anchor resolves from the scene, so without this an F9
    # re-run after nudging the cursor produced a DIFFERENT cut (measured:
    # the mirrored plane moved outside the mesh and simply stopped cutting).
    # Captured at invoke so the operator replays from its own properties.
    cursor_world: bpy.props.FloatVectorProperty(
        name="Cursor", size=3, default=(0.0, 0.0, 0.0), options={'HIDDEN'},
    )
    plane_anchor: bpy.props.EnumProperty(
        name="Anchor",
        description="The point the mirrored cuts reflect through",
        items=PLANE_ANCHOR_ITEMS,
        default='BBOX',
    )
    selected_only: bpy.props.BoolProperty(
        name="Selected Only",
        description="Cut only the selected geometry (native Bisect semantics); "
                    "unselected geometry is never touched",
        default=False,
    )
    select_loop: bpy.props.BoolProperty(name="Select Cut Loop", default=True)

    @classmethod
    def poll(cls, context):
        return (context.mode == 'EDIT_MESH'
                and context.edit_object is not None
                and context.edit_object.type == 'MESH')

    def invoke(self, context, event):
        """Snapshot the current plane + tool options, then execute."""
        plane = cutting.get_plane_world(context)
        if plane is None:
            self.report({'WARNING'}, "No valid plane to cut with")
            return {'CANCELLED'}
        plane_co, plane_no = plane
        self.plane_co = plane_co
        self.plane_no = plane_no

        sc = context.window_manager.cut_plane
        self.clear_inner = sc.cut_clear_inner
        self.clear_outer = sc.cut_clear_outer
        self.use_fill = sc.cut_use_fill
        self.selected_only = sc.cut_selected_only
        self.select_loop = sc.cut_select_loop
        self.mirror_x = sc.mirror_x
        self.mirror_y = sc.mirror_y
        self.mirror_z = sc.mirror_z
        self.plane_anchor = sc.plane_anchor
        self.cursor_world = tuple(context.scene.cursor.location)

        return self.execute(context)

    def execute(self, context):
        obj = context.edit_object
        if obj is None or obj.type != 'MESH':
            return {'CANCELLED'}

        # F9 / redo path: ED_undo_operator_repeat re-executes IMMEDIATELY
        # after the undo, BEFORE the depsgraph re-evaluates - every object's
        # cached matrix_world still reads stale (identity for restored
        # objects). plane_to_object_local then converts the stored WORLD
        # plane with the SAME wrong matrix for every object: every mesh was
        # cut at one identical LOCAL plane ("all my objects look like
        # instances" - measured live: six scattered objects, one shared
        # local cut plane), and with a Clear on, a plane missing the
        # identity-space mesh WIPED it to zero verts (measured: 507v -> 0v,
        # all objects, status CUT). One synchronous view-layer update
        # restores true matrices before any conversion; ordinary clicks pay
        # a no-op refresh.
        context.view_layer.update()

        # Live-cut preview edits the mesh outside the undo system; restore the
        # original first so this operator bisects the clean mesh and stays
        # undo-clean (one step). Preview sessions are single-object (the
        # ACTIVE mesh) - the other edit meshes were never preview-touched.
        cutting.restore_live_cut(context)

        # Options write-back: keeps the tool settings in sync when this
        # operator runs with EXPLICIT properties (scripts) - the normal
        # invoke path snapshots the same values, so it's a no-op there.
        # Subscript assignment SKIPS the prop update callbacks - no preview
        # re-entry mid-operator; adopt_committed_baseline at the end
        # establishes the matching baseline from the same values.
        sc = context.window_manager.cut_plane
        sc["cut_use_fill"] = self.use_fill
        sc["cut_clear_inner"] = self.clear_inner
        sc["cut_clear_outer"] = self.clear_outer
        sc["cut_selected_only"] = self.selected_only
        sc["cut_select_loop"] = self.select_loop
        sc["mirror_x"] = self.mirror_x
        sc["mirror_y"] = self.mirror_y
        sc["mirror_z"] = self.mirror_z
        # Subscript assignment stores the RAW IDProperty, and Blender
        # keeps an enum as its INT index - handing it the string
        # identifier raises "Cannot assign a 'str' value to the
        # existing 'plane_anchor' Int IDProperty". Normal assignment
        # would take the string but fire the update callback, which is
        # exactly the preview re-entry the subscript form avoids.
        sc["plane_anchor"] = next(
            (item[4] for item in PLANE_ANCHOR_ITEMS
             if item[0] == self.plane_anchor), 0)

        # Native edit-mode semantics: the cut applies to EVERY mesh in the
        # multi-object edit session, like native Bisect. Deduped by mesh
        # DATA: linked duplicates share one edit-bmesh, and bisecting it
        # once per user would double-cut it.
        objects = [ob for ob in context.objects_in_mode_unique_data
                   if ob.type == 'MESH'] or [obj]

        statuses = [self._bisect_object(ob, context) for ob in objects]

        if 'ERROR' in statuses:
            self.report({'ERROR'}, "Bisect failed (see console for details)")
            return {'CANCELLED'}

        if 'CUT' not in statuses:
            # A cut that changed nothing anywhere was a silent no-op: warn
            # and CANCEL - a FINISHED no-op would push an empty undo step
            # and retarget F9 away from the previous (real) cut.
            if all(s == 'SKIP' for s in statuses):
                self.report({'WARNING'}, "Degenerate plane normal")
            elif self.selected_only and 'NOOP' not in statuses:
                self.report({'WARNING'}, "Selected Only: nothing selected to cut")
            else:
                target = "the selection" if self.selected_only else (
                    "the meshes" if len(objects) > 1 else "the mesh")
                self.report({'WARNING'}, f"Cut plane doesn't intersect {target}")
            return {'CANCELLED'}

        # Adopt the committed ACTIVE mesh as the live baseline WITHOUT re-previewing on
        # top, so this cut stays a single clean undo step (no watchdog re-bisect,
        # which forced a second Ctrl+Z and made Clear Outer show a committed loop
        # that should have been a preview). The next plane move re-previews.
        # The executed plane is passed so an F9 re-run with the gizmo plane
        # moved elsewhere re-establishes the preview UNDER the user's plane
        # instead of suppressing it there (the "cut teleported away" report).
        cutting.adopt_committed_baseline(
            context, executed_plane=(Vector(self.plane_co), Vector(self.plane_no)))

        # Advance the pose-undo token so the undo step Blender pushes for
        # this cut provably postdates every plane move - the tool-scoped
        # Ctrl+Z must undo the CUT before an older move (pose_history).
        from ..state import pose_history
        pose_history.mark_mesh_step(context)

        if context.area:
            context.area.tag_redraw()
        return {'FINISHED'}

    
    def _bisect_object(self, obj, context):
        """Bisect ONE edit-mesh object at the stored world plane.

        Returns 'CUT' (geometry changed), 'NOOP' (the plane misses it),
        'EMPTY' (Selected Only with nothing selected on this object - native
        semantics: nothing selected here means nothing cut here), 'SKIP'
        (degenerate object transform), or 'ERROR' (bisect raised).
        """
        bm = bmesh.from_edit_mesh(obj.data)
        pre_counts = (len(bm.verts), len(bm.edges), len(bm.faces))

        co_local, no_local = cutting.plane_to_object_local(
            obj, Vector(self.plane_co), Vector(self.plane_no))
        if no_local.length_squared < 1e-12:
            return 'SKIP'
        no_local.normalize()
        # The mirror toggles expand the one plane into its reflections; each
        # runs the full bisect/clear/fill phase in sequence - identical to
        # cutting at every plane by hand (same expansion as the preview).
        mirror_axes = tuple(i for i, on in enumerate(
            (self.mirror_x, self.mirror_y, self.mirror_z)) if on)
        # One WORLD set for the session, from the active object's frame, then
        # converted into this object's space - the same treatment the real
        # plane gets. pivot_local comes from _op_pivot_local so a CURSOR
        # anchor replays from the operator's stored cursor, not the scene.
        act = getattr(context, 'edit_object', None) or obj
        planes = []
        for c_w, n_w in cutting._mirrored_planes_world(
                context, Vector(self.plane_co), Vector(self.plane_no),
                mirror_axes, self, _op_pivot_local(act, self)):
            c_l, n_l = cutting.plane_to_object_local(obj, c_w, n_w)
            if n_l.length_squared < 1e-12:
                continue
            n_l.normalize()
            planes.append((c_l, n_l))
        if not planes:
            planes = [(co_local, no_local)]

        any_cut = False
        for plane_index, (p_co, p_no) in enumerate(planes):
            if self.selected_only:
                # Native mesh.bisect semantics: operate on selected geometry
                # only (restore_live_cut in execute brought back the baseline
                # selection; reselect_cut_domain keeps the domain coherent
                # between mirrored planes). Rebuilt per plane: a bisect
                # invalidates prior element lists.
                geom = ([v for v in bm.verts if v.select]
                        + [e for e in bm.edges if e.select]
                        + [f for f in bm.faces if f.select])
                if not geom:
                    # Nothing selected on THIS object and nothing done to it
                    # yet: native semantics, nothing cut here.
                    if plane_index == 0:
                        return 'EMPTY'
                    # But an earlier mirrored plane has already run, and `bm`
                    # IS the live edit mesh - its deletions are real whether
                    # or not we flush. Returning here skipped the
                    # count-changed guard below, so execute() reported
                    # "nothing selected to cut" + CANCELLED after wiping the
                    # mesh: no undo step pushed, mark_mesh_step never run.
                    # (Reachable with a fully-selected mesh + a Clear side
                    # covering all of it, e.g. Mirror X + Clear Inner + a
                    # plane past the geometry.) Stop the plane loop instead
                    # and let the guard classify what actually happened.
                    break
            else:
                geom = bm.verts[:] + bm.edges[:] + bm.faces[:]
            try:
                # NEVER the bisect's own clear flags: (a) Selected Only needs
                # the face-granular clear (vert-level would kill unselected
                # neighbor faces sharing the domain's boundary verts); (b)
                # standard mode needs BOTH halves alive for the selection
                # repair - the bisect reuses the original face for an
                # arbitrary side, so an in-bisect clear could delete the
                # selected sibling before the repair sees it (the sometimes-
                # deselects-under-Clear bug). One-shot operator: the python-
                # side clear cost is irrelevant here (the per-mousemove
                # preview keeps the fast native clear + snapshot-BVH repair).
                result = bmesh.ops.bisect_plane(
                    bm,
                    geom=geom,
                    dist=1e-5,
                    plane_co=p_co,
                    plane_no=p_no,
                    use_snap_center=False,
                    clear_inner=False,
                    clear_outer=False,
                )
            except Exception as e:
                logger.error(f"[CUT PLANE] Bisect failed on '{obj.name}': {e}")
                return 'ERROR'

            geom_cut = result.get('geom_cut', [])
            if not self.selected_only:
                # Two-sided repair while both halves still exist (see above)
                cutting.repair_split_selection(geom_cut)
            if self.clear_inner or self.clear_outer:
                # Face-granular for BOTH modes: equivalent to the native
                # clear on a whole-mesh domain, required for Selected Only
                cutting.clear_domain_side(bm, result.get('geom', ()), p_co,
                                          p_no, self.clear_inner,
                                          self.clear_outer)

            fill_geom = ()
            if self.use_fill:
                # Native Bisect's fill algorithm (mesh_bisect.cc):
                # triangle_fill with dissolve - handles non-manifold cross-
                # sections and interior membranes that edgenet_fill silently
                # skipped (see cutting/preview.py).
                cut_edges = [g for g in geom_cut
                             if isinstance(g, bmesh.types.BMEdge)]
                if cut_edges:
                    try:
                        fill_res = bmesh.ops.triangle_fill(
                            bm, edges=cut_edges, use_dissolve=True, normal=p_no)
                        fill_geom = fill_res.get('geom', ())
                    except Exception as e:
                        logger.warning(
                            f"[CUT PLANE] Fill failed on '{obj.name}': {e}")

            if self.selected_only:
                # Selection is the cut's INPUT domain - never rewrite it with
                # the loop-select output (via adopt_committed_baseline's
                # snapshot it would become the domain of every subsequent
                # cut). Keep the surviving domain fully selected instead, new
                # elements included.
                cutting.reselect_cut_domain(result.get('geom', ()), geom_cut,
                                            fill_geom)
            else:
                cutting.apply_cut_selection(
                    geom_cut, select=self.select_loop)

            any_cut = any_cut or bool(geom_cut) or bool(fill_geom)

        if not any_cut and \
                (len(bm.verts), len(bm.edges), len(bm.faces)) == pre_counts:
            return 'NOOP'

        bmesh.update_edit_mesh(obj.data, destructive=True)
        return 'CUT'

    def draw(self, context):
        """Adjust-last-operation (F9) panel: the cut options only. The plane
        point/normal props exist but are deliberately not drawn (see their
        declaration). OPERATOR properties only - see bl_options for why."""
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop(self, "use_fill")
        layout.prop(self, "clear_inner")
        layout.prop(self, "clear_outer")
        col = layout.column(heading="Mirror")
        col.prop(self, "mirror_x", text="X")
        col.prop(self, "mirror_y", text="Y")
        col.prop(self, "mirror_z", text="Z")
        layout.prop(self, "selected_only")
        # Same rule as the tool UI: Selected Only makes selection the cut's
        # INPUT domain, so the loop-select OUTPUT option is moot while on.
        row = layout.row()
        row.active = not self.selected_only
        row.prop(self, "select_loop")
