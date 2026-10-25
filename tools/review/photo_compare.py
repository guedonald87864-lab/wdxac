#!/usr/bin/env python3
"""Photo vs. render comparison sheet for face-fit review (private use; photos are not stored).

  photo_compare.py --lm lm.json --photos DIR --renders DIR --out sheet.png [--names a.png,b.png]
Each row: photo crop | render from the fitted camera | 50 % blend | edge overlay.
Crops follow the photo's MediaPipe landmark bounding box (+ margin for the hair patch).
"""
import argparse
import json
import os

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

ap = argparse.ArgumentParser()
ap.add_argument("--lm", required=True)
ap.add_argument("--photos", required=True)
ap.add_argument("--renders", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--names", default="")
ap.add_argument("--tile", type=int, default=260)
a = ap.parse_args()
lm = json.load(open(a.lm))
names = [n for n in a.names.split(",") if n] or sorted(os.listdir(a.renders))
rows = []
for n in names:
    key = [k for k in lm if os.path.basename(k) == n or os.path.splitext(os.path.basename(k))[0] == os.path.splitext(n)[0]]
    if not key or not lm[key[0]]:
        continue
    L = np.array(lm[key[0]]["landmarks"])[:, :2]
    x0, y0 = L.min(0)
    x1, y1 = L.max(0)
    w, h = x1 - x0, y1 - y0
    box = (int(x0 - 0.45 * w), int(y0 - 0.7 * h), int(x1 + 0.45 * w), int(y1 + 0.2 * h))
    ph = Image.open(os.path.join(a.photos, os.path.basename(key[0]))).convert("RGB").crop(box)
    rp = os.path.join(a.renders, os.path.splitext(n)[0] + ".png")
    if not os.path.exists(rp):
        continue
    rd = Image.open(rp).convert("RGB").crop(box)
    T = a.tile
    sz = (T, int(T * ph.height / ph.width))
    ph, rd = ph.resize(sz), rd.resize(sz)
    blend = Image.blend(ph, rd, 0.5)
    edges = rd.convert("L").filter(ImageFilter.FIND_EDGES).point(lambda v: 255 if v > 18 else 0)
    ov = ph.copy()
    ov.paste((0, 255, 0), mask=edges)
    row = Image.new("RGB", (4 * T, sz[1] + 16), (0, 0, 0))
    for i, im in enumerate((ph, rd, blend, ov)):
        row.paste(im, (i * T, 16))
    ImageDraw.Draw(row).text((4, 2), n, fill=(255, 255, 0))
    rows.append(row)
H = sum(r.height for r in rows)
sheet = Image.new("RGB", (4 * a.tile, H))
y = 0
for r in rows:
    sheet.paste(r, (0, y))
    y += r.height
sheet.save(a.out)
print("rows", len(rows))
