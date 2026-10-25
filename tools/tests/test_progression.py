"""Tests for tools/data/efootball_rules.py and tools/data/progression_sim.py.

验证数据：
  * fixtures/ref_cards_04.json —— 04 号档案 §8.3 六张参考卡（逐格转录）与 §7.3 Adriano 13 个位置评分；
  * fixtures/pesdb_cards_20261025.json —— 04 §7.3 复算所用的 pesdb 单卡页缓存中提取的数值（99 张不同的卡，
    其中 52 张可培养）。04 档案写的是 97/51；本夹具是从同一缓存目录重新提取的全部卡，数量多 2/1 张。
两份数据都是社区数据库（T3），这里的"一致"只说明实现与社区公式自洽，不等于客户端公式（04 §7.3、U11）。
"""

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
FIX = os.path.join(HERE, "fixtures")
sys.path.insert(0, DATA)

import efootball_rules as R  # noqa: E402
import progression_sim as S  # noqa: E402


def _load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return json.load(fh)


REF = _load("ref_cards_04.json")
PESDB = _load("pesdb_cards_20261025.json")
EXAMPLE = _load("example_adriano_epic_0506.json")
ORDER = REF["_about"]["stat_order"]


def _stats(values):
    return dict(zip(ORDER, values))


# ---------------------------------------------------------------------------------------------
# 数据表
# ---------------------------------------------------------------------------------------------

def test_stat_table_matches_dossier():
    assert len(R.STATS) == 26
    assert [s.id for s in R.STATS] == list(range(1, 27))
    assert list(R.STAT_KEYS) == ORDER  # 04 §2.1 / pesdb 顺序
    by = R.STAT_BY_KEY
    assert (by["offensive_awareness"].en, by["offensive_awareness"].en_us) == ("Attacking Awareness", "Offensive Awareness")
    assert by["set_piece_taking"].en_us == "Set Pieces"
    assert by["gk_parrying"].en_us == "GK Deflecting"
    assert [by[k].zh for k in ("offensive_awareness", "tight_possession", "lofted_pass", "set_piece_taking",
                               "defensive_engagement", "kicking_power", "jumping")] == \
        ["进攻意识", "紧密控球", "空中传球", "罚定位球", "防守参与度", "脚下力量", "跳起"]
    assert len(R.GK_STAT_KEYS) == 5
    assert R.resolve_stat("Offensive Awareness") == R.resolve_stat("进攻意识") == "offensive_awareness"
    assert R.resolve_stat("加速度") == "acceleration"
    with pytest.raises(KeyError):
        R.resolve_stat("Place Kicking")


def test_training_categories_match_dossier():
    expect = {  # 04 §5.3
        "shooting": {"finishing", "set_piece_taking", "curl"},
        "passing": {"low_pass", "lofted_pass"},
        "dribbling": {"ball_control", "dribbling", "tight_possession"},
        "dexterity": {"offensive_awareness", "acceleration", "balance"},
        "lower_body_strength": {"speed", "kicking_power", "stamina"},
        "aerial_strength": {"heading", "jumping", "physical_contact"},
        "defending": {"defensive_awareness", "tackling", "aggression", "defensive_engagement"},
        "gk_1": {"gk_awareness", "jumping"},
        "gk_2": {"gk_parrying", "gk_reach"},
        "gk_3": {"gk_catching", "gk_reflexes"},
    }
    assert {k: set(c.stats) for k, c in R.TRAINING_CATEGORIES.items()} == expect
    # 每项能力都恰好属于一个类别，Jumping 例外（两个）
    for s in R.STATS:
        owners = [k for k, c in R.TRAINING_CATEGORIES.items() if s.key in c.stats]
        assert set(owners) == set(s.categories)
        assert len(owners) == (2 if s.key == "jumping" else 1)
    assert R.categories_for_position("CF") == ("shooting", "passing", "dribbling", "dexterity", "defending",
                                               "aerial_strength", "lower_body_strength")
    assert len(R.categories_for_position("GK")) == 10


def test_points_and_step_cost():
    assert R.progression_points(34) == 66 and R.progression_points(31) == 60 and R.progression_points(1) == 0
    assert [R.step_cost(n) for n in range(1, 21)] == [1] * 4 + [2] * 4 + [3] * 4 + [4] * 4 + [5] * 4
    assert [R.cumulative_cost(n) for n in (4, 8, 12, 16, 20)] == [4, 12, 24, 40, 60]  # 04 §5.4 表
    running = 0
    for n in range(1, 201):
        running += R.step_cost(n)
        assert R.cumulative_cost(n) == running
    assert R.allocation_cost({"shooting": 11, "passing": 1, "dribbling": 4, "dexterity": 12,
                              "aerial_strength": 4, "lower_body_strength": 8}) == 66


def test_apply_training_caps_at_99_and_jumping_is_shared():
    base = {k: 40 for k in R.STAT_KEYS}
    base["finishing"] = 97
    t = R.apply_training(base, {"shooting": 5, "aerial_strength": 3, "gk_1": 2})
    assert t["finishing"] == 99 and t["curl"] == 45
    assert t["jumping"] == 45 and t["heading"] == 43 and t["gk_awareness"] == 42


def test_rating_weight_table_exact():
    assert len(R.HEIGHT_WEIGHTS) == 13 and set(R.RATING_WEIGHTS) == set(R.STAT_KEYS)
    for k, row in R.RATING_WEIGHTS.items():
        assert len(row) == 13, k
    totals = tuple(sum(R.RATING_WEIGHTS[k][i] for k in R.STAT_KEYS) for i in range(13))
    assert totals == R.RATING_WEIGHT_TOTALS  # 04 §7.2 末行
    W = lambda k, p: R.RATING_WEIGHTS[k][R.POS_INDEX[p]]  # noqa: E731
    assert (W("finishing", "CF"), W("finishing", "SS"), W("offensive_awareness", "CF")) == (358, 284, 210)
    assert (W("defensive_awareness", "CB"), W("gk_awareness", "GK"), W("ball_control", "SS")) == (286, 279, 210)
    # 04 §7.2 注明的 LMF/RMF 不对称必须原样保留
    assert (W("dribbling", "LMF"), W("dribbling", "RMF")) == (122, 110)
    assert (W("tight_possession", "LMF"), W("tight_possession", "RMF")) == (61, 73)
    assert R.HEIGHT_WEIGHTS[R.POS_INDEX["CF"]] == 99 and R.HEIGHT_WEIGHTS[R.POS_INDEX["GK"]] == 186
    assert [R.weak_foot_rating(i) for i in range(4)] == [40, 59, 79, 99]
    assert [R.wfa_index(a) for a in ("Low", "Medium", "High", "Very High")] == [0, 1, 2, 3]


def test_height_effect_example_from_dossier():
    # 04 §9.6：183 cm 比 169 cm 在 CF 多 (183−169)×99/1000 ≈ 1.4 总评
    st = {k: 70 for k in R.STAT_KEYS}
    d = R.raw_overall(st, 183, 2, "CF") - R.raw_overall(st, 169, 2, "CF")
    assert d == pytest.approx(1.386)


def test_skill_tables():
    assert len(R.SKILLS) == 67
    special = [s for s in R.SKILLS.values() if s.special]
    assert len(special) == 23  # 19 Show Time + 4 个 2026 新技能
    groups = {}
    for s in R.SKILLS.values():
        if not s.special:
            groups[s.group] = groups.get(s.group, 0) + 1
    # [S34] 的分组（04 核查记录 C22）：Aerial Superiority 在防守组，防守 7、通用 6
    assert groups == {"dribbling": 9, "shooting": 9, "passing": 9, "defending": 7, "goalkeeping": 4, "general": 6}
    # 直接加能力值的技能与"按能力 +N 瞄准"的技能互不重叠（C21：两类的封顶规则不同）
    assert not [s.name for s in R.SKILLS.values() if s.stat_bonus and s.aim_bonus]
    assert dict(R.SKILLS["Phenomenal Finishing"].aim_bonus) == {"finishing": 7, "heading": 7}
    assert dict(R.SKILLS["Through Passing"].aim_bonus) == {"low_pass": 5, "lofted_pass": 5}
    assert sorted(s.no for s in R.SKILLS.values()) == list(range(1, 68))
    assert len(R.DUMMY_SKILLS) == 7
    assert R.resolve_skill("One-time Shot") == "First-time Shot"
    assert R.resolve_skill("Inside Bounce") == "Scotch Move"
    assert R.resolve_skill("无定型射门") == "Phenomenal Finishing"
    assert R.resolve_skill("百折不挠") == "Willpower"
    with pytest.raises(KeyError, match="Dummy"):
        R.resolve_skill("Thames")
    with pytest.raises(KeyError):
        R.resolve_skill("Toe Poke")
    md = R.SKILLS["Momentum Dribbling"]
    assert dict(md.stat_bonus) == {"dribbling": 6, "tight_possession": 6, "speed": 6, "acceleration": 6}
    assert (R.MAX_CARD_SKILLS, R.MAX_ADDITIONAL_SKILLS) == (10, 5)


def test_playing_style_tables():
    assert len(R.ATTACKING_STYLES) == 22 and len(R.DEFENSIVE_STYLES) == 17 and len(R.AI_STYLES) == 7
    assert R.ATTACKING_STYLES["Goal Poacher"].positions == ("CF",)
    assert R.ATTACKING_STYLES["Fox in the Box"].positions == ("CF",)
    assert R.ATTACKING_STYLES["Classic No. 10"].positions == ("AMF", "SS")
    assert R.DEFENSIVE_STYLES["Attack Outlet"].positions == ("AMF", "LWF", "RWF", "SS", "CF")
    assert R.DEFENSIVE_STYLES["Front Line Pressure"].positions == ("LWF", "RWF", "SS", "CF")
    assert R.resolve_style(R.ATTACKING_STYLES, "Adv. Striker") == "Goal Poacher"
    assert R.resolve_style(R.AI_STYLES, "Agility King") == "Mazing Run"
    assert R.style_active_positions(R.ATTACKING_STYLES["Goal Poacher"], ("CF", "SS")) == ("CF",)


def test_booster_table():
    assert len(R.REGULAR_BOOSTERS) == 29
    ids = [b.pesdb_id for b in R.REGULAR_BOOSTERS.values()]
    assert len(set(ids)) == 29
    for b in R.REGULAR_BOOSTERS.values():
        assert len(set(b.stats)) == 4 and set(b.stats) <= set(R.STAT_KEYS), b.name
    assert R.resolve_booster("Offence creator") == ("Offence Creator", "regular")
    assert R.resolve_booster("射手本能") == ("Striker's Instinct", "regular")
    assert R.resolve_booster("Kicking Power") == ("kicking_power", "single")
    assert R.resolve_booster("Total Package") == ("Total Package", "special")
    with pytest.raises(KeyError):
        R.booster_stats("Son of God", "special")
    assert R.parse_booster_label("Striker's Instinct +4") == ("Striker's Instinct", 4)


def test_booster_definitions_agree_with_pesdb_sample():
    seen = 0
    for c in PESDB["cards"]:
        for b in c["boosters"]:
            name, val = R.parse_booster_label(b["name"])
            if name.lower().startswith("empty booster slot"):
                assert b["stats"] == {}
                continue
            cname, kind = R.resolve_booster(name)
            stats = R.booster_stats(cname, kind) if R.SPECIAL_BOOSTERS.get(cname, 1) is not None else None
            assert set(b["stats"]) == set(stats), b
            assert set(b["stats"].values()) == {val}, b
            seen += 1
    assert seen >= 40


# ---------------------------------------------------------------------------------------------
# 04 §8.3 / §7.3 验证样例
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("card", REF["cards"], ids=lambda c: f"{c['name']}|{c['card']}")
def test_reference_cards_04(card):
    base = _stats(card["lvl1"])
    wfa = R.wfa_index(card["weak_foot"][1])
    pos, h = card["position"], card["height_cm"]
    assert R.progression_points(card["max_level"]) == card["points"]
    assert R.overall(base, h, wfa, pos) == card["ovr_l1"]
    alloc, steps = S.greedy_allocation(base, h, wfa, pos, card["points"])
    assert {k: v for k, v in alloc.items() if v} == card["alloc"]
    assert steps[-1].used == R.allocation_cost(alloc) <= card["points"]
    trained = R.apply_training(base, alloc)
    assert R.overall(trained, h, wfa, pos) == card["ovr_max_nob"]
    b1 = [b for b in card["boosters"] if b.get("name") and b["active"]]
    bonus = R.sum_bonus(*({k: b["value"] for k in R.booster_stats(*R.resolve_booster(b["name"]))} for b in b1))
    with_b = R.apply_boosters(trained, bonus)
    assert [with_b[k] for k in ORDER] == card["max_b1"]
    assert R.overall(with_b, h, wfa, pos) == card["ovr_max_b1"]


def test_adriano_13_position_ratings():
    card = REF["cards"][1]
    assert card["name"] == "Adriano" and "05-06" in card["card"]
    got = R.overall_all_positions(_stats(card["lvl1"]), card["height_cm"], R.wfa_index(card["weak_foot"][1]))
    assert got == card["position_ratings_l1"]


def test_reference_cards_parse_and_validate_cleanly():
    """六张官方参考卡按输入格式解析后，不应触发任何 ERROR（检验校验器不会误伤真实卡）。"""
    for c in REF["cards"]:
        d = {"name": c["name"], "registered_position": c["position"], "height_cm": c["height_cm"],
             "max_level": c["max_level"], "stats": _stats(c["lvl1"]), "weak_foot_usage": c["weak_foot"][0],
             "weak_foot_accuracy": c["weak_foot"][1], "form": c["form"], "injury_resistance": c["injury_resistance"],
             "playing_style": {"attacking": c["styles"][0], "defensive": c["styles"][1]},
             "ai_playing_styles": c["ai"], "skills": c["skills"], "boosters": c["boosters"]}
        card, issues = S.parse_card(d)
        res = S.simulate(card, per_level=False)
        errs = [str(i) for i in issues + res.issues if i.level == "ERROR"]
        assert not errs, (c["name"], errs)
        assert res.ratings["L1"][c["position"]] == c["ovr_l1"]
        assert res.ratings["MAX+FIXED"][c["position"]] == c["ovr_max_b1"]


def test_pesdb_sample_all_cards():
    cards = PESDB["cards"]
    assert len(cards) == 99
    n_prog = 0
    for c in cards:
        base = dict(zip(PESDB["_about"]["stat_order"], c["stats"]))
        h, w, p = c["height_cm"], c["wfa_index"], c["position"]
        assert R.overall(base, h, w, p) == c["base_overall"], c["name"]
        bonus = R.sum_bonus(*(b["stats"] for b in c["boosters"] if b["active"]))
        if c["progression_enabled"] and c["max_level"] > 1:
            n_prog += 1
            budget = R.progression_points(c["max_level"])
            alloc, _ = S.greedy_allocation(base, h, w, p, budget)
            got = R.overall(R.apply_boosters(R.apply_training(base, alloc), bonus), h, w, p)
            assert got == c["db_max_overall"], (c["name"], c["card_type"])
            # 精确最优在 raw 上不会低于贪心；本样本中 floor 后的总评两者都相同
            opt = S.optimal_allocation(base, budget, S.objective_weights({p: 1}), categories=R.categories_for_position(p))
            assert R.allocation_cost(opt) <= budget
            t_opt = R.rating_total(R.apply_training(base, opt), h, w, p)
            t_gr = R.rating_total(R.apply_training(base, alloc), h, w, p)
            assert t_opt >= t_gr
            assert t_opt // 1000 == t_gr // 1000
        else:
            assert R.overall(R.apply_boosters(base, bonus), h, w, p) == c["db_max_overall"], c["name"]
    assert n_prog == 52


# ---------------------------------------------------------------------------------------------
# 增能、主教练、比赛中能力
# ---------------------------------------------------------------------------------------------

def test_boosters_after_cap_and_120_limit():
    base = {k: 40 for k in R.STAT_KEYS}
    base["finishing"] = 97
    trained = R.apply_training(base, {"shooting": 6})
    assert trained["finishing"] == 99
    assert R.apply_boosters(trained, {"finishing": 4})["finishing"] == 103
    assert R.apply_boosters(trained, {"finishing": 30})["finishing"] == R.BOOSTED_MAX == 120
    assert R.apply_boosters(trained, R.sum_bonus({"finishing": 4}, {"finishing": 6}))["finishing"] == 109


def test_manager_multiplier_examples():
    # 04 §6.5 例子
    for p, m in ((40, 0.905), (67, 1.0003), (89, 1.036), (99, 1.0405)):
        assert R.manager_multiplier(p) == pytest.approx(m, abs=5e-4)
    # 分段边界：p<90 用曲线，p≥90 用直线（90 → 99.253+0.0485×90 = 103.618%）
    assert R.manager_multiplier(90) == pytest.approx(1.03618, abs=1e-9)
    assert R.manager_multiplier(89) == pytest.approx((104 - 38 * 100 / 99 ** 2) / 100, abs=1e-12)
    assert [80 + R.manager_delta(80, 90), 55 + R.manager_delta(55, 90), 56 + R.manager_delta(56, 90)] == [82, 56, 58]
    # p = 64–71 时不改变任何能力
    for p in range(64, 72):
        assert all(R.manager_delta(v, p) == 0 for v in range(40, 100)), p
    # efootballexplained 原文的 80 能力示例（[S25]）：40→73，50→76，63–72→80，80–92→82，≥93→83
    for p, want in [(40, 73), (50, 76)] + [(p, 80) for p in range(63, 73)] + [(p, 82) for p in range(80, 93)] + \
            [(p, 83) for p in range(93, 100)]:
        assert 80 + R.manager_delta(80, p) == want, p
    assert R.manager_delta(-0, 40) == 0
    # 向零取整 + 夹在 [40, 99]
    st = {k: 40 for k in R.STAT_KEYS}
    st["finishing"] = 99
    out = R.apply_manager(st, 40)
    assert out["speed"] == 40 and out["finishing"] == 99 + int((0.905 - 1) * 99) == 90
    assert R.apply_manager(st, 99)["finishing"] == 99


def test_manager_formula_vs_pesdb_thresholds():
    # 04 §6.5 的 T4 核对例子：p=89 时门槛 40、56、84
    assert R.PESDB_MANAGER_THRESHOLDS[89] == (40, 56, 84)
    assert [R.manager_delta(v, 89, "pesdb") for v in (40, 55, 56, 83, 84, 99)] == [1, 1, 2, 2, 3, 3]
    assert [R.manager_delta(v, 89, "efx") for v in (40, 55, 56, 83, 84, 99)] == [1, 1, 2, 2, 3, 3]
    # 两种社区描述并非处处一致：以下熟练度值的门槛差一档（本次实现发现，04 档案中未记录）
    mism = R.manager_threshold_mismatches()
    assert [m[0] for m in mism] == [73, 81, 82, 84, 85, 86, 90]
    for p, derived, thr in mism:
        assert len(derived) == len(thr) and sum(abs(a - b) for a, b in zip(derived, thr)) <= 3


def test_out_of_position_and_match_order():
    groups = {}
    for k, f in R.OUT_OF_POSITION_FACTOR["mid"].items():
        groups.setdefault(f, set()).add(k)
    assert {f: len(v) for f, v in groups.items()} == {0.92: 11, 0.94: 9, 0.96: 3, 1.0: 3}
    assert groups[1.0] == {"defensive_engagement", "tight_possession", "aggression"}
    card = {k: 40 for k in R.STAT_KEYS}
    card.update(speed=91, tight_possession=90, finishing=99)
    m = R.match_stats(card, proficiency="mid", booster_bonus={"speed": 4})
    assert m["speed"] == 91 - 7 + 4      # trunc(−0.08×91)=−7，增能不打折
    assert m["tight_possession"] == 90   # 不受位置影响
    low = R.match_stats(card, proficiency="low")
    assert low["finishing"] == 99 - 19   # trunc(−0.2×99) = −19
    # 位置 × 主教练相乘后再取整
    both = R.match_stats(card, proficiency="mid", manager_p=89)
    import math
    assert both["speed"] == 91 + math.trunc((0.92 * R.manager_multiplier(89) - 1) * 91)
    # 技能加成在 99 封顶前相加：99 的能力吃不到
    sk = R.match_stats(card, skill_bonus={"finishing": 6, "speed": 6}, booster_bonus={"finishing": 4})
    assert sk["finishing"] == 103 and sk["speed"] == 97


# ---------------------------------------------------------------------------------------------
# 加点优化器
# ---------------------------------------------------------------------------------------------

def _brute(base, budget, weights, cats, targets=None, limits=None):
    targets, limits = targets or {}, limits or {}
    best = None

    def val(alloc):
        t = R.apply_training(base, alloc)
        p = sum(weights.get(k, 0) * (min(t[k], targets.get(k, 999)) - min(base[k], targets.get(k, 999))) for k in R.STAT_KEYS)
        return p

    def rec(i, alloc, cost):
        nonlocal best
        if i == len(cats):
            t = R.apply_training(base, alloc)
            if any(t[k] > max(lim, base[k]) for k, lim in limits.items()):
                return
            v = val(alloc)
            if best is None or v > best:
                best = v
            return
        n = 0
        while cost + R.cumulative_cost(n) <= budget:
            alloc[cats[i]] = n
            rec(i + 1, alloc, cost + R.cumulative_cost(n))
            n += 1
        alloc[cats[i]] = 0
    rec(0, {}, 0)
    return best


@pytest.mark.parametrize("pos,budget", [("CF", 9), ("AMF", 11), ("GK", 7)])
def test_optimal_matches_brute_force(pos, budget):
    base = _stats(REF["cards"][0]["lvl1"])
    if pos == "GK":
        base.update(gk_awareness=80, gk_catching=78, gk_parrying=76, gk_reflexes=81, gk_reach=79, jumping=70)
    w = {k: R.RATING_WEIGHTS[k][R.POS_INDEX[pos]] for k in R.STAT_KEYS}
    cats = R.categories_for_position(pos)
    opt = S.optimal_allocation(base, budget, w, categories=cats)
    t = R.apply_training(base, opt)
    got = sum(w[k] * (t[k] - base[k]) for k in R.STAT_KEYS)
    assert R.allocation_cost(opt) <= budget
    assert got == _brute(base, budget, w, list(cats))


def test_optimal_with_targets_and_position_mix():
    base = _stats(REF["cards"][1]["lvl1"])   # Adriano Epic
    cats = R.categories_for_position("CF")
    targets = {"finishing": 85, "offensive_awareness": 80}
    w = S.objective_weights({"CF": 1})
    opt = S.optimal_allocation(base, 14, w, categories=cats, targets=targets)
    assert _brute(base, 14, {k: int(v) for k, v in w.items()}, list(cats), targets) == sum(
        int(w[k]) * (min(v, targets.get(k, 999)) - min(base[k], targets.get(k, 999)))
        for k, v in R.apply_training(base, opt).items())
    mix = S.objective_weights({"CF": 1, "SS": 0.5})
    assert mix["finishing"] == 358 + 142 and mix["ball_control"] == 123 + 105


def test_optimal_with_hard_limits():
    base = _stats(REF["cards"][1]["lvl1"])   # Adriano Epic: heading 76, physical_contact 83
    cats = R.categories_for_position("CF")
    w = {k: int(v) for k, v in S.objective_weights({"CF": 1}).items()}
    limits = {"heading": 78, "finishing": 85, "curl": 70}   # curl 已是 76 > 70：射门类不得再加
    opt = S.optimal_allocation(base, 16, w, categories=cats, limits=limits)
    t = R.apply_training(base, opt)
    assert t["heading"] <= 78 and opt["aerial_strength"] <= 2 and opt["shooting"] == 0
    got = sum(w[k] * (t[k] - base[k]) for k in R.STAT_KEYS)
    assert got == _brute(base, 16, w, list(cats), limits=limits)
    card, _ = S.parse_card({**EXAMPLE, "allocation": "optimal", "objective": {"limits": {"Heading": 76}}})
    res = S.simulate(card, per_level=False)
    assert res.stages["MAX"]["heading"] == 76 and res.alloc.get("aerial_strength", 0) == 0
    assert any(i.code == "ALLOC_NOT_OPTIMAL" and i.level == "INFO" for i in res.issues)


def test_greedy_takes_zero_gain_steps_like_pesdb():
    base = {k: 99 for k in R.STAT_KEYS}
    alloc, steps = S.greedy_allocation(base, 180, 2, "CF", 10)
    assert R.allocation_cost(alloc) == 10 and alloc["shooting"] > 0


def test_order_steps_reproduces_allocation():
    base = _stats(REF["cards"][2]["lvl1"])
    alloc = {"shooting": 4, "passing": 8, "dribbling": 12, "dexterity": 8, "lower_body_strength": 8}
    steps = S.order_steps(base, alloc, 182, 2, "AMF")
    cnt = {}
    for s in steps:
        cnt[s.category] = cnt.get(s.category, 0) + 1
        assert s.nth == cnt[s.category] and s.cost == R.step_cost(s.nth)
    assert cnt == alloc and steps[-1].used == R.allocation_cost(alloc) == 64
    assert steps[-1].ovr == 100


# ---------------------------------------------------------------------------------------------
# 校验器
# ---------------------------------------------------------------------------------------------

def _issues(mutate):
    d = json.loads(json.dumps(EXAMPLE))
    mutate(d)
    card, issues = S.parse_card(d)
    res = S.simulate(card, per_level=False)
    return issues + res.issues


def _codes(issues, level="ERROR"):
    return {i.code for i in issues if i.level == level}


def test_example_card_has_no_errors():
    assert not _codes(_issues(lambda d: None))


@pytest.mark.parametrize("mutate,code", [
    (lambda d: d["skills"].append("Chip Shot Control"), "SKILL_CARD_MAX"),
    (lambda d: d.update(additional_skills=["Scissors Feint", "Double Touch", "Flip Flap", "Chop Turn", "Sombrero", "Rabona"]),
     "SKILL_ADDITIONAL_MAX"),
    (lambda d: d.update(additional_skills=["Momentum Dribbling"]), "SKILL_SPECIAL_ADDITIONAL"),
    (lambda d: d.update(additional_skills=["Heading"]), "SKILL_DUP"),
    (lambda d: d["skills"].__setitem__(0, "Nile"), "SKILL_NAME"),
    (lambda d: d["skills"].__setitem__(0, "Toe Poke"), "SKILL_NAME"),
    (lambda d: d["stats"].update({"Finishing": 100}), "STAT_RANGE"),
    (lambda d: d["stats"].update({"Tackling": 39}), "STAT_RANGE"),
    (lambda d: d["boosters"].__setitem__(1, {"slot": 2, "kind": "craftable", "name": "Agility", "value": 2}), "BOOSTER_CRAFTED"),
    (lambda d: d["boosters"].__setitem__(1, {"slot": 2, "kind": "craftable", "name": "Total Package", "value": 1}), "BOOSTER_CRAFTED"),
    (lambda d: d["boosters"][0].update(value=7), "BOOSTER_VALUE"),
    (lambda d: d["boosters"][1].update(value=5), "BOOSTER_VALUE"),
    (lambda d: d["boosters"][0].update(stats=["finishing", "speed", "acceleration", "balance"]), "BOOSTER_STATS"),
    (lambda d: d["boosters"].append({"slot": 3, "kind": "craftable", "name": None}), "BOOSTER_SLOTS"),
    (lambda d: d["boosters"].append({"slot": 3, "kind": "fixed", "name": "Son of God", "value": 3}), "BOOSTER_STATS"),
    (lambda d: d["boosters"].append({"slot": 3, "kind": "fixed", "name": "Magic Boots", "value": 3}), "BOOSTER_NAME"),
    (lambda d: d.update(ai_playing_styles=["Trickster", "Mazing Run", "Long Ranger", "Incisive Run"]), "AI_STYLE_MAX"),
    (lambda d: d.update(playing_style={"attacking": "Anchor Man", "defensive": "Basic"}), "STYLE_INACTIVE"),
    (lambda d: d["position_proficiency"].update(CF="mid"), "POSITION"),
    (lambda d: d.update(allocation={"shooting": 20, "dexterity": 20}), "ALLOC_BUDGET"),
    (lambda d: d.update(allocation={"gk_1": 2}), "ALLOC_GK"),
    (lambda d: d.update(weak_foot_accuracy="Super"), "WEAK_FOOT_ACCURACY"),
    (lambda d: d.update(manager={"proficiency": 120}), "MANAGER"),
])
def test_validation_errors(mutate, code):
    assert code in _codes(_issues(mutate))


def test_validation_warnings():
    w = _codes(_issues(lambda d: d.update(card_pool="x", price=100)), "WARN")
    assert "UNKNOWN_FIELD" in w
    w = _codes(_issues(lambda d: d.update(playing_style={"attacking": "Hole Player", "defensive": "Basic"})), "WARN")
    assert "STYLE_POSITION" in w   # Hole Player 在 SS 生效（中适性），但注册位置 CF 不兼容
    w = _codes(_issues(lambda d: d.update(allocation={"defending": 30})), "WARN")
    assert "ALLOC_NOT_OPTIMAL" in w


def test_missing_stat_raises():
    d = json.loads(json.dumps(EXAMPLE))
    del d["stats"]["Stamina"]
    with pytest.raises(S.CardError):
        S.parse_card(d)


def test_skill_bonus_wasted_at_99_is_reported():
    def mut(d):
        d["skills"][0] = "Momentum Dribbling"
        d["stats"]["Dribbling"] = 99
    infos = _issues(mut)
    assert any(i.code == "SKILL_BONUS_CAPPED" and "dribbling" in i.message for i in infos)


# ---------------------------------------------------------------------------------------------
# 规则登记与 CLI
# ---------------------------------------------------------------------------------------------

def test_rules_registry_marks_community_and_client_checks():
    for r in R.RULES.values():
        if "T3" in r.tier:
            assert "社区逆向" in r.tag()
        if r.client_check:
            assert R.CLIENT_CHECK in r.tag()
    for rid in ("RATING_FORMULA", "STEP_COST", "POINTS", "BOOST_CAP_120", "MANAGER_FORMULA", "SKILL_CARD_MAX"):
        assert R.RULES[rid].client_check and R.RULES[rid].community
    assert not R.RULES["SKILL_ADDITIONAL_MAX"].community  # T1


def test_cli_example(tmp_path, capsys):
    out = tmp_path / "res.json"
    rc = S.main([os.path.join(FIX, "example_adriano_epic_0506.json"), "--json", str(out)])
    text = capsys.readouterr().out
    assert rc == 0
    for needle in ("加点方案", "各位置总评", "MAX+FIXED", "MAX+MANAGER", "比赛中实际能力", "校验", "待客户端核实"):
        assert needle in text
    res = json.loads(out.read_text(encoding="utf-8"))
    assert res["ratings"]["L1"]["CF"] == 86 and res["ratings"]["MAX"]["CF"] == 99 and res["ratings"]["MAX+FIXED"]["CF"] == 102
    assert res["allocation_cost"] == 66 and len(res["per_level"]) == 34
    assert res["per_level"][-1]["overall"] == 99
    assert res["stages"]["MAX+MANAGER"]["physical_contact"] == 87 + 3 + 1   # 87×3.6% → +3，再 + 主教练增能 1


def test_cli_optimal_and_pesdb_manager(tmp_path, capsys):
    rc = S.main([os.path.join(FIX, "example_adriano_epic_0506.json"), "--allocation", "optimal", "--objective", "CF=1,SS=0.3",
                 "--manager", "85", "--manager-model", "pesdb", "--manager-booster", "stamina=1", "--no-levels"])
    text = capsys.readouterr().out
    assert rc == 0 and "optimal" in text and "pesdb" in text


def test_cli_invalid_card_exit_code(tmp_path, capsys):
    d = json.loads(json.dumps(EXAMPLE))
    d["additional_skills"] = ["Phenomenal Finishing"]
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(d), encoding="utf-8")
    assert S.main([str(p), "--no-levels", "--no-steps"]) == 1
    assert "SKILL_DUP" in capsys.readouterr().out


# ---------------------------------------------------------------------------------------------
# 审查后新增：瞄准型技能、top-k 加点、simulation 子对象、Live Update Rating、技能数提示
# ---------------------------------------------------------------------------------------------

def test_aim_bonus_not_capped_at_99():
    m = {k: 99 for k in R.STAT_KEYS}
    m["finishing"] = 102                                    # 增能后 >99
    assert R.aim_effective(m, "Phenomenal Finishing")["finishing"] == (102, 109)
    assert R.RULES["SKILL_AIM_UNCAPPED"].client_check and R.RULES["SKILL_AIM_UNCAPPED"].community


def test_top_allocations_agree_with_optimal():
    base = {k: v for k, v in S.parse_card(EXAMPLE)[0].stats.items()}
    cats = R.categories_for_position("CF")
    top = S.top_allocations(base, 189, 1, "CF", 66, categories=cats, k=3)
    opt = S.optimal_allocation(base, 66, S.objective_weights({"CF": 1}), categories=cats)
    assert top[0][0] == R.rating_total(R.apply_training(base, opt), 189, 1, "CF")
    assert top[0][0] >= top[1][0] >= top[2][0] and all(c <= 66 for _, c, _ in top)


def test_simulation_subobject_and_duplicates():
    d = json.loads(json.dumps(EXAMPLE))
    d["simulation"] = {"allocation": d.pop("allocation"), "manager": d.pop("manager")}
    card, issues = S.parse_card(d)
    assert not issues and card.manager.proficiency == 89 and card.allocation == "auto"
    d["manager"] = {"proficiency": 80}
    _, issues = S.parse_card(d)
    assert "SIM_FIELD_DUP" in {i.code for i in issues if i.level == "ERROR"}


def test_proficiency_colour_names():
    d = json.loads(json.dumps(EXAMPLE))
    d["position_proficiency"] = {"CF": "亮绿", "SS": "淡绿", "LWF": "浅绿"}
    card, issues = S.parse_card(d)
    assert not issues and card.proficiency["CF"] == "high" and card.proficiency["SS"] == card.proficiency["LWF"] == "mid"


def test_live_update_rating_and_skill_count_infos():
    infos = _codes(_issues(lambda d: d.update(live_update_rating="A")), "INFO")
    assert "LIVE_UPDATE_RATING" in infos
    assert "LIVE_UPDATE_RATING" not in _codes(_issues(lambda d: d.update(live_update_rating="B")), "INFO")
    assert "SKILL_CARD_COUNT" in _codes(_issues(lambda d: d["skills"].pop()), "INFO")
    assert "SKILL_CARD_COUNT" not in _codes(_issues(lambda d: None), "INFO")      # 10 个技能
    assert "LIVE_UPDATE_RATING" in _codes(_issues(lambda d: d.update(live_update_rating="Z")), "ERROR")
