"""R9 2002 卡数据（data/game/r9_2002_player.json）的回归测试。

检查：模拟器校验零 ERROR/WARN；派生产物（JSON notes.derived、CSV、模拟报告）与输入一致；
设计约束（任务书 §6）：不超出客户端已知上限、特殊技能存在、增能存在、加点用满且与自动提升一致、
总评落在同类 Epic 锋线卡的范围内。规则均为社区逆向（T3），待客户端核实。
"""

import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools", "data"))
import efootball_rules as R  # noqa: E402
import progression_sim as S  # noqa: E402
import build_r9_2002 as B  # noqa: E402

CARD = os.path.join(ROOT, "data", "game", "r9_2002_player.json")


@pytest.fixture(scope="module")
def raw():
    with open(CARD, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def card(raw):
    c, issues = S.parse_card(raw)
    assert not issues
    return c


def test_simulator_cli_clean():
    p = subprocess.run([sys.executable, os.path.join("tools", "data", "progression_sim.py"), CARD, "--no-steps", "--no-levels"],
                       cwd=ROOT, capture_output=True, text=True)
    assert p.returncode == 0, p.stdout
    assert "--- 校验 ---\n  无问题" in p.stdout


def test_derived_outputs_up_to_date():
    assert B.main(["--check"]) == 0, "运行 python3 tools/data/build_r9_2002.py 重新生成"


def test_limits(card):
    assert all(R.STAT_MIN <= v <= R.STAT_MAX for v in card.stats.values())
    assert all(card.stats[k] == 40 for k in R.GK_STAT_KEYS)
    assert len(card.skills) <= R.MAX_CARD_SKILLS and len(set(card.skills)) == len(card.skills)
    assert card.additional_skills == []                       # 附加技能由玩家训练，出厂为空
    assert len(card.ai_styles) <= R.MAX_AI_STYLES
    assert len(card.boosters) <= R.MAX_BOOSTER_SLOTS
    assert 31 <= card.max_level <= 34                          # Epic 抽样范围（04 §5.2）


def test_skills_exist_and_specials(card, raw):
    specials = [s for s in card.skills if R.SKILLS[s].special]
    assert specials == ["Acceleration Burst", "Phenomenal Finishing"]
    for s in raw["notes"]["skills"]["card_skills"]:
        sk = R.SKILLS[R.resolve_skill(s["name"])]
        assert s["special"] == sk.special and s["trainable_as_additional"] == (not sk.special)
    for s in raw["notes"]["skills"]["additional_skills_recommended"]:
        assert not R.SKILLS[R.resolve_skill(s["name"])].special
    # 不放会在比赛中加能力的技能（避免与增能重复叠加）
    assert not any(R.SKILLS[s].stat_bonus for s in card.skills)


def test_boosters(card, raw):
    b1, b2 = card.boosters
    assert (b1.kind, b1.name, b1.value, b1.active) == ("fixed", "Striker's Instinct", 3, True)
    assert b2.kind == "craftable" and b2.name is None           # 出厂空槽
    rec = raw["notes"]["boosters"]["slot2_recommendation"]
    assert rec["name"] in R.REGULAR_BOOSTERS and rec["value"] == R.CRAFTED_BOOSTER_VALUE
    assert tuple(sorted(R.resolve_stat(s) for s in rec["stats"])) == tuple(sorted(R.REGULAR_BOOSTERS[rec["name"]].stats))


def test_allocation_is_auto_and_optimal(card):
    cats = R.categories_for_position("CF")
    g, _ = S.greedy_allocation(card.stats, card.height_cm, card.wfa_idx, "CF", card.budget, categories=cats)
    o = S.optimal_allocation(card.stats, card.budget, S.objective_weights({"CF": 1}), categories=cats)
    rec = {c: card.allocation.get(c, 0) for c in cats}
    assert g == rec == o
    assert R.allocation_cost(rec) == card.budget == 62


def test_ratings_and_design_band(card):
    mx = R.apply_training(card.stats, card.allocation)
    fixed = card.booster_bonus(("fixed",))
    ovr = lambda s: R.overall(s, card.height_cm, card.wfa_idx, "CF")  # noqa: E731
    assert ovr(card.stats) == 87
    assert ovr(mx) == 100
    assert ovr(R.apply_boosters(mx, fixed)) == 102
    assert not [k for k in R.STAT_KEYS if mx[k] >= 95]           # 满级中性没有 ≥95 的能力
    # 2002 年的设计红线（03 §4）：加速 > 速度，防守/头球/传中不拔高，体能偏低
    assert mx["acceleration"] > mx["speed"]
    assert mx["heading"] <= 74 and mx["lofted_pass"] <= 74 and mx["stamina"] <= 72
    assert max(mx[k] for k in ("defensive_awareness", "tackling", "aggression", "defensive_engagement")) <= 60
