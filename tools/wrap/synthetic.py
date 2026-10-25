#!/usr/bin/env python3
"""Synthetic-donor proof of the wrap pipeline (README §6). Used by tools/tests/test_wrap_synthetic.py.

  python3 tools/wrap/synthetic.py --work DIR [--blend r9.blend --textures TEXDIR] [--skip-guess]

1. target: our head (default: rebuilt from model/source/r9_params_start.json with
   tools/model/blender_build.py + texture_build.py, i.e. reproducible from the repo alone;
   --blend/--textures use an existing build instead) -> export_head.py (NPZ, blender frame)
2. donor: make_fake_donor.py (another MakeHuman face, remeshed, fresh UVs, LOD1, dummy skin,
   UE centimetre frame + random rigid offset, top-left UVs)
3. wrap.py with the ground-truth feature ids (the equivalent of a careful manual pick)
4. transfer_texture.py (2048 + 256, ASTC 4x4 round trip)
5. evaluation against ground truth that only a synthetic donor can give:
   * buffers: vertex count/order, index buffer, UVs and skin weights bit-identical to the donor
   * surface distance both ways (face region), flips, locked seam displacement in the donor frame
   * LOD1 vs wrapped LOD0
   * SEMANTIC correspondence: every donor vertex comes from a known point of the MakeHuman
     surface, so its ideal wrapped position is that same MakeHuman point on our head
   * colour at the MediaPipe landmarks of the face (CIEDE2000) and face-surface PSNR
6. (optional) the same wrap with feature ids guessed by wrap.guess_features, plus the guess error
Writes DIR/synthetic_summary.json with every number and the pass/fail of each criterion.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
import features as FT  # noqa: E402
import geom as G  # noqa: E402
import glb as GLB  # noqa: E402
import meshdata as MD  # noqa: E402
import transfer_texture as TT  # noqa: E402
import wrap as WR  # noqa: E402

CRITERIA = {
    "face_d2t_mean_mm": 1.0, "face_d2t_p95_mm": 2.5, "face_t2d_mean_mm": 1.0, "face_t2d_p95_mm": 2.5,
    "locked_max_mm": 0.1, "lod1_vertex_to_lod0_max_mm": 1.5, "lod1_surface_to_lod0_mean_mm": 1.5,
    "lod1_surface_to_lod0_p95_mm": 1.5, "landmark_dE00_p95_2048": 5.0, "landmark_dE00_p90_256_astc": 5.0,
    "face_surface_psnr_2048_db": 30.0, "face_surface_psnr_256_astc_db": 30.0,
    "corr_face_mean_mm": 3.0, "corr_face_p95_mm": 6.0,
    "render_front_face_psnr_2048_db": 30.0, "render_front_face_dE00_mean_2048": 2.0,
    "render_front_face_psnr_256_astc_db": 30.0,
}


def sh(cmd, log_path=None):
    t = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True)
    if log_path:
        open(log_path, "a").write(" ".join(cmd) + "\n" + r.stdout[-20000:] + r.stderr[-20000:] + "\n")
    if r.returncode != 0:
        raise RuntimeError(f"failed: {' '.join(cmd)}\n{r.stdout[-3000:]}\n{r.stderr[-3000:]}")
    return round(time.time() - t, 1)


def build_target(work, blend=None, textures=None, log=None):
    os.makedirs(work, exist_ok=True)
    t = {}
    if not blend:
        blend = os.path.join(work, "r9_target.blend")
        t["build_blend_s"] = sh(["blender", "-b", "--python", os.path.join(REPO, "tools/model/blender_build.py"), "--",
                                 "--params", os.path.join(REPO, "model/source/r9_params_start.json"), "--out", blend], log)
    if not textures:
        textures = os.path.join(work, "target_tex")
        t["build_textures_s"] = sh(["blender", "-b", blend, "--python", os.path.join(REPO, "tools/model/texture_build.py"),
                                    "--", "--corr", os.path.join(REPO, "model/source/face_corr_mediapipe478.json"),
                                    "--palette", os.path.join(REPO, "model/source/r9_skin_palette.json"),
                                    "--out", textures, "--size", "1024"], log)
    npz = os.path.join(work, "target.npz")
    t["export_s"] = sh(["blender", "-b", blend, "--python", os.path.join(HERE, "export_head.py"), "--",
                        "--out", npz, "--glb", os.path.join(work, "target.glb"), "--textures", textures], log)
    return npz, blend, textures, t


def wrap_args(target, donor, out, features=None, guess=False, extra=()):
    a = ["--target", target, "--donor", donor, "--out-dir", out]
    a += ["--features", features] if features else (["--guess-features"] if guess else [])
    return WR.build_argparser().parse_args(a + list(extra))


def semantic_error(target_npz, wrap_npz, gt_npz):
    """Distance (mm) between each wrapped donor vertex and its ground-truth position on our head."""
    T, _ = MD.load_npz(target_npz)
    R, _ = MD.load_npz(wrap_npz)
    gt = np.load(gt_npz)
    q = G.TriMeshQuery(gt["src_positions"], gt["src_faces"])
    c = q.closest(gt["lod0_blender"])
    mh = gt["src_mh_index"][gt["src_faces"][c["tri"]]]
    tpos = {int(m): i for i, m in enumerate(T["head.mh_index"])}
    ok = np.array([all(int(x) in tpos for x in r) for r in mh])
    tP = MD.frame_to_blender(T["head.positions"], json.loads(str(np.load(target_npz)["meta"]))["frame"])
    ideal = np.zeros((len(mh), 3))
    for i in np.nonzero(ok)[0]:
        ideal[i] = sum(b * tP[tpos[int(m)]] for m, b in zip(mh[i], c["bary"][i]))
    err = np.linalg.norm(R["lod0.work_positions"] - ideal, axis=1) * 1000
    lab, nod, lk = R["lod0.region_label"], R["lod0.nodata"], R["lod0.locked"]
    out = {"gt_embedding_mm": G.stats_mm(c["dist"])}
    for name, rid in FT.REGION_ID.items():
        m = ok & (lab == rid) & ~nod & ~lk
        if m.sum():
            out[name] = G.stats_mm(err[m] / 1000)
    m = ok & nod
    if m.any():
        out["nodata"] = G.stats_mm(err[m] / 1000)
    return out


def render_space_check(target_npz, wrap_npz, target_tex, donor_texs, out_dir, log_path=None, res=640):
    """Render HD head and wrapped donor with Blender (tools/wrap/render_compare.py) and compare the
    images pixel by pixel where both are covered (alpha == 255, 2 px eroded): PSNR and CIEDE2000,
    over the whole overlap and inside the projected MediaPipe face oval box."""
    from scipy.ndimage import binary_erosion
    from PIL import Image
    cmd = ["blender", "-b", "--factory-startup", "--python", os.path.join(HERE, "render_compare.py"), "--",
           "--target", target_npz, "--wrap", wrap_npz, "--target-tex", target_tex, "--out-dir", out_dir,
           "--res", str(res)]
    for t in donor_texs:
        cmd += ["--donor-tex", t]
    sh(cmd, log_path)
    info = json.load(open(os.path.join(out_dir, "render_compare.json")))
    out = {}
    for v, vi in info["views"].items():
        A = np.asarray(Image.open(os.path.join(out_dir, f"{v}_target.png")), np.float64) / 255
        for k in range(len(donor_texs)):
            B = np.asarray(Image.open(os.path.join(out_dir, f"{v}_donor{k}.png")), np.float64) / 255
            m = binary_erosion((A[..., 3] > 0.999) & (B[..., 3] > 0.999), iterations=2)
            x0, y0, x1, y1 = (int(round(c)) for c in vi["face_box"])
            fb = np.zeros_like(m)
            fb[max(0, y0):y1, max(0, x0):x1] = True
            for reg, mm in (("overlap", m), ("face_box", m & fb)):
                a, b = A[..., :3][mm], B[..., :3][mm]
                de = TT.delta_e2000(TT.srgb_to_lab(a), TT.srgb_to_lab(b))
                out[f"{v}/{os.path.basename(donor_texs[k])}/{reg}"] = {
                    "pixels": int(mm.sum()), "psnr_db": TT.psnr(a, b), "dE00_mean": float(de.mean()),
                    "dE00_p95": float(np.percentile(de, 95)), "frac_dE00_lt5": float((de < 5).mean())}
    return out


def donor_npz_to_glb(npz, glb_path):
    """Re-express a donor NPZ as a GLB (glTF frame, metres, top-left UVs), one mesh per LOD, same
    vertex order - to exercise wrap.py's GLB reader / patch writer."""
    D, m = MD.load_npz(npz)
    gl = MD.frame_def("gltf")
    meshes = []
    for tag, pre in (("lod0", ""), ("lod1", "lod1.")):
        if pre + "positions" not in D:
            continue
        Pb = MD.frame_to_blender(D[pre + "positions"], m["frame"])
        Nb = MD.dir_frame_to_blender(D[pre + "normals"], m["frame"])
        F = D[pre + "faces"]
        if m["frame"]["det"] * gl["det"] < 0:
            F = F[:, ::-1]
        uv = MD.uv_from_bottom_left(MD.uv_to_bottom_left(D[pre + "uv0"], m["uv_origin"]), "top-left")
        meshes.append({"name": tag, "positions": MD.blender_to_frame(Pb, gl), "normals": MD.dir_blender_to_frame(Nb, gl),
                       "faces": F, "uv0": uv, "joints": D[pre + "joints"], "weights": D[pre + "weights"]})
    GLB.write_glb(glb_path, meshes)
    return glb_path


def buffer_checks(donor_npz, wrap_npz, report):
    D, dm = MD.load_npz(donor_npz)
    R, rm = MD.load_npz(wrap_npz)
    out = {}
    for tag, pre in (("lod0", ""), ("lod1", "lod1.")):
        if pre + "positions" not in D:
            continue
        P0, P1 = D[pre + "positions"], R[f"{tag}.positions"]
        u = 1000.0 / dm["frame"]["scale"]                     # native units -> mm
        lk = R["lod0.locked"] if tag == "lod0" else None
        o = {"vertex_count_equal": bool(len(P0) == len(P1)),
             "index_buffer_identical": bool(np.array_equal(D[pre + "faces"], R[f"{tag}.faces"])),
             "uv_bit_identical": bool(np.array_equal(D[pre + "uv0"], R[f"{tag}.uv0"])),
             "joints_identical": bool(np.array_equal(D[pre + "joints"], R[f"{tag}.joints"])),
             "weights_identical": bool(np.array_equal(D[pre + "weights"], R[f"{tag}.weights"])),
             "max_displacement_mm": float(np.linalg.norm(P1 - P0, axis=1).max() * u)}
        if lk is not None:
            o["locked_max_displacement_mm_native"] = float(np.linalg.norm(P1[lk] - P0[lk], axis=1).max() * u)
        wid, rep = G.weld(P0)
        o["seam_duplicates_identical"] = bool(np.array_equal(P1, P1[rep][wid]))
        out[tag] = o
    return out


def run_pipeline(work, blend=None, textures=None, guess=True, glb_check=True, log=print):
    t0 = time.time()
    os.makedirs(work, exist_ok=True)
    logf = os.path.join(work, "subprocess.log")
    S = {"timings_s": {}}
    tnpz, blend, textures, tt = build_target(os.path.join(work, "target"), blend, textures, logf)
    S["timings_s"].update(tt)
    S["target_blend"] = blend
    dd = os.path.join(work, "donor")
    S["timings_s"]["fake_donor_s"] = sh(["blender", "-b", "--python", os.path.join(HERE, "make_fake_donor.py"), "--",
                                         "--out", dd], logf)
    donor, gt_feat, gt_npz = (os.path.join(dd, f) for f in ("donor.npz", "donor_features_gt.json", "donor_gt.npz"))
    dmeta = MD.load_npz(donor)[1]
    S["donor"] = {k: dmeta[k] for k in ("lod0_gpu_verts", "lod1_gpu_verts", "frame", "uv_origin")}
    log("donor", S["donor"]["lod0_gpu_verts"], S["donor"]["lod1_gpu_verts"])
    t = time.time()
    rep = WR.run(wrap_args(tnpz, donor, os.path.join(work, "wrap"), features=gt_feat))
    S["timings_s"]["wrap_s"] = round(time.time() - t, 1)
    wnpz = rep["outputs"]["npz"]
    t = time.time()
    trep = TT.run(TT.build_argparser().parse_args(["--wrap", wnpz, "--target", tnpz,
                                                   "--out-dir", os.path.join(work, "tex")]))
    S["timings_s"]["texture_s"] = round(time.time() - t, 1)
    sd = rep["surface_distance_mm"]
    S["wrap"] = {"alignment": rep["alignment"], "face_d2t": sd["donor_to_target"]["face"],
                 "face_t2d": sd["target_to_donor"]["face"], "all_d2t": sd["donor_to_target"]["all"],
                 "all_t2d": sd["target_to_donor"]["all"], "per_region": sd, "mesh_checks": rep["mesh_checks"],
                 "lod1": rep["lod1"], "locked_max_mm_work": rep["locked_max_displacement_mm"],
                 "displacement_mm": rep["displacement_mm"], "regions": rep["donor_regions"],
                 "tangents": rep["lod0_buffers"]["tangents"], "nicp_iterations": rep["nicp"]["iterations"],
                 "flip_guarded": rep["nicp"]["flip_guarded_vertices"]}
    S["buffers"] = buffer_checks(donor, wnpz, rep)
    S["post_write_verification"] = rep["post_write_verification"]
    S["semantic_correspondence_mm"] = semantic_error(tnpz, wnpz, gt_npz)
    cc = trep["consistency_check"]
    S["texture"] = {"bake": trep["bake"], "sizes": trep["sizes"], "check": cc}
    tex_dir = os.path.join(work, "tex")
    t = time.time()
    S["render_space"] = render_space_check(
        tnpz, wnpz, json.loads(str(np.load(tnpz)["meta"]))["textures"]["r9_head_albedo"],
        [os.path.join(tex_dir, "donor_albedo_2048.png"), os.path.join(tex_dir, "donor_albedo_256_astc4x4_decoded.png")],
        os.path.join(work, "render_compare"), logf)
    S["timings_s"]["render_compare_s"] = round(time.time() - t, 1)
    if glb_check:
        g = GLB.read_glb(rep["outputs"]["glb"])
        S["glb_out"] = {m["name"]: sum(len(p["attributes"]["POSITION"]) for p in m["primitives"]) for m in g["meshes"]}
    # ---------------------------------------------------------------- criteria
    c = {}
    fd, ft = S["wrap"]["face_d2t"], S["wrap"]["face_t2d"]
    vals = {"face_d2t_mean_mm": fd["mean"], "face_d2t_p95_mm": fd["p95"], "face_t2d_mean_mm": ft["mean"],
            "face_t2d_p95_mm": ft["p95"],
            "locked_max_mm": S["buffers"]["lod0"]["locked_max_displacement_mm_native"],
            "lod1_vertex_to_lod0_max_mm": rep["lod1"]["vertex_to_wrapped_lod0_mm"]["max"],
            "lod1_surface_to_lod0_mean_mm": rep["lod1"]["surface_to_wrapped_lod0_mm"]["mean"],
            "lod1_surface_to_lod0_p95_mm": rep["lod1"]["surface_to_wrapped_lod0_mm"]["p95"],
            "landmark_dE00_p95_2048": cc["landmarks_face/albedo_2048"]["dE00_p95"],
            "landmark_dE00_p90_256_astc": cc["landmarks_face/albedo_256_astc"]["dE00_p90"],
            "face_surface_psnr_2048_db": cc["face_surface/albedo_2048"]["psnr_db"],
            "face_surface_psnr_256_astc_db": cc["face_surface/albedo_256_astc"]["psnr_db"],
            "render_front_face_psnr_2048_db": S["render_space"]["front/donor_albedo_2048.png/face_box"]["psnr_db"],
            "render_front_face_dE00_mean_2048": S["render_space"]["front/donor_albedo_2048.png/face_box"]["dE00_mean"],
            "render_front_face_psnr_256_astc_db":
                S["render_space"]["front/donor_albedo_256_astc4x4_decoded.png/face_box"]["psnr_db"],
            "corr_face_mean_mm": S["semantic_correspondence_mm"]["face"]["mean"],
            "corr_face_p95_mm": S["semantic_correspondence_mm"]["face"]["p95"]}
    for k, lim in CRITERIA.items():
        hi_is_good = k.endswith("_db")
        c[k] = {"value": vals[k], "limit": lim, "pass": bool(vals[k] > lim if hi_is_good else vals[k] < lim)}
    for tag in ("lod0", "lod1"):
        b = S["buffers"][tag]
        c[f"{tag}_buffers_identical"] = {"pass": bool(b["vertex_count_equal"] and b["index_buffer_identical"] and
                                                      b["uv_bit_identical"] and b["joints_identical"] and
                                                      b["weights_identical"] and b["seam_duplicates_identical"])}
    c["no_flipped_triangles_lod0"] = {"value": rep["mesh_checks"]["flipped_triangles"], "pass": rep["mesh_checks"]["flipped_triangles"] == 0}
    c["no_flipped_triangles_lod1"] = {"value": rep["lod1"]["mesh_checks"]["flipped_triangles"],
                                      "pass": rep["lod1"]["mesh_checks"]["flipped_triangles"] == 0}
    S["criteria"] = c
    # ---------------------------------------------------------------- optional: guessed features
    if guess:
        t = time.time()
        g_rep = WR.run(wrap_args(tnpz, donor, os.path.join(work, "wrap_guess"), guess=True, extra=["--tangents", "none"]))
        S["timings_s"]["wrap_guess_s"] = round(time.time() - t, 1)
        gf = json.load(open(os.path.join(work, "wrap_guess", "guessed_features.json")))["features"]
        gtf = json.load(open(gt_feat))["features"]
        P = MD.load_npz(donor)[0]["positions"]
        u = 1000.0 / dmeta["frame"]["scale"]
        S["guess"] = {"feature_error_mm": {k: float(np.linalg.norm(P[gf[k]] - P[gtf[k]]) * u) for k in gf if k in gtf},
                      "face_d2t": g_rep["surface_distance_mm"]["donor_to_target"]["face"],
                      "face_t2d": g_rep["surface_distance_mm"]["target_to_donor"]["face"],
                      "flipped": g_rep["mesh_checks"]["flipped_triangles"],
                      "edge_ratio": g_rep["mesh_checks"]["edge_ratio"],
                      "semantic_correspondence_mm": semantic_error(tnpz, g_rep["outputs"]["npz"], gt_npz)}
        e = S["guess"]["feature_error_mm"]
        S["guess"]["basic_mean_mm"] = float(np.mean([e[k] for k in FT.BASIC if k in e]))
    S["timings_s"]["total_s"] = round(time.time() - t0, 1)
    json.dump(S, open(os.path.join(work, "synthetic_summary.json"), "w"), indent=1, default=float)
    return S


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", required=True)
    ap.add_argument("--blend")
    ap.add_argument("--textures")
    ap.add_argument("--skip-guess", action="store_true")
    a = ap.parse_args()
    if os.path.exists(a.work) and os.listdir(a.work):
        shutil.rmtree(a.work)
    S = run_pipeline(a.work, a.blend, a.textures, guess=not a.skip_guess)
    for k, v in S["criteria"].items():
        print(f"{'PASS' if v['pass'] else 'FAIL'} {k} {v.get('value', '')} (limit {v.get('limit', '-')})")
    print("total", S["timings_s"]["total_s"], "s ->", os.path.join(a.work, "synthetic_summary.json"))
