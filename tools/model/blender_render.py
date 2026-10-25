"""Render the standard review views of an R9 .blend.

  blender -b model/source/r9_2002.blend --python tools/model/blender_render.py -- \
      --outdir renders/hd --views head_front,head_l45,head_r45,head_lprof,head_rprof,head_back,head_top,body_front,body_side,body_back \
      [--res 900] [--samples 48] [--kit] [--textures model/textures] [--tag hd]

Lighting is a fixed neutral three-point rig + soft environment so that renders from
different iterations are directly comparable. Cameras are orthographic for the
profile/front/back views (proportion checking) and perspective for 3/4 views.
"""
import argparse
import math
import os
import sys

import bpy
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
ap = argparse.ArgumentParser()
ap.add_argument("--outdir", required=True)
ap.add_argument("--views", default="head_front,head_l45,head_r45,head_lprof,head_rprof,head_back,head_top")
ap.add_argument("--res", type=int, default=900)
ap.add_argument("--samples", type=int, default=48)
ap.add_argument("--textures", default="")
ap.add_argument("--kit", action="store_true", help="paint the 2002 Brazil home kit on the body (preview only)")
ap.add_argument("--tag", default="")
ap.add_argument("--engine", default="CYCLES")
ap.add_argument("--wire", action="store_true")
ap.add_argument("--light", type=float, default=0.08,
                help="global light scale; 0.08 calibrated 2026-10-25: lit forehead of albedo #B5845E renders L*61.7 a*12.5 b*23.6 (photo measurements L*~60, a* 12-15, b* 21-31)")
args = ap.parse_args(argv)
os.makedirs(args.outdir, exist_ok=True)

scene = bpy.context.scene
scene.render.engine = args.engine
if args.engine == "CYCLES":
    scene.cycles.device = "CPU"
    scene.cycles.samples = args.samples
    scene.cycles.use_denoising = True
scene.render.film_transparent = False
# "Standard" keeps albedo colours readable for likeness/colour review (AgX/Filmic desaturate)
scene.view_settings.view_transform = "Standard"
scene.view_settings.look = "None"

for o in list(scene.objects):
    if o.type in ("CAMERA", "LIGHT"):
        bpy.data.objects.remove(o)

head = bpy.data.objects.get("R9_Head")
rig = bpy.data.objects.get("R9_Rig")


def world_bbox(obs):
    pts = []
    dg = bpy.context.evaluated_depsgraph_get()
    for o in obs:
        oe = o.evaluated_get(dg)
        me = oe.to_mesh()
        pts += [oe.matrix_world @ v.co for v in me.vertices]
        oe.to_mesh_clear()
    mn = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    mx = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    return mn, mx


# ----------------------------------------------------------------------------- materials
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import materials  # noqa: E402

materials.apply_materials(args.textures, args.kit)
if args.wire:
    for o in scene.objects:
        if o.type == "MESH":
            w = o.modifiers.new("wire", "WIREFRAME")
            w.thickness = 0.0006
            w.use_replace = False

# ----------------------------------------------------------------------------- world + lights
world = bpy.data.worlds.get("World") or bpy.data.worlds.new("World")
scene.world = world
world.use_nodes = True
bg = world.node_tree.nodes.get("Background")
bg.inputs[0].default_value = (0.32, 0.33, 0.35, 1)
bg.inputs[1].default_value = 0.6 * args.light / 0.3


def light(name, kind, loc, energy, size=1.0, color=(1, 1, 1)):
    ld = bpy.data.lights.new(name, kind)
    ld.energy = energy
    ld.color = color
    if kind == "AREA":
        ld.size = size
    o = bpy.data.objects.new(name, ld)
    scene.collection.objects.link(o)
    o.location = loc
    return o


def aim(o, target):
    d = (target - o.location).normalized()
    o.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()


meshes = [o for o in scene.objects if o.type == "MESH"]
bmn, bmx = world_bbox(meshes)
hmn, hmx = world_bbox([head]) if head else (bmn, bmx)
head_top = hmx.z
head_c = Vector(((hmn.x + hmx.x) / 2, (hmn.y + hmx.y) / 2, head_top - 0.13))
body_c = (bmn + bmx) / 2
H = bmx.z - bmn.z


def lights_for(center, dist):
    for o in [o for o in scene.objects if o.type == "LIGHT"]:
        bpy.data.objects.remove(o)
    k = light("Key", "AREA", center + Vector((-0.6, -1.0, 0.55)) * dist, args.light * 220 * dist * dist, 1.2 * dist)
    f = light("Fill", "AREA", center + Vector((0.9, -0.7, 0.1)) * dist, args.light * 70 * dist * dist, 1.5 * dist)
    r = light("Rim", "AREA", center + Vector((0.3, 1.0, 0.7)) * dist, args.light * 160 * dist * dist, 0.8 * dist)
    for o in (k, f, r):
        aim(o, center)


VIEWS = {
    # name: (target, azimuth deg (0 = front, +90 = character's left side), elevation deg, ortho, dist)
    "head_front": ("head", 0, 0, True, 1.0),
    "head_l45": ("head", 45, 4, False, 1.0),
    "head_r45": ("head", -45, 4, False, 1.0),
    "head_lprof": ("head", 90, 0, True, 1.0),
    "head_rprof": ("head", -90, 0, True, 1.0),
    "head_back": ("head", 180, 0, True, 1.0),
    "head_top": ("head", 0, 80, True, 1.0),
    "head_low": ("head", 20, -18, False, 1.0),
    "body_front": ("body", 0, 0, True, 1.0),
    "body_side": ("body", 90, 0, True, 1.0),
    "body_back": ("body", 180, 0, True, 1.0),
    "body_34": ("body", 35, 5, False, 1.0),
}

cam_data = bpy.data.cameras.new("Cam")
cam = bpy.data.objects.new("Cam", cam_data)
scene.collection.objects.link(cam)
scene.camera = cam

for vname in args.views.split(","):
    tgt, az, el, ortho, _ = VIEWS[vname]
    if tgt == "head":
        center, span, dist = head_c, 0.34, 1.6
        scene.render.resolution_x = scene.render.resolution_y = args.res
    else:
        center, span, dist = body_c, H * 1.08, 6.0
        scene.render.resolution_x = int(args.res * 0.6)
        scene.render.resolution_y = args.res
    a, e = math.radians(az), math.radians(el)
    # character faces -Y; its left side is +X
    direction = Vector((math.sin(a) * math.cos(e), -math.cos(a) * math.cos(e), math.sin(e)))
    cam.location = center + direction * dist
    aim(cam, center)
    if ortho:
        cam_data.type = "ORTHO"
        cam_data.ortho_scale = span
    else:
        cam_data.type = "PERSP"
        cam_data.lens = 85 if tgt == "head" else 50
        cam_data.sensor_width = 36
        if tgt == "head":
            cam.location = center + direction * (span * 85 / 36 * 1.05)
        else:
            cam.location = center + direction * (span * 50 / 36 * 1.15)
    lights_for(center, 1.0 if tgt == "head" else 2.5)
    scene.render.filepath = os.path.join(args.outdir, f"{vname}{('_' + args.tag) if args.tag else ''}.png")
    bpy.ops.render.render(write_still=True)
    print("RENDERED", scene.render.filepath)
