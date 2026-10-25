#!/usr/bin/env python3
"""'Game-native precision' previews of OUR head without any donor: two decimated LODs at game
vertex budgets + 256 px ASTC textures, rendered with the same views/lights as the HD model.

  python3 tools/wrap/game_precision.py --blend r9.blend --textures TEXDIR --out-dir OUT \
      [--lod0-verts 5376 --lod1-verts 1338] [--parts R9_Head,R9_Eyes] \
      [--hair-lod0 1600 --hair-lod1 400] [--tex-size 256] [--block 4x4|6x6] \
      [--views <blender_render.py views>] [--res 600] [--samples 24] [--no-render] [--target-npz head.npz]

Defaults mimic the Henry donor (LOD0 5,376 / LOD1 1,338 GPU vertices, 256 px ASTC face texture).
The budget counts GPU vertices (split at UV seams) summed over --parts; the hair shell has its own
budget because in-game hair is normally a separate mesh (an ASSUMPTION until real eFootball
assets are inspected - change the grouping with --parts / --hair-*).
Steps: (1) tools/wrap/game_precision_blender.py decimates (collapse, X-symmetric, UVs kept) and
writes lod0.blend / lod1.blend; (2) every texture of TEXDIR is reduced to --tex-size (colour maps
in linear light, normal maps by vector averaging + renormalisation, both area filters) and
round-tripped through ASTC (tools/native/astc_tool.py); the DECODED images are what the renders
use; (3) tools/model/blender_render.py renders HD / LOD0 / LOD1; (4) a side-by-side sheet and a
report JSON (vertex/triangle counts, texture PSNR: ASTC loss and resolution loss).
Because decimation keeps the HD UV layout, "baking to 256 px" is a resampling in the same UV
space - no reprojection. Geometric detail removed by decimation is NOT re-baked into the
normal map (the HD normal map is reused), so LOD previews show slightly less shape detail than a
production asset with a re-baked normal map would.
"""
import argparse
import json
import os
import subprocess
import sys
import time

import numpy as np
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "tools", "native"))
import astc_tool  # noqa: E402
from transfer_texture import lin_to_srgb, psnr, srgb_to_lin  # noqa: E402

RENDER = os.path.join(REPO, "tools", "model", "blender_render.py")


def log(*a):
    print("[game]", *a, flush=True)


def blender(args, **kw):
    r = subprocess.run(["blender", "-b"] + args, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise RuntimeError("blender failed:\n" + r.stdout[-3000:] + r.stderr[-3000:])
    return r.stdout


def reduce_texture(path, size, kind):
    im = Image.open(path)
    has_alpha = im.mode in ("RGBA", "LA")
    a = np.asarray(im.convert("RGBA" if has_alpha else "RGB"), np.float64) / 255
    H, W = a.shape[:2]
    if H % size or W % size:
        raise SystemExit(f"{path}: {W}x{H} is not a multiple of {size}")
    f = H // size
    rgb = a[..., :3]
    al = a[..., 3].reshape(size, f, size, f).mean((1, 3)) if has_alpha else None
    if kind == "color":
        x = srgb_to_lin(rgb)
        if has_alpha:                              # premultiplied so transparent texels do not bleed
            x = x * a[..., 3:4]
        x = x.reshape(size, f, size, f, 3).mean((1, 3))
        if has_alpha:
            x = x / np.maximum(al[..., None], 1e-6)
        out = lin_to_srgb(x)
    elif kind == "normal":
        v = rgb * 2 - 1
        v = v.reshape(size, f, size, f, 3).mean((1, 3))
        v /= np.maximum(np.linalg.norm(v, axis=2, keepdims=True), 1e-9)
        out = v * 0.5 + 0.5
    else:
        out = rgb.reshape(size, f, size, f, 3).mean((1, 3))
    if has_alpha:
        out = np.concatenate([out, al[..., None]], 2)
    return out, a


def kind_of(name):
    return "normal" if "normal" in name else ("data" if any(k in name for k in ("rough", "mask")) else "color")


def contact_sheet(rows, cols, labels, out, cell=360):
    W = cell * len(cols)
    H = cell * len(rows) + 60
    sheet = Image.new("RGB", (W, H), (30, 30, 30))
    d = ImageDraw.Draw(sheet)
    for j, lab in enumerate(labels):
        d.text((j * cell + 8, 8), lab, fill=(255, 255, 255))
    for i, row in enumerate(rows):
        for j, p in enumerate(row):
            if p and os.path.exists(p):
                sheet.paste(Image.open(p).convert("RGB").resize((cell, cell)), (j * cell, 60 + i * cell))
    sheet.save(out)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--blend", required=True)
    ap.add_argument("--textures", required=True)
    ap.add_argument("--out-dir", required=True)
    # teeth/tongue are left out: the HD procedural teeth (2 x 6,832 verts) would eat the head's budget,
    # and in game the mouth interior comes from the donor head
    ap.add_argument("--parts", default="R9_Head,R9_Eyes")
    ap.add_argument("--lod0-verts", type=int, default=5376)
    ap.add_argument("--lod1-verts", type=int, default=1338)
    ap.add_argument("--hair", default="R9_Hair")
    ap.add_argument("--hair-lod0", type=int, default=1600)
    ap.add_argument("--hair-lod1", type=int, default=400)
    ap.add_argument("--tex-size", type=int, default=256)
    ap.add_argument("--block", default="4x4")
    ap.add_argument("--quality", default="-medium")
    ap.add_argument("--views", default="head_front,head_l45,head_r45,head_lprof,head_rprof,head_back,head_top")
    ap.add_argument("--res", type=int, default=600)
    ap.add_argument("--samples", type=int, default=24)
    ap.add_argument("--no-render", action="store_true")
    ap.add_argument("--target-npz", help="export_head.py NPZ of the same head: adds face-crop PSNR for the head maps")
    a = ap.parse_args()
    t0 = time.time()
    os.makedirs(a.out_dir, exist_ok=True)
    rep = {"inputs": vars(a)}
    # 1. decimation
    budgets = ["--budget", f"{a.parts}={a.lod0_verts}/{a.lod1_verts}"]
    if a.hair:
        budgets += ["--budget", f"{a.hair}={a.hair_lod0}/{a.hair_lod1}"]
    blender([os.path.abspath(a.blend), "--python", os.path.join(HERE, "game_precision_blender.py"), "--",
             "--out-dir", os.path.abspath(a.out_dir)] + budgets)
    st = json.load(open(os.path.join(a.out_dir, "lod_stats.json")))
    rep["lods"] = st
    for g in st["groups"]:
        log(f"LOD{g['lod']} {g['objects']}: budget {g['budget_gpu_verts']} -> {g['gpu_verts']} GPU verts "
            f"(ratio {g['ratio']:.3f}) " + ", ".join(f"{k}: {v['gpu_verts']}v/{v['tris']}t" for k, v in g["per_object"].items()))
    # 2. textures
    tex_game = os.path.join(a.out_dir, f"tex_{a.tex_size}_astc{a.block}")
    tex_plain = os.path.join(a.out_dir, f"tex_{a.tex_size}")
    os.makedirs(tex_game, exist_ok=True)
    os.makedirs(tex_plain, exist_ok=True)
    rep["textures"] = {}
    bx, by = (int(x) for x in a.block.split("x"))
    face_box = None
    if a.target_npz:                    # UV bounding box of the 'face' region of the head (HeadUV)
        import features as FT
        import meshdata as MD
        A, meta = MD.load_npz(a.target_npz)
        face = A["head.region"] == FT.REGION_ID["face"]
        tri = face[A["head.faces"]].all(1)
        uv = A[f"head.uv.{meta.get('head_uv', 'HeadUV')}"][tri].reshape(-1, 2)
        face_box = (uv.min(0), uv.max(0))
        rep["face_uv_box"] = [float(x) for x in np.concatenate(face_box)]
    for fn in sorted(os.listdir(a.textures)):
        if not fn.endswith(".png"):
            continue
        k = kind_of(fn)
        small, full = reduce_texture(os.path.join(a.textures, fn), a.tex_size, k)
        mode = "RGBA" if small.shape[2] == 4 else "RGB"
        im = Image.fromarray(np.clip(np.round(small * 255), 0, 255).astype(np.uint8), mode)
        im.save(os.path.join(tex_plain, fn))
        raw = astc_tool.encode_image(im.convert("RGBA"), bx, by, srgb=(k == "color"), quality=a.quality,
                                     normal=False)
        dec = astc_tool.decode_raw(raw, im.width, im.height, bx, by, srgb=(k == "color")).convert(mode)
        dec.save(os.path.join(tex_game, fn))
        da = np.asarray(dec, np.float64) / 255
        up = np.asarray(dec.convert("RGB").resize((full.shape[1], full.shape[0]), Image.Resampling.BICUBIC),
                        np.float64) / 255           # RGB only: PIL premultiplies RGBA when resizing
        nc = 3
        rep["textures"][fn] = {"kind": k, "hd_size": list(full.shape[:2]), "game_size": a.tex_size,
                               "astc_block": a.block, "astc_bytes": len(raw),
                               "psnr_astc_vs_256_db": psnr(da[..., :nc], small[..., :nc]),
                               "psnr_game_upsampled_vs_hd_db": psnr(up[..., :nc], full[..., :nc])}
        if face_box is not None and fn.startswith("r9_head"):
            Hh, Wh = full.shape[:2]
            (u0, v0), (u1, v1) = face_box
            ys = slice(int((1 - v1) * Hh), int(np.ceil((1 - v0) * Hh)))
            xs = slice(int(u0 * Wh), int(np.ceil(u1 * Wh)))
            rep["textures"][fn]["psnr_game_upsampled_vs_hd_face_crop_db"] = psnr(up[ys, xs, :nc], full[ys, xs, :nc])
            rep["textures"][fn]["face_crop_px_hd"] = [xs.stop - xs.start, ys.stop - ys.start]
            rep["textures"][fn]["face_crop_px_game"] = [int((xs.stop - xs.start) * a.tex_size / Wh),
                                                        int((ys.stop - ys.start) * a.tex_size / Hh)]
        log(f"{fn}: {k}, ASTC {a.block} PSNR {rep['textures'][fn]['psnr_astc_vs_256_db']:.2f} dB, "
            f"256->HD PSNR {rep['textures'][fn]['psnr_game_upsampled_vs_hd_db']:.2f} dB")
    # eye texture: materials.py falls back to MakeHuman's brown_eye.png (RGBA: the cornea region is
    # transparent and materials.py honours that alpha) - give the game set a 256 RGBA ASTC copy
    eye = "/opt/tools/mh/makehuman/makehuman/data/eyes/materials/brown_eye.png"
    if os.path.exists(eye) and not os.path.exists(os.path.join(a.textures, "r9_eye_albedo.png")):
        small, full = reduce_texture(eye, a.tex_size, "color")
        im = Image.fromarray(np.clip(np.round(small * 255), 0, 255).astype(np.uint8), "RGBA")
        raw = astc_tool.encode_image(im, bx, by, True, a.quality)
        dec = astc_tool.decode_raw(raw, a.tex_size, a.tex_size, bx, by, True)
        dec.save(os.path.join(tex_game, "r9_eye_albedo.png"))
        rep["textures"]["r9_eye_albedo.png (MakeHuman brown_eye.png)"] = {
            "kind": "color", "game_size": a.tex_size, "astc_block": a.block, "astc_bytes": len(raw),
            "psnr_astc_vs_256_db": psnr(np.asarray(dec, np.float64)[..., :3] / 255, small[..., :3])}
    # 3. renders
    if not a.no_render:
        variants = [("hd", os.path.abspath(a.blend), os.path.abspath(a.textures)),
                    ("lod0", os.path.join(os.path.abspath(a.out_dir), "lod0.blend"), os.path.abspath(tex_game)),
                    ("lod1", os.path.join(os.path.abspath(a.out_dir), "lod1.blend"), os.path.abspath(tex_game))]
        rdir = os.path.join(a.out_dir, "renders")
        for tag, bl, tx in variants:
            log("render", tag)
            blender([bl, "--python", RENDER, "--", "--outdir", rdir, "--views", a.views, "--res", str(a.res),
                     "--samples", str(a.samples), "--textures", tx, "--tag", tag])
        views = a.views.split(",")
        rows = [[os.path.join(rdir, f"{v}_{t}.png") for t, _, _ in variants] for v in views]
        cnt = {g["lod"]: g["gpu_verts"] for g in st["groups"] if g["objects"] == a.parts}
        hd = sum(st["hd"][p]["gpu_verts"] for p in a.parts.split(",") if p in st["hd"])
        labels = [f"HD: {hd} GPU verts, {a.textures and 'HD textures'}",
                  f"LOD0: {cnt.get(0)} verts, {a.tex_size}px ASTC {a.block}",
                  f"LOD1: {cnt.get(1)} verts, {a.tex_size}px ASTC {a.block}"]
        rep["sheet"] = contact_sheet(rows, variants, labels, os.path.join(a.out_dir, "hd_vs_game_sheet.png"))
        rep["renders"] = rows
    rep["seconds"] = round(time.time() - t0, 1)
    json.dump(rep, open(os.path.join(a.out_dir, "game_precision_report.json"), "w"), indent=1, default=str)
    log("wrote", os.path.join(a.out_dir, "game_precision_report.json"), f"({rep['seconds']} s)")


if __name__ == "__main__":
    main()
