#!/usr/bin/env python3
"""Uniformly scale the whole head to a target crown-chin height, then re-solve stature.

Photo fitting fixes only the head's internal proportions (skull vs face, feature sizes); the
head's size relative to the body comes from the body evidence (research/notes/02 §4.3:
crown-chin 23.8 +/- 0.8 cm at 183 cm). This adds the same increment to head-scale-vert,
-horiz and -depth (proportions preserved) until head_height hits the target, then adjusts the
height macro until stature is back on target.

  normalize_head.py model/source/r9_params.json --head-height 23.0 --stature 183.0 [--write]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import body_fit as B  # noqa: E402
import mh_engine as E  # noqa: E402

KEYS = ["head/head-scale-vert-decr|incr", "head/head-scale-depth-decr|incr", "head/head-scale-horiz-decr|incr"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("params")
    ap.add_argument("--head-height", type=float, default=23.0)
    ap.add_argument("--stature", type=float, default=183.0)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    p = json.load(open(a.params))
    M = B.Measurer()
    base = {k: p["modifiers"].get(k, 0.0) for k in KEYS}

    def meas(d, h=None):
        q = json.loads(json.dumps(p))
        for k in KEYS:
            q["modifiers"][k] = max(-1.0, min(1.0, base[k] + d))
        if h is not None:
            q["macro"]["height"] = h
        V, _ = E.build(q, M.base)
        return M.measure(V), q

    lo, hi = -0.6, 0.6
    for _ in range(30):
        mid = (lo + hi) / 2
        if meas(mid)[0]["head_height"] < a.head_height:
            lo = mid
        else:
            hi = mid
    d = (lo + hi) / 2
    h0 = p["macro"]["height"]
    lo, hi = h0 - 0.08, h0 + 0.08
    for _ in range(30):
        mid = (lo + hi) / 2
        if meas(d, mid)[0]["height"] < a.stature:
            lo = mid
        else:
            hi = mid
    m, q = meas(d, (lo + hi) / 2)
    note = (f"uniform head scale {d:+.4f} on head-scale vert/horiz/depth -> crown-chin {m['head_height']:.2f} cm "
            f"(target {a.head_height}); height macro {h0:.4f} -> {q['macro']['height']:.4f} -> stature {m['height']:.2f} cm")
    print(note)
    print({k: round(m[k], 2) for k in ("head_height", "head_breadth", "head_length", "height", "mass_kg", "neck_circ")})
    if a.write:
        q.setdefault("_provenance", {})["normalize_head"] = note
        json.dump(q, open(a.params, "w"), indent=1)


if __name__ == "__main__":
    main()
