#!/usr/bin/env python3
"""Adversarial cases for the donor wrap (README §6.5). Used by tools/tests/test_wrap_adversarial.py.

  python3 tools/wrap/adversarial.py --work DIR [--base SYNTH_DIR] [--cases a,b,...]

Every case takes the synthetic setup of synthetic.py (our head rebuilt from
model/source/r9_params_start.json + the fake MakeHuman donor of make_fake_donor.py) and breaks ONE
assumption a real eFootball donor or a hurried user can break:

  frames      mirrored donor frame (refused), mirrored donor with features picked on the mirrored
              display (refused by the skin-weight side check), centimetres declared as metres +
              Y-up declared as Z-up (must give the same result as the base), --align rigid with wrong
              units (refused), target exported in UE centimetres (identical millimetre numbers)
  guess_yup   --guess-features on a Y-up donor declared Z-up
  features    2-5 mm feature-pick noise, two swapped feature names (rejected as outliers), 1-based
              feature ids (refused)
  submeshes   eyeballs, upper/lower teeth and a double-sided eyelash card as separate components of
              LOD0 and LOD1, with UVs laid over the skin's texture space (crashed the solver before)
  uv_seam     a UV seam cut through the middle of the face
  nonmanifold fins on lip edges (edges with 3 triangles) + a double-sided scalp decal, donor without
              normals (exercises the normal fallback)
  coarse_lod1 a 350-vertex LOD1
  hair        the hair shell merged into the target's head part (must be flagged)
  tangents    donor with its own MikkTSpace tangents, and with the opposite bitangent-sign convention
  glb         GLB donor with a scaled/rotated node and an eyes mesh larger than LOD1 (LOD1 pick)
Each case returns {"checks": {name: bool}, ...numbers}; adversarial_summary.json collects them.
"""
import argparse
import json
import os
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
import features as FT  # noqa: E402
import geom as G  # noqa: E402
import glb as GLB  # noqa: E402
import meshdata as MD  # noqa: E402
import transfer_texture as TT  # noqa: E402
import wrap as WR  # noqa: E402

# reduced schedule for cases whose property does not depend on the last 0.1 mm of fit (compared
# against a base run with the same schedule)
FAST = ["--schedule", "20,5,1", "--max-inner", "3", "--refine-iters", "1", "--metric-samples", "15000"]


def sh(cmd, log_path=None):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if log_path:
        open(log_path, "a").write(" ".join(cmd) + "\n" + r.stdout[-5000:] + r.stderr[-5000:] + "\n")
    if r.returncode != 0:
        raise RuntimeError(f"failed: {' '.join(cmd)}\n{r.stdout[-3000:]}\n{r.stderr[-3000:]}")


# ============================================================================= base setup
class Ctx:
    """Paths + arrays of the base synthetic setup (target NPZ, donor NPZ, ground truth)."""

    def __init__(self, work, base=None, log=print):
        self.work = work
        self.log = log
        os.makedirs(work, exist_ok=True)
        self.logf = os.path.join(work, "subprocess.log")
        if base and os.path.exists(os.path.join(base, "target", "target.npz")) and \
                os.path.exists(os.path.join(base, "donor", "donor.npz")):
            self.target = os.path.join(base, "target", "target.npz")
            dd = os.path.join(base, "donor")
        else:
            td = os.path.join(work, "target")
            os.makedirs(td, exist_ok=True)
            blend = os.path.join(td, "r9_target.blend")
            sh(["blender", "-b", "--python", os.path.join(REPO, "tools/model/blender_build.py"), "--",
                "--params", os.path.join(REPO, "model/source/r9_params_start.json"), "--out", blend], self.logf)
            self.target = os.path.join(td, "target.npz")
            sh(["blender", "-b", blend, "--python", os.path.join(HERE, "export_head.py"), "--", "--out", self.target],
               self.logf)
            dd = os.path.join(work, "donor")
            sh(["blender", "-b", "--python", os.path.join(HERE, "make_fake_donor.py"), "--", "--out", dd], self.logf)
        self.donor = os.path.join(dd, "donor.npz")
        self.gt_npz = os.path.join(dd, "donor_gt.npz")
        self.gt_json = os.path.join(dd, "donor_features_gt.json")
        self.D, self.meta = MD.load_npz(self.donor)
        self.gt = json.load(open(self.gt_json))
        self.feats = self.gt["features"]
        self._ideal = None

    def path(self, *p):
        q = os.path.join(self.work, *p)
        os.makedirs(os.path.dirname(q), exist_ok=True)
        return q

    def save_donor(self, name, D, meta=None):
        p = self.path("donors", name + ".npz")
        MD.save_npz(p, D, meta or self.meta)
        return p

    def save_features(self, name, feats):
        p = self.path("features", name + ".json")
        json.dump({"features": feats}, open(p, "w"))
        return p

    def Pb(self):
        return MD.frame_to_blender(np.asarray(self.D["positions"], float), self.meta["frame"])

    def feature_points_blender(self):
        return {k: MD.frame_to_blender(np.array(v)[None], self.meta["frame"])[0]
                for k, v in self.gt["feature_points"].items()}

    # ------------------------------------------------------------------------- evaluation
    def ideal(self, target_npz=None):
        """Ground-truth position on our head (work frame) of every base donor LOD0 vertex."""
        if self._ideal is not None and target_npz is None:
            return self._ideal
        T, tm = MD.load_npz(target_npz or self.target)
        gt = np.load(self.gt_npz)
        q = G.TriMeshQuery(gt["src_positions"], gt["src_faces"])
        c = q.closest(gt["lod0_blender"])
        mh = gt["src_mh_index"][gt["src_faces"][c["tri"]]]
        tpos = {int(m): i for i, m in enumerate(T["head.mh_index"])}
        tP = MD.frame_to_blender(T["head.positions"], tm["frame"])
        ideal = np.full((len(mh), 3), np.nan)
        for i in range(len(mh)):
            if all(int(x) in tpos for x in mh[i]):
                ideal[i] = sum(b * tP[tpos[int(m)]] for m, b in zip(mh[i], c["bary"][i]))
        if target_npz is None:
            self._ideal = ideal
        return ideal

    def semantic(self, wrap_npz, src_index=None, target_npz=None):
        """Face-region semantic correspondence error (mm) of a wrap whose LOD0 vertices map to base
        donor vertices through src_index (-1 = added vertex, not evaluated)."""
        R, _ = MD.load_npz(wrap_npz)
        W = R["lod0.work_positions"]
        src = np.arange(len(W)) if src_index is None else np.asarray(src_index)
        ideal = self.ideal(target_npz)
        idl = np.full((len(W), 3), np.nan)
        ok = src >= 0
        idl[ok] = ideal[src[ok]]
        err = np.linalg.norm(W - idl, axis=1) * 1000
        m = np.isfinite(err) & (R["lod0.region_label"] == FT.REGION_ID["face"]) & ~R["lod0.nodata"] & ~R["lod0.locked"]
        return {"face_mean_mm": float(err[m].mean()), "face_p95_mm": float(np.percentile(err[m], 95)), "n": int(m.sum())}

    def wrap(self, name, donor, features=None, extra=(), guess=False, target=None):
        """-> (report, None) or (None, refusal message)."""
        out = self.path("runs", name, "x")[:-2]
        a = ["--target", target or self.target, "--donor", donor, "--out-dir", out, "--tangents", "none"]
        a += ["--features", features or self.gt_json] if not guess else ["--guess-features"]
        a += list(extra)
        t = time.time()
        try:
            rep = WR.run(WR.build_argparser().parse_args(a))
        except SystemExit as e:
            self.log(f"[adv] {name}: refused ({time.time() - t:.1f} s): {str(e).splitlines()[0]}")
            return None, str(e)
        self.log(f"[adv] {name}: wrapped in {time.time() - t:.1f} s")
        return rep, None


def summary(rep):
    s = rep["surface_distance_mm"]
    return {"align_rms_mm": rep["alignment"]["rms_mm"], "align_pairs": rep["alignment"]["pairs"],
            "rejected": rep["alignment"]["rejected_outliers_mm"], "face_d2t_mean_mm": s["donor_to_target"]["face"]["mean"],
            "face_t2d_mean_mm": s["target_to_donor"]["face"]["mean"], "flips_lod0": rep["mesh_checks"]["flipped_triangles"],
            "edge_ratio_min": rep["mesh_checks"]["edge_ratio"]["min"],
            "flips_lod1": rep["lod1"]["mesh_checks"]["flipped_triangles"] if "lod1" in rep else None,
            "warnings": rep.get("warnings", [])}


# ============================================================================= variant builders
def mirrored_frame(fd):
    """The same axes with the handedness flipped (one output axis negated): a donor declared like this
    is read as its own mirror image."""
    A = np.asarray(fd["A"], float).copy()
    A[1] *= -1
    rows = []
    for r in A:
        k = int(np.argmax(np.abs(r)))
        rows.append(("-" if r[k] < 0 else "") + "xyz"[k])
    return MD.frame_def(axes=",".join(rows), scale=fd["scale"])


def swap_sides(feats):
    """Feature names as a person picks them on a MIRRORED display: _R <-> _L (mp: points dropped)."""
    out = {}
    for k, v in feats.items():
        if k.endswith("_R"):
            out[k[:-2] + "_L"] = v
        elif k.endswith("_L"):
            out[k[:-2] + "_R"] = v
        elif not k.startswith("mp:"):
            out[k] = v
    return out


def rotate_native(D, R):
    D = dict(D)
    for k in ("positions", "lod1.positions", "normals", "lod1.normals"):
        if k in D:
            D[k] = (np.asarray(D[k], float) @ R.T).astype(np.asarray(D[k]).dtype)
    return D


def perturb_features(P_mm_frame, feats, lo, hi, seed):
    """Replace every feature id by a random vertex lo..hi mm away (closest >= lo if none in the band)."""
    rng = np.random.default_rng(seed)
    out, d = {}, []
    for k, v in feats.items():
        dist = np.linalg.norm(P_mm_frame - P_mm_frame[v], axis=1)
        cand = np.nonzero((dist >= lo) & (dist <= hi))[0]
        if not len(cand):
            cand = np.argsort(np.where(dist >= lo, dist, np.inf))[:3]
        out[k] = int(rng.choice(cand))
        d.append(dist[out[k]])
    return out, float(np.mean(d))


def _sphere(c, r, nu, nv):
    P, UV = [], []
    for j in range(nv + 1):
        th = np.pi * j / nv
        for i in range(nu + 1):
            ph = 2 * np.pi * i / nu
            P.append(c + r * np.array([np.sin(th) * np.cos(ph), np.sin(th) * np.sin(ph), np.cos(th)]))
            UV.append([i / nu, 1 - j / nv])
    P, UV = np.array(P), np.array(UV)
    F = []
    for j in range(nv):
        for i in range(nu):
            a = j * (nu + 1) + i
            b = a + nu + 1
            if j > 0:
                F.append([a, b, a + 1])
            if j < nv - 1:
                F.append([a + 1, b, b + 1])
    return P, np.array(F), UV, (P - c) / r


def _band(centre, fwd, up, lat, radius, h, span_deg, nseg, nh=2):
    P, UV = [], []
    for j in range(nh + 1):
        for i in range(nseg + 1):
            a = np.radians(-span_deg / 2 + span_deg * i / nseg)
            P.append(centre + radius * (np.sin(a) * lat + np.cos(a) * fwd) + (j / nh - 0.5) * h * up)
            UV.append([i / nseg, j / nh])
    P, UV = np.array(P), np.array(UV)
    F = []
    for j in range(nh):
        for i in range(nseg):
            a = j * (nseg + 1) + i
            b = a + nseg + 1
            F += [[a, a + 1, b + 1], [a, b + 1, b]]
    F = np.array(F)
    n = np.cross(P[F[:, 1]] - P[F[:, 0]], P[F[:, 2]] - P[F[:, 0]])
    if (n @ fwd).mean() < 0:
        F = F[:, ::-1]
    return P, F, UV, MD.vertex_normals(P, F)


def _axes(fp):
    lat = fp["eye_outer_L"] - fp["eye_outer_R"] + fp["mouth_corner_L"] - fp["mouth_corner_R"]
    lat /= np.linalg.norm(lat)
    up = (fp["eye_outer_L"] + fp["eye_outer_R"]) / 2 - fp["chin"]
    up -= lat * (up @ lat)
    up /= np.linalg.norm(up)
    return lat, up, np.cross(lat, up)


def submesh_parts(fp, coarse=False):
    """Eyeballs (bones eye_r=5 / eye_l=4), upper/lower teeth arcs (head=2 / jaw=3) and a double-sided
    eyelash card on the right upper lid (front + back copy with own vertices, reversed winding,
    flipped normals - what game exporters write). Blender frame; UVs fill 0..1 (over the skin's)."""
    lat, up, fwd = _axes(fp)
    k = 0.5 if coarse else 1.0
    parts = []
    for s, bone in (("R", 5), ("L", 4)):
        c = (fp[f"eye_outer_{s}"] + fp[f"eye_inner_{s}"]) / 2 - fwd * 0.004
        parts.append(("eye_" + s, *_sphere(c, 0.0115, int(16 * k), int(10 * k)), bone))
    mc = (fp["mouth_corner_R"] + fp["mouth_corner_L"]) / 2
    for nm, dz, bone in (("teeth_up", 0.004, 2), ("teeth_lo", -0.004, 3)):
        parts.append((nm, *_band(mc - fwd * 0.030 + up * dz, fwd, up, lat, 0.026, 0.007, 90, int(12 * k)), bone))
    a, b = fp["eye_outer_R"], fp["eye_inner_R"]
    n = 4 if coarse else 8
    P = np.array([a + (b - a) * (i / n) + up * (0.004 * np.sin(np.pi * i / n) + 0.003 * j) + fwd * (0.002 + 0.003 * j)
                  for j in range(2) for i in range(n + 1)])
    UV = np.array([[i / n, j] for j in range(2) for i in range(n + 1)], float)
    F = np.array([f for i in range(n) for f in ([i, i + 1, n + 2 + i], [i, n + 2 + i, n + 1 + i])])
    nrm = np.cross(P[F[0, 1]] - P[F[0, 0]], P[F[0, 2]] - P[F[0, 0]])
    N = np.tile(nrm / np.linalg.norm(nrm), (len(P), 1))
    parts.append(("lash_R", np.r_[P, P], np.r_[F, F[:, ::-1] + len(P)], np.r_[UV, UV], np.r_[N, -N], 2))
    return parts


def append_parts(D, meta, prefix, parts):
    """Append Blender-frame parts to one LOD of a donor (native frame). Returns (n_before, tags)."""
    fd, uo = meta["frame"], meta["uv_origin"]
    n0 = len(D[prefix + "positions"])
    off, tags = n0, []
    cat = {k: [np.asarray(D[prefix + k])] for k in ("positions", "faces", "uv0", "normals", "joints", "weights")}
    for nm, P, F, UV, N, bone in parts:
        cat["positions"].append(MD.blender_to_frame(P, fd))
        cat["faces"].append(MD.faces_for_frame(F, fd) + off)
        cat["uv0"].append(MD.uv_from_bottom_left(UV, uo))
        cat["normals"].append(MD.dir_blender_to_frame(N, fd))
        j = np.zeros((len(P), 4), np.uint16)
        j[:, 0] = bone
        w = np.zeros((len(P), 4), np.float32)
        w[:, 0] = 1
        cat["joints"].append(j)
        cat["weights"].append(w)
        tags.append((nm, off, len(P)))
        off += len(P)
    for k, v in cat.items():
        D[prefix + k] = np.concatenate(v).astype(v[0].dtype)
    return n0, tags


def split_face_uv(D, meta, feats, gap=0.02, shrink=0.9):
    """Cut the UV island holding the nose tip along the face midline (LOD0): the cut vertices get a
    duplicate GPU vertex with its own UV; both halves are shrunk and pulled apart. -> (D, src_index)."""
    D = {k: np.array(v) for k, v in D.items()}
    P = np.asarray(D["positions"], float)
    F = np.asarray(D["faces"]).astype(np.int64)
    uv = np.asarray(D["uv0"], float)
    Pb = MD.frame_to_blender(P, meta["frame"])
    _, isl = G.components(F, len(P))
    lat = Pb[feats["eye_outer_L"]] - Pb[feats["eye_outer_R"]]
    lat /= np.linalg.norm(lat)
    mid = Pb[feats["nose_tip"]] @ lat
    tf = isl[F[:, 0]] == isl[feats["nose_tip"]]
    right = (Pb[F].mean(1) @ lat) < mid
    vR = np.zeros(len(P), bool)
    vR[F[tf & right].ravel()] = True
    vL = np.zeros(len(P), bool)
    vL[F[tf & ~right].ravel()] = True
    cut = np.nonzero(vR & vL)[0]
    newid = {int(v): len(P) + i for i, v in enumerate(cut)}
    src = np.r_[np.arange(len(P)), cut]
    F2 = F.copy()
    for t in np.nonzero(tf & right)[0]:
        F2[t] = [newid.get(int(v), v) for v in F[t]]
    uv2 = np.concatenate([uv, uv[cut]])
    inR = np.zeros(len(src), bool)
    inR[np.unique(F2[tf & right])] = True
    inL = np.zeros(len(src), bool)
    inL[np.unique(F2[tf & ~right])] = True
    ids = np.nonzero(inR | inL)[0]
    c = uv2[ids].mean(0)
    x = Pb[src[ids]] @ lat
    g = np.linalg.lstsq(np.c_[x - x.mean(), np.ones(len(x))], uv2[ids] - c, rcond=None)[0][0]
    g /= np.linalg.norm(g)
    uv2[ids] = c + shrink * (uv2[ids] - c)
    uv2[inR] -= g * gap / 2
    uv2[inL] += g * gap / 2
    for k in ("positions", "normals", "joints", "weights"):
        D[k] = np.asarray(D[k])[src]
    D["uv0"] = uv2.astype(np.float32)
    D["faces"] = F2.astype(np.int32)
    return D, src, int(len(cut))


def add_nonmanifold(D, meta, feats, drop_normals=True):
    """Fins: 6 triangles hanging inward off lip-area edges (those edges get 3 triangles); a double-sided
    decal: 20 scalp triangles repeated with their own vertices and reversed winding. The donor's
    normals are dropped (default) so wrap.py must derive the decal's back-face normals itself."""
    D = {k: np.array(v) for k, v in D.items()}
    P = np.asarray(D["positions"], float)
    F = np.asarray(D["faces"]).astype(np.int64)
    Pb = MD.frame_to_blender(P, meta["frame"])
    mc = (Pb[feats["mouth_corner_R"]] + Pb[feats["mouth_corner_L"]]) / 2
    lat, up, fwd = _axes({k: Pb[v] for k, v in feats.items() if not k.startswith("mp:")})
    near = np.argsort(np.linalg.norm(Pb[F].mean(1) - mc, axis=1))[:6]
    addP, addF = [], []
    n = len(P)
    for t in near:
        a, b = F[t, 0], F[t, 1]
        addP.append((Pb[a] + Pb[b]) / 2 - fwd * 0.006)
        addF.append([b, a, n + len(addP) - 1])
    nfin = len(addP)
    top = np.argsort(-(Pb[F].mean(1) @ up))[:20]
    vs = np.unique(F[top])
    vmap = {int(v): n + len(addP) + i for i, v in enumerate(vs)}
    addP += [Pb[v] for v in vs]
    for t in top:
        addF.append([vmap[int(F[t, 2])], vmap[int(F[t, 1])], vmap[int(F[t, 0])]])
    k = len(addP)
    # added faces are written in native index order (the decal reverses the native winding)
    D["positions"] = np.concatenate([P, MD.blender_to_frame(np.array(addP), meta["frame"])])
    D["faces"] = np.concatenate([F, np.array(addF)]).astype(np.int32)
    D["uv0"] = np.concatenate([D["uv0"], np.zeros((k, 2), np.float32)])
    D["joints"] = np.concatenate([D["joints"], np.tile(D["joints"][:1], (k, 1))])
    D["weights"] = np.concatenate([D["weights"], np.tile(D["weights"][:1], (k, 1))])
    if drop_normals:
        D.pop("normals", None)
        D.pop("lod1.normals", None)
    else:
        D["normals"] = np.concatenate([D["normals"], np.zeros((k, 3), np.float32)])
    src = np.r_[np.arange(n), -np.ones(k, int)]
    decal_front = vs
    decal_back = np.array([vmap[int(v)] for v in vs])
    return D, src, {"fins": nfin, "decal_front": decal_front, "decal_back": decal_back}


def with_rest_tangents(D, meta, flip_sign=False):
    """Give the donor its own tangents = MikkTSpace (Blender) of its REST buffers; flip_sign stores the
    opposite bitangent sign (what a donor exported with the other winding convention carries)."""
    D = dict(D)
    for pre in ("", "lod1."):
        N = np.asarray(D[pre + "normals"], float)
        N /= np.linalg.norm(N, axis=1, keepdims=True)
        T, info = WR.tangents_blender(np.asarray(D[pre + "positions"], float), np.asarray(D[pre + "faces"]),
                                      np.asarray(D[pre + "uv0"], float), N)
        if T is None:
            raise RuntimeError(info)
        if flip_sign:
            T[:, 3] *= -1
        D[pre + "tangents"] = T.astype(np.float32)
    return D


def reframe_target(src, dst, frame):
    """Re-express a target NPZ (export_head.py format) in another frame preset (what --frame does)."""
    A, meta = MD.load_npz(src)
    fd0, fd1 = meta["frame"], MD.frame_def(frame)
    out = {}
    for k, v in A.items():
        if k.endswith(".positions") or k in ("landmarks.mp478", "features.positions"):
            out[k] = MD.blender_to_frame(MD.frame_to_blender(v, fd0), fd1)
        elif k.endswith(".normals"):
            out[k] = MD.dir_blender_to_frame(MD.dir_frame_to_blender(v, fd0), fd1)
        else:
            out[k] = v
    if fd0["det"] * fd1["det"] < 0:
        for k in list(out):
            if k.endswith(".faces"):
                out[k] = out[k][:, ::-1].copy()
            elif ".uv." in k:
                out[k] = out[k][:, ::-1].copy()
    meta = dict(meta, frame=fd1)
    MD.save_npz(dst, out, meta)
    return dst


def merge_hair_into_head(src, dst):
    """Target whose head part also contains the hair shell (as if R9_Hair were joined to R9_Head)."""
    T, tm = MD.load_npz(src)
    T = dict(T)
    nh, nhair = len(T["head.positions"]), len(T["hair.positions"])
    Fh = T["hair.faces"] + nh
    T["head.positions"] = np.concatenate([T["head.positions"], T["hair.positions"]])
    T["head.normals"] = np.concatenate([T["head.normals"], T["hair.normals"]])
    T["head.faces"] = np.concatenate([T["head.faces"], Fh]).astype(np.int32)
    for k in [k for k in T if k.startswith("head.uv.")]:
        T[k] = np.concatenate([T[k], np.zeros((len(Fh), 3, 2), np.float32)])
    T["head.region"] = np.concatenate([T["head.region"], np.full(nhair, FT.REGION_ID["scalp"], np.int8)])
    T["head.mh_index"] = np.concatenate([T["head.mh_index"], -np.ones(nhair, np.int32)])
    for k in ("head.cavity", "head.boundary"):
        if k in T:
            T[k] = np.concatenate([T[k], np.zeros(nhair, bool)])
    if "head.face_poly" in T:
        T["head.face_poly"] = np.concatenate([T["head.face_poly"], -np.ones(len(Fh), np.int32)])
    MD.save_npz(dst, T, tm)
    return dst


def donor_glb(ctx, dst, node_matrix=None, eyes_mesh=False):
    """The donor as a GLB: meshes 'head' (LOD0), optionally 'eyes' (two dense eyeballs, MORE vertices
    than LOD1, listed before it) and 'head_1' (LOD1 - deliberately not named 'LOD1'). node_matrix:
    a node transform (glTF column-major semantics as a 4x4 row matrix M: world = M @ local); the
    stored local positions are inv(M) @ world, so the world geometry is unchanged."""
    D, m = ctx.D, ctx.meta
    gl = MD.frame_def("gltf")
    M = np.eye(4) if node_matrix is None else np.asarray(node_matrix, float)
    Mi = np.linalg.inv(M)
    Rl = M[:3, :3] / np.cbrt(np.linalg.det(M[:3, :3]))
    meshes = []

    def mesh(name, Pb, Nb, F_bl, uv_bl, J=None, W=None):
        Pw = MD.blender_to_frame(Pb, gl)
        Nw = MD.dir_blender_to_frame(Nb, gl)
        Pl = Pw @ Mi[:3, :3].T + Mi[:3, 3]
        Nl = Nw @ np.linalg.inv(Rl).T
        meshes.append({"name": name, "positions": Pl, "normals": Nl, "faces": F_bl,
                       "uv0": MD.uv_from_bottom_left(uv_bl, "top-left"), "joints": J, "weights": W})
    for tag, pre in (("head", ""), ("head_1", "lod1.")):
        if tag == "head_1" and eyes_mesh:
            fp = ctx.feature_points_blender()
            eye = [p for p in submesh_parts(fp) if p[0].startswith("eye_")]
            Pe, Fe, UVe, Ne, off = [], [], [], [], 0
            for _, P, F, UV, N, _b in eye:
                P2, F2, UV2, N2 = _sphere(P.mean(0), 0.0115, 40, 24)     # dense: 2 x 1,025 vertices
                Pe.append(P2)
                Fe.append(F2 + off)
                UVe.append(UV2)
                Ne.append(N2)
                off += len(P2)
            mesh("eyes", np.concatenate(Pe), np.concatenate(Ne), np.concatenate(Fe), np.concatenate(UVe))
        Pb = MD.frame_to_blender(D[pre + "positions"], m["frame"])
        Nb = MD.dir_frame_to_blender(D[pre + "normals"], m["frame"])
        F = D[pre + "faces"]
        if m["frame"]["det"] * gl["det"] < 0:
            F = F[:, ::-1]
        mesh(tag, Pb, Nb, F, MD.uv_to_bottom_left(D[pre + "uv0"], m["uv_origin"]), D[pre + "joints"], D[pre + "weights"])
    GLB.write_glb(dst, meshes)
    if node_matrix is not None:
        js, binc = GLB.read_glb_raw(dst)
        for nd in js["nodes"]:
            nd["matrix"] = M.T.ravel().tolist()                       # glTF stores column-major
        open(dst, "wb").write(GLB._pack(js, binc))
    return dst


# ============================================================================= cases
def case_frames(ctx):
    """Mirrored frame (refused), mirrored + features picked on the mirrored display (refused by the
    skin check), cm declared as m + Y-up stored (works, same result), --align rigid with wrong units
    (refused), target in UE centimetres (same millimetre numbers)."""
    out, chk = {}, {}
    m2 = dict(ctx.meta, frame=mirrored_frame(ctx.meta["frame"]))
    dm = ctx.save_donor("mirrored", ctx.D, m2)
    _, err = ctx.wrap("mirror_true_labels", dm)
    chk["mirror_with_true_labels_refused"] = bool(err and "MIRRORED" in err)
    _, err2 = ctx.wrap("mirror_visual_labels", dm, ctx.save_features("swapped_sides", swap_sides(ctx.feats)))
    chk["mirror_with_visual_labels_refused"] = bool(err2 and "MIRRORED" in err2)
    out["refusals"] = {"true_labels": (err or "").splitlines()[:2], "visual_labels": (err2 or "").splitlines()[:1]}
    # cm declared as m AND Y-up stored, declared Z-up
    fd = ctx.meta["frame"]
    m3 = dict(ctx.meta, frame=MD.frame_def(axes=",".join(("-" if r[np.argmax(np.abs(r))] < 0 else "") + "xyz"[np.argmax(np.abs(r))]
                                                         for r in np.asarray(fd["A"])), scale=1.0))
    Rx = np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], float)
    du = ctx.save_donor("cm_as_m_yup", rotate_native(ctx.D, Rx), m3)
    base, _ = ctx.wrap("frames_base_fast", ctx.donor, extra=FAST)
    rep, err3 = ctx.wrap("cm_as_m_yup", du, extra=FAST)
    sb = ctx.semantic(base["outputs"]["npz"])
    su = ctx.semantic(rep["outputs"]["npz"]) if rep else None
    out["base_fast"], out["cm_as_m_yup"] = sb, su
    chk["cm_as_m_yup_same_result"] = bool(rep and abs(su["face_mean_mm"] - sb["face_mean_mm"]) < 0.01)
    chk["cm_as_m_units_warning"] = bool(rep and any("units look wrong" in w for w in rep["warnings"]))
    out["cm_as_m_yup_alignment"] = {k: rep["alignment"][k] for k in ("scale", "rotation_deg", "rms_mm")} if rep else err3
    _, err4 = ctx.wrap("units_rigid", ctx.save_donor("cm_as_m", ctx.D, m3), extra=["--align", "rigid"])
    chk["rigid_wrong_units_refused"] = bool(err4 and "units" in err4)
    # target in UE centimetres: every millimetre number must be the same
    # target in UE centimetres: read back in metres to rounding, and the millimetre report agrees (a
    # unit slip would be x10 / x1000; rounding-level input changes move a few discrete decisions of
    # the solver, so the wrap is compared to 0.01 mm, not bit for bit)
    tue = reframe_target(ctx.target, ctx.path("targets", "target_ue.npz"), "ue")
    Tb, Tu = WR.Target(ctx.target), WR.Target(tue)
    same_in = float(np.abs(Tb.P - Tu.P).max()), bool(np.array_equal(Tb.F, Tu.F))
    rue, _ = ctx.wrap("frames_target_ue_fast", ctx.donor, extra=FAST, target=tue)
    a = base["surface_distance_mm"]["donor_to_target"]["face"]
    b = rue["surface_distance_mm"]["donor_to_target"]["face"]
    su2 = ctx.semantic(rue["outputs"]["npz"])
    out["target_blender_vs_ue"] = {"max_position_diff_m": same_in[0], "faces_equal": same_in[1], "face_d2t": [a, b],
                                   "semantic": [sb, su2]}
    chk["target_ue_reads_back_in_metres"] = same_in[0] < 1e-12 and same_in[1]
    chk["target_ue_same_mm_report"] = bool(abs(a["mean"] - b["mean"]) < 0.01 and abs(a["p95"] - b["p95"]) < 0.02
                                           and abs(su2["face_mean_mm"] - sb["face_mean_mm"]) < 0.01)
    return {"checks": chk, **out}


def case_guess_yup(ctx):
    """--guess-features on a donor stored Y-up but declared Z-up (the guess assumed an upright donor)."""
    Rx = np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], float)
    lod, _ = WR.load_npz_donor(ctx.save_donor("yup", rotate_native(ctx.D, Rx)))
    T = WR.Target(ctx.target)
    t = time.time()
    feats, diag = WR.guess_features(lod, T)
    P = np.asarray(ctx.D["positions"], float) * 1000 / ctx.meta["frame"]["scale"]
    err = {k: float(np.linalg.norm(P[feats[k]] - P[ctx.feats[k]])) for k in feats if k in ctx.feats}
    basic = float(np.mean([err[k] for k in FT.BASIC if k in err]))
    return {"checks": {"guess_on_yup_donor_basic_mean_lt_6mm": basic < 6.0}, "basic_mean_mm": basic,
            "feature_error_mm": err, "seconds": round(time.time() - t, 1), "diagnostics": diag}


def case_features(ctx):
    """2-5 mm feature noise (accuracy), two swapped names (outliers rejected), 1-based ids (refused)."""
    out, chk = {}, {}
    Pmm = np.asarray(ctx.D["positions"], float) * 1000 / ctx.meta["frame"]["scale"]
    f, md = perturb_features(Pmm, ctx.feats, 2.0, 5.0, 0)
    rep, _ = ctx.wrap("features_noise", ctx.donor, ctx.save_features("noise_2_5mm", f))
    out["noise"] = {"mean_feature_error_mm": md, **summary(rep), "semantic": ctx.semantic(rep["outputs"]["npz"]),
                    "lod1_flip_repair": rep["lod1"]["flip_repair"]}
    chk["noise_semantic_face_mean_lt_3mm"] = out["noise"]["semantic"]["face_mean_mm"] < 3.0
    chk["noise_semantic_face_p95_lt_6mm"] = out["noise"]["semantic"]["face_p95_mm"] < 6.0
    chk["noise_no_flips_lod0_lod1"] = out["noise"]["flips_lod0"] == 0 and out["noise"]["flips_lod1"] == 0
    g = dict(ctx.feats)
    g["chin"], g["nose_tip"] = ctx.feats["nose_tip"], ctx.feats["chin"]
    rep, _ = ctx.wrap("features_swapped_names", ctx.donor, ctx.save_features("swap_chin_nose", g))
    out["swapped_names"] = {**summary(rep), "semantic": ctx.semantic(rep["outputs"]["npz"])}
    chk["swapped_names_rejected"] = set(rep["alignment"]["rejected_outliers_mm"]) == {"chin", "nose_tip"}
    chk["swapped_names_semantic_face_mean_lt_2mm"] = out["swapped_names"]["semantic"]["face_mean_mm"] < 2.0
    chk["swapped_names_no_flips"] = out["swapped_names"]["flips_lod0"] == 0 and out["swapped_names"]["flips_lod1"] == 0
    _, err = ctx.wrap("features_one_based", ctx.donor, ctx.save_features("one_based", {k: v + 1 for k, v in ctx.feats.items()}))
    chk["one_based_ids_refused"] = bool(err and "outliers" in err)
    out["one_based_refusal"] = (err or "").splitlines()[:2]
    return {"checks": chk, **out}


def case_submeshes(ctx):
    """Eyeballs, teeth and a double-sided eyelash card as separate components (LOD0 + LOD1), UVs over
    the skin's texture space. Before the fix: 'Factor is exactly singular'."""
    D = dict(ctx.D)
    fp = ctx.feature_points_blender()
    n0, tags0 = append_parts(D, ctx.meta, "", submesh_parts(fp))
    _, tags1 = append_parts(D, ctx.meta, "lod1.", submesh_parts(fp, coarse=True))
    dn = ctx.save_donor("submeshes", D)
    base, _ = ctx.wrap("sub_base_fast", ctx.donor, extra=FAST)
    rep, err = ctx.wrap("submeshes", dn, extra=FAST)
    chk = {"wrapped": rep is not None}
    if rep is None:
        return {"checks": chk, "error": err}
    R, _ = MD.load_npz(rep["outputs"]["npz"])
    P0 = np.asarray(D["positions"], float)
    u = 1000 / ctx.meta["frame"]["scale"]
    parts = {}
    for nm, off, k in tags0:
        sl = slice(off, off + k)
        d = np.linalg.norm(R["lod0.positions"][sl] - P0[sl], axis=1) * u
        # rigid motion: pairwise distances inside the part are preserved
        A, B = P0[sl], R["lod0.positions"][sl]
        dA = np.linalg.norm(A[:, None] - A[None], axis=2)
        dB = np.linalg.norm(B[:, None] - B[None], axis=2)
        nn = R["lod0.normals"][sl].astype(float)
        n_in = np.asarray(D["normals"][sl], float)
        parts[nm] = {"mean_displacement_mm": float(d.mean()), "rigidity_error_mm": float(np.abs(dA - dB).max() * u),
                     "normal_vs_input_min_dot": float((nn * n_in).sum(1).min())}
    R1 = R["lod1.positions"]
    P1 = np.asarray(D["lod1.positions"], float)
    for nm, off, k in tags1:
        d1 = float(np.linalg.norm(R1[off:off + k] - P1[off:off + k], axis=1).mean() * u)
        parts[nm]["lod1_mean_displacement_mm"] = d1
    sb = ctx.semantic(base["outputs"]["npz"])
    ss = ctx.semantic(rep["outputs"]["npz"], np.r_[np.arange(n0), -np.ones(len(P0) - n0, int)])
    chk["skin_result_unchanged"] = abs(ss["face_mean_mm"] - sb["face_mean_mm"]) < 0.01
    chk["submeshes_moved_rigidly"] = all(p["rigidity_error_mm"] < 1e-6 for p in parts.values())
    chk["submeshes_follow_skin_0.3_to_5mm"] = all(0.3 < p["mean_displacement_mm"] < 5 for p in parts.values())
    chk["lod1_submeshes_follow_lod0"] = all(abs(p["lod1_mean_displacement_mm"] - p["mean_displacement_mm"]) < 0.5
                                            for p in parts.values())
    chk["double_sided_card_normals_kept"] = parts["lash_R"]["normal_vs_input_min_dot"] > 0.99
    chk["eye_normals_rotated_not_recomputed"] = parts["eye_R"]["normal_vs_input_min_dot"] > 0.99
    chk["five_submeshes_found"] = len(rep["submeshes"]["components"]) == 5
    # texture: sub-mesh UVs lie over the skin's texture space; the default bake must ignore them,
    # --bake-submeshes must show the conflict
    tex = {}
    for nm, extra in (("default", []), ("bake_submeshes", ["--bake-submeshes"])):
        tr = TT.run(TT.build_argparser().parse_args(
            ["--wrap", rep["outputs"]["npz"], "--target", ctx.target, "--albedo", checker_albedo(ctx),
             "--out-dir", ctx.path("tex", "sub_" + nm, "x")[:-2], "--sizes", "512", "--normal", ""] + extra))
        tex[nm] = {"uv_overlap": tr["bake"]["uv_overlap"], "skipped_submesh_triangles": tr["inputs"]["skipped_submesh_triangles"],
                   "face_surface_dE00_mean": tr["consistency_check"]["face_surface/albedo_512"]["dE00_mean"]}
    chk["bake_ignores_submesh_uvs"] = tex["default"]["uv_overlap"]["conflicting_texels"] == 0 and \
        tex["default"]["skipped_submesh_triangles"] > 0
    chk["overlap_detected_when_baked"] = tex["bake_submeshes"]["uv_overlap"]["conflicting_texels"] > 100
    return {"checks": chk, "parts": parts, "semantic_base": sb, "semantic": ss, "texture": tex,
            "submesh_report": rep["submeshes"], "lod1_groups": rep["lod1"]["embedding"]}


def checker_albedo(ctx, n=1024, cell=16):
    p = ctx.path("tex", "checker_albedo.png")
    if not os.path.exists(p):
        yy, xx = np.mgrid[0:n, 0:n]
        chk = (((xx // cell) + (yy // cell)) % 2).astype(float)
        TT.save_png(p, np.stack([0.25 + 0.5 * chk, 0.35 + 0.3 * (xx / n), 0.3 + 0.4 * (yy / n)], 2))
    return p


def near_midline_colour(ctx, wrap_npz, baked_png, albedo_png, band_mm=4.0, n=20000):
    """dE00 between our texture and the baked donor texture at face points within band_mm of the
    face midline (where a midline UV seam is)."""
    src = TT.HDSource(ctx.target)
    R, rm = MD.load_npz(wrap_npz)
    lat, up, fwd = FT.head_axes(src.L)
    mid = FT.mid_lateral(src.L, lat)
    face = np.isin(src.region, [FT.REGION_ID["face"]])
    near = face & (np.abs(src.P @ lat - mid) < band_mm / 1000)
    Ps, tri, bary = G.sample_surface(src.P, src.F, n, 3, tri_mask=near[src.F].all(1).astype(float))
    alb = TT.load_image(albedo_png, True)
    ref = TT.lin_to_srgb(TT.bilinear(alb, src.uv_at(tri, bary)))
    F = R["lod0.faces"].astype(np.int64)
    keep = ~R["lod0.submesh"][F].any(1)
    F = F[keep]
    dq = G.TriMeshQuery(R["lod0.work_positions"], F)
    c = dq.closest(Ps)
    uv = MD.uv_to_bottom_left(R["lod0.uv0"], rm["uv_origin"])
    uvp = (uv[F[c["tri"]]] * c["bary"][:, :, None]).sum(1)
    got = TT.lin_to_srgb(TT.bilinear(TT.load_image(baked_png, True), uvp))
    de = TT.delta_e2000(TT.srgb_to_lab(ref), TT.srgb_to_lab(got))
    return {"n": int(len(Ps)), "dE00_mean": float(de.mean()), "dE00_p95": float(np.percentile(de, 95)),
            "psnr_db": TT.psnr(ref, got)}


def case_uv_seam(ctx):
    """A UV seam through the middle of the face (LOD0)."""
    D, src, ncut = split_face_uv(ctx.D, ctx.meta, ctx.feats)
    dn = ctx.save_donor("uv_seam", D)
    base, _ = ctx.wrap("seam_base_fast", ctx.donor, extra=FAST)
    rep, _ = ctx.wrap("uv_seam", dn, extra=FAST)
    R, _ = MD.load_npz(rep["outputs"]["npz"])
    P1 = R["lod0.positions"]
    dup = np.arange(len(ctx.D["positions"]), len(P1))
    sb, ss = ctx.semantic(base["outputs"]["npz"]), ctx.semantic(rep["outputs"]["npz"], src)
    alb = checker_albedo(ctx)
    col = {}
    for nm, r in (("base", base), ("seam", rep)):
        od = ctx.path("tex", "seam_" + nm, "x")[:-2]
        tr = TT.run(TT.build_argparser().parse_args(["--wrap", r["outputs"]["npz"], "--target", ctx.target, "--albedo", alb,
                                                     "--out-dir", od, "--sizes", "1024", "--normal", ""]))
        col[nm] = {"near_midline": near_midline_colour(ctx, r["outputs"]["npz"], os.path.join(od, "donor_albedo_1024.png"), alb),
                   "whole_face_dE00_mean": tr["consistency_check"]["face_surface/albedo_1024"]["dE00_mean"],
                   "uv_overlap": tr["bake"]["uv_overlap"]["conflicting_texels"]}
        col[nm]["midline_to_face_ratio"] = col[nm]["near_midline"]["dE00_mean"] / col[nm]["whole_face_dE00_mean"]
    chk = {"seam_duplicates_bit_identical": bool(np.array_equal(P1[dup], P1[src[dup]])),
           "uv_kept": bool(np.array_equal(R["lod0.uv0"], D["uv0"])),
           "semantic_unchanged": abs(ss["face_mean_mm"] - sb["face_mean_mm"]) < 0.05,
           "no_uv_overlap": col["seam"]["uv_overlap"] == 0,
           # the cut halves were shrunk to 90 % (fewer texels for the whole face): judge the seam by the
           # near-midline error RELATIVE to the whole-face error (a mapping error at the seam would be ~x5)
           "seam_adds_no_local_error_ratio_within_20pct": col["seam"]["midline_to_face_ratio"] <
           1.2 * col["base"]["midline_to_face_ratio"]}
    return {"checks": chk, "cut_vertices": ncut, "semantic_base": sb, "semantic": ss, "colour": col}


def case_nonmanifold(ctx):
    """Fins (edges with 3 triangles) + a double-sided scalp decal, donor without normals."""
    D, src, info = add_nonmanifold(ctx.D, ctx.meta, ctx.feats)
    dn = ctx.save_donor("nonmanifold", D)
    base, _ = ctx.wrap("nm_base_fast", ctx.donor, extra=FAST)
    rep, err = ctx.wrap("nonmanifold", dn, extra=FAST)
    chk = {"wrapped": rep is not None}
    if rep is None:
        return {"checks": chk, "error": err}
    R, _ = MD.load_npz(rep["outputs"]["npz"])
    N = R["lod0.normals"].astype(float)
    front, back = info["decal_front"], info["decal_back"]
    dots = (N[front] * N[back]).sum(1)
    P = np.asarray(D["positions"], float)
    Pb = MD.frame_to_blender(P, ctx.meta["frame"])
    c = Pb.mean(0)
    outward = ((MD.frame_to_blender(R["lod0.positions"][front], ctx.meta["frame"]) - c) *
               MD.dir_frame_to_blender(N[front], ctx.meta["frame"])).sum(1)
    sb, ss = ctx.semantic(base["outputs"]["npz"]), ctx.semantic(rep["outputs"]["npz"], src)
    chk.update({"nonmanifold_edges_reported": rep["lod0_buffers"]["nonmanifold_edges"] > 0,
                # the front copy is part of the skin (welded smooth normal over all its faces), the back copy
                # only has the decal's own faces: opposite to within a few degrees at the decal border
                "decal_back_normals_opposite_front": bool((dots < -0.95).all()),
                "decal_front_normals_outward": bool((outward > 0).all()),
                "normals_finite_unit": bool(np.allclose(np.linalg.norm(N, axis=1), 1, atol=1e-3)),
                "no_flips": rep["mesh_checks"]["flipped_triangles"] == 0,
                "semantic_unchanged": abs(ss["face_mean_mm"] - sb["face_mean_mm"]) < 0.05})
    return {"checks": chk, "decal_front_back_dot_max": float(dots.max()), "semantic_base": sb, "semantic": ss,
            "buffers": {k: rep["lod0_buffers"][k] for k in ("nonmanifold_edges", "duplicate_triangles_after_weld",
                                                            "border_loops", "normals")}}


def case_coarse_lod1(ctx):
    """A 350-vertex LOD1 (fake donor rebuilt with --lod1-verts 350; LOD0 is identical)."""
    dd = ctx.path("donor_coarse", "x")[:-2]
    if not os.path.exists(os.path.join(dd, "donor.npz")):
        sh(["blender", "-b", "--python", os.path.join(HERE, "make_fake_donor.py"), "--", "--out", dd, "--lod1-verts", "350"],
           ctx.logf)
    rep, _ = ctx.wrap("coarse_lod1", os.path.join(dd, "donor.npz"), os.path.join(dd, "donor_features_gt.json"))
    l1 = rep["lod1"]
    rest = l1["rest_surface_to_rest_lod0_mm"]
    now = l1["surface_to_wrapped_lod0_mm"]
    chk = {"lod1_no_flips": l1["mesh_checks"]["flipped_triangles"] == 0,
           "lod1_p95_within_1mm_of_rest_decimation_error": now["p95"] - rest["p95"] < 1.0,
           "lod1_mean_within_0.5mm_of_rest": now["mean"] - rest["mean"] < 0.5}
    return {"checks": chk, "lod1_verts": int(len(np.load(os.path.join(dd, "donor.npz"))["lod1.positions"])),
            "rest": rest, "wrapped": now, "edge_ratio": l1["mesh_checks"]["edge_ratio"], "flip_repair": l1["flip_repair"]}


def case_hair(ctx):
    """Target with the hair shell: kept as its own part it is ignored; merged into the head part it
    must be flagged (the wrap would pull the donor scalp onto it)."""
    hd = ctx.path("hair_target", "x")[:-2]
    tn = os.path.join(hd, "target_hair.npz")
    if not os.path.exists(tn):
        blend = os.path.join(hd, "r9_hair.blend")
        sh(["blender", "-b", "--python", os.path.join(REPO, "tools/model/blender_build.py"), "--",
            "--params", os.path.join(REPO, "model/source/r9_params.json"), "--out", blend], ctx.logf)
        sh(["blender", "-b", blend, "--python", os.path.join(HERE, "export_head.py"), "--", "--out", tn], ctx.logf)
    A, _ = MD.load_npz(tn)
    has_hair = "hair.positions" in A
    T = WR.Target(tn)
    sep = T.layer_check()
    mg = WR.Target(merge_hair_into_head(tn, os.path.join(hd, "target_hair_merged.npz"))).layer_check()
    chk = {"target_has_hair_part": has_hair, "separate_hair_ignored": sep["double_layer_fraction"] < 0.005 and
           len(T.P) == len(A["head.positions"]), "merged_hair_flagged": mg["double_layer_fraction"] > 0.02}
    return {"checks": chk, "separate": sep, "merged": mg, "hair_vertices": int(len(A.get("hair.positions", [])))}


def case_tangents(ctx):
    """Donor tangents = MikkTSpace of the rest buffers -> 'match'; the same with the opposite sign
    convention -> detected and the output sign follows the donor. Output tangents are recomputed on
    the wrapped buffers (equal to an independent MikkTSpace pass, different from the rest ones)."""
    out, chk = {}, {}
    for nm, flip in (("match", False), ("flipped", True)):
        dn = ctx.save_donor("tangents_" + nm, with_rest_tangents(ctx.D, ctx.meta, flip))
        rep, _ = ctx.wrap("tangents_" + nm, dn, extra=FAST + ["--tangents", "blender"])
        t = rep["lod0_buffers"]["tangents"]
        R, _ = MD.load_npz(rep["outputs"]["npz"])
        Tn = R["lod0.tangents"].astype(float)
        Ti, _ = WR.tangents_blender(R["lod0.positions"], R["lod0.faces"], R["lod0.uv0"].astype(float),
                                    R["lod0.normals"].astype(float))
        sgn = -1 if flip else 1
        moved = ~R["lod0.unmoved"]
        Tr = np.load(dn)["tangents"].astype(float)
        out[nm] = {"check": t["donor_tangent_check"], "independent_xyz_max_deg": float(np.degrees(np.arccos(np.clip(
            (Tn[:, :3] * Ti[:, :3]).sum(1), -1, 1))).max()), "sign_follows_donor": bool(np.all(Tn[:, 3] == sgn * Ti[:, 3])),
            "differs_from_rest_p50_deg": float(np.median(np.degrees(np.arccos(np.clip((Tn[moved, :3] * Tr[moved, :3]).sum(1), -1, 1)))))}
    chk["rest_tangents_verdict_match"] = out["match"]["check"]["verdict"] == "match"
    chk["flipped_convention_detected"] = out["flipped"]["check"]["verdict"] == "sign_flipped"
    chk["output_sign_follows_donor"] = out["match"]["sign_follows_donor"] and out["flipped"]["sign_follows_donor"]
    chk["recomputed_equals_independent_mikktspace"] = out["match"]["independent_xyz_max_deg"] < 0.05   # float32 i/o
    chk["recomputed_differs_from_rest"] = out["match"]["differs_from_rest_p50_deg"] > 0.05
    return {"checks": chk, **out}


def case_glb(ctx):
    """GLB donor whose node scales (cm -> m) and rotates (Z-up -> Y-up) the mesh, with an 'eyes' mesh
    larger than LOD1 and LOD1 not named LOD1: same world result as the plain GLB, LOD1 found."""
    import synthetic as SY
    plain = SY.donor_npz_to_glb(ctx.donor, ctx.path("glb", "donor_plain.glb"))
    a = np.radians(90)
    M = np.eye(4)
    M[:3, :3] = 0.01 * np.array([[1, 0, 0], [0, np.cos(a), -np.sin(a)], [0, np.sin(a), np.cos(a)]])
    M[:3, 3] = [0.02, 1.5, -0.01]
    tricky = donor_glb(ctx, ctx.path("glb", "donor_node_eyes.glb"), node_matrix=M, eyes_mesh=True)
    r0, _ = ctx.wrap("glb_plain", plain, extra=FAST)
    r1, err = ctx.wrap("glb_node_eyes", tricky, extra=FAST)
    chk = {"wrapped": r1 is not None}
    if r1 is None:
        return {"checks": chk, "error": err}
    g0, g1 = GLB.read_glb(r0["outputs"]["glb"]), GLB.read_glb(r1["outputs"]["glb"])
    m0 = {m["name"]: m for m in g0["meshes"]}
    m1 = {m["name"]: m for m in g1["meshes"]}

    def world(m):
        P = m["primitives"][0]["attributes"]["POSITION"].astype(float)
        return P @ m["world"][:3, :3].T + m["world"][:3, 3]
    # the node GLB stores float32(inv(M) @ world): the rest geometry may differ by ~1e-7 m, so the
    # frame handling must reproduce the rest pose to that level; the wrapped result is compared
    # statistically (a few discrete decisions - target-border rejection at the bottom of the neck -
    # can flip for such tiny input changes)
    a, _ = MD.load_npz(r0["outputs"]["npz"])
    b, _ = MD.load_npz(r1["outputs"]["npz"])
    rest = float(np.abs(a["lod0.rest_work_positions"] - b["lod0.rest_work_positions"]).max())
    d = np.linalg.norm(a["lod0.work_positions"] - b["lod0.work_positions"], axis=1) * 1000
    face = a["lod0.region_label"] == FT.REGION_ID["face"]
    dw = np.linalg.norm(world(m0["lod0"]) - world(m1["head"]), axis=1) * 1000
    eyes_in = GLB.read_glb(tricky)["meshes"][1]["primitives"][0]["attributes"]["POSITION"]
    chk.update({"lod1_pick_is_head_1": "head_1" in str(r1["inputs"]["lod1_pick"]),
                "rest_pose_identical_1e-6m": rest < 1e-6,
                "wrapped_p99_lt_0.1mm_face_max_lt_0.1mm": bool(np.percentile(d, 99) < 0.1 and d[face].max() < 0.1),
                "glb_world_positions_p99_lt_0.1mm": bool(np.percentile(dw, 99) < 0.1),
                "eyes_mesh_untouched": bool(np.array_equal(m1["eyes"]["primitives"][0]["attributes"]["POSITION"], eyes_in))})
    return {"checks": chk, "lod1_pick": r1["inputs"]["lod1_pick"], "rest_max_diff_m": rest,
            "wrapped_diff_mm": {"p50": float(np.median(d)), "p99": float(np.percentile(d, 99)), "max": float(d.max()),
                                "face_max": float(d[face].max())},
            "glb_world_diff_mm": {"p99": float(np.percentile(dw, 99)), "max": float(dw.max())}}


CASES = {"frames": case_frames, "guess_yup": case_guess_yup, "features": case_features, "submeshes": case_submeshes,
         "uv_seam": case_uv_seam, "nonmanifold": case_nonmanifold, "coarse_lod1": case_coarse_lod1,
         "hair": case_hair, "tangents": case_tangents, "glb": case_glb}


def run_suite(work, base=None, cases=None, log=print):
    ctx = Ctx(work, base, log)
    S = {}
    for name in (cases or list(CASES)):
        t = time.time()
        S[name] = CASES[name](ctx)
        S[name]["seconds"] = round(time.time() - t, 1)
        log(f"[adv] case {name}: {S[name]['checks']} ({S[name]['seconds']} s)")
        json.dump(S, open(os.path.join(work, "adversarial_summary.json"), "w"), indent=1, default=float)
    return S


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", required=True)
    ap.add_argument("--base", help="a synthetic.py work dir to reuse (target/target.npz, donor/)")
    ap.add_argument("--cases", help="comma list, default all: " + ",".join(CASES))
    a = ap.parse_args()
    S = run_suite(a.work, a.base, a.cases.split(",") if a.cases else None)
    bad = [(c, k) for c, v in S.items() for k, ok in v["checks"].items() if not ok]
    print("FAILED:" if bad else "all checks passed", bad if bad else "")
