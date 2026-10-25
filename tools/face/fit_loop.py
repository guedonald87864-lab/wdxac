#!/usr/bin/env python3
"""Analysis-by-synthesis face fitting loop (detector-bias cancellation).

MediaPipe landmarks are a learned regression that pulls faces toward an average shape, so
they are not geometrically exact. Fitting raw detections to the mesh therefore converges
to the wrong shape. This loop cancels that bias:

  round 0: fit morphs + cameras to the photo detections            (fit_face.fit)
  round k: render the current fit from each estimated camera        (blender_render_cams.py)
           detect landmarks on those renders with the same detector (landmarks.py)
           bias_p = detect(render_p) - project(model landmarks, cam_p)
           refit against  photo_detection_p - bias_p
Converged when the morph weights stop changing.

  fit_loop.py --corr corr.json --params start.json --photos lm.json [--select a.png,b.png]
              --workdir D --rounds 3 [--truth truth.json]   # truth: synthetic validation only
"""
import argparse
import json
import os
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "model"))
import fit_face as F  # noqa: E402
import mh_engine as E  # noqa: E402

BLENDER = os.environ.get("BLENDER", "blender")
MP_PY = os.environ.get("MP_PYTHON", "/opt/tools/mpvenv/bin/python")


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(" ".join(cmd) + "\n" + r.stdout[-3000:] + r.stderr[-3000:])
    return r.stdout


def model_landmarks(params, corr, base):
    V, _ = E.build(params, base)
    Bv = np.array([c["v"] for c in corr])
    Bw = np.array([c["w"] for c in corr])
    return (V[Bv] * Bw[..., None]).sum(1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corr", required=True)
    ap.add_argument("--params", required=True)
    ap.add_argument("--photos", nargs="+", required=True)
    ap.add_argument("--select", default="", help="comma list of image basenames to use")
    ap.add_argument("--conf", default="", help="json {basename: {'scale': c, 'drop_lips': bool, 'drop_lower': bool}}")
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--lam", type=float, default=5.0)
    ap.add_argument("--exclude", default="head-trans,neck")
    ap.add_argument("--truth")
    ap.add_argument("--all-vars", action="store_true", help="fit every face morph, not only IDENTITY")
    a = ap.parse_args()
    os.makedirs(a.workdir, exist_ok=True)
    start = json.load(open(a.params))
    corr = json.load(open(a.corr))
    base = E.load_obj()
    obs = F.load_obs(a.photos)
    sel = [s for s in a.select.split(",") if s]
    if sel:
        obs = [o for o in obs if os.path.basename(o["image"]) in sel]
    conf = json.load(open(a.conf)) if a.conf else {}
    for o in obs:
        c = conf.get(os.path.basename(o["image"]), {})
        o["conf"] = o["conf"] * c.get("scale", 1.0)
        if c.get("drop_lips"):
            o["conf"][F.LIPS] = 0
        if c.get("drop_lower"):
            lower = [i for i in range(478) if i in F.LIPS or i in (152, 148, 176, 149, 150, 136, 172, 58, 377, 400, 378, 379, 365, 397, 288, 361)]
            o["conf"][lower] = 0
    photo_uv = [o["uv"].copy() for o in obs]
    vars_ = F.variables(exclude=[x for x in a.exclude.split(",") if x],
                        include=None if a.all_vars else F.IDENTITY)
    print(len(vars_), "variables")
    history = []
    params = start
    for rnd in range(a.rounds + 1):
        mods, rep = F.fit(start, corr, obs, vars_, lam=a.lam)
        params = dict(start)
        params["modifiers"] = mods
        pj = os.path.join(a.workdir, f"fit_r{rnd}.json")
        json.dump(params, open(pj, "w"), indent=1)
        entry = {"round": rnd, "rms_pct_iod": [p["rms_pct_iod_after"] for p in rep["photos"]]}
        if a.truth:
            sys.path.insert(0, HERE)
            import eval_fit
            entry["vs_truth"] = eval_fit.compare(params, json.load(open(a.truth)), base)["face"]
        history.append(entry)
        print(json.dumps(entry))
        json.dump([{"name": os.path.basename(o["image"]), "cam": p["camera"], "w": o["w"], "h": o["h"]}
                   for o, p in zip(obs, rep["photos"])], open(os.path.join(a.workdir, f"cams_fit_r{rnd}.json"), "w"))
        if rnd == a.rounds:
            break
        # --- synthesise: render the current fit from the estimated cameras and re-detect
        blend = os.path.join(a.workdir, f"fit_r{rnd}.blend")
        run([BLENDER, "-b", "--python", os.path.join(HERE, "..", "model", "blender_build.py"), "--",
             "--params", pj, "--out", blend])
        cams = [{"name": os.path.basename(o["image"]), "cam": p["camera"], "w": o["w"], "h": o["h"]}
                for o, p in zip(obs, rep["photos"])]
        cj = os.path.join(a.workdir, f"cams_r{rnd}.json")
        json.dump(cams, open(cj, "w"))
        rdir = os.path.join(a.workdir, f"render_r{rnd}")
        run([BLENDER, "-b", blend, "--python", os.path.join(HERE, "blender_render_cams.py"), "--",
             "--cams", cj, "--outdir", rdir])
        lmj = os.path.join(a.workdir, f"lm_render_r{rnd}.json")
        renders = [os.path.join(rdir, os.path.splitext(c["name"])[0] + ".png") for c in cams]
        run([MP_PY, os.path.join(HERE, "landmarks.py")] + renders + ["--out", lmj])
        det = json.load(open(lmj))
        L = model_landmarks(params, corr, base)
        n_ok = 0
        for o, p, r_path, uv0 in zip(obs, rep["photos"], renders, photo_uv):
            d = det.get(r_path)
            if not d:
                o["uv"] = uv0.copy()
                continue
            n_ok += 1
            proj = F.project(L, np.array(p["camera"]), o["w"], o["h"])
            bias = np.array(d["landmarks"])[:, :2] - proj
            o["uv"] = uv0 - bias
        history[-1]["renders_detected"] = n_ok
    json.dump({"history": history, "final": params}, open(os.path.join(a.workdir, "loop_report.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
