#!/usr/bin/env python3
"""Photo-guided head/face fitting on the MakeHuman morph basis.

Model
  V(w) = V_macro + sum_k w_k * D_k        (D_k = MakeHuman face/head target offsets; linear)
  L(w) = B @ V(w)                         (B = barycentric landmark correspondences, corr.json)
  per photo p: camera R_p, t_p, f_p (pinhole, principal point = image centre)
  minimise  sum_p sum_i c_i * || proj_p(L_i(w)) - obs_pi ||^2 / s_p^2  +  lam * ||w||^2
with w_k >= 0 for every target side (each decr|incr modifier is split into two
non-negative variables) and upper bound 1. Left/right modifier pairs are tied (one
variable) unless --asym. Contour (face-oval) landmarks slide with view angle so they get
a lower confidence c_i. Mouth landmarks are dropped on photos whose MediaPipe blendshapes
say the mouth is open/smiling (expression is not identity).

Inputs
  --corr       corr.json from tools/face/blender_correspondence.py
  --params     base params JSON (macros already chosen; modifiers in it are the start point)
  --photos     one or more landmark JSON files from tools/face/landmarks.py
  --manual     optional JSON of hand-annotated points for profile photos:
               {"image": {"w":..,"h":..,"points": {"<mediapipe landmark id>": [x, y], ...}}}
Outputs
  --out        params JSON with fitted modifiers merged in
  --report     JSON report: per-photo reprojection RMS (px and % of inter-ocular), weights
"""
import argparse
import json
import os
import sys

import cv2
import numpy as np
from scipy.optimize import least_squares, lsq_linear

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "model"))
import mh_engine as E  # noqa: E402

FACE_GROUPS = ["head", "forehead", "eyebrows", "eyes", "nose", "mouth", "ears", "chin", "cheek"]
# MediaPipe FACE_OVAL contour indices (silhouette-dependent)
FACE_OVAL = [10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365, 379, 378, 400, 377,
             152, 148, 176, 149, 150, 136, 172, 58, 132, 93, 234, 127, 162, 21, 54, 103, 67, 109]
LIPS = sorted({61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291, 185, 40, 39, 37, 0, 267, 269, 270, 409,
               78, 95, 88, 178, 87, 14, 317, 402, 318, 324, 308, 191, 80, 81, 82, 13, 312, 311, 310, 415})
EYE_L, EYE_R = 473, 468  # iris centres (refined landmarks)
D_MIN, D_MAX = 15.0, 400.0  # camera distance bounds, decimetres (1.5 m .. 40 m)


# Identity features that face landmarks actually constrain (global skull-shape morphs are
# left to profile/silhouette work because the camera absorbs them and they distort the skull).
IDENTITY = ["nose-scale", "nose-nostrils-width", "nose-point-width", "nose-width", "nose-trans", "nose-point-",
            "nose-flaring", "nose-volume", "nose-curve", "nose-hump", "nose-base",
            "mouth-scale", "mouth-trans", "mouth-upperlip-height", "mouth-lowerlip-height",
            "mouth-upperlip-volume", "mouth-lowerlip-volume", "mouth-upperlip-width", "mouth-lowerlip-width",
            "mouth-angles", "chin-width", "chin-height", "chin-prominent", "chin-jaw-drop", "chin-bones",
            "chin-prognathism", "cheek-volume", "cheek-bones", "cheek-trans", "eye-scale", "eye-trans",
            "eye-height1", "eye-height2", "eye-height3", "eyebrows-trans", "eyebrows-angle", "forehead-scale-vert",
            "head-fat", "head-angle"]


def variables(asym=False, groups=FACE_GROUPS, exclude=(), include=None):
    """List of (var_name, [(modifier_name, sign)]) — sign=+1 incr side, -1 decr side, 0 single target."""
    tab = E.modifier_table()
    out, seen = [], set()
    for name, (group, target, lo, hi) in sorted(tab.items()):
        if group not in groups or any(x in name for x in exclude):
            continue
        if include is not None and not any(x in name for x in include):
            continue
        key = name
        mods = [name]
        if not asym and (target.startswith("r-") or target.startswith("l-")):
            twin = name.replace(f"{group}/{target[:2]}", f"{group}/{'l-' if target.startswith('r-') else 'r-'}", 1)
            if twin in tab:
                key = name.replace(f"{group}/{target[:2]}", f"{group}/", 1)
                mods = sorted({name, twin})
        if key in seen:
            continue
        seen.add(key)
        if lo is None and hi is None:
            out.append((key, [(m, 0) for m in mods]))
        else:
            out.append((key + ":+", [(m, +1) for m in mods]))
            out.append((key + ":-", [(m, -1) for m in mods]))
    return out


def basis(params, vars_, corr, base):
    """Landmark-space basis: L0 (478,3) and dL (K,478,3) at the given macro state."""
    v0, _ = E.build(params, base)
    Bv = np.array([c["v"] for c in corr])
    Bw = np.array([c["w"] for c in corr])

    def lm(V):
        return (V[Bv] * Bw[..., None]).sum(1)
    L0 = lm(v0)
    dL = np.zeros((len(vars_), len(corr), 3))
    factors = E.macro_factors(params.get("macro", {}))
    for k, (_, mods) in enumerate(vars_):
        D = np.zeros_like(v0)
        for m, sgn in mods:
            val = 1.0 if sgn >= 0 else -1.0
            for rel, w in E.regular_target_weights({m: val}, factors).items():
                idx, off = E.load_target(os.path.join(E.TARGETS, rel))
                D[idx] += w * off
        dL[k] = lm(D)
    return L0, dL


def rodrigues(r):
    return cv2.Rodrigues(np.asarray(r, float))[0]


def project(P, cam, w, h):
    R = rodrigues(cam[:3])
    X = P @ R.T + cam[3:6]
    f = cam[6]
    return np.stack([f * X[:, 0] / X[:, 2] + w / 2, f * X[:, 1] / X[:, 2] + h / 2], 1)


def init_camera(P, uv, w, h, f=None):
    f = f or 2.5 * max(w, h)
    K = np.array([[f, 0, w / 2], [0, f, h / 2], [0, 0, 1]], float)
    ok, rvec, tvec = cv2.solvePnP(P.astype(np.float64), uv.astype(np.float64), K, None,
                                  flags=cv2.SOLVEPNP_ITERATIVE)
    return np.concatenate([rvec.ravel(), tvec.ravel(), [f]])


def load_obs(photo_files, manual_file=None, drop_mouth_thresh=0.35):
    obs = []
    for pf in photo_files:
        for img, d in json.load(open(pf)).items():
            if not d:
                continue
            L = np.array(d["landmarks"])[:, :2]
            conf = np.ones(len(L))
            conf[FACE_OVAL] = 0.35
            bs = d.get("blendshapes", {})
            expr = max(bs.get("jawOpen", 0), bs.get("mouthSmileLeft", 0), bs.get("mouthSmileRight", 0))
            if expr > drop_mouth_thresh:
                conf[LIPS] = 0.0
            obs.append({"image": img, "w": d["w"], "h": d["h"], "uv": L, "conf": conf, "expr": expr})
    if manual_file:
        for img, d in json.load(open(manual_file)).items():
            ids = [int(k) for k in d["points"]]
            uv = np.full((478, 2), np.nan)
            conf = np.zeros(478)
            for k, xy in d["points"].items():
                uv[int(k)] = xy
                conf[int(k)] = d.get("confidence", 0.7)
            obs.append({"image": img, "w": d["w"], "h": d["h"], "uv": uv, "conf": conf, "expr": 0.0,
                        "manual_ids": ids, "f": d.get("focal")})
    return obs


def fit(params, corr, obs, vars_, lam=0.5, fix_focal=False, iters=3, method="alt", alt_iters=60, damping=1.0):
    base = E.load_obj()
    L0, dL = basis(params, vars_, corr, base)
    K = len(vars_)
    w = np.zeros(K)
    cams = []
    for o in obs:
        m = o["conf"] > 0
        cams.append(init_camera(L0[m], o["uv"][m], o["w"], o["h"], o.get("f")))
    cams = np.array(cams)

    def unpack(x):
        return x[:K], x[K:].reshape(len(obs), 7)

    masks = [o["conf"] > 0 for o in obs]
    scales = []
    for o, m in zip(obs, masks):
        iod = np.linalg.norm(o["uv"][EYE_L] - o["uv"][EYE_R]) if not np.isnan(o["uv"][EYE_L]).any() else o["w"] / 6
        scales.append(100.0 * np.sqrt(o["conf"][m]) / iod)

    def resid(x):
        w, cs = unpack(x)
        L = L0 + np.tensordot(w, dL, 1)
        rs = []
        for o, c, m, s in zip(obs, cs, masks, scales):
            pr = project(L[m], c, o["w"], o["h"])
            rs.append(((pr - o["uv"][m]) * s[:, None]).ravel())
        rs.append(np.sqrt(lam) * w)
        return np.concatenate(rs)

    def jac(x):
        """Analytic for morph weights, translation and focal; finite differences for rotation."""
        w, cs = unpack(x)
        L = L0 + np.tensordot(w, dL, 1)
        rows = []
        J = np.zeros((sum(2 * int(m.sum()) for m in masks) + K, len(x)))
        r0 = 0
        for p, (o, c, m, s) in enumerate(zip(obs, cs, masks, scales)):
            n = int(m.sum())
            R = rodrigues(c[:3])
            X = L[m] @ R.T + c[3:6]
            f = c[6]
            Z = X[:, 2]
            # d(u,v)/dX : (n, 2, 3)
            dP = np.zeros((n, 2, 3))
            dP[:, 0, 0] = f / Z
            dP[:, 0, 2] = -f * X[:, 0] / Z ** 2
            dP[:, 1, 1] = f / Z
            dP[:, 1, 2] = -f * X[:, 1] / Z ** 2
            dP *= s[:, None, None]
            # morph weights: dX/dw_k = R dL_k
            dLm = dL[:, m, :] @ R.T                      # (K, n, 3)
            Jw = np.einsum("nij,knj->nik", dP, dLm)      # (n, 2, K)
            J[r0:r0 + 2 * n, :K] = Jw.reshape(2 * n, K)
            col = K + 7 * p
            J[r0:r0 + 2 * n, col + 3:col + 6] = dP.reshape(2 * n, 3)
            J[r0:r0 + 2 * n, col + 6] = (np.stack([X[:, 0] / Z, X[:, 1] / Z], 1) * s[:, None]).ravel()
            base_uv = project(L[m], c, o["w"], o["h"])
            for j in range(3):
                cc = c.copy()
                cc[j] += 1e-6
                J[r0:r0 + 2 * n, col + j] = (((project(L[m], cc, o["w"], o["h"]) - base_uv) / 1e-6)
                                             * s[:, None]).ravel()
            r0 += 2 * n
        J[r0:r0 + K, :K] = np.sqrt(lam) * np.eye(K)
        return J

    # Physically plausible cameras: subject 1.5-40 m away (press photos are long-lens shots;
    # left free, focal length and distance run off together towards the orthographic limit or
    # collapse into a wide-angle close-up that "explains" shape errors with perspective).
    lb = np.concatenate([np.zeros(K), np.tile([-np.inf] * 5 + [D_MIN, 100.0], len(obs))])
    ub = np.concatenate([np.ones(K), np.tile([np.inf] * 5 + [D_MAX, np.inf], len(obs))])
    for i, o in enumerate(obs):
        c = cams[i]
        z = np.clip(c[5], D_MIN * 1.01, D_MAX * 0.99)
        if z != c[5]:   # rescale focal so the projected size is unchanged
            c[6] *= z / c[5]
            c[3:5] *= z / c[5]
            c[5] = z
    x = np.concatenate([w, cams.ravel()])
    if fix_focal:
        for i in range(len(obs)):
            j = K + 7 * i + 6
            lb[j], ub[j] = x[j] - 1e-6, x[j] + 1e-6
    r0 = resid(x)
    if method == "joint":
        for _ in range(iters):
            res = least_squares(resid, np.clip(x, lb + 1e-9, ub - 1e-9), jac=jac, bounds=(lb, ub),
                                x_scale="jac", max_nfev=2000)
            x = res.x
    else:
        # Alternating Gauss-Newton: (1) refine each camera alone (7 params, small problem);
        # (2) linearise in the morph weights and solve a bounded, damped linear least squares.
        x = x.copy()
        n_data = len(r0) - K
        prev = np.inf
        for it in range(alt_iters):
            for p in range(len(obs)):
                sl = slice(K + 7 * p, K + 7 * p + 7)

                def rc(cvec, p=p, sl=sl):
                    xx = x.copy()
                    xx[sl] = cvec
                    return resid(xx)[:n_data]
                res = least_squares(rc, np.clip(x[sl], lb[sl] + 1e-9, ub[sl] - 1e-9),
                                    bounds=(lb[sl], ub[sl]), x_scale="jac", max_nfev=200)
                x[sl] = res.x
            r = resid(x)[:n_data]
            Jw = jac(x)[:n_data, :K]
            mu = damping
            A = np.vstack([Jw, np.sqrt(lam) * np.eye(K), np.sqrt(mu) * np.eye(K)])
            b = np.concatenate([Jw @ x[:K] - r, np.zeros(K), np.sqrt(mu) * x[:K]])
            Q, Rm = np.linalg.qr(A)           # same minimiser, K rows instead of thousands
            sol = lsq_linear(Rm, Q.T @ b, bounds=(0.0, 1.0), method="bvls")
            x[:K] = sol.x
            cost = float(np.sum(resid(x) ** 2))
            if abs(prev - cost) < 1e-4 * max(1.0, cost):
                break
            prev = cost
    w, cs = unpack(x)
    L = L0 + np.tensordot(w, dL, 1)
    report = {"cost_start": float(np.sum(r0 ** 2)), "cost_end": float(np.sum(resid(x) ** 2)), "photos": []}
    for o, c in zip(obs, cs):
        m = o["conf"] > 0
        pr = project(L[m], c, o["w"], o["h"])
        pr0 = project(L0[m], init_camera(L0[m], o["uv"][m], o["w"], o["h"], o.get("f")), o["w"], o["h"])
        iod = np.linalg.norm(o["uv"][EYE_L] - o["uv"][EYE_R]) if not np.isnan(o["uv"][EYE_L]).any() else float("nan")
        e1 = np.sqrt(np.mean(np.sum((pr - o["uv"][m]) ** 2, 1)))
        e0 = np.sqrt(np.mean(np.sum((pr0 - o["uv"][m]) ** 2, 1)))
        report["photos"].append({"image": o["image"], "n_points": int(m.sum()), "expr": o["expr"],
                                 "rms_px_before": float(e0), "rms_px_after": float(e1),
                                 "rms_pct_iod_after": float(100 * e1 / iod) if iod == iod else None,
                                 "camera": c.tolist()})
    mods = dict(params.get("modifiers", {}))
    weights = {}
    for (name, mlist), val in zip(vars_, w):
        weights[name] = float(val)
        for m, sgn in mlist:
            if sgn == 0:
                mods[m] = mods.get(m, 0.0) + float(val)
            else:
                mods[m] = mods.get(m, 0.0) + sgn * float(val)
    mods = {k: round(v, 4) for k, v in mods.items() if abs(v) > 1e-4}
    report["weights"] = {k: round(v, 4) for k, v in weights.items() if v > 1e-3}
    return mods, report


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corr", required=True)
    ap.add_argument("--params", required=True)
    ap.add_argument("--photos", nargs="+", default=[])
    ap.add_argument("--manual")
    ap.add_argument("--out", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--lam", type=float, default=0.5,
                    help="L2 prior on morph weights (residuals are in %% of inter-ocular distance)")
    ap.add_argument("--asym", action="store_true")
    ap.add_argument("--groups", default=",".join(FACE_GROUPS))
    ap.add_argument("--exclude", default="head-trans,neck",
                    help="modifier substrings never fitted (pure translations are absorbed by the camera)")
    ap.add_argument("--fix-focal", action="store_true")
    a = ap.parse_args()
    params = json.load(open(a.params))
    corr = json.load(open(a.corr))
    vars_ = variables(a.asym, a.groups.split(","), [x for x in a.exclude.split(",") if x])
    obs = load_obs(a.photos, a.manual)
    print(f"{len(vars_)} variables, {len(obs)} photos")
    mods, rep = fit(params, corr, obs, vars_, a.lam, a.fix_focal)
    out = dict(params)
    out["modifiers"] = mods
    json.dump(out, open(a.out, "w"), indent=1)
    json.dump(rep, open(a.report, "w"), indent=1)
    for p in rep["photos"]:
        print(os.path.basename(p["image"]), "rms px %.2f -> %.2f (%.1f%% IOD)" % (
            p["rms_px_before"], p["rms_px_after"], p["rms_pct_iod_after"] or -1))
