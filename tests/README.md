# Tests

Headless unit tests for the addon's **pure logic** — the parts that don't need a
live Blender viewport. They exist to catch regressions in the fiddly bits
(numeric type-in, the orientation invariant, matrix round-trips, snap rounding)
without loading Blender and clicking.

## Layout

| File | Needs `mathutils`? | Covers |
|------|--------------------|--------|
| `test_numeric_input.py` | no | the numeric type-in state machine (buffer edits, sign/inverse, expression mode, backspace stages, paste, multi-value Tab) |
| `test_math_utils.py` | **yes** | the ROTATE-orientation invariant, alpha smoothstep, matrix↔flat round-trip, dial basis orthonormality, offset transform |
| `test_snapping.py` | **yes** | `round_vector_to_step` |

`conftest.py` makes this work outside Blender: it injects a bare `cut_plane`
package (so the heavy `__init__.py` never runs) and stubs `bpy` / `bmesh` /
`bpy_extras` **only when they're absent**. `mathutils` is never stubbed — a fake
would make those tests meaningless — so the math tests `importorskip` and skip on
a bare interpreter.

## Running

### Bare Python (pure subset — fast, no Blender)
```sh
cd cut_plane_addon
python -m pip install pytest      # once
python -m pytest                  # math/snapping tests SKIP (no mathutils)
```

### Under Blender's Python (full suite)
Blender's bundled interpreter has real `bpy` **and** `mathutils`, so nothing
skips. Install pytest into it once, then run:
```sh
# adjust the path to your Blender install
"<blender>/<ver>/python/bin/python" -m ensurepip
"<blender>/<ver>/python/bin/python" -m pip install pytest
"<blender>/<ver>/python/bin/python" -m pytest cut_plane_addon/tests -q
```
or headless through Blender itself:
```sh
blender --background --python cut_plane_addon/tests/run_in_blender.py
```

## Scope (pytest suite)

The pytest files above are **not** integration tests — they don't drive gizmos
or run operators. They lock down the deterministic, side-effect-free helpers so
refactors there fail loudly instead of silently.

## Integration soak / fuzz test (`integration/soak.py`)

A headless fuzz test for the **live-cut state machine** — the cut/snapshot/cache/
undo-handler/save-load area where the nastiest bugs have lived. It registers the
addon with `DEBUG` on, builds a mesh in Edit Mode, then fires thousands of
randomized operations (cut, move/rotate plane, toggle clear/fill/select, reset,
mode/orientation switch, simulated undo/redo + save round-trips, restore,
selection churn). After **every** operation it asserts:

- no exception escaped,
- `cutting.check_invariants()` is empty (cache stays consistent), and
- after simulating the deferred purge timer, ≤ 1 snapshot datablock survives.

```sh
# default 2000 ops, random seed (printed so failures reproduce)
blender --background --python cut_plane_addon/tests/integration/soak.py
# explicit iters + seed:
blender --background --python cut_plane_addon/tests/integration/soak.py -- 5000 13
```

It backs the **self-instrumentation** in the `cutting` package: `check_invariants()`
(pure, returns violations) + `_debug_check()` (raises under `DEBUG` at each
state-settling point, so interactive dev also crashes loudly on a broken
invariant). Validated by fault injection — disabling the purge surfaces a leak,
corrupting the cache surfaces an invariant break. Modal drags, real Ctrl+Z, and
gizmo *visual* placement are still out of reach here (that's the MCP layer).
