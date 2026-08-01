# Changelog

User-facing changes to Cut Plane. Versions match `blender_manifest.toml`;
entries are written to be paste-ready for store update notes.

## 1.0.0 — 2026-07-22
Initial public release.
- Live destructive cut preview: Fill, Clear Inner, Clear Outer — see the
  exact result before committing with Enter.
- Full plane gizmo (move arrows, X/Y/Z rotation dials, view dial,
  trackball, scale handles) plus native-style G / R / S transforms with
  axis constraints, numeric input and scene units.
- Snapping to vertices, edges and faces with the native snap popover.
- Mirror cutting across X / Y / Z with ghost previews.
- Multi-object Edit Mode support.
- Tool-scoped Ctrl+Z: plane movements undo first, then cuts — outside the
  tool one press still undoes one cut.
- Rebindable tool shortcuts (a frozen keymap template — your edits
  survive updates), macOS Cmd support, HiDPI/Retina-correct drawing.
- Crash recovery: an interrupted session restores the original mesh on
  the next file load.

---

## Pre-release history (internal 4.x numbering)

The project used internal version numbers before the public launch;
1.0.0 is identical to internal 4.1.6.

### 4.1.6 — 2026-07-22
- Release metadata: author **Firg** in the manifest maintainer and
  copyright fields. No functional changes.

### 4.1.5 — 2026-07-21
- **Fixed:** Alt+G (clear plane location) centered on the live preview's
  shrinking bounding box instead of the real mesh — repeated presses
  walked the plane toward the surviving half. Reset now always centers on
  the uncut mesh.

### 4.1.4 — 2026-07-21
- **Improved:** the snap popover now matches native Blender snapping —
  same widget, same look, same click behavior (click picks one target,
  Shift/Ctrl+click builds a multi-selection). The target set can never
  end up empty.

### 4.1.3 — 2026-07-21
- **Fixed:** F9 / Adjust Last Operation with multiple objects in Edit
  Mode re-cut every object at a stale transform (all cuts landed as if
  the objects shared one origin). Matrices are now refreshed before the
  redo.

### 4.1.2 — 2026-07-21
- **macOS:** Cmd now works everywhere Ctrl does — in every tool shortcut
  and inside the transform modals (snap toggle, numeric input).

### 4.1.1 — 2026-07-21
- **HiDPI/Retina:** gizmo layout, snap marker sizes and line weights now
  scale correctly at any UI scale and pixel density, on Windows
  resolution scaling and on Retina Macs (Metal-safe wide-line rendering).

### 4.1.0 — 2026-07-16
First release-quality build: frozen rebindable keymap template, 16 bugs
fixed across three audit rounds, crash recovery, full headless test
battery green on Blender 4.5 LTS and 5.1.
