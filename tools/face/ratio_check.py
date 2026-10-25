#!/usr/bin/env python3
"""Compare facial proportion ratios between photos and renders of the model from the same
fitted cameras, measured with the SAME detector (MediaPipe), so its bias cancels in ratios.

  ratio_check.py --photos lm_photos.json --renders lm_renders.json [--names a.png,b.png]

Ratios (2D, image space):
  nose_w/intercanthal   alar width (64-294) / inner canthi (133-362)       dossier 01 §2.2: 1.28-1.37
  mouth_w/ipd           mouth corners (61-291) / pupil distance (468-473)  dossier: ~0.78 at rest
  eye_open              eye height (159-145) / eye width (33-133), mean of both eyes
  brow_eye/ipd          brow (105) to upper lid (159) distance / ipd
  lower/middle third    (subnasale 2 - menton 152) / (nasion 168 - subnasale 2)  dossier: 1.25-1.35
  face_w/ipd            cheek-to-cheek (234-454) / ipd
  lip_h/mouth_w         (0 - 17) / mouth width
"""
import argparse
import json
import os

import numpy as np

RATIOS = {
    "nose_w/intercanthal": ((64, 294), (133, 362)),
    "mouth_w/ipd": ((61, 291), (468, 473)),
    "brow_eye/ipd": ((105, 159), (468, 473)),
    "lower/middle_third": ((2, 152), (168, 2)),
    "face_w/ipd": ((234, 454), (468, 473)),
    "lip_h/mouth_w": ((0, 17), (61, 291)),
}


def ratios(L):
    L = np.asarray(L)[:, :2]
    d = lambda a, b: np.linalg.norm(L[a] - L[b])
    out = {k: d(*n) / max(d(*m), 1e-9) for k, (n, m) in RATIOS.items()}
    out["eye_open"] = 0.5 * (d(159, 145) / d(33, 133) + d(386, 374) / d(263, 362))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--photos", required=True)
    ap.add_argument("--renders", required=True)
    ap.add_argument("--names", default="")
    ap.add_argument("--out")
    a = ap.parse_args()
    P = json.load(open(a.photos))
    R = json.load(open(a.renders))
    names = [n for n in a.names.split(",") if n]
    rows = {}
    for rk, rv in R.items():
        n = os.path.splitext(os.path.basename(rk))[0]
        if names and not any(os.path.splitext(x)[0] == n for x in names):
            continue
        pk = [k for k in P if os.path.splitext(os.path.basename(k))[0] == n]
        if not pk or not P[pk[0]] or not rv:
            continue
        rows[n] = {"photo": ratios(P[pk[0]]["landmarks"]), "model": ratios(rv["landmarks"])}
    keys = list(next(iter(rows.values()))["photo"].keys()) if rows else []
    summary = {}
    print("%-22s" % "ratio" + "".join("%18s" % n[:16] for n in rows) + "%12s" % "mean dev%")
    for k in keys:
        devs = [(r["model"][k] / r["photo"][k] - 1) * 100 for r in rows.values()]
        summary[k] = {"mean_dev_pct": float(np.mean(devs)), "per_image": {n: [round(r["photo"][k], 3), round(r["model"][k], 3)] for n, r in rows.items()}}
        print("%-22s" % k + "".join("%9.3f/%-8.3f" % (r["photo"][k], r["model"][k]) for r in rows.values()) + "%+11.1f" % np.mean(devs))
    if a.out:
        json.dump(summary, open(a.out, "w"), indent=1)


if __name__ == "__main__":
    main()
