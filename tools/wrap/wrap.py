#!/usr/bin/env python3
"""Non-rigid wrap of a DONOR game head (its topology / vertex order / UVs / skin weights are kept)
onto our TARGET HD head (tools/wrap/export_head.py output).

  python3 tools/wrap/wrap.py --target head.npz --donor donor.{npz,glb,obj} --out-dir OUT \
      [--donor-frame ue] [--donor-uv-origin top-left] [--donor-lod1 lod1.{npz,glb,obj}|auto] \
      [--features donor_features.json | --guess-features] [--locked locked.json] [--lock-rings 1] \
      [--eye-mode follow|anchor] [--align similarity|rigid] [--tangents blender|numpy|none]

Pipeline (README.md §4 has the maths and every default):
  0. load + weld: GPU vertex buffers duplicate vertices along UV/normal seams; all solving happens
     on the welded ("unique") mesh, results are scattered back so duplicates stay bit-identical.
  1. similarity (or rigid) alignment from >= 8 named feature correspondences (Umeyama).
  2. donor regions: neck border loop + N rings = LOCKED (hard constraint, identity transform);
     no-data = donor cavities (normal ray hits the donor itself: mouth bag, eye sockets,
     nostrils), other border loops (eye/mouth holes) + 1 ring, and everything the aligned donor
     inherits from the target's eye-margin / inner-mouth labels.
  3. optimal-step non-rigid ICP (Amberg, Romdhani, Vetter, CVPR 2007): per-vertex affine
     transforms, stiffness ||(M x G) X||^2 decreasing over a schedule, landmark term decreasing
     with it, closest-point data term with rejection of (a) target-border hits, (b) normal
     incompatibility (> --max-normal-deg), (c) outliers (> --max-dist-mm), plus a symmetric
     target->donor term (target samples pull their closest donor surface point) so protruding
     features (nose tip, lips, ears) are reached.
  4. LOD1: each LOD1 vertex is embedded in the REST LOD0 surface (closest point + signed normal
     offset) and re-evaluated on the wrapped LOD0; LOD1's own neck ring is locked too.
  5. normals (angle-weighted, welded; split normals preserved where the donor had them), MikkTSpace
     tangents (Blender's mesh.calc_tangents on the native buffers - tools/wrap/blender_tangents.py;
     numpy Lengyel fallback), bounds, and a report JSON with every check listed in README §4.6.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import numpy as np
from scipy import sparse
from scipy.sparse.linalg import splu

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import features as FT  # noqa: E402
import geom as G  # noqa: E402
import glb as GLB  # noqa: E402
import meshdata as MD  # noqa: E402


def log(*a):
    print("[wrap]", *a, flush=True)


# ============================================================================= target
class Target:
    def __init__(self, path, part="head"):
        A, meta = MD.load_npz(path)
        self.meta = meta
        fd = meta["frame"]
        self.path = path
        self.P = MD.frame_to_blender(A[f"{part}.positions"], fd)
        F = A[f"{part}.faces"].astype(np.int64)
        self.N = MD.dir_frame_to_blender(A[f"{part}.normals"], fd)
        uv = {k.split(".uv.")[1]: v for k, v in A.items() if k.startswith(f"{part}.uv.")}
        if G.orientation_sign(self.P, F) < 0:          # (left-handed export frames reverse the winding)
            F = F[:, ::-1].copy()
            uv = {k: v[:, ::-1].copy() for k, v in uv.items()}
        self.F, self.uv = F, uv
        self.region = A.get(f"{part}.region", np.zeros(len(self.P), np.int8)).astype(int)
        self.L = MD.frame_to_blender(A["landmarks.mp478"], fd) if "landmarks.mp478" in A else None
        names = [str(x) for x in A.get("features.names", [])]
        pos = MD.frame_to_blender(A["features.positions"], fd) if names else np.zeros((0, 3))
        self.feats = dict(zip(names, pos))
        self.q = G.TriMeshQuery(self.P, self.F)
        self.textures = meta.get("textures", {})

    def point(self, name):
        if name.startswith("mp:"):
            p = self.L[int(name[3:])]
            if not np.all(np.isfinite(p)):
                raise KeyError(f"landmark {name} not on the target head")
            return p
        return self.feats[name]

    def has(self, name):
        try:
            self.point(name)
            return True
        except (KeyError, TypeError, IndexError):
            return False


# ============================================================================= donor io
class LOD:
    """One donor LOD: native buffers kept verbatim + welded working copy in the work frame."""

    def __init__(self, P, F, M_nw, uv=None, uv_corner=None, uv_origin="bottom-left", normals=None,
                 tangents=None, joints=None, weights=None, source=None):
        self.P_native = np.asarray(P, np.float64)
        self.F = np.asarray(F, np.int64)
        self.M_nw = M_nw                      # 4x4: native -> work (blender frame, metres)
        self.uv, self.uv_corner, self.uv_origin = uv, uv_corner, uv_origin
        self.normals, self.tangents = normals, tangents
        self.joints, self.weights = joints, weights
        self.source = source or {}
        self.wid, self.rep = G.weld(self.P_native)
        self.n = len(self.rep)
        self.Pw = apply_affine(M_nw, self.P_native[self.rep])          # unique, work frame, rest
        Fu = self.wid[self.F]
        ok = (Fu[:, 0] != Fu[:, 1]) & (Fu[:, 1] != Fu[:, 2]) & (Fu[:, 0] != Fu[:, 2])
        Fu = Fu[ok]
        self.degenerate = int((~ok).sum())
        self.flip = G.orientation_sign(self.Pw, Fu) < 0
        self.Fu = Fu[:, ::-1].copy() if self.flip else Fu
        self.adj, self.edges = G.vertex_adjacency(self.Fu, self.n)
        ue, cnt = G.edges_with_count(self.Fu)
        self.nonmanifold_edges = int((cnt > 2).sum())
        self.loops = G.boundary_loops(self.Fu, self.n)

    def corner_uv_bl(self):
        """(F,3,2) per-corner UV, bottom-left origin (for texture work)."""
        if self.uv_corner is not None:
            return MD.uv_to_bottom_left(self.uv_corner, self.uv_origin)
        if self.uv is None:
            return None
        return MD.uv_to_bottom_left(self.uv[self.F], self.uv_origin)

    def neck_loop(self):
        if not self.loops:
            return None
        return max(self.loops, key=lambda lp: G.loop_length(self.Pw, lp))


def apply_affine(M, P):
    return np.asarray(P, float) @ M[:3, :3].T + M[:3, 3]


def frame_affine(fd):
    """4x4 native(frame) -> work(blender metres)."""
    A, s = np.asarray(fd["A"], float), float(fd["scale"])
    M = np.eye(4)
    M[:3, :3] = A.T / s
    return M


def dir_matrix(M):
    """Rotation(+reflection) part of a similarity/affine, normalised (for normals/tangents)."""
    R = M[:3, :3]
    s = np.cbrt(abs(np.linalg.det(R)))
    return R / s


def load_npz_donor(path, frame=None, uv_origin=None, prefix=""):
    A, meta = MD.load_npz(path)
    fd = MD.frame_def(frame) if frame else meta.get("frame") or MD.frame_def("blender")
    uo = uv_origin or meta.get("uv_origin", "bottom-left")
    g = lambda k: A.get(prefix + k)  # noqa: E731
    if g("positions") is None:
        return None, meta
    lod = LOD(g("positions"), g("faces"), frame_affine(fd), uv=g("uv0"), uv_origin=uo, normals=g("normals"),
              tangents=g("tangents"), joints=g("joints"), weights=g("weights"),
              source={"kind": "npz", "path": path, "prefix": prefix, "frame": fd})
    return lod, meta


def load_obj_donor(path, frame="gltf"):
    v, vt, faces, fuv = [], [], [], []
    for line in open(path):
        if line.startswith("v "):
            v.append([float(x) for x in line.split()[1:4]])
        elif line.startswith("vt "):
            vt.append([float(x) for x in line.split()[1:3]])
        elif line.startswith("f "):
            toks = [t.split("/") for t in line.split()[1:]]
            vi = [int(t[0]) - 1 for t in toks]
            ti = [int(t[1]) - 1 if len(t) > 1 and t[1] else -1 for t in toks]
            for k in range(1, len(vi) - 1):
                faces.append([vi[0], vi[k], vi[k + 1]])
                fuv.append([ti[0], ti[k], ti[k + 1]])
    fd = MD.frame_def(frame)
    F = np.array(faces)
    fuv = np.array(fuv)
    uvc = np.array(vt)[fuv] if len(vt) and (fuv >= 0).all() else None
    return LOD(np.array(v), F, frame_affine(fd), uv_corner=uvc, uv_origin="bottom-left",
               source={"kind": "obj", "path": path, "frame": fd})


def load_glb_donor(path, mesh_name=None, pick="largest"):
    g = GLB.read_glb(path)
    meshes = g["meshes"]
    if mesh_name:
        cand = [m for m in meshes if m["name"] == mesh_name]
        if not cand:
            raise SystemExit(f"{path}: no mesh named {mesh_name!r}; meshes: {[m['name'] for m in meshes]}")
        m = cand[0]
    else:
        nv = [sum(len(p["attributes"]["POSITION"]) for p in mm["primitives"]) for mm in meshes]
        m = meshes[int(np.argmax(nv) if pick == "largest" else np.argsort(nv)[-2])]
    P, F, uv, N, T, Jn, Wt, spans = [], [], [], [], [], [], [], []
    off = 0
    for pi, p in enumerate(m["primitives"]):
        a = p["attributes"]
        n = len(a["POSITION"])
        P.append(a["POSITION"])
        F.append(p["faces"] + off)
        uv.append(a.get("TEXCOORD_0", np.zeros((n, 2))))
        N.append(a.get("NORMAL"))
        T.append(a.get("TANGENT"))
        Jn.append(a.get("JOINTS_0"))
        Wt.append(a.get("WEIGHTS_0"))
        spans.append((pi, off, n))
        off += n
    cat = lambda xs: None if any(x is None for x in xs) else np.concatenate(xs)  # noqa: E731
    # native (mesh-local) -> glTF world
    if m["skin"] is not None:
        sk = g["skins"][m["skin"]]
        Ms = np.einsum("jab,jbc->jac", sk["joint_world"], sk["ibm"])
        if np.abs(Ms - Ms[0]).max() > 1e-4:
            raise SystemExit("skinned donor GLB is not in its bind pose (joint_world @ inverseBind differs "
                             "between joints); export it in bind pose")
        Mw = Ms[0]
    else:
        Mw = m["world"]
    M_nw = frame_affine(MD.frame_def("gltf")) @ Mw
    return LOD(np.concatenate(P), np.concatenate(F), M_nw, uv=np.concatenate(uv), uv_origin="top-left",
               normals=cat(N), tangents=cat(T), joints=cat(Jn), weights=cat(Wt),
               source={"kind": "glb", "path": path, "mesh": m["index"], "mesh_name": m["name"], "spans": spans,
                       "M_world": Mw.tolist()})


def load_donor(path, frame=None, uv_origin=None, mesh=None, lod1=None, lod1_mesh=None):
    """Returns (lod0, lod1 or None, meta)."""
    ext = os.path.splitext(path)[1].lower()
    meta = {}
    if ext == ".npz":
        l0, meta = load_npz_donor(path, frame, uv_origin)
        l1 = None
        if lod1 in (None, "auto"):
            l1, _ = load_npz_donor(path, frame, uv_origin, prefix="lod1.")
    elif ext == ".glb":
        l0 = load_glb_donor(path, mesh)
        l1 = None
        if lod1 == "auto":
            g = GLB.read_glb(path)
            if len(g["meshes"]) > 1:
                l1 = load_glb_donor(path, lod1_mesh, pick="second")
    elif ext == ".obj":
        l0 = load_obj_donor(path, frame or "gltf")
        l1 = None
    else:
        raise SystemExit(f"unsupported donor format {ext}")
    if lod1 not in (None, "auto"):
        e1 = os.path.splitext(lod1)[1].lower()
        l1 = (load_npz_donor(lod1, frame, uv_origin)[0] if e1 == ".npz" else
              load_glb_donor(lod1, lod1_mesh) if e1 == ".glb" else load_obj_donor(lod1, frame or "gltf"))
    if uv_origin and l0.uv_origin != uv_origin:
        l0.uv_origin = uv_origin
        if l1 is not None:
            l1.uv_origin = uv_origin
    return l0, l1, meta


def load_feature_json(path):
    d = json.load(open(path))
    return {k: int(v) for k, v in (d.get("features", d)).items()}


# ============================================================================= alignment
def similarity_from_features(lod, target, feats_native, mode="similarity", min_pairs=8):
    names = [k for k in feats_native if target.has(k)]
    missing = [k for k in feats_native if not target.has(k)]
    if len(names) < min_pairs:
        raise SystemExit(f"need >= {min_pairs} feature correspondences known on both meshes, got {len(names)} "
                         f"(unknown on target: {missing})")
    src = lod.Pw[lod.wid[[feats_native[k] for k in names]]]
    dst = np.array([target.point(k) for k in names])
    w = np.array([1.0 if k in FT.BASIC else 0.5 for k in names])
    s, R, t = G.umeyama(src, dst, w, scale=(mode == "similarity"))
    res = np.linalg.norm(s * src @ R.T + t - dst, axis=1)
    return (s, R, t), {"pairs": len(names), "scale": float(s), "rotation_deg": float(np.degrees(np.arccos(
        np.clip((np.trace(R) - 1) / 2, -1, 1)))), "residual_mm": dict(zip(names, (res * 1000).round(2).tolist())),
        "rms_mm": float(np.sqrt((res ** 2).mean()) * 1000), "ignored_unknown": missing}


def sim_apply(sim, P):
    s, R, t = sim
    return s * np.asarray(P) @ R.T + t


def sim_inverse_apply(sim, P):
    s, R, t = sim
    return (np.asarray(P) - t) @ R / s


# ============================================================================= donor regions
def donor_regions(lod, Pa, target, lock_rings=1, locked_native=None, cavity_ray=0.03):
    """Returns dict of unique-vertex masks + labels (target label of the nearest target vertex)."""
    n = lod.n
    neck = lod.neck_loop()
    locked = np.zeros(n, bool)
    if locked_native is not None:
        locked[lod.wid[np.asarray(locked_native, np.int64)]] = True
    elif neck is not None:
        locked[neck] = True
        locked = G.grow(locked, lod.adj, lock_rings)
    holes = np.zeros(n, bool)
    for lp in lod.loops:
        if neck is not None and lp is neck:
            continue
        holes[lp] = True
    holes = G.grow(holes, lod.adj, 1)
    Nn = MD.vertex_normals(Pa, lod.Fu)
    cav = G.rays_hit_any(Pa + Nn * 2e-4, Nn, Pa, lod.Fu, 0.0, cavity_ray)
    cp = target.q.closest(Pa)
    tv = target.F[cp["tri"], np.argmax(cp["bary"], 1)]
    label = target.region[tv]
    nodata_lab = np.isin(label, [FT.REGION_ID[r] for r in FT.NO_DATA_REGIONS])
    nodata = (cav | holes | nodata_lab) & ~locked
    return {"locked": locked, "cavity": cav, "holes": holes, "label": label, "nodata": nodata, "neck_loop": neck}


# ============================================================================= NICP
def _block_diag_rows(Vh, w):
    """D^T diag(w) D for D = per-vertex [x y z 1] rows -> (4n x 4n) block diagonal."""
    n = len(Vh)
    blocks = w[:, None, None] * Vh[:, :, None] * Vh[:, None, :]
    r = (np.arange(n)[:, None, None] * 4 + np.arange(4)[None, :, None]).repeat(4, 2)
    c = (np.arange(n)[:, None, None] * 4 + np.arange(4)[None, None, :]).repeat(4, 1)
    return sparse.coo_matrix((blocks.ravel(), (r.ravel(), c.ravel())), shape=(4 * n, 4 * n)).tocsr()


def _bary_rows(tri_verts, bary, Vh, n):
    """Sparse (k x 4n): row j = sum_c bary[j,c] * [x y z 1](v_c) placed at block v_c."""
    k, c = tri_verts.shape
    rows = np.repeat(np.arange(k), 4 * c)
    cols = (tri_verts[:, :, None] * 4 + np.arange(4)[None, None, :]).reshape(k, 4 * c).ravel()
    vals = (bary[:, :, None] * Vh[tri_verts]).reshape(k, 4 * c).ravel()
    return sparse.coo_matrix((vals, (rows, cols)), shape=(k, 4 * n)).tocsr()


def nicp(V0, F, edges, tq_target, tgt_normals, opts, locked=None, data_mask=None, landmarks=None,
         tgt_samples=None, anchor_mask=None, verbose=True):
    """Optimal-step NICP. V0 (n,3) rest positions (aligned, metres), F oriented triangles.
    tq_target: geom.TriMeshQuery of the target (metres); tgt_normals: target vertex normals.
    landmarks: (ids (k,), positions (k,3)); tgt_samples: dict(P, N, w) target points for the
    symmetric target->donor term. Flip guard: if a solve turns triangles over (vs. the rest
    pose), the edges around them are stiffened x4 and their data terms dropped, then the step is
    re-solved (<= opts['flip_retries'] times). Returns (V, history)."""
    S = 10.0                                  # work in decimetres (better conditioned)
    c0 = V0.mean(0)
    n = len(V0)
    Vs = (V0 - c0) * S
    Vh = np.concatenate([Vs, np.ones((n, 1))], 1)
    locked = np.zeros(n, bool) if locked is None else locked
    data_mask = np.ones(n, bool) if data_mask is None else data_mask
    gamma = opts.get("gamma", 1.0)
    g = np.array([1.0, 1.0, 1.0, gamma])
    m = len(edges)
    rows = np.arange(4 * m)
    MG = sparse.coo_matrix((np.concatenate([np.tile(g, m), -np.tile(g, m)]),
                            (np.concatenate([rows, rows]),
                             np.concatenate([(edges[:, 0:1] * 4 + np.arange(4)).ravel(),
                                             (edges[:, 1:2] * 4 + np.arange(4)).ravel()]))),
                           shape=(4 * m, 4 * n)).tocsr()
    ew = np.ones(m)                                       # per-edge stiffness multipliers (flip guard)

    def stiffness():
        return (MG.T @ sparse.diags(np.repeat(ew ** 2, 4)) @ MG).tocsr()

    K = stiffness()
    X = np.zeros((4 * n, 3))
    X[0::4, 0] = X[1::4, 1] = X[2::4, 2] = 1.0           # identity transforms
    fixed_cols = (np.nonzero(locked)[0][:, None] * 4 + np.arange(4)).ravel()
    free = np.ones(4 * n, bool)
    free[fixed_cols] = False
    fcols = np.nonzero(free)[0]
    cos_max = np.cos(np.radians(opts.get("max_normal_deg", 60.0)))
    dmax = opts.get("max_dist_mm", 25.0) / 1000 * S
    tq_s = G.TriMeshQuery((tq_target.V - c0) * S, tq_target.F)
    TN = tgt_normals
    if tgt_samples is not None:
        TS = (tgt_samples["P"] - c0) * S
        TSN, TSW = tgt_samples["N"], tgt_samples["w"]
    n_rest = MD.tri_normals(Vs, F)
    guard = np.zeros(n, bool)                             # vertices whose data term was dropped
    hist = []
    sched = opts.get("schedule", [50, 20, 10, 5, 2, 1])
    lm_w0 = opts.get("landmark_weight", 3.0)
    t2d_w = opts.get("t2d_weight", 0.5)
    for alpha in sched:
        if opts.get("guard_release", True):
            guard[:] = False                              # data terms come back at every new stiffness
        beta = lm_w0 * (alpha / sched[0]) ** opts.get("landmark_decay", 0.5) if landmarks is not None else 0.0
        for it in range(opts.get("max_inner", 6)):
            V = (Vh[:, None, :] @ X.reshape(n, 4, 3))[:, 0, :]
            Nd = MD.vertex_normals(V, F)
            cp = tq_s.closest(V)
            tn = tq_s.interp(cp["tri"], cp["bary"], TN)
            tn /= np.maximum(np.linalg.norm(tn, axis=1, keepdims=True), 1e-30)
            ok0 = data_mask & ~locked & ~cp["border"] & ((Nd * tn).sum(1) > cos_max) & (cp["dist"] < dmax)
            Nadd = None
            radd = np.zeros((4 * n, 3))
            n_t2d = 0
            if tgt_samples is not None and t2d_w > 0:
                dq = G.TriMeshQuery(V, F)
                cs = dq.closest(TS)
                dn = dq.interp(cs["tri"], cs["bary"], Nd)
                dn /= np.maximum(np.linalg.norm(dn, axis=1, keepdims=True), 1e-30)
                tv = F[cs["tri"]]
                ok2 = (~cs["border"] & ((dn * TSN).sum(1) > cos_max) & (cs["dist"] < dmax)
                       & data_mask[tv].all(1))
                if ok2.any():
                    B = _bary_rows(tv[ok2], cs["bary"][ok2], Vh, n)
                    ww = t2d_w * TSW[ok2]
                    Nadd = B.T @ sparse.diags(ww) @ B
                    radd += B.T @ (TS[ok2] * ww[:, None])
                    n_t2d = int(ok2.sum())
            if landmarks is not None and beta > 0:
                ids, lp = landmarks
                keep = ~locked[ids]
                if keep.any():
                    Bl = _bary_rows(ids[keep][:, None], np.ones((keep.sum(), 1)), Vh, n)
                    Nl = beta ** 2 * (Bl.T @ Bl)
                    Nadd = Nl if Nadd is None else Nadd + Nl
                    radd += beta ** 2 * (Bl.T @ ((lp[keep] - c0) * S))
            if anchor_mask is not None and anchor_mask.any():
                wa = opts.get("anchor_weight", 1.0) * anchor_mask
                Na = _block_diag_rows(Vh, wa)
                Nadd = Na if Nadd is None else Nadd + Na
                radd += ((Vh * wa[:, None])[:, :, None] * Vs[:, None, :]).reshape(4 * n, 3)
            for retry in range(opts.get("flip_retries", 3) + 1):
                ok = ok0 & ~guard
                w = ok.astype(float)
                Nmat = alpha ** 2 * K + _block_diag_rows(Vh, w)
                if Nadd is not None:
                    Nmat = Nmat + Nadd
                rhs = ((Vh * w[:, None])[:, :, None] * cp["point"][:, None, :]).reshape(4 * n, 3) + radd
                Nmat = Nmat.tocsr()
                A_ff = Nmat[fcols][:, fcols].tocsc()
                b = rhs[fcols] - Nmat[fcols][:, fixed_cols] @ X[fixed_cols] if len(fixed_cols) else rhs[fcols]
                Xn = X.copy()
                Xn[fcols] = splu(A_ff, permc_spec="COLAMD").solve(b)
                Vn = (Vh[:, None, :] @ Xn.reshape(n, 4, 3))[:, 0, :]
                flipped = np.nonzero((MD.tri_normals(Vn, F) * n_rest).sum(1) < opts.get("flip_cos", 0.0))[0]
                if not len(flipped) or retry == opts.get("flip_retries", 3):
                    break
                bad = np.zeros(n, bool)
                bad[F[flipped].ravel()] = True
                bad = bad | (np.bincount(edges[bad[edges[:, 0]] | bad[edges[:, 1]]].ravel(), minlength=n) > 0)
                ew[bad[edges[:, 0]] | bad[edges[:, 1]]] *= 4.0
                guard |= bad
                K = stiffness()
            delta = float(np.abs(Vn - V).max() / S * 1000)
            X = Xn
            hist.append({"alpha": alpha, "iter": it, "valid": int(ok.sum()), "t2d": n_t2d,
                         "mean_dist_mm": float(cp["dist"][ok].mean() / S * 1000) if ok.any() else None,
                         "delta_mm": delta, "flipped": int(len(flipped)), "guarded": int(guard.sum())})
            if verbose:
                log(f"  alpha={alpha:<5} it={it} valid={ok.sum()}/{n} t2d={n_t2d} "
                    f"mean_d={hist[-1]['mean_dist_mm'] or 0:.3f}mm step={delta:.3f}mm flips={len(flipped)} "
                    f"guarded={guard.sum()}")
            if delta < opts.get("tol_mm", 0.02):
                break
    V = (Vh[:, None, :] @ X.reshape(n, 4, 3))[:, 0, :]
    return V / S + c0, hist, guard


def refine_projection(V, F, edges, rest, tq_target, tgt_normals, locked, data_mask, opts, verbose=True):
    """Final snap without tangential sliding: every data vertex is projected onto the target along
    its own normal (two-sided ray, closest point if the ray misses within --refine-max-mm); the
    projection displacements are then smoothed with a membrane (graph Laplacian) energy
    sum_valid |d_i - d_i*|^2 + lam * sum_edges |d_i - d_j|^2, locked vertices fixed at d = 0, so
    no-data vertices follow their neighbours. Steps that would turn a triangle over are undone
    vertex-wise. Repeated opts['refine_iters'] times."""
    n = len(V)
    L = sparse.coo_matrix((np.ones(len(edges)), (edges[:, 0], edges[:, 1])), shape=(n, n))
    L = L + L.T
    Lap = (sparse.diags(np.asarray(L.sum(1)).ravel()) - L).tocsr()
    n_rest = MD.tri_normals(rest, F)
    lam = opts.get("refine_smooth", 0.05)
    rmax = opts.get("refine_max_mm", 3.0) / 1000
    cos_max = np.cos(np.radians(opts.get("max_normal_deg", 60.0)))
    info = []
    free = ~locked
    for it in range(opts.get("refine_iters", 2)):
        Nd = MD.vertex_normals(V, F)
        hit, t, tri, bary = tq_target.ray_nearest_hit(V, Nd, rmax, k=24)
        cp = tq_target.closest(V)
        hp = (tq_target.V[tq_target.F[np.maximum(tri, 0)]] * bary[:, :, None]).sum(1)
        tgt = np.where(hit[:, None], hp, cp["point"])
        tri_used = np.where(hit, tri, cp["tri"])
        bry = np.where(hit[:, None], bary, cp["bary"])
        tn = tq_target.interp(tri_used, bry, tgt_normals)
        tn /= np.maximum(np.linalg.norm(tn, axis=1, keepdims=True), 1e-30)
        border = tq_target.on_border(tri_used, bry)
        dstar = tgt - V
        ok = data_mask & free & ~border & ((Nd * tn).sum(1) > cos_max) & (np.linalg.norm(dstar, axis=1) < rmax)
        w = ok.astype(float)
        A = (sparse.diags(w) + lam * Lap).tocsr()
        fi = np.nonzero(free)[0]
        Aff = A[fi][:, fi].tocsc()
        d = np.zeros((n, 3))
        d[fi] = splu(Aff, permc_spec="COLAMD").solve(w[fi, None] * dstar[fi])
        Vn = V + d
        flipped = (MD.tri_normals(Vn, F) * n_rest).sum(1) < 0
        undone = 0
        for _ in range(5):
            if not flipped.any():
                break
            bad = np.unique(F[flipped].ravel())
            Vn[bad] = V[bad]
            undone += len(bad)
            flipped = (MD.tri_normals(Vn, F) * n_rest).sum(1) < 0
        info.append({"iter": it, "valid": int(ok.sum()), "ray_hits": int((hit & ok).sum()),
                     "mean_dstar_mm": float(np.linalg.norm(dstar[ok], axis=1).mean() * 1000) if ok.any() else None,
                     "undone_vertices": int(undone)})
        if verbose:
            log("  refine", info[-1])
        V = Vn
    return V, info


# ============================================================================= feature guess (testing helper)
def guess_features(lod, target, names=None, opts=None):
    """Interactive-free donor feature ids from geometry (for tests and first drafts; production
    feature ids should be checked by a person, e.g. with tools/wrap/README.md §3.2):
      1. coarse pose from the donor's own extremes (crown = highest point, nose tip = most forward
         point of the upper head, lateral extremes at ear height), assuming the donor frame is upright
         and faces like the target (true for the frame presets);
      2. trimmed similarity ICP on the cranium + upper face (the neck and jaw differ most between
         heads and would tilt the fit);
      3. the TARGET is fitted non-rigidly onto the donor as a template (same NICP, no landmarks) and
         every named target feature is carried along and snapped to the nearest donor vertex;
      4. geometric re-definition on the donor itself where a feature is an extremum or a crease:
         crown (highest midline point), ear tops (highest point of the donor vertices covered by the
         template's ears), nose tip (most forward point near the carried one), mouth corners and
         inner-lip midpoints (lateral / vertical extremes of the donor's own mouth-slit cavity
         vertices), chin (midline vertex whose normal makes the same angle with the head axes as the
         target's normal at MediaPipe 152)."""
    names = names or (FT.BASIC + list(FT.GUESS_EXTRA))
    names = [k for k in names if target.has(k)]
    D = lod.Pw

    def extremes(P, up=np.array([0, 0, 1.0]), fwd=np.array([0, -1.0, 0])):
        h = P @ up
        top = int(np.argmax(h))
        upper = h > h.min() + 0.35 * np.ptp(h)
        nose = int(np.argmax(np.where(upper, P @ fwd, -np.inf)))
        lat = np.cross(fwd, up)
        band = np.abs(h - (h[top] + h[nose]) / 2) < 0.08 * np.ptp(h)
        x = P @ lat
        lo = int(np.argmin(np.where(band, x, np.inf)))
        hi = int(np.argmax(np.where(band, x, -np.inf)))
        return [top, nose, lo, hi]

    lat, up, fwd = FT.head_axes(target.L)
    di = extremes(D)
    ti = extremes(target.P)
    sim = G.umeyama(D[di], target.P[ti])
    h_cut = (target.point("nose_tip") @ up) - 0.015
    for _ in range(20):                       # trimmed similarity ICP donor -> target (upper head)
        Pa = sim_apply(sim, D)
        cp = target.q.closest(Pa)
        keep = ~cp["border"] & (Pa @ up > h_cut)
        thr = np.percentile(cp["dist"][keep], 80)
        keep &= cp["dist"] <= thr
        sim = G.umeyama(D[keep], cp["point"][keep])
    Pa = sim_apply(sim, D)
    dq = G.TriMeshQuery(Pa, lod.Fu)
    dn = MD.vertex_normals(Pa, lod.Fu)
    _, tedges = G.vertex_adjacency(target.F, len(target.P))
    tb = G.tri_edge_boundary(target.F, len(target.P))[1]
    o = {"schedule": [20, 10, 5, 2, 1], "max_inner": 3, "t2d_weight": 0.5, "max_normal_deg": 60.0}
    o.update(opts or {})
    tdata = ~tb & ~np.isin(target.region, [FT.REGION_ID[r] for r in FT.NO_DATA_REGIONS])
    Tw, _, _ = nicp(target.P, target.F, tedges, dq, dn, o, data_mask=tdata, verbose=False,
                    tgt_samples={"P": Pa, "N": dn, "w": np.ones(len(Pa))})

    def carry(p):
        c = target.q.closest(np.asarray(p)[None])
        return (Tw[target.F[c["tri"][0]]] * c["bary"][0][:, None]).sum(0)

    def nearest(pt):
        return int(np.argmin(np.linalg.norm(Pa - pt, axis=1)))

    out_u, how = {}, {}
    for k in names:
        out_u[k] = nearest(carry(target.point(k)))
        how[k] = "template"
    # ---- geometric re-definitions on the donor
    eye_w = np.linalg.norm(target.point("eye_outer_L") - target.point("eye_outer_R"))
    mid = float(np.mean([(carry(target.L[a]) + carry(target.L[b])) / 2 @ lat for a, b in FT.SYM_PAIRS]))
    off = Pa @ lat - mid
    nt = carry(target.point("nose_tip"))
    m = np.linalg.norm(Pa - nt, axis=1) < 0.015
    out_u["nose_tip"] = int(np.argmax(np.where(m, Pa @ fwd, -np.inf)))
    how["nose_tip"] = "most forward vertex near the carried nose tip"
    cav = G.rays_hit_any(Pa + dn * 2e-4, dn, Pa, lod.Fu, 0.0, 0.03)
    mc = (carry(target.point("mouth_corner_R")) + carry(target.point("mouth_corner_L"))) / 2
    slit = cav & (np.linalg.norm(Pa - mc, axis=1) < 0.03) & ((Pa - mc) @ fwd > -0.012)
    if slit.sum() >= 4:
        x = np.where(slit, (Pa - mc) @ lat, np.nan)
        out_u["mouth_corner_R"] = int(np.nanargmin(x))
        out_u["mouth_corner_L"] = int(np.nanargmax(x))
        how["mouth_corner_R"] = how["mouth_corner_L"] = "lateral extremes of the donor mouth-slit cavity"
        front = slit & (np.abs(off) < 0.004)
        if front.sum() >= 2:
            fr = np.where(front, Pa @ fwd, -np.inf)
            top = front & (fr >= np.sort(fr[front])[max(0, front.sum() - 6)])
            if "mp:13" in out_u:
                out_u["mp:13"] = int(np.argmax(np.where(top, Pa @ up, -np.inf)))
                how["mp:13"] = "upper inner-lip midpoint of the mouth slit"
            if "mp:14" in out_u:
                out_u["mp:14"] = int(np.argmin(np.where(top, Pa @ up, np.inf)))
                how["mp:14"] = "lower inner-lip midpoint of the mouth slit"
    # chin: same normal angle (in the sagittal plane) as the target's normal at MediaPipe 152
    ct = target.q.closest(target.point("chin")[None])
    tn = target.q.interp(ct["tri"], ct["bary"], target.N)[0]
    a_t = np.arctan2(-(tn @ up), tn @ fwd)
    cc = carry(target.point("chin"))
    a_d = np.arctan2(-(dn @ up), dn @ fwd)
    cand = (np.abs(off) < 0.006) & (np.linalg.norm(Pa - cc, axis=1) < 0.025) & (dn @ fwd > -0.2)
    if cand.sum():
        score = np.abs(a_d - a_t) + np.linalg.norm(Pa - cc, axis=1) / 0.02
        out_u["chin"] = int(np.argmin(np.where(cand, score, np.inf)))
        how["chin"] = "midline vertex with the target's chin-normal angle"
    out = {k: int(lod.rep[u]) for k, u in out_u.items()}
    return out, {"method": how, "align_scale": float(sim[0])}


# ============================================================================= LOD1 transfer
def transfer_lod(rest0, wrapped0, F0, rest1, n0_rest=None, n0_wrap=None):
    """Embed rest1 points in the rest LOD0 surface and re-evaluate on the wrapped LOD0."""
    q = G.TriMeshQuery(rest0, F0)
    c = q.closest(rest1)
    n0_rest = MD.vertex_normals(rest0, F0) if n0_rest is None else n0_rest
    n0_wrap = MD.vertex_normals(wrapped0, F0) if n0_wrap is None else n0_wrap
    nr = q.interp(c["tri"], c["bary"], n0_rest)
    nr /= np.maximum(np.linalg.norm(nr, axis=1, keepdims=True), 1e-30)
    off = rest1 - c["point"]
    h = (off * nr).sum(1)
    tang = off - h[:, None] * nr
    tri = F0[c["tri"]]
    base = (wrapped0[tri] * c["bary"][:, :, None]).sum(1)
    nw = (n0_wrap[tri] * c["bary"][:, :, None]).sum(1)
    nw /= np.maximum(np.linalg.norm(nw, axis=1, keepdims=True), 1e-30)
    return base + h[:, None] * nw + tang, {"embed_dist_mm": G.stats_mm(c["dist"]), "on_border": int(c["border"].sum())}


# ============================================================================= normals / tangents
def native_normals(lod, Vu_work):
    """Per native vertex normals (work frame): welded smooth normals, except where the donor's own
    normals were split (hard edges): those vertices get normals from their own native faces."""
    Fu = lod.Fu
    Nu = MD.vertex_normals(Vu_work, Fu)
    N = Nu[lod.wid]
    if lod.normals is not None:
        n0 = np.asarray(lod.normals, float)
        n0 = n0 / np.maximum(np.linalg.norm(n0, axis=1, keepdims=True), 1e-30)
        ref = np.zeros((lod.n, 3))
        np.add.at(ref, lod.wid, n0)
        ref /= np.maximum(np.linalg.norm(ref, axis=1, keepdims=True), 1e-30)
        split = (n0 * ref[lod.wid]).sum(1) < 0.995
        if split.any():
            Pn = Vu_work[lod.wid]
            Fn = lod.F[:, ::-1] if lod.flip else lod.F
            Nn = MD.vertex_normals(Pn, Fn)
            N[split] = Nn[split]
        return N, int(split.sum())
    return N, 0


def tangents_numpy(P, F, uv, N):
    """Lengyel per-vertex tangents (approximation of MikkTSpace; used only without Blender)."""
    v0, v1, v2 = P[F[:, 0]], P[F[:, 1]], P[F[:, 2]]
    w0, w1, w2 = uv[F[:, 0]], uv[F[:, 1]], uv[F[:, 2]]
    e1, e2 = v1 - v0, v2 - v0
    d1, d2 = w1 - w0, w2 - w0
    r = d1[:, 0] * d2[:, 1] - d2[:, 0] * d1[:, 1]
    r = np.where(np.abs(r) < 1e-20, 1e-20, r)
    sdir = (e1 * d2[:, 1:2] - e2 * d1[:, 1:2]) / r[:, None]
    tdir = (e2 * d1[:, 0:1] - e1 * d2[:, 0:1]) / r[:, None]
    T = np.zeros_like(P)
    B = np.zeros_like(P)
    for k in range(3):
        np.add.at(T, F[:, k], sdir)
        np.add.at(B, F[:, k], tdir)
    T = T - N * (N * T).sum(1, keepdims=True)
    T /= np.maximum(np.linalg.norm(T, axis=1, keepdims=True), 1e-30)
    w = np.where((np.cross(N, T) * B).sum(1) < 0, -1.0, 1.0)
    return np.concatenate([T, w[:, None]], 1)


def tangents_blender(P, F, uv, N):
    exe = shutil.which("blender")
    if not exe:
        return None, "blender not on PATH"
    with tempfile.TemporaryDirectory() as td:
        src, dst = os.path.join(td, "in.npz"), os.path.join(td, "out.npz")
        np.savez(src, positions=P, faces=F, uv=uv, normals=N)
        r = subprocess.run([exe, "-b", "--factory-startup", "--python", os.path.join(HERE, "blender_tangents.py"),
                            "--", src, dst], capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(dst):
            return None, "blender failed: " + r.stdout[-500:] + r.stderr[-500:]
        z = np.load(dst)
        return z["tangents"], json.loads(str(z["info"]))


# ============================================================================= metrics
def surface_metrics(Vd, Fd, d_label, target, n_samples=60000, seed=1, d_exclude=None):
    """Bidirectional surface distance (metres -> mm stats), per region."""
    out = {"donor_to_target": {}, "target_to_donor": {}}
    Ps, tri, bary = G.sample_surface(Vd, Fd, n_samples, seed)
    lab = d_label[Fd[tri, np.argmax(bary, 1)]]
    cp = target.q.closest(Ps)
    valid = ~cp["border"]
    if d_exclude is not None:
        excl = d_exclude[Fd[tri]].any(1)
    else:
        excl = np.zeros(len(Ps), bool)
    for name, rid in list(FT.REGION_ID.items()) + [("all", None)]:
        m = (np.ones(len(Ps), bool) if rid is None else lab == rid) & valid & ~excl
        if m.sum():
            out["donor_to_target"][name] = G.stats_mm(cp["dist"][m])
    out["donor_to_target"]["_outside_target_coverage_fraction"] = float((~valid).mean())
    Pt, ttri, tbary = G.sample_surface(target.P, target.F, n_samples, seed + 1)
    tlab = target.region[target.F[ttri, np.argmax(tbary, 1)]]
    dq = G.TriMeshQuery(Vd, Fd)
    cd = dq.closest(Pt)
    tvalid = ~cd["border"]
    if d_exclude is not None:
        tvalid &= ~d_exclude[Fd[cd["tri"]]].any(1)
    for name, rid in list(FT.REGION_ID.items()) + [("all", None)]:
        m = (np.ones(len(Pt), bool) if rid is None else tlab == rid) & tvalid
        if m.sum():
            out["target_to_donor"][name] = G.stats_mm(cd["dist"][m])
    out["target_to_donor"]["_outside_donor_coverage_fraction"] = float((~tvalid).mean())
    return out


def mesh_checks(rest, new, F, edges):
    l0 = np.linalg.norm(rest[edges[:, 0]] - rest[edges[:, 1]], axis=1)
    l1 = np.linalg.norm(new[edges[:, 0]] - new[edges[:, 1]], axis=1)
    ratio = l1 / np.maximum(l0, 1e-30)
    n0 = MD.tri_normals(rest, F)
    n1 = MD.tri_normals(new, F)
    a0 = np.linalg.norm(np.cross(rest[F[:, 1]] - rest[F[:, 0]], rest[F[:, 2]] - rest[F[:, 0]]), axis=1)
    a1 = np.linalg.norm(np.cross(new[F[:, 1]] - new[F[:, 0]], new[F[:, 2]] - new[F[:, 0]]), axis=1)
    return {"edge_ratio": {"min": float(ratio.min()), "p1": float(np.percentile(ratio, 1)),
                           "p99": float(np.percentile(ratio, 99)), "max": float(ratio.max())},
            "flipped_triangles": int(((n0 * n1).sum(1) < 0).sum()),
            "triangles_turned_over_90deg": int(((n0 * n1).sum(1) < 0).sum()),
            "max_normal_change_deg": float(np.degrees(np.arccos(np.clip((n0 * n1).sum(1), -1, 1))).max()),
            "degenerate_new_triangles": int((a1 < 1e-12).sum()), "area_ratio_min": float((a1 / np.maximum(a0, 1e-30)).min())}


def sha(a):
    return hashlib.sha256(np.ascontiguousarray(a).tobytes()).hexdigest()


# ============================================================================= main pipeline
def run(args):
    t0 = time.time()
    os.makedirs(args.out_dir, exist_ok=True)
    target = Target(args.target)
    log("target", args.target, len(target.P), "verts", len(target.F), "tris")
    lod0, lod1, dmeta = load_donor(args.donor, args.donor_frame, args.donor_uv_origin, args.donor_mesh,
                                   args.donor_lod1, args.donor_lod1_mesh)
    log(f"donor LOD0 {len(lod0.P_native)} verts ({lod0.n} unique, {len(lod0.loops)} border loops, flip={lod0.flip})"
        + (f", LOD1 {len(lod1.P_native)} verts ({lod1.n} unique)" if lod1 else ""))
    report = {"inputs": {"target": os.path.abspath(args.target), "donor": os.path.abspath(args.donor),
                         "donor_source": lod0.source.get("kind"), "donor_lod1": bool(lod1)},
              "options": {k: v for k, v in vars(args).items() if isinstance(v, (str, int, float, bool, type(None)))}}
    # ---------------------------------------------------------------- features
    if args.features:
        feats = load_feature_json(args.features)
        report["features_source"] = os.path.abspath(args.features)
    elif args.guess_features:
        feats, gd = guess_features(lod0, target)
        json.dump({"features": feats, "diagnostics": gd, "note": "guessed by wrap.guess_features - check by eye"},
                  open(os.path.join(args.out_dir, "guessed_features.json"), "w"), indent=1)
        report["features_source"] = "guessed"
        report["feature_guess"] = gd
    else:
        raise SystemExit("give --features JSON or --guess-features")
    sim, sinfo = similarity_from_features(lod0, target, feats, args.align)
    report["alignment"] = sinfo
    log(f"alignment: {sinfo['pairs']} pairs, scale {sinfo['scale']:.4f}, rms {sinfo['rms_mm']:.2f} mm")
    Pa = sim_apply(sim, lod0.Pw)                                   # aligned rest LOD0 (work frame)
    # ---------------------------------------------------------------- regions
    locked_native = None
    if args.locked:
        d = json.load(open(args.locked))
        locked_native = d.get("locked", d) if isinstance(d, dict) else d
    reg = donor_regions(lod0, Pa, target, args.lock_rings, locked_native)
    rcount = {"locked": int(reg["locked"].sum()), "nodata": int(reg["nodata"].sum()),
              "cavity": int(reg["cavity"].sum()), "hole_rims": int(reg["holes"].sum()),
              "by_target_label": {FT.REGIONS[i]: int((reg["label"] == i).sum()) for i in np.unique(reg["label"])}}
    report["donor_regions"] = rcount
    log("regions:", json.dumps(rcount))
    # landmarks for NICP (features not in locked/no-data regions, unless eye-mode follow)
    lm_names = [k for k in feats if target.has(k)]
    lm_ids = lod0.wid[[feats[k] for k in lm_names]]
    keep = ~reg["locked"][lm_ids]
    eye_lab = np.isin(reg["label"], [FT.REGION_ID["eye_R"], FT.REGION_ID["eye_L"]])
    anchor = None
    if args.eye_mode == "anchor":
        anchor = (eye_lab | reg["cavity"]) & reg["nodata"]
        keep &= ~anchor[lm_ids]
    landmarks = (lm_ids[keep], np.array([target.point(k) for k, kk in zip(lm_names, keep) if kk]))
    # target samples for the symmetric term: target vertices outside no-data / border regions
    tr = target.region
    tmask = ~np.isin(tr, [FT.REGION_ID[r] for r in FT.NO_DATA_REGIONS + ("neck_ring",)])
    tsamples = {"P": target.P[tmask], "N": target.N[tmask], "w": np.ones(int(tmask.sum()))}
    opts = {"schedule": [float(x) for x in args.schedule.split(",")], "max_normal_deg": args.max_normal_deg,
            "max_dist_mm": args.max_dist_mm, "landmark_weight": args.landmark_weight,
            "landmark_decay": args.landmark_decay, "t2d_weight": args.t2d_weight,
            "max_inner": args.max_inner, "anchor_weight": args.anchor_weight, "gamma": 1.0,
            "refine_iters": args.refine_iters, "refine_smooth": args.refine_smooth, "refine_max_mm": args.refine_max_mm}
    data_mask = ~reg["nodata"] & ~reg["locked"]
    t1 = time.time()
    Vw, hist, guard = nicp(Pa, lod0.Fu, lod0.edges, target.q, target.N, opts, locked=reg["locked"], data_mask=data_mask,
                    landmarks=landmarks, tgt_samples=tsamples, anchor_mask=anchor)
    report["nicp"] = {"iterations": len(hist), "seconds": round(time.time() - t1, 2), "history": hist,
                      "landmarks_used": int(keep.sum()), "options": opts,
                      "flip_guarded_vertices": {FT.REGIONS[i]: int(((reg["label"] == i) & guard).sum())
                                                for i in np.unique(reg["label"][guard])}}
    if args.refine_iters > 0:
        Vw, rinfo = refine_projection(Vw, lod0.Fu, lod0.edges, Pa, target.q, target.N, reg["locked"], data_mask, opts)
        report["refine"] = rinfo
    Vw[reg["locked"]] = Pa[reg["locked"]]                          # exact (already identity)
    # ---------------------------------------------------------------- outputs LOD0
    out = {}
    res0 = finish_lod("lod0", lod0, Pa, Vw, sim, args, report, locked=reg["locked"])
    out.update(res0)
    # metrics (work frame, target units = metres -> mm)
    report["surface_distance_mm"] = surface_metrics(Vw, lod0.Fu, reg["label"], target, args.metric_samples,
                                                    d_exclude=reg["nodata"] | reg["locked"])
    report["surface_distance_mm_incl_nodata"] = surface_metrics(Vw, lod0.Fu, reg["label"], target,
                                                                args.metric_samples // 3)
    report["mesh_checks"] = mesh_checks(Pa, Vw, lod0.Fu, lod0.edges)
    disp = np.linalg.norm(Vw - Pa, axis=1)
    report["locked_max_displacement_mm"] = float(disp[reg["locked"]].max() * 1000) if reg["locked"].any() else None
    report["displacement_mm"] = {"all": G.stats_mm(disp),
                                 "nodata": G.stats_mm(disp[reg["nodata"]]) if reg["nodata"].any() else None,
                                 "eye_zone": G.stats_mm(disp[eye_lab]) if eye_lab.any() else None}
    # ---------------------------------------------------------------- LOD1
    if lod1 is not None:
        P1a = sim_apply(sim, lod1.Pw)
        V1, emb = transfer_lod(Pa, Vw, lod0.Fu, P1a)
        neck1 = lod1.neck_loop()
        lock1 = np.zeros(lod1.n, bool)
        if neck1 is not None:            # LOD1 seam = its border loop (LOD1 rings are wider than LOD0's)
            lock1[neck1] = True
        pre = np.linalg.norm(V1[lock1] - P1a[lock1], axis=1)
        V1[lock1] = P1a[lock1]
        res1 = finish_lod("lod1", lod1, P1a, V1, sim, args, report, locked=lock1)
        out.update(res1)
        q0 = G.TriMeshQuery(Vw, lod0.Fu)
        q0r = G.TriMeshQuery(Pa, lod0.Fu)
        Ps1, _, _ = G.sample_surface(V1, lod1.Fu, 30000, 3)
        Ps1r, _, _ = G.sample_surface(P1a, lod1.Fu, 30000, 3)
        report["lod1"] = {"embedding": emb, "locked_verts": int(lock1.sum()),
                          "locked_transfer_residual_before_clamp_mm": G.stats_mm(pre) if len(pre) else None,
                          "vertex_to_wrapped_lod0_mm": G.stats_mm(q0.closest(V1)["dist"]),
                          "surface_to_wrapped_lod0_mm": G.stats_mm(q0.closest(Ps1)["dist"]),
                          "rest_vertex_to_rest_lod0_mm": G.stats_mm(q0r.closest(P1a)["dist"]),
                          "rest_surface_to_rest_lod0_mm": G.stats_mm(q0r.closest(Ps1r)["dist"]),
                          "mesh_checks": mesh_checks(P1a, V1, lod1.Fu, lod1.edges)}
        report["lod1"]["surface_to_target_mm"] = surface_metrics(V1, lod1.Fu,
                                                                 donor_regions(lod1, P1a, target, args.lock_rings)["label"],
                                                                 target, args.metric_samples // 3)
    # ---------------------------------------------------------------- write
    meta = {"format": "r9wrap-result/1", "donor_frame": lod0.source.get("frame"), "uv_origin": lod0.uv_origin,
            "work_frame": MD.frame_def("blender"), "similarity": {"s": float(sim[0]), "R": sim[1].tolist(),
                                                                   "t": sim[2].tolist()},
            "M_native_to_work": {"lod0": lod0.M_nw.tolist(), "lod1": lod1.M_nw.tolist() if lod1 else None},
            "target": os.path.abspath(args.target), "donor": os.path.abspath(args.donor),
            "regions": FT.REGIONS}
    out["lod0.region_label"] = reg["label"][lod0.wid].astype(np.int8)
    out["lod0.locked"] = reg["locked"][lod0.wid]
    out["lod0.nodata"] = reg["nodata"][lod0.wid]
    npz = os.path.join(args.out_dir, args.name + ".npz")
    MD.save_npz(npz, out, meta)
    glb_path = os.path.join(args.out_dir, args.name + ".glb")
    write_glb_out(glb_path, lod0, lod1, out)
    report["outputs"] = {"npz": npz, "glb": glb_path}
    report["seconds_total"] = round(time.time() - t0, 2)
    rp = os.path.join(args.out_dir, args.name + "_report.json")
    json.dump(report, open(rp, "w"), indent=1, default=float)
    report["outputs"]["report"] = rp
    s = report["surface_distance_mm"]
    log("face d->t", fmt(s["donor_to_target"].get("face")), "| t->d", fmt(s["target_to_donor"].get("face")))
    log("flipped", report["mesh_checks"]["flipped_triangles"], "locked max", report["locked_max_displacement_mm"],
        "mm; edge ratio", report["mesh_checks"]["edge_ratio"])
    log("wrote", npz, glb_path, rp, f"({report['seconds_total']} s)")
    return report


def fmt(st):
    return "n/a" if not st else f"mean {st['mean']:.3f} p95 {st['p95']:.3f} max {st['max']:.3f} mm"


def finish_lod(tag, lod, Pa, Vw, sim, args, report, locked=None):
    """Scatter to native order, back to the native frame, normals, tangents, checks.
    Locked (seam) vertices keep the donor's own normals when it has them, so the shading across
    the head/body seam cannot change."""
    V_native_work = sim_inverse_apply(sim, Vw)                     # donor work frame, unaligned
    Minv = np.linalg.inv(lod.M_nw)
    Pn = apply_affine(Minv, V_native_work[lod.wid])
    Nw, nsplit = native_normals(lod, Vw)
    Rn = dir_matrix(Minv)
    Rs = sim[1]
    Nn = (Nw @ Rs) @ Rn.T                                          # undo similarity rotation, to native
    Nn /= np.maximum(np.linalg.norm(Nn, axis=1, keepdims=True), 1e-30)
    kept_normals = 0
    if locked is not None and lod.normals is not None:
        ln = locked[lod.wid]
        n0 = np.asarray(lod.normals, float)
        Nn[ln] = n0[ln] / np.maximum(np.linalg.norm(n0[ln], axis=1, keepdims=True), 1e-30)
        kept_normals = int(ln.sum())
    out = {f"{tag}.positions": Pn, f"{tag}.normals": Nn.astype(np.float32), f"{tag}.faces": lod.F.astype(np.int32),
           f"{tag}.work_positions": Vw[lod.wid], f"{tag}.work_normals": Nw.astype(np.float32),
           f"{tag}.rest_work_positions": Pa[lod.wid]}
    if lod.uv is not None:
        out[f"{tag}.uv0"] = lod.uv
    if lod.uv_corner is not None:
        out[f"{tag}.uv_corner"] = lod.uv_corner
    if lod.joints is not None:
        out[f"{tag}.joints"], out[f"{tag}.weights"] = lod.joints, lod.weights
    tinfo = {"method": "none"}
    uv_for_t = lod.uv if lod.uv is not None else None
    if args.tangents != "none" and uv_for_t is not None:
        T = None
        if args.tangents == "blender":
            T, info = tangents_blender(Pn, lod.F, uv_for_t, Nn)
            tinfo = {"method": "mikktspace (Blender mesh.calc_tangents)", "info": info} if T is not None else \
                {"method": "numpy-lengyel (blender unavailable)", "why": info}
        if T is None:
            T = tangents_numpy(Pn, lod.F, np.asarray(uv_for_t, float), Nn)
            tinfo.setdefault("method", "numpy-lengyel")
            if args.tangents == "numpy":
                tinfo = {"method": "numpy-lengyel"}
        out[f"{tag}.tangents"] = T.astype(np.float32)
    seam_gap = 0.0
    if len(lod.rep) < len(lod.P_native):
        grp = Pn - Pn[lod.rep][lod.wid]
        seam_gap = float(np.abs(grp).max())
    rep = {"vertex_count_in": int(len(lod.P_native)), "vertex_count_out": int(len(Pn)),
           "unique_vertices": int(lod.n), "faces_identical": True,
           "uv_unchanged": (lod.uv is None) or sha(out[f"{tag}.uv0"]) == sha(lod.uv),
           "uv_sha256": sha(lod.uv) if lod.uv is not None else None,
           "skin_unchanged": (lod.joints is None) or (sha(out[f"{tag}.joints"]) == sha(lod.joints)
                                                      and sha(out[f"{tag}.weights"]) == sha(lod.weights)),
           "seam_duplicate_max_gap_native_units": seam_gap,
           "split_normal_vertices": nsplit, "tangents": tinfo, "locked_vertices_kept_input_normals": kept_normals,
           "bounds_native_before": MD.bounds(lod.P_native), "bounds_native_after": MD.bounds(Pn),
           "native_units_per_metre": float(1.0 / np.cbrt(abs(np.linalg.det(lod.M_nw[:3, :3])))),
           "winding_flipped_internally": bool(lod.flip), "degenerate_input_triangles": lod.degenerate,
           "nonmanifold_edges": lod.nonmanifold_edges, "border_loops": [int(len(x)) for x in lod.loops]}
    if lod.normals is not None:
        n0 = np.asarray(lod.normals, float)
        n0 /= np.maximum(np.linalg.norm(n0, axis=1, keepdims=True), 1e-30)
        rep["normal_sign_agreement_with_input"] = float(((n0 * Nn).sum(1) > 0).mean())
    report[tag + "_buffers"] = rep
    return out


def write_glb_out(path, lod0, lod1, out):
    """Donor GLB in -> patched copy (skins/materials kept). Other donors -> fresh GLB (glTF frame)."""
    if lod0.source.get("kind") == "glb" and (lod1 is None or lod1.source.get("path") == lod0.source["path"]):
        g = GLB.read_glb(lod0.source["path"])
        repl = {}
        for tag, lod in (("lod0", lod0), ("lod1", lod1)):
            if lod is None:
                continue
            m = g["meshes"][lod.source["mesh"]]
            for (pi, off, n) in lod.source["spans"]:
                acc = m["primitives"][pi]["accessors"]
                repl[acc["POSITION"]] = out[f"{tag}.positions"][off:off + n].astype(np.float32)
                if "NORMAL" in acc:
                    repl[acc["NORMAL"]] = out[f"{tag}.normals"][off:off + n].astype(np.float32)
                if "TANGENT" in acc and f"{tag}.tangents" in out:
                    repl[acc["TANGENT"]] = out[f"{tag}.tangents"][off:off + n].astype(np.float32)
        GLB.patch_glb(lod0.source["path"], path, repl, extras={"r9wrap": "positions/normals/tangents replaced"})
        return
    gl = MD.frame_def("gltf")
    meshes = []
    for tag, lod in (("lod0", lod0), ("lod1", lod1)):
        if lod is None:
            continue
        W = out[f"{tag}.positions"]
        Pw = apply_affine(lod.M_nw, W)
        Rn = dir_matrix(lod.M_nw)
        Nw = out[f"{tag}.normals"] @ Rn.T
        uv = None
        if lod.uv is not None:
            uv = MD.uv_from_bottom_left(MD.uv_to_bottom_left(lod.uv, lod.uv_origin), "top-left")
        F = lod.F
        det = np.linalg.det(lod.M_nw[:3, :3]) * gl["det"]
        if det < 0:
            F = F[:, ::-1]
        T = None
        if f"{tag}.tangents" in out:
            T0 = out[f"{tag}.tangents"]
            Tx = MD.dir_blender_to_frame(T0[:, :3] @ Rn.T, gl)
            flip_uv = (lod.uv_origin != "top-left")
            T = np.concatenate([Tx, (T0[:, 3:] * np.sign(det) * (-1 if flip_uv else 1))], 1)
        meshes.append({"name": tag, "positions": MD.blender_to_frame(Pw, gl), "normals": MD.dir_blender_to_frame(Nw, gl),
                       "tangents": T, "uv0": uv, "faces": F, "joints": lod.joints, "weights": lod.weights})
    GLB.write_glb(path, meshes, extras={"r9wrap": "fresh GLB in glTF frame; native buffers are in the NPZ"})


def build_argparser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", required=True)
    ap.add_argument("--donor", required=True)
    ap.add_argument("--donor-frame", help="frame preset of a NPZ/OBJ donor (NPZ meta wins if omitted; GLB = gltf)")
    ap.add_argument("--donor-uv-origin", choices=["top-left", "bottom-left"])
    ap.add_argument("--donor-mesh")
    ap.add_argument("--donor-lod1", default="auto")
    ap.add_argument("--donor-lod1-mesh")
    ap.add_argument("--features")
    ap.add_argument("--guess-features", action="store_true")
    ap.add_argument("--locked", help="JSON list (or {'locked': [...]}) of native LOD0 vertex ids to lock")
    ap.add_argument("--lock-rings", type=int, default=1)
    ap.add_argument("--eye-mode", choices=["follow", "anchor"], default="follow")
    ap.add_argument("--anchor-weight", type=float, default=1.0)
    ap.add_argument("--align", choices=["similarity", "rigid"], default="similarity")
    ap.add_argument("--schedule", default="50,20,10,5,2,1")
    ap.add_argument("--refine-iters", type=int, default=2)
    ap.add_argument("--refine-smooth", type=float, default=0.05)
    ap.add_argument("--refine-max-mm", type=float, default=3.0)
    ap.add_argument("--max-inner", type=int, default=6)
    ap.add_argument("--max-normal-deg", type=float, default=60.0)
    ap.add_argument("--max-dist-mm", type=float, default=25.0)
    ap.add_argument("--landmark-weight", type=float, default=3.0)
    ap.add_argument("--landmark-decay", type=float, default=0.5,
                    help="landmark weight = w * (alpha/alpha0)^decay (0 = constant)")
    ap.add_argument("--t2d-weight", type=float, default=0.5)
    ap.add_argument("--tangents", choices=["blender", "numpy", "none"], default="blender")
    ap.add_argument("--metric-samples", type=int, default=60000)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--name", default="wrapped")
    return ap


if __name__ == "__main__":
    run(build_argparser().parse_args())
