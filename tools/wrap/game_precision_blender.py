"""Blender half of tools/wrap/game_precision.py: decimate the R9 head parts to game vertex budgets.

  blender -b r9.blend --python tools/wrap/game_precision_blender.py -- --out-dir DIR \
      --budget "R9_Head,R9_Eyes,R9_Teeth=5376/1338" --budget "R9_Hair=1500/400"

Every budget group shares ONE collapse ratio (bisection) so that the summed GPU vertex count of
the group (vertices split per UV, which is what a vertex buffer holds) is the largest value
<= the budget; groups already under budget are kept as they are. Collapse decimation runs with
X symmetry and keeps the UV layers (interpolated), so the HD texture layout stays valid and the
game texture can be produced by resampling (game_precision.py). Writes DIR/lod0.blend,
DIR/lod1.blend (decimated parts under their original names, so tools/model/blender_render.py and
materials.py work unchanged) and DIR/lod_stats.json.
"""
import argparse
import json
import os
import sys

import bpy
import numpy as np

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
ap = argparse.ArgumentParser()
ap.add_argument("--out-dir", required=True)
ap.add_argument("--budget", action="append", required=True, help="'Obj1,Obj2=LOD0/LOD1' GPU vertices")
ap.add_argument("--no-symmetry", action="store_true")
args = ap.parse_args(argv)
os.makedirs(args.out_dir, exist_ok=True)
SRC = bpy.data.filepath
for o in bpy.data.objects:
    if o.type == "ARMATURE":
        o.data.pose_position = "REST"

RENDER_UV = {"R9_Head": "HeadUV", "R9_Hair": "HairUV"}


def uv_layer(me, name):
    want = RENDER_UV.get(name)
    if want and want in me.uv_layers:
        return me.uv_layers[want]
    return me.uv_layers.active or (me.uv_layers[0] if len(me.uv_layers) else None)


def counts(me, name):
    me.calc_loop_triangles()
    nv = len(me.vertices)
    layer = uv_layer(me, name)
    if layer is None:
        gpu = nv
    else:
        uv = np.empty(len(me.loops) * 2)
        layer.data.foreach_get("uv", uv)
        vi = np.empty(len(me.loops), np.int64)
        me.loops.foreach_get("vertex_index", vi)
        gpu = len(np.unique(np.concatenate([vi[:, None].astype(float), np.round(uv.reshape(-1, 2), 6)], 1), axis=0))
    return {"verts": nv, "gpu_verts": int(gpu), "tris": len(me.loop_triangles)}


def decimated_mesh(ob, ratio):
    md = ob.modifiers.new("gp_decimate", "DECIMATE")
    md.decimate_type = "COLLAPSE"
    md.ratio = ratio
    md.use_collapse_triangulate = True
    if not args.no_symmetry:
        md.use_symmetry = True
        md.symmetry_axis = "X"
    # evaluate decimation only (no armature deformation needed in rest pose, but keep order)
    dg = bpy.context.evaluated_depsgraph_get()
    me = bpy.data.meshes.new_from_object(ob.evaluated_get(dg))
    ob.modifiers.remove(md)
    return me


def group_count(objs, ratio):
    tot, per = 0, {}
    for ob in objs:
        if ratio >= 1.0:
            c = counts(ob.data, ob.name)
        else:
            me = decimated_mesh(ob, ratio)
            c = counts(me, ob.name)
            bpy.data.meshes.remove(me)
        per[ob.name] = c
        tot += c["gpu_verts"]
    return tot, per


def solve_ratio(objs, budget):
    full, per = group_count(objs, 1.0)
    if full <= budget:
        return 1.0, full, per
    lo, hi = 0.0, 1.0
    best = (lo, 0, None)
    for _ in range(16):
        r = (lo + hi) / 2
        n, p = group_count(objs, r)
        if n <= budget:
            best = (r, n, p)
            lo = r
        else:
            hi = r
        if best[2] is not None and budget - best[1] <= max(2, 0.005 * budget):
            break
    if best[2] is None:
        n, p = group_count(objs, max(lo, 0.01))
        best = (max(lo, 0.01), n, p)
    return best


groups = []
for spec in args.budget:
    names, b = spec.split("=")
    b0, b1 = (int(x) for x in b.split("/"))
    objs = [bpy.data.objects[n] for n in names.split(",") if bpy.data.objects.get(n)]
    groups.append((objs, (b0, b1), names))

stats = {"source_blend": SRC, "groups": [], "hd": {}}
for objs, _, _ in groups:
    for ob in objs:
        stats["hd"][ob.name] = counts(ob.data, ob.name)

def reload_src():
    bpy.ops.wm.open_mainfile(filepath=SRC)
    for o in bpy.data.objects:
        if o.type == "ARMATURE":
            o.data.pose_position = "REST"


for lod in (0, 1):
    plan = []
    for names, budgets in [(nm, b) for _, b, nm in groups]:
        objs = [bpy.data.objects[n] for n in names.split(",") if bpy.data.objects.get(n)]
        r, n, per = solve_ratio(objs, budgets[lod])
        plan.append(([o.name for o in objs], r))
        stats["groups"].append({"lod": lod, "objects": names, "budget_gpu_verts": budgets[lod], "ratio": r,
                                "gpu_verts": n, "per_object": per, "under_budget_kept": r >= 1.0})
    reload_src()                                   # apply the plan on a fresh copy of the file
    for names, r in plan:
        for name in names:
            ob = bpy.data.objects[name]
            if r < 1.0:
                me = decimated_mesh(ob, r)
                old = ob.data
                ob.data = me
                bpy.data.meshes.remove(old)
            uvl = uv_layer(ob.data, ob.name)
            if uvl is not None:
                ob.data.uv_layers.active = uvl
    out = os.path.join(args.out_dir, f"lod{lod}.blend")
    bpy.ops.wm.save_as_mainfile(filepath=out, copy=True)
    print("SAVED", out)
    reload_src()

json.dump(stats, open(os.path.join(args.out_dir, "lod_stats.json"), "w"), indent=1)
print("LOD_STATS", json.dumps([{k: g[k] for k in ("lod", "objects", "budget_gpu_verts", "ratio", "gpu_verts")}
                               for g in stats["groups"]]))
