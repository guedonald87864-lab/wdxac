#!/usr/bin/env python3
"""Paste images side by side (optionally labelled) into one contact sheet: sheet.py out.png a.png b.png ..."""
import sys
from PIL import Image, ImageDraw

out, paths = sys.argv[1], sys.argv[2:]
ims = [Image.open(p).convert("RGB") for p in paths]
h = max(i.height for i in ims)
ims = [i.resize((int(i.width * h / i.height), h)) for i in ims]
sheet = Image.new("RGB", (sum(i.width for i in ims), h + 22), (20, 20, 20))
d = ImageDraw.Draw(sheet)
x = 0
for p, i in zip(paths, ims):
    sheet.paste(i, (x, 22))
    d.text((x + 6, 5), p.rsplit("/", 1)[-1], fill=(230, 230, 230))
    x += i.width
sheet.save(out)
