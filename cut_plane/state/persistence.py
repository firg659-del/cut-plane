# SPDX-License-Identifier: GPL-3.0-or-later
"""Tool-state persistence: .blend save/load for the WindowManager group.

CutPlaneProperties lives on the WindowManager so undo - including the undo
half of F9 (adjust-last-operation) - can never move the plane. The cost:
WindowManager data is not saved into .blend files. These handlers bridge
that gap: save_pre serializes the group to JSON in a scene ID property;
load_post restores it, or resets everything to defaults when the loaded
file carries no stamp - a foreign file must never inherit the previous
file's plane pose.

The serializer walks the group's RNA instead of keeping a hand-written
field list, so a property added to CutPlaneProperties is persisted
automatically (the same coverage Scene storage used to give for free).
Restores go through ID-property subscript writes, which skip update
callbacks - restoring cut options on load must not fire live-preview
re-cuts mid-load.

Autosave caveat (measured 2026-07-11): autosave skips save_pre, so an
autosave .blend carries the stamp of the last MANUAL save. After a crash
recovery the plane pose may be slightly stale - acceptable for tool state
(the recovered MESH is protected separately, see cutting's orphan-snapshot
recovery).
"""

import json
import math

import bpy

from . import pose_history
from ..lib.constants import DEBUG, logger, on_internal_error

# Scene ID property holding the JSON stamp (scenes are saved; the WM is not)
_STATE_PROP = "cut_plane_tool_state"

# RNA metadata fields on the group that are not tool state
_NON_STATE = {'rna_type', 'name'}


def _get_props():
    """The tool-state group via bpy.data (handlers may lack a full context)."""
    wms = bpy.data.window_managers
    return getattr(wms[0], 'cut_plane', None) if wms else None


def _state_props(props):
    """RNA definitions of every persisted property on the group."""
    return (p for p in props.bl_rna.properties
            if p.identifier not in _NON_STATE)


def _state_dict(props) -> dict:
    """Group -> plain dict. Enums as identifiers (readable, reorder-proof),
    vectors as lists."""
    state = {}
    for p in _state_props(props):
        value = getattr(props, p.identifier)
        if p.type == 'ENUM':
            state[p.identifier] = sorted(value) if p.is_enum_flag else value
        elif getattr(p, 'is_array', False):
            state[p.identifier] = list(value)
        else:
            state[p.identifier] = value
    return state


def _serialize(props) -> str:
    """Group -> JSON. json floats always use '.', locale be damned."""
    return json.dumps(_state_dict(props))


def _values_equal(a, b) -> bool:
    """Value equality that treats floats as float32-backed: RNA stores
    FLOAT properties in single precision, so a live value never compares
    exactly equal to its Python-double RNA default unless the default is
    float32-representable. Tolerant comparison, or the is-default check
    below could NEVER be true for such a property."""
    if isinstance(a, float) or isinstance(b, float):
        return math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-9)
    if isinstance(a, list) and isinstance(b, list):
        return (len(a) == len(b)
                and all(_values_equal(x, y) for x, y in zip(a, b)))
    return a == b


def _is_default_state(state: dict, props) -> bool:
    defaults = _defaults_state(props)
    return (state.keys() == defaults.keys()
            and all(_values_equal(state[k], defaults[k]) for k in state))


def _default_for(p):
    """One property's RNA default, in the shape _serialize produces."""
    if p.type == 'ENUM':
        return sorted(p.default_flag) if p.is_enum_flag else p.default
    if getattr(p, 'is_array', False):
        return list(p.default_array)
    return p.default


def _defaults_state(props) -> dict:
    """The group's RNA defaults, in the same shape _serialize produces."""
    return {p.identifier: _default_for(p) for p in _state_props(props)}


def _write_field(props, ident, p, value) -> None:
    """Write ONE field via its ID-property subscript, or raise.

    Raising on an unusable value is the point: the caller turns that into a
    reset to the RNA default. Subscript assignment skips update callbacks by
    design (see module docstring). Enum values map identifier -> int through
    the RNA items, because the ID-property backing stores the int.
    """
    if p.type == 'ENUM':
        items = p.enum_items
        if p.is_enum_flag:
            # Unknown members are dropped, not fatal: a stamp from a NEWER
            # version may legitimately carry flags this build has no name for.
            props[ident] = sum(items[i].value for i in value if i in items)
        else:
            props[ident] = items[value].value    # KeyError if not a valid id
    elif p.type == 'BOOLEAN':
        props[ident] = bool(value)
    elif getattr(p, 'is_array', False):
        seq = [float(v) for v in value]
        if len(seq) != p.array_length:
            raise ValueError(f"expected {p.array_length} values, got {len(seq)}")
        props[ident] = seq
    elif p.type == 'FLOAT':
        props[ident] = float(value)
    elif p.type == 'INT':
        props[ident] = int(value)
    elif p.type == 'STRING':
        props[ident] = str(value)


def _apply_state(props, state: dict) -> None:
    """Write a state dict into the group, resetting anything unusable.

    Every field lands on SOMETHING: the stamped value if it can be applied,
    the RNA default otherwise. That completeness is the whole contract -
    _load_tool_state seeds `state` with the defaults precisely so a field the
    stamp cannot supply resets instead of leaking the previously open file's
    value. Silently skipping an unusable field defeated that (measured: an
    invalid enum in the stamp left the previous file's orientation live).
    Unknown keys - a stamp from a different addon version - are skipped;
    there is no property to reset.
    """
    rna = props.bl_rna.properties
    for ident, value in state.items():
        p = rna.get(ident)
        if p is None or p.identifier in _NON_STATE:
            continue
        try:
            _write_field(props, ident, p, value)
        except Exception:
            # A stamp is EXTERNAL data (edited file, other addon version) - a
            # malformed field is not an internal bug, so never raise here.
            logger.warning(f"[PERSISTENCE] Malformed field '{ident}' - "
                           f"resetting to default")
            try:
                _write_field(props, ident, p, _default_for(p))
            except Exception:
                # The RNA default itself is unusable: nothing sane is left to
                # write, and the field keeps whatever it held.
                on_internal_error()


def _reset_runtime_trackers() -> None:
    """Fresh-file bookkeeping on the runtime group: empty orientation/mode
    trackers make the depsgraph handler's next tick RECORD state instead of
    converting offsets against the previous file's state, and clearing the
    centering latch lets the new file's virgin plane default-center."""
    wms = bpy.data.window_managers
    runtime = getattr(wms[0], 'cut_plane_runtime', None) if wms else None
    if runtime is None:
        return
    runtime.last_orientation = ''
    runtime.last_mode = ''
    runtime.centered_edit_uid = 0


@bpy.app.handlers.persistent
def _save_tool_state(*_args) -> None:
    """save_pre: stamp the WM tool state into the scene about to be saved.

    Only when there is something worth saying: a state equal to the RNA
    defaults writes NO stamp - and clears any stale one from every scene
    (the load path scans all scenes) - so a user who never posed the plane
    saves .blend files carrying no trace of this addon, and resetting the
    pose genuinely un-stamps a file on its next save."""
    props = _get_props()
    if props is None:
        return
    scene = getattr(bpy.context, 'scene', None)
    if scene is None:
        scenes = bpy.data.scenes
        scene = scenes[0] if scenes else None
    if scene is None:
        return
    try:
        state = _state_dict(props)
        if _is_default_state(state, props):
            for sc in bpy.data.scenes:
                if _STATE_PROP in sc.keys():
                    del sc[_STATE_PROP]
            if DEBUG:
                logger.debug("[PERSISTENCE] Default state - no stamp written")
        else:
            scene[_STATE_PROP] = json.dumps(state)
            if DEBUG:
                logger.debug(f"[PERSISTENCE] Tool state stamped into '{scene.name}'")
    except Exception:
        on_internal_error()  # DEBUG: surface; prod: never block the save


@bpy.app.handlers.persistent
def _load_tool_state(*_args) -> None:
    """load_post: restore the stamp, or reset to defaults when there is none.

    Defaults are applied FIRST and the stamp merged on top: a stamp written
    by an older addon version misses newer properties, and those must still
    reset rather than leak values from the previously open file.
    """
    props = _get_props()
    if props is None:
        return
    raw = None
    scene = getattr(bpy.context, 'scene', None)
    if scene is not None:
        raw = scene.get(_STATE_PROP)
    if raw is None:
        # Active scene unstamped (or context-less load) - any scene's will do
        for candidate in bpy.data.scenes:
            raw = candidate.get(_STATE_PROP)
            if raw is not None:
                break
    state = _defaults_state(props)
    if raw is not None:
        try:
            state.update(json.loads(raw))
        except Exception:
            # External data again: a corrupt stamp degrades to defaults
            logger.warning("[PERSISTENCE] Corrupt tool-state stamp - "
                           "falling back to defaults")
    _apply_state(props, state)
    _reset_runtime_trackers()
    # Pose-undo history belongs to the previous file's timeline
    pose_history.clear()
    if DEBUG:
        logger.debug(f"[PERSISTENCE] Tool state {'restored' if raw else 'reset'}")


_HANDLERS = (
    ("save_pre", _save_tool_state),
    ("load_post", _load_tool_state),
)


# Ownership anchor = the addon ROOT package (one level up from this state/
# subpackage; the full bl_ext.<repo>.cut_plane path when installed as an
# extension). Anchoring at the root, not __package__, keeps stale copies from
# an OLDER layout matched (these handlers lived at cut_plane.persistence
# before the state/ move); the dot boundary keeps a foreign "cut_plane_tools"
# module from ever matching.
_ADDON_ROOT = __package__.rsplit(".", 1)[0]


def _is_ours(handler, name: str) -> bool:
    """True when `handler` is a copy of OUR handler `name`: matched by
    __name__ (reloads create fresh function objects, so identity fails) AND
    by module path - a foreign add-on's handler that happens to share a
    generic name like "_save_tool_state" must never be touched."""
    mod = getattr(handler, "__module__", "")
    return (getattr(handler, "__name__", None) == name
            and (mod == _ADDON_ROOT or mod.startswith(_ADDON_ROOT + ".")))


def register_persistence_handlers() -> None:
    """Register the save/load handlers (idempotent; stale reload copies are
    removed first - see _is_ours for the matching rule)."""
    for list_name, fn in _HANDLERS:
        handlers = getattr(bpy.app.handlers, list_name)
        handlers[:] = [h for h in handlers if not _is_ours(h, fn.__name__)]
        handlers.append(fn)


def unregister_persistence_handlers() -> None:
    """Remove the handlers (same matching rule clears stale copies too)."""
    for list_name, fn in _HANDLERS:
        handlers = getattr(bpy.app.handlers, list_name)
        handlers[:] = [h for h in handlers if not _is_ours(h, fn.__name__)]
