"""2002 World Cup final kit PREVIEW for body-shape review (Blender script).

  blender -b r9.blend --python tools/model/kit_preview.py -- --out r9_kit.blend

NOT a game asset: in eFootball the kit comes from the game's own kit system. This only
dresses the body so full-body proportions can be judged the way people saw him on
30 June 2002. Garments are offset shells of body regions selected by dominant bone weight.
Colours (sRGB) from research/notes/01_appearance_2002.md §6.1 (photo measurements):
shirt canary yellow #FCF068 with green #468E31 trims, shorts royal blue #1C3FC5 with white
side stripe, BLUE socks in the final, chrome-silver Mercurial Vapor boots with lime accents.
"""
import os
import sys

import bmesh
import bpy
import numpy as np

argv = sys.argv[sys.argv.index("--") + 1:]
out = argv[argv.index("--out") + 1]
body = bpy.data.objects["R9_Body"]
rig = bpy.data.objects["R9_Rig"]
me = body.data
names = {g.index: g.name for g in body.vertex_groups}
dom = {}
for v in me.vertices:
    if v.groups:
        g = max(v.groups, key=lambda g: g.weight)
        dom[v.index] = names[g.group]
co = np.array([v.co[:] for v in me.vertices])


def srgb(h):
    h = h.lstrip("#")
    c = np.array([int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)])
    return tuple(np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4))


def mat(name, hexcol, rough=0.6, metal=0.0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*srgb(hexcol), 1)
    b.inputs["Roughness"].default_value = rough
    b.inputs["Metallic"].default_value = metal
    return m


# joint heights for cut planes (Blender Z up)
def bone_head(n):
    return np.array((rig.matrix_world @ rig.data.bones[n].head_local)[:])


def bone_tail(n):
    return np.array((rig.matrix_world @ rig.data.bones[n].tail_local)[:])


knee_z = (bone_head("lowerleg01.L")[2] + bone_head("lowerleg01.R")[2]) / 2
hip_z = (bone_head("upperleg01.L")[2] + bone_head("upperleg01.R")[2]) / 2
ankle_z = (bone_head("foot.L")[2] + bone_head("foot.R")[2]) / 2
shorts_low = knee_z + 0.42 * (hip_z - knee_z)          # long, baggy 2002 shorts
sock_top = knee_z - 0.04
sh_l, el_l = bone_head("upperarm01.L"), bone_tail("upperarm02.L")
sleeve_frac = 0.55                                      # short sleeve ends ~mid upper arm
neck_z = bone_head("neck01")[2]

TORSO = ("spine", "clavicle", "breast", "shoulder", "pectoral", "special03", "root", "pelvis")


def region(v):
    z = co[v, 2]
    b = dom.get(v, "")
    if b.startswith("upperarm") or b.startswith("shoulder"):
        d = abs(co[v, 0]) - abs(sh_l[0])
        return "shirt" if d < sleeve_frac * abs(el_l[0] - sh_l[0]) + 0.02 else None
    if any(b.startswith(t) for t in TORSO) and z < neck_z - 0.015:
        return "shorts+shirt" if hip_z + 0.0 < z < hip_z + 0.06 else ("shorts" if z <= hip_z else "shirt")
    if b.startswith("upperleg") or b.startswith("pelvis"):
        return "shorts" if z > shorts_low else None
    if b.startswith("lowerleg") and ankle_z + 0.03 < z < sock_top:
        return "socks"
    if b.startswith("foot") or b.startswith("toe") or (b.startswith("lowerleg") and z <= ankle_z + 0.03):
        return "boots"
    return None


reg = {v: region(v) for v in range(len(co))}


def add_trims(ob, gm):
    import bmesh as _bm
    bm = _bm.new()
    bm.from_mesh(gm)
    edges = [e for e in bm.edges if e.is_boundary]
    adj = {}
    for e in edges:
        a_, b_ = e.verts
        adj.setdefault(a_.index, []).append(b_.index)
        adj.setdefault(b_.index, []).append(a_.index)
    pos = {v.index: v.co.copy() for v in bm.verts}
    bm.free()
    seen = set()
    loops = []
    for start in adj:
        if start in seen:
            continue
        loop, prev, cur = [start], None, start
        seen.add(start)
        while True:
            nxt = [n for n in adj[cur] if n != prev and n not in seen]
            if not nxt:
                break
            prev, cur = cur, nxt[0]
            seen.add(cur)
            loop.append(cur)
        if len(loop) > 8:
            loops.append(loop)
    for i, loop in enumerate(loops):
        zc = np.mean([pos[k].z for k in loop])
        if zc < hip_z + 0.1:              # hem tucked into the shorts: no trim
            continue
        # relax the saw-tooth opening (cut along mesh faces) into a smooth hem line
        P_ = np.array([pos[k][:] for k in loop])
        for _ in range(25):
            P_ = 0.5 * P_ + 0.25 * (np.roll(P_, 1, 0) + np.roll(P_, -1, 0))
        for k, p_ in zip(loop, P_):
            pos[k] = type(pos[k])(p_)
        cd = bpy.data.curves.new(f"Kit_trim{i}", "CURVE")
        cd.dimensions = "3D"
        cd.bevel_depth = 0.008
        cd.bevel_resolution = 3
        sp = cd.splines.new("POLY")
        sp.points.add(len(loop))
        for j, k in enumerate(loop + loop[:1]):
            p_ = pos[k]
            sp.points[j].co = (p_.x, p_.y, p_.z, 1)
        cobj = bpy.data.objects.new(f"_trimcurve{i}", cd)
        bpy.context.scene.collection.objects.link(cobj)
        cd.materials.append(trim)
        dg = bpy.context.evaluated_depsgraph_get()
        tm = bpy.data.meshes.new_from_object(cobj.evaluated_get(dg))
        bpy.data.objects.remove(cobj)
        tob = bpy.data.objects.new(f"Kit_trim{i}", tm)
        bpy.context.scene.collection.objects.link(tob)
        # skin each trim vertex like its nearest body vertex
        from mathutils.kdtree import KDTree
        kd = KDTree(len(co))
        for vi, c in enumerate(co):
            kd.insert(c, vi)
        kd.balance()
        groups = {g.name: tob.vertex_groups.new(name=g.name) for g in body.vertex_groups}
        for v in tm.vertices:
            _, vi, _ = kd.find(v.co)
            for g in me.vertices[vi].groups:
                groups[names[g.group]].add([v.index], g.weight, "REPLACE")
        tob.parent = rig
        am_ = tob.modifiers.new("Armature", "ARMATURE")
        am_.object = rig
OFFS = {"shirt": 0.016, "shorts": 0.020, "socks": 0.004, "boots": 0.008}
COLS = {"shirt": mat("Kit_Shirt", "#FCF068", 0.75), "shorts": mat("Kit_Shorts", "#1C3FC5", 0.7),
        "socks": mat("Kit_Socks", "#1C3FC5", 0.8), "boots": mat("Kit_Boots", "#C8CCD0", 0.25, 0.9)}
trim = mat("Kit_Trim", "#468E31", 0.75)
white = mat("Kit_White", "#F2F2F2", 0.7)
lime = mat("Kit_Lime", "#B5E61D", 0.4)

for name, off in OFFS.items():
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.verts.ensure_lookup_table()
    # the waistband band belongs to both garments (shirt tucked into the shorts at kick-off,
    # dossier 01 §6.1) so no skin shows between them; shorts are offset further out
    kill = [f for f in bm.faces if not all(name in (reg[v.index] or "").split("+") for v in f.verts)]
    bmesh.ops.delete(bm, geom=kill, context="FACES")
    bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context="VERTS")
    for v in bm.verts:
        v.co += v.normal * off
    # finer faces for clean trim edges, then relax so cloth does not show ribs/toes
    bmesh.ops.subdivide_edges(bm, edges=list(bm.edges), cuts=1, use_grid_fill=True)
    boundary = {v for e in bm.edges if e.is_boundary for v in e.verts}
    for _ in range({"boots": 12, "socks": 3, "shirt": 4, "shorts": 6}[name]):
        bmesh.ops.smooth_vert(bm, verts=[v for v in bm.verts if v not in boundary], factor=0.5,
                              use_axis_x=True, use_axis_y=True, use_axis_z=True)
    gm = bpy.data.meshes.new(f"Kit_{name}")
    bm.to_mesh(gm)
    bm.free()
    ob = bpy.data.objects.new(f"Kit_{name}", gm)
    bpy.context.scene.collection.objects.link(ob)
    gm.materials.append(COLS[name])
    # accents: shirt collar + sleeve hems green, shorts side stripe white, boots lime heel
    acc = {"boots": lime}.get(name)
    if acc:
        gm.materials.append(acc)
        vc = np.array([v.co[:] for v in gm.vertices])
        for p in gm.polygons:
            c = vc[list(p.vertices)].mean(0)
            if name == "boots" and c[1] > np.percentile(vc[:, 1], 85):
                p.material_index = 1
    for p in gm.polygons:
        p.use_smooth = True
    if name == "shirt":
        # clean green ribbing on the collar and sleeve hems: a round tube along each opening
        # of the shirt shell (per-face colouring gave saw-tooth edges in close-ups)
        add_trims(ob, gm)
    # follow the body skinning: bmesh.from_mesh() already copied the body's deform weights;
    # recreate the vertex groups in the same order so the indices line up
    for g in body.vertex_groups:
        ob.vertex_groups.new(name=g.name)
    ob.parent = rig
    am = ob.modifiers.new("Armature", "ARMATURE")
    am.object = rig
    print("KIT", name, len(gm.vertices))

bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(out))
print("KIT_DONE")
