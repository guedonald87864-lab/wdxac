#!/usr/bin/env python3
"""Compose the 2002 R9 player card from separate layers (independent artwork).

  compose_card.py --portrait card/layers/portrait_34.png --player data/game/r9_2002_player.json \
                  --outdir card/out

Layers (each also written on its own, RGBA, same canvas, so the game UI or the master project
can re-stack them or swap the frame):
  10_background.png   night stadium gradient, light streaks, yellow/green diagonal bands
  20_watermark.png    "2002" type watermark + "YOKOHAMA 30.06.2002"
  30_portrait.png     the 3D model portrait (our render, never a press photo)
  40_frame.png        chrome/gold frame with the stat plate
  50_text.png         overall rating, position, name, flag
Composites: card_full.png (1024x1434), card_detail.png (portrait plate 1024x1024),
card_squad.png (256x358), card_pack.png (glow version for pack opening), card_back.png.
Rating/position are read from the player JSON (notes.derived.overall). No Konami, FIFA or
Nike marks are used; the card frame is an original design.
"""
import argparse
import json
import math
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FONTS = os.path.join(HERE, "..", "..", "card", "fonts")
W, H = 1024, 1434
YELLOW = (252, 222, 46)
GREEN = (0, 151, 57)
BLUE = (30, 63, 197)
CHROME = (214, 220, 228)
GOLD = (228, 190, 92)


def font(name, size):
    return ImageFont.truetype(os.path.join(FONTS, name), size)


def rounded_mask(w, h, r):
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w - 1, h - 1), r, fill=255)
    return m


def background():
    y = np.linspace(0, 1, H)[:, None]
    x = np.linspace(0, 1, W)[None, :]
    top = np.array([10, 16, 40])
    bot = np.array([4, 30, 22])
    base = top * (1 - y[..., None]) + bot * y[..., None]
    base = np.broadcast_to(base, (H, W, 3)).copy()
    # floodlight glow
    for cx, cy, s, col in ((0.18, 0.08, 0.35, (255, 236, 170)), (0.85, 0.05, 0.3, (190, 220, 255))):
        g = np.exp(-(((x - cx) ** 2 + (y - cy) ** 2) / (2 * s ** 2)))
        base += 90 * g[..., None] * np.array(col) / 255
    img = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8)).convert("RGBA")
    # draw translucent shapes on their own layer, then alpha-composite (ImageDraw does not
    # blend into an existing RGBA image)
    over = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    for off, col, wd in ((-260, YELLOW + (46,), 110), (-110, GREEN + (60,), 60), (560, YELLOW + (30,), 50)):
        d.polygon([(off, H), (off + wd, H), (off + wd + 900, 0), (off + 900, 0)], fill=col)
    rng = np.random.default_rng(2002)
    for _ in range(10):
        x0 = rng.uniform(0, W)
        d.line([(x0, 0), (x0 + rng.uniform(-250, 250), H * rng.uniform(0.3, 0.7))],
               fill=(255, 255, 255, int(rng.uniform(10, 26))), width=int(rng.uniform(3, 10)))
    over = over.filter(ImageFilter.GaussianBlur(3))
    img.alpha_composite(over)
    return img


def watermark():
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f = font("Oswald[wght].ttf", 520)
    d.text((W // 2, 640), "2002", font=f, anchor="mm", fill=(255, 255, 255, 22))
    f2 = font("BarlowCondensed-SemiBold.ttf", 40)
    d.text((W - 60, 120), "YOKOHAMA  30.06.2002", font=f2, anchor="rm", fill=(255, 255, 255, 120))
    return img


def portrait_layer(path):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    p = Image.open(path).convert("RGBA")
    scale = (H * 0.74) / p.height
    p = p.resize((int(p.width * scale), int(p.height * scale)), Image.Resampling.LANCZOS)
    x = (W - p.width) // 2 + 70
    y = H - p.height - 300
    # soft rim glow behind the player
    glow = Image.new("RGBA", p.size, (255, 230, 140, 0))
    glow.putalpha(p.split()[3].filter(ImageFilter.GaussianBlur(24)).point(lambda v: int(v * 0.45)))
    img.alpha_composite(glow, (x, y))
    img.alpha_composite(p, (x, y))
    # fade the bottom into the stat plate
    a = np.asarray(img).astype(np.float32)
    fade = np.clip((np.arange(H) - (H - 470)) / 200.0, 0, 1)[:, None]
    a[..., 3] *= (1 - fade)
    return Image.fromarray(a.astype(np.uint8))


def flag(w=120):
    h = int(w * 0.7)
    f = Image.new("RGBA", (w, h), GREEN + (255,))
    d = ImageDraw.Draw(f)
    d.polygon([(w * 0.08, h / 2), (w / 2, h * 0.1), (w * 0.92, h / 2), (w / 2, h * 0.9)], fill=YELLOW + (255,))
    d.ellipse((w / 2 - h * 0.25, h / 2 - h * 0.25, w / 2 + h * 0.25, h / 2 + h * 0.25), fill=BLUE + (255,))
    d.arc((w / 2 - h * 0.32, h / 2 - h * 0.14, w / 2 + h * 0.32, h / 2 + h * 0.5), 200, 340, fill=(255, 255, 255, 255), width=3)
    return f


def frame():
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i, (col, wd) in enumerate(((GOLD, 10), (CHROME, 4))):
        inset = 14 + i * 14
        d.rounded_rectangle((inset, inset, W - inset, H - inset), 46 - i * 8, outline=col + (255,), width=wd)
    # stat plate
    plate = Image.new("RGBA", (W - 80, 300), (0, 0, 0, 0))
    pd = ImageDraw.Draw(plate)
    pd.rounded_rectangle((0, 0, plate.width - 1, plate.height - 1), 30, fill=(8, 18, 40, 225), outline=GOLD + (255,), width=4)
    pd.rectangle((0, 60, plate.width, 66), fill=YELLOW + (255,))
    pd.rectangle((0, 66, plate.width, 70), fill=GREEN + (255,))
    img.alpha_composite(plate, (40, H - 340))
    # rating badge
    d.polygon([(48, 48), (290, 48), (290, 330), (169, 390), (48, 330)], fill=(8, 18, 40, 220), outline=GOLD + (255,))
    return img


def text_layer(rating, position, top_style):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.text((169, 165), str(rating), font=font("Oswald[wght].ttf", 190), anchor="mm", fill=(255, 255, 255, 255))
    d.text((169, 285), position, font=font("BarlowCondensed-Bold.ttf", 76), anchor="mm", fill=YELLOW + (255,))
    img.alpha_composite(flag(130), (104, 420))
    d.text((W // 2, H - 250), "RONALDO", font=font("Oswald[wght].ttf", 150), anchor="mm", fill=(255, 255, 255, 255))
    d.text((W // 2, H - 150), "NAZÁRIO  ·  BRAZIL 2002", font=font("BarlowCondensed-SemiBold.ttf", 54), anchor="mm",
           fill=(230, 236, 246, 255))
    d.text((W // 2, H - 92), top_style, font=font("BarlowCondensed-SemiBold.ttf", 40), anchor="mm", fill=GOLD + (255,))
    d.text((W - 70, H - 400), "R9", font=font("Oswald[wght].ttf", 110), anchor="rm", fill=(255, 255, 255, 60))
    return img


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--portrait", required=True)
    ap.add_argument("--player", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--stage", default="MAX+FIXED", help="which overall to print (see notes.derived.stages)")
    a = ap.parse_args()
    os.makedirs(a.outdir, exist_ok=True)
    pl = json.load(open(a.player))
    pos = pl.get("registered_position", "CF")
    rating = pl.get("notes", {}).get("derived", {}).get("overall", {}).get(a.stage, {}).get(pos, "??")
    style = pl.get("playing_style", {}).get("attacking", "")
    layers = {"10_background": background(), "20_watermark": watermark(), "30_portrait": portrait_layer(a.portrait),
              "40_frame": frame(), "50_text": text_layer(rating, pos, style.upper())}
    card = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    for name, L in layers.items():
        L.save(os.path.join(a.outdir, name + ".png"))
        card.alpha_composite(L)
    card.putalpha(rounded_mask(W, H, 52))
    card.save(os.path.join(a.outdir, "card_full.png"))
    card.resize((256, 358), Image.Resampling.LANCZOS).save(os.path.join(a.outdir, "card_squad.png"))
    det = Image.alpha_composite(layers["10_background"], layers["30_portrait"]).crop((0, 80, W, 80 + W))
    det.save(os.path.join(a.outdir, "card_detail.png"))
    glow = Image.new("RGBA", (W + 160, H + 160), (0, 0, 0, 0))
    halo = Image.new("RGBA", (W, H), YELLOW + (255,))
    halo.putalpha(rounded_mask(W, H, 52))
    glow.alpha_composite(halo, (80, 80))
    glow = glow.filter(ImageFilter.GaussianBlur(40))
    glow.alpha_composite(card, (80, 80))
    glow.save(os.path.join(a.outdir, "card_pack.png"))
    back = Image.alpha_composite(layers["10_background"], layers["20_watermark"])
    bd = ImageDraw.Draw(back)
    bd.text((W // 2, H // 2), "R9", font=font("Oswald[wght].ttf", 360), anchor="mm", fill=YELLOW + (230,))
    back.putalpha(rounded_mask(W, H, 52))
    back.save(os.path.join(a.outdir, "card_back.png"))
    spec = {"canvas": [W, H], "layers": list(layers), "rating_stage": a.stage, "rating": rating, "position": pos,
            "outputs": {"card_full": [W, H], "card_squad": [256, 358], "card_detail": [W, W], "card_pack": [W + 160, H + 160],
                        "card_back": [W, H]},
            "safe_areas": {"rating_badge": [48, 48, 290, 390], "name_plate": [40, H - 340, W - 40, H - 40],
                           "portrait": [0, 0, W, H - 340]}}
    json.dump(spec, open(os.path.join(a.outdir, "card_spec.json"), "w"), indent=1, ensure_ascii=False)
    print("CARD", rating, pos)


if __name__ == "__main__":
    main()
