#!/usr/bin/env python3
"""eFootball 球员卡培养模拟器 + 校验器（命令行）。

用法：
    python3 tools/data/progression_sim.py player.json [--allocation auto|optimal|card]
        [--objective CF=1,SS=0.25] [--targets targets.json] [--manager 89] [--manager-model efx|pesdb]
        [--manager-booster physical_contact=1 ...] [--json out.json] [--no-steps] [--no-levels]

输出：1 级卡面 → 每一步加点 → 满级中性卡面 → 加固定增能 → 加可训练增能 → 加主教练影响，
每个阶段 13 个位置的总评；比赛中的实际能力（位置适性/主教练/增能/技能情境加成）；以及全部校验结果。
存在 ERROR 时退出码为 1。

规则全部来自 tools/data/efootball_rules.py（其出处见 04 号档案）。凡是社区逆向（T3）的规则和必须在客户端确认的值，
输出里都会标注"社区逆向"/"待客户端核实"。

输入 JSON（字段名英文，取值可写英文、美式英文或简中）：
{
  "name": "...", "card_id": "88044146203441", "card_type": "Epic",     # card_id/card_type 只作标识
  "registered_position": "CF",
  "position_proficiency": {"CF": "high", "SS": "mid", "LWF": "mid"},  # 其余位置视为 low
  "height_cm": 183, "weight_kg": 80, "age": 25,
  "stronger_foot": "Right", "weak_foot_usage": "Occasionally", "weak_foot_accuracy": "High",
  "form": "Standard", "injury_resistance": "Low",
  "max_level": 32,
  "stats": {26 项 1 级卡面值，键可用 key/英文/简中},
  "playing_style": {"attacking": "Goal Poacher", "defensive": "Basic"},
  "ai_playing_styles": ["..."],
  "skills": ["卡面技能（含特殊技能），≤10"],
  "additional_skills": ["附加技能（只能是可训练技能），≤5"],
  "boosters": [
    {"slot": 1, "kind": "fixed", "name": "Striker's Instinct", "value": 4, "active": true},
    {"slot": 2, "kind": "craftable", "name": "Agility", "value": 1, "active": true}   # name=null 表示空槽
  ],
  "live_update_rating": "B",           # 可选；Epic/Big Time/Legendary 固定 B（T3）
  "allocation": "auto" | "optimal" | {"shooting": 10, "dexterity": 12, ...},
  "objective": {"positions": {"CF": 1.0}, "targets": {"finishing": 95}, "limits": {"heading": 74}},  # optimal 用：
                                       # targets=软上限（超出不计分），limits=硬上限（培养后不得超过）
  "manager": {"proficiency": 89, "model": "efx", "boosters": {"physical_contact": 1}}
}
allocation / objective / manager 是模拟用的输入，不属于卡数据。可以写在顶层（旧格式），也可以放进
"simulation": {"allocation": ..., "objective": ..., "manager": ...}（推荐：避免接入时被误写进客户端卡数据）；
两处同时给出同一个键时报 ERROR。
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import unicodedata
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import efootball_rules as R  # noqa: E402

KNOWN_KEYS = {
    "name", "card_id", "card_type", "registered_position", "position_proficiency", "height_cm", "weight_kg", "age",
    "stronger_foot", "weak_foot_usage", "weak_foot_accuracy", "form", "injury_resistance", "max_level", "stats",
    "playing_style", "ai_playing_styles", "skills", "additional_skills", "boosters", "allocation", "objective",
    "manager", "notes", "_comment", "schema", "simulation", "live_update_rating",
}
SIM_KEYS = ("allocation", "objective", "manager")      # 只用于模拟、不属于卡数据的键


# ---------------------------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------------------------

@dataclass
class Issue:
    level: str          # ERROR / WARN / INFO
    code: str
    message: str
    rule: Optional[str] = None

    def __str__(self) -> str:
        tag = f" {R.RULES[self.rule].tag()}" if self.rule in R.RULES else ""
        return f"[{self.level}] {self.code}: {self.message}{tag}"


@dataclass
class BoosterSlot:
    slot: int
    kind: str                      # fixed / craftable
    name: Optional[str]            # 规范名；None = 空槽
    btype: Optional[str]           # regular / special / single
    value: int
    stats: Tuple[str, ...]
    active: bool

    def bonus(self) -> Dict[str, int]:
        if not self.active or self.name is None:
            return {}
        return {k: self.value for k in self.stats}

    def display_name(self) -> str:
        if self.btype == "single" and self.name in R.STAT_BY_KEY:
            return R.STAT_BY_KEY[self.name].en
        return self.name or ""

    def label(self) -> str:
        if self.name is None:
            return f"slot {self.slot}: 空增能槽（可制作 +1）"
        state = "激活" if self.active else "未激活（激活条件未知）"
        kind = "固定" if self.kind == "fixed" else "可训练/制作"
        return f"slot {self.slot}: {self.display_name()} +{self.value}（{kind}，{state}；{', '.join(self.stats) if len(self.stats) < 26 else '26 项全部'}）"


@dataclass
class ManagerSpec:
    proficiency: Optional[int] = None
    model: str = "efx"
    boosters: Dict[str, int] = field(default_factory=dict)


@dataclass
class PlayerCard:
    name: str
    registered_position: str
    proficiency: Dict[str, str]
    height_cm: int
    stats: Dict[str, int]
    max_level: int
    weak_foot_accuracy: str
    weak_foot_usage: Optional[str] = None
    stronger_foot: Optional[str] = None
    form: Optional[str] = None
    injury_resistance: Optional[str] = None
    weight_kg: Optional[int] = None
    age: Optional[int] = None
    live_update_rating: Optional[str] = None
    card_id: Optional[str] = None
    card_type: Optional[str] = None
    attacking_style: str = "Basic"
    defensive_style: str = "Basic"
    ai_styles: List[str] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    additional_skills: List[str] = field(default_factory=list)
    boosters: List[BoosterSlot] = field(default_factory=list)
    allocation: Any = "auto"
    objective_positions: Dict[str, float] = field(default_factory=dict)
    targets: Dict[str, int] = field(default_factory=dict)
    limits: Dict[str, int] = field(default_factory=dict)
    manager: ManagerSpec = field(default_factory=ManagerSpec)

    @property
    def wfa_idx(self) -> int:
        return R.wfa_index(self.weak_foot_accuracy)

    @property
    def budget(self) -> int:
        return R.progression_points(self.max_level)

    def usable_positions(self) -> Tuple[str, ...]:
        return tuple(p for p in R.POSITIONS if self.proficiency.get(p, "low") in ("high", "mid"))

    def booster_bonus(self, kinds: Sequence[str] = ("fixed", "craftable")) -> Dict[str, int]:
        return R.sum_bonus(*(b.bonus() for b in self.boosters if b.kind in kinds))


class CardError(Exception):
    def __init__(self, issues: List[Issue]):
        super().__init__("; ".join(str(i) for i in issues))
        self.issues = issues


# ---------------------------------------------------------------------------------------------
# 读取与解析
# ---------------------------------------------------------------------------------------------

def _resolve(fn, value, issues: List[Issue], code: str, rule: Optional[str] = None):
    try:
        return fn(value)
    except (KeyError, ValueError) as e:
        issues.append(Issue("ERROR", code, str(e).strip("'\""), rule))
        return None


def parse_card(d: Mapping[str, Any]) -> Tuple[PlayerCard, List[Issue]]:
    """把输入 JSON 解析成 PlayerCard。名称解析失败记为 ERROR；缺少必需字段时抛 CardError。"""
    issues: List[Issue] = []
    unknown = sorted(set(d) - KNOWN_KEYS)
    if unknown:
        issues.append(Issue("WARN", "UNKNOWN_FIELD", f"忽略未知字段：{', '.join(unknown)}"
                            "（本工具不处理卡池、经济、奖励或 T0 资格，任务书 §14）"))
    for req in ("registered_position", "height_cm", "stats", "max_level", "weak_foot_accuracy"):
        if req not in d:
            issues.append(Issue("ERROR", "MISSING_FIELD", f"缺少必需字段 {req}"))
    if any(i.level == "ERROR" for i in issues):
        raise CardError(issues)

    pos = str(d["registered_position"]).upper()
    if pos not in R.POS_INDEX:
        raise CardError(issues + [Issue("ERROR", "POSITION", f"未知注册位置 {pos!r}")])

    stats: Dict[str, int] = {}
    for k, v in dict(d["stats"]).items():
        key = _resolve(R.resolve_stat, k, issues, "STAT_NAME", "STAT_IDS")
        if key is None:
            continue
        if key in stats:
            issues.append(Issue("ERROR", "STAT_DUP", f"能力 {key} 重复给出"))
        if isinstance(v, bool) or not isinstance(v, int):
            issues.append(Issue("ERROR", "STAT_TYPE", f"{key} 必须是整数，收到 {v!r}", "STAT_RANGE"))
            continue
        stats[key] = v
    missing = [k for k in R.STAT_KEYS if k not in stats]
    if missing:
        issues.append(Issue("ERROR", "STAT_MISSING", f"缺少能力：{', '.join(missing)}（必须给全 26 项）", "STAT_IDS"))
        raise CardError(issues)

    prof: Dict[str, str] = {p: "low" for p in R.POSITIONS}
    for p, lvl in dict(d.get("position_proficiency", {})).items():
        P = str(p).upper()
        if P not in R.POS_INDEX:
            issues.append(Issue("ERROR", "POSITION", f"未知位置 {p!r}"))
            continue
        L = R.PROFICIENCY_ALIASES.get(str(lvl).strip().lower())
        if L is None:
            issues.append(Issue("ERROR", "PROFICIENCY", f"{P} 的适性 {lvl!r} 不是 high/mid/low", "POSITION_LEVELS"))
            continue
        prof[P] = L
    if "position_proficiency" not in d or pos not in {str(p).upper() for p in d.get("position_proficiency", {})}:
        prof[pos] = "high"

    def enum(field_, table, rule, required=False):
        if field_ not in d or d[field_] is None:
            if required:
                issues.append(Issue("ERROR", "MISSING_FIELD", f"缺少 {field_}", rule))
            return None
        return _resolve(lambda v: R.resolve_enum(table, v), d[field_], issues, field_.upper(), rule)

    wfa = enum("weak_foot_accuracy", R.WEAK_FOOT_ACCURACY, "WEAK_FOOT", True) or "Low"
    ps = dict(d.get("playing_style", {}) or {})
    att = _resolve(lambda v: R.resolve_style(R.ATTACKING_STYLES, v), ps.get("attacking", "Basic"), issues,
                   "ATTACKING_STYLE", "STYLE_SPLIT") or "Basic"
    dfn = _resolve(lambda v: R.resolve_style(R.DEFENSIVE_STYLES, v), ps.get("defensive", "Basic"), issues,
                   "DEFENSIVE_STYLE", "STYLE_SPLIT") or "Basic"
    ai = [s for s in (_resolve(lambda v: R.resolve_style(R.AI_STYLES, v), x, issues, "AI_STYLE", "AI_STYLE_MAX")
                      for x in d.get("ai_playing_styles", []) or []) if s]
    skills = [s for s in (_resolve(R.resolve_skill, x, issues, "SKILL_NAME", "SKILL_LIST")
                          for x in d.get("skills", []) or []) if s]
    add_sk = [s for s in (_resolve(R.resolve_skill, x, issues, "SKILL_NAME", "SKILL_LIST")
                          for x in d.get("additional_skills", []) or []) if s]

    boosters: List[BoosterSlot] = []
    for i, b in enumerate(d.get("boosters", []) or []):
        b = dict(b)
        slot = int(b.get("slot", i + 1))
        kind = str(b.get("kind", "fixed")).lower()
        if kind not in ("fixed", "craftable"):
            issues.append(Issue("ERROR", "BOOSTER_KIND", f"增能 slot {slot} 的 kind {kind!r} 必须是 fixed/craftable"))
            kind = "fixed"
        raw_name = b.get("name")
        if raw_name is None:
            boosters.append(BoosterSlot(slot, kind, None, None, 0, (), False))
            continue
        nm, lbl_val = R.parse_booster_label(raw_name)
        value = b.get("value", lbl_val)
        if not isinstance(value, int) or isinstance(value, bool):
            issues.append(Issue("ERROR", "BOOSTER_VALUE", f"增能 slot {slot} 缺少整数 value"))
            continue
        res = _resolve(R.resolve_booster, nm, issues, "BOOSTER_NAME", "BOOSTER_LIST")
        if res is None:
            continue
        cname, btype = res
        if b.get("type") and b["type"] != btype:
            issues.append(Issue("ERROR", "BOOSTER_TYPE", f"增能 {cname} 是 {btype} 型，输入写的是 {b['type']}"))
        try:
            st = R.booster_stats(cname, btype, b.get("stats"))
        except KeyError as e:
            issues.append(Issue("ERROR", "BOOSTER_STATS", str(e).strip("'\""), "BOOSTER_SPECIAL"))
            continue
        if b.get("stats") and btype != "special":
            given = tuple(sorted(R.resolve_stat(s) for s in b["stats"]))
            if given != tuple(sorted(st)):
                issues.append(Issue("ERROR", "BOOSTER_STATS", f"增能 {cname} 的能力组合应为 {', '.join(st)}，"
                                    f"输入为 {', '.join(given)}", "BOOSTER_LIST"))
        default_active = (kind == "craftable") or slot == 1
        boosters.append(BoosterSlot(slot, kind, cname, btype, int(value), tuple(st), bool(b.get("active", default_active))))

    sim = d.get("simulation", {}) or {}
    if not isinstance(sim, dict):
        issues.append(Issue("ERROR", "SIMULATION", "simulation 必须是对象"))
        sim = {}
    unknown_sim = sorted(set(sim) - set(SIM_KEYS))
    if unknown_sim:
        issues.append(Issue("WARN", "UNKNOWN_FIELD", f"simulation 中忽略未知字段：{', '.join(unknown_sim)}"))
    for key in SIM_KEYS:
        if key in sim and key in d:
            issues.append(Issue("ERROR", "SIM_FIELD_DUP", f"{key} 同时出现在顶层和 simulation 中"))

    def sim_get(key, default):
        return sim[key] if key in sim else d.get(key, default)

    alloc = sim_get("allocation", "auto")
    if isinstance(alloc, dict):
        a2: Dict[str, int] = {}
        for k, v in alloc.items():
            ck = _resolve(R.resolve_category, k, issues, "CATEGORY_NAME", "CATEGORY_MAP")
            if ck is None:
                continue
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                issues.append(Issue("ERROR", "ALLOC_VALUE", f"类别 {ck} 的加点次数必须是非负整数"))
                continue
            a2[ck] = v
        alloc = a2
    elif alloc not in ("auto", "optimal"):
        issues.append(Issue("ERROR", "ALLOC", f"allocation 必须是 auto / optimal / 对象，收到 {alloc!r}"))
        alloc = "auto"

    obj = dict(sim_get("objective", {}) or {})
    obj_pos = {str(k).upper(): float(v) for k, v in dict(obj.get("positions", {}) or {}).items()}
    for p in obj_pos:
        if p not in R.POS_INDEX:
            issues.append(Issue("ERROR", "POSITION", f"objective 中未知位置 {p!r}"))
    obj_pos = {p: w for p, w in obj_pos.items() if p in R.POS_INDEX}
    targets, limits = {}, {}
    for src, dst in (("targets", targets), ("limits", limits)):
        for k, v in dict(obj.get(src, {}) or {}).items():
            sk = _resolve(R.resolve_stat, k, issues, "STAT_NAME", "STAT_IDS")
            if sk:
                dst[sk] = int(v)

    m = dict(sim_get("manager", {}) or {})
    mboost = {}
    for k, v in dict(m.get("boosters", {}) or {}).items():
        sk = _resolve(R.resolve_stat, k, issues, "STAT_NAME", "MANAGER_BOOSTER")
        if sk:
            mboost[sk] = int(v)
    manager = ManagerSpec(m.get("proficiency"), str(m.get("model", "efx")), mboost)

    card = PlayerCard(
        name=str(d.get("name", "")), registered_position=pos, proficiency=prof, height_cm=int(d["height_cm"]),
        stats=stats, max_level=int(d["max_level"]), weak_foot_accuracy=wfa,
        weak_foot_usage=enum("weak_foot_usage", R.WEAK_FOOT_USAGE, "WEAK_FOOT"),
        stronger_foot=enum("stronger_foot", R.STRONGER_FOOT, None), form=enum("form", R.FORM, "FORM"),
        injury_resistance=enum("injury_resistance", R.INJURY_RESISTANCE, "INJURY"),
        weight_kg=d.get("weight_kg"), age=d.get("age"),
        live_update_rating=enum("live_update_rating", R.LIVE_UPDATE_RATING, "LIVE_UPDATE_RATING"),
        card_id=None if d.get("card_id") is None else str(d.get("card_id")), card_type=d.get("card_type"),
        attacking_style=att, defensive_style=dfn, ai_styles=ai, skills=skills, additional_skills=add_sk,
        boosters=boosters, allocation=alloc, objective_positions=obj_pos, targets=targets, limits=limits, manager=manager)
    return card, issues


def load_card(path: str) -> Tuple[PlayerCard, List[Issue]]:
    with open(path, encoding="utf-8") as fh:
        return parse_card(json.load(fh))


# ---------------------------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------------------------

def validate_card(card: PlayerCard, alloc: Optional[Mapping[str, int]] = None) -> List[Issue]:
    out: List[Issue] = []
    E = lambda c, m, r=None: out.append(Issue("ERROR", c, m, r))  # noqa: E731
    W = lambda c, m, r=None: out.append(Issue("WARN", c, m, r))   # noqa: E731
    I = lambda c, m, r=None: out.append(Issue("INFO", c, m, r))   # noqa: E731

    # 能力范围
    for k in R.STAT_KEYS:
        v = card.stats[k]
        if not (R.STAT_MIN <= v <= R.STAT_MAX):
            E("STAT_RANGE", f"{R.STAT_BY_KEY[k].en} = {v} 超出卡面范围 {R.STAT_MIN}–{R.STAT_MAX}", "STAT_RANGE")
    if card.registered_position != "GK":
        odd = [k for k in R.GK_STAT_KEYS if card.stats[k] != R.STAT_MIN]
        if odd:
            W("GK_STATS", f"外场球员的 GK 能力抽查均为 40，本卡为：{', '.join(f'{k}={card.stats[k]}' for k in odd)}", "STAT_RANGE")

    # 等级 / 点数
    if card.max_level < 1:
        E("LEVEL", "max_level 必须 ≥ 1", "POINTS")
    elif not (29 <= card.max_level <= 36) and card.max_level != 1:
        W("LEVEL", f"最高等级 {card.max_level} 超出抽样所见范围（Epic/Big Time/Show Time 31–34，Highlight 29–36）", "LEVEL_CAP")
    if card.card_type in ("Epic", "Big Time", "Show Time") and not (31 <= card.max_level <= 34):
        W("LEVEL", f"{card.card_type} 卡抽样所见最高等级为 31–34，本卡为 {card.max_level}", "LEVEL_CAP")

    # 身高（总评要用）
    if not (150 <= card.height_cm <= 210):
        W("HEIGHT", f"身高 {card.height_cm} cm 看起来不合理（总评公式含身高）", "RATING_FORMULA")

    # 位置
    if card.proficiency.get(card.registered_position) != "high":
        E("POSITION", f"注册位置 {card.registered_position} 的适性必须是高（亮绿）", "POSITION_LEVELS")

    # 比赛风格
    usable = card.usable_positions()
    for label, table, name in (("进攻型", R.ATTACKING_STYLES, card.attacking_style),
                               ("防守型", R.DEFENSIVE_STYLES, card.defensive_style)):
        st = table[name]
        if name == "Basic":
            continue
        if card.registered_position not in st.positions:
            W("STYLE_POSITION", f"{label}风格 {name} 与注册位置 {card.registered_position} 不兼容"
              f"（兼容位置：{', '.join(st.positions)}），在注册位置上大部分失效", "STYLE_COMPAT")
        if not R.style_active_positions(st, usable):
            E("STYLE_INACTIVE", f"{label}风格 {name} 在球员任何可用位置上都不生效", "STYLE_COMPAT")
    if len(card.ai_styles) > R.MAX_AI_STYLES:
        E("AI_STYLE_MAX", f"AI 风格 {len(card.ai_styles)} 个，超过实测上限 {R.MAX_AI_STYLES}", "AI_STYLE_MAX")
    if len(set(card.ai_styles)) != len(card.ai_styles):
        E("AI_STYLE_DUP", "AI 风格有重复", "AI_STYLE_MAX")

    # 技能
    if len(card.skills) > R.MAX_CARD_SKILLS:
        E("SKILL_CARD_MAX", f"卡面技能 {len(card.skills)} 个，超过 {R.MAX_CARD_SKILLS}", "SKILL_CARD_MAX")
    if len(card.additional_skills) > R.MAX_ADDITIONAL_SKILLS:
        E("SKILL_ADDITIONAL_MAX", f"附加技能 {len(card.additional_skills)} 个，超过 {R.MAX_ADDITIONAL_SKILLS}", "SKILL_ADDITIONAL_MAX")
    if card.card_type in ("Epic", "Big Time", "Show Time") and len(card.skills) < R.MAX_CARD_SKILLS:
        I("SKILL_CARD_COUNT", f"卡面技能 {len(card.skills)} 个；抽样所见 {card.card_type} 卡都是 {R.MAX_CARD_SKILLS} 个，"
          "少于 10 个时客户端的编码与是否接受未知", "SKILL_CARD_COUNT_TYPICAL")
    allsk = card.skills + card.additional_skills
    dups = sorted({s for s in allsk if allsk.count(s) > 1})
    if dups:
        E("SKILL_DUP", f"技能重复：{', '.join(dups)}", "SKILL_NO_DUPLICATE")
    for s in card.additional_skills:
        if R.SKILLS[s].special:
            E("SKILL_SPECIAL_ADDITIONAL", f"{s} 是特殊技能，不能作为附加技能（只能写在卡面）", "SKILL_SPECIAL")
    for s in allsk:
        sk = R.SKILLS[s]
        if sk.note and ("推断" in sk.note or "T3" in sk.note):
            I("SKILL_NOTE", f"{s}：{sk.note}", "SKILL_SPECIAL")
        if sk.group == "goalkeeping" and card.registered_position != "GK":
            W("SKILL_GK", f"外场球员带门将技能 {s}", None)

    # Live Update Rating
    if card.live_update_rating and card.card_type in R.FIXED_LIVE_UPDATE_CARD_TYPES and card.live_update_rating != "B":
        I("LIVE_UPDATE_RATING", f"{card.card_type} 卡抽样所见固定为 B，本卡为 {card.live_update_rating}", "LIVE_UPDATE_RATING")

    # 增能
    slots = [b.slot for b in card.boosters]
    if len(card.boosters) > R.MAX_BOOSTER_SLOTS:
        E("BOOSTER_SLOTS", f"增能槽 {len(card.boosters)} 个，超过 {R.MAX_BOOSTER_SLOTS}", "BOOSTER_SLOTS")
    if len(set(slots)) != len(slots):
        E("BOOSTER_SLOT_DUP", "增能槽编号重复", "BOOSTER_SLOTS")
    for b in card.boosters:
        if b.name is None:
            if b.kind != "craftable":
                E("BOOSTER_EMPTY", f"slot {b.slot} 为空但 kind 不是 craftable", "BOOSTER_CRAFTED")
            continue
        if b.kind == "craftable":
            if b.btype != "regular":
                E("BOOSTER_CRAFTED", f"slot {b.slot}：可制作的只有常规增能，{b.name} 不是", "BOOSTER_CRAFTED")
            if b.value != R.CRAFTED_BOOSTER_VALUE:
                E("BOOSTER_CRAFTED", f"slot {b.slot}：制作增能每项 +{R.CRAFTED_BOOSTER_VALUE}，输入为 +{b.value}", "BOOSTER_CRAFTED")
        else:
            if b.btype == "regular" and not (R.FIXED_REGULAR_RANGE[0] <= b.value <= R.FIXED_REGULAR_RANGE[1]):
                E("BOOSTER_VALUE", f"slot {b.slot}：4 项型固定增能抽样所见 +{R.FIXED_REGULAR_RANGE[0]}–"
                  f"+{R.FIXED_REGULAR_RANGE[1]}，输入为 +{b.value}", "BOOSTER_FIXED_RANGE")
            elif b.btype == "single" and b.value not in R.FIXED_SINGLE_VALUES:
                E("BOOSTER_VALUE", f"slot {b.slot}：单项型固定增能抽样所见 {R.FIXED_SINGLE_VALUES}，输入为 +{b.value}",
                  "BOOSTER_FIXED_RANGE")
            elif b.btype == "special":
                ok = R.FIXED_SPECIAL_VALUES.get(b.name)
                if ok is None:
                    W("BOOSTER_SPECIAL", f"slot {b.slot}：专属增能 {b.name} 的组合与数值未查证", "BOOSTER_SPECIAL")
                elif b.value not in ok:
                    E("BOOSTER_VALUE", f"slot {b.slot}：{b.name} 抽样所见 +{ok}，输入为 +{b.value}", "BOOSTER_FIXED_RANGE")
            if b.slot == 2 and b.active:
                I("BOOSTER_SLOT2", f"slot 2 固定增能 {b.display_name()} 设为激活；官方卡的增能 2 常'默认未激活'，条件未知",
                  "BOOSTER_SLOT2")
    if card.registered_position != "GK":
        for b in card.boosters:
            if b.name and b.active and all(k in R.GK_STAT_KEYS for k in b.stats):
                W("BOOSTER_GK", f"外场球员的增能 {b.display_name()} 只作用于 GK 能力", "BOOSTER_LIST")

    # 加点
    if alloc is not None:
        used = R.allocation_cost(alloc)
        if used > card.budget:
            E("ALLOC_BUDGET", f"加点花费 {used} 超过可用能力点 {card.budget}", "POINTS")
        allowed = set(R.categories_for_position(card.registered_position))
        for c, n in alloc.items():
            if n and c not in allowed:
                E("ALLOC_GK", f"外场球员不能给 {c} 加点（pesdb 只对门将启用 GK 1–3；客户端是否允许未知）", "GK_CATEGORIES")
            if n:
                need = max(R.STAT_MAX - card.stats[s] for s in R.TRAINING_CATEGORIES[c].stats)
                if n > need:
                    W("ALLOC_WASTE", f"{c} 加了 {n} 次，但该类能力最多只需 {need} 次就全部到 99", "TRAIN_CAP_99")

    # 主教练
    m = card.manager
    if m.proficiency is not None:
        if not (0 <= int(m.proficiency) <= 99):
            E("MANAGER", f"主教练熟练度 {m.proficiency} 超出 0–99", "MANAGER_FORMULA")
        if m.model == "pesdb" and not (70 <= int(m.proficiency) <= 90):
            W("MANAGER", "pesdb 门槛表只覆盖 70–90，超出部分按边界值处理", "MANAGER_THRESHOLDS")
    if m.model not in ("efx", "pesdb"):
        E("MANAGER", f"未知主教练模型 {m.model!r}（efx / pesdb）", "MANAGER_FORMULA")
    return out


# ---------------------------------------------------------------------------------------------
# 加点：pesdb 贪心（=自动分配的复现）、精确最优、步骤排序
# ---------------------------------------------------------------------------------------------

@dataclass
class Step:
    index: int
    category: str
    nth: int            # 该类别第几次
    cost: int
    used: int           # 累计花费
    ovr: int            # 加完这一步后注册位置总评（不含增能）
    raw: float


def _stats_for(base, alloc, bonus=None, manager_p=None, manager_model="pesdb"):
    s = R.apply_training(base, alloc)
    if manager_p is not None:
        s = R.apply_manager(s, manager_p, manager_model)
    if bonus:
        s = R.apply_boosters(s, bonus)
    return s


def greedy_allocation(base: Mapping[str, int], height_cm: int, wfa_idx: int, position: str, budget: int, *,
                      categories: Optional[Sequence[str]] = None, bonus: Optional[Mapping[str, int]] = None,
                      manager_p: Optional[int] = None, manager_model: str = "pesdb"
                      ) -> Tuple[Dict[str, int], List[Step]]:
    """复现 pesdb player.js 的 optimizeAllocations（RULES['AUTO_DEV_GREEDY']）。

    每一步在类别顺序中选"(raw 总评增量) ÷ 下一步花费"最大的类别；并列取顺序靠前者；增量为 0 也会被选
    （pesdb 的 bestGain 初值为 −1）。raw 用浮点 total/1000，与 JS 的双精度计算一致。
    """
    cats = list(categories or R.categories_for_position(position))
    alloc = {c: 0 for c in cats}
    steps: List[Step] = []
    used = 0
    while used < budget:
        cur = R.raw_overall(_stats_for(base, alloc, bonus, manager_p, manager_model), height_cm, wfa_idx, position)
        best, best_gain = None, -1.0
        for c in cats:
            cost = R.step_cost(alloc[c] + 1)
            if used + cost > budget:
                continue
            t = dict(alloc)
            t[c] += 1
            gain = (R.raw_overall(_stats_for(base, t, bonus, manager_p, manager_model), height_cm, wfa_idx, position) - cur) / cost
            if gain > best_gain:
                best, best_gain = c, gain
        if best is None:
            break
        cost = R.step_cost(alloc[best] + 1)
        alloc[best] += 1
        used += cost
        s = R.apply_training(base, alloc)
        steps.append(Step(len(steps) + 1, best, alloc[best], cost, used, R.overall(s, height_cm, wfa_idx, position),
                          R.raw_overall(s, height_cm, wfa_idx, position)))
    return alloc, steps


def objective_weights(positions: Mapping[str, float]) -> Dict[str, Fraction]:
    """多个位置总评权重的线性组合（系数可为小数）。"""
    w = {k: Fraction(0) for k in R.STAT_KEYS}
    for p, c in positions.items():
        cf = Fraction(str(c))
        for k in R.STAT_KEYS:
            w[k] += cf * R.RATING_WEIGHTS[k][R.POS_INDEX[p]]
    return w


def optimal_allocation(base: Mapping[str, int], budget: int, weights: Mapping[str, Any], *,
                       categories: Sequence[str], targets: Optional[Mapping[str, int]] = None,
                       limits: Optional[Mapping[str, int]] = None) -> Dict[str, int]:
    """在点数预算内精确最大化 Σ w_s × min(培养后值_s, 目标_s)（RULES 中的类别/花费/99 封顶规则）。

    targets：软上限，超过目标的部分不计分；limits：硬上限，培养后不得超过（低于 1 级值时视为"不得再加"）。
    多选背包动态规划：共享能力的类别（Aerial Strength 与 GK 1 共享 Jumping）合并为一组枚举。
    并列时依次偏好：能力总增量更大 → 花费更少 → 类别顺序靠前的方案。增能在 99 之后相加，不影响最优加点。
    """
    targets = dict(targets or {})
    limits = dict(limits or {})
    W = {k: Fraction(str(weights.get(k, 0))) if not isinstance(weights.get(k, 0), Fraction) else weights[k]
         for k in R.STAT_KEYS}

    def max_n(c):
        need = max(R.STAT_MAX - base[s] for s in R.TRAINING_CATEGORIES[c].stats)
        n = 0
        while n < need and R.cumulative_cost(n + 1) <= budget:
            n += 1
        return n

    # 按共享能力分组
    groups: List[List[str]] = []
    for c in categories:
        st = set(R.TRAINING_CATEGORIES[c].stats)
        hit = [g for g in groups if any(st & set(R.TRAINING_CATEGORIES[x].stats) for x in g)]
        merged = [c] + [x for g in hit for x in g]
        groups = [g for g in groups if g not in hit] + [sorted(merged, key=list(categories).index)]

    def value(add: Mapping[str, int], touched) -> Tuple[Fraction, int]:
        prim, sec = Fraction(0), 0
        for s in touched:
            b = base[s]
            v = min(R.STAT_MAX, b + add.get(s, 0))
            t = targets.get(s, R.BOOSTED_MAX)
            prim += W[s] * (min(v, t) - min(b, t))
            sec += v - b
        return prim, sec

    options = []
    for g in groups:
        touched = sorted({s for c in g for s in R.TRAINING_CATEGORIES[c].stats})
        opts = []
        for ns in itertools.product(*(range(max_n(c) + 1) for c in g)):
            cost = sum(R.cumulative_cost(n) for n in ns)
            if cost > budget:
                continue
            add: Dict[str, int] = {}
            for c, n in zip(g, ns):
                for s in R.TRAINING_CATEGORIES[c].stats:
                    add[s] = add.get(s, 0) + n
            if any(min(R.STAT_MAX, base[s] + add.get(s, 0)) > max(limits[s], base[s]) for s in touched if s in limits):
                continue
            p, s2 = value(add, touched)
            opts.append((cost, (p, s2, -cost), dict(zip(g, ns))))
        options.append(opts)

    zero = (Fraction(0), 0, 0)
    dp: List[Tuple[Tuple, Dict[str, int]]] = [(zero, {}) for _ in range(budget + 1)]
    for opts in options:
        new = []
        for b in range(budget + 1):
            best = None
            for cost, val, ch in opts:
                if cost > b:
                    continue
                pv, pch = dp[b - cost]
                tot = (pv[0] + val[0], pv[1] + val[1], pv[2] + val[2])
                if best is None or tot > best[0]:
                    best = (tot, {**pch, **ch})
            new.append(best)
        dp = new
    res = dp[budget][1]
    return {c: int(res.get(c, 0)) for c in categories}


def top_allocations(base: Mapping[str, int], height_cm: int, wfa_idx: int, position: str, budget: int, *,
                    categories: Optional[Sequence[str]] = None, k: int = 5) -> List[Tuple[int, int, Dict[str, int]]]:
    """精确列出注册位置总评分子（rating_total）最高的 k 个加点方案：[(rating_total, 花费, 加点), ...]。

    用于说明"自动提升"的结果有多稳：第一名与第二名差多少（总评 = 分子 // 1000）。
    共享能力的类别（Aerial Strength 与 GK 1）合并枚举；同分时花费少的排前面，再按类别顺序。
    """
    cats = list(categories or R.categories_for_position(position))
    groups: List[List[str]] = []
    for c in cats:
        st = set(R.TRAINING_CATEGORIES[c].stats)
        hit = [g for g in groups if any(st & set(R.TRAINING_CATEGORIES[x].stats) for x in g)]
        merged = [c] + [x for g in hit for x in g]
        groups = [g for g in groups if g not in hit] + [sorted(merged, key=cats.index)]
    base_t = R.rating_total(base, height_cm, wfa_idx, position)

    def max_n(c):
        n = 0
        while R.cumulative_cost(n + 1) <= budget:
            n += 1
        return n

    dp: Dict[int, List[Tuple[int, Dict[str, int]]]] = {0: [(0, {})]}
    for g in groups:
        opts = []
        for ns in itertools.product(*(range(max_n(c) + 1) for c in g)):
            cost = sum(R.cumulative_cost(n) for n in ns)
            if cost > budget:
                continue
            a = dict(zip(g, ns))
            opts.append((cost, R.rating_total(R.apply_training(base, a), height_cm, wfa_idx, position) - base_t, a))
        nd: Dict[int, List[Tuple[int, Dict[str, int]]]] = {}
        for cost0, items in dp.items():
            for cost, val, a in opts:
                t = cost0 + cost
                if t > budget:
                    continue
                for v0, a0 in items:
                    nd.setdefault(t, []).append((v0 + val, {**a0, **a}))
        dp = {t: sorted(L, key=lambda x: -x[0])[:k] for t, L in nd.items()}
    out = [(v + base_t, t, {c: int(a.get(c, 0)) for c in cats}) for t, L in dp.items() for v, a in L]
    out.sort(key=lambda x: (-x[0], x[1], [-x[2][c] for c in cats]))
    return out[:k]


def order_steps(base: Mapping[str, int], alloc: Mapping[str, int], height_cm: int, wfa_idx: int, position: str
                ) -> List[Step]:
    """给定最终加点，排出一个"逐步点击"的顺序：每步在剩余次数里选 (总评增量 ÷ 花费) 最大的类别。"""
    remaining = {c: int(n) for c, n in alloc.items() if n}
    cur = {c: 0 for c in alloc}
    steps: List[Step] = []
    used = 0
    while any(remaining.values()):
        base_raw = R.raw_overall(R.apply_training(base, cur), height_cm, wfa_idx, position)
        best, best_gain = None, None
        for c in alloc:
            if not remaining.get(c):
                continue
            cost = R.step_cost(cur[c] + 1)
            t = dict(cur)
            t[c] += 1
            g = (R.raw_overall(R.apply_training(base, t), height_cm, wfa_idx, position) - base_raw) / cost
            if best_gain is None or g > best_gain:
                best, best_gain = c, g
        cost = R.step_cost(cur[best] + 1)
        cur[best] += 1
        remaining[best] -= 1
        used += cost
        s = R.apply_training(base, cur)
        steps.append(Step(len(steps) + 1, best, cur[best], cost, used, R.overall(s, height_cm, wfa_idx, position),
                          R.raw_overall(s, height_cm, wfa_idx, position)))
    return steps


# ---------------------------------------------------------------------------------------------
# 阶段计算
# ---------------------------------------------------------------------------------------------

STAGE_LABELS = {
    "L1": "1 级卡面（无增能）",
    "L1+FIXED": "1 级 + 已激活固定增能",
    "MAX": "满级中性（培养后，无增能、无主教练）",
    "MAX+FIXED": "满级 + 已激活固定增能",
    "MAX+BOOSTERS": "满级 + 固定增能 + 可训练（制作）增能",
    "MAX+MANAGER": "满级 + 主教练熟练度 + 全部增能 + 主教练增能",
}


@dataclass
class Result:
    card: PlayerCard
    alloc: Dict[str, int]
    alloc_kind: str
    alloc_mode: str
    steps: List[Step]
    stages: Dict[str, Dict[str, int]]
    ratings: Dict[str, Dict[str, int]]
    per_level: List[Tuple[int, int, Dict[str, int], int]]
    match: Dict[str, Dict[str, int]]
    situational: List[Tuple[str, str, List[Tuple[str, int, int, int]]]]
    issues: List[Issue]
    aim: List[Tuple[str, str, Dict[str, Tuple[int, int]]]] = field(default_factory=list)


def resolve_allocation(card: PlayerCard, mode: Optional[str] = None, auto_with_manager: bool = False
                       ) -> Tuple[Dict[str, int], str, str, List[Step]]:
    """返回 (加点, 类型 auto/optimal/card, 说明, 步骤)。

    auto_with_manager=True：自动加点按"比赛计划下（含主教练熟练度）"的总评最大化（RULES AUTO_DEV_MANAGER，未核实）。
    """
    spec = card.allocation if mode in (None, "card") else mode
    cats = R.categories_for_position(card.registered_position)
    args = (card.stats, card.height_cm, card.wfa_idx, card.registered_position)
    if spec == "auto":
        m = card.manager
        if auto_with_manager and m.proficiency is not None:
            alloc, steps = greedy_allocation(*args, card.budget, categories=cats, manager_p=m.proficiency, manager_model=m.model)
            return alloc, "auto", (f"auto（复现 pesdb 贪心，按含主教练熟练度 {m.proficiency}（{m.model}）的总评最大化；"
                                   "游戏内是否这样做未找到）"), steps
        alloc, steps = greedy_allocation(*args, card.budget, categories=cats)
        return alloc, "auto", "auto（复现 pesdb 贪心；游戏内'自动提升'的目标是最大化总评（T1），具体算法未找到）", steps
    if spec == "optimal":
        obj = card.objective_positions or {card.registered_position: 1.0}
        alloc = optimal_allocation(card.stats, card.budget, objective_weights(obj), categories=cats, targets=card.targets,
                                   limits=card.limits)
        desc = "optimal（精确最大化 " + " + ".join(f"{w:g}×{p}" for p, w in obj.items()) + " 总评权重"
        desc += (f"，软上限 {card.targets}" if card.targets else "") + (f"，硬上限 {card.limits}" if card.limits else "") + "）"
        return alloc, "optimal", desc, order_steps(card.stats, alloc, *args[1:])
    alloc = {c: int(spec.get(c, 0)) for c in R.CATEGORY_KEYS if spec.get(c, 0) or c in cats}
    return alloc, "card", "card（输入文件给定；步骤顺序为本工具按总评增量排出）", order_steps(card.stats, alloc, *args[1:])


def simulate(card: PlayerCard, *, allocation: Optional[str] = None, manager_p: Optional[int] = None,
             manager_model: Optional[str] = None, manager_boosters: Optional[Mapping[str, int]] = None,
             per_level: bool = True, auto_with_manager: bool = False) -> Result:
    if manager_p is not None:
        card.manager.proficiency = manager_p
    if manager_model is not None:
        card.manager.model = manager_model
    if manager_boosters:
        card.manager.boosters = dict(manager_boosters)
    alloc, kind, mode, steps = resolve_allocation(card, allocation, auto_with_manager)
    issues = validate_card(card, alloc)

    h, w = card.height_cm, card.wfa_idx
    fixed = card.booster_bonus(("fixed",))
    allb = card.booster_bonus(("fixed", "craftable"))
    trained = R.apply_training(card.stats, alloc)
    stages = {
        "L1": dict(card.stats),
        "L1+FIXED": R.apply_boosters(card.stats, fixed),
        "MAX": trained,
        "MAX+FIXED": R.apply_boosters(trained, fixed),
        "MAX+BOOSTERS": R.apply_boosters(trained, allb),
    }
    m = card.manager
    if m.proficiency is not None or m.boosters:
        scaled = R.apply_manager(trained, m.proficiency, m.model)
        stages["MAX+MANAGER"] = R.apply_boosters(scaled, R.sum_bonus(allb, m.boosters))
    ratings = {k: R.overall_all_positions(v, h, w) for k, v in stages.items()}

    for k, v in stages.items():
        over = [s for s in R.STAT_KEYS if v[s] > R.BOOSTED_MAX]
        if over:
            issues.append(Issue("ERROR", "BOOST_CAP", f"{k}: {over} 超过 {R.BOOSTED_MAX}", "BOOST_CAP_120"))
    for s in R.STAT_KEYS:
        add = allb.get(s, 0) + m.boosters.get(s, 0)
        if add and trained[s] + add > R.BOOSTED_MAX:
            issues.append(Issue("WARN", "BOOST_CAP", f"{s}: {trained[s]}+{add} 超过 {R.BOOSTED_MAX}，按 {R.BOOSTED_MAX} 截断", "BOOST_CAP_120"))

    levels: List[Tuple[int, int, Dict[str, int], int]] = []
    cats = R.categories_for_position(card.registered_position)
    if per_level and kind == "auto":
        mkw = ({"manager_p": card.manager.proficiency, "manager_model": card.manager.model}
               if auto_with_manager and card.manager.proficiency is not None else {})
        for L in range(1, card.max_level + 1):
            b = R.progression_points(L)
            a, _ = greedy_allocation(card.stats, h, w, card.registered_position, b, categories=cats, **mkw)
            levels.append((L, b, a, R.overall(R.apply_training(card.stats, a), h, w, card.registered_position)))
    # 与精确最优比较（注册位置总评，无增能；增能在 99 后相加，不影响最优加点）
    best = optimal_allocation(card.stats, card.budget, objective_weights({card.registered_position: 1}), categories=cats)
    t_best = R.rating_total(R.apply_training(card.stats, best), h, w, card.registered_position)
    t_used = R.rating_total(trained, h, w, card.registered_position)
    if t_best > t_used:
        deliberate = (kind == "optimal" and (bool(card.targets) or bool(card.limits) or
                                             (card.objective_positions or {card.registered_position: 1.0}) != {card.registered_position: 1.0})
                      ) or (kind == "auto" and auto_with_manager and m.proficiency is not None)
        lvl = "INFO" if (t_best // 1000 == t_used // 1000 or deliberate) else "WARN"
        issues.append(Issue(lvl, "ALLOC_NOT_OPTIMAL", f"所用加点的注册位置总评 raw={t_used / 1000:.3f}，精确最优 raw={t_best / 1000:.3f}"
                            f"（floor {t_used // 1000} vs {t_best // 1000}）：" + ", ".join(f"{k}={v}" for k, v in best.items() if v),
                            "AUTO_DEV_GREEDY"))

    # 比赛中的实际能力：各可用位置（满体力、状态正常、无情境技能），主教练按 efx 模型
    match: Dict[str, Dict[str, int]] = {}
    mb = R.sum_bonus(allb, m.boosters)
    for p in card.usable_positions():
        match[p] = R.match_stats(trained, proficiency=card.proficiency[p], manager_p=m.proficiency, booster_bonus=mb)
    # 技能情境加成（注册位置）
    situ = []
    for s in card.skills + card.additional_skills:
        sk = R.SKILLS[s]
        if not sk.stat_bonus:
            continue
        bonus = {k: (sk.bonus_max if sk.bonus_max else v) for k, v in sk.stat_bonus}
        with_ = R.match_stats(trained, proficiency="high", manager_p=m.proficiency, booster_bonus=mb, skill_bonus=bonus)
        without = match.get(card.registered_position) or R.match_stats(trained, manager_p=m.proficiency, booster_bonus=mb)
        rows = [(k, bonus[k], without[k], with_[k]) for k in bonus]
        situ.append((s, sk.condition, rows))
        wasted = [k for k, b, a0, a1 in rows if a1 - a0 < b]
        if wasted:
            issues.append(Issue("INFO", "SKILL_BONUS_CAPPED", f"{s} 的加成在 99 封顶前相加，"
                                f"{', '.join(wasted)} 已接近/达到 99，加成部分浪费", "SKILL_BONUS_BEFORE_CAP"))

    # "按能力 +N 瞄准/计算"型技能的有效值（注册位置、完整培养；不受 99 限，RULES SKILL_AIM_UNCAPPED）
    aim = []
    reg_match = match.get(card.registered_position) or R.match_stats(trained, manager_p=m.proficiency, booster_bonus=mb)
    for s in card.skills + card.additional_skills:
        if R.SKILLS[s].aim_bonus:
            aim.append((s, R.SKILLS[s].aim_condition, R.aim_effective(reg_match, s)))

    # 设计护栏（T4，不是规则）
    pos = card.registered_position
    l1, mx = ratings["L1"][pos], ratings["MAX+FIXED"][pos]
    if card.card_type in (None, "Epic", "Big Time") and pos in ("CF", "SS", "LWF", "RWF"):
        if not (84 <= l1 <= 87) or not (97 <= mx <= 103):
            issues.append(Issue("INFO", "DESIGN_BAND", f"{pos} 1 级总评 {l1}、满级+固定增能 {mx}；"
                                "同类 Epic/Big Time 锋线卡抽样为 1 级 84–87、满级 97–103", "DESIGN_BAND"))
    top = [s for s in R.STAT_KEYS if trained[s] >= 95]
    if len(top) >= 8:
        issues.append(Issue("WARN", "DESIGN_TOO_HIGH", f"满级中性卡有 {len(top)} 项 ≥95：{', '.join(top)}"
                            "（任务书 §6：不能全部 95–99）", "DESIGN_BAND"))
    return Result(card, alloc, kind, mode, steps, stages, ratings, levels, match, situ, issues, aim)


# ---------------------------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------------------------

def _w(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _ljust(text: str, width: int) -> str:
    return text + " " * max(0, width - _w(text))


def _rjust(text: str, width: int) -> str:
    return " " * max(0, width - _w(text)) + text


def _stat_label(k: str) -> str:
    s = R.STAT_BY_KEY[k]
    return _ljust(f"{s.id:>2} {s.en} {s.zh}", 38)


def format_report(res: Result, show_steps: bool = True, show_levels: bool = True) -> str:
    c = res.card
    L: List[str] = []
    P = L.append
    P(f"=== {c.name or '(未命名)'}  card_id={c.card_id or '-'}  卡种={c.card_type or '-'} ===")
    P(f"注册位置 {c.registered_position}；身高 {c.height_cm} cm；体重 {c.weight_kg or '-'} kg（比赛逻辑不用体重，社区逆向）；年龄 {c.age or '-'}")
    P(f"惯用脚 {c.stronger_foot or '-'}；逆足频率 {c.weak_foot_usage or '-'}"
      f"（{R.WEAK_FOOT_USAGE[c.weak_foot_usage].note if c.weak_foot_usage else ''}）；"
      f"逆足精度 {c.weak_foot_accuracy}（pesdb 编码 {c.wfa_idx}，总评 WFR={R.weak_foot_rating(c.wfa_idx)}；"
      f"逆足踢球扣分 {R.WEAK_FOOT_ACCURACY[c.weak_foot_accuracy].note}）{R.RULES['WEAK_FOOT'].tag()}")
    if c.live_update_rating:
        P(f"Live Update Rating {c.live_update_rating}（{R.LIVE_UPDATE_RATING[c.live_update_rating].note}）{R.RULES['LIVE_UPDATE_RATING'].tag()}")
    P(f"状态波动 {c.form or '-'}（影响状态抽取，比赛逻辑不直接读取）{R.RULES['FORM'].tag()}；抗伤 {c.injury_resistance or '-'}"
      f"（{R.INJURY_RESISTANCE[c.injury_resistance].note if c.injury_resistance else ''}）")
    P("位置适性：" + "  ".join(f"{p}={R.PROFICIENCY_ZH[c.proficiency[p]]}" for p in R.POSITIONS if c.proficiency[p] != "low")
      + "（其余为低）")
    usable = c.usable_positions()
    for lbl, table, nm in (("进攻型风格", R.ATTACKING_STYLES, c.attacking_style), ("防守型风格", R.DEFENSIVE_STYLES, c.defensive_style)):
        st = table[nm]
        act = R.style_active_positions(st, usable)
        P(f"{lbl} {nm}（{st.zh}；美式 {st.en_us}）兼容 {', '.join(st.positions) or '—'}；本卡生效位置 {', '.join(act) or '—'}")
    P(f"AI 风格：{', '.join(c.ai_styles) or '无'}")
    P(f"卡面技能（{len(c.skills)}/{R.MAX_CARD_SKILLS}）：" + ", ".join(
        f"{s}{'★' if R.SKILLS[s].special else ''}" for s in c.skills) + "（★=特殊技能）")
    P(f"附加技能（{len(c.additional_skills)}/{R.MAX_ADDITIONAL_SKILLS}）：{', '.join(c.additional_skills) or '无'}")
    P("增能：" + ("；".join(b.label() for b in c.boosters) or "无"))
    P(f"最高等级 {c.max_level} → 能力点 {c.budget} = 2×({c.max_level}−1) {R.RULES['POINTS'].tag()}")
    P("")

    P(f"--- 加点方案：{res.alloc_mode} ---")
    P("  " + ", ".join(f"{R.TRAINING_CATEGORIES[k].en}({R.TRAINING_CATEGORIES[k].zh})={v}" for k, v in res.alloc.items() if v)
      + f"；花费 {R.allocation_cost(res.alloc)}/{c.budget} {R.RULES['STEP_COST'].tag()}")
    if show_steps and res.steps:
        P("  " + _ljust("步骤", 5) + _ljust("类别", 22) + _rjust("第n次", 6) + _rjust("花费", 6) + _rjust("累计", 6) + "  注册位置总评(无增能)")
        for s in res.steps:
            P(f"  {s.index:>3}  {R.TRAINING_CATEGORIES[s.category].en:<22}{s.nth:>6}{s.cost:>6}{s.used:>6}  {s.ovr}")
    if show_levels and res.per_level:
        P("  每级自动分配（每次升级重新分配全部点数，复现 pesdb 贪心）：")
        for Lv, b, a, o in res.per_level:
            if Lv in (1, c.max_level) or Lv % 5 == 0:
                P(f"    Lv{Lv:>2} 点数 {b:>3} 总评 {o}  " + ", ".join(f"{k}={v}" for k, v in a.items() if v))
    P("")

    stage_keys = list(res.stages)
    P("--- 26 项能力：各阶段（增能在 99 封顶之后相加，上限 120）---")
    hdr = "  " + _ljust("能力", 38) + "".join(f"{k:>14}" for k in stage_keys)
    P(hdr)
    for k in R.STAT_KEYS:
        P(f"  {_stat_label(k)}" + "".join(f"{res.stages[s][k]:>14}" for s in stage_keys))
    for s in stage_keys:
        P(f"  {s}: {STAGE_LABELS[s]}")
    if "MAX+MANAGER" in res.stages:
        m = c.manager
        P(f"  主教练：熟练度 {m.proficiency}（模型 {m.model}）、增能 {m.boosters or '无'} "
          f"{R.RULES['MANAGER_FORMULA' if m.model == 'efx' else 'MANAGER_THRESHOLDS'].tag()}；"
          f"游戏内总评是否计入主教练影响 {R.RULES['MANAGER_IN_RATING'].tag()}")
    P("")
    P(f"--- 各位置总评 {R.RULES['RATING_FORMULA'].tag()} ---")
    P("  " + _ljust("阶段", 14) + "".join(f"{p:>5}" for p in R.POSITIONS))
    for s in stage_keys:
        P(f"  {s:<14}" + "".join(f"{res.ratings[s][p]:>5}" for p in R.POSITIONS))
    P("")
    P(f"--- 比赛中实际能力（满体力、状态正常、无情境技能；位置适性/主教练按 efx 模型）{R.RULES['MATCH_ORDER'].tag()} ---")
    mp = list(res.match)
    P("  " + _ljust("能力", 38) + "".join(f"{p + '(' + c.proficiency[p][0].upper() + ')':>10}" for p in mp))
    for k in R.STAT_KEYS:
        if k in R.GK_STAT_KEYS and c.registered_position != "GK":
            continue
        P(f"  {_stat_label(k)}" + "".join(f"{res.match[p][k]:>10}" for p in mp))
    if res.situational:
        P(f"  技能情境加成（注册位置，按最大叠加）{R.RULES['SKILL_BONUS_BEFORE_CAP'].tag()}：")
        for s, cond, rows in res.situational:
            P(f"    {s}（{cond}）：" + "；".join(f"{k} +{b}: {a0}→{a1}" for k, b, a0, a1 in rows))
    else:
        P(f"  直接加能力值的技能：无 {R.RULES['SKILL_BONUS_BEFORE_CAP'].tag()}")
    if res.aim:
        P(f"  '按能力 +N 瞄准/计算'型技能的有效值（注册位置；不改能力值，不受 99 封顶）{R.RULES['SKILL_AIM_UNCAPPED'].tag()}：")
        for s, cond, vals in res.aim:
            P(f"    {s}（{cond}）：" + "；".join(f"{R.STAT_BY_KEY[k].en} {a}→{e}" for k, (a, e) in vals.items()))
    P("")
    P("--- 校验 ---")
    if not res.issues:
        P("  无问题")
    for i in sorted(res.issues, key=lambda x: ("ERROR", "WARN", "INFO").index(x.level)):
        P("  " + str(i))
    P("")
    used_rules = ["STAT_RANGE", "POINTS", "STEP_COST", "CATEGORY_MAP", "TRAIN_CAP_99", "AUTO_DEV_GOAL", "AUTO_DEV_GREEDY",
                  "BOOST_AFTER_CAP", "BOOST_CAP_120", "RATING_FORMULA", "RATING_WEIGHTS", "MANAGER_FORMULA",
                  "MANAGER_THRESHOLDS", "MATCH_ORDER", "OUT_OF_POSITION", "POSITION_LEVELS", "SKILL_LIST", "SKILL_CARD_MAX",
                  "SKILL_CARD_COUNT_TYPICAL", "SKILL_SPECIAL", "SKILL_BONUS_BEFORE_CAP", "SKILL_AIM_UNCAPPED",
                  "AUTO_DEV_MANAGER", "BOOSTER_LIST", "BOOSTER_FIXED_RANGE", "STYLE_COMPAT"]
    P("--- 本次用到的规则与出处 ---")
    for r in used_rules:
        rule = R.RULES[r]
        P(f"  {r}: {rule.text} —— {rule.source} {rule.tag()}")
    return "\n".join(L)


def result_to_json(res: Result) -> Dict[str, Any]:
    c = res.card
    return {
        "name": c.name, "card_id": c.card_id, "registered_position": c.registered_position,
        "max_level": c.max_level, "progression_points": c.budget,
        "allocation_kind": res.alloc_kind, "allocation_mode": res.alloc_mode, "allocation": res.alloc, "allocation_cost": R.allocation_cost(res.alloc),
        "steps": [s.__dict__ for s in res.steps],
        "stages": res.stages, "ratings": res.ratings,
        "per_level": [{"level": L, "points": b, "allocation": a, "overall": o} for L, b, a, o in res.per_level],
        "match": res.match,
        "aim_effective": [{"skill": s, "condition": c, "values": {k: {"match": a, "effective": e} for k, (a, e) in v.items()}}
                          for s, c, v in res.aim],
        "issues": [i.__dict__ for i in res.issues],
        "note": "全部规则来自社区逆向/数据库（T3）与本组推断（T4），数值待客户端核实；见 efootball_rules.RULES",
    }


def _kv(items: Sequence[str]) -> Dict[str, int]:
    out = {}
    for it in items:
        k, v = it.split("=", 1)
        out[R.resolve_stat(k)] = int(v)
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="eFootball 球员卡培养模拟与校验（规则见 efootball_rules.py）")
    ap.add_argument("player", help="球员 JSON 文件")
    ap.add_argument("--allocation", choices=["card", "auto", "optimal"], default="card",
                    help="card=用 JSON 里的 allocation（默认）；auto=复现自动分配；optimal=精确最优")
    ap.add_argument("--objective", help="optimal 的目标位置及系数，如 CF=1,SS=0.25（覆盖 JSON）")
    ap.add_argument("--targets", help="optimal 的能力软上限 JSON 文件（超过目标的部分不计分）")
    ap.add_argument("--limits", help="optimal 的能力硬上限 JSON 文件（培养后不得超过）")
    ap.add_argument("--manager", type=int, help="主教练球队风格熟练度（0–99）")
    ap.add_argument("--manager-model", choices=["efx", "pesdb"], help="主教练倍率模型（默认 efx）")
    ap.add_argument("--manager-booster", nargs="*", default=[], help="主教练增能，如 physical_contact=1 stamina=1")
    ap.add_argument("--auto-with-manager", action="store_true",
                    help="--allocation auto 时按含主教练熟练度的总评最大化（游戏内是否这样做未找到，待客户端核实）")
    ap.add_argument("--json", help="把结果另存为 JSON")
    ap.add_argument("--no-steps", action="store_true")
    ap.add_argument("--no-levels", action="store_true")
    a = ap.parse_args(argv)

    try:
        card, issues = load_card(a.player)
    except CardError as e:
        for i in e.issues:
            print(i)
        return 1
    if a.objective:
        card.objective_positions = {p.split("=")[0].strip().upper(): float(p.split("=")[1]) for p in a.objective.split(",")}
    if a.targets:
        with open(a.targets, encoding="utf-8") as fh:
            card.targets = {R.resolve_stat(k): int(v) for k, v in json.load(fh).items()}
    if a.limits:
        with open(a.limits, encoding="utf-8") as fh:
            card.limits = {R.resolve_stat(k): int(v) for k, v in json.load(fh).items()}
    res = simulate(card, allocation=a.allocation, manager_p=a.manager, manager_model=a.manager_model,
                   manager_boosters=_kv(a.manager_booster), per_level=not a.no_levels,
                   auto_with_manager=a.auto_with_manager)
    res.issues = issues + res.issues
    print(format_report(res, show_steps=not a.no_steps, show_levels=not a.no_levels))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(result_to_json(res), fh, ensure_ascii=False, indent=1)
    return 1 if any(i.level == "ERROR" for i in res.issues) else 0


if __name__ == "__main__":
    sys.exit(main())
