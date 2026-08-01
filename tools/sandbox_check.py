"""Post-install verification for the built extension zip.

Run inside Blender, against a THROWAWAY user-resources directory, after
installing the zip with:

    BLENDER_USER_RESOURCES=<tmp> blender --command extension install-file \
        -r user_default --enable cut_plane-1.0.0.zip
    BLENDER_USER_RESOURCES=<tmp> blender --background \
        --python tools/sandbox_check.py

The point is to catch what a build cannot: the extension installing under a
different module path than the source tree, so identity-sensitive lookups
(addon preferences, handler ownership, keymap owners) silently resolve to
nothing. A formally-enabled addon whose preferences answer with defaults
looks fine until a user changes a setting and it does not take.

Exits non-zero on the first failure, and prints a PASS/FAIL line per check.
"""
import sys
import traceback

import bpy

ROOT = "bl_ext.user_default.cut_plane"

_results = []


def check(name):
    """Register a predicate as a named check. The function returns
    True/False, or raises - a raise is reported as a failure with its
    traceback rather than aborting the whole run."""
    def deco(fn):
        try:
            ok = bool(fn())
            detail = ""
        except Exception:
            ok = False
            detail = traceback.format_exc(limit=3).strip().splitlines()[-1]
        _results.append((name, ok, detail))
        return fn
    return deco


# --- the extension is actually enabled, under the expected module path -----

@check("extension is enabled")
def _():
    return ROOT in {a.module for a in bpy.context.preferences.addons}


@check("package imports under its installed name")
def _():
    __import__(ROOT)
    return True


@check("package __package__ matches the install path")
def _():
    mod = sys.modules[ROOT]
    return mod.__package__ == ROOT


# --- identity: preferences must resolve, not fall back to defaults ---------

@check("addon preferences resolve (not a silent default)")
def _():
    return bpy.context.preferences.addons[ROOT].preferences is not None


@check("get_prefs() returns the live preferences object")
def _():
    prefs_mod = __import__(ROOT + ".state.preferences", fromlist=["x"])
    live = bpy.context.preferences.addons[ROOT].preferences
    got = prefs_mod.get_prefs()
    # bpy hands back a fresh Python wrapper on every access, so `is` would
    # always be False - compare the underlying RNA pointer instead.
    return got is not None and got.as_pointer() == live.as_pointer()


@check("AddonPreferences.bl_idname equals the root package")
def _():
    prefs_mod = __import__(ROOT + ".state.preferences", fromlist=["x"])
    cls = prefs_mod.CutPlaneAddonPreferences
    return cls.bl_idname == ROOT


@check("preview face cap is readable from preferences")
def _():
    prefs_mod = __import__(ROOT + ".state.preferences", fromlist=["x"])
    return isinstance(prefs_mod.preview_face_cap(), int)


# --- registration ---------------------------------------------------------

@check("window-manager property groups registered")
def _():
    wm = bpy.types.WindowManager
    return hasattr(wm, "cut_plane") and hasattr(wm, "cut_plane_runtime")


@check("workspace tool is registered")
def _():
    from bl_ui.space_toolsystem_common import ToolSelectPanelHelper
    cls = ToolSelectPanelHelper._tool_class_from_space_type('VIEW_3D')
    found = []
    for item in ToolSelectPanelHelper._tools_flatten(
            cls.tools_from_context(bpy.context, mode='EDIT_MESH')):
        if item is not None and getattr(item, "idname", None):
            found.append(item.idname)
    return "mesh_tool.cut_plane" in found


@check("all five modal operators registered")
def _():
    ops = ("cut_plane_move", "cut_plane_rotate", "cut_plane_scale",
           "cut_plane_cut", "cut_plane_reset")
    return all(hasattr(bpy.ops.view3d, o) for o in ops)


@check("undo / redo operators registered")
def _():
    return (hasattr(bpy.ops.view3d, "cut_plane_undo")
            and hasattr(bpy.ops.view3d, "cut_plane_redo"))


@check("gizmo group and plane visual registered")
def _():
    # Gizmos are NOT exposed on bpy.types by bl_idname the way operators are -
    # bl_rna_get_subclass_py is the supported lookup, returning None if the
    # class was never registered.
    return (bpy.types.GizmoGroup.bl_rna_get_subclass_py(
                "OBJECT_GGT_plane_3_axis") is not None
            and bpy.types.Gizmo.bl_rna_get_subclass_py(
                "VIEW3D_GT_plane_visual") is not None)


@check("N-panel sections registered")
def _():
    return (hasattr(bpy.types, "CUT_PLANE_PT_cut_options")
            and hasattr(bpy.types, "CUT_PLANE_PT_snapping"))


# --- handlers: exactly one of each, owned by this package -----------------

@check("no duplicate handlers, and every one is ours")
def _():
    import bpy.app.handlers as H
    names = ("_on_save_pre", "_on_save_post", "_on_load_post",
             "_on_undo_redo_post")
    for lst in (H.save_pre, H.save_post, H.load_post,
                H.undo_post, H.redo_post, H.depsgraph_update_post):
        seen = {}
        for h in lst:
            n = getattr(h, "__name__", "")
            m = getattr(h, "__module__", "")
            if n in names or m.startswith(ROOT):
                if not (m == ROOT or m.startswith(ROOT + ".")):
                    return False            # an impostor from another module
                seen[n] = seen.get(n, 0) + 1
        if any(c > 1 for c in seen.values()):
            return False                    # a stale duplicate survived reload
    return True


# --- the shipped payload --------------------------------------------------

@check("manifest ships and declares the clipboard permission")
def _():
    import os
    base = os.path.dirname(sys.modules[ROOT].__file__)
    with open(os.path.join(base, "blender_manifest.toml"), encoding="utf-8") as fh:
        text = fh.read()
    return "[permissions]" in text and "clipboard" in text


@check("LICENSE ships in the installed package")
def _():
    import os
    base = os.path.dirname(sys.modules[ROOT].__file__)
    return os.path.isfile(os.path.join(base, "LICENSE"))


@check("DOCUMENTATION.md ships in the installed package")
def _():
    import os
    base = os.path.dirname(sys.modules[ROOT].__file__)
    return os.path.isfile(os.path.join(base, "DOCUMENTATION.md"))


@check("guide explains the cut is an infinite plane")
def _():
    import os
    base = os.path.dirname(sys.modules[ROOT].__file__)
    with open(os.path.join(base, "DOCUMENTATION.md"), encoding="utf-8") as fh:
        text = fh.read()
    return "infinite plane" in text and "viewing aid" in text


@check("guide documents right-click as a cancel")
def _():
    # modal_base cancels on {'RIGHTMOUSE', 'ESC'}; the guide listed only Esc,
    # which is the kind of omission that survives unnoticed for a long time
    import os
    base = os.path.dirname(sys.modules[ROOT].__file__)
    with open(os.path.join(base, "DOCUMENTATION.md"), encoding="utf-8") as fh:
        text = fh.read().lower()
    return "right-click cancels" in text or "right-click" in text and "cancel" in text


@check("support address matches across manifest and guide")
def _():
    import os
    import re
    base = os.path.dirname(sys.modules[ROOT].__file__)
    found = []
    for name in ("blender_manifest.toml", "DOCUMENTATION.md"):
        with open(os.path.join(base, name), encoding="utf-8") as fh:
            found += re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", fh.read())
    return len(found) >= 2 and len(set(found)) == 1


@check("CHANGELOG.md ships in the installed package")
def _():
    import os
    base = os.path.dirname(sys.modules[ROOT].__file__)
    return os.path.isfile(os.path.join(base, "CHANGELOG.md"))


@check("no legacy bl_info (extensions-only install)")
def _():
    return not hasattr(sys.modules[ROOT], "bl_info")


# --- report ---------------------------------------------------------------

failed = 0
for name, ok, detail in _results:
    print(("PASS  " if ok else "FAIL  ") + name + (("  -- " + detail) if detail else ""))
    if not ok:
        failed += 1

print("\n%d/%d checks passed" % (len(_results) - failed, len(_results)))
sys.exit(1 if failed else 0)
