"""Render a built .blend from the pinhole cameras estimated by fit_face.py.

  blender -b X.blend --python tools/face/blender_render_cams.py -- --cams cams.json --outdir D

cams.json: [{"name": "photo.png", "cam": [rx, ry, rz, tx, ty, tz, f], "w": W, "h": H}, ...]
The camera model is fit_face.project(): OpenCV convention, applied to MakeHuman coordinates
(decimetres, Y up, face +Z). Blender scene coordinates are (x, -z, y) * 0.1 of those.
Used for analysis-by-synthesis: the same landmark detector is run on these renders and on
the photos, so its systematic bias cancels.
"""
import json
import os
import sys

import bpy
import numpy as np
from mathutils import Matrix

argv = sys.argv[sys.argv.index("--") + 1:]
opt = dict(zip(argv[0::2], argv[1::2]))
cams = json.load(open(opt["--cams"]))
outdir = opt["--outdir"]
os.makedirs(outdir, exist_ok=True)
scene = bpy.context.scene
scene.render.engine = "CYCLES"
scene.cycles.device = "CPU"
scene.cycles.samples = int(opt.get("--samples", 12))
scene.cycles.use_denoising = True
if "--textures" in opt:
    # textured review mode (same shading as tools/model/blender_render.py), hair visible
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "model"))
    import materials
    materials.apply_materials(opt["--textures"])
    for o in [o for o in scene.objects if o.type == "LIGHT"]:
        bpy.data.objects.remove(o)
else:
    # analysis-by-synthesis mode: untextured skin, helpers hidden (matches the correspondence renders)
    for o in scene.objects:
        if o.type == "MESH" and o.name not in ("R9_Head", "R9_Eyes", "R9_Body", "R9_Teeth"):
            o.hide_render = True
    mat = bpy.data.materials.new("abs_skin")
    mat.use_nodes = True
    mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.42, 0.27, 0.17, 1)
    for nm in ("R9_Head", "R9_Body"):
        o = bpy.data.objects[nm]
        o.data.materials.clear()
        o.data.materials.append(mat)
if not scene.world:
    scene.world = bpy.data.worlds.new("W")
scene.world.use_nodes = True
scene.world.node_tree.nodes["Background"].inputs[0].default_value = (0.5, 0.5, 0.5, 1)
scene.world.node_tree.nodes["Background"].inputs[1].default_value = 1.2

cd = bpy.data.cameras.new("AbsCam")
cam = bpy.data.objects.new("AbsCam", cd)
scene.collection.objects.link(cam)
scene.camera = cam
M = np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], float)   # mh -> blender axes
D = np.diag([1.0, -1.0, -1.0])                               # opencv cam -> blender cam


def rodrigues(r):
    r = np.asarray(r, float)
    th = np.linalg.norm(r)
    if th < 1e-12:
        return np.eye(3)
    k = r / th
    K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + np.sin(th) * K + (1 - np.cos(th)) * K @ K


for c in cams:
    R = rodrigues(c["cam"][:3])
    t = np.array(c["cam"][3:6])
    Rwc = D @ R @ M.T
    twc = 0.1 * D @ t
    Rcw = Rwc.T
    loc = -Rcw @ twc
    mw = Matrix.Identity(4)
    for i in range(3):
        for j in range(3):
            mw[i][j] = Rcw[i, j]
        mw[i][3] = loc[i]
    cam.matrix_world = mw
    cd.type = "PERSP"
    cd.sensor_fit = "HORIZONTAL"
    cd.sensor_width = 36.0
    cd.lens = c["cam"][6] * 36.0 / c["w"]
    cd.clip_start = 0.01
    cd.clip_end = 1.0e4
    scene.render.resolution_x = int(c["w"])
    scene.render.resolution_y = int(c["h"])
    scene.render.resolution_percentage = 100
    scene.render.filepath = os.path.join(outdir, os.path.splitext(os.path.basename(c["name"]))[0] + ".png")
    bpy.ops.render.render(write_still=True)
print("ABS_RENDER_DONE", len(cams))
