"""Two-stage helper to map MediaPipe's 478 face landmarks onto the MakeHuman base mesh.

Stage "render": render the neutral head from a ring of yaw/pitch views and save the exact
camera matrices.
  blender -b X.blend --python tools/face/blender_correspondence.py -- render --outdir D
Stage "raycast": given landmarks.json (from tools/face/landmarks.py run on those renders),
cast a ray through every landmark pixel and record the hit as barycentric coordinates on
base-mesh vertex indices (R9_Head carries the 'mh_index' attribute). Per landmark we keep
the median 3D hit across views and the spread (contour landmarks slide with the view, so
their spread is large and the fitter down-weights them).
  blender -b X.blend --python tools/face/blender_correspondence.py -- raycast --outdir D --landmarks D/lm.json --out corr.json
"""
import json
import math
import os
import sys

import bpy
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree

argv = sys.argv[sys.argv.index("--") + 1:]
mode = argv[0]
opt = dict(zip(argv[1::2], argv[2::2]))
outdir = opt["--outdir"]
os.makedirs(outdir, exist_ok=True)
scene = bpy.context.scene
head = bpy.data.objects["R9_Head"]
RES = 768
VIEWS = [(y, p) for y in (-30, -15, 0, 15, 30) for p in (-8, 0, 8)]


def head_center():
    co = np.array([head.matrix_world @ v.co for v in head.data.vertices])
    top = co[:, 2].max()
    return Vector((0.0, co[:, 1].mean(), top - 0.13))


def setup_camera():
    cd = bpy.data.cameras.get("CorrCam") or bpy.data.cameras.new("CorrCam")
    cam = bpy.data.objects.get("CorrCam") or bpy.data.objects.new("CorrCam", cd)
    if cam.name not in scene.collection.objects:
        scene.collection.objects.link(cam)
    cd.type = "PERSP"
    cd.lens = 85
    cd.sensor_width = 36
    cd.sensor_fit = "HORIZONTAL"
    scene.camera = cam
    scene.render.resolution_x = scene.render.resolution_y = RES
    scene.render.resolution_percentage = 100
    return cam


def place(cam, yaw, pitch, c):
    a, e = math.radians(yaw), math.radians(pitch)
    d = Vector((math.sin(a) * math.cos(e), -math.cos(a) * math.cos(e), math.sin(e)))
    cam.location = c + d * 0.85
    cam.rotation_euler = (c - cam.location).normalized().to_track_quat("-Z", "Y").to_euler()
    bpy.context.view_layer.update()


if mode == "render":
    for o in list(scene.objects):
        if o.type == "MESH" and o.name not in ("R9_Head", "R9_Eyes", "R9_Body"):
            o.hide_render = True
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 16
    scene.cycles.device = "CPU"
    if not scene.world:
        scene.world = bpy.data.worlds.new("W")
    scene.world.use_nodes = True
    scene.world.node_tree.nodes["Background"].inputs[1].default_value = 1.0
    for m in head.data.materials:
        pass
    mat = bpy.data.materials.new("corr_skin")
    mat.use_nodes = True
    mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.55, 0.38, 0.28, 1)
    for nm in ("R9_Head", "R9_Body"):
        o = bpy.data.objects[nm]
        o.data.materials.clear()
        o.data.materials.append(mat)
    ld = bpy.data.lights.new("k", "AREA")
    ld.energy = 120
    ld.size = 1.0
    lo = bpy.data.objects.new("k", ld)
    scene.collection.objects.link(lo)
    cam = setup_camera()
    c = head_center()
    lo.location = c + Vector((-0.5, -1.0, 0.4))
    lo.rotation_euler = (c - lo.location).normalized().to_track_quat("-Z", "Y").to_euler()
    cams = {}
    for yaw, pitch in VIEWS:
        place(cam, yaw, pitch, c)
        name = f"v_y{yaw:+d}_p{pitch:+d}.png"
        scene.render.filepath = os.path.join(outdir, name)
        bpy.ops.render.render(write_still=True)
        cams[name] = {"loc": list(cam.location), "mat": [list(r) for r in cam.matrix_world]}
    json.dump({"res": RES, "lens": 85, "sensor": 36, "cams": cams}, open(os.path.join(outdir, "cams.json"), "w"))
    print("RENDER_DONE", len(cams))

elif mode == "raycast":
    cams = json.load(open(os.path.join(outdir, "cams.json")))
    lms = json.load(open(opt["--landmarks"]))
    dg = bpy.context.evaluated_depsgraph_get()
    he = head.evaluated_get(dg)
    me = he.to_mesh()
    mw = he.matrix_world
    verts = [mw @ v.co for v in me.vertices]
    tris, tri_src = [], []
    for p in me.polygons:
        vs = list(p.vertices)
        for k in range(1, len(vs) - 1):
            tris.append((vs[0], vs[k], vs[k + 1]))
    bvh = BVHTree.FromPolygons(verts, tris)
    mh_index = [0] * len(me.vertices)
    me.attributes["mh_index"].data.foreach_get("value", mh_index)
    hits = {i: [] for i in range(478)}
    f = cams["lens"] / cams["sensor"] * cams["res"]
    for name, cinfo in cams["cams"].items():
        key = [k for k in lms if k.endswith(name)]
        if not key or lms[key[0]] is None:
            continue
        L = lms[key[0]]["landmarks"]
        M = np.array(cinfo["mat"])
        origin = Vector(cinfo["loc"])
        for i, (x, y, _) in enumerate(L):
            dx = (x - cams["res"] / 2) / f
            dy = -(y - cams["res"] / 2) / f
            d_cam = np.array([dx, dy, -1.0])
            d_w = M[:3, :3] @ d_cam
            loc, nrm, ti, dist = bvh.ray_cast(origin, Vector(d_w).normalized())
            if loc is None:
                continue
            hits[i].append((np.array(loc), ti))
    out = []
    for i in range(478):
        if not hits[i]:
            out.append(None)
            continue
        P = np.array([h[0] for h in hits[i]])
        med = np.median(P, axis=0)
        spread = float(np.median(np.linalg.norm(P - med, axis=1)))
        loc, nrm, ti, dist = bvh.find_nearest(Vector(med))
        a, b, c = (np.array(verts[j]) for j in tris[ti])
        # barycentric of loc in triangle abc
        v0, v1, v2 = b - a, c - a, np.array(loc) - a
        d00, d01, d11 = v0 @ v0, v0 @ v1, v1 @ v1
        d20, d21 = v2 @ v0, v2 @ v1
        den = d00 * d11 - d01 * d01
        wb = (d11 * d20 - d01 * d21) / den
        wc = (d00 * d21 - d01 * d20) / den
        wa = 1 - wb - wc
        out.append({"v": [mh_index[j] for j in tris[ti]], "w": [float(wa), float(wb), float(wc)],
                    "spread_m": spread, "n_views": len(hits[i])})
    json.dump(out, open(opt["--out"], "w"))
    ok = [o for o in out if o]
    print("RAYCAST_DONE", len(ok), "median spread mm", 1000 * float(np.median([o["spread_m"] for o in ok])))
