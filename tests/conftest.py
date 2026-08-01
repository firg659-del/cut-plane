# SPDX-License-Identifier: GPL-3.0-or-later
"""Pytest bootstrap: make the addon's pure logic importable without Blender.

Two problems to solve for headless tests:

1. The package's ``__init__.py`` imports the full Blender stack (gizmo/operator
   classes, GPU drawing) and would fail outside Blender. We must NOT run it.
   Injecting a bare ``cut_plane`` package object (with ``__path__`` but no
   code) into ``sys.modules`` lets ``import cut_plane.<submodule>`` resolve a
   submodule directly while the heavy ``__init__.py`` never executes.

2. Submodules still ``import bpy`` / ``bmesh`` / ``bpy_extras``. Outside Blender
   these don't exist, so we install lightweight stubs - but ONLY when the real
   modules are absent, so the very same tests run unmodified under Blender's own
   Python (where the real modules take precedence).

``mathutils`` is deliberately NOT stubbed: the linear algebra is the thing under
test, and a fake would make those tests meaningless. Tests that need it call
``pytest.importorskip("mathutils")`` and skip cleanly on a bare interpreter -
run them under Blender's bundled Python (see tests/README.md).
"""
import pathlib
import sys
import types
from types import SimpleNamespace

_PKG_ROOT = pathlib.Path(__file__).resolve().parent.parent  # cut_plane_addon/
_PKG_DIR = _PKG_ROOT / "cut_plane"

if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))


def _install_blender_stubs() -> None:
    """Provide just-enough stand-ins for the Blender modules our targets import.

    No-op when running inside Blender (the real ``bpy`` is already imported).
    """
    if "bpy" in sys.modules:
        return  # running under Blender - use the real modules

    def _to_value(system, category, text, str_ref_unit=""):
        # Real-API signature (str_ref_unit is keyword-only in Blender). No
        # unit engine offline; raise so NumericInput falls back to float().
        raise AttributeError("units engine unavailable in tests")

    bpy = types.ModuleType("bpy")
    bpy.utils = SimpleNamespace(units=SimpleNamespace(to_value=_to_value))
    bpy.context = SimpleNamespace(
        window_manager=SimpleNamespace(clipboard=""),
        scene=SimpleNamespace(unit_settings=SimpleNamespace(system="METRIC")),
    )
    bpy.app = SimpleNamespace(
        handlers=SimpleNamespace(persistent=(lambda fn: fn)),
        version=(5, 1, 1),
    )
    bpy.props = SimpleNamespace()
    sys.modules["bpy"] = bpy

    bmesh = types.ModuleType("bmesh")
    bmesh.types = SimpleNamespace(
        BMEdge=type("BMEdge", (), {}), BMVert=type("BMVert", (), {}))
    bmesh.ops = SimpleNamespace()
    sys.modules["bmesh"] = bmesh

    bpy_extras = types.ModuleType("bpy_extras")
    bpy_extras.__path__ = []  # mark as a package
    sys.modules["bpy_extras"] = bpy_extras
    sys.modules["bpy_extras.view3d_utils"] = types.ModuleType(
        "bpy_extras.view3d_utils")


def _inject_bare_package() -> None:
    """Register ``cut_plane`` as a package WITHOUT running its __init__.py."""
    if "cut_plane" in sys.modules:
        return
    pkg = types.ModuleType("cut_plane")
    pkg.__path__ = [str(_PKG_DIR)]
    sys.modules["cut_plane"] = pkg


_install_blender_stubs()
_inject_bare_package()
