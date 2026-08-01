# SPDX-License-Identifier: GPL-3.0-or-later
"""Development loader for the cut_plane addon package.

Open this file in Blender's text editor and press Run Script to (re)load
the addon straight from this source folder - no install or zip step.
Safe to re-run after every edit: it unregisters the previous instance,
purges the cached modules, and registers a fresh import.
"""
import os
import sys
import traceback

import bpy

# Last-resort candidate when the text block carries no usable path (e.g. a
# copy-pasted UNSAVED text datablock): set this to your checkout directory.
FALLBACK_DIR = ""


def _find_addon_dir():
    """Locate the folder containing the cut_plane package.

    Blender gives text blocks a fake __file__ (just the text name, e.g.
    "\\dev_load.py"), so every candidate is verified to actually contain
    the package before being trusted.
    """
    candidates = []
    try:
        candidates.append(os.path.dirname(os.path.abspath(__file__)))
    except NameError:
        pass
    # The text editor datablock knows the real path of an external file
    try:
        text = bpy.context.space_data.text
        if text is not None and text.filepath:
            candidates.append(os.path.dirname(bpy.path.abspath(text.filepath)))
    except AttributeError:
        pass
    candidates.append(FALLBACK_DIR)

    for path in candidates:
        if os.path.isdir(os.path.join(path, "cut_plane")):
            return path
    raise RuntimeError(
        f"cut_plane package not found next to dev_load.py (tried: {candidates})"
    )


ADDON_DIR = _find_addon_dir()
if ADDON_DIR not in sys.path:
    sys.path.insert(0, ADDON_DIR)

_previous = sys.modules.get("cut_plane")
if _previous is not None:
    try:
        _previous.unregister()
    except Exception:
        traceback.print_exc()
    # Purge all package modules so the fresh import picks up every edit
    for _name in [n for n in list(sys.modules)
                  if n == "cut_plane" or n.startswith("cut_plane.")]:
        del sys.modules[_name]

import cut_plane  # noqa: E402

cut_plane.register()
print(f"[CUT PLANE] dev reload complete (from {ADDON_DIR})")
