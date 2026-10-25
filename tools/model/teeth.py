"""Procedural dentition for the R9 head (Blender module, used by blender_build.py).

Why: the MakeHuman teeth helper is a single coarse arch. The 2002 photos (dossier 01 §2.1,
F3/F4/R34-2, Commons Ronaldo_2002_cropped) show his most recognisable mouth feature when
he smiles or celebrates: two LARGE, LONG, slightly PROCLINED upper central incisors with a
clear GAP between them (midline diastema). Teeth are therefore built individually along a
parabolic arch fitted to the helper teeth position, with sizes from typical adult crown
dimensions (mm) scaled per model/source/r9_teeth_params.json.

Coordinates in: MakeHuman (decimetres, Y up, face +Z). Output: Blender objects in the
build's (x, -z, y) * 0.1 metre space, skinned to 'head' (upper) and 'jaw' (lower).
"""
import math

import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Vector

# mesiodistal width, crown height, labio-lingual depth (mm), typical adult values
UPPER = [("central", 8.6, 10.5, 7.0), ("lateral", 6.6, 9.0, 6.0), ("canine", 7.6, 10.0, 8.0),
         ("premolar1", 7.0, 8.5, 9.0), ("premolar2", 6.6, 7.8, 9.0), ("molar1", 10.0, 7.5, 11.0),
         ("molar2", 9.0, 7.0, 11.0)]
LOWER = [("central", 5.3, 9.0, 5.7), ("lateral", 5.9, 9.5, 6.1), ("canine", 6.9, 11.0, 7.7),
         ("premolar1", 7.0, 8.5, 7.7), ("premolar2", 7.1, 8.0, 8.2), ("molar1", 11.2, 7.5, 10.3),
         ("molar2", 10.5, 7.0, 10.1)]


def rounded_tooth(name, w, h, d, incisal_round=0.18):
    """Return a bmesh for one crown, origin at the incisal/occlusal edge centre, crown
    extending +Y (towards the gum) in local space. Incisors and canines are shovel-shaped:
    labio-lingual depth tapers to ~35 % at the incisal edge and the edge is slightly narrower."""
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bmesh.ops.subdivide_edges(bm, edges=list(bm.edges), cuts=2, use_grid_fill=True)
    bmesh.ops.scale(bm, vec=(w, h, d), verts=bm.verts)
    bmesh.ops.translate(bm, vec=(0, h / 2, 0), verts=bm.verts)
    anterior = name in ("central", "lateral", "canine")
    for v in bm.verts:
        t = min(max(v.co.y / h, 0.0), 1.0)              # 0 at the edge, 1 at the gum
        if anterior:
            v.co.z = (v.co.z + d / 2) * (0.35 + 0.65 * t) - d / 2 * (0.35 + 0.65 * t) + (d / 2) * (1 - (0.35 + 0.65 * t))
            v.co.x *= 0.92 + 0.08 * t
        else:
            v.co.x *= 0.9 + 0.1 * t
    bmesh.ops.bevel(bm, geom=list(bm.edges), offset=min(w, h, d) * incisal_round, segments=2,
                    affect="EDGES", profile=0.5, clamp_overlap=True)
    return bm


def arch_points(front, half_width, depth, n=200):
    """Parabolic dental arch in the horizontal plane: x in [-hw, hw], z = front - depth*(x/hw)^2."""
    xs = np.linspace(-half_width, half_width, n)
    zs = front - depth * (xs / half_width) ** 2
    return xs, zs


def place_along_arch(teeth, xs, zs, gap_mid, start_offset=0.0):
    """Walk from the midline outward along the arch, returning (x, z, tangent angle) of each
    tooth centre for one side (+x). Arc length is in mm (xs, zs in mm)."""
    pts = np.stack([xs, zs], 1)
    mid = len(xs) // 2
    seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    s = np.concatenate([[0], np.cumsum(seg)])
    s = s - s[mid]
    out = []
    cur = gap_mid / 2 + start_offset
    for t in teeth:
        w = t[1]
        c = cur + w / 2
        i = np.searchsorted(s, c)
        i = min(max(i, 1), len(s) - 1)
        f = (c - s[i - 1]) / max(s[i] - s[i - 1], 1e-9)
        x = xs[i - 1] + f * (xs[i] - xs[i - 1])
        z = zs[i - 1] + f * (zs[i] - zs[i - 1])
        ang = math.atan2(zs[i] - zs[i - 1], xs[i] - xs[i - 1])
        out.append((x, z, ang))
        cur += w + 0.15
    return out


def build_teeth(params, helper_upper_mm, helper_lower_mm, to_bl, rig):
    """helper_*_mm: (N,3) helper teeth vertices in MakeHuman mm. Returns list of objects."""
    objs = []
    for jaw, helper, table in (("upper", helper_upper_mm, UPPER), ("lower", helper_lower_mm, LOWER)):
        P = np.asarray(helper)
        front = P[:, 2].max() + (params["upper_forward_mm"] if jaw == "upper" else 0.0)
        hw = (P[:, 0].max() - P[:, 0].min()) / 2 * params["arch_width_scale"]
        depth = params["arch_depth_mm"]
        edge_y = P[:, 1].min() if jaw == "upper" else P[:, 1].max()
        xs, zs = arch_points(front, hw, depth)
        gap = params["midline_gap_mm"] if jaw == "upper" else 0.3
        scale = dict(params.get("scale", {}))
        teeth = [(n, w * scale.get(f"{jaw}_{n}", 1.0), h * scale.get(f"{jaw}_{n}_h", scale.get(f"{jaw}_{n}", 1.0)), d)
                 for n, w, h, d in table]
        placements = place_along_arch(teeth, xs, zs, gap)
        bm_all = bmesh.new()
        for side in (+1, -1):
            for (n, w, h, d), (x, z, ang) in zip(teeth, placements):
                bm = rounded_tooth(n, w, h, d)
                tilt = math.radians(params["central_procline_deg"]) if (jaw == "upper" and n == "central") else \
                    math.radians(params.get("lateral_procline_deg", 0.0)) if (jaw == "upper" and n == "lateral") else 0.0
                # local: X along arch, Y up towards gum (upper) / down (lower), Z labial
                M = Matrix.Identity(4)
                if jaw == "lower":
                    M = Matrix.Rotation(math.pi, 4, "Z") @ M          # crown grows downward
                M = Matrix.Rotation(-tilt, 4, "X") @ M                 # procline: incisal edge forward
                yaw = -ang if side > 0 else ang
                M = Matrix.Rotation(yaw, 4, "Y") @ M
                M = Matrix.Translation((side * x, edge_y, z - d / 2)) @ M
                bmesh.ops.transform(bm, matrix=M, verts=bm.verts)
                tmp = bpy.data.meshes.new("tmp")
                bm.to_mesh(tmp)
                bm.free()
                bm_all.from_mesh(tmp)
                bpy.data.meshes.remove(tmp)
        me = bpy.data.meshes.new(f"R9_Teeth_{jaw}")
        bm_all.to_mesh(me)
        bm_all.free()
        # mm (MakeHuman axes) -> Blender build space
        co = np.array([v.co[:] for v in me.vertices]) / 100.0      # mm -> dm
        bl = to_bl(co)
        for v, c in zip(me.vertices, bl):
            v.co = Vector(c)
        for p in me.polygons:
            p.use_smooth = True
        ob = bpy.data.objects.new(f"R9_Teeth_{jaw}", me)
        bpy.context.scene.collection.objects.link(ob)
        vg = ob.vertex_groups.new(name="head" if jaw == "upper" else "jaw")
        vg.add(list(range(len(me.vertices))), 1.0, "REPLACE")
        mod = ob.modifiers.new("Armature", "ARMATURE")
        mod.object = rig
        ob.parent = rig
        objs.append(ob)
    return objs
