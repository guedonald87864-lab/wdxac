"""Render-space check of a texture transfer (Blender script, Workbench, flat texture colour).

  blender -b --factory-startup --python tools/wrap/render_compare.py -- --target head.npz \
      --wrap wrapped.npz --target-tex r9_head_albedo.png --donor-tex donor_albedo_2048.png \
      [--donor-tex donor_albedo_256_astc4x4_decoded.png ...] --out-dir DIR [--res 640] \
      [--views front,l45,lprof,r45]

Our HD head (its own UV + texture) and the WRAPPED donor (donor UVs + baked texture) are
rendered from the same orthographic cameras with unlit texture colour (Workbench FLAT/TEXTURE,
Standard view transform, 8x AA, transparent film). Blender's own UV handling and texture
sampling are used, so this is independent of the sampling code in transfer_texture.py.
Writes <view>_target.png, <view>_donor<k>.png and render_compare.json (camera, face box of the
MediaPipe face oval per view) - the comparison itself is done by synthetic.py / any caller.
"""
import argparse
import json
import math
import os
import sys

import bpy
import numpy as np
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Vector

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import features as FT  # noqa: E402
import meshdata as MD  # noqa: E402

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
ap = argparse.ArgumentParser()
ap.add_argument("--target", required=True)
ap.add_argument("--wrap", required=True)
ap.add_argument("--target-tex", required=True)
ap.add_argument("--donor-tex", action="append", required=True)
ap.add_argument("--lod", default="lod0")
ap.add_argument("--out-dir", required=True)
ap.add_argument("--res", type=int, default=640)
ap.add_argument("--views", default="front,l45,lprof,r45")
a = ap.parse_args(argv)
os.makedirs(a.out_dir, exist_ok=True)

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.engine = "BLENDER_WORKBENCH"
scene.display.shading.light = "FLAT"
scene.display.shading.color_type = "TEXTURE"
scene.display.render_aa = "8"
scene.view_settings.view_transform = "Standard"
scene.view_settings.look = "None"
scene.render.film_transparent = True
scene.render.resolution_x = scene.render.resolution_y = a.res
scene.render.image_settings.color_mode = "RGBA"


def mesh_obj(name, P, F, corner_uv):
    me = bpy.data.meshes.new(name)
    me.vertices.add(len(P))
    me.vertices.foreach_set("co", np.asarray(P, np.float32).ravel())
    me.loops.add(F.size)
    me.loops.foreach_set("vertex_index", F.ravel().astype(np.int32))
    me.polygons.add(len(F))
    me.polygons.foreach_set("loop_start", (np.arange(len(F)) * 3).astype(np.int32))
    me.polygons.foreach_set("loop_total", np.full(len(F), 3, np.int32))
    me.update(calc_edges=True)
    uvl = me.uv_layers.new(name="UV")
    uvl.data.foreach_set("uv", np.asarray(corner_uv, np.float32).reshape(-1, 2).ravel())
    ob = bpy.data.objects.new(name, me)
    scene.collection.objects.link(ob)
    return ob


def material(name, path):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = bpy.data.images.load(path)
    tex.interpolation = "Linear"
    bs = nt.nodes.get("Principled BSDF")
    nt.links.new(tex.outputs["Color"], bs.inputs["Base Color"])
    nt.nodes.active = tex
    return m


T, tm = MD.load_npz(a.target)
fd = tm["frame"]
tP = MD.frame_to_blender(T["head.positions"], fd)
tF = T["head.faces"].astype(np.int64)
tuv = T[f"head.uv.{tm.get('head_uv', 'HeadUV')}"]
R, rm = MD.load_npz(a.wrap)
dP = R[f"{a.lod}.work_positions"]
dF = R[f"{a.lod}.faces"].astype(np.int64)
duv = R[f"{a.lod}.uv0"] if f"{a.lod}.uv0" in R else None
duv = MD.uv_to_bottom_left(duv[dF] if duv is not None else R[f"{a.lod}.uv_corner"], rm["uv_origin"])
tob = mesh_obj("target", tP, tF, tuv)
tob.data.materials.append(material("t", a.target_tex))
dob = mesh_obj("donor", dP, dF, duv)
dmats = [material(f"d{k}", p) for k, p in enumerate(a.donor_tex)]
dob.data.materials.append(dmats[0])

L = MD.frame_to_blender(T["landmarks.mp478"], fd)
oval = L[FT.FACE_OVAL]
centre = Vector(((tP.min(0) + tP.max(0)) / 2).tolist())
span = float(np.ptp(tP, axis=0).max()) * 1.1
cam_d = bpy.data.cameras.new("cam")
cam_d.type = "ORTHO"
cam_d.ortho_scale = span
cam = bpy.data.objects.new("cam", cam_d)
scene.collection.objects.link(cam)
scene.camera = cam
VIEWS = {"front": 0, "l45": 45, "lprof": 90, "r45": -45, "rprof": -90}
info = {"views": {}, "res": a.res}
for v in a.views.split(","):
    az = math.radians(VIEWS[v])
    d = Vector((math.sin(az), -math.cos(az), 0.0))
    cam.location = centre + d * 1.0
    cam.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
    bpy.context.view_layer.update()
    pts = [world_to_camera_view(scene, cam, Vector(p.tolist())) for p in oval]
    xs = [p.x * a.res for p in pts]
    ys = [(1 - p.y) * a.res for p in pts]
    info["views"][v] = {"face_box": [min(xs), min(ys), max(xs), max(ys)]}
    for who, show in (("target", tob), ("donor", dob)):
        tob.hide_render = show is not tob
        dob.hide_render = show is not dob
        variants = [None] if who == "target" else list(range(len(dmats)))
        for k in variants:
            if k is not None:
                dob.data.materials[0] = dmats[k]
            name = f"{v}_{who}" + ("" if k is None else str(k))
            scene.render.filepath = os.path.join(a.out_dir, name + ".png")
            bpy.ops.render.render(write_still=True)
info["donor_tex"] = a.donor_tex
json.dump(info, open(os.path.join(a.out_dir, "render_compare.json"), "w"), indent=1)
print("RENDER_COMPARE", json.dumps(info))
