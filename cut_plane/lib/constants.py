# SPDX-License-Identifier: GPL-3.0-or-later
"""Tuning constants and module-load-time caches for the Cut Plane addon."""

import logging
import math
import os

from mathutils import Matrix, Vector

# Package logger: errors/warnings always reach the console, debug output
# is gated by the DEBUG flag below. The plain formatter keeps the output
# identical to the previous print() messages (which carry their own
# [CUT PLANE]-style prefixes).
logger = logging.getLogger("cut_plane")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_handler)
    logger.propagate = False

# Plane appearance
PLANE_SIZE = 2.0  # Base size in meters (before scaling)
PLANE_COLOR_OUTLINE = (1.0, 0.6, 0.0, 1.0)  # Bright orange, full opacity
PLANE_OUTLINE_WIDTH = 2.0  # Outline thickness in pixels
# Mirrored-plane ghosts: neutral gray so the ONE real (orange) plane stays
# unmistakable; translucent so they read as previews, not extra gizmos
MIRROR_GHOST_COLOR = (0.55, 0.55, 0.55, 0.6)

# Gizmo appearance (RGB axis convention: X=Red, Y=Green, Z=Blue)
# Tuned to match Blender's native default theme colors
GIZMO_ARROW_COLOR_X = (0.96, 0.22, 0.36)  # Native Red
GIZMO_ARROW_COLOR_Y = (0.46, 0.84, 0.13)  # Native Green
GIZMO_ARROW_COLOR_Z = (0.18, 0.48, 0.98)  # Native Blue
GIZMO_ARROW_ALPHA = 0.6  # Move and scale gizmos opacity (matches native)
GIZMO_ARROW_LINE_WIDTH = 2.0  # Arrow line thickness (matches native GIZMO_AXIS_LINE_WIDTH)
GIZMO_DIAL_LINE_WIDTH = 3.0  # Rotation dial line thickness (native: GIZMO_AXIS_LINE_WIDTH + 1.0)
GIZMO_DIAL_ALPHA = 0.6  # Rotation dial normal opacity
GIZMO_DIAL_ALPHA_HIGHLIGHT = 1.0  # Rotation dial hover opacity

# Rotation angles for orienting gizmos (in degrees)
ROTATION_X_ARROW = 90  # Rotate around Y to point arrow along X
ROTATION_Y_ARROW = -90  # Rotate around X to point arrow along Y
ROTATION_X_DIAL = 90  # Rotate dial to face YZ plane
ROTATION_Y_DIAL = -90  # Rotate dial to face XZ plane

# Gizmo sizing
GIZMO_CENTER_BUTTON_SCALE = 0.2  # Center button scale (20% of standard)
GIZMO_VIEW_DIAL_SCALE = 1.2  # View dial scale (120% of standard, larger than trackball)
GIZMO_TRACKBALL_SCALE = 1.0  # Trackball dial scale (100% of standard)
GIZMO_PLANE_SCALE = 0.073  # Size of the 2D plane handles (calibrated by eye against native)
GIZMO_PLANE_OFFSET_PIXELS = 43  # Distance from center in screen pixels

# Trackball appearance (matches native behavior)
# Native trackball is invisible until hovered, then shows as subtle gray overlay
GIZMO_TRACKBALL_ALPHA = 0.0  # Trackball normal opacity (invisible until hover)
GIZMO_TRACKBALL_ALPHA_HIGHLIGHT = 0.05  # Trackball hover opacity (5% - subtle overlay)

# Property bounds
SCALE_SOFT_MAX = 10.0  # Soft maximum for UI slider

# Native scale behavior constants (matches Blender source: gizmo_library_utils.cc, transform_mode_resize.cc)
SCALE_PRECISION_FAC = 0.05    # 5% sensitivity in precision mode (native GIZMO_PRECISION_FAC)
SCALE_SNAP_INCREMENT = 0.1    # 10% snap increments (native t->snap[0])
SCALE_ZERO_EPSILON = 0.0001   # Prevent matrix singularity while allowing negative scale

# Rotation behavior
ROTATION_PRECISION_FAC = 0.05   # 5% sensitivity in precision mode (Shift), matches scale
TRACKBALL_SENSITIVITY = 0.01    # Pixels-to-radians factor for trackball rotation
TRACKBALL_PRECISION_FAC = 0.2   # Sensitivity multiplier in trackball precision mode (Shift)

# Move behavior
MOVE_PRECISION_FAC = 0.1        # 10% sensitivity in move precision mode (Shift)

# Debug mode: verbose logging + the invariant checks in cutting._debug_check
# raise loudly + on_internal_error() re-raises swallowed exceptions. Enable by
# setting the environment variable CUT_PLANE_DEBUG=1 before starting Blender
# (no source edit needed); tests set constants.DEBUG directly.
DEBUG = os.environ.get("CUT_PLANE_DEBUG", "").strip().lower() not in {"", "0", "false", "off"}
logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)


def on_internal_error() -> None:
    """Surface a swallowed internal error during development.

    Call from an ``except`` block whose production behavior is to swallow -
    a preview/callback hiccup must never break the user's action. Under DEBUG
    this re-raises the active exception so the bug crashes loudly with a full
    traceback; in production it does nothing, so production behavior is byte-for-
    byte unchanged. (Bare ``raise`` re-raises the exception currently being
    handled, which stays active while this is called from within the except.)
    """
    if DEBUG:
        raise

# Cached axis vectors (avoid repeated Vector creation)
AXIS_X = Vector((1, 0, 0))
AXIS_Y = Vector((0, 1, 0))
AXIS_Z = Vector((0, 0, 1))

# Axis string to vector mapping (for constraint_axis lookups)
AXIS_VECTOR_MAP = {'X': AXIS_X, 'Y': AXIS_Y, 'Z': AXIS_Z}

# Arrow hiding thresholds (matches native Blender - transform_gizmo_3d.cc:95-104)
# Uses inverted dot product: idot = 1.0 - abs(axis.dot(view_dir))
# idot = 0: Arrow parallel to view (should hide)
# idot = 1: Arrow perpendicular to view (fully visible)
ARROW_HIDE_THRESHOLD_MIN = 0.02  # Hide arrows when idot < this (parallel to view)
ARROW_HIDE_THRESHOLD_MAX = 0.1   # Fully visible when idot > this

# Gizmo positioning constants
ARROW_OFFSET_PIXELS = 16.0  # Distance to push arrows outside center circle (in screen pixels)
ANGLE_WRAP_THRESHOLD_RADIANS = math.pi / 2  # 90° threshold for rotation wrap-around detection

# Drawing constants
ARC_RADIUS_PIXELS = 75.0  # Rotation arc radius in screen pixels
TICK_INNER_FACTOR = 1.1   # Inner radius factor for snap tick marks
TICK_OUTER_FACTOR = 1.21  # Outer radius factor for snap tick marks

# Workspace tool identifier - single source shared by tool.py (bl_idname)
# and every tool-active check (math_utils.is_plane_tool_active, which feeds
# the gizmo poll and the cutting watchdog), so they never drift.
TOOL_IDNAME = "mesh_tool.cut_plane"

# Cutting tool: live-bisect preview. Above this
# face count the per-move live bisect is skipped to keep the viewport
# responsive (Execute Cut still works - it is one-shot, not per-frame).
CUT_PREVIEW_MAX_FACES = 300000

# Numerical precision constants
ZERO_EPSILON = 1e-10      # Threshold for near-zero checks (axis length, etc.)
PARALLEL_EPSILON = 1e-6   # Threshold for parallel line/plane checks


# Cached constant rotation matrices (computed once at module load)
# These are used for arrow/dial positioning and don't change during runtime
_CACHED_ROTATION_ARROW_X = Matrix.Rotation(math.radians(ROTATION_X_ARROW), 4, 'Y')
_CACHED_ROTATION_ARROW_Y = Matrix.Rotation(math.radians(ROTATION_Y_ARROW), 4, 'X')
_CACHED_ROTATION_DIAL_X = Matrix.Rotation(math.radians(ROTATION_X_DIAL), 4, 'Y')
_CACHED_ROTATION_DIAL_Y = Matrix.Rotation(math.radians(ROTATION_Y_DIAL), 4, 'X')