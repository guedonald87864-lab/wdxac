#!/usr/bin/env python3
"""从 06 号档案 §7 的 schema 生成 data/body/layouts/{edit72,pesdb60,ef64}.json。

档案 §7 是唯一的位置来源；本脚本只补充：值变换（transform）、半字节顺序（nibble）、
每个布局的置信度、宽度/取值冲突标记、未分配位统计。生成结果是确定的，
tools/tests/test_player_appearance.py 会重新对照档案检查每个字段的位置和宽度，
并检查本脚本的输出与已提交的 JSON 一致。

  python3 tools/data/gen_appearance_layouts.py            # 重新生成
  python3 tools/data/gen_appearance_layouts.py --check    # 只比较，不写文件（不同则退出码 1）
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MD = os.path.join(ROOT, "research", "notes", "06_player_appearance_format.md")
OUT = os.path.join(ROOT, "data", "body", "layouts")
txt = open(MD, encoding="utf-8").read()
sec = txt[txt.index("## 7. "):txt.index("## 8. ")]
S = json.loads(re.search(r"```json\n(.*?)\n```", sec, re.S).group(1))
BODY = ["NeckLength","NeckSize","ShoulderHeight","ShoulderWidth","ChestMeasurement","WaistSize",
        "ArmSize","ArmLength","ThighSize","CalfSize","LegLength","HeadLength","HeadWidth","HeadDepth"]
WIKI_CONFLICT = {  # 06 §2.3: width conflicts wiki vs 4cc-studio (schema keeps 4cc-studio measured width)
 "gk_gloves_id": "wiki 10 bit + 4-bit 'Unknown A'; 4ccEditor reads 14 bit",
 "UpperEyelidType": "wiki 3 bit; 4cc-studio 5 bit", "LowerEyelidType": "wiki 3 bit; 4cc-studio 4 bit",
 "EyebrowType": "wiki 3 bit; 4cc-studio 5 bit", "NeckLineType": "wiki 2 bit; 4cc-studio 3 bit",
 "NoseType": "wiki 3 bit + 5-bit 'Unknown N'; 4cc-studio 6 bit", "HairStyle": "wiki 3 bit; 4cc-studio 4 bit",
 "HairCroppedStyle": "wiki 3 bit (1-6); 4cc-studio 4 bit; PES21 crashes if the high bit is set (4cc-studio)",
 "HairVariation": "wiki 5 bit (1-29); 4cc-studio 8 bit", "Hairline": "wiki 2 bit; 4cc-studio 3 bit",
 "HairForeheadWidth": "wiki 2 bit; 4cc-studio 4 bit",
 "SkinColor": "value meaning conflicts: wiki 0=white/transparent,1-6 tones,7 none; GodotFootball 1-6 (0/7 special); 4ccEditor 7->0 for PES18+",
}
def transform(f):
    e = f["enc"]
    if e.startswith("slider"):
        return {"kind": "offset", "display_offset": -7, "valid_min": 0, "valid_max": 14, "neutral": 7,
                "rule": "stored 0..14, display = stored - 7 (-7..+7), neutral 7; 15 is not legal (PES 2021 decoder reads 15 as 0, 06 §2.2)"}
    if f["name"] == "HairWaveLevel":
        return {"kind": "offset", "display_offset": -1, "rule": "stored = display + 1 (06 §2.3, wiki)"}
    if e == "bool":
        return {"kind": "bool"}
    if e.startswith("colour"):
        return {"kind": "uint", "rule": "colour channel 0..63"}
    if e.startswith("unused"):
        return {"kind": "unused", "rule": "measured unused by 4cc-studio; " + f.get("note", "")}
    if e.startswith("u32") or e == "id":
        return {"kind": "uint"}
    return {"kind": "enum", "rule": "raw type/enum code (meanings: 06 §2.3)"}
def group(f):
    if f["name"] in BODY: return "body"
    if f["name"].startswith("unused"): return "unused"
    if f["name"] in ("player_id","edited_face_flag","edited_hair_flag","edited_physique_flag","edited_strip_flag","boots_id","gk_gloves_id","base_copy_player_id"): return "header"
    if f["name"] in ("WristTapeColorLeft","WristTapeColorRight","WristTaping","SpectaclesColor","Spectacles","Sleeves","LongSleevedInners","SockLength","Undershorts","Shirttail","AnkleTaping","PlayerGloves","PlayerGlovesColor"): return "kit"
    if f["name"].startswith("Hair") or f["name"].startswith("Eyebrow") or f["name"].startswith("FacialHair"): return "hair"
    return "face"
META = {
 "edit72": dict(record_size=72, title="PES 2016-2021 EDIT save appearance block (72 B, decrypted data.dat)",
   status="measured-community", confidence="high (T3: 4ccEditor, 4cc-studio in-game measurement 2026-10-13, 4cc/Rigged wiki, GodotFootball live reads; 06 §2.2-2.3)",
   where=S["layouts"]["edit72"]["where"], pid_field="player_id", pid_width=32,
   offset_space="decrypted EDIT data.dat (pesXdecrypter output); appearance block start = player record + 0xF0 (PES 20/21). NOT the encrypted EDIT00000000 file, NOT memory.",
   sorted_by_pid="no (save blocks follow player records)"),
 "pesdb60": dict(record_size=60, title="PES 2016/2017-2021 PlayerAppearance.bin row (60 B, decoded table)",
   status="measured-community", confidence="high (T3: GodotFootball face report, 4cc-studio model.md, pes-db-generator template; 06 §2.5)",
   rule=S["layouts"]["pesdb60"]["rule"], pid_field="pid", pid_width=32,
   offset_space="decoded table payload (row i at i*60). NOT CPK/file-container offsets, NOT memory.",
   sorted_by_pid="unknown (not claimed for PES tables) - find reports it, never assumes it"),
 "ef64": dict(record_size=64, title="eFootball PC PlayerAppearance.bin row (64 B) - CANDIDATE",
   status="candidate", confidence="MEDIUM for eFootball PC (T3+T4: Mod Studio structural test on real dt200 files + EvoTool v5.2.0 bit definitions that land exactly on the PES16-21 fields when shifted by 4; EvoTool's reader uses a 4-byte id = internal conflict, 06 §3.2-3.3). UNVERIFIED for eFootball iOS 11.1.1 (no public mobile decode, 06 §6.4)",
   rule=S["layouts"]["ef64"]["rule"], pid_field="pid", pid_width=64,
   offset_space="DECODED table payload after WESYS decrypt + zlib inflate (row i at i*64). NOT IPA/CPK/.utoc/.ucas/encrypted .bin offsets, NOT memory (06 §1.1).",
   sorted_by_pid="claimed strictly ascending by u64 PID (Mod Studio §6) - checked by find/roundtrip-check, never assumed"),
}
COMMON = {
 "bit_numbering": "LSB-first inside each byte, bytes concatenated little-endian (06 §1.2): bit position p = byte*8 + bit; value = (int.from_bytes(record, 'little') >> p) & ((1 << width) - 1). 'nibble' = which half of the byte a byte-aligned 4-bit field occupies (low = bits 0-3, high = bits 4-7); the 14 body fields are 7 bytes, first field of each pair in the LOW nibble.",
 "warning": "Offsets are positions inside the DECODED table/record. Never apply them to IPA, CPK, .utoc/.ucas, encrypted/compressed .bin files, and never write in-memory structure bit positions as file offsets (TASK_BRIEF §5; 06 §1.1: PES 2021 memory object is float[16]+u8 with the arm pair swapped).",
 "source": "research/notes/06_player_appearance_format.md §7 schema '%s' (dossier date %s), generated by tools/data/gen_appearance_layouts.py; widths = 4cc-studio measured values where wiki differs (06 §2.3)" % (S["schema_id"], S["generated"]),
}
results = {}
for lay, meta in META.items():
    fields = []
    if lay == "pesdb60":
        fields.append({"name": "pid", "byte": 0, "bit": 0, "width": 32, "group": "header", "transform": {"kind": "uint"},
                       "confidence": "high (pesdb60)", "nibble": None, "note": "u32 LE player id (06 §2.5)"})
    if lay == "ef64":
        fields.append({"name": "pid", "byte": 0, "bit": 0, "width": 64, "group": "header", "transform": {"kind": "uint"},
                       "confidence": "medium-high (Mod Studio: u64 external PID, strictly ascending) - CONFLICT: EvoTool v520 Load() reads ReadUInt32 + commented-out u32 cardType (06 §3.2b). Check bytes 4-7 per 06 §8 step 4",
                       "nibble": None, "note": "u64 LE external PID / card id (card ids exceed 2^32, e.g. 88044145348046)"})
    for f in S["fields"]:
        if lay not in f: continue
        b, bit = f[lay]
        conf = f["conf"]
        if lay == "edit72": c = conf.split(";")[0].strip()
        elif lay == "pesdb60": c = "high (pesdb60 = edit72 - 8)" if "high" in conf else conf
        else:
            c = "medium (ef64 candidate); UNVERIFIED for iOS 11.1.1"
        d = {"name": f["name"], "byte": b, "bit": bit, "width": f["width"], "group": group(f),
             "transform": transform(f), "confidence": c}
        d["nibble"] = ("low" if bit == 0 else "high") if (f["width"] == 4 and bit in (0, 4)) else None
        if lay == "ef64" and "ef64_u32" in f:
            d["u32_word"] = [int(f["ef64_u32"][0], 16), f["ef64_u32"][1]]
        other = {k: f[k] for k in ("edit72", "pesdb60", "ef64") if k in f and k != lay}
        if other: d["same_field_in"] = other
        note = "; ".join(x for x in (f.get("note", ""), WIKI_CONFLICT.get(f["name"], "")) if x)
        if f["name"] in WIKI_CONFLICT:
            d["conflict"] = "value-meaning" if f["name"] == "SkinColor" else "width (wiki vs 4cc-studio)"
        if note: d["note"] = note
        fields.append(d)
    fields.sort(key=lambda d: d["byte"] * 8 + d["bit"])
    out = {"layout_id": lay, **{k: v for k, v in meta.items() if k not in ("record_size",)}, "record_size": meta["record_size"], **COMMON, "fields": fields}
    # coverage
    rs = meta["record_size"] * 8
    occ = [None] * rs
    for d in fields:
        p = d["byte"] * 8 + d["bit"]
        for q in range(p, p + d["width"]):
            assert q < rs, (lay, d["name"])
            assert occ[q] is None, ("overlap", lay, d["name"], occ[q])
            occ[q] = d["name"]
    gaps = []
    q = 0
    while q < rs:
        if occ[q] is None:
            s = q
            while q < rs and occ[q] is None: q += 1
            gaps.append([s // 8, s % 8, q - s])
        else: q += 1
    out["unmapped_bits"] = {"runs": gaps, "note": "bits no source assigns to a field; decode reports them raw, encode/patch always keep them unchanged"}
    results[lay] = json.dumps(out, indent=1, ensure_ascii=False) + "\n"
    print(lay, len(fields), "fields; unmapped runs:", gaps)
check = "--check" in sys.argv
bad = []
for lay, text in results.items():
    path = os.path.join(OUT, lay + ".json")
    if check:
        if not os.path.exists(path) or open(path, encoding="utf-8").read() != text:
            bad.append(path)
    else:
        os.makedirs(OUT, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
if bad:
    print("out of date:", bad)
    sys.exit(1)
