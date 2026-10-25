"""Procedural, photo-free texture set for the R9 head (and hair shell).

  blender -b model/source/r9_2002.blend --python tools/model/texture_build.py -- \
      --corr model/source/face_corr_mediapipe478.json --palette model/source/r9_skin_palette.json \
      --out model/textures --size 2048

Why procedural: the likeness must come from the geometry (brief 4.1: no photo pasted on a
non-matching head), the press photos are copyrighted, and a 256 px game face texture keeps
only low-frequency colour anyway. Colours come from research/notes/01_appearance_2002.md §3.3.

Method: every texel of the head UV ("HeadUV") is mapped back to its 3D rest position by
rasterising the UV triangles; region masks are then defined in 3D relative to the MediaPipe
landmark positions on the mesh (brows, lids, lips, nostrils, beard zone) and the hair patch
vertex group (shaved scalp vs. Cascao patch). Outputs (sRGB PNG unless noted):
  r9_head_albedo.png, r9_head_normal.png (tangent-space, OpenGL +Y), r9_head_rough.png (linear),
  r9_head_masks.png (R=hair patch, G=shaved scalp, B=lips, A=brows; linear, for re-use),
  r9_hair_albedo.png (RGBA), r9_hair_normal.png
"""
import argparse
import json
import math
import os
import sys

import bpy
import numpy as np

argv = sys.argv[sys.argv.index("--") + 1:]
ap = argparse.ArgumentParser()
ap.add_argument("--corr", required=True)
ap.add_argument("--palette", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--size", type=int, default=2048)
ap.add_argument("--seed", type=int, default=2002)
a = ap.parse_args(argv)
os.makedirs(a.out, exist_ok=True)
PAL = json.load(open(a.palette))
RNG = np.random.default_rng(a.seed)
N = a.size


def srgb_to_lin(c):
    c = np.asarray(c, float)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def lin_to_srgb(c):
    c = np.clip(c, 0, 1)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)


def hexc(h):
    h = h.lstrip("#")
    return srgb_to_lin([int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)])


# ----------------------------------------------------------------------------- raster
def rasterise(uv_tris, attr_tris, size):
    """uv_tris (T,3,2) in [0,1]; attr_tris (T,3,C). Returns (size,size,C) map and coverage mask."""
    C = attr_tris.shape[2]
    out = np.zeros((size, size, C), np.float32)
    cov = np.zeros((size, size), bool)
    P = uv_tris * (size - 1)
    for t in range(len(P)):
        (x0, y0), (x1, y1), (x2, y2) = P[t]
        xmin, xmax = int(max(0, math.floor(min(x0, x1, x2)))), int(min(size - 1, math.ceil(max(x0, x1, x2))))
        ymin, ymax = int(max(0, math.floor(min(y0, y1, y2)))), int(min(size - 1, math.ceil(max(y0, y1, y2))))
        if xmax < xmin or ymax < ymin:
            continue
        xs, ys = np.meshgrid(np.arange(xmin, xmax + 1), np.arange(ymin, ymax + 1))
        den = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if abs(den) < 1e-12:
            continue
        w0 = ((y1 - y2) * (xs - x2) + (x2 - x1) * (ys - y2)) / den
        w1 = ((y2 - y0) * (xs - x2) + (x0 - x2) * (ys - y2)) / den
        w2 = 1 - w0 - w1
        m = (w0 >= -1e-3) & (w1 >= -1e-3) & (w2 >= -1e-3)
        if not m.any():
            continue
        A = attr_tris[t]
        val = w0[m, None] * A[0] + w1[m, None] * A[1] + w2[m, None] * A[2]
        out[ys[m], xs[m]] = val
        cov[ys[m], xs[m]] = True
    return out[::-1], cov[::-1]  # image row 0 = top (v = 1)


def dilate(img, cov, iters=16):
    """Push colours outward into empty texels so mip-maps/ASTC blocks do not bleed background."""
    img = img.copy()
    cov = cov.copy()
    for _ in range(iters):
        acc = np.zeros_like(img)
        cnt = np.zeros(cov.shape, np.float32)
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            sh = np.roll(np.roll(cov, dy, 0), dx, 1)
            acc += np.roll(np.roll(img * cov[..., None], dy, 0), dx, 1)
            cnt += sh
        new = (~cov) & (cnt > 0)
        img[new] = acc[new] / cnt[new, None]
        cov |= new
    return img


def value_noise(size, cells, rng, octaves=4):
    out = np.zeros((size, size), np.float32)
    amp, tot = 1.0, 0.0
    for o in range(octaves):
        c = cells * 2 ** o
        g = rng.random((c + 1, c + 1)).astype(np.float32)
        x = np.linspace(0, c, size, endpoint=False)
        xi = x.astype(int)
        xf = x - xi
        xf = xf * xf * (3 - 2 * xf)
        a00 = g[np.ix_(xi, xi)]
        a10 = g[np.ix_(xi + 1, xi)]
        a01 = g[np.ix_(xi, xi + 1)]
        a11 = g[np.ix_(xi + 1, xi + 1)]
        fy, fx = xf[:, None], xf[None, :]
        out += amp * (a00 * (1 - fy) * (1 - fx) + a10 * fy * (1 - fx) + a01 * (1 - fy) * fx + a11 * fy * fx)
        tot += amp
        amp *= 0.5
    return out / tot


def noise3(P, freq, seed, octaves=3):
    """Isotropic 3D value noise evaluated at surface positions P (..., 3) in metres -> [0,1].
    Being defined in 3D it does not stretch with the UV layout."""
    rng = np.random.default_rng(seed)
    perm = rng.permutation(256)
    vals = rng.random(256).astype(np.float32)
    out = np.zeros(P.shape[:-1], np.float32)
    amp, tot = 1.0, 0.0
    for o in range(octaves):
        Q = P * freq * 2 ** o
        I = np.floor(Q).astype(np.int64)
        F = (Q - I).astype(np.float32)
        F = F * F * (3 - 2 * F)
        acc = np.zeros_like(out)
        for dx in (0, 1):
            for dy in (0, 1):
                for dz in (0, 1):
                    h = perm[(perm[(perm[(I[..., 0] + dx) & 255] + I[..., 1] + dy) & 255] + I[..., 2] + dz) & 255]
                    w = (F[..., 0] if dx else 1 - F[..., 0]) * (F[..., 1] if dy else 1 - F[..., 1]) * \
                        (F[..., 2] if dz else 1 - F[..., 2])
                    acc += w * vals[h]
        out += amp * acc
        tot += amp
        amp *= 0.5
    return out / tot


def smooth(x, e0, e1):
    t = np.clip((x - e0) / (e1 - e0), 0, 1)
    return t * t * (3 - 2 * t)


def save(path, arr, srgb=True):
    if srgb:
        arr = lin_to_srgb(arr)
    arr = np.clip(arr, 0, 1)
    h, w, c = arr.shape
    img = bpy.data.images.new(os.path.basename(path), w, h, alpha=(c == 4))
    if c == 3:
        arr = np.concatenate([arr, np.ones((h, w, 1))], 2)
    img.pixels.foreach_set(arr[::-1].astype(np.float32).ravel())
    img.filepath_raw = path
    img.file_format = "PNG"
    img.save()
    print("WROTE", path)


# ----------------------------------------------------------------------------- mesh data
head = bpy.data.objects["R9_Head"]
me = head.data
uvl = me.uv_layers["HeadUV"]
co = np.array([v.co[:] for v in me.vertices])
mh_index = np.zeros(len(me.vertices), int)
me.attributes["mh_index"].data.foreach_get("value", mh_index)
inv = {int(m): i for i, m in enumerate(mh_index)}
hair_vg = head.vertex_groups.get("hair_patch")
hair_w = np.zeros(len(co))
if hair_vg:
    for v in me.vertices:
        for g in v.groups:
            if g.group == hair_vg.index:
                hair_w[v.index] = g.weight

tri_uv, tri_attr = [], []
nrm = np.array([v.normal[:] for v in me.vertices])
for p in me.polygons:
    li = list(p.loop_indices)
    for k in range(1, len(li) - 1):
        ls = (li[0], li[k], li[k + 1])
        vs = [me.loops[l].vertex_index for l in ls]
        tri_uv.append([uvl.data[l].uv[:] for l in ls])
        tri_attr.append([list(co[v]) + list(nrm[v]) + [hair_w[v]] for v in vs])
tri_uv = np.array(tri_uv)
tri_attr = np.array(tri_attr, np.float32)
amap, cov = rasterise(tri_uv, tri_attr, N)
POS, NRM, HAIR = amap[..., 0:3], amap[..., 3:6], amap[..., 6]

# landmarks in Blender object space
corr = json.load(open(a.corr))
L = np.zeros((478, 3))
for i, c in enumerate(corr):
    pts = [co[inv[v]] if v in inv else None for v in c["v"]]
    if any(p is None for p in pts):
        L[i] = np.nan
        continue
    L[i] = sum(w * p for w, p in zip(c["w"], pts))


def dist_to_polyline(P, ids, closed=False):
    pts = L[ids]
    if closed:
        pts = np.vstack([pts, pts[:1]])
    d = np.full(P.shape[:-1], np.inf, np.float32)
    for a_, b_ in zip(pts[:-1], pts[1:]):
        ab = b_ - a_
        t = np.clip(((P - a_) @ ab) / max(ab @ ab, 1e-12), 0, 1)
        d = np.minimum(d, np.linalg.norm(P - (a_ + t[..., None] * ab), axis=-1))
    return d


# feature curves (MediaPipe topology)
BROW_R = [70, 63, 105, 66, 107]           # image-left brow
BROW_L = [336, 296, 334, 293, 300]
BROW_R_LOW = [46, 53, 52, 65, 55]
BROW_L_LOW = [285, 295, 282, 283, 276]
LIP_OUTER = [61, 185, 40, 39, 37, 0, 267, 269, 270, 409, 291, 375, 321, 405, 314, 17, 84, 181, 91, 146]
LIP_INNER = [78, 191, 80, 81, 82, 13, 312, 311, 310, 415, 308, 324, 318, 402, 317, 14, 87, 178, 88, 95]
EYE_R = [33, 246, 161, 160, 159, 158, 157, 173, 133, 155, 154, 153, 145, 144, 163, 7]
EYE_L = [263, 466, 388, 387, 386, 385, 384, 398, 362, 382, 381, 380, 374, 373, 390, 249]
NOSTRIL = [98, 327, 79, 309, 240, 460]
cm = 0.01  # Blender metres

# ----------------------------------------------------------------------------- albedo
skin = hexc(PAL["skin_lit"])
nz_lo = noise3(POS, 12.0, a.seed, 4)[..., None]           # ~8 cm blotches
nz_mid = noise3(POS, 90.0, a.seed + 1, 3)[..., None]       # ~1 cm mottling
alb = np.broadcast_to(skin, (N, N, 3)).astype(np.float32).copy()
alb *= (0.92 + 0.16 * nz_lo) * (0.97 + 0.06 * nz_mid)
# warmer/redder cheeks, nose and ears; slightly darker periorbital and under-jaw
cheek = np.exp(-(np.minimum(np.linalg.norm(POS - L[50], axis=-1), np.linalg.norm(POS - L[280], axis=-1)) / (2.2 * cm)) ** 2)
alb = alb * (1 - 0.10 * cheek[..., None]) + hexc(PAL["skin_warm"]) * 0.10 * cheek[..., None]
peri = np.exp(-(np.minimum(dist_to_polyline(POS, EYE_R, True), dist_to_polyline(POS, EYE_L, True)) / (0.9 * cm)) ** 2)
alb *= (1 - 0.18 * peri[..., None])
nostr = np.exp(-(np.min([np.linalg.norm(POS - L[i], axis=-1) for i in NOSTRIL], axis=0) / (0.45 * cm)) ** 2)
alb *= (1 - 0.35 * nostr[..., None])
# lips
lip_d = dist_to_polyline(POS, LIP_OUTER, True)
poly = L[LIP_OUTER][:, [0, 2]]
X, Z = POS[..., 0], POS[..., 2]
inside_lip = np.zeros(X.shape, bool)
for (x1, z1), (x2, z2) in zip(poly, np.roll(poly, -1, 0)):
    cond = ((z1 > Z) != (z2 > Z)) & (X < (x2 - x1) * (Z - z1) / ((z2 - z1) + 1e-12) + x1)
    inside_lip ^= cond
c_out = L[LIP_OUTER].mean(0)
inside_lip &= (POS[..., 1] < c_out[1] + 1.5 * cm)          # front of the face only
lip_mask = np.where(inside_lip, 1.0, 1 - smooth(lip_d, 0.0, 0.12 * cm)).astype(np.float32)
alb = alb * (1 - lip_mask[..., None]) + hexc(PAL["lip"]) * (0.9 + 0.2 * nz_mid) * lip_mask[..., None]
# brows: dense dark short hairs between upper and lower brow curves
def brow_mask(up, low):
    mid = 0.5 * (L[up] + L[low])
    width = np.mean(np.linalg.norm(L[up] - L[low], axis=-1))
    dc = np.full(POS.shape[:-1], np.inf, np.float32)
    for a_, b_ in zip(mid[:-1], mid[1:]):
        ab = b_ - a_
        t = np.clip(((POS - a_) @ ab) / max(ab @ ab, 1e-12), 0, 1)
        dc = np.minimum(dc, np.linalg.norm(POS - (a_ + t[..., None] * ab), axis=-1))
    return 1 - smooth(dc, 0.35 * width, 0.65 * width)
brows = np.maximum(brow_mask(BROW_R, BROW_R_LOW), brow_mask(BROW_L, BROW_L_LOW))
hair_strands = noise3(POS * np.array([1.0, 1.0, 3.0]), 900.0, a.seed + 2, 2)
brows = brows * (0.75 + 0.25 * smooth(hair_strands, 0.3, 0.6))
alb = alb * (1 - 0.92 * brows[..., None]) + hexc(PAL["brow"]) * 0.92 * brows[..., None]
# lash line / upper lid shadow
lash = np.exp(-(np.minimum(dist_to_polyline(POS, EYE_R[:9]), dist_to_polyline(POS, EYE_L[:9])) / (0.12 * cm)) ** 2)
alb *= (1 - 0.7 * lash[..., None])
# scalp: hair-bearing region bounded by a natural hairline that runs round the head:
# forehead -> temples -> (shaved) sideburns in front of the ear -> behind the ear -> nape.
# The threshold height is a smooth function of azimuth about the head's vertical axis.
z_up = POS[..., 2]
brow_z = np.nanmean(L[BROW_R + BROW_L][:, 2])
hairline_z = brow_z + PAL["hairline_above_brow_cm"] * cm
ear_lm = L[[234, 454]]                                  # face-oval points at the ears
tragus_z = float(np.nanmean(ear_lm[:, 2]))
centre = np.array([0.0, float(np.nanmean(ear_lm[:, 1])), tragus_z])
az = np.degrees(np.arctan2(np.abs(POS[..., 0] - centre[0]), -(POS[..., 1] - centre[1])))  # 0 front, 180 back
lobe_z = tragus_z - 2.0 * cm
knots_az = np.array([0, 45, 62, 80, 100, 135, 180], float)
knots_z = np.array([hairline_z, hairline_z - 0.6 * cm, hairline_z - 2.0 * cm, tragus_z - 0.5 * cm,
                    lobe_z + 1.5 * cm, lobe_z - 1.0 * cm, lobe_z - 3.0 * cm])
thr = np.interp(az, knots_az, knots_z)
scalp = smooth(z_up - thr, -0.15 * cm, 0.25 * cm).astype(np.float32)
patch = smooth(HAIR, 0.3, 0.7)
shaved = scalp * (1 - patch)
# ears: never stubble (vertex set from the MakeHuman ear morph targets)
ear_ids = set()
for side in ("l", "r"):
    tf = os.path.join(os.environ.get("MH_DATA", "/opt/tools/mh/makehuman/makehuman/data"), "targets", "ears", f"{side}-ear-wing-incr.target")
    for line in open(tf):
        if line.strip() and not line.startswith("#"):
            ear_ids.add(int(line.split()[0]))
ear_pts = np.array([co[inv[i]] for i in ear_ids if i in inv])
from mathutils.kdtree import KDTree
kd = KDTree(len(ear_pts))
for i, p_ in enumerate(ear_pts):
    kd.insert(p_, i)
kd.balance()
flatP = POS.reshape(-1, 3)
ear_d = np.array([kd.find(p_)[2] for p_ in flatP], np.float32).reshape(POS.shape[:-1])
shaved *= smooth(ear_d, 0.4 * cm, 1.0 * cm)
# sub-millimetre stubble cannot be resolved by a 1-2 mm texel (and certainly not at the game's
# 256 px), so it is rendered as an averaged tone plus a little ~2 mm noise, not as dots
stub_cov = PAL["stubble_coverage"]
dots = (stub_cov * (0.8 + 0.4 * noise3(POS, 500.0, a.seed + 3, 2))).astype(np.float32)
scalp_base = alb * 0.85 + hexc(PAL["scalp_tint"]) * 0.15
scalp_col = scalp_base * (1 - dots[..., None]) + hexc(PAL["stubble"]) * dots[..., None]
alb = alb * (1 - shaved[..., None]) + scalp_col * shaved[..., None]
alb = alb * (1 - patch[..., None]) + hexc(PAL["hair"]) * patch[..., None]
# very light beard shadow (final was clean-shaven; <=10 % per dossier §4)
chin_zone = np.exp(-(np.linalg.norm(POS - L[152], axis=-1) / (3.0 * cm)) ** 2) + \
    np.exp(-(np.linalg.norm(POS - L[0] - np.array([0, 0, 0.6 * cm]), axis=-1) / (1.2 * cm)) ** 2)
beard = np.clip(chin_zone, 0, 1) * (1 - lip_mask) * PAL["beard_opacity"]
alb = alb * (1 - beard[..., None]) + hexc(PAL["stubble"]) * beard[..., None]
alb = dilate(alb, cov)

# ----------------------------------------------------------------------------- normal + roughness
h = 0.6 * noise3(POS, 1500.0, a.seed + 4, 2) + 0.4 * noise3(POS, 4000.0, a.seed + 5, 1)   # pores
h += 0.3 * noise3(POS, 1200.0, a.seed + 6, 1) * shaved                          # stubble texture
h -= 1.2 * lash
gy, gx = np.gradient(h.astype(np.float32))
strength = PAL["normal_strength"]
nmap = np.stack([-gx * strength, gy * strength, np.ones_like(gx)], -1)
nmap /= np.linalg.norm(nmap, axis=-1, keepdims=True)
nmap = nmap * 0.5 + 0.5
rough = np.full((N, N, 1), PAL["roughness_face"], np.float32)
rough[..., 0] -= 0.12 * shaved            # sweat/rain sheen on the shaved scalp
rough[..., 0] += 0.15 * patch
rough[..., 0] -= 0.08 * lip_mask
masks = np.stack([patch, shaved, lip_mask, brows], -1).astype(np.float32)

save(os.path.join(a.out, "r9_head_albedo.png"), alb)
save(os.path.join(a.out, "r9_head_normal.png"), dilate(nmap, cov), srgb=False)
save(os.path.join(a.out, "r9_head_rough.png"), np.repeat(dilate(rough, cov), 3, 2), srgb=False)
save(os.path.join(a.out, "r9_head_masks.png"), masks, srgb=False)

# ----------------------------------------------------------------------------- hair shell texture
M = 1024
curl = value_noise(M, 160, RNG, 3)
curl2 = value_noise(M, 420, RNG, 2)
tight = np.abs(np.sin(curl * 40.0)) * 0.6 + curl2 * 0.4
hair_alb = hexc(PAL["hair"]) * (0.7 + 0.6 * tight[..., None])
hair_rgba = np.concatenate([hair_alb, np.ones((M, M, 1))], 2)
save(os.path.join(a.out, "r9_hair_albedo.png"), hair_rgba)
gy, gx = np.gradient(tight.astype(np.float32))
hn = np.stack([-gx * 6, gy * 6, np.ones_like(gx)], -1)
hn /= np.linalg.norm(hn, axis=-1, keepdims=True)
save(os.path.join(a.out, "r9_hair_normal.png"), hn * 0.5 + 0.5, srgb=False)
print("TEXTURES_DONE")
