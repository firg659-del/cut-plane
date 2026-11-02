# SPDX-License-Identifier: GPL-3.0-or-later
"""Tool-state property groups, update callbacks, and the depsgraph handler."""

import math

import bpy
from mathutils import Vector

from ..lib.constants import (
    DEBUG,
    PLANE_ANCHOR_ITEMS,
    SCALE_SOFT_MAX,
    logger,
    on_internal_error,
)
from ..lib.math_utils import (
    flat_to_matrix,
    get_current_orientation_type,
    orientation_with_rotate_override,
)


def _tag_redraw_view3d(context) -> None:
    """Trigger an immediate viewport redraw (all 3D views as fallback)."""
    try:
        if context and hasattr(context, 'area') and context.area:
            context.area.tag_redraw()
        else:
            # No area on this context (timer/handler) - tag every 3D view
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type == 'VIEW_3D':
                        area.tag_redraw()
    except Exception:
        pass  # Property update succeeded even if redraw failed


def update_gizmo_mode(self, context) -> None:
    """Update callback for mode property - offset sync + immediate redraw.

    Offsets are converted HERE, synchronously: the group lives on the
    WindowManager, whose property writes never tag the depsgraph, so the
    depsgraph handler cannot be relied on to see this change in time (a
    drag could start before its next tick and read offsets in the wrong
    space). Update callbacks run in a write-safe UI context.
    """
    if DEBUG:
        logger.debug(f"[CUT PLANE] Mode changed to {self.mode}")
    sync_orientation_tracking(context)
    _tag_redraw_view3d(context)


def update_gizmo_orientation(self, context) -> None:
    """Update callback for the orientation override - offset sync + redraw.

    Same rule as update_gizmo_mode: convert the offsets synchronously,
    because WindowManager property writes never reach the depsgraph handler.
    """
    if DEBUG:
        logger.debug(f"[CUT PLANE] Orientation override changed to {self.orientation}")
    sync_orientation_tracking(context)
    _tag_redraw_view3d(context)


def update_cut_option(_self, context) -> None:
    """Update callback for cut options - the live preview reflects them at once.

    A UI click is a write-safe context, so re-cut immediately rather than
    waiting for the next plane move.
    """
    from .. import cutting
    try:
        cutting.update_live_preview(context)
    except Exception:
        on_internal_error()  # DEBUG: surface; prod: never block the option change
    _tag_redraw_view3d(context)


def update_cut_select(_self, context) -> None:
    """Select Cut Loop toggled - re-apply the preview so the cut loop selection
    appears/clears at once (the plane/clear key is unchanged, so force a refresh)."""
    from .. import cutting
    try:
        cutting.refresh_live_preview(context)
    except Exception:
        on_internal_error()  # DEBUG: surface; prod: graceful
    _tag_redraw_view3d(context)


# The last non-empty snap-target set, so emptying the set restores WHAT WAS
# THERE - native's RNA refuses the empty assignment outright (measured), and
# "you can't turn the last one off" must not morph into "turning the last
# one off jumps to Increment".
_last_snap_elements = 1 << 0  # INCREMENT


def _keep_snap_target_nonempty(self, _context) -> None:
    """Native ToolSettings.snap_elements REFUSES an empty assignment at the
    RNA level (measured); a Python ENUM_FLAG cannot, so unticking every
    target left the header reading "Mix" while the engine silently fell
    back to INCREMENT. Restore the LAST non-empty set into the property
    instead, so the UI and the engine agree by construction and the
    behavior matches native's refusal. Subscript write: skips this
    callback, no re-entry."""
    global _last_snap_elements
    if self.snap_elements:
        _last_snap_elements = self["snap_elements"]
    else:
        self["snap_elements"] = _last_snap_elements


def update_show_handles(_self, context) -> None:
    """A display toggle changed (gizmo handles / mirror ghosts) - redraw so
    the visuals appear/vanish at once. Display only, never touches the cut."""
    _tag_redraw_view3d(context)


def ensure_default_center(context) -> None:
    """Default plane placement, once per edit object: a VIRGIN plane (never
    positioned by the user) is placed at the mesh bounding-box center instead
    of the object origin, so the live preview intersects the mesh immediately.

    One-shot per edit object (tracked by session_uid on the runtime group).
    The moment the plane has ANY position state - user drag, typed value, or
    an earlier centering - it is the user's, and switching objects never
    moves it (the pose formula stays origin-relative). Reset re-centers
    explicitly via the same default_center_offset.

    Called from the gizmo group's setup() through a one-shot timer (setup
    runs only when OUR tool is active, and the timer runs outside the draw
    context, where scene writes are allowed).
    """
    props = getattr(context.window_manager, 'cut_plane', None)
    runtime = get_runtime(context)
    # Timer contexts may lack edit_object - fall back to the active object
    # when it is in Edit Mode (the same object by definition).
    edit_obj = getattr(context, 'edit_object', None)
    if edit_obj is None and getattr(context, 'view_layer', None) is not None:
        active = context.view_layer.objects.active
        if active is not None and active.mode == 'EDIT':
            edit_obj = active
    if props is None or runtime is None or edit_obj is None \
            or edit_obj.type != 'MESH':
        return
    if runtime.centered_edit_uid == edit_obj.session_uid:
        return
    runtime.centered_edit_uid = edit_obj.session_uid
    virgin = (props.x_offset == 0.0 and props.y_offset == 0.0
              and props.z_offset == 0.0
              and tuple(props.center_offset) == (0.0, 0.0, 0.0))
    if not virgin:
        return
    # The ORIGINAL's box, like Reset: the watchdog's first apply runs before
    # this timer, so with a Clear on, obj.bound_box is already the cut half
    # (measured on 5.1 and 5.2: a default cube got its plane at z -0.5; 4.5's
    # box lagged a turn and read the original by luck).
    from .. import cutting
    props.center_offset = cutting.baseline_center_offset(
        edit_obj, props.plane_anchor)
    # Same rule as every plane-moving write path: the preview only follows
    # the plane via explicit refreshes. Timers are write-safe.
    try:
        cutting.update_live_preview(context)
    except Exception:
        on_internal_error()  # DEBUG: surface; prod: never block the centering
    _tag_redraw_view3d(context)
    if DEBUG:
        logger.debug(f"[DEFAULT CENTER] {edit_obj.name}: "
                     f"center_offset={tuple(props.center_offset)}")


class CutPlaneProperties(bpy.types.PropertyGroup):
    """Tool state: plane pose, gizmo display, cut options, tool-local snap.

    Lives on the WINDOW MANAGER, not the Scene, deliberately: undo rolls the
    scene back but never touches WindowManager data, so neither Ctrl+Z nor
    the undo half of F9 (adjust-last-operation) can move the user's plane -
    the pose is tool state, and it stays exactly where the user put it.
    The cost: WM properties are not saved into .blend
    files, so persistence.py stamps this state into the scene on save and
    restores it on load.
    """

    # The gizmo HANDLE SET on display (arrows / boxes / dials) - a gizmo
    # concern only; keyboard G/R/S work in any mode. Icons make the compact
    # header dropdown readable (constraint-limit glyphs = the transform set).
    mode: bpy.props.EnumProperty(
        name="Gizmo Mode",
        description="Which handle set the gizmo shows",
        items=[
            ('MOVE', "Move", "Arrow and square handles: translate the plane",
             'CON_LOCLIMIT', 0),
            ('SCALE', "Scale", "Box handles: scale the plane",
             'CON_SIZELIMIT', 1),
            ('ROTATE', "Rotate", "Dial handles: rotate the plane",
             'CON_ROTLIMIT', 2),
        ],
        default='MOVE',
        update=update_gizmo_mode
    )

    # Tool's own orientation - standalone, independent of the scene's transform
    # orientation dropdown (the gizmo never reads or writes the scene setting).
    # Icons match the native orientation enum, so the label-less header
    # dropdown renders exactly like the native header one.
    orientation: bpy.props.EnumProperty(
        name="Orientation",
        description="Coordinate space for the cut plane "
                    "(independent of the scene's transform orientation)",
        items=[
            ('GLOBAL', "Global", "Align the gizmo to world axes",
             'ORIENTATION_GLOBAL', 0),
            ('LOCAL', "Local", "Align the gizmo to the plane's own rotation",
             'ORIENTATION_LOCAL', 1),
        ],
        default='GLOBAL',
        update=update_gizmo_orientation
    )

    # Keyboard-first option: hide the interactive handles (arrows/dials/squares)
    # and drive the plane with G/R/S only. The orange plane itself stays visible.
    show_gizmo_handles: bpy.props.BoolProperty(
        name="Show Gizmo Handles",
        description="Show the interactive move/rotate/scale handles. Turn off "
                    "for a keyboard-driven workflow (G / R / S still transform "
                    "the plane; the plane outline stays visible)",
        default=True,
        update=update_show_handles,
    )

    # NOTE: transient state (active drag flags, arc/marker visuals,
    # change-detection trackers) lives in CutPlaneRuntimeProperties on the
    # WindowManager - never saved into .blend files, never in the undo
    # stack, and writes do not trigger depsgraph updates.

    # Plane offsets from the object origin, in the EFFECTIVE orientation's
    # space (GLOBAL: world axes; LOCAL: plane axes). Orientation/mode changes
    # convert them in-place via a pure change of basis (see
    # handle_orientation_or_mode_change) - no world-space cache involved.
    x_offset: bpy.props.FloatProperty(
        name="X Offset",
        description="Plane offset from the object origin along X",
        default=0.0,
        unit='LENGTH',
    )
    y_offset: bpy.props.FloatProperty(
        name="Y Offset",
        description="Plane offset from the object origin along Y",
        default=0.0,
        unit='LENGTH',
    )
    z_offset: bpy.props.FloatProperty(
        name="Z Offset",
        description="Plane offset from the object origin along Z",
        default=0.0,
        unit='LENGTH',
    )
    center_offset: bpy.props.FloatVectorProperty(
        name="Center Offset",
        description="Free 3D movement offset",
        default=(0.0, 0.0, 0.0),
        size=3,
        unit='LENGTH',
        subtype='TRANSLATION'
    )


    # Scale Mode Factors (allow negative for geometry flipping, like native Blender)
    x_scale: bpy.props.FloatProperty(
        name="X Scale", description="X-axis scale", default=1.0,
        soft_min=-SCALE_SOFT_MAX, soft_max=SCALE_SOFT_MAX
    )
    y_scale: bpy.props.FloatProperty(
        name="Y Scale", description="Y-axis scale", default=1.0,
        soft_min=-SCALE_SOFT_MAX, soft_max=SCALE_SOFT_MAX
    )
    z_scale: bpy.props.FloatProperty(
        name="Z Scale", description="Z-axis scale", default=1.0,
        soft_min=-SCALE_SOFT_MAX, soft_max=SCALE_SOFT_MAX
    )

    # Rotation Matrix Storage (for operator-based rotation)
    # 4x4 matrix stored as 16 floats in row-major order
    rotation_matrix_flat: bpy.props.FloatVectorProperty(
        name="Rotation Matrix",
        description="4x4 rotation matrix flattened to 16 floats",
        size=16,
        default=(1, 0, 0, 0,  0, 1, 0, 0,  0, 0, 1, 0,  0, 0, 0, 1),
    )

    # Cutting tool settings. Persistent options
    # that drive BOTH the realtime preview and what Execute Cut commits;
    # the operator snapshots them into its own properties at invoke so the
    # redo panel (F9) can re-adjust each cut independently.
    # Native-Bisect-style independent toggles. Neither = keep both sides
    # (live split); either removes that side; both removes everything at the cut.
    cut_clear_inner: bpy.props.BoolProperty(
        name="Clear Inner",
        description="Remove geometry on the back side of the plane "
                    "(the side the normal points away from)",
        default=False,
        update=update_cut_option,
    )
    cut_clear_outer: bpy.props.BoolProperty(
        name="Clear Outer",
        description="Remove geometry on the front side of the plane "
                    "(the side the normal points toward)",
        default=False,
        update=update_cut_option,
    )
    cut_use_fill: bpy.props.BoolProperty(
        name="Fill",
        description="Cap the cut on the side you keep with new faces. "
                    "Works with Clear Inner or Clear Outer: with both sides "
                    "kept there is no open edge to close",
        default=False,
        update=update_cut_option,
    )
    cut_select_loop: bpy.props.BoolProperty(
        name="Select Cut Loop",
        description="ADD the new cut edge loop to the selection in the live "
                    "preview / after cutting. Never deselects anything - your "
                    "own selection always survives, and loops from successive "
                    "cuts stay selected together (deselect to shed them; "
                    "deselections made during the preview stick)",
        default=True,
        update=update_cut_select,
    )
    # Native mesh.bisect is selection-scoped; this makes that available as an
    # EXPLICIT toggle (implicit native behavior would misfire here: this is a
    # persistent tool and Select Cut Loop rewrites the selection after every
    # cut). While on, selection is the cut's INPUT domain, so the loop-select
    # output options are disabled (one selection role at a time).
    cut_selected_only: bpy.props.BoolProperty(
        name="Selected Only",
        description="Cut only the selected geometry (like native Bisect); "
                    "unselected geometry is never touched, cleared, or filled. "
                    "Select BEFORE activating the tool (or before the current "
                    "preview session): the preview keeps cutting the selection "
                    "captured at its start. Nothing selected = nothing cut. "
                    "Disables Select Cut Loop while on",
        default=False,
        # Force-refresh callback: the toggle changes the cut DOMAIN, which is
        # not part of the plane dirty-key (update_cut_option would no-op).
        update=update_cut_select,
    )

    # --- Mirror -----------------------------------------------------------
    # Reflect the cut across the object's local axes through its origin
    # (Mirror-modifier semantics). Each enabled axis doubles the planes:
    # X+Y = 4 cuts, X+Y+Z = 8. Preview and Execute both honor them; the
    # mirrored planes are DERIVED from the one real plane at cut time
    # (cutting._mirrored_planes_local) - never stored state.
    mirror_x: bpy.props.BoolProperty(
        name="Mirror X",
        description="Also cut at the plane mirrored across the object's "
                    "local X axis (like the Mirror modifier)",
        default=False,
        update=update_cut_option,
    )
    mirror_y: bpy.props.BoolProperty(
        name="Mirror Y",
        description="Also cut at the plane mirrored across the object's "
                    "local Y axis (like the Mirror modifier)",
        default=False,
        update=update_cut_option,
    )
    mirror_z: bpy.props.BoolProperty(
        name="Mirror Z",
        description="Also cut at the plane mirrored across the object's "
                    "local Z axis (like the Mirror modifier)",
        default=False,
        update=update_cut_option,
    )
    # ONE reference for where a fresh plane centres AND what the mirrors
    # reflect through. They used to disagree (bbox for placement, origin for
    # mirroring), which put the mirrored cut 2 x (bbox centre - origin) away
    # from where it belonged. Still derived, never stored - resolved per
    # object at cut time like the planes themselves.
    plane_anchor: bpy.props.EnumProperty(
        name="Anchor",
        description="The point a fresh plane centres on, and the point the "
                    "mirrored cuts reflect through",
        items=PLANE_ANCHOR_ITEMS,
        default='BBOX',
        update=update_cut_option,
    )
    # Display only: the gray ghost planes at the mirrored positions. Hiding
    # them never changes what gets cut - the mirror toggles do that.
    show_mirror_planes: bpy.props.BoolProperty(
        name="Show Mirrored Planes",
        description="Show gray ghost planes at the mirrored cut positions "
                    "(display only - the mirrored cuts happen either way)",
        default=True,
        update=update_show_handles,
    )

    # --- Tool-local snapping ---------------------------------------------
    # The gizmo's snapping is INDEPENDENT of Blender's global snap settings
    # (magnet, targets, etc.) - configuring it here never touches the scene,
    # and the scene's magnet never affects the gizmo. These mirror the native
    # snap settings the gizmo reads (snapping.py); identifiers/icons match
    # native so the trimmed popover looks identical. Seeded once from the
    # scene's current snap on first tool activation (snap_seeded), then
    # standalone. Ctrl still inverts the magnet, as in native.
    snap_use: bpy.props.BoolProperty(
        name="Snap",
        description="Snap the cut plane to geometry / increments while dragging "
                    "(Ctrl inverts). Independent of Blender's global snap toggle",
        default=False,
    )
    snap_elements: bpy.props.EnumProperty(
        name="Snap Target",
        description="What the cut plane snaps to",
        items=[
            ('INCREMENT', "Increment", "Snap to increments", 'SNAP_INCREMENT', 1 << 0),
            ('GRID', "Grid", "Snap to grid", 'SNAP_GRID', 1 << 1),
            ('VERTEX', "Vertex", "Snap to vertices", 'SNAP_VERTEX', 1 << 2),
            ('EDGE', "Edge", "Snap to edges", 'SNAP_EDGE', 1 << 3),
            ('FACE', "Face", "Snap to faces", 'SNAP_FACE', 1 << 4),
            ('EDGE_MIDPOINT', "Edge Center", "Snap to the middle of edges",
             'SNAP_MIDPOINT', 1 << 5),
            ('FACE_MIDPOINT', "Face Center", "Snap to the middle of faces",
             'SNAP_FACE_CENTER', 1 << 6),
        ],
        default={'INCREMENT'},
        options={'ENUM_FLAG'},
        update=_keep_snap_target_nonempty,
    )
    snap_affect_move: bpy.props.BoolProperty(
        name="Move", description="Use snapping when moving the plane", default=True,
    )
    snap_affect_rotate: bpy.props.BoolProperty(
        name="Rotate", description="Use snapping when rotating the plane", default=True,
    )
    snap_affect_scale: bpy.props.BoolProperty(
        name="Scale", description="Use snapping when scaling the plane", default=True,
    )
    snap_angle_increment: bpy.props.FloatProperty(
        name="Angle Increment",
        description="Angle step for rotation snapping",
        default=math.radians(5.0), min=0.0, subtype='ANGLE',
    )
    snap_angle_increment_precision: bpy.props.FloatProperty(
        name="Angle Increment (Precision)",
        description="Finer angle step for rotation snapping (with Shift)",
        default=math.radians(1.0), min=0.0, subtype='ANGLE',
    )
    snap_align_rotation: bpy.props.BoolProperty(
        name="Align Rotation to Target",
        description="Align the plane's normal to the snapped surface normal",
        default=False,
    )
    # Seed-once latch: copy the scene's snap settings into the tool the first
    # time the tool is activated in this scene, then leave the tool standalone.
    snap_seeded: bpy.props.BoolProperty(default=False, options={'HIDDEN'})


class CutPlaneRuntimeProperties(bpy.types.PropertyGroup):
    """Transient per-session state, attached to the WindowManager.

    Everything here is written during modal drags (often per mousemove)
    or used for change detection. Living on the WindowManager instead of
    the Scene means: never saved into .blend files, never part of the
    undo stack, and - crucially - writes do NOT trigger depsgraph
    updates, so per-event updates stop re-running the depsgraph handler.
    """

    # Active rotation state for arc drawing
    rotation_active: bpy.props.BoolProperty(
        name="Rotation Active",
        description="Whether a rotation operation is in progress",
        default=False
    )
    rotation_active_dial: bpy.props.StringProperty(
        name="Active Dial",
        description="Which dial is currently being rotated (X, Y, Z, VIEW, TRACKBALL, or empty)",
        default=""
    )
    move_active_axis: bpy.props.StringProperty(
        name="Active Move Axis",
        description="Axis arrow currently being dragged (X, Y, Z, or empty)",
        default=""
    )
    rotation_arc_center: bpy.props.FloatVectorProperty(
        name="Arc Center",
        description="Center point for rotation arc display",
        size=3,
        default=(0.0, 0.0, 0.0)
    )
    rotation_arc_axis: bpy.props.FloatVectorProperty(
        name="Arc Axis",
        description="Rotation axis for arc display",
        size=3,
        default=(0.0, 0.0, 1.0)
    )
    rotation_arc_start_angle: bpy.props.FloatProperty(
        name="Arc Start Angle",
        description="Starting angle for rotation arc",
        default=0.0
    )
    rotation_arc_current_angle: bpy.props.FloatProperty(
        name="Arc Current Angle",
        description="Current angle for rotation arc",
        default=0.0
    )
    rotation_arc_color: bpy.props.FloatVectorProperty(
        name="Arc Color",
        description="RGB color for rotation arc",
        size=3,
        default=(1.0, 1.0, 1.0)
    )
    rotation_arc_scale: bpy.props.FloatProperty(
        name="Arc Scale",
        description="Scale factor for arc radius (1.0 for axis dials, 1.2 for view dial)",
        default=1.0
    )
    rotation_snap_active: bpy.props.BoolProperty(
        name="Snap Active",
        description="Whether angle snapping is active during rotation",
        default=False
    )
    rotation_snap_increment: bpy.props.FloatProperty(
        name="Snap Increment",
        description="Angle increment used for the snap tick marks (radians)",
        default=math.radians(5.0)
    )

    # Snap marker (drawn at the geometry point the center snapped to)
    snap_marker_active: bpy.props.BoolProperty(
        name="Snap Marker Active",
        description="Whether the geometry snap marker is visible",
        default=False
    )
    snap_marker_location: bpy.props.FloatVectorProperty(
        name="Snap Marker Location",
        description="World position of the geometry snap point",
        size=3,
        default=(0.0, 0.0, 0.0)
    )
    snap_marker_element: bpy.props.StringProperty(
        name="Snap Marker Element",
        description="Snap element type the marker represents (drives its shape)",
        default=""
    )

    # One-shot tracker for the default plane placement: the session_uid of
    # the last edit object ensure_default_center() considered (0 = none yet)
    centered_edit_uid: bpy.props.IntProperty(default=0, options={'HIDDEN'})

    # Change detection for the depsgraph handler. EMPTY defaults on purpose:
    # the first tick must RECORD the actual state, not convert offsets
    # against assumed defaults (scene offsets persist in .blend files in
    # their saved orientation space; these trackers do not).
    last_orientation: bpy.props.StringProperty(
        name="Last Orientation",
        description="Last known orientation type for change detection",
        default='',
        options={'HIDDEN'}
    )
    last_mode: bpy.props.StringProperty(
        name="Last Mode",
        description="Last known mode for change detection",
        default='',
        options={'HIDDEN'}
    )

    # NOTE on F9 (adjust-last-operation): its undo half rolls the SCENE
    # back to the operator's undo step. While CutPlaneProperties was a
    # Scene group, that reset the pose to its cut-time values (measured
    # live 2026-07-11: center (1.705, 4.583, 0.586) -> (0, 0, 0) on a
    # panel toggle) - the reason BOTH tool-state groups now live on the
    # WindowManager, which undo does not manage. Keep them there.


def get_runtime(context):
    """The WindowManager-level transient state, or None pre-registration."""
    return getattr(context.window_manager, 'cut_plane_runtime', None)


def _restore_preview_on_edit_exit() -> None:
    """Restore the live-cut preview when edit mode is left.

    Leaving edit mode tags the depsgraph, which is the only reliable signal for
    it - so the depsgraph handler delegates here. Kept separate from the offset
    bookkeeping below: they are unrelated jobs sharing one handler slot. (Live
    preview updates during a drag can't ride the depsgraph - scene PropertyGroup
    writes don't tag it - so those are driven from the transform modals.)
    """
    from .. import cutting
    if not cutting._sessions:
        # Fast path: no live preview anywhere, nothing to restore. This is
        # the steady state whenever the tool is not in use, so the handler
        # costs two attribute reads and returns.
        return
    try:
        if getattr(bpy.context, 'mode', None) != 'EDIT_MESH':
            cutting.restore_live_cut(bpy.context)
    except Exception:
        # DEBUG: surface; prod: a preview hiccup must never break offset bookkeeping
        on_internal_error()


def sync_orientation_tracking(context) -> None:
    """Offset-space conversion when the effective orientation changes.

    Offsets are stored in the EFFECTIVE orientation's space, so a
    GLOBAL<->LOCAL flip must rewrite them to keep the plane still. That is a
    pure CHANGE OF BASIS on the offset vector - the object origin cancels
    out of `origin + f_old(offsets) = origin + f_new(offsets')`.

    Called synchronously from the mode/orientation update callbacks (the
    group's WindowManager writes never tag the depsgraph) and from the
    depsgraph handler as the tracker-initializing safety net. Idempotent:
    no effective change means no work.

    Deliberately NO world-space anchor: the previous implementation cached
    an absolute world position (written only by offset updates) and
    subtracted the CURRENT origin here - switching or moving the edit object
    between those two moments teleported the plane to the OLD object's world
    position on the next orientation/mode toggle, and the very first tick
    (or a file load with saved LOCAL orientation) converted against a stale
    default cache. Verified + regression-locked in
    tests/integration/anchor_stability.py.
    """
    props = getattr(context.window_manager, 'cut_plane', None)
    runtime = get_runtime(context)
    if props is None or runtime is None:
        return

    orientation_type = get_current_orientation_type(context)
    current_mode = props.mode

    if (orientation_type == runtime.last_orientation
            and current_mode == runtime.last_mode):
        return  # No change, nothing to do

    # Effective orientation collapses to GLOBAL in ROTATE mode (rotating
    # changes the local->world transform, so a local offset would drift);
    # orientation_with_rotate_override is the single source of that rule.
    # An empty tracker means FIRST TICK of the session: record only - the
    # stored offsets are already in the saved state's space, converting
    # them against assumed defaults is exactly the file-load corruption.
    old_space = orientation_with_rotate_override(
        runtime.last_orientation, runtime.last_mode) \
        if runtime.last_orientation and runtime.last_mode else None
    new_space = orientation_with_rotate_override(orientation_type, current_mode)

    if old_space is not None and old_space != new_space:
        offsets = Vector((props.x_offset, props.y_offset, props.z_offset))
        rotation_matrix = flat_to_matrix(props.rotation_matrix_flat)
        converted = None
        if old_space == 'LOCAL':  # LOCAL -> GLOBAL
            converted = rotation_matrix @ offsets
        else:  # GLOBAL -> LOCAL
            try:
                converted = rotation_matrix.inverted() @ offsets
            except ValueError:
                if DEBUG:
                    logger.warning("[HANDLER] Singular rotation matrix - "
                                   "offsets left unconverted")
        if converted is not None:
            props.x_offset = converted.x
            props.y_offset = converted.y
            props.z_offset = converted.z
        if DEBUG:
            logger.debug(f"[HANDLER] Offset basis change {old_space} -> "
                         f"{new_space}: {tuple(offsets)} -> "
                         f"{tuple(converted) if converted else 'unchanged'}")

    runtime.last_orientation = orientation_type
    runtime.last_mode = current_mode


@bpy.app.handlers.persistent
def handle_orientation_or_mode_change(_scene):
    """Depsgraph handler: preview teardown on edit-mode exit + orientation
    tracker safety net.

    The offset conversion itself happens synchronously in the mode/
    orientation update callbacks (see sync_orientation_tracking). This
    handler keeps two jobs: restoring the preview when edit mode is left,
    and initializing the orientation trackers early in a session (the
    first sync call must RECORD state, and it must happen before the user's
    first orientation flip - the depsgraph ticks long before that).

    COST INVARIANT (why an always-on depsgraph handler is acceptable here):
    the steady-state tick is O(1) and constant in scene size - a handful of
    RNA attribute reads, no object iteration, no mesh access, no redraw
    tagging, and zero writes (writes happen only at tracker init and on a
    real orientation/mode flip, onto WindowManager properties, which do not
    tag the depsgraph - so the handler can never retrigger itself). The one
    non-trivial path (restoring a live preview) runs only when a preview
    session actually exists AND edit mode was just left - and leaving edit
    mode tags the depsgraph, making this the only reliable, write-safe
    signal for it.

    Runs OUTSIDE the drawing context, so it CAN write properties.
    """
    if not hasattr(bpy.types.WindowManager, 'cut_plane'):
        return

    _restore_preview_on_edit_exit()
    sync_orientation_tracking(bpy.context)
