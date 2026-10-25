"""Shared material setup for R9 review renders (imported inside Blender).

apply_materials(textures_dir, kit=False) assigns the skin/eye/teeth/hair materials used by
tools/model/blender_render.py and tools/face/blender_render_cams.py so every review render
uses identical shading. Colours: model/source/r9_skin_palette.json (linear values below).
"""
import os

import bpy

SKIN = (0.462, 0.231, 0.111)   # linear of sRGB #B5845E (model/source/r9_skin_palette.json)


def principled(name, color, rough=0.5, sss=0.0, tex=None, alpha_tex=False, normal_tex=None, spec=0.5):
    m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    bs = nt.nodes.new("ShaderNodeBsdfPrincipled")
    nt.links.new(bs.outputs[0], out.inputs[0])
    bs.inputs["Base Color"].default_value = (*color, 1)
    bs.inputs["Roughness"].default_value = rough
    if "Specular IOR Level" in bs.inputs:
        bs.inputs["Specular IOR Level"].default_value = spec
    if sss > 0:
        bs.inputs["Subsurface Weight"].default_value = sss
        bs.inputs["Subsurface Radius"].default_value = (1.0, 0.35, 0.2)
        bs.inputs["Subsurface Scale"].default_value = 0.004
    if tex:
        ti = nt.nodes.new("ShaderNodeTexImage")
        ti.image = bpy.data.images.load(tex, check_existing=True)
        nt.links.new(ti.outputs["Color"], bs.inputs["Base Color"])
        if alpha_tex:
            nt.links.new(ti.outputs["Alpha"], bs.inputs["Alpha"])
            m.blend_method = "HASHED"
    if normal_tex:
        tn = nt.nodes.new("ShaderNodeTexImage")
        tn.image = bpy.data.images.load(normal_tex, check_existing=True)
        tn.image.colorspace_settings.name = "Non-Color"
        nm = nt.nodes.new("ShaderNodeNormalMap")
        nt.links.new(tn.outputs["Color"], nm.inputs["Color"])
        nt.links.new(nm.outputs["Normal"], bs.inputs["Normal"])
    return m


def assign(obname, mat, uv=None):
    o = bpy.data.objects.get(obname)
    if not o:
        return
    o.data.materials.clear()
    o.data.materials.append(mat)
    if uv and uv in o.data.uv_layers:
        o.data.uv_layers.active = o.data.uv_layers[uv]
        for l in o.data.uv_layers:
            l.active_render = (l.name == uv)


def apply_materials(textures="", kit=False):
    def tex_path(name):
        if not textures:
            return None
        p = os.path.join(textures, name)
        return p if os.path.exists(p) else None

    assign("R9_Head", principled("Skin_Head", SKIN, 0.55, 0.15, tex_path("r9_head_albedo.png"),
                                 normal_tex=tex_path("r9_head_normal.png")), "HeadUV")
    body_tex = tex_path("r9_body_kit_albedo.png") if kit else tex_path("r9_body_albedo.png")
    assign("R9_Body", principled("Skin_Body", SKIN, 0.55, 0.15, body_tex), "UVMap")
    assign("R9_Eyes", principled("Eyes", (0.9, 0.9, 0.9), 0.15, 0.0,
                                 tex_path("r9_eye_albedo.png") or "/opt/tools/mh/makehuman/makehuman/data/eyes/materials/brown_eye.png"))
    assign("R9_Teeth", principled("Teeth", (0.70, 0.66, 0.58), 0.3, 0.05))
    assign("R9_Tongue", principled("Tongue", (0.55, 0.22, 0.22), 0.4, 0.1))
    if bpy.data.objects.get("R9_Hair"):
        assign("R9_Hair", principled("Hair", (0.012, 0.010, 0.009), 0.9, 0.0, tex_path("r9_hair_albedo.png"),
                                     normal_tex=tex_path("r9_hair_normal.png"), spec=0.15))
