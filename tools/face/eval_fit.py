#!/usr/bin/env python3
"""Compare two params JSONs geometrically (e.g. fitted vs synthetic ground truth).

Reports mean / p95 vertex distance (mm) over the face and whole head, both raw and after a
similarity (Procrustes) alignment, because absolute head scale/position is not observable
from photos (the camera absorbs it); head scale is set separately from body data.
  eval_fit.py truth.json fitted.json [--start start.json]
"""
import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "model"))
import mh_engine as E  # noqa: E402


def similarity_align(A, B):
    """Return A aligned onto B with optimal scale/rotation/translation (Umeyama)."""
    ma, mb = A.mean(0), B.mean(0)
    A0, B0 = A - ma, B - mb
    U, S, Vt = np.linalg.svd(B0.T @ A0)
    d = np.sign(np.linalg.det(U @ Vt))
    D = np.diag([1, 1, d])
    R = U @ D @ Vt
    s = (S * np.diag(D)).sum() / (A0 ** 2).sum()
    return s * A0 @ R.T + mb, s


CORR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "model", "source",
                    "face_corr_mediapipe478.json")


def regions(base, v0):
    """face/head: coarse regions; observable: vertices within 8 mm of a MediaPipe landmark
    (the only part photos constrain)."""
    from scipy.spatial import cKDTree
    ids = np.array(E.group_vertex_ids(base, "body"))
    vv = v0[ids]
    out = {"face": ids[(vv[:, 1] > 7.0) & (vv[:, 2] > 0.4)], "head": ids[vv[:, 1] > 7.0]}
    if os.path.exists(CORR):
        corr = json.load(open(CORR))
        Bv = np.array([c["v"] for c in corr])
        Bw = np.array([c["w"] for c in corr])
        L = (v0[Bv] * Bw[..., None]).sum(1)
        d, _ = cKDTree(L).query(vv)
        out["observable"] = ids[d < 0.08]
    return out


def compare(pa, pb, base=None):
    base = base or E.load_obj()
    va, _ = E.build(pa, base)
    vb, _ = E.build(pb, base)
    out = {}
    for name, ix in regions(base, vb).items():
        d_raw = np.linalg.norm(va[ix] - vb[ix], axis=1) * 100
        al, s = similarity_align(va[ix], vb[ix])
        d_al = np.linalg.norm(al - vb[ix], axis=1) * 100
        out[name] = {"n": int(len(ix)), "raw_mean_mm": float(d_raw.mean()), "raw_p95_mm": float(np.percentile(d_raw, 95)),
                     "aligned_mean_mm": float(d_al.mean()), "aligned_p95_mm": float(np.percentile(d_al, 95)),
                     "scale": float(s)}
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("truth")
    ap.add_argument("fitted")
    ap.add_argument("--start")
    a = ap.parse_args()
    t = json.load(open(a.truth))
    base = E.load_obj()
    res = {"fitted": compare(json.load(open(a.fitted)), t, base)}
    if a.start:
        res["start"] = compare(json.load(open(a.start)), t, base)
    print(json.dumps(res, indent=1))
