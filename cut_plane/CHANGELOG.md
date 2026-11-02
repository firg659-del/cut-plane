# Changelog

User-facing changes to Cut Plane. Versions match `blender_manifest.toml`;
entries are written to be paste-ready for store update notes.

## 1.2.0 - 2026-11-02
- **Pick the cutting plane in one key: XY, XZ or YZ.** Shift+Z lays the
  plane in the world XY plane, Shift+Y in XZ and Shift+X in YZ, keeping
  its position - the same keys that leave an axis out in a native move.
  Rebindable in the tool's shortcut list like the others.
- **Undo no longer brings back a cut that was never made.** An undo
  step taken while the preview showed - a click-select, Tab out of
  Edit Mode - stored the preview, so a later Ctrl+Z could turn the
  un-executed cut into real geometry, including everything a Clear had
  removed. Such a cut is now reverted as the undo lands.
- **Edits made while the preview shows are kept.** A move from the
  Mesh menu, Flip Normals, Shade Smooth, a material, UV Unwrap, seams
  or sharp edges were silently reverted by the next plane move, by
  leaving the tool, by Tab and by saving. The mesh now stays exactly as
  you see it, preview included, as it always did for an extrude.
- **Exports never contain the uncommitted cut.** While File > Export
  (or any file browser) is open, the preview pauses and the mesh shows
  its original shape; it comes back when the browser closes. STL, PLY,
  USD, Alembic and Collada exports used to write the cut.
- **Dragging stays fluid on dense meshes.** Every mouse move used to re-cut
  the whole mesh, so on a mesh of 100k+ faces the plane and the viewport
  froze for seconds (a 2-second drag took 32 s at 126k faces, 59 s at
  250k). The plane now follows the mouse; the cut catches up when the mouse
  rests and is always exact on release (4-5 s for the same drags).
- **The live cut itself is faster on dense meshes.** Each re-cut rebuilt and
  cut the whole mesh; it now builds only what survives the cut, straight
  from the original, with the same result. While you move the plane, a
  re-cut costs what the cut touches rather than the whole mesh: at 126k
  faces 280 -> about 55 ms (the preview went from ~4 to ~13 updates a
  second), at 31k faces about 18 ms.
- **Your selection survives a cut exactly as you made it.** In Vertex
  select mode Cut deselected every face it split, while the preview showed
  them selected; a plane along existing edges selected the faces next to
  them; Face mode picked up stray edges; and selecting something during the
  preview could leave a stray column of the old cut loop selected, lose a
  click on a concave or twisted face, or drop what was selected on the
  cleared side. The preview and Cut now always agree, in every select mode.
- **Fill matches native Bisect:** caps face outward and take the
  neighbouring faces' material, smooth shading and UVs.
- **Fill works with Clear Inner or Clear Outer.** With both sides kept,
  Fill used to add a face inside the mesh - an invisible internal wall
  that made it non-manifold. Fill is now dimmed until a side is cleared.
- **Fill caps look the same in the preview and after Cut.** Where a cap
  borders faces of different materials, the preview and Cut could give it
  different materials and UVs. A cap now takes the most common material
  around it (ties go to the lowest slot), in both.
- **No more sliver triangles.** A plane passing within a hair of a vertex
  split the edge right beside it, leaving a near-zero triangle there.
  Vertices that close now count as on the plane, as in native Bisect.
- **Hidden geometry is never cut or cleared**, in the preview or by
  Execute, like native Bisect.
- **Mirror** cuts in the object's own space on an object under a
  non-uniformly scaled parent (it cut a skewed copy there), follows the 3D
  cursor at once when Anchor is the cursor, and no longer turns the preview
  off as "too dense" for a mirror that adds no cut.
- **Mirror with the Bounding Box anchor** cuts where the preview showed
  after an earlier cut in the same Edit Mode session; the mirrored cut
  could land outside the mesh and vanish.
- A plane through a single vertex with a Clear on no longer stops with
  an error, and keeps that vertex, like native Bisect.
- Leaving the tool no longer leaves the mesh with no selection mode.
- **Shift during a locked move follows the mouse as natively.** With
  G locked to a plane (Shift+X/Y/Z) or an axis (X/Y/Z), Precision was
  applied to the result instead of the mouse, so at a shallow view the
  plane raced ahead of the cursor and kept speeding up. It now moves
  exactly as Blender's own move does.
- **Typed values during a drag:** an axis key or Space after a number
  (G 2 X, R 90 Space) works as natively instead of resetting the
  plane; "/" inverts the degrees you typed (R 4 / is a quarter degree);
  the numpad decimal key types a point on comma-decimal keyboards.
- With a Clear on, a fresh plane starts at the mesh centre again (on
  Blender 5.1 and later it started at the centre of the cut half).
- A transform that stops on an internal error puts the plane back where
  the drag started and clears its overlays, as Esc does.

## 1.1.0 — 2026-08-29
- **Mirror no longer deletes the mesh.** A plane that a mirror
  reflects onto ITSELF was expanded into two cuts of the same plane
  facing opposite ways, so Clear Inner or Clear Outer removed both
  sides and the mesh vanished. This was the default state — the
  plane starts at the mesh centre, which on a fresh object is the
  origin — so Mirror Z plus a Clear emptied it. Fill capped such a
  cut twice, leaving coincident faces.
- **Anchor** — one point that a fresh plane centres on and that the
  mirrored cuts reflect through: Bounding Box (default, unchanged
  placement), Object Origin, or 3D Cursor. These used to disagree:
  the plane centred on the bounding box while Mirror reflected about
  the origin, so on a mesh whose origin sits away from its geometry
  — most imported models — the mirrored cut landed twice that
  distance off, often outside the mesh entirely. With several meshes
  in Edit Mode they now mirror through the same Anchor, read from the
  active object.
- **Mirror says when it adds nothing.** A mirror that reflects the
  plane onto itself has no second cut to make, which is every
  combination while the plane sits on the Anchor. The header and the
  Active Tool panel name the axes that add nothing; move the plane
  off them and it clears.

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
