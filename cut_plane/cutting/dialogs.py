# SPDX-License-Identifier: GPL-3.0-or-later
"""Is an operator's file browser open? The preview pauses while one is.

The preview lives in the edit mesh, and exporters read the edit mesh: with
a preview showing, File > Export > STL, PLY, USD, Alembic and Collada wrote
the uncommitted cut (measured on 4.5, 5.1 and 5.2), while OBJ, glTF and FBX -
which leave Edit Mode first - and a save got the real mesh. So while a
browser that belongs to an operator is open (any export, import or save-as
dialog), the original is restored and nothing re-applies the preview; it
comes back when the browser closes (preview.pause_for_dialog,
watchdog._dialog_watch).

Exports that open no browser - File > Export All Collections, a
collection's Export button, scripts - give no warning and still read what
the viewport shows.
"""

import bpy

# File Browser areas seen with an operator attached, by as_pointer().
_browser_areas = set()


def operator_file_browser_open() -> bool:
    """True while an operator's file browser is open.

    Pressing Export detaches the operator from its browser one event-loop
    turn BEFORE the export runs; the area itself closes in the same call
    that runs it. Keyed on the operator alone, a watch turn inside that gap
    resumed the preview and the file got the cut (measured through the real
    dialog on 4.5, 5.1 and 5.2). So an area seen with an operator stays
    latched for as long as it remains a File Browser. A File Browser editor
    kept in a workspace never had an operator and pauses nothing.
    """
    wm = bpy.context.window_manager
    if wm is None:
        return False
    latched = set()
    for window in wm.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != 'FILE_BROWSER':
                continue
            pointer = area.as_pointer()
            if (pointer in _browser_areas
                    or getattr(area.spaces.active, 'active_operator', None) is not None):
                latched.add(pointer)
    _browser_areas.clear()
    _browser_areas.update(latched)  # closed areas drop out; their addresses get reused
    return bool(latched)


def forget_browsers() -> None:
    """Drop the latch while nothing is watching, so a later File Browser
    editor that reuses a latched area's address is not taken for a dialog."""
    _browser_areas.clear()
