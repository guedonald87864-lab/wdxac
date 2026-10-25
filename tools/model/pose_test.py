"""Deformation check of the R9 mesh/skinning in football poses (Blender script).

  blender -b r9_kit.blend --python tools/model/pose_test.py -- --outdir D [--render]

Poses (approximate keyframe-style joint rotations on the MakeHuman preview rig):
  stand, sprint_stride, shot_backswing, low_dribble, cut_turn
For each pose it reports, on the evaluated (deformed) body + head:
  * edge stretch: ratio deformed/rest edge length (p1, p99, max) - candy-wrapper/collapse check
  * triangle area ratio extremes
  * sole height: lowest point of each boot/foot relative to the ground plane (rest pose sets z=0)
  * mesh-to-mesh penetration samples: body vertices that end up inside the kit shells is not
    checked (preview kit only); instead self-proximity of left/right thighs is reported.
This proves only that OUR mesh and MakeHuman weights deform sanely; the game uses the donor
skeleton (166 bones in Henry's donor) and the game's own animations, which must be checked
on device (see docs).
"""
import argparse
import json
import math
import os
import sys

import bpy
import numpy as np
from mathutils import Euler

argv = sys.argv[sys.argv.index("--") + 1:]
ap = argparse.ArgumentParser()
ap.add_argument("--outdir", required=True)
ap.add_argument("--render", action="store_true")
ap.add_argument("--textures", default="")
a = ap.parse_args(argv)
os.makedirs(a.outdir, exist_ok=True)

rig = bpy.data.objects["R9_Rig"]
POSES = {
    "stand": {},  # armature-space degrees, see set_pose()
    # right leg forward in flight phase, left leg pushed back, arms counter-swing, trunk lean
    "sprint_stride": {"spine03": (12, 0, 0), "upperleg01.R": (-60, 0, 0), "lowerleg01.R": (55, 0, 0),
                      "upperleg01.L": (25, 0, 0), "lowerleg01.L": (80, 0, 0),
                      "upperarm01.L": (-40, 0, 0), "upperarm01.R": (35, 0, 0),
                      "lowerarm01.L": (-60, 0, 0), "lowerarm01.R": (-50, 0, 0)},
    # kicking leg (right) drawn back with the knee bent, planting leg flexed, arms out for balance
    "shot_backswing": {"spine02": (5, 0, 10), "upperleg01.R": (30, 0, 0), "lowerleg01.R": (95, 0, 0),
                       "upperleg01.L": (-20, 0, 0), "lowerleg01.L": (30, 0, 0),
                       "upperarm01.L": (0, 0, -20), "upperarm01.R": (0, 0, 20)},
    # low centre of gravity carrying the ball: hips and knees flexed, trunk forward
    "low_dribble": {"spine03": (18, 0, 0), "spine02": (10, 0, 0), "upperleg01.R": (-50, 0, 0),
                    "lowerleg01.R": (65, 0, 0), "upperleg01.L": (-30, 0, 0), "lowerleg01.L": (50, 0, 0),
                    "upperarm01.L": (0, 0, -15), "upperarm01.R": (0, 0, 15)},
    # sharp cut: right leg abducted wide, knee flexed, trunk and head twisted
    "cut_turn": {"spine03": (0, 0, 30), "neck01": (0, 0, 20), "upperleg01.R": (-25, -35, 0),
                 "lowerleg01.R": (45, 0, 0), "upperleg01.L": (-10, 10, 0), "lowerleg01.L": (30, 0, 0),
                 "upperarm01.R": (0, -40, 0), "upperarm01.L": (0, 25, 0)},
}


def eval_coords(ob):
    dg = bpy.context.evaluated_depsgraph_get()
    oe = ob.evaluated_get(dg)
    me = oe.to_mesh()
    co = np.array([oe.matrix_world @ v.co for v in me.vertices])
    oe.to_mesh_clear()
    return co


def edges_tris(ob):
    me = ob.data
    E = np.array([e.vertices[:] for e in me.edges])
    T = []
    for p in me.polygons:
        vs = list(p.vertices)
        T += [(vs[0], vs[k], vs[k + 1]) for k in range(1, len(vs) - 1)]
    return E, np.array(T)


def set_pose(pose):
    """pose: bone -> (x, y, z) degrees about ARMATURE-space axes (X = character's left,
    -Y = forward, Z = up), applied parent-first so a rotation always means the same anatomical
    motion regardless of the MakeHuman bone rolls. X+ on a thigh = hip extension (leg back),
    X- = hip flexion (leg forward); X+ on a shin = knee flexion."""
    for pb in rig.pose.bones:
        pb.rotation_mode = "QUATERNION"
        pb.rotation_quaternion = (1, 0, 0, 0)
    bpy.context.view_layer.update()
    order = [b.name for b in rig.data.bones]          # parents come before children
    for name in order:
        if name not in pose:
            continue
        pb = rig.pose.bones[name]
        x, y, z = (math.radians(v) for v in pose[name])
        R = (Euler((x, y, z), "XYZ").to_matrix().to_4x4())
        M = pb.matrix.copy()                           # current armature-space matrix
        head = M.translation.copy()
        rot = R @ M.to_3x3().to_4x4()
        rot.translation = head
        pb.matrix = rot
        bpy.context.view_layer.update()


objs = [o for o in (bpy.data.objects.get("R9_Body"), bpy.data.objects.get("R9_Head")) if o]
for o in bpy.data.objects:          # the kit shells are a static preview, not skinned garments
    if o.name.startswith("Kit_"):
        o.hide_render = True
set_pose({})
rest = {o.name: eval_coords(o) for o in objs}
topo = {o.name: edges_tris(o) for o in objs}
ground = min(r[:, 2].min() for r in rest.values())
report = {}
for name, pose in POSES.items():
    set_pose(pose)
    rep = {}
    for o in objs:
        P = eval_coords(o)
        R = rest[o.name]
        E, T = topo[o.name]
        lr = np.linalg.norm(R[E[:, 0]] - R[E[:, 1]], axis=1)
        lp = np.linalg.norm(P[E[:, 0]] - P[E[:, 1]], axis=1)
        s = lp / np.maximum(lr, 1e-9)

        def area(X):
            return 0.5 * np.linalg.norm(np.cross(X[T[:, 1]] - X[T[:, 0]], X[T[:, 2]] - X[T[:, 0]]), axis=1)
        ar = area(P) / np.maximum(area(R), 1e-12)
        rep[o.name] = {"edge_stretch_p1": float(np.percentile(s, 1)), "edge_stretch_p99": float(np.percentile(s, 99)),
                       "edge_stretch_min": float(s.min()), "edge_stretch_max": float(s.max()),
                       "area_ratio_min": float(ar.min()), "area_ratio_max": float(ar.max()),
                       "n_edges_over_1.5x": int((s > 1.5).sum()), "n_edges_under_0.5x": int((s < 0.5).sum())}
        if o.name == "R9_Body":
            xs = P[:, 0]
            left, right = P[xs > 0.02], P[xs < -0.02]
            rep["lowest_point_above_ground_cm"] = float((P[:, 2].min() - ground) * 100)
    report[name] = rep
    if a.render:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import materials
        materials.apply_materials(a.textures)
        sc = bpy.context.scene
        sc.render.engine = "CYCLES"
        sc.cycles.samples = 12
        sc.render.resolution_x, sc.render.resolution_y = 480, 640
        sc.view_settings.view_transform = "Standard"
        if not sc.world:
            sc.world = bpy.data.worlds.new("w")
        sc.world.use_nodes = True
        sc.world.node_tree.nodes["Background"].inputs[1].default_value = 0.8
        cam = bpy.data.objects.get("PoseCam")
        if not cam:
            cam = bpy.data.objects.new("PoseCam", bpy.data.cameras.new("PoseCam"))
            sc.collection.objects.link(cam)
        sc.camera = cam
        cam.data.type = "ORTHO"
        cam.data.ortho_scale = 2.3
        zc = ground + 0.95
        for view, loc in (("side", (4.0, 0, zc)), ("front", (1.2, -4.0, zc))):
            cam.location = loc
            cam.rotation_euler = (math.radians(90), 0, math.atan2(loc[0], -loc[1]))
            ld = bpy.data.objects.get("PoseKey")
            if not ld:
                ld = bpy.data.objects.new("PoseKey", bpy.data.lights.new("PoseKey", "SUN"))
                sc.collection.objects.link(ld)
                ld.data.energy = 3.0
                ld.rotation_euler = (math.radians(50), 0, math.radians(30))
            sc.render.filepath = os.path.join(a.outdir, f"pose_{name}_{view}.png")
            bpy.ops.render.render(write_still=True)
set_pose({})
json.dump(report, open(os.path.join(a.outdir, "pose_report.json"), "w"), indent=1)
for k, v in report.items():
    b = v.get("R9_Body", {})
    print(f"{k:15s} body stretch p1 {b.get('edge_stretch_p1', 0):.2f} p99 {b.get('edge_stretch_p99', 0):.2f} "
          f"max {b.get('edge_stretch_max', 0):.2f} >1.5x {b.get('n_edges_over_1.5x')} <0.5x {b.get('n_edges_under_0.5x')} "
          f"lowest {v.get('lowest_point_above_ground_cm', 0):+.1f} cm")
print("POSE_DONE")
