"""Render card portrait layers of the R9 model (Blender script).

  blender -b r9_kit.blend --python tools/card/render_portrait.py -- --outdir card/layers \
          --textures model/textures [--size 1536]

Outputs RGBA PNGs with transparent background (the card frame/background are separate
layers, see tools/card/compose_card.py):
  portrait_34.png      head and shoulders, 3/4 view from the subject's left, 85 mm
  portrait_front.png   head and shoulders, near-frontal
  portrait_full.png    full body, for pack-opening/large detail views
The portrait is our own 3D model (independent artwork), never a press photo.
"""
import argparse
import math
import os
import sys

import bpy
from mathutils import Euler, Vector

argv = sys.argv[sys.argv.index("--") + 1:]
ap = argparse.ArgumentParser()
ap.add_argument("--outdir", required=True)
ap.add_argument("--textures", default="")
ap.add_argument("--size", type=int, default=1536)
ap.add_argument("--samples", type=int, default=64)
a = ap.parse_args(argv)
os.makedirs(a.outdir, exist_ok=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "model"))
import materials  # noqa: E402

materials.apply_materials(a.textures)
sc = bpy.context.scene
sc.render.engine = "CYCLES"
sc.cycles.device = "CPU"
sc.cycles.samples = a.samples
sc.cycles.use_denoising = True
sc.render.film_transparent = True
sc.view_settings.view_transform = "Standard"
sc.view_settings.look = "None"
for o in [o for o in sc.objects if o.type in ("LIGHT", "CAMERA")]:
    bpy.data.objects.remove(o)

rig = bpy.data.objects["R9_Rig"]
# arms down and slightly forward, shoulders relaxed (armature-space rotations, parent first)
POSE = {"upperarm01.L": (0, 40, 0), "upperarm01.R": (0, -40, 0),
        "lowerarm01.L": (-12, 0, 0), "lowerarm01.R": (-12, 0, 0), "neck01": (-4, 0, 0)}
for pb in rig.pose.bones:
    pb.rotation_mode = "QUATERNION"
    pb.rotation_quaternion = (1, 0, 0, 0)
bpy.context.view_layer.update()
for b in [b.name for b in rig.data.bones]:
    if b in POSE:
        pb = rig.pose.bones[b]
        R = Euler(tuple(math.radians(v) for v in POSE[b]), "XYZ").to_matrix().to_4x4()
        M = pb.matrix.copy()
        h = M.translation.copy()
        N = R @ M.to_3x3().to_4x4()
        N.translation = h
        pb.matrix = N
        bpy.context.view_layer.update()

world = bpy.data.worlds.get("World") or bpy.data.worlds.new("World")
sc.world = world
world.use_nodes = True
world.node_tree.nodes["Background"].inputs[0].default_value = (0.05, 0.06, 0.09, 1)
world.node_tree.nodes["Background"].inputs[1].default_value = 0.35


def light(name, kind, loc, energy, color, size=1.0, target=None):
    ld = bpy.data.lights.new(name, kind)
    ld.energy = energy
    ld.color = color
    if kind == "AREA":
        ld.size = size
    o = bpy.data.objects.new(name, ld)
    sc.collection.objects.link(o)
    o.location = loc
    if target is not None:
        o.rotation_euler = (target - Vector(loc)).to_track_quat("-Z", "Y").to_euler()
    return o


head = bpy.data.objects["R9_Head"]
hz = max((head.matrix_world @ v.co).z for v in head.data.vertices)
chest = Vector((0, 0, hz - 0.42))
face = Vector((0, -0.05, hz - 0.13))
# rims sit at head height and stay weak: from above they bleach the shaved scalp to white
light("Key", "AREA", (-1.4, -1.6, hz + 0.25), 55, (1.0, 0.94, 0.86), 1.2, face)
light("Fill", "AREA", (1.6, -1.2, hz - 0.2), 18, (0.80, 0.86, 1.0), 1.5, face)
light("RimL", "AREA", (-1.2, 1.4, hz - 0.15), 30, (1.0, 0.85, 0.45), 0.6, face)    # warm stadium rim
light("RimR", "AREA", (1.3, 1.3, hz - 0.15), 30, (0.55, 0.85, 1.0), 0.6, face)

cam = bpy.data.objects.new("CardCam", bpy.data.cameras.new("CardCam"))
sc.collection.objects.link(cam)
sc.camera = cam
SHOTS = {
    # head and shoulders: the face is the deliverable, the kit is only a preview shell
    "portrait_34": (Vector((-0.55, -1.0, 0.08)), 85, Vector((0, 0, hz - 0.30)), 2.7, (1, 1.25)),
    "portrait_front": (Vector((-0.12, -1.0, 0.05)), 85, Vector((0, 0, hz - 0.30)), 2.7, (1, 1.25)),
    "portrait_full": (Vector((-0.45, -1.0, 0.05)), 50, Vector((0, 0, hz - 0.92)), 4.6, (1, 1.6)),
}
for name, (d, lens, tgt, dist, aspect) in SHOTS.items():
    cam.data.lens = lens
    cam.data.sensor_fit = "VERTICAL"
    cam.location = tgt + d.normalized() * dist
    cam.rotation_euler = (tgt - cam.location).to_track_quat("-Z", "Y").to_euler()
    sc.render.resolution_x = int(a.size * aspect[0] / aspect[1])
    sc.render.resolution_y = a.size
    sc.render.filepath = os.path.join(a.outdir, name + ".png")
    bpy.ops.render.render(write_still=True)
    print("PORTRAIT", sc.render.filepath)
