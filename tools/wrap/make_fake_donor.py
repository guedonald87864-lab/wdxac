"""Build a SYNTHETIC donor head for testing tools/wrap/wrap.py (Blender script).

  blender -b --python tools/wrap/make_fake_donor.py -- --out <dir> [--lod0-verts 5400] \
      [--lod1-verts 1340] [--frame ue] [--seed 7]

The donor is deliberately unlike our R9 head in everything that matters to a wrap:
  * another face: a MakeHuman head with different macros/face modifiers (older, caucasian,
    narrower nose with a hump, smaller mouth, longer chin, bigger ears...)
  * another topology: subdivided, triangulated and collapse-decimated to ~5,400 GPU vertices
    (vertex count AFTER splitting at UV seams, like a game vertex buffer) - no MakeHuman vertex
    survives as such, connectivity is irregular
  * another UV layout: a fresh angle-based unwrap with its own seams (face mask island, head
    shell opened along the back midline, each ear its own island)
  * a LOD1 (~1,340 GPU vertices) collapse-decimated from LOD0 with its UVs carried along
  * dummy 4-influence skin weights (6 bones), quantised to 1/255 like a game asset
  * another frame: UE centimetres (left-handed, X forward), plus a random rigid offset, UV
    origin top-left
Outputs in <dir>: donor.npz (the donor, tools/wrap NPZ donor schema - README §3),
donor_features_gt.json (ground-truth feature vertex ids, the equivalent of a careful manual
pick), donor_gt.npz (the MakeHuman source surface + donor vertices in the Blender frame, so the
test can measure SEMANTIC correspondence error, not only surface distance).
"""
import argparse
import json
import math
import os
import sys

import bmesh
import bpy
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "tools", "model"))
import features as FT  # noqa: E402
import meshdata as MD  # noqa: E402
import mh_engine as E  # noqa: E402

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
ap = argparse.ArgumentParser()
ap.add_argument("--out", required=True)
ap.add_argument("--params")
ap.add_argument("--lod0-verts", type=int, default=5400)
ap.add_argument("--lod1-verts", type=int, default=1340)
ap.add_argument("--frame", default="ue")
ap.add_argument("--seed", type=int, default=7)
ap.add_argument("--corr", default=os.path.join(REPO, "model", "source", "face_corr_mediapipe478.json"))
args = ap.parse_args(argv)
os.makedirs(args.out, exist_ok=True)

DEFAULT_PARAMS = {
    "macro": {"gender": 1.0, "age_years": 38, "african": 0.1, "caucasian": 0.85, "asian": 0.05,
              "muscle": 0.5, "weight": 0.62, "height": 0.6, "proportions": 0.45},
    "modifiers": {
        "nose/nose-scale-horiz-decr|incr": -0.5, "nose/nose-scale-vert-decr|incr": 0.4,
        "nose/nose-hump-decr|incr": 0.6, "mouth/mouth-scale-horiz-decr|incr": -0.3,
        "mouth/mouth-upperlip-volume-decr|incr": -0.5, "chin/chin-prominent-decr|incr": 0.6,
        "chin/chin-width-decr|incr": 0.4, "head/head-oval": 0.6, "head/head-scale-vert-decr|incr": 0.3,
        "eyes/r-eye-scale-decr|incr": -0.3, "eyes/l-eye-scale-decr|incr": -0.3,
        "eyes/r-eye-trans-in|out": 0.3, "eyes/l-eye-trans-in|out": 0.3,
        "ears/r-ear-scale-decr|incr": 0.4, "ears/l-ear-scale-decr|incr": 0.4,
        "forehead/forehead-scale-vert-decr|incr": 0.4,
        "cheek/l-cheek-bones-decr|incr": 0.5, "cheek/r-cheek-bones-decr|incr": 0.5,
    },
}
PARAMS = json.load(open(args.params)) if args.params else DEFAULT_PARAMS


def to_bl(v):
    v = np.asarray(v, float)
    return np.stack([v[..., 0], -v[..., 2], v[..., 1]], axis=-1) * 0.1


bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
BASE = E.load_obj()
VMH, _ = E.build(PARAMS, BASE)
VB = to_bl(VMH)
JMH, SKEL = E.joint_positions(VMH)
J = {k: to_bl(p) for k, p in JMH.items()}


# ----------------------------------------------------------------------------- source head (MH topology)
def head_faces():
    b = SKEL["bones"]["neck01"]
    h, t = J[b["head"]], J[b["tail"]]
    cut = h + (t - h) * 0.35
    ax = (t - h) / np.linalg.norm(t - h)
    return [fi for fi in BASE["groups"]["body"] if (VB[BASE["faces"][fi][0]].mean(0) - cut) @ ax > 0], cut, ax


def link(name, me):
    ob = bpy.data.objects.new(name, me)
    scene.collection.objects.link(ob)
    return ob


hf, CUT, AX = head_faces()
used = sorted({i for fi in hf for i in BASE["faces"][fi][0]})
remap = {o: n for n, o in enumerate(used)}
me = bpy.data.meshes.new("src")
me.from_pydata([tuple(VB[i]) for i in used], [], [[remap[i] for i in BASE["faces"][fi][0]] for fi in hf])
me.update()
src = link("src", me)
SRC_P = VB[used]
SRC_MH = np.array(used)


def select_only(ob):
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    ob.select_set(True)
    bpy.context.view_layer.objects.active = ob


def evaluated_copy(ob, name, mods):
    """Duplicate `ob`, add modifiers [(type, {attr: val})], apply them -> new object."""
    m2 = ob.data.copy()
    o2 = link(name, m2)
    for typ, kw in mods:
        md = o2.modifiers.new(typ, typ)
        for k, v in kw.items():
            setattr(md, k, v)
    dg = bpy.context.evaluated_depsgraph_get()
    oe = o2.evaluated_get(dg)
    m3 = bpy.data.meshes.new_from_object(oe)
    o2.modifiers.clear()
    o2.data = m3
    bpy.data.meshes.remove(m2)
    return o2


def split_count(me_):
    uvl = me_.uv_layers.active
    uv = np.empty(len(me_.loops) * 2)
    uvl.data.foreach_get("uv", uv)
    vi = np.empty(len(me_.loops), np.int64)
    me_.loops.foreach_get("vertex_index", vi)
    key = np.concatenate([vi[:, None].astype(float), np.round(uv.reshape(-1, 2), 6)], 1)
    return len(np.unique(key, axis=0))


EAR_MH = set(FT.ear_mh_ids("r")) | set(FT.ear_mh_ids("l"))
_corr = json.load(open(args.corr))
_L = FT.landmarks_from_corr(_corr, SRC_P, SRC_MH)
_inv = {int(m): i for i, m in enumerate(SRC_MH)}
_ears = {t: np.array(sorted(_inv[m] for m in FT.ear_mh_ids(s) if m in _inv)) for s, t in (("r", "R"), ("l", "L"))}
_f, _ax = FT.basic_features(_L, SRC_P, _ears["R"], _ears["L"])
SRC_FACE = FT.face_mask(SRC_P, _L, (_f["ear_top_R"] + _f["ear_top_L"]) / 2, _ax)


def unwrap(ob):
    """Fresh, game-like UV layout: 3 islands (head cut open along the back midline, each ear cut
    off at its root), angle-based unwrap, islands packed. Nothing is shared with our HeadUV."""
    import heapq
    me_ = ob.data
    bm = bmesh.new()
    bm.from_mesh(me_)
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    co = np.array([v.co[:] for v in bm.verts])
    # ear membership through the nearest source (MakeHuman) vertex
    near = np.array([np.argmin(np.linalg.norm(SRC_P - c, axis=1)) for c in co])
    ear = np.array([SRC_MH[i] in EAR_MH for i in near])
    face = SRC_FACE[near]
    for f in bm.faces:            # island id: 1 = ear, 2 = face mask, 0 = rest of the head
        f.material_index = 1 if all(ear[v.index] for v in f.verts) else (2 if all(face[v.index] for v in f.verts) else 0)
    for e in bm.edges:
        e.seam = len(e.link_faces) == 2 and (e.link_faces[0].material_index != e.link_faces[1].material_index)
    # back-midline path: Dijkstra from the back of the neck border to the crown
    yc = np.median(co[:, 1])
    bverts = [v.index for v in bm.verts if v.is_boundary and co[v.index, 1] > yc]
    start = min(bverts, key=lambda i: abs(co[i, 0]))
    mid = np.abs(co[:, 0]) < 0.01
    goal = int(np.argmax(np.where(mid, co[:, 2], -np.inf)))
    dist = {start: 0.0}
    prev = {}
    pq = [(0.0, start)]
    while pq:
        d, u = heapq.heappop(pq)
        if u == goal:
            break
        if d > dist.get(u, np.inf):
            continue
        for e in bm.verts[u].link_edges:
            w = e.other_vert(bm.verts[u]).index
            m = (co[u] + co[w]) / 2
            if m[1] < yc and w != goal:
                continue
            c = np.linalg.norm(co[u] - co[w]) * (1 + abs(m[0]) / 0.002)
            if d + c < dist.get(w, np.inf):
                dist[w] = d + c
                prev[w] = (u, e)
                heapq.heappush(pq, (d + c, w))
    v = goal
    while v in prev:
        u, e = prev[v]
        e.seam = True
        v = u
    for f in bm.faces:
        f.material_index = 0
    bm.to_mesh(me_)
    bm.free()
    select_only(ob)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.uv.unwrap(method="ANGLE_BASED", margin=0.02)
    bpy.ops.object.mode_set(mode="OBJECT")


def bisect(make, target, lo, hi, tol=0.02, iters=14):
    best = None
    for _ in range(iters):
        r = (lo + hi) / 2
        ob, n = make(r)
        if best is None or abs(n - target) < abs(best[1] - target):
            if best:
                bpy.data.objects.remove(best[0])
            best = (ob, n, r)
        else:
            bpy.data.objects.remove(ob)
        if abs(n - target) <= tol * target:
            break
        if n > target:
            hi = r
        else:
            lo = r
    return best


sub = evaluated_copy(src, "sub", [("SUBSURF", {"subdivision_type": "SIMPLE", "levels": 1, "render_levels": 1}),
                                  ("TRIANGULATE", {})])


def make_lod0(r):
    ob = evaluated_copy(sub, "lod0_try", [("DECIMATE", {"decimate_type": "COLLAPSE", "ratio": r})])
    unwrap(ob)
    return ob, split_count(ob.data)


lod0, n0, r0 = bisect(make_lod0, args.lod0_verts, 0.05, 0.9)
lod0.name = "LOD0"


def make_lod1(r):
    ob = evaluated_copy(lod0, "lod1_try", [("DECIMATE", {"decimate_type": "COLLAPSE", "ratio": r,
                                                          "use_collapse_triangulate": True})])
    return ob, split_count(ob.data)


lod1, n1, r1 = bisect(make_lod1, args.lod1_verts, 0.05, 0.9)
lod1.name = "LOD1"
print("FAKE_DONOR lod0", n0, "ratio", r0, "lod1", n1, "ratio", r1)


# ----------------------------------------------------------------------------- GPU-style vertex buffers
def gpu_buffers(ob):
    me_ = ob.data
    me_.calc_loop_triangles()
    nl = len(me_.loops)
    uv = np.empty(nl * 2)
    me_.uv_layers.active.data.foreach_get("uv", uv)
    uv = uv.reshape(-1, 2)
    vi = np.empty(nl, np.int64)
    me_.loops.foreach_get("vertex_index", vi)
    cn = np.empty(nl * 3)
    me_.corner_normals.foreach_get("vector", cn)
    cn = cn.reshape(-1, 3)
    key = np.concatenate([vi[:, None].astype(float), np.round(uv, 6)], 1)
    _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    # order GPU vertices by first loop use (stable and "natural")
    order = np.argsort(first)
    rank = np.empty(len(order), np.int64)
    rank[order] = np.arange(len(order))
    gid = rank[inv.ravel()]
    first = first[order]
    co = np.empty(len(me_.vertices) * 3)
    me_.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    tl = np.empty(len(me_.loop_triangles) * 3, np.int64)
    me_.loop_triangles.foreach_get("loops", tl)
    faces = gid[tl].reshape(-1, 3)
    return co[vi[first]], uv[first], cn[first], faces


P0, UV0, N0, F0 = gpu_buffers(lod0)
P1, UV1, N1, F1 = gpu_buffers(lod1)

# ----------------------------------------------------------------------------- landmarks / features of the source face
corr = json.load(open(args.corr))
L = FT.landmarks_from_corr(corr, SRC_P, SRC_MH)
inv_mh = {int(m): i for i, m in enumerate(SRC_MH)}
ears = {t: np.array(sorted(inv_mh[m] for m in FT.ear_mh_ids(s) if m in inv_mh)) for s, t in (("r", "R"), ("l", "L"))}
feats, (lat, up, fwd) = FT.basic_features(L, SRC_P, ears["R"], ears["L"])
pts = dict(feats)
for k in FT.EXTRA_MP:
    pts[k] = L[int(k[3:])]


def nearest_vertex(p, P):
    return int(np.argmin(np.linalg.norm(P - p, axis=1)))


feat_ids = {k: nearest_vertex(p, P0) for k, p in pts.items()}
feat_err_mm = {k: float(np.linalg.norm(P0[feat_ids[k]] - p) * 1000) for k, p in pts.items()}


# ----------------------------------------------------------------------------- dummy skin weights
def skin(P):
    eye_c = {"eye_r": L[FT.EYE_R].mean(0), "eye_l": L[FT.EYE_L].mean(0)}
    h = (P - CUT) @ AX                                     # height above the neck cut
    mouth_h = (L[13] - CUT) @ AX
    ear_f = ((P[ears["R"][0]] + P[ears["L"][0]]) / 2 if False else (feats["ear_top_R"] + feats["ear_top_L"]) / 2)
    front = (P - ear_f) @ fwd
    w = {"neck_01": np.clip(1 - h / 0.04, 0, 1),
         "jaw": np.clip((mouth_h - h) / 0.02, 0, 1) * np.clip(front / 0.03, 0, 1) * np.clip((h - 0.01) / 0.02, 0, 1),
         "eye_r": np.clip(1 - np.linalg.norm(P - eye_c["eye_r"], axis=1) / 0.015, 0, 1),
         "eye_l": np.clip(1 - np.linalg.norm(P - eye_c["eye_l"], axis=1) / 0.015, 0, 1)}
    names = ["root", "neck_01", "head", "jaw", "eye_l", "eye_r"]
    W = np.zeros((len(P), len(names)))
    W[:, 1], W[:, 3], W[:, 4], W[:, 5] = w["neck_01"], w["jaw"], w["eye_l"], w["eye_r"]
    W[:, 2] = np.clip(1 - W.sum(1), 0.05, 1)
    W[:, 0] = 0.02 * w["neck_01"]
    idx = np.argsort(-W, axis=1)[:, :4]
    top = np.take_along_axis(W, idx, 1)
    top /= top.sum(1, keepdims=True)
    q = np.floor(top * 255).astype(int)
    q[:, 0] += 255 - q.sum(1)
    return idx.astype(np.uint16), (q / 255.0).astype(np.float32), names


J0, W0, BONES = skin(P0)
J1, W1, _ = skin(P1)

# ----------------------------------------------------------------------------- donor frame + random offset
rng = np.random.default_rng(args.seed)
axis = rng.normal(size=3)
axis /= np.linalg.norm(axis)
ang = math.radians(6.0)
K = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
Roff = np.eye(3) + math.sin(ang) * K + (1 - math.cos(ang)) * K @ K
toff = np.array([0.03, -0.02, 0.75])            # metres, in the blender frame
FD = MD.frame_def(args.frame)


def place(P):
    return MD.blender_to_frame(P @ Roff.T + toff, FD)


def place_dir(N):
    return MD.dir_blender_to_frame(N @ Roff.T, FD)


arrays = {
    "positions": place(P0), "faces": MD.faces_for_frame(F0, FD).astype(np.int32),
    "uv0": MD.uv_from_bottom_left(UV0, "top-left").astype(np.float32), "normals": place_dir(N0).astype(np.float32),
    "joints": J0, "weights": W0,
    "lod1.positions": place(P1), "lod1.faces": MD.faces_for_frame(F1, FD).astype(np.int32),
    "lod1.uv0": MD.uv_from_bottom_left(UV1, "top-left").astype(np.float32),
    "lod1.normals": place_dir(N1).astype(np.float32), "lod1.joints": J1, "lod1.weights": W1,
}
meta = {"format": "wrapmesh-donor/1", "frame": FD, "uv_origin": "top-left", "bones": BONES,
        "synthetic": True, "params": PARAMS, "lod0_gpu_verts": int(len(P0)), "lod1_gpu_verts": int(len(P1)),
        "decimate_ratio": [r0, r1]}
MD.save_npz(os.path.join(args.out, "donor.npz"), arrays, meta)
json.dump({"features": {k: feat_ids[k] for k in FT.BASIC + list(FT.EXTRA_MP)},
           "snap_error_mm": feat_err_mm, "note": "ground truth from the MakeHuman source surface; ids index donor.npz LOD0"},
          open(os.path.join(args.out, "donor_features_gt.json"), "w"), indent=1)
src.data.calc_loop_triangles()
tv = np.empty(len(src.data.loop_triangles) * 3, np.int64)
src.data.loop_triangles.foreach_get("vertices", tv)
np.savez_compressed(os.path.join(args.out, "donor_gt.npz"), src_positions=SRC_P, src_faces=tv.reshape(-1, 3),
                    src_mh_index=SRC_MH, lod0_blender=P0, lod1_blender=P1, offset_R=Roff, offset_t=toff)
print("FAKE_DONOR", json.dumps({"lod0_verts": int(len(P0)), "lod0_tris": int(len(F0)), "lod1_verts": int(len(P1)),
                                "lod1_tris": int(len(F1)), "src_verts": int(len(SRC_P)),
                                "feature_snap_max_mm": max(feat_err_mm.values())}))
