# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate the toolbar icon for the Cut Plane workspace tool.

Writes ``cut_plane/icons/mesh_tool.cut_plane.dat`` in Blender's geometry
icon format (the format native tool icons ship in), decoded from the shipped
`.dat` files and Blender's ``BKE_icon_geom_from_memory`` reader:

    header:  b'VCO\\x00' + coords_range (2 bytes, native uses 255,255) + 2 pad
    body:    N vertices x 2 bytes (x, y) in 0..255, ORIGIN TOP-LEFT / Y DOWN
             (verified against ops.mesh.primitive_cube_add_gizmo.dat), then
             N vertices x 4 bytes RGBA. Vertices are consumed 3 per triangle.

Design: the canonical native-gray wireframe cube sliced by the tool's ORANGE
plane (a perspective parallelogram outline, overhanging the cube - matching
how the tool looks in the viewport), in the stroke style of the native
edit-mode icons (6-unit gray strokes, heavier 9-unit accent lines).

Run:  python tools/make_tool_icon.py [--preview]
Pure Python (no bpy) - regenerates the committed .dat deterministically.
"""
import os
import struct
import sys

# Native icon conventions (measured from the shipped .dat files -
# ops.mesh.loopcut_slide/inset/extrude share ONE standard cube: front square
# outline centerlines 51..165, back square 90..204 (depth down-right),
# hidden-line style, 6-unit strokes, gray 229 + one accent color).
# Accent LINES are heavier than the gray: loopcut's cut line is 9 units,
# spin's arc ~11 (bisect ~7), so the accent pops at toolbar size.
GRAY = (229, 229, 229, 255)          # main stroke color of native tool icons
ORANGE = (255, 153, 0, 255)          # PLANE_COLOR_OUTLINE (1.0, 0.6, 0.0)
STROKE = 6                           # native GRAY stroke width (0..255 units)
ACCENT_STROKE = 9                    # native accent-line width (loopcut = 9)

OUT = os.path.join(os.path.dirname(__file__), os.pardir,
                   "cut_plane", "icons", "mesh_tool.cut_plane.dat")


def stroke_quad(p1, p2, width, color, caps=True):
    """A line segment as two triangles (native icons are built from these)."""
    (x1, y1), (x2, y2) = p1, p2
    dx, dy = x2 - x1, y2 - y1
    length = (dx * dx + dy * dy) ** 0.5 or 1.0
    ux, uy = dx / length, dy / length
    if caps:  # square caps: extend both ends by half the width
        x1, y1 = x1 - ux * width / 2, y1 - uy * width / 2
        x2, y2 = x2 + ux * width / 2, y2 + uy * width / 2
    nx, ny = -uy * width / 2, ux * width / 2
    a = (x1 + nx, y1 + ny)
    b = (x1 - nx, y1 - ny)
    c = (x2 - nx, y2 - ny)
    d = (x2 + nx, y2 + ny)
    return [(a, b, c, color), (a, c, d, color)]


def build_triangles():
    """The icon: THE standard native cube + orange slicing plane. Y DOWN.

    Cube geometry is the exact canonical one shared by the native edit-mode
    tool icons (inset/bevel/loop-cut/extrude): front square outline with
    centerlines 51..165, back square 90..204 (depth toward down-right),
    hidden-line style (back top/left edges omitted). The gray outline gets a
    GAP where the orange plane crosses it - the same trick native icons use
    (bisect's box has a gap where its green cut line passes).
    """
    tris = []

    f0, f1 = 51, 165      # front square centerlines
    b0, b1 = 90, 204      # back square centerlines (depth = +39, +39)

    cube_segments = (
        ((f0, f0), (f1, f0)),          # front top
        ((f0, f0), (f0, 93)),          # front left, upper (gap for the plane)
        ((f0, 113), (f0, f1)),         # front left, lower
        ((f0, f1), (f1, f1)),          # front bottom
        ((f1, f0), (f1, f1)),          # front right
        ((f1, f0), (b1, b0)),          # connector top-right
        ((f1, f1), (b1, b1)),          # connector bottom-right
        ((f0, f1), (b0, b1)),          # connector bottom-left
        ((b1, b0), (b1, b1)),          # back right vertical
        ((b0, b1), (b1, b1)),          # back bottom
    )
    for p, q in cube_segments:
        tris += stroke_quad(p, q, STROKE, GRAY)

    # --- the slice plane: orange parallelogram lying "flat" in the cube's
    # projection (horizontal width, depth along the cube's diagonal),
    # through the cube center, clearly overhanging it on both sides -
    # native accents overhang generously (bisect's line spans y55..209
    # across a box of 58..201). Heavier ACCENT_STROKE (native accent lines
    # are thicker than the gray: loopcut 9 vs 6). Drawn OVER the cube
    # except at the left-edge gap above.
    p1 = (25, 103)    # far left (outside the cube)
    p2 = (175, 103)   # top right
    p3 = (227, 155)   # far right (outside the cube)
    p4 = (77, 155)    # bottom left
    for p, q in ((p1, p2), (p2, p3), (p3, p4), (p4, p1)):
        tris += stroke_quad(p, q, ACCENT_STROKE, ORANGE)

    return tris


def write_dat(tris, path):
    coords = bytearray()
    colors = bytearray()
    count = 0
    for a, b, c, color in tris:
        for (x, y) in (a, b, c):
            xi = max(0, min(255, int(round(x))))
            yi = max(0, min(255, int(round(y))))
            coords += struct.pack('<BB', xi, yi)
            colors += bytes(color)
            count += 1
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as fh:
        fh.write(b'VCO\x00')
        fh.write(bytes((255, 255, 0, 0)))  # coords_range + pad (native values)
        fh.write(coords)
        fh.write(colors)
    return count, os.path.getsize(path)


def preview(tris, w=52, h=26):
    """ASCII render (o = orange plane, # = gray cube) for quick iteration."""
    grid = [[' '] * w for _ in range(h)]

    def edge(ax, ay, bx, by, cx, cy):
        return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)

    for a, b, c, color in tris:
        ch = 'o' if color == ORANGE else '#'
        for gy in range(h):
            for gx in range(w):
                px, py = gx / (w - 1) * 255, gy / (h - 1) * 255
                d0 = edge(*a, *b, px, py)
                d1 = edge(*b, *c, px, py)
                d2 = edge(*c, *a, px, py)
                if (d0 >= 0 and d1 >= 0 and d2 >= 0) or (d0 <= 0 and d1 <= 0 and d2 <= 0):
                    if not (ch == '#' and grid[gy][gx] == 'o'):  # orange on top
                        grid[gy][gx] = ch
    return '\n'.join(''.join(row) for row in grid)


if __name__ == "__main__":
    triangles = build_triangles()
    if "--preview" in sys.argv:
        print(preview(triangles))
    n, size = write_dat(triangles, os.path.abspath(OUT))
    print(f"wrote {os.path.abspath(OUT)}: {n} verts, {n // 3} tris, {size} bytes")
