#!/usr/bin/env python3
"""MakeHuman-compatible morph engine (numpy only, no MakeHuman runtime needed).

Re-implements the parts of MakeHuman 1.x we need, from the CC0 data released by
the MakeHuman project (commit pinned in tools/model/fetch_makehuman.sh):

  * base mesh loading (base.obj, 1 unit = 1 decimetre)
  * macro modifiers (gender/age/ethnicity/muscle/weight/height/proportions),
    with the same factor maths as makehuman/apps/human.py (_set*Vals)
  * regular modifiers ("group/target-min|max" in [-1, 1], or single targets in [0, 1])
  * skeleton joint positions from mhskel joint vertex lists
  * per-bone skin weights from default_weights.mhw

Everything is deterministic: the same params JSON always gives the same mesh.
"""
import json
import os
import re
from functools import lru_cache

import numpy as np

MH_DATA = os.environ.get("MH_DATA", "/opt/tools/mh/makehuman/makehuman/data")
TARGETS = os.path.join(MH_DATA, "targets")

MACRO_TOKENS = {
    "male", "female", "baby", "child", "young", "old",
    "african", "asian", "caucasian",
    "minmuscle", "averagemuscle", "maxmuscle",
    "minweight", "averageweight", "maxweight",
    "minheight", "averageheight", "maxheight",
    "idealproportions", "regularproportions", "uncommonproportions",
    "mincup", "averagecup", "maxcup", "minfirmness", "averagefirmness", "maxfirmness",
    "universal",
}


# ----------------------------------------------------------------------------- mesh io
def load_obj(path=None):
    """Return dict with verts (N,3), uvs (M,2), faces [list of (vidx list, uvidx list, group)], groups."""
    path = path or os.path.join(MH_DATA, "3dobjs", "base.obj")
    verts, uvs, faces = [], [], []
    group = None
    with open(path) as fh:
        for line in fh:
            if line.startswith("v "):
                verts.append([float(x) for x in line.split()[1:4]])
            elif line.startswith("vt "):
                uvs.append([float(x) for x in line.split()[1:3]])
            elif line.startswith("g "):
                group = line[2:].strip()
            elif line.startswith("f "):
                vi, ti = [], []
                for tok in line.split()[1:]:
                    parts = tok.split("/")
                    vi.append(int(parts[0]) - 1)
                    ti.append(int(parts[1]) - 1 if len(parts) > 1 and parts[1] else -1)
                faces.append((vi, ti, group))
    groups = {}
    for fi, (_, _, g) in enumerate(faces):
        groups.setdefault(g, []).append(fi)
    return {"verts": np.asarray(verts, np.float64), "uvs": np.asarray(uvs, np.float64),
            "faces": faces, "groups": groups}


@lru_cache(maxsize=None)
def load_target(path):
    idx, off = [], []
    with open(path) as fh:
        for line in fh:
            if not line.strip() or line.startswith("#"):
                continue
            p = line.split()
            idx.append(int(p[0]))
            off.append([float(p[1]), float(p[2]), float(p[3])])
    return np.asarray(idx, np.int64), np.asarray(off, np.float64).reshape(-1, 3)


# ----------------------------------------------------------------------------- macros
def macro_factors(m):
    """Port of makehuman/apps/human.py _set*Vals. m: dict of macro values in [0,1]."""
    f = {}
    g = m.get("gender", 1.0)
    f["male"], f["female"] = g, 1 - g
    age = m["age"] if "age" in m else age_years_to_value(m.get("age_years", 25.0))
    if age < 0.5:
        f["old"] = 0.0
        f["baby"] = max(0.0, 1 - age * 5.333)
        f["young"] = max(0.0, (age - 0.1875) * 3.2)
        f["child"] = max(0.0, min(1.0, 5.333 * age) - f["young"])
    else:
        f["child"] = f["baby"] = 0.0
        f["old"] = max(0.0, age * 2 - 1)
        f["young"] = 1 - f["old"]
    eth = np.array([m.get("african", 1 / 3), m.get("asian", 1 / 3), m.get("caucasian", 1 / 3)], float)
    eth = eth / eth.sum()
    f["african"], f["asian"], f["caucasian"] = eth

    def tri(v, lo, mid, hi, avg_rule="sum"):
        f[hi] = max(0.0, v * 2 - 1)
        f[lo] = max(0.0, 1 - v * 2)
        if avg_rule == "sum":
            f[mid] = 1 - (f[hi] + f[lo])
        else:  # height / proportions / cup / firmness rule
            f[mid] = 1 - f[hi] if f[hi] > f[lo] else 1 - f[lo]

    tri(m.get("muscle", 0.5), "minmuscle", "averagemuscle", "maxmuscle")
    tri(m.get("weight", 0.5), "minweight", "averageweight", "maxweight")
    tri(m.get("height", 0.5), "minheight", "averageheight", "maxheight", "max")
    tri(m.get("proportions", 0.5), "uncommonproportions", "regularproportions", "idealproportions", "max")
    tri(m.get("breast_size", 0.5), "mincup", "averagecup", "maxcup", "max")
    tri(m.get("breast_firmness", 0.5), "minfirmness", "averagefirmness", "maxfirmness", "max")
    f["universal"] = 1.0
    return f


def age_years_to_value(years, min_age=1.0, mid_age=25.0, max_age=90.0):
    if years < mid_age:
        return (years - min_age) / ((mid_age - min_age) * 2)
    return (years - mid_age) / ((max_age - mid_age) * 2) + 0.5


def macro_target_weights(m):
    f = macro_factors(m)
    out = {}
    for sub in ("macrodetails", "macrodetails/height", "macrodetails/proportions", "breast"):
        d = os.path.join(TARGETS, sub)
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".target"):
                continue
            toks = fn[:-7].split("-")
            if not all(t in MACRO_TOKENS for t in toks):
                continue  # regular (non-macro) targets in breast/
            w = 1.0
            for t in toks:
                w *= f[t]
            if w > 1e-9:
                out[os.path.join(sub, fn)] = w
    return out


# ----------------------------------------------------------------------------- regular modifiers
@lru_cache(maxsize=None)
def modifier_table():
    """name -> (group, target, min, max). name format 'group/target-min|max' or 'group/target'."""
    tab = {}
    for fn in ("modeling_modifiers.json", "measurement_modifiers.json", "bodyshapes_modifiers.json"):
        p = os.path.join(MH_DATA, "modifiers", fn)
        if not os.path.exists(p):
            continue
        for g in json.load(open(p)):
            for mod in g["modifiers"]:
                if "target" not in mod:
                    continue
                if "min" in mod or "max" in mod:
                    name = f"{g['group']}/{mod['target']}-{mod.get('min')}|{mod.get('max')}"
                else:
                    name = f"{g['group']}/{mod['target']}"
                tab[name] = (g["group"], mod["target"], mod.get("min"), mod.get("max"))
    return tab


def _target_files(group, stem, factors):
    """Resolve the (possibly macro-dependent) target files for one modifier side."""
    d = os.path.join(TARGETS, group)
    exact = os.path.join(d, stem + ".target")
    if os.path.exists(exact):
        return [(os.path.join(group, stem + ".target"), 1.0)]
    res = []
    for fn in sorted(os.listdir(d)):
        if fn.startswith(stem + "-") and fn.endswith(".target"):
            extra = fn[len(stem) + 1:-7].split("-")
            if all(t in MACRO_TOKENS for t in extra):
                w = 1.0
                for t in extra:
                    w *= factors[t]
                if w > 1e-9:
                    res.append((os.path.join(group, fn), w))
    if not res:
        raise FileNotFoundError(f"no target for {group}/{stem}")
    return res


def regular_target_weights(mods, factors):
    tab = modifier_table()
    out = {}
    for name, val in mods.items():
        if abs(val) < 1e-9:
            continue
        if name not in tab:
            raise KeyError(f"unknown modifier {name}")
        group, target, lo, hi = tab[name]
        if lo is None and hi is None:
            sides = [(target, max(0.0, min(1.0, val)))]
        else:
            v = max(-1.0, min(1.0, val))
            sides = [(f"{target}-{lo}", -v)] if v < 0 else [(f"{target}-{hi}", v)]
        for stem, w in sides:
            for path, fw in _target_files(group, stem, factors):
                out[path] = out.get(path, 0.0) + w * fw
    return out


# ----------------------------------------------------------------------------- apply
def build(params, base=None):
    """params: {'macro': {...}, 'modifiers': {...}, 'raw_targets': {relpath: weight}, 'offsets': path.npy}"""
    base = base or load_obj()
    verts = base["verts"].copy()
    macro = params.get("macro", {})
    weights = macro_target_weights(macro)
    for k, v in regular_target_weights(params.get("modifiers", {}), macro_factors(macro)).items():
        weights[k] = weights.get(k, 0.0) + v
    for k, v in params.get("raw_targets", {}).items():
        weights[k] = weights.get(k, 0.0) + v
    for rel, w in sorted(weights.items()):
        idx, off = load_target(os.path.join(TARGETS, rel))
        verts[idx] += w * off
    extra = params.get("sculpt_offsets")
    if extra:
        verts += np.load(extra)
    return verts, weights


def joint_positions(verts, skel=None):
    skel = skel or json.load(open(os.path.join(MH_DATA, "rigs", "default.mhskel")))
    return {k: verts[v].mean(axis=0) for k, v in skel["joints"].items()}, skel


def height_cm(verts, base):
    """MakeHuman getHeightCm: bbox height of the 'body' group only (helpers excluded)."""
    vi = sorted({i for fi in base["groups"]["body"] for i in base["faces"][fi][0]})
    v = verts[vi]
    return 10.0 * (v[:, 1].max() - v[:, 1].min())


def group_vertex_ids(base, group):
    return sorted({i for fi in base["groups"][group] for i in base["faces"][fi][0]})


def write_obj(path, verts, base, groups=None, scale=1.0):
    groups = groups or [g for g in base["groups"]]
    with open(path, "w") as fh:
        fh.write("# generated by tools/model/mh_engine.py (MakeHuman CC0 base mesh)\n")
        for v in verts:
            fh.write("v %.6f %.6f %.6f\n" % tuple(v * scale))
        for t in base["uvs"]:
            fh.write("vt %.6f %.6f\n" % tuple(t))
        for g in groups:
            fh.write(f"g {g}\n")
            for fi in base["groups"][g]:
                vi, ti, _ = base["faces"][fi]
                fh.write("f " + " ".join(f"{a + 1}/{b + 1}" for a, b in zip(vi, ti)) + "\n")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("params")
    ap.add_argument("--obj")
    a = ap.parse_args()
    p = json.load(open(a.params))
    base = load_obj()
    v, w = build(p, base)
    print("targets applied:", len(w))
    print("height_cm: %.2f" % height_cm(v, base))
    if a.obj:
        write_obj(a.obj, v, base)


# ----------------------------------------------------------------------------- proxies (.mhclo)
def load_mhclo(path):
    """Parse a MakeHuman proxy fitting file -> dict(obj_file, scales, refs (N,3), weights (N,3), offsets (N,3))."""
    d = {"scales": {}, "refs": [], "weights": [], "offsets": []}
    in_verts = False
    with open(path) as fh:
        for line in fh:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            p = s.split()
            if in_verts:
                if len(p) == 9:
                    d["refs"].append([int(x) for x in p[:3]])
                    d["weights"].append([float(x) for x in p[3:6]])
                    d["offsets"].append([float(x) for x in p[6:9]])
                    continue
                if len(p) == 1 and p[0].isdigit():  # exact-copy vertex
                    d["refs"].append([int(p[0])] * 3)
                    d["weights"].append([1.0, 0.0, 0.0])
                    d["offsets"].append([0.0, 0.0, 0.0])
                    continue
                in_verts = False
            if p[0] == "obj_file":
                d["obj_file"] = os.path.join(os.path.dirname(path), p[1])
            elif p[0] in ("x_scale", "y_scale", "z_scale"):
                d["scales"][p[0][0]] = (int(p[1]), int(p[2]), float(p[3]))
            elif p[0] == "verts":
                in_verts = True
    for k in ("refs", "weights", "offsets"):
        d[k] = np.asarray(d[k])
    return d


def fit_proxy(clo, verts):
    """MakeHuman proxy fitting: barycentric combo of 3 base verts + per-axis scaled offset."""
    scale = np.ones(3)
    for ax, i in (("x", 0), ("y", 1), ("z", 2)):
        if ax in clo["scales"]:
            a, b, s = clo["scales"][ax]
            scale[i] = abs(verts[a][i] - verts[b][i]) / s
    r, w = clo["refs"], clo["weights"]
    return (verts[r[:, 0]] * w[:, :1] + verts[r[:, 1]] * w[:, 1:2] + verts[r[:, 2]] * w[:, 2:3]
            + clo["offsets"] * scale)
