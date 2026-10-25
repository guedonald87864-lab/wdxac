"""data/body/r9_body_params.json 与 tools/model/body_params_estimate.py 的一致性测试。

  * 14 项候选值合法（0–14）、落在各自范围内、codec_patch 与候选值一致；身高 183、体重 82（80–84）；
  * 对照表逐值等于 02 号档案 §6.2 正文（解析自档案，不手抄），11 项候选都落在 02 §6.4 的建议区间内；
  * 模型估计（T4，PES 2021 刻度）可由脚本重新算出（与已存数值相差 ≤ 0.05 档），对照统计可重算；
  * 中性模型确实是"同身高同质量 / 同 weight 宏 / PES 标准体重"三种定义。
这些测试只说明候选值自洽、可复现；候选值是否正确必须实机标定（见 data/body/BODY_PARAMS.md）。
"""

import json
import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "tools", "model"))
sys.path.insert(0, os.path.join(ROOT, "tools", "data"))

import player_appearance as PA  # noqa: E402

PARAMS_JSON = os.path.join(ROOT, "data", "body", "r9_body_params.json")
DOSSIER02 = os.path.join(ROOT, "research", "notes", "02_body_2002.md")


@pytest.fixture(scope="module")
def doc():
    with open(PARAMS_JSON, encoding="utf-8") as fh:
        return json.load(fh)


def _dossier02():
    with open(DOSSIER02, encoding="utf-8") as fh:
        return fh.read()


def test_candidates_legal_and_consistent(doc):
    P = doc["params"]
    assert list(P) == list(PA.BODY_FIELDS)
    for name, p in P.items():
        lo, hi = p["range"]
        assert 0 <= lo <= p["candidate"] <= hi <= 14, name
        assert p["calibration"]["view"] and p["calibration"]["direction"] and p["reasoning"], name
        assert doc["codec_patch"][name] == p["candidate"]
        me = p["model_estimate_T4"]
        assert {"primary_B", "variant_A", "variant_G"} <= set(me)
    assert doc["height_cm"]["value"] == 183
    assert 80 <= doc["weight_kg"]["value"] <= 84 and doc["weight_kg"]["acceptable"] == [80, 84]
    assert doc["status"] == "已完成候选、等待实机"
    assert "T4" in doc["model_estimate_T4"]["label"] and "PES 2021" in doc["model_estimate_T4"]["label"]


def test_codec_patch_is_accepted_by_codec(doc):
    """codec_patch 能被编解码器接受：只改 14 个身体半字节，其余逐位保留（三种布局）。"""
    import random
    vals = {k: v for k, v in doc["codec_patch"].items() if not k.startswith("_")}
    for lid in PA.LAYOUT_IDS:
        lay = PA.load_layout(lid)
        rec = random.Random(lid).randbytes(lay.record_size)
        new, _ = lay.apply(rec, {k: lay.resolve_value(k, v) for k, v in vals.items()})
        mask = 0
        for n in PA.BODY_FIELDS:
            mask |= lay.by_name[n].mask
        assert (int.from_bytes(new, "little") ^ int.from_bytes(rec, "little")) & ~mask == 0
        assert {n: lay.get(new, n) for n in PA.BODY_FIELDS} == vals


def test_comparator_table_matches_dossier_6_2(doc):
    txt = _dossier02()
    sec = txt[txt.index("### 6.2"):txt.index("### 6.3")]
    rows = {}
    for line in sec.splitlines():
        m = re.match(r"\|\s*([^|]+?)\s*\|\s*[^|]+?\s*\|\s*(\d+) / \*{0,2}(\d+)\*{0,2}\s*\|((?:\s*\*{0,2}\d+\*{0,2}\s*\|){11})",
                     line)
        if m:
            rows[m.group(1)] = [int(x.strip().strip("*")) for x in m.group(4).split("|") if x.strip()]
    tab = doc["comparator_table"]["players"]
    assert len(rows) == 12
    for name, vals in rows.items():
        assert tab[name]["values"] == vals, name
    assert {k for k, v in tab.items() if v["role"] == "anchor"} == {"Karim Benzema", "Christian Vieri", "Luis Suárez",
                                                                     "Andriy Shevchenko"}
    assert doc["comparator_table"]["param_order"] == list(PA.BODY_FIELDS[:11])


def test_candidates_inside_dossier_6_4_ranges(doc):
    txt = _dossier02()
    sec = txt[txt.index("### 6.4"):txt.index("## 7.")]
    found = 0
    for line in sec.splitlines():
        m = re.match(r"\|\s*([A-Za-z]+)\s*\|\s*\*{0,2}(\d+)–(\d+)\*{0,2}\s*\|", line)
        if m and m.group(1) in doc["params"]:
            lo, hi = int(m.group(2)), int(m.group(3))
            assert lo <= doc["params"][m.group(1)]["candidate"] <= hi, m.group(1)
            found += 1
    assert found == 11


def test_model_estimate_reproducible(doc):
    import body_params_estimate as BP
    res = BP.estimate()
    assert BP.check_against(PARAMS_JSON, res) == []
    v = res["variants"]
    assert abs(v["B"]["height_cm"] - 183.0) < 0.05 and abs(v["B"]["mass_kg"] - res["r9"]["mass_kg"]) < 0.05
    assert abs(v["A"]["height_cm"] - 183.0) < 0.05 and v["A"]["macro"]["weight"] == round(
        json.load(open(BP.R9_PARAMS))["macro"]["weight"], 4)
    assert abs(v["G"]["mass_kg_before_pes_weight"] - BP.PES_WSTD_183) < 0.05
    for var in ("B", "A", "G"):
        assert v[var]["macro"]["muscle"] == 0.5 and v[var]["macro"]["proportions"] == 0.5
    assert abs(res["r9"]["height_cm"] - 183.0) < 0.05
