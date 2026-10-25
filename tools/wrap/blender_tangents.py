"""MikkTSpace tangents for a plain vertex buffer, computed by Blender (mesh.calc_tangents).

  blender -b --factory-startup --python tools/wrap/blender_tangents.py -- in.npz out.npz

in.npz : positions (N,3), faces (F,3), uv (N,2), normals (N,3) - all in the DONOR's native numbers
         (frame, winding and UV origin exactly as the engine stores them), so the result is what a
         MikkTSpace pass inside the engine computes from the same buffers.
out.npz: tangents (N,4) = xyz + bitangent sign (w), info (json): per-vertex disagreement between
         the loops of one vertex (MikkTSpace works per face corner; a GPU vertex gets the average).
"""
import json
import sys

import bpy
import numpy as np

src, dst = sys.argv[sys.argv.index("--") + 1:][:2]
z = np.load(src)
P, F, uv, N = z["positions"], z["faces"].astype(np.int64), z["uv"].astype(np.float64), z["normals"]
me = bpy.data.meshes.new("t")
me.vertices.add(len(P))
me.vertices.foreach_set("co", P.astype(np.float32).ravel())
me.loops.add(F.size)
me.loops.foreach_set("vertex_index", F.ravel().astype(np.int32))
me.polygons.add(len(F))
me.polygons.foreach_set("loop_start", (np.arange(len(F)) * 3).astype(np.int32))
me.polygons.foreach_set("loop_total", np.full(len(F), 3, np.int32))
me.update(calc_edges=True)
me.polygons.foreach_set("use_smooth", np.ones(len(F), bool))
uvl = me.uv_layers.new(name="UV0")
uvl.data.foreach_set("uv", uv[F.ravel()].astype(np.float32).ravel())
me.normals_split_custom_set_from_vertices([tuple(n) for n in N])
me.calc_tangents(uvmap="UV0")
nl = len(me.loops)
t = np.empty(nl * 3, np.float32)
s = np.empty(nl, np.float32)
me.loops.foreach_get("tangent", t)
me.loops.foreach_get("bitangent_sign", s)
t = t.reshape(-1, 3)
vi = F.ravel()
T = np.zeros((len(P), 3))
np.add.at(T, vi, t)
S = np.zeros(len(P))
np.add.at(S, vi, s)
T /= np.maximum(np.linalg.norm(T, axis=1, keepdims=True), 1e-30)
w = np.where(S < 0, -1.0, 1.0)
dis = np.degrees(np.arccos(np.clip((t * T[vi]).sum(1), -1, 1)))
mixed_sign = int((np.abs(S) < np.bincount(vi, minlength=len(P))).sum())
info = {"loops": nl, "max_loop_vs_vertex_deg": float(dis.max()), "p99_loop_vs_vertex_deg": float(np.percentile(dis, 99)),
        "vertices_with_mixed_sign": mixed_sign, "blender": bpy.app.version_string}
np.savez(dst, tangents=np.concatenate([T, w[:, None]], 1), info=np.array(json.dumps(info)))
print("TANGENTS", json.dumps(info))
