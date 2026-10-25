"""R9 2002 卡数据（data/game/r9_2002_player.json）的回归测试。

检查：模拟器校验零 ERROR/WARN（唯一的 INFO 是"卡面技能少于 10 个"）；派生产物（JSON notes.derived、CSV、模拟报告）与输入一致；
设计约束（任务书 §6）：不超出客户端已知上限、特殊技能存在、增能存在、加点用满且与自动提升一致、
总评落在同类 Epic 锋线卡的范围内；模拟专用字段不在卡数据层（审查 M4）。规则均为社区逆向（T3），待客户端核实。
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
    check = p.stdout.split("--- 校验 ---\n", 1)[1].split("\n\n", 1)[0].strip().splitlines()
    assert len(check) == 1 and check[0].strip().startswith("[INFO] SKILL_CARD_COUNT"), check


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
    assert card.skills == ["Scissors Feint", "Cut Behind & Turn", "Acceleration Burst", "First-time Shot",
                           "Phenomenal Finishing", "Gamesmanship"]
    assert "Acrobatic Finishing" not in card.skills            # 审查 A3：与 PF 同一进球、不重复计证
    specials = [s for s in card.skills if R.SKILLS[s].special]
    assert specials == ["Acceleration Burst", "Phenomenal Finishing"]
    assert raw["notes"]["skills"]["card_skill_count"] == len(card.skills)
    # 瞄准型技能只有 PF（不受 99 限，审查 H2）；它的有效值写在 notes.derived.aim_effective
    assert [s for s in card.skills if R.SKILLS[s].aim_bonus] == ["Phenomenal Finishing"]
    aim = raw["notes"]["derived"]["aim_effective"]["Phenomenal Finishing"]["values"]
    assert aim["比赛中 CF（完整培养 + 主教练 89）"]["Finishing"] == {"stat": 97, "effective": 104}
    assert [x["name"] for x in raw["notes"]["skills"]["additional_skills_recommended"]] == ["One-touch Pass"]
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


def test_ratings_and_design_band(card, raw):
    mx = R.apply_training(card.stats, card.allocation)
    fixed = card.booster_bonus(("fixed",))
    ovr = lambda s: R.overall(s, card.height_cm, card.wfa_idx, "CF")  # noqa: E731
    assert ovr(card.stats) == 87
    assert ovr(mx) == 100
    assert ovr(R.apply_boosters(mx, fixed)) == 102
    o = raw["notes"]["derived"]["overall"]
    assert (o["MAX+FIXED+CRAFTED"]["CF"], o["MAX+FIXED+CRAFTED+MANAGER"]["CF"]) == (102, 107)
    # 审查后的关键值（A1、A2、B6、C10）
    assert (mx["acceleration"], mx["finishing"], mx["dribbling"], mx["tight_possession"], mx["lofted_pass"]) == (89, 91, 88, 88, 69)
    assert not [k for k in R.STAT_KEYS if mx[k] >= 95]           # 满级中性没有 ≥95 的能力
    # 2002 年的设计红线（03 §4）：加速 > 速度，防守/头球/传中不拔高，体能偏低
    assert mx["acceleration"] > mx["speed"]
    assert mx["heading"] <= 74 and mx["lofted_pass"] <= 74 and mx["stamina"] <= 72
    assert max(mx[k] for k in ("defensive_awareness", "tackling", "aggression", "defensive_engagement")) <= 60


def test_simulation_fields_not_in_card_layer(raw):
    """审查 M4：推荐加点和主教练假设只在 simulation 下，客户端只写 client_apply_fields。"""
    assert "allocation" not in raw and "manager" not in raw
    assert set(raw["simulation"]) == {"allocation", "manager"}
    apply = raw["notes"]["client_apply_fields"]["fields"]
    assert not set(apply) & {"simulation", "notes", "allocation", "manager", "objective"}
    assert set(apply) <= set(raw) and "simulation" in raw["notes"]["client_apply_fields"]["never_apply"]


def test_auto_plan_margin_documented(card, raw):
    """审查 H3：第一名与第二名的差距如实记录，两者显示总评相同。"""
    cats = R.categories_for_position("CF")
    top = S.top_allocations(card.stats, card.height_cm, card.wfa_idx, "CF", card.budget, categories=cats, k=2)
    a = raw["notes"]["derived"]["allocation"]
    assert a["margin_first_second_per_1000"] == top[0][0] - top[1][0] == 13
    assert {k: v for k, v in top[0][2].items() if v} == a["recommended"]
    assert top[0][0] // 1000 == top[1][0] // 1000 == 100
    second = raw["notes"]["progression"]["second_auto_candidate"]["allocation"]
    assert {k: v for k, v in top[1][2].items() if v} == second
    # 审查 H4：熟练度 89 时含主教练的自动提升仍是推荐方案
    for model in ("efx", "pesdb"):
        assert "89" not in raw["notes"]["derived"]["auto_plan_under_manager"][model]["plan_differs_at"]
    # 模拟器的 --auto-with-manager 与派生数据一致（DESIGN §11 第 3 条命令）
    c, _ = S.parse_card(json.loads(json.dumps(raw)))
    res = S.simulate(c, allocation="auto", manager_p=84, manager_model="pesdb", per_level=False, auto_with_manager=True)
    assert {k: v for k, v in res.alloc.items() if v} == \
        raw["notes"]["derived"]["auto_plan_under_manager"]["pesdb"]["plan_differs_at"]["84"]["allocation"]
    assert not [i for i in res.issues if i.level in ("ERROR", "WARN")]


def test_profile_fields(raw):
    """审查 B7、B8、M6。"""
    assert (raw["weak_foot_usage"], raw["weak_foot_accuracy"], raw["form"], raw["live_update_rating"]) == \
        ("Occasionally", "High", "Unwavering", "B")
    for k in ("weak_foot_usage", "form", "live_update_rating"):
        assert raw["notes"]["other_fields"][k]["value"] == raw[k]
