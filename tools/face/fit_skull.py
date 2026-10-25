#!/usr/bin/env python3
"""Fit cranium shape to photo head silhouettes (the part face landmarks cannot see).

Cameras come from the face fit (fit_loop.py cams_fit_rN.json) and stay fixed, so the
face landmarks pin the absolute scale and only the skull above the brow line is compared.

  loss_p = |model_mask XOR photo_mask| within ROI_p / |photo_mask within ROI_p|
  ROI_p  = image half-plane above the brow line (normal from mouth to brow midpoint), minus
           a 4 % band right at the brows (eyebrow/eye segmentation is noisy there)
  total  = mean_p loss_p + w_lm * landmark RMS (% IOD) + lam * ||x||^2

Model silhouette: the head + ears region of the body mesh (and the hair shell is ignored:
the segmenter labels the clipped patch and the shaved scalp alike, 1 cm of curls is
within its edge noise) projected with fit_face.project and filled per triangle.

  fit_skull.py --params fit.json --corr corr.json --cams cams.json --masks DIR --lm lm.json --out out.json --report rep.json
"""
import argparse
import json
import os
import sys

import numpy as np
from PIL import Image, ImageDraw
from scipy.optimize import minimize

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "model"))
import fit_face as F  # noqa: E402
import mh_engine as E  # noqa: E402

SKULL_VARS = ["head/head-scale-vert-decr|incr", "head/head-scale-depth-decr|incr",
              "head/head-scale-horiz-decr|incr", "head/head-back-scale-depth-decr|incr",
              "head/head-round", "head/head-oval", "head/head-square",
              "forehead/forehead-scale-vert-decr|incr", "forehead/forehead-trans-backward|forward",
              "forehead/forehead-temple-decr|incr"]
SCALE = 0.5   # rasterise at half resolution


class Problem:
    def __init__(self, params, corr, cams, masks_dir, lm, names, w_lm, lam):
        self.base = E.load_obj()
        self.params = params
        self.corr = corr
        self.w_lm, self.lam = w_lm, lam
        V0, _ = E.build(params, self.base)
        J, _ = E.joint_positions(V0)
        neck_y = J["neck02____tail"][1]
        body = self.base["groups"]["body"]
        self.tris = []
        for fi in body:
            vi = self.base["faces"][fi][0]
            if all(V0[i, 1] > neck_y for i in vi):
                self.tris += [(vi[0], vi[k], vi[k + 1]) for k in range(1, len(vi) - 1)]
        self.tris = np.array(self.tris)
        self.Bv = np.array([c["v"] for c in corr])
        self.Bw = np.array([c["w"] for c in corr])
        self.views = []
        for c in cams:
            if names and c["name"] not in names:
                continue
            base = os.path.splitext(c["name"])[0]
            mp = os.path.join(masks_dir, base + "_head.png")
            key = [k for k in lm if os.path.basename(k) == c["name"]]
            if not os.path.exists(mp) or not key:
                continue
            L = np.array(lm[key[0]]["landmarks"])[:, :2]
            w, h = int(c["w"] * SCALE), int(c["h"] * SCALE)
            photo = np.asarray(Image.open(mp).convert("L").resize((w, h))) > 127
            # keep only the connected component that contains this subject's face (press
            # photos often contain team-mates' heads)
            from scipy import ndimage
            lab, n = ndimage.label(photo)
            cx, cy = (L.mean(0) * SCALE).astype(int)
            if 0 <= cy < h and 0 <= cx < w and lab[cy, cx] > 0:
                photo = lab == lab[cy, cx]
            brow = (L[105] + L[334]) / 2 * SCALE
            mouth = L[13] * SCALE
            up = brow - mouth
            up /= np.linalg.norm(up)
            ys, xs = np.mgrid[0:h, 0:w]
            sd = (xs - brow[0]) * up[0] + (ys - brow[1]) * up[1]
            iod = np.linalg.norm(L[473] - L[468]) * SCALE
            roi = sd > 0.25 * iod
            self.views.append({"name": c["name"], "cam": np.array(c["cam"]), "w": c["w"], "h": c["h"],
                               "photo": photo, "roi": roi, "lm": L,
                               "conf": np.where(np.isin(np.arange(478), F.FACE_OVAL), 0.35, 1.0),
                               "iod": iod / SCALE})

    def build(self, x):
        p = json.loads(json.dumps(self.params))
        mods = p.setdefault("modifiers", {})
        for name, v in zip(SKULL_VARS, x):
            mods[name] = float(v)
        V, _ = E.build(p, self.base)
        return p, V

    def model_mask(self, V, v):
        uv = F.project(V, v["cam"], v["w"], v["h"]) * SCALE
        w, h = v["photo"].shape[1], v["photo"].shape[0]
        img = Image.new("L", (w, h), 0)
        d = ImageDraw.Draw(img)
        P = uv[self.tris]
        for t in P:
            d.polygon([tuple(t[0]), tuple(t[1]), tuple(t[2])], fill=255)
        return np.asarray(img) > 127

    def evaluate(self, x, detail=False):
        p, V = self.build(x)
        L = (V[self.Bv] * self.Bw[..., None]).sum(1)
        sil, lmr, per = [], [], []
        for v in self.views:
            m = self.model_mask(V, v)
            roi = v["roi"]
            ph = v["photo"] & roi
            err = ((m & roi) ^ ph).sum() / max(1, ph.sum())
            sil.append(err)
            pr = F.project(L, v["cam"], v["w"], v["h"])
            r = np.sqrt(np.average(np.sum((pr - v["lm"]) ** 2, 1), weights=v["conf"])) / v["iod"] * 100
            lmr.append(r)
            if detail:
                per.append({"name": v["name"], "silhouette_err": float(err), "lm_rms_pct_iod": float(r),
                            "model_area_roi": int((m & roi).sum()), "photo_area_roi": int(ph.sum())})
        loss = float(np.mean(sil) + self.w_lm * np.mean(lmr) / 100 + self.lam * np.sum(np.asarray(x) ** 2))
        return (loss, per, p) if detail else loss


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--params", required=True)
    ap.add_argument("--corr", required=True)
    ap.add_argument("--cams", required=True)
    ap.add_argument("--masks", required=True)
    ap.add_argument("--lm", required=True)
    ap.add_argument("--names", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--w-lm", type=float, default=1.0)
    ap.add_argument("--lam", type=float, default=0.01)
    ap.add_argument("--maxiter", type=int, default=400)
    a = ap.parse_args()
    params = json.load(open(a.params))
    prob = Problem(params, json.load(open(a.corr)), json.load(open(a.cams)), a.masks, json.load(open(a.lm)),
                   [n for n in a.names.split(",") if n], a.w_lm, a.lam)
    x0 = np.array([params.get("modifiers", {}).get(n, 0.0) for n in SKULL_VARS])
    l0, per0, _ = prob.evaluate(x0, True)
    bounds = [(-1, 1) if "|" in n else (0, 1) for n in SKULL_VARS]
    res = minimize(prob.evaluate, x0, method="Powell", bounds=bounds,
                   options={"maxiter": a.maxiter, "xtol": 1e-3, "ftol": 1e-4})
    l1, per1, p = prob.evaluate(res.x, True)
    json.dump(p, open(a.out, "w"), indent=1)
    rep = {"loss_start": l0, "loss_end": l1, "vars": dict(zip(SKULL_VARS, map(float, res.x))),
           "views_start": per0, "views_end": per1, "nfev": int(res.nfev)}
    json.dump(rep, open(a.report, "w"), indent=1)
    print("loss %.4f -> %.4f (%d evals)" % (l0, l1, res.nfev))
    for s0, s1 in zip(per0, per1):
        print("%-18s sil %.3f -> %.3f   lm %.1f%% -> %.1f%%" % (s0["name"], s0["silhouette_err"], s1["silhouette_err"],
                                                               s0["lm_rms_pct_iod"], s1["lm_rms_pct_iod"]))
    for k, v in rep["vars"].items():
        print("  %-45s %+.3f" % (k, v))


if __name__ == "__main__":
    main()
