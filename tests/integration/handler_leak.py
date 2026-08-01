# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless test: GPU draw handlers are never orphaned.

Runs inside Blender:

    blender --background --factory-startup --python-exit-code 1 \
            --python tests/integration/handler_leak.py

Contract: a draw handler can ONLY be removed with the exact handle object
draw_handler_add returned. Lose the handle and the handler draws forever -
Blender offers no way to enumerate or clear them, so the only cure is a
restart. Therefore unregister() must remove every handler the addon added,
under every reachable session history.

The bug this locks: the handles lived ONLY in bpy.app.driver_namespace, which
Blender WIPES on every file load. After File > New, unregister() popped None
and removed nothing - all three handlers (arc, pixel overlay, live-cut
watchdog) stayed live with their handles lost, and every disable/enable cycle
stacked another set, unboundedly. The code's own comment had the mechanism
backwards ("Handler may have been removed by Blender (e.g. file reload)"):
Blender does not remove the handler, it only forgets the reference.

Detection trick: draw_handler_remove SUCCEEDS on a live handler and RAISES on
an already-removed one. So re-removing a saved handle after unregister tells
us whether unregister really removed it.

Exit code 0 on success, 1 with the failed checks listed.
"""
import os
import sys

import bpy

ADDON = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)

import cut_plane  # noqa: E402
from cut_plane import cutting  # noqa: E402
from cut_plane.lib import handler_store  # noqa: E402
from cut_plane.ui import drawing  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"[{'ok  ' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


KEYS = (drawing._ARC_HANDLER_KEY, drawing._PIXEL_HANDLER_KEY,
        cutting._WATCHDOG_KEY)


def still_live(handle):
    """True if `handle` was still installed (and is now removed)."""
    if handle is None:
        return False
    try:
        bpy.types.SpaceView3D.draw_handler_remove(handle, 'WINDOW')
        return True
    except Exception:
        return False


# --- baseline: register stores a handle for every handler ------------------
cut_plane.register()
check("register stores a handle for all three draw handlers",
      all(handler_store.get(k) is not None for k in KEYS),
      str([k for k in KEYS if handler_store.get(k) is None]))

# --- THE REGRESSION: a file load must not orphan them ----------------------
saved = {k: handler_store.get(k) for k in KEYS}
bpy.ops.wm.read_homefile(use_empty=True)   # File > New: the ordinary action

check("a file load wipes driver_namespace (the reason the old store failed)",
      all(k not in bpy.app.driver_namespace for k in KEYS),
      str([k for k in KEYS if k in bpy.app.driver_namespace]))
check("...but the handles are still reachable after it",
      all(handler_store.get(k) is not None for k in KEYS),
      str([k for k in KEYS if handler_store.get(k) is None]))

cut_plane.unregister()
leaked = [k for k, h in saved.items() if still_live(h)]
check("REGRESSION: unregister removes every handler even after a file load",
      not leaked, f"leaked: {leaked}")
check("...and the store is left empty",
      all(handler_store.get(k) is None for k in KEYS))

# --- a module re-exec without unregister must not stack duplicates ---------
# The case the driver_namespace mirror exists for, exercised on the store
# itself: cut_plane.register() is deliberately not idempotent for classes, so
# calling it twice would only prove Blender rejects a double register_class.
KEY = "__cut_plane_test_handler__"


def _noop_draw():
    pass


first = handler_store.add(KEY, bpy.types.SpaceView3D.draw_handler_add,
                          _noop_draw, (), 'WINDOW', 'POST_VIEW')
handler_store._handles.clear()             # the module globals are gone
check("the driver_namespace mirror still reaches the handler",
      handler_store.get(KEY) is first)
handler_store.add(KEY, bpy.types.SpaceView3D.draw_handler_add,
                  _noop_draw, (), 'WINDOW', 'POST_VIEW')
check("a re-add without the module global does not stack a duplicate",
      not still_live(first), "the predecessor was left installed")
handler_store.remove(KEY)
check("remove clears both stores",
      handler_store.get(KEY) is None
      and KEY not in bpy.app.driver_namespace)

# --- repeated cycles must not accumulate ----------------------------------
survivors = []
for _ in range(3):
    cut_plane.register()
    handles = {k: handler_store.get(k) for k in KEYS}
    bpy.ops.wm.read_homefile(use_empty=True)
    cut_plane.unregister()
    survivors += [k for k, h in handles.items() if still_live(h)]
check("three enable/file-load/disable cycles leak nothing",
      not survivors, f"leaked: {survivors}")

check("clear_all is a safe no-op once everything is removed",
      handler_store.clear_all() is None)

# --- foreign same-named handlers are never removed (moderation rule) --------
# The register/unregister dedup matches stale reload copies by __name__ AND
# module path. A FOREIGN add-on's handler that happens to share one of our
# generic names must survive our whole lifecycle untouched - removing it
# would be exactly the "interfering with other add-ons" violation.


def handle_orientation_or_mode_change(scene):  # the foreign impostor
    pass


def _save_tool_state(*_a):
    pass


def _load_tool_state(*_a):
    pass


# Impostors for the CUT-SESSION handler names too: their dedup once matched
# by __name__ alone, so a foreign handler sharing one of these generic names
# was silently stripped from bpy.app.handlers by our register()/unregister().
def _on_save_pre(*_a):
    pass


def _on_save_post(*_a):
    pass


def _on_load_post(*_a):
    pass


def _on_undo_redo_post(*_a):
    pass


_FOREIGN = (
    (bpy.app.handlers.depsgraph_update_post, handle_orientation_or_mode_change),
    (bpy.app.handlers.save_pre, _save_tool_state),
    (bpy.app.handlers.load_post, _load_tool_state),
    (bpy.app.handlers.save_pre, _on_save_pre),
    (bpy.app.handlers.save_post, _on_save_post),
    (bpy.app.handlers.load_post, _on_load_post),
    (bpy.app.handlers.undo_post, _on_undo_redo_post),
    (bpy.app.handlers.redo_post, _on_undo_redo_post),
)
for _lst, _fn in _FOREIGN:
    _fn.__module__ = "totally_foreign_addon.core"
    _lst.append(_fn)

cut_plane.register()
kept_register = [f.__name__ for lst, f in _FOREIGN if f not in lst]
cut_plane.unregister()
kept_unregister = [f.__name__ for lst, f in _FOREIGN if f not in lst]
for _lst, _fn in _FOREIGN:
    if _fn in _lst:
        _lst.remove(_fn)
check("foreign same-named handlers survive our register()",
      not kept_register, f"removed: {kept_register}")
check("...and survive our unregister()",
      not kept_unregister, f"removed: {kept_unregister}")

# ...while OUR OWN stale copies still get deduped (same name, our module)
from cut_plane.state.properties import handle_orientation_or_mode_change as _real_h  # noqa: E402
_dg = bpy.app.handlers.depsgraph_update_post
_dg.append(_real_h)  # simulate a stale copy surviving a dev reload
cut_plane.register()
_count = sum(1 for h in _dg
             if getattr(h, "__name__", "") == "handle_orientation_or_mode_change")
check("our own stale copy IS still deduped (exactly one instance)",
      _count == 1, f"count={_count}")
cut_plane.unregister()

# ...same for a stale CUT-SESSION handler copy (the module-scoped dedup must
# still collapse our own duplicates, or dev reloads stack save_pre handlers)
_sp = bpy.app.handlers.save_pre
_sp.append(cutting._on_save_pre)  # simulate a stale copy surviving a dev reload
cut_plane.register()
_count = sum(1 for h in _sp
             if getattr(h, "__name__", "") == "_on_save_pre"
             and getattr(h, "__module__", "").startswith("cut_plane"))
check("our own stale cut-session copy IS still deduped (exactly one instance)",
      _count == 1, f"count={_count}")
cut_plane.unregister()

print(f"\nHANDLER LEAK: {'FAIL - ' + ', '.join(failures) if failures else 'OK'}")
sys.stdout.flush()
os._exit(1 if failures else 0)
