"""Adversarial donors / targets for tools/wrap (independent review; ~8 min on 4 CPU cores, needs
Blender + astcenc). Every case lives in tools/wrap/adversarial.py and returns named checks; the
numbers land in <work>/adversarial_summary.json (README §6.5).

Environment:
  WRAP_ADV_WORK   keep the outputs there (default: pytest tmp dir)
  WRAP_ADV_BASE   reuse a synthetic.py work dir (target/target.npz + donor/) instead of rebuilding
                  (WRAP_TEST_WORK of test_wrap_synthetic.py works too)
"""
import json
import os
import shutil
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "wrap"))

pytestmark = pytest.mark.skipif(shutil.which("blender") is None or shutil.which("astcenc") is None,
                                reason="needs blender and astcenc on PATH")

_RESULTS = {}


@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    import adversarial as AD
    work = os.environ.get("WRAP_ADV_WORK") or str(tmp_path_factory.mktemp("wrap_adv"))
    if os.path.isdir(work) and os.listdir(work):
        shutil.rmtree(work)
    base = os.environ.get("WRAP_ADV_BASE") or os.environ.get("WRAP_TEST_WORK")
    c = AD.Ctx(work, base)
    yield c
    json.dump(_RESULTS, open(os.path.join(work, "adversarial_summary.json"), "w"), indent=1, default=float)


def _run(ctx, name):
    import adversarial as AD
    r = AD.CASES[name](ctx)
    _RESULTS[name] = r
    failed = [k for k, ok in r["checks"].items() if not ok]
    assert not failed, f"{name}: failed {failed}\n{json.dumps(r, default=float)[:3000]}"
    return r


def test_frames_mirror_units_orientation(ctx):
    """mirrored frame and mirrored display refused; cm-as-m + Y-up gives the base result; rigid with
    wrong units refused; a target in UE centimetres gives identical millimetre numbers."""
    _run(ctx, "frames")


def test_feature_guess_on_a_y_up_donor(ctx):
    _run(ctx, "guess_yup")


def test_feature_noise_typos_and_one_based_ids(ctx):
    r = _run(ctx, "features")
    assert r["noise"]["mean_feature_error_mm"] > 2.0          # the noise really is there


def test_eyes_teeth_eyelash_submeshes(ctx):
    """used to crash with 'Factor is exactly singular'"""
    _run(ctx, "submeshes")


def test_uv_seam_through_the_face(ctx):
    _run(ctx, "uv_seam")


def test_nonmanifold_fins_and_double_sided_decal(ctx):
    _run(ctx, "nonmanifold")


def test_very_coarse_lod1(ctx):
    _run(ctx, "coarse_lod1")


def test_hair_shell_in_the_target(ctx):
    _run(ctx, "hair")


def test_tangent_convention_against_donor_tangents(ctx):
    _run(ctx, "tangents")


def test_glb_node_transform_and_lod1_pick(ctx):
    _run(ctx, "glb")
