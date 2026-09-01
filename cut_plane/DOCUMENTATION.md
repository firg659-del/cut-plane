# Cut Plane — User Guide

Cut Plane is an Edit Mode tool: an orange plane you position with gizmos
or G / R / S, showing the cut **live** on your mesh — fill, clear one
side, mirror it — and press **Enter** to commit.

Requires **Blender 4.5 or newer** (tested through 5.1).

---

## Install

1. In Blender: **Edit → Preferences → Get Extensions → Install from Disk…**
   and pick the downloaded `cut_plane-1.1.0.zip`
   (or simply drag the zip into the Blender window).
2. That's it — the extension enables itself.

## Quick start

1. Enter **Edit Mode** on a mesh and pick **Cut Plane** in the toolbar
   (press `T` if the toolbar is hidden). It's also in the **Mesh** menu.
2. The plane appears at the mesh center. Position it with the gizmo, or
   press **G / R / S** — the keys act on the plane, exactly like they act
   on geometry elsewhere in Blender.
3. Turn on **Fill**, **Clear Inner** or **Clear Outer** in the header —
   the mesh updates live so you always see the exact result.
4. Press **Enter** (or click **Cut**) to commit the cut.

## Controls

**Gizmo** — arrows move, dials rotate (one per axis + a view dial),
corner handles scale, the center circle drags freely. The **Mode**
dropdown switches the visible handle set (Move / Rotate / Scale);
**Orientation** switches Global / Local; **Anchor** sets the point a
fresh plane centers on and the mirrors reflect through — the mesh
bounding box (default), the object origin, or the 3D cursor. The gizmo
icon at the far left of the header hides every handle, leaving just the
plane outline — handy once you work by keyboard.

The cut itself is an **infinite plane**: the orange rectangle only shows
you where that plane lies. Scaling it (`S`, or the corner handles) resizes
the rectangle to frame your model nicely — it never changes what gets cut.
Move and rotate change the cut; scale is a viewing aid.

**Keyboard** (while the tool is active):

| Keys | Action |
|---|---|
| `G` / `R` / `S` | Move / rotate / scale the plane |
| `R, R` | Second tap of R switches to trackball rotation |
| `X` `Y` `Z` (during a drag) | Constrain to an axis — carries over when you switch transforms mid-drag |
| type numbers (during a drag) | Exact values; scene units work (`0.5m`, `25cm`, degrees for angles) |
| `Shift` (during a drag) | Precision mode |
| `Ctrl` (during a drag) | Toggle snapping |
| click / `Enter` | Confirm the drag · `Esc` or right-click cancels |
| `Alt+G` / `Alt+R` / `Alt+S` | Clear the plane's location / rotation / scale (the **Reset** button clears all three) |
| `Enter` | Execute the cut |
| `Ctrl+Z` / `Ctrl+Shift+Z` | Undo / redo (see below) |

On **macOS**, Cmd works everywhere Ctrl is listed.

## Snapping

The magnet icon in the header toggles snapping; the popover next to it
picks targets (vertices, edge centers, faces, grid…) — click selects
one target, **Shift+click** adds more, just like native snapping.
**Align Rotation** orients the plane to the surface you snap onto, and
you can limit snapping to Move / Rotate / Scale individually.

## Cut options

- **Fill** — caps the cut with faces (same result native Bisect gives).
- **Clear Inner / Clear Outer** — delete one side of the cut.
- **Mirror X / Y / Z** — cut with mirrored copies of the plane at once,
  reflected through the **Anchor**; the eye icon shows or hides the ghost
  planes. An axis that reflects the plane onto itself adds no cut — the
  header names them. With an axis-aligned plane that is every axis except
  the plane's own normal.
- **Selected Only** — cut only the selected geometry.
- **Select Loop** (in the `…` popover) — select the fresh edge loop
  after cutting.

If the tool can't preview something, it tells you — a warning appears in
the header instead of silently doing nothing.

## Undo, redo and F9

While the tool is active, **Ctrl+Z first undoes plane movements, then
cuts** — so experimenting with the plane never costs you mesh history.
Once you switch to another tool, undo is fully native again: one press,
one cut. After a cut, **F9** (Adjust Last Operation) reopens the cut's
options like any native operator.

## Changing the shortcuts

Every tool shortcut above is rebindable: **Edit → Preferences → Add-ons
→ Cut Plane**, or in **Preferences → Keymap** search for "Cut Plane".
Your bindings survive addon updates.

## Good to know

- With several meshes in Edit Mode at once, one plane cuts **all of
  them** in the same pass, and they all mirror through the same Anchor —
  read from the active object, the one the plane is placed from.
- A fresh plane starts **on** the Anchor, so mirroring adds nothing until
  you move the plane off it.
- While the tool is active, `Alt+R` and `Alt+S` override native *Spin*
  and *Shrink/Fatten*. Rebind them in the preferences if you rely on
  those — everywhere outside the tool they're untouched.
- Meshes with **shape keys** don't show the live preview (a header
  warning explains); the same applies above a very high polygon count,
  where previewing every mouse move would be too slow.
- The in-drag keys (`X/Y/Z`, `Shift`, `Ctrl`, numbers) are fixed; only
  the tool shortcuts are rebindable.

## Support

Questions or bug reports: **firgcg@gmail.com** — include your
Blender version and, if possible, the .blend file.
