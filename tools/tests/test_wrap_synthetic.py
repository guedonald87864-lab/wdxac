"""Synthetic-donor proof of tools/wrap (the key test; ~5 min on 4 CPU cores, needs Blender + astcenc).

A fake donor with another face, another topology, fresh UVs, a LOD1 and dummy skin weights, stored
in a left-handed UE centimetre frame with a random rigid offset, is wrapped onto our head built
from model/source/r9_params_start.json (so the test depends on the repository only).
Every threshold is in tools/wrap/synthetic.py::CRITERIA and README §6.

Environment:
  WRAP_TEST_WORK   keep the outputs there (default: pytest tmp dir)
  WRAP_TEST_BLEND / WRAP_TEST_TEXTURES   use an existing head build instead of rebuilding
"""
import json
import os
import shutil
import sys

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "wrap"))

pytestmark = pytest.mark.skipif(shutil.which("blender") is None or shutil.which("astcenc") is None,
                                reason="needs blender and astcenc on PATH")


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    import synthetic as SY
    work = os.environ.get("WRAP_TEST_WORK") or str(tmp_path_factory.mktemp("wrap_synth"))
    if os.path.isdir(work) and os.listdir(work):
        shutil.rmtree(work)
    S = SY.run_pipeline(work, os.environ.get("WRAP_TEST_BLEND"), os.environ.get("WRAP_TEST_TEXTURES"),
                        guess=True)
    S["_work"] = work
    return S


def crit(S, k):
    c = S["criteria"][k]
    assert c["pass"], f"{k}: {c}"


@pytest.mark.parametrize("tag", ["lod0", "lod1"])
def test_vertex_count_order_uv_skin_unchanged(run, tag):
    b = run["buffers"][tag]
    assert b["vertex_count_equal"] and b["index_buffer_identical"] and b["uv_bit_identical"]
    assert b["joints_identical"] and b["weights_identical"]
    assert b["seam_duplicates_identical"]            # UV-seam duplicates moved identically: no cracks
    assert all(v in (True, None) for v in run["post_write_verification"][tag].values())
    assert b["max_displacement_mm"] > 5              # ... and the head really was reshaped


def test_face_surface_distance_both_ways(run):
    for k in ("face_d2t_mean_mm", "face_d2t_p95_mm", "face_t2d_mean_mm", "face_t2d_p95_mm"):
        crit(run, k)


def test_no_flipped_triangles(run):
    crit(run, "no_flipped_triangles_lod0")
    crit(run, "no_flipped_triangles_lod1")


def test_locked_neck_ring_does_not_move(run):
    crit(run, "locked_max_mm")


def test_lod1_follows_wrapped_lod0(run):
    for k in ("lod1_vertex_to_lod0_max_mm", "lod1_surface_to_lod0_mean_mm", "lod1_surface_to_lod0_p95_mm"):
        crit(run, k)


def test_semantic_correspondence_against_ground_truth(run):
    crit(run, "corr_face_mean_mm")
    crit(run, "corr_face_p95_mm")


def test_texture_transfer_colour(run):
    for k in ("landmark_dE00_p95_2048", "landmark_dE00_p90_256_astc", "face_surface_psnr_2048_db",
              "face_surface_psnr_256_astc_db", "render_front_face_psnr_2048_db", "render_front_face_dE00_mean_2048",
              "render_front_face_psnr_256_astc_db"):
        crit(run, k)
    s = run["texture"]["sizes"]
    assert s["256"]["astc"]["bytes"] == 64 * 64 * 16          # 256 px, ASTC 4x4 = 64x64 blocks of 16 B
    assert run["texture"]["bake"]["ray_hit_fraction"] > 0.9


def test_tangents_are_mikktspace_from_blender(run):
    assert run["wrap"]["tangents"]["method"].startswith("mikktspace")


def test_feature_guess_helper(run):
    g = run["guess"]
    assert g["basic_mean_mm"] < 6.0, g["feature_error_mm"]
    assert g["face_d2t"]["mean"] < 1.0 and g["face_t2d"]["mean"] < 1.0 and g["flipped"] == 0


def test_glb_donor_roundtrip(run, tmp_path):
    """Same donor as a GLB (glTF frame): reader keeps the order, the output GLB is a patched copy."""
    import glb as GLB
    import synthetic as SY
    import wrap as WR
    work = run["_work"]
    gpath = SY.donor_npz_to_glb(os.path.join(work, "donor", "donor.npz"), str(tmp_path / "donor.glb"))
    a = WR.build_argparser().parse_args(["--target", os.path.join(work, "target", "target.npz"), "--donor", gpath,
                                         "--features", os.path.join(work, "donor", "donor_features_gt.json"),
                                         "--out-dir", str(tmp_path / "out"), "--tangents", "none",
                                         "--metric-samples", "20000"])
    rep = WR.run(a)
    assert rep["surface_distance_mm"]["donor_to_target"]["face"]["mean"] < 1.0
    assert rep["mesh_checks"]["flipped_triangles"] == 0
    gin, gout = GLB.read_glb(gpath), GLB.read_glb(rep["outputs"]["glb"])
    for mi, mo in zip(gin["meshes"], gout["meshes"]):
        pi, po = mi["primitives"][0], mo["primitives"][0]
        assert np.array_equal(pi["faces"], po["faces"])
        assert np.array_equal(pi["attributes"]["TEXCOORD_0"], po["attributes"]["TEXCOORD_0"])
        assert np.array_equal(pi["attributes"]["_JOINTS_0"], po["attributes"]["_JOINTS_0"])
        assert len(pi["attributes"]["POSITION"]) == len(po["attributes"]["POSITION"])
        assert not np.allclose(pi["attributes"]["POSITION"], po["attributes"]["POSITION"])


def test_summary_written(run):
    p = os.path.join(run["_work"], "synthetic_summary.json")
    S = json.load(open(p))
    assert all(v["pass"] for v in S["criteria"].values())
