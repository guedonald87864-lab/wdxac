#!/usr/bin/env python3
"""Bake our HD head textures onto the DONOR UV layout through the wrapped donor surface.

  python3 tools/wrap/transfer_texture.py --wrap OUT/wrapped.npz --target head.npz --out-dir TEX \
      [--albedo r9_head_albedo.png] [--normal r9_head_normal.png] [--sizes 2048,256] \
      [--block 4x4] [--ray-mm 3] [--dilate 16]

Per donor texel (pixel centre (i+0.5)/N; README §5):
  1. rasterise the donor UV triangles -> (triangle, barycentric) of every covered texel;
  2. 3D point P and normal N on the WRAPPED donor surface (wrap.py output, work frame);
  3. two-sided ray P +- t N (|t| <= --ray-mm) against our HD head; the nearest hit whose surface
     normal faces the same way wins; otherwise the exact closest point on the HD head (fallback);
  4. our texture is sampled there with bilinear filtering in LINEAR light (sRGB decoded first),
     using the HD head's own UV ("HeadUV");
  5. islands are dilated (--dilate px, push-pull average of covered neighbours), the rest of the
     image is filled with the mean colour so mip-maps never pick up black;
  6. smaller sizes (256 = game size) come from the full-size bake by coverage-weighted box
     filtering in linear light (exact integer factors), then their own dilation - never by
     re-baking at low resolution (which would alias);
  7. every size is pushed through ASTC (tools/native/astc_tool.py, ARM astcenc) and decoded back:
     the decoded PNG is the preview of what the game would show; PSNR vs. the uncompressed bake.
Normal maps (optional): the HD map (tangent space of our HeadUV, OpenGL +Y) is decoded to object
space with per-corner tangents of the HD head, and re-encoded in the DONOR's MikkTSpace frame
(tangents written by wrap.py, computed on the native buffers): G = n . (w N x T). For a donor whose
native UV origin is top-left (UE/DirectX) this is DirectX-style green, otherwise OpenGL-style.
A consistency check (--check, default on) compares, at points of our HD face surface and at the
MediaPipe landmarks, the HD texture colour with the colour read back through the donor UVs of the
baked maps (CIEDE2000 and PSNR) - the surface-space equivalent of rendering both with flat
unlit shading from every direction.
"""
import argparse
import json
import math
import os
import sys
import time

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "native"))
import astc_tool  # noqa: E402
import features as FT  # noqa: E402
import geom as G  # noqa: E402
import meshdata as MD  # noqa: E402


def log(*a):
    print("[tex]", *a, flush=True)


# ----------------------------------------------------------------------------- colour
def srgb_to_lin(c):
    c = np.asarray(c, np.float64)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def lin_to_srgb(c):
    c = np.clip(np.asarray(c, np.float64), 0, 1)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)


def srgb_to_lab(rgb):
    """sRGB [0,1] -> CIE Lab (D65)."""
    lin = srgb_to_lin(rgb)
    M = np.array([[0.4124564, 0.3575761, 0.1804375], [0.2126729, 0.7151522, 0.0721750],
                  [0.0193339, 0.1191920, 0.9503041]])
    xyz = lin @ M.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > (6 / 29) ** 3, np.cbrt(xyz), xyz / (3 * (6 / 29) ** 2) + 4 / 29)
    return np.stack([116 * f[:, 1] - 16, 500 * (f[:, 0] - f[:, 1]), 200 * (f[:, 1] - f[:, 2])], 1)


def delta_e2000(lab1, lab2):
    """CIEDE2000 (Sharma, Wu, Dalal 2005), vectorised."""
    L1, a1, b1 = lab1.T
    L2, a2, b2 = lab2.T
    C1, C2 = np.hypot(a1, b1), np.hypot(a2, b2)
    Cb = (C1 + C2) / 2
    G_ = 0.5 * (1 - np.sqrt(Cb ** 7 / (Cb ** 7 + 25 ** 7)))
    a1p, a2p = (1 + G_) * a1, (1 + G_) * a2
    C1p, C2p = np.hypot(a1p, b1), np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360
    dLp = L2 - L1
    dCp = C2p - C1p
    dh = h2p - h1p
    dh = np.where(C1p * C2p == 0, 0, np.where(dh > 180, dh - 360, np.where(dh < -180, dh + 360, dh)))
    dHp = 2 * np.sqrt(C1p * C2p) * np.sin(np.radians(dh / 2))
    Lbp = (L1 + L2) / 2
    Cbp = (C1p + C2p) / 2
    hs = h1p + h2p
    hbp = np.where(C1p * C2p == 0, hs, np.where(np.abs(h1p - h2p) > 180, np.where(hs < 360, (hs + 360) / 2, (hs - 360) / 2), hs / 2))
    T = (1 - 0.17 * np.cos(np.radians(hbp - 30)) + 0.24 * np.cos(np.radians(2 * hbp))
         + 0.32 * np.cos(np.radians(3 * hbp + 6)) - 0.20 * np.cos(np.radians(4 * hbp - 63)))
    dth = 30 * np.exp(-(((hbp - 275) / 25) ** 2))
    Rc = 2 * np.sqrt(Cbp ** 7 / (Cbp ** 7 + 25 ** 7))
    Sl = 1 + 0.015 * (Lbp - 50) ** 2 / np.sqrt(20 + (Lbp - 50) ** 2)
    Sc = 1 + 0.045 * Cbp
    Sh = 1 + 0.015 * Cbp * T
    Rt = -np.sin(np.radians(2 * dth)) * Rc
    return np.sqrt((dLp / Sl) ** 2 + (dCp / Sc) ** 2 + (dHp / Sh) ** 2 + Rt * (dCp / Sc) * (dHp / Sh))


# ----------------------------------------------------------------------------- image helpers
def load_image(path, linear):
    a = np.asarray(Image.open(path).convert("RGB"), np.float64) / 255.0
    return srgb_to_lin(a) if linear else a


def bilinear(img, uv):
    """img (H,W,C), uv (n,2) bottom-left origin, pixel centres at (i+0.5)/W; clamp-to-edge."""
    H, W = img.shape[:2]
    x = uv[:, 0] * W - 0.5
    y = (1 - uv[:, 1]) * H - 0.5
    x0 = np.floor(x).astype(int)
    y0 = np.floor(y).astype(int)
    fx, fy = (x - x0)[:, None], (y - y0)[:, None]
    xa, xb = np.clip(x0, 0, W - 1), np.clip(x0 + 1, 0, W - 1)
    ya, yb = np.clip(y0, 0, H - 1), np.clip(y0 + 1, 0, H - 1)
    return ((img[ya, xa] * (1 - fx) + img[ya, xb] * fx) * (1 - fy)
            + (img[yb, xa] * (1 - fx) + img[yb, xb] * fx) * fy)


def rasterise(uv_tris, size):
    """uv_tris (T,3,2) bottom-left. Returns pixel ids (k,) flat, tri (k,), bary (k,3)."""
    P = np.stack([uv_tris[..., 0] * size - 0.5, (1 - uv_tris[..., 1]) * size - 0.5], -1)   # pixel coords
    lo = np.clip(np.floor(P.min(1)).astype(int), 0, size - 1)
    hi = np.clip(np.ceil(P.max(1)).astype(int), 0, size - 1)
    pix, tri, bary = [], [], []
    a, b, c = P[:, 0], P[:, 1], P[:, 2]
    den = (b[:, 1] - c[:, 1]) * (a[:, 0] - c[:, 0]) + (c[:, 0] - b[:, 0]) * (a[:, 1] - c[:, 1])
    for t in np.nonzero(np.abs(den) > 1e-12)[0]:
        xs = np.arange(lo[t, 0], hi[t, 0] + 1)
        ys = np.arange(lo[t, 1], hi[t, 1] + 1)
        X, Y = np.meshgrid(xs, ys)
        X, Y = X.ravel(), Y.ravel()
        l1 = ((b[t, 1] - c[t, 1]) * (X - c[t, 0]) + (c[t, 0] - b[t, 0]) * (Y - c[t, 1])) / den[t]
        l2 = ((c[t, 1] - a[t, 1]) * (X - c[t, 0]) + (a[t, 0] - c[t, 0]) * (Y - c[t, 1])) / den[t]
        l3 = 1 - l1 - l2
        m = (l1 >= -1e-9) & (l2 >= -1e-9) & (l3 >= -1e-9)
        if m.any():
            pix.append(Y[m] * size + X[m])
            tri.append(np.full(m.sum(), t))
            bary.append(np.stack([l1[m], l2[m], l3[m]], 1))
    pix = np.concatenate(pix)
    tri = np.concatenate(tri)
    bary = np.concatenate(bary)
    # overlapping islands: keep the last writer (deterministic)
    _, last = np.unique(pix[::-1], return_index=True)
    keep = len(pix) - 1 - last
    return pix[keep], tri[keep], bary[keep]


def dilate(img, cov, iters):
    """`iters` passes of edge extension (each uncovered texel with covered 8-neighbours takes their
    mean), then a push-pull fill (coverage-weighted mip pyramid, coarse values pulled back into the
    remaining holes) so no texel - and no mip level - ever sees an undefined colour."""
    img = img.copy()
    cov = cov.copy()
    H, W, C = img.shape
    for _ in range(iters):
        if cov.all():
            break
        acc = np.zeros_like(img)
        cnt = np.zeros((H, W))
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                sl_dst = (slice(max(dy, 0), H + min(dy, 0)), slice(max(dx, 0), W + min(dx, 0)))
                sl_src = (slice(max(-dy, 0), H + min(-dy, 0)), slice(max(-dx, 0), W + min(-dx, 0)))
                acc[sl_dst] += img[sl_src] * cov[sl_src][..., None]
                cnt[sl_dst] += cov[sl_src]
        new = (~cov) & (cnt > 0)
        img[new] = acc[new] / cnt[new][:, None]
        cov = cov | new
    if not cov.all():
        img = push_pull(img, cov)
    return img


def push_pull(img, cov):
    levels = [(img * cov[..., None], cov.astype(float))]
    while max(levels[-1][1].shape) > 1:
        a, w = levels[-1]
        h, ww = a.shape[:2]
        if h % 2 or ww % 2:
            a = np.pad(a, ((0, h % 2), (0, ww % 2), (0, 0)))
            w = np.pad(w, ((0, h % 2), (0, ww % 2)))
        a2 = a.reshape(a.shape[0] // 2, 2, a.shape[1] // 2, 2, -1).sum((1, 3))
        w2 = w.reshape(w.shape[0] // 2, 2, w.shape[1] // 2, 2).sum((1, 3))
        levels.append((a2, w2))
    filled = levels[-1][0] / np.maximum(levels[-1][1], 1e-12)[..., None]
    for a, w in reversed(levels[:-1]):
        up = np.repeat(np.repeat(filled, 2, 0), 2, 1)[:a.shape[0], :a.shape[1]]
        mean = a / np.maximum(w, 1e-12)[..., None]
        filled = np.where((w > 0)[..., None], mean, up)
    out = img.copy()
    out[~cov] = filled[~cov]
    return out


def box_down(img_lin, cov, factor):
    H, W, C = img_lin.shape
    h, w = H // factor, W // factor
    m = cov.astype(float)[..., None]
    num = (img_lin * m).reshape(h, factor, w, factor, C).sum((1, 3))
    den = m.reshape(h, factor, w, factor, 1).sum((1, 3))
    out = np.where(den > 0, num / np.maximum(den, 1e-12), 0)
    return out, den[..., 0] > 0


def save_png(path, arr01):
    Image.fromarray(np.clip(np.round(arr01 * 255), 0, 255).astype(np.uint8)).save(path)


def psnr(a, b):
    mse = np.mean((np.asarray(a, float) - np.asarray(b, float)) ** 2)
    return float("inf") if mse == 0 else float(10 * math.log10(1.0 / mse))


# ----------------------------------------------------------------------------- core
class HDSource:
    def __init__(self, target_npz, part="head", uv_layer=None):
        A, meta = MD.load_npz(target_npz)
        fd = meta["frame"]
        self.P = MD.frame_to_blender(A[f"{part}.positions"], fd)
        F = A[f"{part}.faces"].astype(np.int64)
        layer = uv_layer or meta.get("head_uv", "HeadUV")
        uv = A[f"{part}.uv.{layer}"].astype(np.float64)
        if G.orientation_sign(self.P, F) < 0:         # corners and UVs are reversed together
            F, uv = F[:, ::-1].copy(), uv[:, ::-1].copy()
        self.F, self.uv = F, uv
        self.N = MD.vertex_normals(self.P, F)
        self.q = G.TriMeshQuery(self.P, F)
        self.meta, self.A = meta, A
        self.region = A.get(f"{part}.region")
        self.L = MD.frame_to_blender(A["landmarks.mp478"], fd) if "landmarks.mp478" in A else None
        self._tan = None

    def corner_tangents(self):
        """Per-corner (F,3,4) tangents of the HD head on its UV layer (Lengyel, split at UV seams)."""
        if self._tan is None:
            key = np.concatenate([self.F.reshape(-1, 1).astype(float), np.round(self.uv.reshape(-1, 2), 6)], 1)
            _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
            src = self.F.reshape(-1)[first]
            Fs = inv.reshape(-1, 3)
            uvs = self.uv.reshape(-1, 2)[first]
            Ps, Ns = self.P[src], self.N[src]
            import wrap as WR
            T = WR.tangents_numpy(Ps, Fs, uvs, Ns)
            self._tan = T[Fs]
        return self._tan

    def locate(self, P, N, ray_m):
        """Ray first (two-sided, along N, normal-compatible), exact closest point otherwise."""
        hit, t, tri, bary = self.q.ray_nearest_hit(P, N, ray_m, k=24)
        if hit.any():
            n_hit = self.q.interp(np.maximum(tri, 0), bary, self.N)
            hit &= (n_hit * N).sum(1) > 0
        miss = ~hit
        tri = tri.copy()
        bary = bary.copy()
        dist = np.where(hit, t, np.nan)
        if miss.any():
            c = self.q.closest(P[miss])
            tri[miss], bary[miss], dist[miss] = c["tri"], c["bary"], c["dist"]
        return tri, np.clip(bary, 0, 1), dist, hit

    def uv_at(self, tri, bary):
        return (self.uv[tri] * bary[:, :, None]).sum(1)


def bake(src, Vw, Nw, F, uv_bl, size, ray_m, albedo_lin=None, normal_map=None, Tw=None, chunk=400000):
    """Returns dict(cov, albedo_lin (size,size,3), normal (size,size,3) or None, stats)."""
    t0 = time.time()
    pix, tri, bary = rasterise(uv_bl[F] if uv_bl.ndim == 2 else uv_bl, size)
    out_a = np.zeros((size * size, 3))
    out_n = np.zeros((size * size, 3)) if normal_map is not None else None
    hits, dists = np.zeros(len(pix), bool), np.zeros(len(pix))
    for s in range(0, len(pix), chunk):
        tt, bb = tri[s:s + chunk], bary[s:s + chunk]
        P = (Vw[F[tt]] * bb[:, :, None]).sum(1)
        N = (Nw[F[tt]] * bb[:, :, None]).sum(1)
        N /= np.maximum(np.linalg.norm(N, axis=1, keepdims=True), 1e-30)
        htri, hbary, d, hit = src.locate(P, N, ray_m)
        hits[s:s + chunk], dists[s:s + chunk] = hit, d
        uvh = src.uv_at(htri, hbary)
        if albedo_lin is not None:
            out_a[pix[s:s + chunk]] = bilinear(albedo_lin, uvh)
        if normal_map is not None:
            ts = bilinear(normal_map, uvh) * 2 - 1                      # OpenGL tangent space of HeadUV
            CT = src.corner_tangents()[htri]                             # (k,3,4)
            T = (CT[:, :, :3] * hbary[:, :, None]).sum(1)
            w = np.where((CT[:, :, 3] * hbary).sum(1) < 0, -1.0, 1.0)
            Nn = src.q.interp(htri, hbary, src.N)
            Nn /= np.linalg.norm(Nn, axis=1, keepdims=True)
            T = T - Nn * (T * Nn).sum(1, keepdims=True)
            T /= np.maximum(np.linalg.norm(T, axis=1, keepdims=True), 1e-30)
            B = w[:, None] * np.cross(Nn, T)
            n_obj = ts[:, :1] * T + ts[:, 1:2] * B + ts[:, 2:3] * Nn
            n_obj /= np.maximum(np.linalg.norm(n_obj, axis=1, keepdims=True), 1e-30)
            Td = (Tw[F[tt], :3] * bb[:, :, None]).sum(1)
            wd = np.where((Tw[F[tt], 3] * bb).sum(1) < 0, -1.0, 1.0)
            Td = Td - N * (Td * N).sum(1, keepdims=True)
            Td /= np.maximum(np.linalg.norm(Td, axis=1, keepdims=True), 1e-30)
            Bd = wd[:, None] * np.cross(N, Td)
            enc = np.stack([(n_obj * Td).sum(1), (n_obj * Bd).sum(1), (n_obj * N).sum(1)], 1)
            out_n[pix[s:s + chunk]] = enc * 0.5 + 0.5
    cov = np.zeros(size * size, bool)
    cov[pix] = True
    st = {"size": size, "covered_texels": int(len(pix)), "coverage": float(len(pix) / size ** 2),
          "ray_hit_fraction": float(hits.mean()), "closest_point_fallback_fraction": float((~hits).mean()),
          "source_distance_mm": G.stats_mm(dists), "seconds": round(time.time() - t0, 2)}
    return {"cov": cov.reshape(size, size), "albedo": out_a.reshape(size, size, 3),
            "normal": None if out_n is None else out_n.reshape(size, size, 3), "stats": st}


def astc_roundtrip(img01, path_png, block, srgb=True, normal=False, quality="-medium"):
    im = Image.fromarray(np.clip(np.round(img01 * 255), 0, 255).astype(np.uint8)).convert("RGBA")
    bx, by = (int(x) for x in block.split("x"))
    raw = astc_tool.encode_image(im, bx, by, srgb, quality, normal)
    back = astc_tool.decode_raw(raw, im.width, im.height, bx, by, srgb).convert("RGB")
    back.save(path_png)
    a = np.asarray(back, np.float64) / 255
    return a, {"block": block, "bytes": len(raw), "bpp": 8 * len(raw) / (im.width * im.height),
               "psnr_db_vs_uncompressed": psnr(a, np.asarray(im.convert("RGB"), float) / 255),
               "decoded_png": path_png}


def consistency_check(src, Vw, F, uv_bl, maps, albedo_hd_lin, n=40000, seed=5):
    """HD texture colour vs. colour read back through the donor UVs of each baked map."""
    region = src.region
    face_t = np.isin(region, [FT.REGION_ID["face"]]) if region is not None else np.ones(len(src.P), bool)
    tmask = face_t[src.F].all(1).astype(float)
    Ps, tri, bary = G.sample_surface(src.P, src.F, n, seed, tri_mask=tmask)
    ref = lin_to_srgb(bilinear(albedo_hd_lin, src.uv_at(tri, bary)))
    dq = G.TriMeshQuery(Vw, F)
    out = {}
    pts = {"face_surface": Ps}
    if src.L is not None:
        ok = np.all(np.isfinite(src.L), 1)
        Lc = src.q.closest(src.L[ok])
        lab = region[src.F[Lc["tri"], np.argmax(Lc["bary"], 1)]] if region is not None else None
        keep = np.isin(lab, [FT.REGION_ID["face"]]) if lab is not None else np.ones(ok.sum(), bool)
        pts["landmarks_face"] = Lc["point"][keep]
        ref_l = lin_to_srgb(bilinear(albedo_hd_lin, src.uv_at(Lc["tri"][keep], Lc["bary"][keep])))
    uvd = uv_bl[F] if uv_bl.ndim == 2 else uv_bl
    for name, P in pts.items():
        c = dq.closest(P)
        uv = (uvd[c["tri"]] * c["bary"][:, :, None]).sum(1)
        r = ref if name == "face_surface" else ref_l
        for mname, img in maps.items():
            got = lin_to_srgb(bilinear(img, uv))
            de = delta_e2000(srgb_to_lab(r), srgb_to_lab(got))
            out[f"{name}/{mname}"] = {"n": int(len(P)), "psnr_db": psnr(r, got), "dE00_mean": float(de.mean()),
                                      "dE00_p50": float(np.median(de)), "dE00_p90": float(np.percentile(de, 90)),
                                      "dE00_p95": float(np.percentile(de, 95)), "dE00_max": float(de.max()),
                                      "frac_dE00_lt5": float((de < 5).mean())}
    return out


def run(a):
    t0 = time.time()
    os.makedirs(a.out_dir, exist_ok=True)
    R, rmeta = MD.load_npz(a.wrap)
    tag = a.lod
    Vw = R[f"{tag}.work_positions"]
    Nw = R[f"{tag}.work_normals"].astype(np.float64)
    F = R[f"{tag}.faces"].astype(np.int64)
    if f"{tag}.uv0" in R:
        uv_bl = MD.uv_to_bottom_left(R[f"{tag}.uv0"], rmeta["uv_origin"])
    else:
        uv_bl = MD.uv_to_bottom_left(R[f"{tag}.uv_corner"], rmeta["uv_origin"])
    src = HDSource(a.target)
    tex = src.meta.get("textures", {})
    alb_path = a.albedo or tex.get("r9_head_albedo")
    if not alb_path:
        raise SystemExit("no albedo: pass --albedo or export the target with --textures")
    alb = load_image(alb_path, linear=True)
    nrm_map, Tw = None, None
    nrm_path = a.normal if a.normal is not None else tex.get("r9_head_normal")
    if nrm_path and a.normal != "" and f"{tag}.tangents" in R:
        nrm_map = load_image(nrm_path, linear=False)
        T_nat = R[f"{tag}.tangents"].astype(np.float64)
        M = np.asarray(rmeta["M_native_to_work"][tag], float)[:3, :3]
        Rn = M / np.cbrt(abs(np.linalg.det(M)))
        Rs = np.asarray(rmeta["similarity"]["R"], float)
        Tx = T_nat[:, :3] @ Rn.T @ Rs.T
        Tw = np.concatenate([Tx, T_nat[:, 3:] * np.sign(np.linalg.det(Rn))], 1)
    sizes = [int(s) for s in a.sizes.split(",")]
    big = max(sizes)
    log(f"baking {big}px from {alb_path}" + (f" + normal {nrm_path}" if nrm_map is not None else ""))
    b = bake(src, Vw, Nw, F, uv_bl, big, a.ray_mm / 1000, alb, nrm_map, Tw)
    log("bake stats", json.dumps(b["stats"]))
    rep = {"inputs": {"wrap": os.path.abspath(a.wrap), "target": os.path.abspath(a.target), "albedo": alb_path,
                      "normal": nrm_path if nrm_map is not None else None, "lod": tag},
           "bake": b["stats"], "sizes": {}}
    maps_for_check = {}
    for size in sorted(sizes, reverse=True):
        f = big // size
        if size == big:
            lin, cov = b["albedo"], b["cov"]
            nrm, ncov = b["normal"], b["cov"]
        elif big % size == 0:
            lin, cov = box_down(b["albedo"], b["cov"], f)
            nrm = None
            if b["normal"] is not None:
                nl, _ = box_down(b["normal"] * 2 - 1, b["cov"], f)
                nl /= np.maximum(np.linalg.norm(nl, axis=2, keepdims=True), 1e-30)
                nrm = nl * 0.5 + 0.5
            ncov = cov
        else:
            raise SystemExit(f"size {size} must divide {big}")
        dil = max(2, int(round(a.dilate * size / 2048)))
        lin_d = dilate(lin, cov, dil)
        srgb = lin_to_srgb(lin_d)
        p = os.path.join(a.out_dir, f"{a.prefix}_albedo_{size}.png")
        save_png(p, srgb)
        dec, ainfo = astc_roundtrip(srgb, os.path.join(a.out_dir, f"{a.prefix}_albedo_{size}_astc{a.block}_decoded.png"),
                                    a.block, srgb=True)
        entry = {"albedo_png": p, "dilate_px": dil, "covered": float(cov.mean()), "astc": ainfo}
        maps_for_check[f"albedo_{size}"] = srgb_to_lin(srgb)
        maps_for_check[f"albedo_{size}_astc"] = srgb_to_lin(dec)
        if nrm is not None:
            nd = dilate(nrm, ncov, dil)
            pn = os.path.join(a.out_dir, f"{a.prefix}_normal_{size}.png")
            save_png(pn, nd)
            _, ninfo = astc_roundtrip(nd, os.path.join(a.out_dir, f"{a.prefix}_normal_{size}_astc{a.block}_decoded.png"),
                                      a.block, srgb=False, normal=False)
            dev = np.degrees(np.arccos(np.clip(((nd * 2 - 1)[cov] / np.linalg.norm((nd * 2 - 1)[cov], axis=1, keepdims=True))
                                               @ np.array([0, 0, 1.0]), -1, 1)))
            entry.update({"normal_png": pn, "normal_astc": ninfo,
                          "normal_tilt_from_flat_deg": {"mean": float(dev.mean()), "p95": float(np.percentile(dev, 95))}})
        rep["sizes"][str(size)] = entry
        log(f"{size}px: astc {a.block} psnr {ainfo['psnr_db_vs_uncompressed']:.2f} dB -> {p}")
    if a.check:
        rep["consistency_check"] = consistency_check(src, Vw, F, uv_bl, maps_for_check, alb)
        for k, v in rep["consistency_check"].items():
            log(f"check {k}: PSNR {v['psnr_db']:.2f} dB, dE00 mean {v['dE00_mean']:.2f} p95 {v['dE00_p95']:.2f}")
    rep["seconds"] = round(time.time() - t0, 1)
    rp = os.path.join(a.out_dir, f"{a.prefix}_texture_report.json")
    json.dump(rep, open(rp, "w"), indent=1, default=float)
    log("wrote", rp)
    return rep


def build_argparser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wrap", required=True, help="wrap.py output NPZ")
    ap.add_argument("--target", required=True, help="export_head.py NPZ (HD head + UVs)")
    ap.add_argument("--albedo")
    ap.add_argument("--normal", help="HD tangent-space normal map; '' disables (default: target meta)")
    ap.add_argument("--lod", default="lod0")
    ap.add_argument("--sizes", default="2048,256")
    ap.add_argument("--block", default="4x4")
    ap.add_argument("--ray-mm", type=float, default=3.0)
    ap.add_argument("--dilate", type=int, default=16, help="edge-extension passes at 2048 px (scaled with size); push-pull fills the rest")
    ap.add_argument("--no-check", dest="check", action="store_false")
    ap.add_argument("--prefix", default="donor")
    ap.add_argument("--out-dir", required=True)
    return ap


if __name__ == "__main__":
    run(build_argparser().parse_args())
