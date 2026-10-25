"""Export the editable R9 HD head (+ eyes, teeth, hair shell) to a neutral NPZ (+ GLB).

  blender -b <r9>.blend --python tools/wrap/export_head.py -- --out head.npz [--glb head.glb] \
      [--frame blender|gltf|ue|ue_yfwd | --axes '-y,-x,z' --scale 100] \
      [--parts R9_Head,R9_Eyes,R9_Hair] [--textures <dir with r9_head_albedo.png ...>] \
      [--corr model/source/face_corr_mediapipe478.json]

NPZ layout (all positions in the requested frame, see tools/wrap/meshdata.py; README.md §2):
  <part>.positions (N,3) f8      unique (welded) vertices, Blender vertex order, REST pose
  <part>.normals   (N,3) f8      smooth vertex normals (Blender's, transformed as directions)
  <part>.faces     (F,3) i4      triangles (Blender loop triangles; reversed for left-handed frames)
  <part>.face_poly (F,)  i4      source polygon index of each triangle
  <part>.uv.<layer>(F,3,2) f4    per-corner UV, origin bottom-left
  <part>.mh_index  (N,)  i4      MakeHuman base-mesh vertex id (-1 if none)
  <part>.vg.<name> (N,)  f4      selected vertex-group weights (hair_patch)
  head.region      (N,)  i1      semantic label per head vertex, names in meta['regions']
  head.cavity      (N,)  ?       vertex normal ray hits the head itself within 3 cm (mouth bag, nostrils...)
  landmarks.mp478  (478,3) f8    MediaPipe FaceMesh points on the head (via mh_index barycentrics)
  features.names / features.positions  the 11 BASIC named features (tools/wrap/features.py)
  meta (json)                    frame, units, uv origin, textures, counts, source file, ...
The GLB (optional) is ALWAYS written in the glTF frame (metres, Y-up, face +Z), one mesh per
part, vertices split per (vertex, UV) as a GPU would need, with a '_SRC_VERTEX' float attribute
mapping each GLB vertex back to the NPZ vertex index.
"""
import argparse
import json
import os
import sys

import bpy
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "tools", "model"))
import features as FT  # noqa: E402
import glb as G  # noqa: E402
import meshdata as MD  # noqa: E402

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
ap = argparse.ArgumentParser()
ap.add_argument("--out", required=True)
ap.add_argument("--glb")
ap.add_argument("--frame", default="blender")
ap.add_argument("--axes")
ap.add_argument("--scale", type=float)
ap.add_argument("--parts", default="R9_Head,R9_Eyes,R9_Hair")   # missing objects are skipped
ap.add_argument("--head", default="R9_Head")
ap.add_argument("--head-uv", default="HeadUV")
ap.add_argument("--textures", default="")
ap.add_argument("--corr", default=os.path.join(REPO, "model", "source", "face_corr_mediapipe478.json"))
ap.add_argument("--eye-margin-mm", type=float, default=3.0)
ap.add_argument("--cavity-ray-m", type=float, default=0.03)
args = ap.parse_args(argv)

FD = MD.frame_def(args.frame, args.axes, args.scale)
GLTF = MD.frame_def("gltf")

for o in bpy.data.objects:
    if o.type == "ARMATURE":
        o.data.pose_position = "REST"
bpy.context.view_layer.update()
dg = bpy.context.evaluated_depsgraph_get()


def grab(ob):
    oe = ob.evaluated_get(dg)
    me = oe.to_mesh()
    n = len(me.vertices)
    co = np.empty(n * 3)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    if n != len(ob.data.vertices):            # a topology-changing modifier: use the base data
        oe.to_mesh_clear()
        oe, me = ob, ob.data
        co = np.array([v.co[:] for v in me.vertices])
        n = len(co)
    mw = np.array(ob.matrix_world)
    P = co @ mw[:3, :3].T + mw[:3, 3]
    vn = np.empty(n * 3)
    me.vertex_normals.foreach_get("vector", vn)
    nm = np.linalg.inv(mw[:3, :3]).T
    N = vn.reshape(-1, 3) @ nm.T
    N /= np.maximum(np.linalg.norm(N, axis=1, keepdims=True), 1e-30)
    me.calc_loop_triangles()
    nt = len(me.loop_triangles)
    tv = np.empty(nt * 3, np.int64)
    tl = np.empty(nt * 3, np.int64)
    tp = np.empty(nt, np.int64)
    me.loop_triangles.foreach_get("vertices", tv)
    me.loop_triangles.foreach_get("loops", tl)
    me.loop_triangles.foreach_get("polygon_index", tp)
    tv, tl = tv.reshape(-1, 3), tl.reshape(-1, 3)
    uvs = {}
    for layer in me.uv_layers:
        u = np.empty(len(me.loops) * 2)
        layer.data.foreach_get("uv", u)
        uvs[layer.name] = u.reshape(-1, 2)[tl].astype(np.float32)
    mh = np.full(n, -1, np.int64)
    if "mh_index" in me.attributes:
        me.attributes["mh_index"].data.foreach_get("value", mh)
    vgs = {}
    for vg in ob.vertex_groups:
        if vg.name in ("hair_patch",):
            w = np.zeros(n, np.float32)
            for v in ob.data.vertices:
                for g in v.groups:
                    if g.group == vg.index:
                        w[v.index] = g.weight
            vgs[vg.name] = w
    polys = [list(p.vertices) for p in ob.data.polygons]
    if oe is not ob:
        oe.to_mesh_clear()
    return {"P": P, "N": N, "F": tv, "poly": tp, "uv": uvs, "mh": mh, "vg": vgs, "polys": polys}


def head_semantics(h):
    P, N, F = h["P"], h["N"], h["F"]
    corr = json.load(open(args.corr))
    L = FT.landmarks_from_corr(corr, P, h["mh"])
    inv = {int(m): i for i, m in enumerate(h["mh"]) if m >= 0}
    ears = {}
    for side, tag in (("r", "R"), ("l", "L")):
        ears[tag] = np.array(sorted(inv[m] for m in FT.ear_mh_ids(side) if m in inv))
    # MakeHuman 'r' is the subject's right = blender -X; fix silently if the data says otherwise
    if P[ears["R"], 0].mean() > P[ears["L"], 0].mean():
        ears["R"], ears["L"] = ears["L"], ears["R"]
    feats, axes = FT.basic_features(L, P, ears["R"], ears["L"])
    lat, up, fwd = axes
    if lat[0] < 0.5:
        raise RuntimeError(f"landmark side check failed: lateral axis {lat} (expected ~+X)")
    # cavity: vertex normal ray hits the head itself
    bvh = BVHTree.FromPolygons([tuple(p) for p in P], h["polys"])
    cav = np.zeros(len(P), bool)
    for i in range(len(P)):
        o = Vector(P[i] + N[i] * 2e-4)
        loc, _, _, _ = bvh.ray_cast(o, Vector(N[i]), args.cavity_ray_m)
        cav[i] = loc is not None
    # boundary + 1 ring
    edges = {}
    for f in F:
        for a, b in ((f[0], f[1]), (f[1], f[2]), (f[2], f[0])):
            k = (min(a, b), max(a, b))
            edges[k] = edges.get(k, 0) + 1
    # count polygon (quad) edges instead of triangle edges: diagonals appear twice anyway
    bnd = np.zeros(len(P), bool)
    for (a, b), c in edges.items():
        if c == 1:
            bnd[a] = bnd[b] = True
    ring = bnd.copy()
    for (a, b) in edges:
        if bnd[a] or bnd[b]:
            ring[a] = ring[b] = True
    R = np.zeros(len(P), np.int8)
    rid = FT.REGION_ID
    centre = (feats["ear_top_R"] + feats["ear_top_L"]) / 2
    h_up = (P - centre) @ up
    eye_lvl = ((feats["eye_outer_R"] + feats["eye_outer_L"]) / 2 - centre) @ up
    chin_lvl = (feats["chin"] - centre) @ up
    R[h_up > eye_lvl] = rid["scalp"]
    R[h_up < chin_lvl] = rid["neck"]
    face = FT.face_mask(P, L, centre, axes)
    R[face] = rid["face"]
    R[ears["R"]] = rid["ear_R"]
    R[ears["L"]] = rid["ear_L"]
    m = args.eye_margin_mm / 1000
    for tag, ids in (("R", FT.EYE_R), ("L", FT.EYE_L)):
        c = L[ids].mean(0)
        d = FT.dist_to_polyline(P, L[ids], closed=True)
        R[(d < m) | (cav & (np.linalg.norm(P - c, axis=1) < 0.025))] = rid["eye_" + tag]
    mc = (L[13] + L[14]) / 2
    R[cav & (np.linalg.norm(P - mc, axis=1) < 0.035)] = rid["mouth_inner"]
    R[cav & (np.linalg.norm(P - L[1], axis=1) < 0.025) & (R != rid["mouth_inner"])] = rid["nose_cavity"]
    R[ring] = rid["neck_ring"]
    return L, feats, R, cav, bnd


def main():
    parts = [p for p in args.parts.split(",") if p and bpy.data.objects.get(p)]
    if args.head not in parts:
        raise SystemExit(f"head object {args.head!r} not found")
    arrays, meta = {}, {"format": "r9wrap-target/1", "frame": FD, "uv_origin": "bottom-left",
                        "source_blend": bpy.data.filepath, "parts": {}, "head_part": "head",
                        "head_uv": args.head_uv, "regions": FT.REGIONS}
    glb_meshes = []
    for name in parts:
        ob = bpy.data.objects[name]
        d = grab(ob)
        key = "head" if name == args.head else name.replace("R9_", "").lower()
        if key == "head":
            L, feats, R, cav, bnd = head_semantics(d)
            arrays["head.region"] = R
            arrays["head.cavity"] = cav
            arrays["head.boundary"] = bnd
            arrays["landmarks.mp478"] = MD.blender_to_frame(L, FD)
            names = FT.BASIC
            arrays["features.names"] = np.array(names)
            arrays["features.positions"] = MD.blender_to_frame(np.array([feats[n] for n in names]), FD)
            meta["region_counts"] = {n: int((R == i).sum()) for i, n in enumerate(FT.REGIONS)}
        arrays[f"{key}.positions"] = MD.blender_to_frame(d["P"], FD)
        arrays[f"{key}.normals"] = MD.dir_blender_to_frame(d["N"], FD)
        flip = FD["det"] < 0
        arrays[f"{key}.faces"] = MD.faces_for_frame(d["F"], FD).astype(np.int32)
        arrays[f"{key}.face_poly"] = d["poly"].astype(np.int32)
        for ln, uv in d["uv"].items():
            arrays[f"{key}.uv.{ln}"] = uv[:, ::-1] if flip else uv
        arrays[f"{key}.mh_index"] = d["mh"].astype(np.int32)
        for vn, w in d["vg"].items():
            arrays[f"{key}.vg.{vn}"] = w
        meta["parts"][key] = {"object": name, "verts": int(len(d["P"])), "tris": int(len(d["F"])),
                              "uv_layers": list(d["uv"].keys())}
        if args.glb:
            layer = args.head_uv if (key == "head" and args.head_uv in d["uv"]) else next(iter(d["uv"]), None)
            if layer:
                cuv = d["uv"][layer]
                keyv = np.concatenate([d["F"].reshape(-1, 1).astype(np.float64), np.round(cuv.reshape(-1, 2), 6)], 1)
                uniq, first, inv = np.unique(keyv, axis=0, return_index=True, return_inverse=True)
                src = d["F"].reshape(-1)[first]
                uv0 = MD.uv_from_bottom_left(cuv.reshape(-1, 2)[first], "top-left")
                faces = inv.reshape(-1, 3)
            else:
                src = np.arange(len(d["P"]))
                uv0, faces = None, d["F"]
            glb_meshes.append({"name": name, "positions": MD.blender_to_frame(d["P"][src], GLTF),
                               "normals": MD.dir_blender_to_frame(d["N"][src], GLTF), "uv0": uv0,
                               "faces": faces, "extra_attrs": {"_SRC_VERTEX": src.astype(np.float32)}})
    tex = {}
    if args.textures:
        for fn in sorted(os.listdir(args.textures)):
            if fn.endswith(".png"):
                tex[fn[:-4]] = os.path.abspath(os.path.join(args.textures, fn))
    meta["textures"] = tex
    meta["feature_names"] = FT.BASIC
    MD.save_npz(args.out, arrays, meta)
    print("EXPORT_HEAD", json.dumps({"out": args.out, "frame": FD["name"], "parts": meta["parts"],
                                     "regions": meta.get("region_counts")}))
    if args.glb:
        G.write_glb(args.glb, glb_meshes, extras={"source": bpy.data.filepath, "frame": "gltf"})
        print("WROTE", args.glb)


main()
