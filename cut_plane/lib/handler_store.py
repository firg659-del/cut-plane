# SPDX-License-Identifier: GPL-3.0-or-later
"""Leak-proof storage for GPU draw-handler handles.

A draw handler added with SpaceView3D.draw_handler_add can ONLY be removed by
passing back the exact handle object it returned. Lose that handle and the
handler draws forever - there is no API to enumerate or clear them, and the
only cure is restarting Blender. So where the handle is kept IS the bug
surface, and both of the obvious homes are individually wrong:

  * A module-level global is lost when the module is re-executed without an
    unregister first (a dev-loop reload) - the old handler keeps drawing and
    the next register adds a second one.
  * bpy.app.driver_namespace survives that, which is why this addon used it -
    but Blender WIPES driver_namespace on every file load. After a File >
    New / File > Open, unregister() popped None, removed nothing, and every
    handler leaked; each disable/enable cycle then stacked another set,
    unboundedly. (Measured, and the code's own comment had it backwards: it
    assumed Blender removes the handler on a file reload. It does not - it
    only forgets the reference.)

Keeping BOTH covers each other's blind spot: the module global survives a
file load, the driver_namespace mirror survives a module re-exec, and either
one is enough to remove the handler. Callers hold their handles here instead
of in their own globals.
"""

import bpy

from .constants import logger

# Handle per key, in THIS module's namespace: survives file loads, because a
# file load does not re-import Python modules.
_handles = {}


def add(key: str, adder, *args):
    """Add a draw handler, first removing any predecessor stored under `key`.

    `adder` is the draw_handler_add callable (e.g.
    bpy.types.SpaceView3D.draw_handler_add); `args` are passed to it. Both
    stores are written, and both are consulted on removal.
    """
    remove(key)
    handle = adder(*args)
    _handles[key] = handle
    bpy.app.driver_namespace[key] = handle
    return handle


def remove(key: str) -> bool:
    """Remove the handler stored under `key`. True if one was removed.

    Tries every stored reference: after a file load the driver_namespace copy
    is gone but the module global still holds the live handle, and after a
    module re-exec it is the other way round. Removing an already-removed
    handle raises, which is expected and harmless here - the goal is only
    that no live handler survives with its handle lost.
    """
    handles = []
    for h in (_handles.pop(key, None), bpy.app.driver_namespace.pop(key, None)):
        if h is not None and not any(h is seen for seen in handles):
            handles.append(h)
    removed = False
    for handle in handles:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(handle, 'WINDOW')
            removed = True
        except Exception:
            # Already gone (both stores held the same handle, or Blender
            # dropped it) - nothing left to leak either way.
            pass
    return removed


def get(key: str):
    """The stored handle, or None. For tests and diagnostics."""
    return _handles.get(key, bpy.app.driver_namespace.get(key))


def clear_all() -> None:
    """Remove every handler this store knows about (addon unregister)."""
    for key in list(_handles) + [k for k in list(bpy.app.driver_namespace)
                                 if k.startswith("__cut_plane_")]:
        try:
            remove(key)
        except Exception:
            logger.exception("[CUT PLANE] draw handler removal failed: %s", key)
