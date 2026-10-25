#!/usr/bin/env python3
"""由 data/game/r9_2002_player.json 生成派生数据：JSON 的 notes.derived、平表 CSV、模拟器报告。

    python3 tools/data/build_r9_2002.py           # 重新生成三个产物
    python3 tools/data/build_r9_2002.py --check   # 只检查产物是否与 JSON 输入一致（不一致时退出码 1）

输入只有 JSON 顶层字段（progression_sim.py 的输入格式，模拟专用的 allocation/manager 在 simulation 下）和 notes 中手写的设计记录。
本脚本不引入任何新规则：全部计算调用 tools/data/progression_sim.py 和 efootball_rules.py
（规则出处见 04 号档案；社区逆向的规则和数值均为"待客户端核实"）。
"""

from __future__ import annotations

import argparse
import copy
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import efootball_rules as R  # noqa: E402
import progression_sim as S  # noqa: E402

CARD = os.path.join(ROOT, "data", "game", "r9_2002_player.json")
CSV_OUT = os.path.join(ROOT, "data", "game", "r9_2002_player.csv")
REPORT = os.path.join(ROOT, "data", "game", "simulation_report.txt")
SIM = os.path.join("tools", "data", "progression_sim.py")
TAG = "社区逆向（T3）+ 本组推断（T4），待客户端核实"

STAGES = {
    "L1": "1 级卡面（无增能）",
    "L1+FIXED": "1 级 + 固定增能（slot 1）",
    "MAX": "满级中性（培养后，无增能、无主教练）",
    "MAX+FIXED": "满级 + 固定增能（出厂状态）",
    "MAX+FIXED+CRAFTED": "满级 + 固定增能 + 推荐制作增能（slot 2）",
    "MAX+FIXED+MANAGER": "满级 + 典型主教练熟练度 + 固定增能",
    "MAX+FIXED+CRAFTED+MANAGER": "满级 + 典型主教练熟练度 + 固定增能 + 推荐制作增能（完整培养）",
}


def _load() -> Dict[str, Any]:
    with open(CARD, encoding="utf-8") as fh:
        return json.load(fh)


def _crafted_variant(d: Dict[str, Any], name: str) -> Dict[str, Any]:
    v = copy.deepcopy(d)
    v["boosters"] = [b for b in v["boosters"] if b.get("slot") != 2] + [
        {"slot": 2, "kind": "craftable", "name": name, "value": R.CRAFTED_BOOSTER_VALUE, "active": True}]
    return v


def _sim(d: Dict[str, Any], allocation=None, per_level=True) -> S.Result:
    card, issues = S.parse_card(copy.deepcopy(d))
    res = S.simulate(card, allocation=allocation, per_level=per_level)
    res.issues = issues + res.issues
    return res


def _nz(a: Dict[str, int]) -> Dict[str, int]:
    return {k: v for k, v in a.items() if v}


def _auto_under_manager(card: S.PlayerCard, rec: Dict[str, int]) -> Dict[str, Any]:
    """H4：如果自动提升按"比赛计划下（含主教练）"的总评最大化，哪些熟练度会改变加点方案。"""
    pos, cats = card.registered_position, R.categories_for_position(card.registered_position)
    out: Dict[str, Any] = {}
    for model, rng in (("efx", range(40, 100)), ("pesdb", range(70, 91))):
        diff = {}
        for p in rng:
            g, _ = S.greedy_allocation(card.stats, card.height_cm, card.wfa_idx, pos, card.budget, categories=cats,
                                       manager_p=p, manager_model=model)
            if _nz(g) != _nz(rec):
                mx = R.apply_training(card.stats, g)
                fx = R.apply_boosters(mx, card.booster_bonus(("fixed",)))
                diff[str(p)] = {"allocation": _nz(g),
                                "overall_" + pos + "_max": R.overall(mx, card.height_cm, card.wfa_idx, pos),
                                "overall_" + pos + "_max_fixed": R.overall(fx, card.height_cm, card.wfa_idx, pos)}
        out[model] = {"proficiency_range": [rng.start, rng.stop - 1], "plan_differs_at": diff}
    return out


def _level_up_drops(card: S.PlayerCard) -> List[Dict[str, Any]]:
    """L8：每级整体重分配（pesdb 贪心复现）时，升级后下降的能力。"""
    pos, cats = card.registered_position, R.categories_for_position(card.registered_position)
    out, prev = [], None
    for L in range(1, card.max_level + 1):
        a, _ = S.greedy_allocation(card.stats, card.height_cm, card.wfa_idx, pos, R.progression_points(L), categories=cats)
        st = R.apply_training(card.stats, a)
        if prev is not None:
            drops = {R.STAT_BY_KEY[k].en: [prev[k], st[k]] for k in R.STAT_KEYS if st[k] < prev[k]}
            if drops:
                out.append({"level": L, "allocation": _nz(a), "drops": drops})
        prev = st
    return out


def compute(d: Dict[str, Any]) -> Tuple[Dict[str, Any], List[List[Any]]]:
    rec_name = d["notes"]["boosters"]["slot2_recommendation"]["name"]
    base = _sim(d)                                   # 出厂卡 + JSON 中的推荐加点
    auto = _sim(d, allocation="auto")                # pesdb 贪心（复现自动提升）
    opt = _sim(d, allocation="optimal", per_level=False)
    full = _sim(_crafted_variant(d, rec_name), per_level=False)
    card = base.card
    p = card.manager.proficiency
    h, w, pos = card.height_cm, card.wfa_idx, card.registered_position

    st = {
        "L1": base.stages["L1"], "L1+FIXED": base.stages["L1+FIXED"], "MAX": base.stages["MAX"],
        "MAX+FIXED": base.stages["MAX+FIXED"], "MAX+FIXED+CRAFTED": full.stages["MAX+BOOSTERS"],
        "MAX+FIXED+MANAGER": base.stages["MAX+MANAGER"], "MAX+FIXED+CRAFTED+MANAGER": full.stages["MAX+MANAGER"],
    }
    overall = {k: R.overall_all_positions(v, h, w) for k, v in st.items()}
    raw = {k: round(R.raw_overall(v, h, w, pos), 3) for k, v in st.items()}

    cats = R.categories_for_position(pos)
    top = S.top_allocations(card.stats, h, w, pos, card.budget, categories=cats, k=5)
    fixed_b = card.booster_bonus(("fixed",))
    full_b = full.card.booster_bonus(("fixed", "craftable"))
    prof = card.proficiency
    pen = {
        "MAX+FIXED": {q: R.overall_in_position(st["MAX"], h, w, q, prof[q], booster_bonus=fixed_b) for q in R.POSITIONS},
        "MAX+FIXED+CRAFTED": {q: R.overall_in_position(st["MAX"], h, w, q, prof[q], booster_bonus=full_b) for q in R.POSITIONS},
        "MAX+FIXED+CRAFTED+MANAGER": {q: R.overall_in_position(st["MAX"], h, w, q, prof[q], manager_p=p, booster_bonus=full_b)
                                      for q in R.POSITIONS},
    }
    aim = {}
    for sname in card.skills:
        if not R.SKILLS[sname].aim_bonus:
            continue
        rows = {"MAX（满级中性）": R.aim_effective(st["MAX"], sname),
                "MAX+FIXED（无主教练，注册位置）": R.aim_effective(R.match_stats(st["MAX"], booster_bonus=fixed_b), sname)}
        for q, m in full.match.items():
            rows[f"比赛中 {q}（完整培养 + 主教练 {p}）"] = R.aim_effective(m, sname)
        aim[sname] = {"condition": R.SKILLS[sname].aim_condition,
                      "values": {k: {R.STAT_BY_KEY[s].en: {"stat": a, "effective": e} for s, (a, e) in v.items()}
                                 for k, v in rows.items()}}

    alts = {}
    for alt in [a["name"] for a in d["notes"]["boosters"]["slot2_recommendation"].get("alternatives", [])]:
        r = _sim(_crafted_variant(d, alt), per_level=False)
        alts[alt] = {"MAX+FIXED+CRAFTED": r.ratings["MAX+BOOSTERS"][pos],
                     "raw": round(R.raw_overall(r.stages["MAX+BOOSTERS"], h, w, pos), 3),
                     "MAX+FIXED+CRAFTED+MANAGER": r.ratings["MAX+MANAGER"][pos]}

    issues = {lvl: [f"{i.code}: {i.message}" for i in base.issues + full.issues if i.level == lvl]
              for lvl in ("ERROR", "WARN", "INFO")}
    issues = {k: sorted(set(v)) for k, v in issues.items()}
    fixed = base.card.booster_bonus(("fixed",))
    crafted = {k: R.CRAFTED_BOOSTER_VALUE for k in R.REGULAR_BOOSTERS[rec_name].stats}
    add = {k: st["MAX"][k] - st["L1"][k] for k in R.STAT_KEYS}

    derived: Dict[str, Any] = {
        "_comment": "由 tools/data/build_r9_2002.py 生成，不要手改。计算全部来自 progression_sim.py / efootball_rules.py；" + TAG + "。",
        "manager_assumption": {"proficiency": p, "model": card.manager.model, "boosters": card.manager.boosters,
                               "note": "典型主教练假设（本组推断 T4）：熟练度 89、无主教练增能；不属于卡数据"},
        "validation": {"errors": issues["ERROR"], "warnings": issues["WARN"], "infos": issues["INFO"]},
        "allocation": {
            "budget": card.budget, "recommended": _nz(base.alloc), "recommended_cost": R.allocation_cost(base.alloc),
            "auto_pesdb_greedy": _nz(auto.alloc), "auto_exact_optimal": _nz(opt.alloc),
            "all_identical": _nz(base.alloc) == _nz(auto.alloc) == _nz(opt.alloc),
            "top_plans": [{"rating_total_" + pos: t, "overall_" + pos: t // 1000, "cost": c, "allocation": _nz(a)} for t, c, a in top],
            "margin_first_second_per_1000": top[0][0] - top[1][0],
            "note": "top_plans 是精确搜索的前 5 名（总评分子，÷1000 后取整为总评）。第一名与第二名的差很小时，"
                    "客户端权重、取整或并列规则的微小差别都可能改变自动提升的结果（社区逆向，待客户端核实）",
        },
        "auto_plan_under_manager": {
            "_comment": "如果游戏内自动提升按'比赛计划下（含主教练熟练度）'的总评最大化（v6.0.0 起培养界面显示比赛计划下的能力，T1；"
                        "是否如此未找到），下列熟练度会得到与推荐方案不同的加点，并列出该方案不含主教练时的卡面总评（满级 / 满级 + 固定增能）。"
                        "p=" + str(p) + " 时不变。efx=社区逆向公式，pesdb=门槛表；本组复算（T4）",
            **_auto_under_manager(card, base.alloc)},
        "level_up_stat_drops": {
            "_comment": "每次升级重新分配全部点数（v4.0.0 官方，T1）时，按 pesdb 贪心复现，升级后会下降的能力（下降前→后）。属正常现象，总评不降",
            "levels": _level_up_drops(card)},
        "training_steps": [{"step": s.index, "category": s.category, "nth": s.nth, "cost": s.cost, "points_used": s.used,
                            "overall_" + pos: s.ovr} for s in base.steps],
        "auto_plan_by_level": [{"level": L, "points": b, "allocation": _nz(a), "overall_" + pos: o}
                               for L, b, a, o in auto.per_level],
        "stages": STAGES,
        "stats": {R.STAT_BY_KEY[k].en: {
            "L1": st["L1"][k], "training_add": add[k], "MAX": st["MAX"][k], "fixed_booster": fixed.get(k, 0),
            "MAX+FIXED": st["MAX+FIXED"][k], "crafted_booster": crafted.get(k, 0),
            "MAX+FIXED+CRAFTED": st["MAX+FIXED+CRAFTED"][k], "MAX+FIXED+MANAGER": st["MAX+FIXED+MANAGER"][k],
            "MAX+FIXED+CRAFTED+MANAGER": st["MAX+FIXED+CRAFTED+MANAGER"][k]} for k in R.STAT_KEYS},
        "overall": overall,
        "overall_note": "overall 是 §7 公式值，不含位置适性惩罚（与 pesdb 口径相同）；按位置的比赛角色评价估算见 overall_position_penalised",
        "overall_position_penalised": {
            "_comment": "本组估算（T4）：先按 04 §3.6 的位置适性倍率（和主教练倍率）折算卡面值，再加增能，再代入 §7 公式。"
                        "v4.2.0 官方说比赛中角色适配值计入增能、位置适性和主教练（T1），算法未公布。待客户端核实",
            **pen},
        "overall_raw_" + pos: raw,
        "overall_alternative_crafted_" + pos: alts,
        "match_stats_full_build": {
            "_comment": "比赛中实际能力：满体力、状态正常、不含技能情境加成；位置适性倍率 + 主教练熟练度 + 两个增能槽（04 §6.6 顺序）",
            **{q: {R.STAT_BY_KEY[k].en: v for k, v in m.items() if k not in R.GK_STAT_KEYS} for q, m in full.match.items()}},
        "aim_effective": {
            "_comment": "'按能力 +N 瞄准/计算'型技能的有效值：不改能力值、不受 99 封顶、叠在增能之后（04 §4.2 核查更正 C21；"
                        "Through Passing 有原文 T3，其余同类技能为推断 T4）。待客户端核实",
            **aim},
        "count_ge_95": {s: sorted(R.STAT_BY_KEY[k].en for k in R.STAT_KEYS if st[s][k] >= 95)
                        for s in ("MAX", "MAX+FIXED", "MAX+FIXED+CRAFTED")},
    }

    # ---- 平表 CSV ----
    n = d["notes"]
    ident = n["identity"]
    hdr = ["section", "key", "name_en", "name_en_us", "name_zh", "category", "level1", "training_add", "max_neutral",
           "fixed_booster", "max_fixed", "crafted_booster_rec", "max_fixed_crafted", f"manager{p}_fixed",
           f"manager{p}_full", "match_CF", "match_SS", "match_LWF", "penalised_max_fixed_T4", f"penalised_manager{p}_full_T4",
           "value", "special", "trainable_as_additional", "client_check", "source"]
    rows: List[List[Any]] = [hdr]

    def row(**kw):
        rows.append([kw.get(c, "") for c in hdr])

    for key, val, src in (
            ("display_name", d["name"], "—"), ("full_name", ident["full_name"], "03 抬头"),
            ("nationality", ident["nationality"], "任务书 §4；03 抬头（巴西队）"), ("date_of_birth", ident["date_of_birth"], "02 H8（Inter 官网）"),
            ("age", d["age"], "03 抬头；04 §3.1"), ("shirt_number", ident["shirt_number"], "03 抬头"),
            ("card_moment", ident["card_moment"], "03 抬头"), ("height_cm", d["height_cm"], "02 工作基线"),
            ("weight_kg", d["weight_kg"], "02 §3（可接受 80–84）"), ("card_id", d["card_id"], "04 §8.4（T4）"),
            ("card_type", d["card_type"], "只用于套用 Epic 培养规格")):
        row(section="identity", key=key, value=val, source=src,
            client_check="必须与主项目核对" if key == "card_id" else ("待客户端核实" if key in ("card_type",) else ""))
    of = n["other_fields"]
    for key in ("stronger_foot", "weak_foot_usage", "weak_foot_accuracy", "form", "injury_resistance", "live_update_rating"):
        row(section="profile", key=key, value=d[key], client_check="编码待客户端核实", source=of[key]["evidence"])
    row(section="profile", key="registered_position", value=pos, client_check="位置序号与适性编码待客户端核实", source="03 §4")
    row(section="profile", key="max_level", value=d["max_level"], client_check="待客户端核实", source="04 §5.2")
    row(section="profile", key="progression_points", value=card.budget, client_check="待客户端核实",
        source="04 §5.2：2×(最高等级−1)，社区逆向")

    for k in R.STAT_KEYS:
        s = R.STAT_BY_KEY[k]
        row(section="stat", key=k, name_en=s.en, name_en_us=s.en_us, name_zh=s.zh, category="/".join(s.categories),
            level1=st["L1"][k], training_add=add[k], max_neutral=st["MAX"][k], fixed_booster=fixed.get(k, 0),
            max_fixed=st["MAX+FIXED"][k], crafted_booster_rec=crafted.get(k, 0), max_fixed_crafted=st["MAX+FIXED+CRAFTED"][k],
            **{f"manager{p}_fixed": st["MAX+FIXED+MANAGER"][k], f"manager{p}_full": st["MAX+FIXED+CRAFTED+MANAGER"][k]},
            match_CF=full.match.get("CF", {}).get(k, ""), match_SS=full.match.get("SS", {}).get(k, ""),
            match_LWF=full.match.get("LWF", {}).get(k, ""), client_check="字段名/编码待客户端核实",
            source="DESIGN.md §2")
    for q in R.POSITIONS:
        row(section="overall", key=q, value=card.proficiency[q], level1=overall["L1"][q], max_neutral=overall["MAX"][q],
            max_fixed=overall["MAX+FIXED"][q], max_fixed_crafted=overall["MAX+FIXED+CRAFTED"][q],
            **{f"manager{p}_fixed": overall["MAX+FIXED+MANAGER"][q], f"manager{p}_full": overall["MAX+FIXED+CRAFTED+MANAGER"][q],
               "penalised_max_fixed_T4": pen["MAX+FIXED"][q],
               f"penalised_manager{p}_full_T4": pen["MAX+FIXED+CRAFTED+MANAGER"][q]},
            client_check="总评公式待客户端核实；penalised 列为本组估算（T4）",
            source="04 §7（社区逆向公式）；penalised = §3.6 位置倍率折算后再代入公式（T4）")
    for c in R.CATEGORY_KEYS:
        if c in R.categories_for_position(pos):
            tc = R.TRAINING_CATEGORIES[c]
            row(section="allocation", key=c, name_en=tc.en, name_zh=tc.zh, value=base.alloc.get(c, 0),
                training_add=base.alloc.get(c, 0), category=",".join(tc.stats), client_check="待客户端核实",
                source=f"推荐 = 自动提升的复现（pesdb 贪心 = 精确最优；第二名只差 {top[0][0] - top[1][0]}/1000）")
    row(section="allocation", key="cost", value=f"{R.allocation_cost(base.alloc)}/{card.budget}", source="04 §5.4")
    for lbl, table, nm in (("attacking", R.ATTACKING_STYLES, card.attacking_style),
                           ("defensive", R.DEFENSIVE_STYLES, card.defensive_style)):
        stl = table[nm]
        row(section="playing_style", key=lbl, name_en=nm, name_en_us=stl.en_us, name_zh=stl.zh,
            value="active: " + " ".join(R.style_active_positions(stl, card.usable_positions())),
            client_check="ID 待客户端核实", source="04 §3.7")
    for nm in card.ai_styles:
        row(section="ai_playing_style", key=nm, name_en=nm, name_zh=R.AI_STYLES[nm].zh, value="no position check",
            client_check="ID 待客户端核实", source="04 §3.7")
    ev = {s["name"]: s["evidence"] for s in n["skills"]["card_skills"]}
    for nm in card.skills:
        sk = R.SKILLS[nm]
        aim_txt = ("瞄准/计算 " + "、".join(f"{R.STAT_BY_KEY[k].en} +{v}" for k, v in sk.aim_bonus)
                   + f"（{sk.aim_condition}；不受 99 封顶）") if sk.aim_bonus else ""
        row(section="card_skill", key=nm, name_en=nm, name_en_us=sk.en_us, name_zh=sk.zh, special="yes" if sk.special else "no",
            trainable_as_additional="no" if sk.special else "yes", value=aim_txt,
            client_check="ID/特殊标志待客户端核实（特殊身份只有 T2/T3 依据）" if sk.special else "ID 待客户端核实",
            source=ev.get(nm, ""))
    for s in n["skills"]["additional_skills_recommended"]:
        sk = R.SKILLS[R.resolve_skill(s["name"])]
        row(section="additional_skill_recommended", key=sk.name, name_en=sk.name, name_zh=sk.zh, special="no",
            trainable_as_additional="yes", value="optional（出厂为空，玩家训练）", source=s["evidence"])
    b1 = n["boosters"]["slot1_fixed"]
    row(section="booster", key="slot1", name_en=b1["name"], name_zh=b1["name_zh"], value=f"fixed +{b1['value']} active",
        category=",".join(b1["stats"]), client_check="增能 ID 待客户端核实", source=b1["exists_in"])
    row(section="booster", key="slot2_shipped", name_en="Empty Booster Slot", value="craftable +1（出厂为空）",
        client_check="空槽表示方法待客户端核实", source="04 §6.4")
    b2 = n["boosters"]["slot2_recommendation"]
    row(section="booster", key="slot2_recommended", name_en=b2["name"], value=f"crafted +{b2['value']}（玩家制作）",
        category=",".join(b2["stats"]), source=b2["exists_in"])
    row(section="manager_assumption", key="team_playstyle_proficiency", value=p,
        source="典型值假设（T4），只用于模拟（JSON simulation.manager），不属于卡数据；倍率公式 04 §6.5 社区逆向")
    return derived, rows


def render_json(d: Dict[str, Any], derived: Dict[str, Any]) -> str:
    out = copy.deepcopy(d)
    out["notes"]["derived"] = derived
    return json.dumps(out, ensure_ascii=False, indent=1) + "\n"


def render_csv(rows: List[List[Any]]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return "﻿" + buf.getvalue()   # BOM：方便 Excel 正确识别 UTF-8 中文


def render_report(d: Dict[str, Any]) -> str:
    rec = d["notes"]["boosters"]["slot2_recommendation"]["name"]
    rel_card = os.path.relpath(CARD, ROOT)
    parts = ["R9 2002 球员卡模拟报告（由 tools/data/build_r9_2002.py 生成；每一节都是 progression_sim.py 命令行的原样输出）",
             "规则与数值：" + TAG + "。总评 = 社区逆向公式（04 §7）。", ""]
    with tempfile.TemporaryDirectory() as td:
        variant = os.path.join(td, "r9_2002_player.crafted.json")
        with open(variant, "w", encoding="utf-8") as fh:
            json.dump(_crafted_variant(d, rec), fh, ensure_ascii=False)
        runs = [
            ("1. 出厂卡 + 推荐加点（JSON 原样）", [rel_card], rel_card),
            ("2. 自动提升（pesdb 贪心复现，含每级分配）", [rel_card, "--allocation", "auto", "--no-steps"], rel_card),
            ("3. 精确最优加点（核对与 1、2 相同）", [rel_card, "--allocation", "optimal", "--no-steps", "--no-levels"], rel_card),
            (f"4. 变体：slot 2 制作推荐增能 {rec} +1（其余与 JSON 相同）", [variant, "--no-steps", "--no-levels"],
             f"<临时文件：{rel_card} 的 slot 2 改为 {rec} +1>"),
        ]
        for title, args, shown in runs:
            p = subprocess.run([sys.executable, SIM, *args], cwd=ROOT, capture_output=True, text=True)
            shown_args = " ".join([shown] + args[1:])
            parts += ["#" * 100, f"# {title}", f"# $ python3 {SIM} {shown_args}", f"# 退出码 {p.returncode}",
                      "#" * 100, p.stdout.rstrip(), (p.stderr.rstrip() if p.stderr else ""), ""]
    return "\n".join(parts).rstrip() + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="只检查产物是否最新")
    a = ap.parse_args(argv)
    d = _load()
    src = copy.deepcopy(d)
    src["notes"].pop("derived", None)
    derived, rows = compute(src)
    outputs = {CARD: render_json(src, derived), CSV_OUT: render_csv(rows), REPORT: render_report(src)}
    if derived["validation"]["errors"]:
        print("校验有 ERROR：", *derived["validation"]["errors"], sep="\n  ")
    stale = []
    for path, text in outputs.items():
        cur = open(path, encoding="utf-8").read() if os.path.exists(path) else None
        if cur != text:
            stale.append(os.path.relpath(path, ROOT))
            if not a.check:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
    if a.check:
        print("过期：" + ", ".join(stale) if stale else "全部最新")
        return 1 if stale else 0
    print("已写入：" + (", ".join(stale) if stale else "无变化"))
    return 1 if derived["validation"]["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
