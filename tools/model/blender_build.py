"""Assemble the editable R9 character in Blender from a params JSON.

Run:
  blender -b --python tools/model/blender_build.py -- --params model/source/r9_params.json \
          --out model/source/r9_2002.blend [--glb model/export/r9_2002_hd.glb]

Produces (all in one .blend):
  R9_Rig      armature (MakeHuman default skeleton, 163 bones) - preview/animation-test rig
  R9_Body     body below the neck cut, MakeHuman UV, skinned
  R9_Head     head + neck, dedicated head UV (face texture space), skinned
  R9_Eyes     eyeballs (MakeHuman high-poly eye proxy), parented to head bone
  R9_Teeth    upper + lower teeth, R9_Tongue
  R9_Hair     2002 "Cascao" front tuft shell (built by hair_build.py if present)
Coordinates: MakeHuman (Y-up, decimetres, face +Z) -> Blender (Z-up, metres, face -Y).
"""
import argparse
import json
import os
import sys

import bpy
import bmesh
import numpy as np
from mathutils import Vector

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mh_engine as E  # noqa: E402

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
ap = argparse.ArgumentParser()
ap.add_argument("--params", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--glb")
args = ap.parse_args(argv)

PARAMS = json.load(open(args.params))
BASE = E.load_obj()
VERTS_MH, _ = E.build(PARAMS, BASE)


def to_bl(v):
    v = np.asarray(v, float)
    return np.stack([v[..., 0], -v[..., 2], v[..., 1]], axis=-1) * 0.1


VB = to_bl(VERTS_MH)
JOINTS_MH, SKEL = E.joint_positions(VERTS_MH)
JOINTS = {k: Vector(to_bl(p)) for k, p in JOINTS_MH.items()}
WEIGHTS = json.load(open(os.path.join(E.MH_DATA, "rigs", "default_weights.mhw")))["weights"]

# ----------------------------------------------------------------------------- scene reset
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.unit_settings.system = "METRIC"


# ----------------------------------------------------------------------------- armature
def build_armature():
    arm = bpy.data.armatures.new("R9_Rig")
    ob = bpy.data.objects.new("R9_Rig", arm)
    scene.collection.objects.link(ob)
    bpy.context.view_layer.objects.active = ob
    bpy.ops.object.mode_set(mode="EDIT")
    bones = SKEL["bones"]
    order = []
    seen = set()

    def visit(n):
        if n in seen:
            return
        p = bones[n]["parent"]
        if p:
            visit(p)
        seen.add(n)
        order.append(n)

    for n in bones:
        visit(n)
    for n in order:
        b = bones[n]
        eb = arm.edit_bones.new(n)
        eb.head = JOINTS[b["head"]]
        eb.tail = JOINTS[b["tail"]]
        if (eb.tail - eb.head).length < 1e-4:
            eb.tail = eb.head + Vector((0, 0, 0.005))
        if b["parent"]:
            eb.parent = arm.edit_bones[b["parent"]]
        plane = SKEL["planes"].get(b.get("rotation_plane") or "")
        if plane:
            p0, p1, p2 = (JOINTS[j] for j in plane)
            normal = (p1 - p0).cross(p2 - p1)
            if normal.length > 1e-8:
                eb.align_roll(normal.normalized())
    bpy.ops.object.mode_set(mode="OBJECT")
    return ob


# ----------------------------------------------------------------------------- meshes
def faces_of(groups):
    out = []
    for g in groups:
        out.extend(BASE["groups"][g])
    return out


def make_mesh(name, face_ids, verts=VB, uv_mode="base"):
    """Build a Blender mesh from base-mesh faces; keeps a 'mh_index' int attribute for traceability."""
    used = sorted({i for fi in face_ids for i in BASE["faces"][fi][0]})
    remap = {o: n for n, o in enumerate(used)}
    me = bpy.data.meshes.new(name)
    polys = [[remap[i] for i in BASE["faces"][fi][0]] for fi in face_ids]
    me.from_pydata([tuple(verts[i]) for i in used], [], polys)
    me.update()
    attr = me.attributes.new("mh_index", "INT", "POINT")
    attr.data.foreach_set("value", used)
    if uv_mode == "base":
        uvl = me.uv_layers.new(name="UVMap")
        k = 0
        for fi in face_ids:
            for ti in BASE["faces"][fi][1]:
                uvl.data[k].uv = tuple(BASE["uvs"][ti])
                k += 1
    ob = bpy.data.objects.new(name, me)
    scene.collection.objects.link(ob)
    for p in me.polygons:
        p.use_smooth = True
    return ob, used


def seamless_normals(ob, used):
    """Head and body are separate meshes; give both the normals of the UNSPLIT body so the
    neck seam does not show as a shading crease."""
    me = ob.data
    nrm = FULL_NORMALS[used]
    me.normals_split_custom_set_from_vertices([tuple(n) for n in nrm])


def add_skin(ob, used, rig):
    used_set = {o: n for n, o in enumerate(used)}
    for bone, pairs in WEIGHTS.items():
        vg = None
        for vi, w in pairs:
            n = used_set.get(vi)
            if n is None:
                continue
            if vg is None:
                vg = ob.vertex_groups.new(name=bone)
            vg.add([n], w, "REPLACE")
    mod = ob.modifiers.new("Armature", "ARMATURE")
    mod.object = rig
    ob.parent = rig


def split_head_body():
    """Neck cut: plane through the neck01 bone midpoint, perpendicular to the neck axis."""
    bones = SKEL["bones"]
    n_head = JOINTS[bones["neck01"]["head"]]
    n_tail = JOINTS[bones["neck01"]["tail"]]
    cut_p = n_head.lerp(n_tail, PARAMS.get("neck_cut", 0.35))
    axis = (n_tail - n_head).normalized()
    head_f, body_f = [], []
    for fi in BASE["groups"]["body"]:
        c = Vector(VB[BASE["faces"][fi][0]].mean(axis=0))
        (head_f if (c - cut_p).dot(axis) > 0 else body_f).append(fi)
    return head_f, body_f


def head_uv(ob):
    """Dedicated face-texture UV ("HeadUV"): one island, seam down the back of the head.

    Seam edges: the posterior midline (|x| small, behind the ears) from the neck cut to the
    crown. The island is unwrapped angle-based (low distortion over the crown, unlike a
    cylindrical projection) and then rotated/scaled so the face is upright and centred,
    which is the usual game face-texture layout."""
    me = ob.data
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.verts.ensure_lookup_table()
    co = np.array([v.co[:] for v in bm.verts])
    y_ear = np.percentile(co[:, 1], 70)            # Blender +Y is the back of the head
    for e in bm.edges:
        a, b = e.verts
        if abs(a.co.x) < 0.004 and abs(b.co.x) < 0.004 and a.co.y > y_ear and b.co.y > y_ear:
            e.seam = True
    bm.to_mesh(me)
    bm.free()
    uvl = me.uv_layers.new(name="HeadUV")
    me.uv_layers.active = uvl
    bpy.context.view_layer.objects.active = ob
    for o in bpy.context.selected_objects:
        o.select_set(False)
    ob.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.uv.unwrap(method="ANGLE_BASED", margin=0.01)
    bpy.ops.object.mode_set(mode="OBJECT")
    # orient: vector from chin (low z) to crown (high z) should point to +v; nose (min y) centred
    uv = np.array([d.uv[:] for d in uvl.data])
    vidx = np.array([l.vertex_index for l in me.loops])
    vco = np.array([v.co[:] for v in me.vertices])[vidx]
    lo, hi = vco[:, 2] < np.percentile(vco[:, 2], 10), vco[:, 2] > np.percentile(vco[:, 2], 90)
    up = uv[hi].mean(0) - uv[lo].mean(0)
    ang = np.arctan2(up[0], up[1])
    R = np.array([[np.cos(ang), -np.sin(ang)], [np.sin(ang), np.cos(ang)]])
    uv = uv @ R.T
    left = vco[:, 0] > 0.03                        # subject's left side (Blender +X)
    if uv[left, 0].mean() < uv[~left, 0].mean():   # keep the subject's left on image-right
        uv[:, 0] = -uv[:, 0]
    mn, mx = uv.min(0), uv.max(0)
    s = 0.98 / (mx - mn).max()
    uv = (uv - (mn + mx) / 2) * s + 0.5
    uvl.data.foreach_set("uv", uv.ravel())
    return uvl


def full_body_normals():
    faces = [BASE["faces"][fi][0] for fi in BASE["groups"]["body"]]
    N = np.zeros_like(VB)
    for f in faces:
        f = np.asarray(f)
        for k in range(1, len(f) - 1):
            a, b, c = VB[f[0]], VB[f[k]], VB[f[k + 1]]
            n = np.cross(b - a, c - a)
            N[[f[0], f[k], f[k + 1]]] += n
    return N / np.maximum(np.linalg.norm(N, axis=1, keepdims=True), 1e-12)


FULL_NORMALS = full_body_normals()


def main():
    rig = build_armature()
    head_f, body_f = split_head_body()
    body, bused = make_mesh("R9_Body", body_f)
    seamless_normals(body, bused)
    add_skin(body, bused, rig)
    head, hused = make_mesh("R9_Head", head_f)
    head.data.uv_layers[0].name = "MH_UV"
    head_uv(head)
    seamless_normals(head, hused)
    add_skin(head, hused, rig)

    if PARAMS.get("teeth"):
        import teeth as T
        tp = json.load(open(PARAMS["teeth"]))
        up = VERTS_MH[E.group_vertex_ids(BASE, "helper-upper-teeth")] * 100.0
        lo = VERTS_MH[E.group_vertex_ids(BASE, "helper-lower-teeth")] * 100.0
        tobjs = T.build_teeth(tp, up, lo, to_bl, rig)
        teeth = tobjs[0]
    else:
        teeth, tused = make_mesh("R9_Teeth", faces_of(["helper-upper-teeth", "helper-lower-teeth"]))
        add_skin(teeth, tused, rig)
        tobjs = [teeth]
    tongue, tgused = make_mesh("R9_Tongue", faces_of(["helper-tongue"]))
    add_skin(tongue, tgused, rig)

    clo = E.load_mhclo(os.path.join(E.MH_DATA, "eyes", "high-poly", "high-poly.mhclo"))
    eobj = E.load_obj(clo["obj_file"])
    ev = to_bl(E.fit_proxy(clo, VERTS_MH))
    me = bpy.data.meshes.new("R9_Eyes")
    me.from_pydata([tuple(p) for p in ev], [], [f[0] for f in eobj["faces"]])
    uvl = me.uv_layers.new(name="UVMap")
    k = 0
    for f in eobj["faces"]:
        for ti in f[1]:
            uvl.data[k].uv = tuple(eobj["uvs"][ti])
            k += 1
    for p in me.polygons:
        p.use_smooth = True
    eyes = bpy.data.objects.new("R9_Eyes", me)
    scene.collection.objects.link(eyes)
    # eyes follow eye bones: split by side
    vgl = eyes.vertex_groups.new(name="eye.L")
    vgr = eyes.vertex_groups.new(name="eye.R")
    for i, p in enumerate(ev):
        (vgl if p[0] > 0 else vgr).add([i], 1.0, "REPLACE")
    m = eyes.modifiers.new("Armature", "ARMATURE")
    m.object = rig
    eyes.parent = rig

    hair = None
    if PARAMS.get("hair"):
        import hair as H
        corr = json.load(open(PARAMS["face_corr"]))
        hp = H.load_params(PARAMS["hair"])
        mask, frame = H.build_patch(VERTS_MH, BASE, corr, hp)
        from mathutils.bvhtree import BVHTree
        hfaces = [BASE["faces"][fi][0] for fi in BASE["groups"]["body"]]
        tris = [(f[0], f[k], f[k + 1]) for f in hfaces for k in range(1, len(f) - 1)]
        bvh = BVHTree.FromPolygons([tuple(v) for v in VERTS_MH], tris)

        def raycast(origins, dirs):
            hit = np.zeros_like(origins)
            nrm = np.zeros_like(origins)
            ok = np.zeros(len(origins), bool)
            for i, (o, d) in enumerate(zip(origins, dirs)):
                loc, n, _, _ = bvh.ray_cast(Vector(o), Vector(d))
                if loc is not None:
                    hit[i], nrm[i], ok[i] = loc[:], n[:], True
            return hit, nrm, ok
        centre = VERTS_MH[frame["head_ids"]].mean(axis=0)
        hv, hf, huv, hstats = H.build_shell_raycast(raycast, centre, frame, hp)
        frame.update(hstats)
        hme = bpy.data.meshes.new("R9_Hair")
        hme.from_pydata([tuple(p) for p in to_bl(hv)], [], hf)
        uvl = hme.uv_layers.new(name="HairUV")
        for poly in hme.polygons:
            for li in poly.loop_indices:
                uvl.data[li].uv = tuple(huv[hme.loops[li].vertex_index])
        for poly in hme.polygons:
            poly.use_smooth = True
        hair = bpy.data.objects.new("R9_Hair", hme)
        scene.collection.objects.link(hair)
        vg = hair.vertex_groups.new(name="head")
        vg.add(list(range(len(hv))), 1.0, "REPLACE")
        m = hair.modifiers.new("Armature", "ARMATURE")
        m.object = rig
        hair.parent = rig
        # mark the patch on the head for texturing (shaved vs hair area)
        hmap = {o: n for n, o in enumerate(hused)}
        pg = head.vertex_groups.new(name="hair_patch")
        pg.add([hmap[i] for i in np.nonzero(mask)[0] if i in hmap], 1.0, "REPLACE")
        scene["r9_hair_frame"] = json.dumps({k: (float(v) if np.isscalar(v) else None)
                                             for k, v in frame.items() if k != "head_ids"})

    meta = {
        "params": PARAMS,
        "height_cm": float(E.height_cm(VERTS_MH, BASE)),
        "counts": {o.name: [len(o.data.vertices), len(o.data.polygons)] for o in
                   [body, head] + tobjs + [tongue, eyes, hair] if o},
        "bones": len(rig.data.bones),
    }
    scene["r9_build_meta"] = json.dumps(meta)
    print("R9_BUILD_META", json.dumps(meta))
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(args.out))
    if args.glb:
        bpy.ops.export_scene.gltf(filepath=os.path.abspath(args.glb), export_format="GLB",
                                  export_skins=True, export_yup=True)


main()
