"""eFootball（iOS 11.1.1 / 游戏 v6.1.x）球员卡数据规则 —— 只含规则与数据表，不含任何具体球员的设计数值。

全部内容取自 research/notes/04_efootball_systems.md（下称"04 号档案"），节号写在每张表旁边。

证据分级沿用 04 号档案：
  T1 官方/一手；T2 媒体；T3 社区数据库与逆向（pesdb、eFHUB、efootballexplained 等）；T4 本组推断。
  * 凡是 T3/T4 的规则，`RULES` 里都写明"社区逆向"或"本组推断"；
  * 凡是必须在客户端（IPA 数据表或实机）上确认的值，`RULES[...].client_check` 为 True，
    输出时一律标注"待客户端核实"。
  * 本模块里的键名（offensive_awareness 等）和增能 id 是 pesdb 页面数据里的写法，**不是客户端字段名**；
    客户端字段名、枚举编码、位序全部未知（04 §9.8、§10 U2/U4）。

范围（任务书 §14）：只描述一张球员卡的能力、培养、增能、主教练影响与总评。
不涉及卡池、经济、奖励或 T0 获取资格。
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------------------------
# 0. 规则登记表：每条规则的出处、分级、是否待客户端核实
# ---------------------------------------------------------------------------------------------

CLIENT_CHECK = "待客户端核实"


@dataclass(frozen=True)
class Rule:
    id: str
    text: str            # 中文说明
    tier: str            # "T1" / "T3" / "T4" / 组合，如 "T1+T3"
    source: str          # 04 号档案节号与来源编号
    client_check: bool   # True = 待客户端核实

    @property
    def community(self) -> bool:
        """是否（部分）来自社区逆向/社区数据库。"""
        return "T3" in self.tier

    def tag(self) -> str:
        parts = [self.tier]
        if self.community:
            parts.append("社区逆向")
        if "T4" in self.tier:
            parts.append("本组推断")
        if self.client_check:
            parts.append(CLIENT_CHECK)
        return "[" + "，".join(parts) + "]"


_RULE_LIST = [
    Rule("STAT_RANGE", "卡面 26 项能力取值 40–99（外场球员 5 项 GK 能力抽查均为 40）", "T3", "04 §2.2 [S21][S25]", True),
    Rule("STAT_IDS", "26 项能力的键名/英文名/简中名；键名是 pesdb 页面键，不是客户端字段名", "T1+T3", "04 §2.1 [S13][S17][S20][S21]", True),
    Rule("BOOST_ABOVE_99", "增能可以让能力超过 99", "T1", "04 §2.2 [S06]", False),
    Rule("BOOST_AFTER_CAP", "增能点在 99 封顶之后相加，且不被主教练倍率缩放", "T3", "04 §6.6 [S25][S22]", True),
    Rule("BOOST_CAP_120", "加上增能后的能力上限 120", "T3", "04 §2.2/§6.6 [S25]", True),
    Rule("BOOST_SAME_STAT_ADD", "两个增能槽作用在同一项能力上时直接相加", "T3", "04 §6.6 [S22]", True),
    Rule("POINTS", "可用能力点 = 2 × (最高等级 − 1)", "T3", "04 §5.2 [S22][S26][S23]", True),
    Rule("STEP_COST", "同一类别第 n 次加点花费 ⌈n/4⌉；前 n 次累计 (g+1)(2g+r)，g=⌊n/4⌋，r=n mod 4；"
         "第 13–16 次每次 4 点由 ef-labo 明确给出，第 17–20 次每次 5 点由 pesdb 代码直接给出（不是外推，04 核查记录 C13）",
         "T3", "04 §5.4 [S22][S26]、核查记录 C13", True),
    Rule("CATEGORY_MAP", "10 个培养类别与能力的对应（Jumping 同属 Aerial Strength 与 GK 1）", "T3", "04 §5.3 [S22][S26]", True),
    Rule("TRAIN_CAP_99", "培养加点在卡面阶段完成，每项最高 99", "T3", "04 §6.6 第 7 条 [S22]", True),
    Rule("CATEGORY_STEP_LIMIT", "单一类别的加点次数上限：未找到（pesdb 不设上限）", "T3", "04 §5.4、§10 U9", True),
    Rule("GK_CATEGORIES", "GK 1–3 类别只在门将位置启用（pesdb 的行为）", "T3", "04 §5.3 [S22]", True),
    Rule("AUTO_DEV_GOAL", "游戏内'自动提升'（Auto Development）的目标是每次升级后重新分配全部点数使总评最大", "T1", "04 §5.5 [S10][S17]", False),
    Rule("AUTO_DEV_GREEDY", "pesdb 的最大化算法为贪心：每步选'总评增量 ÷ 花费'最高的类别（并列取类别顺序靠前者，"
         "零增量也会被选）。游戏内是否同一算法：未找到", "T3", "04 §5.5 [S22]、§10 U10", True),
    Rule("RATING_FORMULA", "总评 = floor([Σ(能力−25)×权重 + (身高−136)×身高权重 + (WFR−25)×4 + 500] / 1000)，"
         "WFR = floor(59×WFA/3+40)；能力为培养后+增能后值；不含技能/风格/状态。"
         "04 §7.3 原称 97 张卡 1 级、51 张卡满级全部复算一致；核查记录 C16 只独立重算了 5 张卡（全部一致），"
         "97/97、51/51 全量【未核实】", "T3+T4", "04 §7.1 [S22]、§7.2（C15 逐格核对）、§7.3 与核查记录 C16", True),
    Rule("RATING_WEIGHTS", "13 个位置 × 26 项能力 + 身高的总评权重表（原样摘自 pesdb，含 LMF/RMF 不对称）", "T3", "04 §7.2 [S22]", True),
    Rule("MANAGER_FORMULA", "主教练球队风格熟练度倍率：p<90 时 (104−38(p−99)²/99²)/100，p≥90 时 (99.253+0.0485p)/100；"
         "增量 = (倍率−1)×卡面值，向零取整；作用于 26 项（逆足精度不在其内），结果封顶 99", "T3", "04 §6.5 [S25]", True),
    Rule("MANAGER_THRESHOLDS", "pesdb/ef-labo 的主教练熟练度门槛表（p=70–90）：能力值达到门槛即 +1/+2/+3。"
         "与 MANAGER_FORMULA 在 p=73、81、82、84、85、86、90 处有一个门槛差 1–2 点（本组实现时发现，"
         "见 manager_threshold_mismatches()）；其余 14 个值一致。04 §6.5 称两者'同一回事'，与此不完全相符", "T3+T4", "04 §6.5 [S22][S26]", True),
    Rule("MANAGER_BOOSTER", "主教练自带增能（如 Physical Contact +1）对全队生效，在 99 之后相加，不缩放", "T3", "04 §6.5 [S26][S25]", True),
    Rule("MANAGER_IN_RATING", "比赛计划画面显示的总评是否包含主教练影响：未核实（pesdb 选了主教练时会计入）", "T3", "04 §7.1 [S22][S03]", True),
    Rule("MATCH_ORDER", "比赛中叠加顺序：卡面+状态→[40,99]→+技能加成/Link-up→+(位置×疲劳×主教练−1)×卡面值(向零取整)"
         "→[40,99]→+增能（≤120）", "T3", "04 §6.6 [S25]", True),
    Rule("OUT_OF_POSITION", "非高适性位置的能力倍率（淡绿/无色三组），只按卡面值计算，增能不打折", "T3", "04 §3.6 [S25]", True),
    Rule("POSITION_LEVELS", "位置适性 3 级。官方（T1）只给出两种颜色：Bright green 亮绿 = 高、Faded green 淡绿 = 中；"
         "'无色 = 低'与'注册位置永远是高'出自 efootballexplained（T3）。客户端里的位置序号与 3 档编码未知",
         "T1+T3", "04 §3.6 [S08]、核查记录 C18", True),
    Rule("SKILL_LIST", "67 种球员技能（pesdb eFootball 2027 筛选器）；客户端技能位序/编码与 11.1.1 中是否全部存在未核对", "T3", "04 §4.1–4.2 [S20][S25]、§10 U4", True),
    Rule("SKILL_CARD_MAX", "卡面技能实测最多 10 个（硬性上限未找到；本工具按 10 个作硬限制）", "T3", "04 §4.1、§10 U3", True),
    Rule("SKILL_ADDITIONAL_MAX", "附加技能最多 5 个（v2.5.0 官方）", "T1", "04 §4.1 [S15]", False),
    Rule("SKILL_SPECIAL", "特殊技能不能训练，只能写在卡面；19 个 Show Time 技能（[S34]）+ 4 个 2026 新技能（本组推断）。"
         "[S34] 正文写'over 20 Showtime picks'，实列 19 个，自相矛盾（C22）；网易 [S37] 是 PC 端游《实况足球Online》，"
         "不是手游国服，不再作旁证（C27）。客户端的'特殊'标志位未找到（U4）", "T2+T3+T4", "04 §4.1 [S34][S41]、核查记录 C22/C27、§10 U4/U5", True),
    Rule("SKILL_DUMMY", "11.1.1 代码中 7 个未发布占位技能（Dummy），不得使用", "T3", "04 §4.1 [S25]", True),
    Rule("SKILL_NO_DUPLICATE", "不能学已有的技能", "T3", "04 §4.1（指南）", True),
    Rule("SKILL_BONUS_BEFORE_CAP", "直接加能力值的技能（active stat-boost skills，如 Momentum Dribbling、Magnetic Feet、"
         "Attacking Surge、Willpower）在 99 封顶之前相加，已到 99 的能力不再受益。只限这一类（核查记录 C21）",
         "T3", "04 §4.2 [S25]、核查记录 C21", True),
    Rule("SKILL_AIM_UNCAPPED", "'按某能力 +N 瞄准/计算'一类技能（Through Passing、Phenomenal Finishing、Acrobatic Finishing、"
         "Long-range Shooting 等）不受 99 封顶限制，在增能之后仍然生效。Through Passing 有原文 'even at 99 and with extra "
         "points from boosters'（T3）；其余同类技能按同一机制推断（T4）。是否另有上限（如 120）未找到",
         "T3+T4", "04 §4.2 核查更正、核查记录 C21", True),
    Rule("SKILL_CARD_COUNT_TYPICAL", "抽样所见 Epic、Big Time、Show Time 卡（约 60 张）的卡面技能都正好 10 个。"
         "少于 10 个的卡在客户端里如何编码（定长 10 槽还是逐技能标志位，空槽怎么表示）、是否被接受：未找到",
         "T3", "04 §4.1、核查记录 C09、§10 U3/U4", True),
    Rule("AUTO_DEV_MANAGER", "v6.0.0 起培养界面显示'当前比赛计划下'的能力（含主教练影响，T1）；pesdb 的最大化在选了主教练时"
         "也把主教练算进去（T3）。游戏内'自动提升'最大化的是不含主教练的卡面总评还是比赛计划下的总评：未找到",
         "T1+T3", "04 §5.5 [S03]、§7.1 [S22]、§10 U10", True),
    Rule("STYLE_SPLIT", "v6.0.0 起比赛风格分进攻型 + 防守型，每人有其一或两者，没有特色的一侧为 Basic", "T1+T3", "04 §3.7 [S03][S21]", False),
    Rule("STYLE_COMPAT", "比赛风格的兼容位置（11.1.1 代码），不兼容位置上大部分失效；LWF/RWF 等左右同组", "T3", "04 §3.7 [S25]（Classic No. 10 另有 T1 [S08]）", True),
    Rule("AI_STYLE_MAX", "一张卡的 AI 风格实测最多 3 个；AI 风格无位置检查", "T3", "04 §3.7 [S21][S25]", True),
    Rule("WEAK_FOOT", "逆足频率 4 档、逆足精度 4 档；pesdb 页面数据的精度编码 0–3（客户端编码未知）", "T3", "04 §3.1–3.2 [S20][S22][S25]", True),
    Rule("FORM", "状态波动 3 档：Inconsistent / Standard / Unwavering；影响状态抽取，比赛逻辑不直接读取（逆向）", "T1+T3", "04 §3.3 [S06][S25]", True),
    Rule("INJURY", "抗伤 3 档：Low / Medium / High", "T3", "04 §3.4 [S20][S25]", True),
    Rule("LIVE_UPDATE_RATING", "Live Update Rating（实时更新评价）A–E 五档；第三方指南称 Epic、Big Time、Legendary 卡固定为 B"
         "（T3 [S29]，核查记录 C30）。官方在 Form 与条件型增能的说明中提到 'B or higher'（T1）", "T1+T3",
         "04 §3.3 [S06][S07][S29]、核查记录 C30", True),
    Rule("BOOSTER_LIST", "常规增能 29 种（pesdb 列表）与官方'全部 28 种'冲突；各项 4 能力组合取自 pesdb", "T1+T3", "04 §6.2 [S14][S21][S28]、§10 U6", True),
    Rule("BOOSTER_SLOTS", "一张卡最多 2 个增能槽（Epic/Big Time/Show Time 抽样观察）", "T3", "04 §6.4 [S21]", True),
    Rule("BOOSTER_CRAFTED", "空增能槽可用随机/选择增能代币制作常规增能，每项 +1", "T1+T3", "04 §6.1 [S10][S14]、§6.2 [S21][S28]", True),
    Rule("BOOSTER_FIXED_RANGE", "卡面固定增能抽样所见：4 项型 +3 至 +6；单项型 +6 或 +8；Total Package +3（26 项全部）", "T3", "04 §6.3–6.4 [S21]", True),
    Rule("BOOSTER_SLOT2", "Epic/Show Time 增能 2（单项 +6/+8 或 4 项 +3）'默认未激活'的含义：未找到", "T3", "04 §6.4、§10 U7", True),
    Rule("BOOSTER_SPECIAL", "专属增能（Son of God 等）的能力组合与数值：只有 Total Package 已知", "T3", "04 §6.3、§10 U8", True),
    Rule("LEVEL_CAP", "Epic/Big Time/Show Time 观察到的最高等级 31–34（60–66 点），Highlight 29–36", "T3", "04 §5.2 [S21]（抽样）", True),
    Rule("DESIGN_BAND", "设计护栏：同类 Epic/Big Time 锋线卡 1 级总评 84–87、满级 97–103（不是规则，是对照）", "T3+T4", "04 §8.3 观察、§9.5", False),
]
RULES: Dict[str, Rule] = {r.id: r for r in _RULE_LIST}


# ---------------------------------------------------------------------------------------------
# 1. 26 项能力（04 §2.1）
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class StatDef:
    id: int                    # 04 §2.1 表中的序号 1–26（pesdb 顺序）；不是客户端编号
    key: str                   # pesdb/eFHUB 页面键名
    en: str                    # 英式英语 UI 名（Konami 官网英式版）
    en_us: str                 # 美式英语 UI 名
    zh: str                    # 简中（国际服，取自 pesdb 简中界面；部分与官网简中对上）
    zh_hant: str
    ja: str
    categories: Tuple[str, ...]  # 所属培养类别 key
    gk: bool = False


_STAT_ROWS = [
    # id key                    UK                       US                     简中              繁中            日文                  类别
    (1, "offensive_awareness", "Attacking Awareness", "Offensive Awareness", "进攻意识", "進攻意識", "オフェンスセンス", ("dexterity",)),
    (2, "ball_control", "Ball Control", "Ball Control", "控球", "控球", "ボールコントロール", ("dribbling",)),
    (3, "dribbling", "Dribbling", "Dribbling", "盘球", "盤球", "ドリブル", ("dribbling",)),
    (4, "tight_possession", "Tight Possession", "Tight Possession", "紧密控球", "緊密控球", "ボールキープ", ("dribbling",)),
    (5, "low_pass", "Low Pass", "Low Pass", "地面传球", "地面傳球", "グラウンダーパス", ("passing",)),
    (6, "lofted_pass", "Lofted Pass", "Lofted Pass", "空中传球", "高吊傳球", "フライパス", ("passing",)),
    (7, "finishing", "Finishing", "Finishing", "射门", "射門", "決定力", ("shooting",)),
    (8, "heading", "Heading", "Heading", "头球", "頭球", "ヘディング", ("aerial_strength",)),
    (9, "set_piece_taking", "Set Piece Taking", "Set Pieces", "罚定位球", "定位球", "プレースキック", ("shooting",)),
    (10, "curl", "Curl", "Curl", "弧线球", "弧線球", "カーブ", ("shooting",)),
    (11, "defensive_awareness", "Defensive Awareness", "Defensive Awareness", "防守意识", "防守意識", "ディフェンスセンス", ("defending",)),
    (12, "tackling", "Tackling", "Tackling", "抢球", "搶截", "ボール奪取", ("defending",)),
    (13, "aggression", "Aggression", "Aggression", "积极性", "積極性", "アグレッシブネス", ("defending",)),
    (14, "defensive_engagement", "Defensive Engagement", "Defensive Engagement", "防守参与度", "防守參與", "守備意識", ("defending",)),
    (15, "gk_awareness", "GK Awareness", "GK Awareness", "守门员意识", "守門員意識", "GKセンス", ("gk_1",)),
    (16, "gk_catching", "GK Catching", "GK Catching", "守门员接球能力", "守門員接球能力", "キャッチング", ("gk_3",)),
    (17, "gk_parrying", "GK Parrying", "GK Deflecting", "守门员扑救", "守門員撲救能力", "クリアリング", ("gk_2",)),
    (18, "gk_reflexes", "GK Reflexes", "GK Reflexes", "守门员扑救反应", "守門員反應", "コラプシング", ("gk_3",)),
    (19, "gk_reach", "GK Reach", "GK Reach", "守门员臂展", "守門員臂展", "ディフレクティング", ("gk_2",)),
    (20, "speed", "Speed", "Speed", "速度", "速度", "スピード", ("lower_body_strength",)),
    (21, "acceleration", "Acceleration", "Acceleration", "加速", "加速", "瞬発力", ("dexterity",)),
    (22, "kicking_power", "Kicking Power", "Kicking Power", "脚下力量", "腳下力量", "キック力", ("lower_body_strength",)),
    (23, "jumping", "Jumping", "Jumping", "跳起", "跳躍", "ジャンプ", ("aerial_strength", "gk_1")),
    (24, "physical_contact", "Physical Contact", "Physical Contact", "身体接触", "身體對抗", "フィジカルコンタクト", ("aerial_strength",)),
    (25, "balance", "Balance", "Balance", "平衡", "平衡", "ボディコントロール", ("dexterity",)),
    (26, "stamina", "Stamina", "Stamina", "体力", "體力", "スタミナ", ("lower_body_strength",)),
]
STATS: Tuple[StatDef, ...] = tuple(
    StatDef(i, k, en, us, zh, zht, ja, cats, gk=k.startswith("gk_")) for (i, k, en, us, zh, zht, ja, cats) in _STAT_ROWS
)
STAT_KEYS: Tuple[str, ...] = tuple(s.key for s in STATS)
STAT_BY_KEY: Dict[str, StatDef] = {s.key: s for s in STATS}
GK_STAT_KEYS: Tuple[str, ...] = tuple(s.key for s in STATS if s.gk)
# 简中备注：Acceleration 在 Konami 官网简中版本说明里写作"加速度"（04 §2.1）。
STAT_ZH_ALIASES = {"acceleration": ("加速度",)}

STAT_MIN = 40          # RULES["STAT_RANGE"]
STAT_MAX = 99          # RULES["STAT_RANGE"] / RULES["TRAIN_CAP_99"]
BOOSTED_MAX = 120      # RULES["BOOST_CAP_120"]


def _norm(name: str) -> str:
    s = unicodedata.normalize("NFKC", str(name)).strip().lower()
    s = s.replace("’", "'").replace("‘", "'")
    s = re.sub(r"[\s_\-]+", " ", s)
    return s


def resolve_stat(name: str) -> str:
    """接受 key、英式/美式英文名、简中/繁中/日文名，返回 key；找不到抛 KeyError。"""
    n = _norm(name)
    for s in STATS:
        cands = {s.key, s.en, s.en_us, s.zh, s.zh_hant, s.ja, *STAT_ZH_ALIASES.get(s.key, ())}
        if n in {_norm(c) for c in cands}:
            return s.key
    raise KeyError(f"未知能力名：{name!r}")


# ---------------------------------------------------------------------------------------------
# 2. 位置、位置适性（04 §3.6）
# ---------------------------------------------------------------------------------------------

POSITIONS: Tuple[str, ...] = ("GK", "CB", "LB", "RB", "DMF", "CMF", "LMF", "RMF", "AMF", "LWF", "RWF", "SS", "CF")
POS_INDEX: Dict[str, int] = {p: i for i, p in enumerate(POSITIONS)}
# 左右同组（风格兼容判断时 LWF 与 RWF 视为同一位置，04 §3.6）
POSITION_PAIRS = {"LB": "RB", "RB": "LB", "LMF": "RMF", "RMF": "LMF", "LWF": "RWF", "RWF": "LWF"}
# 位置训练的相邻位置：04 只给出 CF 的相邻位置（SS / RWF / LWF，T1 [S08]）
ADJACENT_POSITIONS = {"CF": ("SS", "RWF", "LWF")}

PROFICIENCY_LEVELS: Tuple[str, ...] = ("high", "mid", "low")
# 官方颜色名 Bright green / Faded green（T1）；"无色 = 低"出自 efootballexplained（T3），见 RULES["POSITION_LEVELS"]
PROFICIENCY_ZH = {"high": "高（亮绿）", "mid": "中（淡绿）", "low": "低（无色，T3）"}
# 输入时接受的写法（旧稿用过"深绿/浅绿"，继续兼容）
PROFICIENCY_ALIASES = {"high": "high", "mid": "mid", "low": "low", "高": "high", "中": "mid", "低": "low",
                       "亮绿": "high", "淡绿": "mid", "无色": "low", "深绿": "high", "浅绿": "mid",
                       "bright green": "high", "faded green": "mid"}

# 非高适性位置的能力倍率（04 §3.6，T3 [S25]）：(淡绿, 无色)
_OOP_GROUPS = [
    (("dribbling", "ball_control", "finishing", "low_pass", "lofted_pass", "heading", "tackling",
      "set_piece_taking", "curl", "speed", "jumping"), 0.92, 0.80),
    (("offensive_awareness", "defensive_awareness", "kicking_power", "acceleration") + GK_STAT_KEYS, 0.94, 0.82),
    (("physical_contact", "balance", "stamina"), 0.96, 0.84),
    (("defensive_engagement", "tight_possession", "aggression"), 1.0, 1.0),  # 不受影响（逆足精度也不受影响）
]
OUT_OF_POSITION_FACTOR: Dict[str, Dict[str, float]] = {"high": {k: 1.0 for k in STAT_KEYS}, "mid": {}, "low": {}}
for _keys, _mid, _low in _OOP_GROUPS:
    for _k in _keys:
        OUT_OF_POSITION_FACTOR["mid"][_k] = _mid
        OUT_OF_POSITION_FACTOR["low"][_k] = _low


# ---------------------------------------------------------------------------------------------
# 3. 其他字段：惯用脚、逆足、状态波动、抗伤（04 §3.1–3.4）
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class EnumLevel:
    name: str        # 英式 UI
    zh: str          # 简中（国际服）
    level: int       # 1 起的档位（04 §3.2/3.4 逆向表的 level）；客户端编码未知
    note: str = ""


STRONGER_FOOT = {"Right": EnumLevel("Right", "右脚", 1), "Left": EnumLevel("Left", "左脚", 2)}

WEAK_FOOT_USAGE = {  # 04 §3.1/§3.2：传中/高球朝逆足侧偏 >15° 时允许用逆足的概率
    "Almost Never": EnumLevel("Almost Never", "几乎从不", 1, "逆足概率 0%"),
    "Rarely": EnumLevel("Rarely", "很少", 2, "逆足概率 33%"),
    "Occasionally": EnumLevel("Occasionally", "偶尔", 3, "逆足概率 67%"),
    "Regularly": EnumLevel("Regularly", "经常", 4, "逆足概率 100%；弧线射门等技能射门两只脚都可作候选"),
}
WEAK_FOOT_ACCURACY = {  # pesdb 页面数据 weakFootAccuracy = level − 1（0–3）
    "Low": EnumLevel("Low", "低", 1, "一般传射 −15，脚背大力 −25，Curl −24，逆足停球 −20，逆足射门速度上限 100 km/h"),
    "Medium": EnumLevel("Medium", "中", 2, "−11 / −17 / −17 / −13，107 km/h"),
    "High": EnumLevel("High", "高", 3, "−7 / −10 / −10 / −6，113 km/h"),
    "Very High": EnumLevel("Very High", "非常高", 4, "−3 / −3 / −3 / 0，120 km/h；Blitz Curler 可用逆足"),
}
FORM = {
    "Inconsistent": EnumLevel("Inconsistent", "不稳定", 1),
    "Standard": EnumLevel("Standard", "标准", 2, "官网简中作'普通'"),
    "Unwavering": EnumLevel("Unwavering", "稳定", 3),
}
# 04 §3.3：A–E 五档（pesdb 筛选项，T3）；Epic/Big Time/Legendary 固定 B（T3 [S29]，C30）。客户端编码未知
LIVE_UPDATE_RATING = {
    "A": EnumLevel("A", "A", 1, "条件型增能效果最大（T1 [S07]）"),
    "B": EnumLevel("B", "B", 2, "Epic/Big Time/Legendary 固定值（T3 [S29]）；Inconsistent 球员在 B 及以上时状态至少 Normal（T1 [S06]）"),
    "C": EnumLevel("C", "C", 3, "条件型增能 C 及以下不生效（T1 [S07]）"),
    "D": EnumLevel("D", "D", 4),
    "E": EnumLevel("E", "E", 5),
}
FIXED_LIVE_UPDATE_CARD_TYPES = ("Epic", "Big Time", "Legendary")
INJURY_RESISTANCE = {  # 04 §3.4：非接触伤病概率倍率 / 负荷累积倍率
    "Low": EnumLevel("Low", "低", 1, "非接触伤病 ×1.5，负荷累积 ×1，严重度减半 30%，升级 5%"),
    "Medium": EnumLevel("Medium", "中", 2, "×1.0，×0.8，50%，2%"),
    "High": EnumLevel("High", "高", 3, "×0.5，×0.5，70%，0"),
}

def resolve_enum(table: Mapping[str, EnumLevel], value) -> str:
    """接受英文名（不分大小写）、简中名或 1 起的档位整数。"""
    if isinstance(value, bool):
        raise KeyError(f"无效档位：{value!r}")
    if isinstance(value, int):
        for k, v in table.items():
            if v.level == value:
                return k
        raise KeyError(f"档位越界：{value!r}")
    n = _norm(value)
    for k, v in table.items():
        if n in (_norm(k), _norm(v.zh)):
            return k
    raise KeyError(f"未知取值：{value!r}（可选：{', '.join(table)}）")


def wfa_index(accuracy: str) -> int:
    """逆足精度 → pesdb 的 0–3 编码（总评公式用）。"""
    return WEAK_FOOT_ACCURACY[accuracy].level - 1


# ---------------------------------------------------------------------------------------------
# 4. 比赛风格（04 §3.7）
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class PlayingStyle:
    name: str
    en_us: str
    zh: str
    positions: Tuple[str, ...]   # 兼容位置（含左右同组展开）；空 = 无位置限制/Basic
    note: str = ""


def _expand(pos: Iterable[str]) -> Tuple[str, ...]:
    out: List[str] = []
    for p in pos:
        for q in p.split("/"):
            if q not in out:
                out.append(q)
            if q in POSITION_PAIRS and POSITION_PAIRS[q] not in out:
                out.append(POSITION_PAIRS[q])
    return tuple(sorted(out, key=POS_INDEX.__getitem__))


_ATT = [
    ("Basic", "Basic", "基础战术", ()),
    ("Goal Poacher", "Adv. Striker", "偷猎者", ("CF",)),
    ("Dummy Runner", "Dummy Runner", "虚假跑位", ("CF", "SS", "AMF")),
    ("Fox in the Box", "Poacher", "禁区之狐", ("CF",)),
    ("Prolific Winger", "Prolific Winger", "高产翼锋", ("LWF/RWF",)),
    ("Classic No. 10", "Classic No. 10", "传统十号位置", ("SS", "AMF")),
    ("Hole Player", "Hole Player", "影子前锋", ("SS", "AMF", "CMF", "LMF/RMF")),
    ("Box-to-Box", "Box-to-Box", "全能中场", ("DMF", "CMF", "LMF/RMF")),
    ("Anchor Man", "Anchor Man", "靠山", ("DMF",)),
    ("Extra Frontman", "Extra Frontman", "额外前锋", ("CB",)),
    ("Attacking Full-back", "Offensive Fullback", "进攻型后卫", ("LB/RB",)),
    ("Defensive Full-back", "Defensive Fullback", "防守型后卫", ("LB/RB",)),
    ("Deep-lying Forward", "Deep-Lying Forward", "组织型前锋", ("CF", "SS")),
    ("Creative Playmaker", "Creative Playmaker", "有创意的中场指挥官", ("SS", "LWF/RWF", "AMF", "LMF/RMF")),
    ("Build Up", "Build Up", "组织大师", ("CB",)),
    ("Roaming Flank", "Roaming Flank", "自由侧翼", ("LWF/RWF", "LMF/RMF")),
    ("Cross Specialist", "Cross Specialist", "传中专家", ("LWF/RWF", "LMF/RMF")),
    ("Orchestrator", "Orchestrator", "指挥官", ("DMF", "CMF")),
    ("Full-back Finisher", "Fullback Finisher", "迭瓦式终结者", ("LB/RB",)),
    ("Target Man", "Target Man", "站桩式中锋", ("CF",)),
    ("Build-up GK", "Build-up GK", "组织型门将", ("GK",)),
    ("High Line GK", "High Line GK", "高位防线门将", ("GK",)),
]
_DEF = [
    ("Basic", "Basic", "基础战术", ()),
    ("Box-to-Box", "Box-to-Box", "全能中场", ("DMF", "CMF", "LMF/RMF")),
    ("Anchor Man", "Anchor Man", "靠山", ("DMF",)),
    ("The Destroyer", "The Destroyer", "破坏型中场", ("CB", "LB/RB", "DMF", "CMF")),  # 代码还接受 LMF/RMF（04）
    ("Attacking GK", "Offensive GK", "进攻型守门员", ("GK",)),
    ("Defensive GK", "Defensive GK", "防守型守门员", ("GK",)),
    ("Press Back", "Press Back", "后撤压迫", ("CF", "SS", "LWF/RWF")),
    ("Front Line Pressure", "Front Line Pressure", "锋线施压", ("CF", "SS", "LWF/RWF")),
    ("Front Line Poacher", "Front Line Poacher", "前场埋伏者", ("CF", "SS", "LWF/RWF")),
    ("Attack Outlet", "Attack Outlet", "突前球员", ("CF", "SS", "LWF/RWF", "AMF")),
    ("All-action Defender", "All-action Defender", "全能后卫", ("DMF", "CMF", "LMF/RMF", "AMF")),
    ("Pass Disruptor", "Pass Disruptor", "传球拦截者", ("DMF", "CMF", "LMF/RMF", "AMF")),
    ("Covering Role", "Covering Role", "补位型球员", ("CB", "LB/RB", "DMF", "CMF")),
    ("High Line Master", "High Line Master", "高位防线大师", ("CB", "LB/RB")),
    ("Tough Marker", "Tough Marker", "紧密盯防者", ("CB", "LB/RB", "DMF")),
    ("Deep Defender", "Deep Defender", "纵深防守者", ("CB", "LB/RB")),
    ("Sweeper GK", "Sweeper GK", "清道夫门将", ("GK",)),
]
_AI = [
    ("Trickster", "Trickster", "魔术师", (), "电脑多一种花式动作，更常尝试花式"),
    ("Mazing Run", "Agility King", "惊人突破", (), "电脑更多盘带"),
    ("Speeding Bullet", "Speeding Bullet", "疾速子弹", (), "电脑更多直线带球推进"),
    ("Incisive Run", "Incisive Run", "内切突破", (), "电脑可选弧线射门选项（起始概率 35%）"),
    ("Long Ball Expert", "Long Ball Expert", "长球专家", (), "电脑更多长传、更少盘带"),
    ("Early Cross", "Early Cross", "提前传中", (), "电脑传中分数 +30"),
    ("Long Ranger", "Long Ranger", "重炮手", (), "电脑远射分数 +60"),
]
ATTACKING_STYLES: Dict[str, PlayingStyle] = {n: PlayingStyle(n, us, zh, _expand(p)) for n, us, zh, p in _ATT}
DEFENSIVE_STYLES: Dict[str, PlayingStyle] = {n: PlayingStyle(n, us, zh, _expand(p)) for n, us, zh, p in _DEF}
AI_STYLES: Dict[str, PlayingStyle] = {n: PlayingStyle(n, us, zh, p, note) for n, us, zh, p, note in _AI}
MAX_AI_STYLES = 3   # RULES["AI_STYLE_MAX"]


def resolve_style(table: Mapping[str, PlayingStyle], name: str) -> str:
    n = _norm(name)
    for k, v in table.items():
        if n in (_norm(k), _norm(v.en_us), _norm(v.zh)):
            return k
    raise KeyError(f"未知比赛风格：{name!r}")


def style_active_positions(style: PlayingStyle, usable_positions: Iterable[str]) -> Tuple[str, ...]:
    """风格在哪些位置生效 = 兼容位置 ∩ 球员可用位置（Basic 返回空）。"""
    usable = set(usable_positions)
    return tuple(p for p in style.positions if p in usable)


# ---------------------------------------------------------------------------------------------
# 5. 技能（04 §4）
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Skill:
    no: int                  # 04 §4.2 表中序号
    name: str                # 英式
    en_us: str
    zh: str                  # 简中（国际服）；网易《实况足球Online》PC 端游的写法放 note（不是手游国服，04 C27）
    group: str               # dribbling/shooting/passing/defending/goalkeeping/general/special
    special: bool            # True = 不可训练（只能写在卡面）
    effect: str
    stat_bonus: Tuple[Tuple[str, int], ...] = ()   # 直接加能力值的情境加成（比赛中、99 封顶前，RULES SKILL_BONUS_BEFORE_CAP）
    bonus_max: int = 0                              # 叠加型的最大值（0 = 固定值）
    condition: str = ""
    note: str = ""
    aim_bonus: Tuple[Tuple[str, int], ...] = ()    # "按能力 +N 瞄准/计算"：不改能力值，不受 99 限（RULES SKILL_AIM_UNCAPPED）
    aim_condition: str = ""


_SKILL_ROWS = [
    # no, UK, US, 简中, group, special, effect, bonus, bonus_max, condition, note
    (1, "Scissors Feint", "", "剪刀脚假动作", "dribbling", False, "专属快速剪刀脚，同等 Dribbling 下快约 6–7%"),
    (2, "Double Touch", "", "两次触球", "dribbling", False, "专属快速两次触球"),
    (3, "Flip Flap", "", "牛摆尾", "dribbling", False, "摇杆方向稳定触发牛尾巴", (), 0, "", "网易 PC 端游写作：牛尾巴"),
    (4, "Marseille Turn", "", "马赛回旋", "dribbling", False, "专属马赛回旋，快约 20%"),
    (5, "Sombrero", "", "挑球过顶", "dribbling", False, "挑球过人成功率提高"),
    (6, "Chop Turn", "", "脚后跟磕球变向", "dribbling", False, "快约 16–18%"),
    (7, "Cut Behind & Turn", "", "向后切球并转身", "dribbling", False, "解锁 Cut Behind 与专属停球转身"),
    (8, "Scotch Move", "Inside Bounce", "磕球过人", "dribbling", False, "解锁 Scotch Move"),
    (9, "Tap Trick", "", "后脚磕球变向", "special", True, "解锁 Tap Trick", (), 0, "", "2026 新技能，'特殊'为本组推断（T4）"),
    (10, "Sole Control", "", "足底控球", "dribbling", False, "基础切球计时按 Dribbling +20"),
    (11, "Momentum Dribbling", "", "旋风盘带", "special", True, "持球进入对方半场约 18 m 以上区域时四项 +6",
     (("dribbling", 6), ("tight_possession", 6), ("speed", 6), ("acceleration", 6)), 0, "持球进入对方半场约 18 m 以上区域"),
    (12, "Acceleration Burst", "", "瞬间加速", "special", True, "慢速带球可做快速 Sharp Touch；不直接加 Speed/Acceleration"),
    (13, "Magnetic Feet", "", "磁力脚", "special", True, "5 m 内每名对手四项 +2，最多 +8",
     (("dribbling", 2), ("tight_possession", 2), ("physical_contact", 2), ("balance", 2)), 8, "持球时 5 m 内每有 1 名对手（最多 4 人）"),
    (14, "Heading", "", "头球", "general", False, "头球射门按 Heading +5 瞄准；不增加跳跃或争顶"),
    (15, "Bullet Header", "", "炮弹式头球", "special", True, "头球射门速度上限 +4 km/h"),
    (16, "Long-range Curler", "Long-Range Curler", "远距离弧线球", "shooting", False, "弧线射门按 Finishing +5"),
    (17, "Blitz Curler", "", "弧线落叶射门", "special", True, "力量 ≥50% 的 Controlled Shot 变强上旋弧线；只用强势脚（逆足精度 Very High 例外）"),
    (18, "Chip Shot Control", "", "吊射控制", "shooting", False, "吊射按 Finishing +5"),
    (19, "Knuckle Shot", "", "落叶球射门", "shooting", False, "电梯球，Finishing +5"),
    (20, "Dipping Shot", "", "急坠射门", "shooting", False, "下坠球，Finishing +5"),
    (21, "Rising Shot", "", "急升射门", "shooting", False, "上升球，Finishing +5"),
    (22, "Long-range Shooting", "Long-Range Shooting", "远射", "shooting", False, "禁区外脚背射门按 Finishing +5 瞄准"),
    (23, "Low Screamer", "", "掠地瞬击", "special", True, "力量 <50% 的强势脚 Stunning Shot 约快 3–15 km/h"),
    (24, "Snap Strike", "", "瞬发射门", "special", True, "Stunning Shot 出脚提前 0.07–0.11 s", (), 0, "", "2026 新技能，'特殊'为 T3 新闻片段"),
    (25, "Acrobatic Finishing", "", "杂技般进球", "shooting", False, "杂技射门按 Finishing +5 计算；不改能力值，不受 99 封顶（C21）"),
    (26, "Heel Trick", "", "脚跟绝技", "passing", False, "解锁脚跟传球/近距离脚跟射门"),
    (27, "First-time Shot", "One-time Shot", "一脚射门", "shooting", False, "一脚射门来球速度按 90% 计"),
    (28, "Phenomenal Finishing", "", "无定型射门", "special", True, "别扭姿势射门按 Finishing +7 瞄准，头球也适用；不改能力值，不受 99 封顶（C21）"),
    (29, "Willpower", "", "百折不撓", "special", True, "每射门一次本场 Finishing、Kicking Power +1，最多 +8",
     (("finishing", 1), ("kicking_power", 1)), 8, "每射门一次 +1（最多 8 次）", "网易 PC 端游写作：百折不挠"),
    (30, "One-touch Pass", "", "一脚传球", "passing", False, "来球按慢 10% 计"),
    (31, "Through Passing", "", "直传球", "passing", False, "直塞按 Low/Lofted Pass +5 瞄准"),
    (32, "Weighted Pass", "", "精准长传", "passing", False, "45 m 以上前向长传专属动作"),
    (33, "Pinpoint Crossing", "", "精确横传球", "passing", False, "传中 Lofted Pass +5、Curl +5（仅传中时）"),
    (34, "Edged Crossing", "", "急坠传中", "special", True, "前旋下坠传中", (), 0, "", "网易 PC 端游写作：落叶式横传"),
    (35, "Outside Curler", "", "外脚背弧线球", "passing", False, "外脚背弧线传球与射门"),
    (36, "Rabona", "", "插花脚", "passing", False, "解锁插花脚"),
    (37, "No Look Pass", "", "不看球员传球", "passing", False, "解锁不看人传球"),
    (38, "Game-changing Pass", "", "翻盘传球", "special", True, "Low Pass、Lofted Pass +6",
     (("low_pass", 6), ("lofted_pass", 6)), 0, "下半场开球后己方平局或落后"),
    (39, "Visionary Pass", "", "到位传球", "special", True, "接球队友 +6（作用于队友，不加自身）"),
    (40, "Phenomenal Pass", "", "无定型传球", "special", True, "别扭姿势传球能力 +6（动作用）"),
    (41, "Low Lofted Pass", "", "低空传球", "passing", False, "长高球弹道降低"),
    (42, "GK Low Punt", "", "守门员低弹道凌空球", "goalkeeping", False, "低平大脚"),
    (43, "GK High Punt", "", "守门员高弹道凌空球", "goalkeeping", False, "高大脚约 60 m"),
    (44, "Long Throw", "Long Throws", "长距离投掷", "general", False, "界外球 32 m"),
    (45, "GK Long Throw", "GK Long Throws", "守门员长距离投掷", "goalkeeping", False, "手抛球 50 m"),
    (46, "Penalty Specialist", "", "点球专家", "shooting", False, "说明文本与逆向冲突（04 U15）"),
    (47, "GK Penalty Saver", "", "守门员扑点球", "goalkeeping", False, "点球扑救评分 +5"),
    (48, "GK Directing Defence", "GK Directing Defense", "守门员指挥防守", "special", True, "本方后卫 +2（作用于队友）"),
    (49, "GK Spirit Roar", "", "门神战吼", "special", True, "本方后卫 +2（作用于队友）"),
    (50, "Gamesmanship", "", "假摔", "general", False, "更容易造犯规"),
    (51, "Man Marking", "", "盯人", "defending", False, "盯人站位按 Defensive Awareness +5"),
    (52, "Track Back", "", "压迫", "defending", False, "丢球后 5 s 内持续逼抢"),
    (53, "Interception", "", "截球", "defending", False, "反应延迟降到 60%"),
    (54, "Blocker", "", "封堵", "defending", False, "封堵按 Defensive Awareness +5"),
    (55, "Aerial Superiority", "", "空中优势", "defending", False, "争顶身体对抗比较值 +5", (), 0, "",
     "按 [S34] 归入防守组（04 核查记录 C22：防守 7、通用 6）"),
    (56, "Sliding Tackle", "", "飞身铲球", "defending", False, "铲球按 Tackling +5"),
    (57, "Long-reach Tackle", "", "伸脚抢截", "special", True, "额外站立抢断动作", (), 0, "", "网易 PC 端游写作：伸腿抢截"),
    (58, "Power Tackle", "", "强力抢断", "special", True, "两种身体对抗式抢断", (), 0, "", "2026 新技能，'特殊'为 T3 新闻片段"),
    (59, "Fortress", "", "后防领袖", "special", True, "防守四项 +6",
     (("defensive_awareness", 6), ("tackling", 6), ("aggression", 6), ("defensive_engagement", 6)), 0, "下半场起己方领先"),
    (60, "Acrobatic Clearance", "", "杂技般解围", "defending", False, "杂技解围动作"),
    (61, "Aerial Fort", "", "空中堡垒", "special", True, "Physical Contact、Jumping +4",
     (("physical_contact", 4), ("jumping", 4)), 0, "本方禁区内高球情形"),
    (62, "Shadow Hunt", "", "紧急回防", "special", True, "Speed、Acceleration +3", (("speed", 3), ("acceleration", 3)), 0,
     "担任 CB/边后卫/DMF 回追身后球", "2026 新技能，'特殊'为本组推断（T4）"),
    (63, "Captaincy", "", "队长", "general", False, "说明文本与逆向冲突（04 U15）"),
    (64, "Attack Trigger", "", "进攻号手", "special", True, "其持球时其他队友 Attacking Awareness +3（作用于队友）"),
    (65, "Super-sub", "Super-Sub", "超级候补", "general", False, "半场或之后替补登场时 25 项能力 +1（具体哪 25 项未列出，本工具不计算）"),
    (66, "Fighting Spirit", "", "战斗精神", "general", False, "受压迫时精度损失减少 20%"),
    (67, "Attacking Surge", "", "进攻提速", "special", True, "Speed、Acceleration +3", (("speed", 3), ("acceleration", 3)), 0,
     "在对方半场且队友持球（自己拿球即失效）"),
]


# "按某能力 +N 瞄准/计算"的技能（04 §4.2 效果列；RULES["SKILL_AIM_UNCAPPED"]）：不改能力值，不受 99 封顶。
# Through Passing 的"可超过 99"有原文（T3）；其余为同一机制的推断（T4），待客户端核实。
_SKILL_AIM = {
    'Heading': ((('heading', 5),), '头球射门'),
    'Long-range Curler': ((('finishing', 5),), '弧线射门'),
    'Chip Shot Control': ((('finishing', 5),), '吊射'),
    'Knuckle Shot': ((('finishing', 5),), '力量 50–65% 的 Stunning Shot 电梯球'),
    'Dipping Shot': ((('finishing', 5),), '力量 20–50% 的强势脚 Stunning Shot'),
    'Rising Shot': ((('finishing', 5),), '力量 65–95% 的强势脚 Stunning Shot'),
    'Long-range Shooting': ((('finishing', 5),), '禁区外脚背射门'),
    'Acrobatic Finishing': ((('finishing', 5),), '杂技射门'),
    'Phenomenal Finishing': ((('finishing', 7), ('heading', 7)), '别扭姿势射门（含头球）'),
    'Through Passing': ((('low_pass', 5), ('lofted_pass', 5)), '直塞'),
    'Pinpoint Crossing': ((('lofted_pass', 5), ('curl', 5)), '传中'),
    'Man Marking': ((('defensive_awareness', 5),), '电脑盯人时的站位更新'),
    'Blocker': ((('defensive_awareness', 5),), '封堵'),
    'Sliding Tackle': ((('tackling', 5),), '铲球'),
    'Interception': ((('defensive_engagement', 5),), '三项电脑防守决策'),
    'Aerial Superiority': ((('physical_contact', 5),), '争顶时的身体对抗比较'),
}


def _mk_skill(row) -> Skill:
    row = list(row) + [None] * (11 - len(row))
    no, name, us, zh, group, special, effect, bonus, bmax, cond, note = row
    aim, aim_cond = _SKILL_AIM.get(name, ((), ""))
    return Skill(no, name, us or name, zh, group, bool(special), effect, tuple(bonus or ()), int(bmax or 0),
                 cond or "", note or "", tuple(aim), aim_cond)


SKILLS: Dict[str, Skill] = {r[1]: _mk_skill(r) for r in _SKILL_ROWS}
DUMMY_SKILLS: Tuple[str, ...] = ("Spring Cut", "Thames", "Nile", "Amazon", "Mekong", "Congo", "Indus")
MAX_CARD_SKILLS = 10        # RULES["SKILL_CARD_MAX"]（实测，硬限制按 10）
MAX_ADDITIONAL_SKILLS = 5   # RULES["SKILL_ADDITIONAL_MAX"]（T1）
TRAINABLE_GROUPS = ("dribbling", "shooting", "passing", "defending", "goalkeeping", "general")
# 只用于名称解析。这些写法来自网易《实况足球Online》PC 端游官网（04 核查记录 C27），不是手游国服或客户端名称
_SKILL_ZH_ALIASES = {"Willpower": ("百折不挠",), "Flip Flap": ("牛尾巴",), "Edged Crossing": ("落叶式横传",),
                     "Long-reach Tackle": ("伸腿抢截",), "Momentum Dribbling": ("旋风盘球",)}


def resolve_skill(name: str) -> str:
    n = _norm(name)
    for k, v in SKILLS.items():
        if n in {_norm(k), _norm(v.en_us), _norm(v.zh), *(_norm(a) for a in _SKILL_ZH_ALIASES.get(k, ()))}:
            return k
    if n in {_norm(d) for d in DUMMY_SKILLS}:
        raise KeyError(f"{name!r} 是 11.1.1 代码中的未发布占位技能（Dummy），不得使用")
    raise KeyError(f"未知技能：{name!r}（客户端是否存在无法确认）")


# ---------------------------------------------------------------------------------------------
# 6. 培养（04 §5）
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class TrainingCategory:
    key: str
    en: str
    zh: str
    stats: Tuple[str, ...]
    gk_only: bool = False


# 顺序 = pesdb player.js 中 categories 的顺序（贪心并列时取靠前者，复现 pesdb 结果时必须保持）
TRAINING_CATEGORIES: Dict[str, TrainingCategory] = {c.key: c for c in (
    TrainingCategory("shooting", "Shooting", "射门", ("finishing", "set_piece_taking", "curl")),
    TrainingCategory("passing", "Passing", "传球", ("low_pass", "lofted_pass")),
    TrainingCategory("dribbling", "Dribbling", "盘球", ("ball_control", "dribbling", "tight_possession")),
    TrainingCategory("dexterity", "Dexterity", "灵巧", ("offensive_awareness", "acceleration", "balance")),
    TrainingCategory("defending", "Defending", "防守", ("defensive_awareness", "tackling", "aggression", "defensive_engagement")),
    TrainingCategory("aerial_strength", "Aerial Strength", "空中力量", ("heading", "jumping", "physical_contact")),
    TrainingCategory("lower_body_strength", "Lower Body Strength", "下肢力量", ("speed", "kicking_power", "stamina")),
    TrainingCategory("gk_1", "GK 1", "GK 1", ("jumping", "gk_awareness"), gk_only=True),
    TrainingCategory("gk_2", "GK 2", "GK 2", ("gk_parrying", "gk_reach"), gk_only=True),
    TrainingCategory("gk_3", "GK 3", "GK 3", ("gk_catching", "gk_reflexes"), gk_only=True),
)}
CATEGORY_KEYS: Tuple[str, ...] = tuple(TRAINING_CATEGORIES)


def resolve_category(name: str) -> str:
    n = _norm(name)
    for k, c in TRAINING_CATEGORIES.items():
        if n in (_norm(k), _norm(c.en), _norm(c.zh)) or (k == "lower_body_strength" and n == "lower body"):
            return k
    raise KeyError(f"未知培养类别：{name!r}")


def categories_for_position(position: str) -> Tuple[str, ...]:
    """pesdb：GK 1–3 只在门将位置启用（RULES['GK_CATEGORIES']，待客户端核实）。"""
    return tuple(k for k, c in TRAINING_CATEGORIES.items() if position == "GK" or not c.gk_only)


def progression_points(max_level: int) -> int:
    """可用能力点 = 2 × (最高等级 − 1)（RULES['POINTS']）。"""
    if max_level < 1:
        raise ValueError("max_level 必须 ≥ 1")
    return 2 * (max_level - 1)


def step_cost(n: int) -> int:
    """同一类别第 n 次（n 从 1 起）加点的花费 ⌈n/4⌉（RULES['STEP_COST']）。"""
    if n < 1:
        raise ValueError("n 从 1 起")
    return -(-n // 4)


def cumulative_cost(n: int) -> int:
    """同一类别前 n 次加点的累计花费 (g+1)(2g+r)，g=⌊n/4⌋，r=n mod 4（pesdb 原式）。"""
    if n < 0:
        raise ValueError("n ≥ 0")
    g, r = divmod(n, 4)
    return (g + 1) * (2 * g + r)


def allocation_cost(alloc: Mapping[str, int]) -> int:
    return sum(cumulative_cost(int(v)) for v in alloc.values())


def apply_training(base: Mapping[str, int], alloc: Mapping[str, int]) -> Dict[str, int]:
    """卡面值 + 加点次数，每项封顶 99（RULES['TRAIN_CAP_99']）。Jumping 同时吃 aerial_strength 与 gk_1。"""
    out = {k: int(base[k]) for k in STAT_KEYS}
    add = {k: 0 for k in STAT_KEYS}
    for cat, n in alloc.items():
        for s in TRAINING_CATEGORIES[cat].stats:
            add[s] += int(n)
    for k in STAT_KEYS:
        out[k] = min(STAT_MAX, out[k] + add[k])
    return out


# ---------------------------------------------------------------------------------------------
# 7. 增能（04 §6）
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class BoosterDef:
    name: str
    pesdb_id: Optional[int]     # pesdb 页面数据中的增能 id（不是客户端 id）
    zh: str                     # 官网简中名；"" = 未找到
    stats: Tuple[str, ...]
    kind: str                   # regular / special / single


_BOOSTER_ROWS = [
    (1, "Shooting", "攻门", ("ball_control", "finishing", "kicking_power", "physical_contact")),
    (2, "Free-kick Taking", "罚任意球", ("finishing", "set_piece_taking", "curl", "kicking_power")),
    (3, "Aerial", "空中对抗", ("finishing", "heading", "jumping", "physical_contact")),
    (4, "Passing", "传球", ("low_pass", "lofted_pass", "curl", "kicking_power")),
    (5, "Ball-carrying", "带球", ("dribbling", "tight_possession", "speed", "balance")),
    (6, "Technique", "技术", ("ball_control", "dribbling", "tight_possession", "low_pass")),
    (7, "Defending", "防守", ("acceleration", "jumping", "defensive_awareness", "tackling")),
    (8, "Duelling", "1对1", ("speed", "stamina", "defensive_awareness", "tackling")),
    (9, "Agility", "敏捷", ("speed", "acceleration", "balance", "stamina")),
    (10, "Physicality", "身体素质", ("jumping", "physical_contact", "balance", "stamina")),
    (11, "Goalkeeping", "守门", ("gk_awareness", "gk_catching", "gk_parrying", "gk_reflexes")),
    (64, "Striker's Instinct", "射手本能", ("offensive_awareness", "ball_control", "finishing", "acceleration")),
    (65, "Shutdown", "一夫当关", ("speed", "defensive_awareness", "tackling", "defensive_engagement")),
    (66, "Hard Worker", "防守铁人", ("acceleration", "physical_contact", "stamina", "aggression")),
    (67, "Saving", "固若金汤", ("gk_awareness", "gk_parrying", "gk_reflexes", "gk_reach")),
    (68, "Crossing", "传球大师", ("lofted_pass", "curl", "speed", "stamina")),
    (69, "Fantasista", "球场幻想家", ("ball_control", "dribbling", "finishing", "balance")),
    (172, "Regista", "", ("tight_possession", "low_pass", "defensive_awareness", "tackling")),
    (173, "Rebuilding", "", ("low_pass", "defensive_awareness", "aggression", "defensive_engagement")),
    (174, "Accuracy", "精准", ("low_pass", "lofted_pass", "finishing", "kicking_power")),
    (175, "Offence Creator", "", ("offensive_awareness", "ball_control", "low_pass", "kicking_power")),
    (176, "Ball Protection", "", ("ball_control", "tight_possession", "physical_contact", "balance")),
    (177, "Balancer", "", ("offensive_awareness", "acceleration", "stamina", "defensive_awareness")),
    (178, "Counter", "", ("low_pass", "physical_contact", "tackling", "defensive_engagement")),
    (179, "Aerial Block", "", ("heading", "jumping", "physical_contact", "defensive_awareness")),
    (180, "Breakthrough", "", ("dribbling", "speed", "kicking_power", "physical_contact")),
    (181, "Strength", "强壮", ("speed", "kicking_power", "jumping", "physical_contact")),
    (182, "Off the Ball", "", ("offensive_awareness", "speed", "acceleration", "stamina")),
    (183, "Stealing", "", ("acceleration", "physical_contact", "tackling", "aggression")),
]
REGULAR_BOOSTERS: Dict[str, BoosterDef] = {n: BoosterDef(n, i, zh, st, "regular") for i, n, zh, st in _BOOSTER_ROWS}
SPECIAL_BOOSTERS: Dict[str, Optional[BoosterDef]] = {
    "Total Package": BoosterDef("Total Package", 101, "", STAT_KEYS, "special"),   # +3 = 26 项全部 +3（T3）
    # 以下能力组合未查证（04 §6.3、U8）：使用时必须在输入里显式给出 stats
    "Bearer of Fate": None, "King of Football": None, "Le Petit Prince": None, "Magical": None,
    "Natural-born": None, "Son of God": None, "Striking": None, "The Undisputed": None,
}
MAX_BOOSTER_SLOTS = 2               # RULES["BOOSTER_SLOTS"]
CRAFTED_BOOSTER_VALUE = 1           # RULES["BOOSTER_CRAFTED"]
FIXED_REGULAR_RANGE = (3, 6)        # RULES["BOOSTER_FIXED_RANGE"]：4 项型抽样所见
FIXED_SINGLE_VALUES = (6, 8)        # 单项型抽样所见
FIXED_SPECIAL_VALUES = {"Total Package": (3,)}


def parse_booster_label(label: str) -> Tuple[str, Optional[int]]:
    """'Striker's Instinct +4' → ("Striker's Instinct", 4)。"""
    m = re.match(r"^(.*?)\s*\+\s*(\d+)\s*$", str(label))
    if m:
        return m.group(1).strip(), int(m.group(2))
    return str(label).strip(), None


def resolve_booster(name: str) -> Tuple[str, str]:
    """返回 (规范名, kind)；kind ∈ regular/special/single。单项型以能力名命名（如 'Kicking Power'）。"""
    n = _norm(name)
    for k, b in REGULAR_BOOSTERS.items():
        if n in (_norm(k), _norm(b.zh) if b.zh else "\0"):
            return k, "regular"
    for k in SPECIAL_BOOSTERS:
        if n == _norm(k):
            return k, "special"
    try:
        return resolve_stat(name), "single"
    except KeyError:
        pass
    raise KeyError(f"未知增能：{name!r}")


def booster_stats(name: str, kind: str, explicit: Optional[Sequence[str]] = None) -> Tuple[str, ...]:
    if kind == "regular":
        return REGULAR_BOOSTERS[name].stats
    if kind == "single":
        return (name,)
    d = SPECIAL_BOOSTERS.get(name)
    if d is not None:
        return d.stats
    if explicit:
        return tuple(resolve_stat(s) for s in explicit)
    raise KeyError(f"专属增能 {name!r} 的能力组合未查证，必须在输入中显式给出 stats")


def apply_boosters(stats: Mapping[str, int], bonus: Mapping[str, int]) -> Dict[str, int]:
    """增能在 99 封顶之后相加、不缩放，上限 120（RULES BOOST_AFTER_CAP / BOOST_CAP_120）。"""
    return {k: min(BOOSTED_MAX, int(stats[k]) + int(bonus.get(k, 0))) for k in STAT_KEYS}


def sum_bonus(*bonuses: Mapping[str, int]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for b in bonuses:
        for k, v in b.items():
            out[k] = out.get(k, 0) + int(v)
    return out


# ---------------------------------------------------------------------------------------------
# 8. 主教练（04 §6.5）
# ---------------------------------------------------------------------------------------------

TEAM_PLAYSTYLES = ("Possession Game", "Quick Counter", "Long Ball Counter", "Long Ball", "Out Wide", "Overload")

# pesdb player-progression-data 中的 proficiencyThresholds（p=70–90），ef-labo 给出相同分段（T3）
PESDB_MANAGER_THRESHOLDS: Dict[int, Tuple[int, ...]] = {
    70: (), 71: (), 72: (86,), 73: (72,), 74: (64,), 75: (57,), 76: (52,), 77: (48, 95), 78: (44, 88),
    79: (41, 82), 80: (40, 77), 81: (40, 74), 82: (40, 69), 83: (40, 67), 84: (40, 63, 94), 85: (40, 62, 92),
    86: (40, 60, 89), 87: (40, 59, 88), 88: (40, 57, 85), 89: (40, 56, 84), 90: (40, 55, 83),
}


def manager_multiplier(p: float) -> float:
    """efootballexplained（11.1.1 逆向）的熟练度倍率（RULES['MANAGER_FORMULA']）。"""
    if p < 90:
        return (104 - 38 * (p - 99) ** 2 / 99 ** 2) / 100
    return (99.253 + 0.0485 * p) / 100


def manager_delta(value: int, p: int, model: str = "efx", position_factor: float = 1.0) -> int:
    """主教练（及位置适性）引起的能力增量。

    model="efx"：trunc((位置倍率 × 主教练倍率 − 1) × 卡面值)，向零取整（04 §6.5/§6.6）。
    model="pesdb"：门槛表，只覆盖 p=70–90，且不与位置倍率相乘（pesdb 只做卡面计算）。
    """
    if model == "efx":
        return math.trunc((position_factor * manager_multiplier(p) - 1.0) * value)
    if model == "pesdb":
        if position_factor != 1.0:
            raise ValueError("pesdb 门槛表不含位置倍率")
        lo, hi = min(PESDB_MANAGER_THRESHOLDS), max(PESDB_MANAGER_THRESHOLDS)
        pp = min(hi, max(lo, int(p)))
        return sum(1 for t in PESDB_MANAGER_THRESHOLDS[pp] if value >= t)
    raise ValueError(f"未知主教练模型 {model!r}")


def apply_manager(card: Mapping[str, int], p: Optional[int], model: str = "efx") -> Dict[str, int]:
    """把主教练熟练度作用到（培养后的）卡面值上，结果夹在 [40, 99]；增能需之后再加。"""
    if p is None:
        return {k: int(card[k]) for k in STAT_KEYS}
    return {k: max(STAT_MIN, min(STAT_MAX, int(card[k]) + manager_delta(int(card[k]), p, model))) for k in STAT_KEYS}


def manager_threshold_mismatches() -> List[Tuple[int, Tuple[int, ...], Tuple[int, ...]]]:
    """列出 efx 公式与 pesdb 门槛表不一致的熟练度值：[(p, 公式推出的门槛, pesdb 门槛)]。"""
    out = []
    for p, thr in PESDB_MANAGER_THRESHOLDS.items():
        derived, prev = [], 0
        for v in range(STAT_MIN, STAT_MAX + 1):
            d = manager_delta(v, p, "efx")
            if d > prev:
                derived.append(v)
                prev = d
        if tuple(derived) != thr:
            out.append((p, tuple(derived), thr))
    return out


# ---------------------------------------------------------------------------------------------
# 9. 总评（04 §7）
# ---------------------------------------------------------------------------------------------

# 列顺序 = POSITIONS（GK CB LB RB DMF CMF LMF RMF AMF LWF RWF SS CF），原样摘自 04 §7.2
HEIGHT_WEIGHTS: Tuple[int, ...] = (186, 136, 49, 49, 61, 37, 12, 12, 37, 49, 49, 62, 99)
RATING_WEIGHTS: Dict[str, Tuple[int, ...]] = {
    "offensive_awareness":  (0, 14, 61, 61, 61, 98, 98, 98, 171, 159, 159, 173, 210),
    "ball_control":         (13, 27, 86, 86, 122, 171, 171, 171, 196, 159, 159, 210, 123),
    "dribbling":            (0, 14, 61, 61, 37, 98, 122, 110, 122, 159, 159, 123, 62),
    "tight_possession":     (0, 0, 37, 37, 24, 49, 61, 73, 73, 86, 86, 86, 37),
    "low_pass":             (27, 41, 61, 61, 122, 208, 135, 135, 196, 73, 73, 99, 37),
    "lofted_pass":          (40, 68, 147, 147, 122, 159, 196, 196, 159, 98, 98, 74, 12),
    "finishing":            (0, 27, 24, 24, 37, 73, 86, 86, 184, 159, 159, 284, 358),
    "heading":              (0, 55, 24, 24, 61, 24, 12, 12, 24, 24, 24, 25, 62),
    "set_piece_taking":     (0, 14, 24, 24, 12, 12, 24, 24, 12, 12, 12, 12, 12),
    "curl":                 (0, 14, 24, 24, 12, 12, 24, 24, 12, 12, 12, 12, 12),
    "speed":                (13, 136, 220, 220, 61, 61, 196, 196, 98, 220, 220, 86, 99),
    "acceleration":         (40, 150, 184, 184, 61, 86, 159, 159, 86, 159, 159, 99, 123),
    "kicking_power":        (53, 27, 24, 24, 49, 73, 24, 24, 73, 61, 61, 99, 123),
    "jumping":              (133, 109, 37, 37, 37, 12, 12, 12, 12, 24, 24, 37, 62),
    "physical_contact":     (80, 204, 98, 98, 122, 49, 24, 24, 24, 37, 37, 37, 86),
    "balance":              (0, 0, 24, 24, 12, 24, 61, 61, 24, 73, 73, 74, 86),
    "stamina":              (0, 68, 196, 196, 196, 196, 147, 147, 86, 49, 49, 49, 37),
    "defensive_awareness":  (13, 286, 147, 147, 220, 86, 49, 49, 24, 12, 12, 0, 0),
    "tackling":             (0, 191, 86, 86, 122, 86, 24, 24, 24, 12, 12, 12, 12),
    "aggression":           (0, 82, 37, 37, 98, 37, 12, 12, 12, 12, 12, 12, 12),
    "defensive_engagement": (0, 14, 24, 24, 24, 24, 24, 24, 24, 24, 24, 12, 12),
    "gk_awareness":         (279, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    "gk_catching":          (226, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    "gk_parrying":          (173, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    "gk_reflexes":          (173, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    "gk_reach":             (226, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
}
# 04 §7.2 末行"能力权重合计（不含身高）"，用于自检
RATING_WEIGHT_TOTALS: Tuple[int, ...] = (1489, 1541, 1626, 1626, 1612, 1638, 1661, 1661, 1636, 1624, 1624, 1615, 1577)


def weak_foot_rating(wfa_idx: int) -> int:
    """WFR = floor(59 × WFA / 3 + 40)，WFA = 0–3 → 40 / 59 / 79 / 99。"""
    return math.floor(59 * wfa_idx / 3 + 40)


def rating_total(stats: Mapping[str, int], height_cm: int, wfa_idx: int, position: str) -> int:
    """总评分子（整数，未除以 1000）。"""
    i = POS_INDEX[position]
    t = (int(height_cm) - 136) * HEIGHT_WEIGHTS[i]
    for k in STAT_KEYS:
        t += (int(stats[k]) - 25) * RATING_WEIGHTS[k][i]
    t += (weak_foot_rating(wfa_idx) - 25) * 4 + 500
    return t


def raw_overall(stats: Mapping[str, int], height_cm: int, wfa_idx: int, position: str) -> float:
    return rating_total(stats, height_cm, wfa_idx, position) / 1000


def overall(stats: Mapping[str, int], height_cm: int, wfa_idx: int, position: str) -> int:
    """总评 = floor(raw)（RULES['RATING_FORMULA']）。分子为整数，用整除避免浮点误差。"""
    return rating_total(stats, height_cm, wfa_idx, position) // 1000


def overall_all_positions(stats: Mapping[str, int], height_cm: int, wfa_idx: int) -> Dict[str, int]:
    return {p: overall(stats, height_cm, wfa_idx, p) for p in POSITIONS}


# ---------------------------------------------------------------------------------------------
# 10. 比赛中的实际能力（04 §6.6）
# ---------------------------------------------------------------------------------------------

def aim_effective(match: Mapping[str, int], skill: str) -> Dict[str, Tuple[int, int]]:
    """'按能力 +N 瞄准/计算'型技能在比赛中的有效值：{能力: (比赛中能力值, 有效值)}。

    RULES["SKILL_AIM_UNCAPPED"]：在增能之后再加，不受 99 封顶（T3 原文只覆盖 Through Passing，其余 T4 推断）。
    是否另有 120 之类的上限未找到，这里不截断。
    """
    return {k: (int(match[k]), int(match[k]) + n) for k, n in SKILLS[skill].aim_bonus}


def match_stats(card: Mapping[str, int], *, proficiency: str = "high", manager_p: Optional[int] = None,
                booster_bonus: Optional[Mapping[str, int]] = None, skill_bonus: Optional[Mapping[str, int]] = None,
                condition_bonus: Optional[Mapping[str, int]] = None) -> Dict[str, int]:
    """按 04 §6.6 的顺序计算比赛中某一时刻的能力（社区逆向，待客户端核实）。

    card：培养后的卡面值（≤99，不含增能/主教练）。
    1) 卡面 + 状态箭头 → [40, 99]
    2) + 技能情境加成（+ Link-up）+ trunc((位置倍率 × 主教练倍率 − 1) × 卡面值)  → [40, 99]
    3) + 增能（球员两个槽 + 主教练增能），不缩放，≤ 120
    疲劳倍率按 1（满体力）。
    """
    pf = OUT_OF_POSITION_FACTOR[proficiency]
    mm = manager_multiplier(manager_p) if manager_p is not None else 1.0
    out = {}
    for k in STAT_KEYS:
        v = int(card[k])
        s1 = max(STAT_MIN, min(STAT_MAX, v + int((condition_bonus or {}).get(k, 0))))
        delta = math.trunc((pf[k] * mm - 1.0) * v)
        s2 = max(STAT_MIN, min(STAT_MAX, s1 + int((skill_bonus or {}).get(k, 0)) + delta))
        out[k] = min(BOOSTED_MAX, s2 + int((booster_bonus or {}).get(k, 0)))
    return out


def overall_in_position(card: Mapping[str, int], height_cm: int, wfa_idx: int, position: str, proficiency: str, *,
                        manager_p: Optional[int] = None, booster_bonus: Optional[Mapping[str, int]] = None) -> int:
    """比赛中某位置的"角色总评"估算：先按 04 §3.6 的位置适性倍率（和主教练倍率）折算卡面值，再加增能，再代入 §7 公式。

    本组推断（T4）：v4.2.0 官方说比赛中的位置评价含增能、位置适性和主教练（T1），但没有给出算法；
    pesdb 的各位置总评不含位置适性惩罚。待客户端核实。
    """
    st = match_stats(card, proficiency=proficiency, manager_p=manager_p, booster_bonus=booster_bonus)
    return overall(st, height_cm, wfa_idx, position)
