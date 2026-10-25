#!/usr/bin/env python3
"""Fit the MakeHuman body to centimetre targets (height, circumferences, lengths, mass).

Measurements reuse MakeHuman's own ruler vertex loops (plugins/0_modeling_a_measurement.py,
Ruler.Measures) plus a few skeletal/bounding measures computed from joints and the body
group. Mass is estimated from the closed body volume x density (1.06 kg/L, lean athlete).

  body_fit.py --params start.json --targets data/body/r9_body_targets.json --out fitted.json --report rep.json
  body_fit.py --params fitted.json --measure            # just print measurements
"""
import argparse
import json
import os
import sys

import numpy as np
from scipy.optimize import least_squares

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mh_engine as E  # noqa: E402

# MakeHuman 1.x Ruler.Measures (CC0), polyline vertex loops; length in decimetres*10 = cm
RULERS = {
    "neck_circ": [7514, 10358, 7631, 7496, 7488, 7489, 7474, 7475, 7531, 7537, 7543, 7549, 7555, 7561, 7743, 7722, 856, 1030, 1051, 850, 844, 838, 832, 826, 820, 756, 755, 770, 769, 777, 929, 3690, 804, 800, 808, 801, 799, 803, 7513, 7515, 7521, 7514],
    "upperarm_circ": [8383, 8393, 8392, 8391, 8390, 8394, 8395, 8399, 10455, 10516, 8396, 8397, 8398, 8388, 8387, 8386, 10431, 8385, 8384, 8389],
    "upperarm_length": [8274, 10037],
    "lowerarm_length": [10040, 10548],
    "chest_circ": [8439, 8455, 8462, 8446, 8478, 8494, 8557, 8510, 8526, 8542, 10720, 10601, 10603, 10602, 10612, 10611, 10610, 10613, 10604, 10605, 10606, 3942, 3941, 3940, 3950, 3947, 3948, 3949, 3938, 3939, 3937, 4065, 1870, 1854, 1838, 1885, 1822, 1806, 1774, 1790, 1783, 1767, 1799, 8471],
    "waist_circ": [4121, 10760, 10757, 10777, 10776, 10779, 10780, 10778, 10781, 10771, 10773, 10772, 10775, 10774, 10814, 10834, 10816, 10817, 10818, 10819, 10820, 10821, 4181, 4180, 4179, 4178, 4177, 4176, 4175, 4196, 4173, 4131, 4132, 4129, 4130, 4128, 4138, 4135, 4137, 4136, 4133, 4134, 4108, 4113, 4118, 4121],
    "nape_to_waist": [1491, 4181],
    "shoulder_dist": [7478, 8274],
    "hips_circ": [4341, 10968, 10969, 10971, 10970, 10967, 10928, 10927, 10925, 10926, 10923, 10924, 10868, 10875, 10861, 10862, 4228, 4227, 4226, 4242, 4234, 4294, 4293, 4296, 4295, 4297, 4298, 4342, 4345, 4346, 4344, 4343, 4361, 4341],
    "upperleg_height": [10970, 11230],
    "thigh_circ": [11071, 11080, 11081, 11086, 11076, 11077, 11074, 11075, 11072, 11073, 11069, 11070, 11087, 11085, 11084, 12994, 11083, 11082, 11079, 11071],
    "lowerleg_height": [11225, 12820],
    "calf_circ": [11339, 11336, 11353, 11351, 11350, 13008, 11349, 11348, 11345, 11337, 11344, 11346, 11347, 11352, 11342, 11343, 11340, 11341, 11338, 11339],
    "knee_circ": [11223, 11230, 11232, 11233, 11238, 11228, 11229, 11226, 11227, 11224, 11225, 11221, 11222, 11239, 11237, 11236, 13002, 11235, 11234, 11223],
}
DENSITY = 1.06  # kg / L


class Measurer:
    def __init__(self):
        self.base = E.load_obj()
        self.body_v = np.array(E.group_vertex_ids(self.base, "body"))
        tris = []
        for fi in self.base["groups"]["body"]:
            vi = self.base["faces"][fi][0]
            for k in range(1, len(vi) - 1):
                tris.append((vi[0], vi[k], vi[k + 1]))
        self.tris = np.array(tris)
        self.skel = None

    def measure(self, V):
        m = {}
        for k, loop in RULERS.items():
            P = V[loop]
            m[k] = float(10 * np.linalg.norm(np.diff(P, axis=0), axis=1).sum())
        B = V[self.body_v]
        y0 = B[:, 1].min()
        m["height"] = float(10 * (B[:, 1].max() - y0))
        J, self.skel = E.joint_positions(V, self.skel)
        hip = (J["upperleg01.L____head"] + J["upperleg01.R____head"]) / 2
        knee = (J["lowerleg01.L____head"] + J["lowerleg01.R____head"]) / 2
        m["hip_joint_height"] = float(10 * (hip[1] - y0))
        m["knee_height"] = float(10 * (knee[1] - y0))
        sh_l, sh_r = J["upperarm01.L____head"], J["upperarm01.R____head"]
        m["biacromial"] = float(10 * np.linalg.norm(sh_l - sh_r))
        # head breadth (euryon-euryon) and length (glabella-opisthocranion): max width/depth over
        # horizontal slabs between 3 cm above the eye centres (above the ears) and 2 cm below the crown
        top = B[:, 1].max()
        eye_y = (J["eye.L____head"][1] + J["eye.R____head"][1]) / 2
        head = B[B[:, 1] > J["neck02____head"][1]]
        widths, depths = [], []
        for y in np.arange(eye_y + 0.1, top - 0.2, 0.05):
            s = head[np.abs(head[:, 1] - y) < 0.025]
            if len(s) > 8:
                depths.append(np.ptp(s[:, 2]))
                if y > eye_y + 0.3:
                    widths.append(np.ptp(s[:, 0]))
        m["head_breadth"] = float(10 * max(widths))
        m["head_length"] = float(10 * max(depths))
        mid = head[(np.abs(head[:, 0]) < 0.15) & (head[:, 2] > 0.3)]
        m["head_height"] = float(10 * (top - mid[:, 1].min()))
        # mass from volume of the body group (divergence theorem); holes (eyes/mouth) are small
        a, b, c = V[self.tris[:, 0]], V[self.tris[:, 1]], V[self.tris[:, 2]]
        vol_dm3 = abs(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)
        m["volume_L"] = float(vol_dm3)
        m["mass_kg"] = float(vol_dm3 * DENSITY)
        m["bmi"] = m["mass_kg"] / (m["height"] / 100) ** 2
        return m


def fit(params, targets, free, measurer):
    """targets: {measure: {"value": cm, "tol": cm}}; free: list of ('macro', name, lo, hi) or ('mod', name, lo, hi)."""
    x0 = []
    for kind, name, lo, hi in free:
        v = params["macro"].get(name, 0.5) if kind == "macro" else params.get("modifiers", {}).get(name, 0.0)
        x0.append(min(max(v, lo + 1e-6), hi - 1e-6))

    def apply(x):
        p = json.loads(json.dumps(params))
        p.setdefault("modifiers", {})
        for (kind, name, lo, hi), v in zip(free, x):
            if kind == "macro":
                p["macro"][name] = float(v)
            else:
                p["modifiers"][name] = float(v)
                if "/r-" in name:
                    p["modifiers"][name.replace("/r-", "/l-")] = float(v)
        return p

    def resid(x):
        V, _ = E.build(apply(x), measurer.base)
        m = measurer.measure(V)
        r = [(m[k] - t["value"]) / t.get("tol", 1.0) for k, t in targets.items()]
        r += [0.15 * v for (kind, _, _, _), v in zip(free, x) if kind == "mod"]
        return np.array(r)

    lo = [f[2] for f in free]
    hi = [f[3] for f in free]
    res = least_squares(resid, np.array(x0), bounds=(lo, hi), diff_step=1e-3, max_nfev=200)
    p = apply(res.x)
    V, _ = E.build(p, measurer.base)
    return p, measurer.measure(V)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--params", required=True)
    ap.add_argument("--targets")
    ap.add_argument("--out")
    ap.add_argument("--report")
    ap.add_argument("--measure", action="store_true")
    a = ap.parse_args()
    params = json.load(open(a.params))
    M = Measurer()
    if a.measure or not a.targets:
        V, _ = E.build(params, M.base)
        print(json.dumps({k: round(v, 2) for k, v in M.measure(V).items()}, indent=1))
        sys.exit(0)
    T = json.load(open(a.targets))
    free = [tuple(f) for f in T["free"]]
    p, m = fit(params, T["targets"], free, M)
    json.dump(p, open(a.out, "w"), indent=1)
    rep = {"targets": T["targets"], "achieved": {k: round(v, 2) for k, v in m.items()},
           "within_tol": {k: abs(m[k] - t["value"]) <= t.get("tol", 1.0) for k, t in T["targets"].items()}}
    json.dump(rep, open(a.report, "w"), indent=1)
    for k, t in T["targets"].items():
        print(f"{k:18s} target {t['value']:7.2f}  got {m[k]:7.2f}  {'OK' if rep['within_tol'][k] else 'MISS'}")
    print(f"mass {m['mass_kg']:.1f} kg  BMI {m['bmi']:.1f}")
