#!/usr/bin/env python3
"""14 项身体参数的"模型估计"（T4，PES 2021 刻度）：拟合好的 R9 MakeHuman 身体 vs 同身高/体重的中性男性。

做法
  1. R9 = model/source/r9_params.json（tools/model/body_fit.py 拟合到 data/body/r9_body_targets.json 的结果）。
  2. 中性男性：性别/年龄/人种宏与 R9 相同，muscle 与 proportions 取中性 0.5，**不加任何修饰器**（没有测量修饰器、
     肌肉修饰器、头脸修饰器）。按任务要求有三种"同身高/体重"的定义，结果并列给出（差别本身就是不确定度）：
       B  同身高同质量（主方案）：解 height/weight 宏，使身高 = 183.0 cm、质量 = R9 模型质量（82.6 kg）。
          中性肌肉要达到同样质量只能靠脂肪，所以腰、臀、膝、小腿偏粗 → 会**低估**围度类参数。
       A  同 weight 宏：weight 宏 = R9 的值，只把 height 宏重解到 183.0 cm；质量约 77 kg → 会**高估**围度类参数。
       G  PES 式体重：中性男性取 PES 2021 在 183 cm 的标准体重 Wstd = 73.25 kg（06 §4.3），再按 PES 2021 体重效果
          给围度乘系数（腹部、上臂 y/z 最大 ×1.10，大腿、小腿最大 ×1.05，按归一化体重线性；颈、胸属于 B 表，档案未给
          幅度 → 不乘，结果因此偏高）。这是最接近"游戏里体重字段已经加粗了一部分"的定义，但叠加的假设最多。
  3. 测量（cm，MakeHuman 坐标：y 向上、z 前后、x 左右）：body_fit.Measurer 的全部量 + 颈长（neck01→head 关节）、
     锁骨段长（clavicle→upperarm01 关节）、锁骨抬高角、各围度环的前后深/左右宽、臂长（upperarm01→lowerarm01→wrist）。
  4. 换算：每项参数对应 PES 2021 中它缩放的骨骼轴（06 §4.2 表），步数 = 相对差 / 每档百分比（正负方向分别取表值），
     两个轴时取两轴步数的平均；ShoulderHeight 用锁骨转角（每档 8/7°），LegLength 用髋关节高度（每档 +1.8/7 %、−1.9/7 %）。
  5. 头部 3 项另外对照成年男性常模（见 HEAD_NORMS，含来源与核实状态）。注意 PES 骨轴约定：x = 沿骨长度，
     头骨沿竖直方向，所以 HeadLength ↔ 头高（头顶—颏下），HeadWidth ↔ 头宽，HeadDepth ↔ 人体测量学的"头长"（眉间—枕后）。

**这些数只说明"如果 eFootball 的刻度与 PES 2021 相同"会是几档**；eFootball 的实际效果必须实机标定（任务书第五节）。

  python3 tools/model/body_params_estimate.py                 # 打印
  python3 tools/model/body_params_estimate.py --write data/body/r9_body_params.json   # 只更新 model_estimate_T4 块
  python3 tools/model/body_params_estimate.py --check data/body/r9_body_params.json   # 与已存结果比较（测试用）
"""
import argparse
import hashlib
import json
import math
import os
import sys

import numpy as np
from scipy.optimize import least_squares

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import body_fit as BF  # noqa: E402
import mh_engine as E  # noqa: E402

R9_PARAMS = os.path.join(ROOT, "model", "source", "r9_params.json")
H_TARGET = 183.0

# PES 2021 每档效果（06 §4.2 表；表值 T3 = GodotFootball 引擎逆向，每档百分比 T4 = 06 号档案按公式计算）
# axis: 'y' = 前后（MakeHuman z 方向的深度），'z' = 左右（MakeHuman x 方向的宽度），'x' = 沿骨长度
PES2021_STEP = {
    "NeckLength":       {"bone": "sk_neck x",          "measures": [("neck_length", +1.43, -2.86)]},
    "NeckSize":         {"bone": "sk_neck y / z",      "measures": [("neck_depth", +0.36, -1.43), ("neck_breadth", +1.43, -1.43)]},
    "ShoulderHeight":   {"bone": "sk_shoulder z + clavicle rotation 8*v deg", "measures": [("clavicle_elevation_deg", 8 / 7, -8 / 7)],
                         "unit": "deg", "note": "只用锁骨转角（每档 8/7°）；sk_shoulder z 缩放（+7.86%/−2.86% 每档）与三角肌/斜方肌辅助骨的效果无法在 MakeHuman 上对应"},
    "ShoulderWidth":    {"bone": "sk_shoulder x",      "measures": [("clavicle_length", +2.86, -1.43)]},
    "ChestMeasurement": {"bone": "sk_chest y",         "measures": [("chest_depth", +1.43, -1.14)]},
    "WaistSize":        {"bone": "sk_belly y / z (B table)", "measures": [("waist_depth", +1.00, -1.43), ("waist_breadth", +1.57, -1.86)]},
    "ArmSize":          {"bone": "upper/lower arm y,z", "measures": [("upperarm_circ", +1.14, -1.14)]},
    "ArmLength":        {"bone": "upper/lower arm x",  "measures": [("arm_length", +0.71, -0.71)]},
    "ThighSize":        {"bone": "sk_thigh y / z",     "measures": [("thigh_depth", +1.43, -1.43), ("thigh_breadth", +1.07, -0.71)]},
    "CalfSize":         {"bone": "sk_leg y,z",         "measures": [("calf_circ", +2.86, -1.43)]},
    "LegLength":        {"bone": "G x leg (A table #12)", "measures": [("hip_joint_height", 1.8 / 7, -1.9 / 7)],
                         "note": "PES 2021：+7 档髋高 +1.8%（G×1.018），−7 档 −1.9%（G×0.981），每档约 0.27 cm；"
                                 "且 LegLength 改变整体缩放 G，游戏里的渲染身高会随之变化"},
    "HeadLength":       {"bone": "sk_head x (vertical)", "measures": [("head_height", +1.43, -1.43)]},
    "HeadWidth":        {"bone": "sk_head z",          "measures": [("head_breadth", +1.43, -1.43)]},
    "HeadDepth":        {"bone": "sk_head y",          "measures": [("head_length", +1.43, -1.43)]},
}

# PES 2021 体重模型（06 §4.3）：183 cm 时 Wmin 49.9 / Wstd 73.25 / Wmax 103.4；正端效果按归一化体重线性
PES_WSTD_183, PES_WMAX_183 = 73.25, 103.4
PES_WEIGHT_MAX = {"waist_depth": 0.10, "waist_breadth": 0.10, "upperarm_circ": 0.10,
                  "thigh_depth": 0.05, "thigh_breadth": 0.05, "calf_circ": 0.05}

# 成年男性头部常模。核实状态写在每一项里；未核实的数值不作为事实使用，只用来给出区间。
HEAD_NORMS = {
    "head_breadth": {
        "measure": "最大头宽 eu–eu（软组织，测量器）", "range_cm": [15.2, 15.6],
        "sources": ["ANSUR II 美军男性（Gordon et al. 2014, NATICK/TR-15/007, 'headbreadth'）",
                    "Farkas 1994, Anthropometry of the Head and Face（北美白人青年男性 eu–eu）"],
        "verified": False,
        "note": "区间凭记忆整理；本环境打不开原文（tools.openlab.psu.edu 与 apps.dtic.mil 均 403），待核实。"
                "02 号档案 §4.3 也写明'本次未取得权威数值源'，其估计 15.5–16.0 cm 与此相容。"},
    "head_length": {
        "measure": "最大头长 g–op（眉间—枕后）", "range_cm": [19.5, 20.0],
        "sources": ["ANSUR II 美军男性（Gordon et al. 2014, 'headlength'）", "Farkas 1994（g–op）"],
        "verified": False, "note": "同上，待核实；02 号档案 §4.3 对 R9 的估计为 19.5–20.5 cm（T4）。"},
    "head_height": {
        "measure": "头高 v–gn（头顶—颏下）", "range_cm": [23.0, 24.4],
        "sources": ["Drillis & Contini 1966 身高比例：颏高 0.870H → 头高 0.130H（183 cm → 23.8 cm）；"
                    "该常数不在 02 号档案已核对的 PSU 页面数值列表中，未核实",
                    "02 号档案 §4.3：R9 本人侧面站立照 C2 实测约 0.131H（T4）；区间取 0.126H–0.133H"],
        "verified": False, "note": "R9 模型 23.0 cm 是面部组在 02 档案 23.8±0.8 区间的下沿取值（r9_params.json _provenance.head_scale）。"},
    "head_circumference": {
        "measure": "头围（眉间—枕后水平围）", "value_cm": 57.2,
        "sources": ["Bushby KM, Cole T, Matthews JN, Goodship JA (1992) Centiles for adult head circumference. "
                    "Arch Dis Child 67(10):1286–7：男性均值 57.2 cm（经 en.wikipedia 'Human head' 转引，2026-10-25 读取）"],
        "verified": "二手（维基百科转引）", "note": "只用来检查头长/头宽的组合是否合理，不直接换算档数。"},
}


# ============================================================================= model building
def _solve(params, measurer, free, targets):
    """free: 宏名列表；targets: {measure: value}。最小二乘求宏值。"""
    def res(x):
        for k, v in zip(free, x):
            params["macro"][k] = float(v)
        V, _ = E.build(params, measurer.base)
        m = measurer.measure(V)
        return [(m[k] - t) for k, t in targets.items()]
    x0 = [params["macro"].get(k, 0.5) for k in free]
    r = least_squares(res, x0, bounds=([0.0] * len(free), [1.0] * len(free)), diff_step=1e-3, xtol=1e-10, ftol=1e-10)
    for k, v in zip(free, r.x):
        params["macro"][k] = float(v)
    return params


def extra_measures(V, measurer):
    J, _ = E.joint_positions(V, measurer.skel)
    o = {}
    o["neck_length"] = 10 * float(np.linalg.norm(J["head____head"] - J["neck01____head"]))
    cl, ang, arm = [], [], []
    for s in "LR":
        d = J[f"upperarm01.{s}____head"] - J[f"clavicle.{s}____head"]
        cl.append(np.linalg.norm(d))
        ang.append(math.degrees(math.atan2(d[1], math.hypot(d[0], d[2]))))
        arm.append(np.linalg.norm(J[f"lowerarm01.{s}____head"] - J[f"upperarm01.{s}____head"]) +
                   np.linalg.norm(J[f"wrist.{s}____head"] - J[f"lowerarm01.{s}____head"]))
    o["clavicle_length"] = 10 * float(np.mean(cl))
    o["clavicle_elevation_deg"] = float(np.mean(ang))
    o["arm_length"] = 10 * float(np.mean(arm))
    for k, name in (("neck_circ", "neck"), ("chest_circ", "chest"), ("waist_circ", "waist"),
                    ("thigh_circ", "thigh"), ("calf_circ", "calf"), ("upperarm_circ", "upperarm")):
        P = V[BF.RULERS[k]]
        o[f"{name}_depth"] = 10 * float(np.ptp(P[:, 2]))
        o[f"{name}_breadth"] = 10 * float(np.ptp(P[:, 0]))
    # 头围：头长所在高度附近（最大前后径的水平切片）凸包周长
    B = V[measurer.body_v]
    top = B[:, 1].max()
    head = B[B[:, 1] > J["neck02____head"][1]]
    eye_y = (J["eye.L____head"][1] + J["eye.R____head"][1]) / 2
    best = (0, None)
    for y in np.arange(eye_y + 0.1, top - 0.2, 0.05):
        s = head[np.abs(head[:, 1] - y) < 0.025]
        if len(s) > 8 and np.ptp(s[:, 2]) > best[0]:
            best = (np.ptp(s[:, 2]), s[:, [0, 2]])
    o["head_circumference"] = 10 * _hull_perimeter(best[1])
    return o


def _hull_perimeter(P):
    P = sorted(map(tuple, np.asarray(P, float)))
    if len(P) < 3:
        return 0.0

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lo, hi = [], []
    for p in P:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], p) <= 0:
            lo.pop()
        lo.append(p)
    for p in reversed(P):
        while len(hi) >= 2 and cross(hi[-2], hi[-1], p) <= 0:
            hi.pop()
        hi.append(p)
    h = lo[:-1] + hi[:-1]
    return float(sum(math.dist(h[i], h[(i + 1) % len(h)]) for i in range(len(h))))


def measure_all(params, measurer):
    V, _ = E.build(params, measurer.base)
    m = measurer.measure(V)
    m.update(extra_measures(V, measurer))
    return m


def build_models():
    M = BF.Measurer()
    with open(R9_PARAMS) as fh:
        r9 = json.load(fh)
    mr = measure_all(r9, M)

    def neutral():
        p = {"macro": dict(r9["macro"]), "modifiers": {}}
        p["macro"]["muscle"] = 0.5
        p["macro"]["proportions"] = 0.5
        return p
    B = _solve(neutral(), M, ["height", "weight"], {"height": H_TARGET, "mass_kg": mr["mass_kg"]})
    A = _solve(neutral(), M, ["height"], {"height": H_TARGET})
    G = _solve(neutral(), M, ["height", "weight"], {"height": H_TARGET, "mass_kg": PES_WSTD_183})
    models = {"R9": (r9, mr), "B": (B, measure_all(B, M)), "A": (A, measure_all(A, M)), "G0": (G, measure_all(G, M))}
    # G：G0 的围度乘 PES 2021 体重系数（归一化体重 w = (m − Wstd)/(Wmax − Wstd)）
    w = (mr["mass_kg"] - PES_WSTD_183) / (PES_WMAX_183 - PES_WSTD_183)
    mg = dict(models["G0"][1])
    for k, mx in PES_WEIGHT_MAX.items():
        mg[k] = mg[k] * (1 + mx * w)
    models["G"] = (G, mg)
    return models, w


def steps_for(param, m_r9, m_ref):
    spec = PES2021_STEP[param]
    per = []
    for key, pos, neg in spec["measures"]:
        if spec.get("unit") == "deg":
            diff = m_r9[key] - m_ref[key]
            s = diff / (pos if diff >= 0 else -neg)
            per.append({"measure": key, "r9": round(m_r9[key], 3), "ref": round(m_ref[key], 3),
                        "diff_deg": round(diff, 3), "steps": round(s, 2)})
        else:
            rel = 100 * (m_r9[key] / m_ref[key] - 1)
            s = rel / (pos if rel >= 0 else -neg)
            per.append({"measure": key, "r9": round(m_r9[key], 3), "ref": round(m_ref[key], 3),
                        "rel_pct": round(rel, 2), "pct_per_step": pos if rel >= 0 else neg, "steps": round(s, 2)})
    st = float(np.mean([p["steps"] for p in per]))
    return {"per_measure": per, "steps": round(st, 2), "stored": int(min(14, max(0, round(7 + st))))}


def head_vs_norms(m):
    out = {}
    for key, param in (("head_height", "HeadLength"), ("head_breadth", "HeadWidth"), ("head_length", "HeadDepth")):
        lo, hi = HEAD_NORMS[key]["range_cm"]
        mid = (lo + hi) / 2
        r = m[key]

        def st(ref):
            rel = 100 * (r / ref - 1)
            return rel / 1.43
        s_mid, s_a, s_b = st(mid), st(hi), st(lo)
        out[param] = {"measure": key, "r9_cm": round(r, 2), "norm_range_cm": [lo, hi], "norm_mid_cm": mid,
                      "rel_pct_vs_mid": round(100 * (r / mid - 1), 2), "steps_vs_mid": round(s_mid, 2),
                      "steps_range": [round(min(s_a, s_b), 2), round(max(s_a, s_b), 2)],
                      "stored_per_axis": int(min(14, max(0, round(7 + s_mid)))), "norm_verified": HEAD_NORMS[key]["verified"]}
    gm = (np.prod([m[k] / np.mean(HEAD_NORMS[k]["range_cm"]) for k in ("head_height", "head_breadth", "head_length")])) ** (1 / 3)
    out["uniform"] = {"geometric_mean_ratio": round(float(gm), 4), "steps": round(100 * (gm - 1) / 1.43, 2),
                      "stored": int(min(14, max(0, round(7 + 100 * (gm - 1) / 1.43)))),
                      "meaning": "整体头部大小相对常模（三轴几何平均）；用于'网格只保留形状、大小靠参数'的情形（wrap.py 默认 --align similarity）"}
    out["cephalic_index_r9"] = round(100 * m["head_breadth"] / m["head_length"], 1)
    out["head_circumference_r9_cm"] = round(m["head_circumference"], 1)
    out["head_circumference_norm_cm"] = HEAD_NORMS["head_circumference"]["value_cm"]
    return out


def estimate():
    models, w = build_models()
    mr = models["R9"][1]
    res = {"params": {}, "variants": {}}
    for v in ("B", "A", "G"):
        p, m = models[v]
        res["variants"][v] = {"macro": {k: round(x, 4) for k, x in p["macro"].items() if isinstance(x, float)},
                              "height_cm": round(m["height"], 2), "mass_kg": round(m["mass_kg"], 2)}
    res["variants"]["G"]["mass_kg_before_pes_weight"] = round(models["G0"][1]["mass_kg"], 2)
    res["variants"]["G"]["pes_normalized_weight"] = round(w, 4)
    res["r9"] = {"height_cm": round(mr["height"], 2), "mass_kg": round(mr["mass_kg"], 2)}
    keys = sorted({k for s in PES2021_STEP.values() for k, _, _ in s["measures"]} |
                  {"neck_circ", "chest_circ", "waist_circ", "hips_circ", "thigh_circ", "knee_circ", "biacromial",
                   "knee_height", "chest_breadth", "head_circumference"})
    res["measures_cm"] = {k: {"R9": round(mr[k], 2), **{v: round(models[v][1][k], 2) for v in ("B", "A", "G")}}
                          for k in keys}
    for param in PES2021_STEP:
        d = {v: steps_for(param, mr, models[v][1]) for v in ("B", "A", "G")}
        allst = [d[v]["steps"] for v in d]
        res["params"][param] = {
            "pes2021_bone": PES2021_STEP[param]["bone"],
            "primary_B": d["B"], "variant_A": d["A"], "variant_G": d["G"],
            "steps_span": [round(min(allst), 2), round(max(allst), 2)],
            "stored_span": [int(min(14, max(0, round(7 + min(allst))))), int(min(14, max(0, round(7 + max(allst)))))],
        }
        if "note" in PES2021_STEP[param]:
            res["params"][param]["note"] = PES2021_STEP[param]["note"]
    hip = res["params"]["LegLength"]
    hip["pesdb_alt"] = {"rule": "pesdb 派生指标 'Leg-length Equivalent Height = Height + (LL−7) cm'（06 §4.2，T3）；"
                                "把髋高相对差折成等效身高差，每 1 cm 一档（T4 换算）",
                        "steps_B": round((mr["hip_joint_height"] / models["B"][1]["hip_joint_height"] - 1) * H_TARGET, 2)}
    res["head_vs_norms"] = head_vs_norms(mr)
    res["head_vs_mh_neutral_B"] = {p: res["params"][p]["primary_B"]["steps"] for p in ("HeadLength", "HeadWidth", "HeadDepth")}
    gm = float(np.prod([mr[k] / models["B"][1][k] for k in ("head_height", "head_breadth", "head_length")]) ** (1 / 3))
    res["head_vs_mh_neutral_B"]["uniform"] = {"geometric_mean_ratio": round(gm, 4), "steps": round(100 * (gm - 1) / 1.43, 2),
                                              "neutral_head_circumference_cm": round(models["B"][1]["head_circumference"], 1),
                                              "note": "MakeHuman 中性头围与 Bushby 1992 男性均值 57.2 cm 几乎相同，可作'平均大小'的交叉参照"}
    return res


META = {
    "label": "T4（本组估计），PES 2021 刻度",
    "tool": "tools/model/body_params_estimate.py",
    "inputs": ["model/source/r9_params.json", "MakeHuman 1.x CC0 数据（$MH_DATA）"],
    "method": "R9 拟合模型 vs 中性 MakeHuman 男性（性别/年龄/人种同 R9；muscle、proportions = 0.5；无修饰器）；"
              "相对差 ÷ 06 §4.2 的 PES 2021 每档百分比 = 档数；B 为主方案，A/G 为敏感性方案（见脚本文档）",
    "warning": "PES 2021 的骨骼缩放表不能直接套用到 eFootball（UE4 引擎，实现未找到，06 §4.4）；这里只给方向和量级。",
}


def comparator_stats(doc):
    """由 comparator_table（02 号档案 §6.2 的 pesdb 数值，T3）算出每项参数的统计；只覆盖 pesdb 显示的 11 项。"""
    tab = doc["comparator_table"]
    order = tab["param_order"]
    sim = {k: v for k, v in tab["players"].items() if v["role"] in ("similar", "anchor")}
    anc = {k: v for k, v in sim.items() if v["role"] == "anchor"}
    cnt = {k: v for k, v in tab["players"].items() if v["role"] == "counter"}
    out = {}
    for i, p in enumerate(order):
        sv = sorted(v["values"][i] for v in sim.values())
        out[p] = {"similar_n": len(sv), "similar_median": float(np.median(sv)), "similar_min": sv[0], "similar_max": sv[-1],
                  "anchors_median": float(np.median([v["values"][i] for v in anc.values()])),
                  "anchors": {k: v["values"][i] for k, v in anc.items()},
                  "counterexamples": {k: v["values"][i] for k, v in cnt.items()}}
    return out


def write_into(path, res):
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    with open(R9_PARAMS, "rb") as fh:
        raw = fh.read()
    prov = json.loads(raw).get("_provenance", {})
    doc["model_estimate_T4"] = {**META, "input_model": {"file": "model/source/r9_params.json",
                                                        "sha256": hashlib.sha256(raw).hexdigest(),
                                                        "version": prov.get("version")},
                                "variants": res["variants"], "r9": res["r9"], "measures_cm": res["measures_cm"],
                                "head_vs_norms": res["head_vs_norms"], "head_norms": HEAD_NORMS,
                                "head_vs_mh_neutral_B": res["head_vs_mh_neutral_B"]}
    for p, d in res["params"].items():
        doc["params"][p]["model_estimate_T4"] = d
    if "comparator_table" in doc:
        for p, st in comparator_stats(doc).items():
            doc["params"][p].setdefault("comparators", {})["stats"] = st
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
        fh.write("\n")


def check_against(path, res, tol=0.05):
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    bad = []
    if "comparator_table" in doc:
        for p, st in comparator_stats(doc).items():
            if doc["params"][p].get("comparators", {}).get("stats") != st:
                bad.append((p, "comparators.stats"))
    for p, d in res["params"].items():
        s = doc["params"][p].get("model_estimate_T4", {})
        for v in ("primary_B", "variant_A", "variant_G"):
            if abs(s.get(v, {}).get("steps", 1e9) - d[v]["steps"]) > tol:
                bad.append((p, v, s.get(v, {}).get("steps"), d[v]["steps"]))
    return bad


def input_is_stale(path):
    """r9_params.json 的哈希与写入时不同（例如面部组改了脸）：数值可能仍一致，但应重新 --write 并复核文字。"""
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    with open(R9_PARAMS, "rb") as fh:
        sha = hashlib.sha256(fh.read()).hexdigest()
    return doc.get("model_estimate_T4", {}).get("input_model", {}).get("sha256") != sha


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", help="把结果写进 r9_body_params.json 的 model_estimate_T4 块（其余内容不动）")
    ap.add_argument("--check", help="与该 JSON 中已存的结果比较；不同则退出码 1")
    a = ap.parse_args()
    res = estimate()
    if a.check:
        bad = check_against(a.check, res)
        if input_is_stale(a.check):
            print("警告：model/source/r9_params.json 已变化（哈希不同）；请重新运行 --write 并复核 BODY_PARAMS.md 中引用的数字")
        print("OK" if not bad else f"MISMATCH {bad}")
        return 1 if bad else 0
    if a.write:
        write_into(a.write, res)
        print(f"已更新 {a.write}")
    print(f"{'param':<17}{'B steps':>8}{'A steps':>8}{'G steps':>8}   stored B / span")
    for p, d in res["params"].items():
        print(f"{p:<17}{d['primary_B']['steps']:>8.2f}{d['variant_A']['steps']:>8.2f}{d['variant_G']['steps']:>8.2f}"
              f"   {d['primary_B']['stored']:>2} / {d['stored_span']}")
    print(json.dumps({"variants": res["variants"], "head_vs_norms": res["head_vs_norms"]}, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
