#!/usr/bin/env bash
# Turn a screen recording into a README-weight demo GIF.
#
#   tools/make_demo_gif.sh <recording.mp4> <out.gif> [start] [duration] [crop]
#
# start/duration are seconds, e.g.  make_demo_gif.sh grab.mkv out.gif 4.5 6
# Omit them to convert the whole file.
#
# crop is ffmpeg's w:h:x:y and defaults to the 3D viewport of a maximised
# 1920x1080 Blender window (1198:888:2:49) - that framing keeps the whole tool
# settings header row, which is what tells a silent viewer which options are
# on and carries the live readout during a drag. Pass "none" to skip cropping.
#
# Two things this handles that a naive conversion gets wrong:
#
# 1. PALETTE. The Blender viewport is ~97% grey and ffmpeg's palettegen
#    allocates entries by AREA, so a small budget spends nearly every slot on
#    greys and the orange plane, the red/green axis lines and the gizmo arrows
#    get quantised toward grey. Measured colour retention (share of pixels
#    still reading as coloured, vs the source frame):
#        48 colours, stats=diff -> 70%   96 colours, stats=full -> 99%
#    96 with a full-clip palette is the knee. Do not narrow the frame to save
#    bytes: below ~880px the thin coloured edges average into the grey.
#
# 2. CAPTURE NOISE. Desktop recorders introduce +/-1..2 per-channel noise over
#    the whole frame. GIF stores deltas, so every pixel counts as changed and
#    the file balloons (measured: 4.5 MB for 62 frames). The temporal filter
#    below freezes pixels whose change is below a threshold, restoring
#    byte-identical static regions.
set -euo pipefail

SRC="$1"; OUT="$2"; START="${3:-}"; DUR="${4:-}"; CROP="${5:-1198:888:2:49}"
FPS=10
WIDTH=880
COLORS=96
NOISE=8          # per-channel change below this is treated as capture noise

CROP_VF=""
[ "$CROP" != "none" ] && CROP_VF="crop=${CROP},"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

SEEK=()
[ -n "$START" ] && SEEK+=(-ss "$START")
[ -n "$DUR" ] && SEEK+=(-t "$DUR")

echo "1/3  extracting frames at ${FPS} fps (crop ${CROP})"
ffmpeg -y -v error "${SEEK[@]}" -i "$SRC" -vf "${CROP_VF}fps=${FPS}" "$WORK/raw%04d.png"
echo "     $(ls "$WORK" | wc -l) frames"

echo "2/3  removing capture noise (threshold ${NOISE})"
python - "$WORK" "$NOISE" <<'PY'
import os, sys
import numpy as np
from PIL import Image
work, thresh = sys.argv[1], int(sys.argv[2])
files = sorted(f for f in os.listdir(work) if f.startswith("raw"))
prev = None
for i, f in enumerate(files):
    cur = np.asarray(Image.open(os.path.join(work, f)).convert("RGB")).astype(np.int16)
    if prev is None:
        out = cur
    else:
        keep = (np.abs(cur - prev).max(axis=2) <= thresh)[:, :, None]
        out = np.where(keep, prev, cur)
    prev = out
    Image.fromarray(out.astype(np.uint8)).save(os.path.join(work, "f%04d.png" % i))
PY

echo "3/3  encoding (${WIDTH}px, ${COLORS} colours, full-clip palette)"
# scale flags=area, NOT lanczos. The edit-cage wireframe is a dense regular
# grid; a sharp filter aliases it into a harsh checkerboard (and h264 source
# blocking makes that worse). Box averaging reads as fine texture instead,
# and is ~20% smaller into the bargain.
#
# dither=none, NOT bayer. Bayer is an ORDERED dither - it lays a repeating
# crosshatch over flat areas, which on this mostly-grey viewport is exactly
# the artefact it is meant to hide. With 96 colours the greys are close
# enough together that undithered banding is not visible.
VF="scale=${WIDTH}:-1:flags=area"
ffmpeg -y -v error -framerate "$FPS" -i "$WORK/f%04d.png" \
  -vf "${VF},palettegen=max_colors=${COLORS}:stats_mode=full" "$WORK/pal.png"
ffmpeg -y -v error -framerate "$FPS" -i "$WORK/f%04d.png" -i "$WORK/pal.png" \
  -lavfi "${VF} [x]; [x][1:v] paletteuse=dither=none:diff_mode=rectangle" \
  -loop 0 "$OUT"

printf '\n%s  %s  %d KB\n' "$(basename "$OUT")" \
  "$(ffprobe -v error -select_streams v -show_entries stream=width,height,nb_frames -of csv=p=0 "$OUT")" \
  $(( $(stat -c %s "$OUT") / 1024 ))
