#!/usr/bin/env python3
"""Parametric 2002 World Cup "Cascao" hair patch for the R9 head (numpy, MakeHuman coords).

Geometry follows research/notes/01_appearance_2002.md §1.4 (all T4 estimates, +/-20%):
  * front edge  = the natural hairline, ~5-6 cm above the brows, no widow's peak, slightly
                  receded corners (corners a bit higher than the centre)
  * front width = 70-80 % of the temporal head width (~10-12 cm)
  * side edges  = near-vertical clipper lines running up/back from the temple
  * back edge   = an arc convex towards the back, 9-11 cm (horizontal) behind the front
                  hairline, 4-6 cm in front of the tragus vertical; it does NOT reach the crown
  * hair        = black, tight, dense curls 0.8-1.2 cm tall -> a real shell with a visible step
                  at every edge (the brief forbids a painted-only patch)
The rest of the scalp is clipper-shaved (texture only, see texture_build.py).

Every length below is in centimetres and lives in model/source/r9_hair_params.json.
build_patch() returns the scalp vertex mask; build_shell() returns shell vertices/faces/uvs.
"""
import json
import os

import numpy as np

import mh_engine as E

LM_BROW_L = [70, 63, 105, 66, 107]      # MediaPipe upper-brow landmarks (subject's right side in image-left)
LM_BROW_R = [300, 293, 334, 296, 336]


def _ear_ids(side):
    i, _ = E.load_target(os.path.join(E.TARGETS, "ears", f"{side}-ear-wing-incr.target"))
    return i


def head_frame(V, base, corr):
    """Key reference positions (MakeHuman coords, decimetres)."""
    Bv = np.array([c["v"] for c in corr])
    Bw = np.array([c["w"] for c in corr])
    L = (V[Bv] * Bw[..., None]).sum(1)
    brow_y = float(np.mean(L[LM_BROW_L + LM_BROW_R][:, 1]))
    ears = np.concatenate([V[_ear_ids("r")], V[_ear_ids("l")]])
    tragus_z = float(np.percentile(ears[:, 2], 90))   # front of the ear
    ear_top_y = float(ears[:, 1].max())
    head_ids = np.array([i for i in E.group_vertex_ids(base, "body") if V[i, 1] > brow_y - 1.2])
    return {"brow_y": brow_y, "tragus_z": tragus_z, "ear_top_y": ear_top_y, "head_ids": head_ids,
            "top_y": float(V[head_ids, 1].max())}


def build_patch(V, base, corr, hp):
    """Return (mask over all vertices, frame). hp: params dict in cm."""
    fr = head_frame(V, base, corr)
    cm = 0.1
    ids = fr["head_ids"]
    P = V[ids]
    x, y, z = P[:, 0], P[:, 1], P[:, 2]
    hl_y = fr["brow_y"] + hp["hairline_above_brow"] * cm + hp["corner_recession"] * cm * (x / (hp["front_width"] * cm / 2)) ** 2
    # front hairline z: the most forward scalp point at the hairline height near the midline
    near = np.abs(y - (fr["brow_y"] + hp["hairline_above_brow"] * cm)) < 0.08
    z_front = float(z[near & (np.abs(x) < 0.3)].max())
    # The dossier's "9-11 cm behind the hairline" is only consistent with "4-6 cm in front of
    # the tragus" if read as arc length over the scalp, so the tragus offset is the primary
    # constraint and the midline arc length is reported for checking.
    z_back_c = fr["tragus_z"] + hp["back_edge_front_of_tragus"] * cm
    half_w_front = hp["front_width"] * cm / 2
    half_w_back = hp["back_width"] * cm / 2
    # side edge half-width interpolates front->back (near-vertical clipper lines)
    t = np.clip((z_front - z) / max(1e-6, z_front - z_back_c), 0, 1)
    half_w = half_w_front + (half_w_back - half_w_front) * t
    # back edge: arc convex towards the back -> sides end further forward than the centre
    z_back = z_back_c + hp["back_arc_sag"] * cm * (np.clip(np.abs(x) / half_w_back, 0, 1)) ** 2
    inside = (y >= hl_y) & (np.abs(x) <= half_w) & (z >= z_back)
    mask = np.zeros(len(V), bool)
    mask[ids[inside]] = True
    mid = P[(np.abs(x) < 0.08) & inside]
    mid = mid[np.argsort(-mid[:, 2])]
    arc = float(np.linalg.norm(np.diff(mid[:, 1:], axis=0), axis=1).sum()) / cm if len(mid) > 1 else 0.0
    fr.update({"z_front": z_front, "z_back_centre": z_back_c, "hairline_y": float(fr["brow_y"] + hp["hairline_above_brow"] * cm),
               "back_edge_in_front_of_tragus_cm": (z_back_c - fr["tragus_z"]) / cm,
               "midline_arc_cm_coarse": arc})
    return mask, fr


def inside_patch(S, fr, hp):
    """Analytic patch predicate for arbitrary surface points S (N,3) in MakeHuman coords."""
    cm = 0.1
    x, y, z = S[:, 0], S[:, 1], S[:, 2]
    hl_y = fr["hairline_y"] + hp["corner_recession"] * cm * (x / (hp["front_width"] * cm / 2)) ** 2
    t = np.clip((fr["z_front"] - z) / max(1e-6, fr["z_front"] - fr["z_back_centre"]), 0, 1)
    half_w = hp["front_width"] * cm / 2 + (hp["back_width"] - hp["front_width"]) * cm / 2 * t
    z_back = fr["z_back_centre"] + hp["back_arc_sag"] * cm * np.clip(np.abs(x) / (hp["back_width"] * cm / 2), 0, 1) ** 2
    return (y >= hl_y) & (np.abs(x) <= half_w) & (z >= z_back)


def build_shell_raycast(raycast, centre, fr, hp, step_deg=1.2):
    """High-density shell independent of head topology.

    raycast(origins (N,3), dirs (N,3)) -> (hit points (N,3), normals (N,3), ok (N,)) on the scalp.
    Rays are cast from far outside towards the head centre on a (azimuth, elevation) grid;
    grid quads whose four corners satisfy inside_patch() become hair, and the boundary is
    closed by a vertical wall (the clipper step)."""
    az = np.radians(np.arange(-80, 80 + 1e-6, step_deg))
    el = np.radians(np.arange(-10, 89 + 1e-6, step_deg))
    A, Ee = np.meshgrid(az, el, indexing="ij")
    d = np.stack([np.cos(Ee) * np.sin(A), np.sin(Ee), np.cos(Ee) * np.cos(A)], -1).reshape(-1, 3)
    origins = centre + d * 3.0
    hit, nrm, ok = raycast(origins, -d)
    ins = ok & inside_patch(hit, fr, hp)
    na, ne = A.shape
    idx = -np.ones(len(d), int)
    keep_quads = []
    for i in range(na - 1):
        for j in range(ne - 1):
            q = [i * ne + j, (i + 1) * ne + j, (i + 1) * ne + j + 1, i * ne + j + 1]
            if all(ins[k] for k in q):
                keep_quads.append(q)
    used = sorted({k for q in keep_quads for k in q})
    for n, k in enumerate(used):
        idx[k] = n
    thick = hp["thickness"] * 0.1
    S, N = hit[used], nrm[used]
    outer = S + N * thick
    inner = S - N * hp.get("embed", 0.05) * 0.1
    n = len(used)
    verts = np.vstack([outer, inner])
    faces = [[idx[k] for k in q] for q in keep_quads]
    # orient faces outward (normal should agree with surface normal)
    for f in faces:
        a, b, c = outer[f[0]], outer[f[1]], outer[f[2]]
        if np.dot(np.cross(b - a, c - a), N[f[0]]) < 0:
            f.reverse()
    cnt = {}
    for f in faces:
        for a, b in zip(f, f[1:] + f[:1]):
            cnt.setdefault((min(a, b), max(a, b)), []).append((a, b))
    for uses in cnt.values():
        if len(uses) == 1:
            a, b = uses[0]
            faces.append([b, a, a + n, b + n])
    xz = verts[:, [0, 2]]
    uv = (xz - xz.min(0)) / max(1e-6, np.ptp(xz, 0).max())
    # midline arc length (front hairline -> back edge) for the report
    mids = sorted([k for k in used if abs(A.reshape(-1)[k]) < np.radians(step_deg / 2 + 1e-6)],
                  key=lambda k: Ee.reshape(-1)[k])
    arc = float(np.linalg.norm(np.diff(hit[mids], axis=0), axis=1).sum()) * 10 if len(mids) > 1 else 0.0
    return verts, faces, uv, {"n_quads": len(keep_quads), "midline_arc_cm": arc}


def vertex_normals(V, faces):
    N = np.zeros_like(V)
    for f in faces:
        f = np.asarray(f)
        for k in range(1, len(f) - 1):
            a, b, c = V[f[0]], V[f[k]], V[f[k + 1]]
            n = np.cross(b - a, c - a)
            N[[f[0], f[k], f[k + 1]]] += n
    n = np.linalg.norm(N, axis=1, keepdims=True)
    return N / np.maximum(n, 1e-12)


def build_shell(V, base, mask, hp):
    """Duplicate the scalp faces fully inside the mask, offset them outwards by the hair
    thickness and close the boundary with a vertical wall (the clipper step)."""
    faces = [base["faces"][fi][0] for fi in base["groups"]["body"]
             if all(mask[i] for i in base["faces"][fi][0])]
    N = vertex_normals(V, [base["faces"][fi][0] for fi in base["groups"]["body"]])
    used = sorted({i for f in faces for i in f})
    remap = {o: n for n, o in enumerate(used)}
    thick = hp["thickness"] * 0.1
    inner = V[used] - N[used] * hp.get("embed", 0.05) * 0.1    # sink slightly into the scalp
    outer = V[used] + N[used] * thick
    n = len(used)
    verts = np.vstack([outer, inner])
    out_faces = [[remap[i] for i in f] for f in faces]
    # boundary edges (edges used by exactly one face)
    cnt = {}
    for f in faces:
        for a, b in zip(f, f[1:] + f[:1]):
            k = (min(a, b), max(a, b))
            cnt[k] = cnt.get(k, []) + [(a, b)]
    for k, uses in cnt.items():
        if len(uses) == 1:
            a, b = uses[0]
            ra, rb = remap[a], remap[b]
            out_faces.append([rb, ra, ra + n, rb + n])   # wall, outward winding
    # top-down planar UVs for the curl texture
    xz = verts[:, [0, 2]]
    uv = (xz - xz.min(0)) / max(1e-6, np.ptp(xz, 0).max())
    return verts, out_faces, uv, used


def load_params(path):
    return json.load(open(path))
