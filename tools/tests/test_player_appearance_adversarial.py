"""对抗性复核：tools/data/player_appearance.py 与 data/body/layouts/*.json（全部用合成数据）。

这些测试针对复核时实际找到的缺陷，以及原测试没有覆盖的独立证据：
  * 输出路径：decode --out 覆盖输入表（含 --all）、<out>.patch.json 清单覆盖输入、--out 覆盖 --set-json、
    覆盖已有的原始记录备份 —— 修复前都会**毁掉用户的原始数据**；
  * 取值解析：encode JSON 中 {"raw": 10.9} 被悄悄截断成 10、true 被当成 1、"abc" 让程序崩溃（退出码 1）；
  * --display 与 codec_patch（存储值）同时使用时把 4 写成 11（修复前不报错）；
  * 结构闸门：用错布局（60 B 表当 64 B 表读）时 patch --index 照样写入；PID 乱序的表照样写入；
  * 输入守卫误拒：PID 的字节恰好等于 gzip/zip/LZ4/UE/Mach-O/ASTC 魔数时，合法的解码表被拒且无法绕过；
  * find 的提示：u32 PID + u32 cardType 的表被误报为"非记录起点"；
  * PES 存档 data.dat：球员区之后的其他区段被当成记录，PID 在那里巧合命中；用 --count 限定；
  * 位序的第二个独立参考：逐位移植 4ccEditor 的 read_data/write_data（data_util.cpp L6-95），
    并按 4ccEditor pes20.cpp L146-201 的读取顺序**模拟游标**推出头部/身体/穿着字段的位置，与 edit72 布局对比
    （不经过 06 档案的偏移表，是另一条推导路径）。
"""

import gzip
import io
import json
import os
import random
import re
import subprocess
import sys
import zipfile
import zlib

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DATA = os.path.join(ROOT, "tools", "data")
sys.path.insert(0, DATA)
sys.path.insert(0, HERE)

import player_appearance as PA  # noqa: E402
from test_player_appearance import make_table  # noqa: E402

CLI = [sys.executable, os.path.join(DATA, "player_appearance.py")]


def run_cli(*args, flags=()):
    p = subprocess.run([sys.executable, *flags, CLI[1]] + [str(a) for a in args], capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


@pytest.fixture()
def ef(tmp_path):
    lay = PA.load_layout("ef64")
    t = make_table(lay, 60, seed=31)
    p = tmp_path / "PlayerAppearance.dec"
    p.write_bytes(t)
    return lay, t, p


def _pid(t, i, lid="ef64"):
    return PA.Table(t, PA.load_layout(lid)).pid(i)


# =============================================================================================
# 1. 输出路径：任何输入都不能被覆盖
# =============================================================================================

def test_decode_out_cannot_overwrite_input_table(ef, tmp_path):
    lay, t, p = ef
    pid = _pid(t, 3)
    rc, _, err = run_cli("decode", "--layout", "ef64", "--table", p, "--pid", pid, "--out", p)
    assert rc == 3 and "同一个文件" in err
    assert p.read_bytes() == t
    rc, _, err = run_cli("decode", "--layout", "ef64", "--table", p, "--all", "--out", p)
    assert rc == 3 and "同一个文件" in err
    assert p.read_bytes() == t
    # 通过符号链接指向输入也不行
    link = tmp_path / "link.dec"
    os.symlink(p, link)
    rc, _, err = run_cli("decode", "--layout", "ef64", "--table", p, "--pid", pid, "--out", link)
    assert rc == 3 and p.read_bytes() == t
    # 硬链接也不行（即使加 --overwrite）
    hard = tmp_path / "hard.dec"
    os.link(p, hard)
    rc, _, err = run_cli("patch", "--layout", "ef64", "--table", p, "--pid", pid, "--set", "CalfSize=9",
                         "--out", hard, "--overwrite")
    assert rc == 3 and "同一个文件" in err and p.read_bytes() == t


def test_decode_out_does_not_clobber_existing_backup(ef, tmp_path):
    """decode 的输出通常就是"原始记录备份"（BODY_PARAMS §1 第 1 步）；第二次 decode 不能悄悄覆盖它。"""
    lay, t, p = ef
    bak = tmp_path / "r9_before.json"
    rc, _, err = run_cli("decode", "--layout", "ef64", "--table", p, "--index", 4, "--out", bak)
    assert rc == 0, err
    first = bak.read_text(encoding="utf-8")
    rc, _, err = run_cli("decode", "--layout", "ef64", "--table", p, "--index", 5, "--out", bak)
    assert rc == 3 and "已存在" in err and bak.read_text(encoding="utf-8") == first
    rc, _, err = run_cli("decode", "--layout", "ef64", "--table", p, "--index", 5, "--out", bak, "--overwrite")
    assert rc == 0 and json.loads(bak.read_text(encoding="utf-8"))["source"]["record_index"] == 5


def test_patch_manifest_cannot_overwrite_input(tmp_path):
    lay = PA.load_layout("ef64")
    t = make_table(lay, 30, seed=2)
    inp = tmp_path / "x.patch.json"          # 输入表恰好叫 <out>.patch.json
    inp.write_bytes(t)
    rc, _, err = run_cli("patch", "--layout", "ef64", "--table", inp, "--index", 1, "--set", "ThighSize=12",
                         "--out", tmp_path / "x")
    assert rc == 3 and "清单" in err
    assert inp.read_bytes() == t and not (tmp_path / "x").exists()


def test_patch_manifest_does_not_clobber_existing_file(ef, tmp_path):
    lay, t, p = ef
    out = tmp_path / "o.dec"
    man = tmp_path / "o.dec.patch.json"
    man.write_text("keep", encoding="utf-8")
    rc, _, err = run_cli("patch", "--layout", "ef64", "--table", p, "--index", 1, "--set", "ThighSize=12", "--out", out)
    assert rc == 3 and "已存在" in err and man.read_text(encoding="utf-8") == "keep" and not out.exists()


def test_patch_out_cannot_overwrite_set_json_even_with_overwrite(ef, tmp_path):
    lay, t, p = ef
    sj = tmp_path / "params.json"
    sj.write_text(json.dumps({"ThighSize": 9}), encoding="utf-8")
    before = sj.read_bytes()
    rc, _, err = run_cli("patch", "--layout", "ef64", "--table", p, "--index", 2, "--set-json", sj,
                         "--out", sj, "--overwrite")
    assert rc == 3 and "同一个文件" in err and sj.read_bytes() == before


def test_encode_out_cannot_overwrite_its_json(ef, tmp_path):
    lay, t, p = ef
    js = lay.decode(PA.Table(t, lay).record(3))
    jp = tmp_path / "e.json"
    jp.write_text(json.dumps(js), encoding="utf-8")
    before = jp.read_bytes()
    rc, _, err = run_cli("encode", jp, "--out", jp, "--overwrite")
    assert rc == 3 and jp.read_bytes() == before


# =============================================================================================
# 2. 取值解析：绝不截断、不把布尔当整数、不崩溃
# =============================================================================================

@pytest.mark.parametrize("spec,needle", [
    ({"raw": 10.9}, "不是整数"),
    ({"raw": 10.0}, "不是整数"),
    ({"raw": True}, "布尔"),
    ({"raw": "abc"}, "不是整数"),
    ({"display": 2.7}, "不是整数"),
    ({"raw": [9]}, "不是整数"),
    ({"raw": None, "display": None}, "需要 raw 或 display"),
])
def test_encode_json_values_are_parsed_strictly(ef, tmp_path, spec, needle):
    lay, t, p = ef
    rec = PA.Table(t, lay).record(2)
    jp = tmp_path / "v.json"
    jp.write_text(json.dumps({"layout": "ef64", "record_hex": rec.hex(), "fields": {"ThighSize": spec}}),
                  encoding="utf-8")
    out = tmp_path / "v.bin"
    rc, _, err = run_cli("encode", jp, "--out", out)
    assert rc == 3 and needle in err, (spec, err)
    assert not out.exists()
    assert "Traceback" not in err


def test_int_strings_accepted_forms():
    lay = PA.load_layout("ef64")
    assert lay.resolve_value("ThighSize", "08") == 8          # 前导零按十进制（修复前被拒）
    assert lay.resolve_value("ThighSize", " 11 ") == 11
    assert lay.resolve_value("ThighSize", "0x0B") == 11
    assert lay.resolve_value("ThighSize", "+3", display=True) == 10
    assert lay.resolve_value("ThighSize", {"raw": "9", "display": "2"}) == 9
    for bad in ("1e1", "9.0", "", "0x", "七"):
        with pytest.raises(PA.AppearanceError):
            lay.resolve_value("ThighSize", bad)


def test_cli_bad_pid_is_an_argument_error_not_a_crash(ef):
    lay, t, p = ef
    rc, _, err = run_cli("decode", "--layout", "ef64", "--table", p, "--pid", "abc")
    assert rc == 2 and "Traceback" not in err


def test_display_flag_refused_with_codec_patch(ef, tmp_path):
    """codec_patch 是存储值。修复前 --display 会把 ThighSize 4 写成 11（4 + 7），只在某个值越界时才偶然报错。"""
    lay, t, p = ef
    cp = tmp_path / "cp.json"
    cp.write_text(json.dumps({"codec_patch": {"ThighSize": 4, "NeckLength": 5}}), encoding="utf-8")
    rc, _, err = run_cli("patch", "--layout", "ef64", "--table", p, "--index", 2, "--set-json", cp, "--display",
                         "--out", tmp_path / "o.dec")
    assert rc == 3 and "codec_patch" in err and not (tmp_path / "o.dec").exists()
    # 不带 codec_patch 的普通 {名: 显示值} JSON 仍可用 --display
    plain = tmp_path / "plain.json"
    plain.write_text(json.dumps({"ThighSize": 4}), encoding="utf-8")
    rc, _, err = run_cli("patch", "--layout", "ef64", "--table", p, "--index", 2, "--set-json", plain, "--display",
                         "--out", tmp_path / "o2.dec")
    assert rc == 0, err
    assert lay.get((tmp_path / "o2.dec").read_bytes()[2 * 64:3 * 64], "ThighSize") == 11


def test_set_json_must_be_an_object(ef, tmp_path):
    lay, t, p = ef
    bad = tmp_path / "list.json"
    bad.write_text("[1, 2]", encoding="utf-8")
    rc, _, err = run_cli("patch", "--layout", "ef64", "--table", p, "--index", 2, "--set-json", bad, "--dry-run")
    assert rc == 3 and "Traceback" not in err


# =============================================================================================
# 3. 写入前的结构闸门（06 §8 第 4 步）
# =============================================================================================

def test_patch_refuses_table_read_with_the_wrong_layout(tmp_path):
    """真实风险：iOS 表实际是 60 B 行（身体在 0x04），用户按 ef64 读。长度 3840 同时是 60 和 64 的倍数，
    --index 定位不需要 PID 命中，修复前会把 ThighSize 写到错误的半字节上。"""
    t60 = make_table(PA.load_layout("pesdb60"), 64, seed=2)
    assert len(t60) % 64 == 0
    p = tmp_path / "t60.dec"
    p.write_bytes(t60)
    out = tmp_path / "o.dec"
    rc, _, err = run_cli("patch", "--layout", "ef64", "--table", p, "--index", 3, "--set", "ThighSize=11", "--out", out)
    assert rc == 3 and "结构检验不通过" in err and not out.exists()
    # 明确跳过时才写，并在清单里留下记录
    rc, _, err = run_cli("patch", "--layout", "ef64", "--table", p, "--index", 3, "--set", "ThighSize=11", "--out", out,
                         "--skip-structure-check")
    assert rc == 0, err
    man = json.loads((tmp_path / "o.dec.patch.json").read_text(encoding="utf-8"))
    assert man["structure_check"]["skipped"] is True and not all(man["structure_check"]["checks"].values())


def test_patch_refuses_ef64_table_with_pid_out_of_order(tmp_path):
    lay = PA.load_layout("ef64")
    t = make_table(lay, 20, pids=[100, 200, 300, 250] + [400 + i for i in range(16)])
    p = tmp_path / "t.dec"
    p.write_bytes(t)
    rc, _, err = run_cli("patch", "--layout", "ef64", "--table", p, "--pid", 400, "--set", "ThighSize=11", "--dry-run")
    assert rc == 3 and "pid_strictly_ascending" in err


def test_manifest_records_passing_structure_check(ef, tmp_path):
    lay, t, p = ef
    out = tmp_path / "ok.dec"
    rc, _, err = run_cli("patch", "--layout", "ef64", "--table", p, "--index", 7, "--set", "CalfSize=9", "--out", out)
    assert rc == 0, err
    man = json.loads((tmp_path / "ok.dec.patch.json").read_text(encoding="utf-8"))
    assert man["structure_check"]["skipped"] is False and all(man["structure_check"]["checks"].values())


def test_single_record_candidate_layout_warns(ef, tmp_path):
    lay, t, p = ef
    r = tmp_path / "r.bin"
    r.write_bytes(t[:64])
    rc, _, err = run_cli("patch", "--layout", "ef64", "--record", r, "--set", "CalfSize=9", "--dry-run")
    assert rc == 0 and "候选布局" in err


def test_structure_check_matches_roundtrip_report(ef):
    lay, t, p = ef
    tab = PA.Table(t, lay)
    st, checks = PA.structure_check(tab)
    rep = PA.roundtrip_check(tab)
    assert rep["structure"] == st and rep["structure_checks"] == checks and rep["structure_ok"]


# =============================================================================================
# 4. 输入守卫：不误拒"PID 恰好像魔数"的解码表，仍拒绝真实的压缩/容器文件
# =============================================================================================

MAGIC_PIDS = {
    "gzip": 0x088B1F,             # 1F 8B 08 00 …（PID 559903）
    "zip": 0x04034B50,            # 50 4B 03 04
    "zip-eocd": 0x06054B50,       # 50 4B 05 06
    "lz4": 0x184D2204,            # 04 22 4D 18
    "uasset": 0x9E2A83C1,         # C1 83 2A 9E
    "macho": 0xFEEDFACF,          # CF FA ED FE
    "macho-fat": 0xBEBAFECA,      # CA FE BA BE
    "astc": 0x5CA1AB13,           # 13 AB A1 5C
}


@pytest.mark.parametrize("name", sorted(MAGIC_PIDS))
@pytest.mark.parametrize("lid", ["ef64", "pesdb60"])
def test_guard_accepts_tables_whose_first_pid_looks_like_a_magic(name, lid):
    lay = PA.load_layout(lid)
    pid0 = MAGIC_PIDS[name]
    t = make_table(lay, 40, seed=5, pids=[pid0 + i for i in range(40)])
    assert t[:4] == pid0.to_bytes(4, "little")
    PA.check_decoded(t, name)                          # 整表
    PA.check_decoded(t[:lay.record_size], name)        # 单条记录


def _lz4_frame(payload):
    # 真实 LZ4 frame 头：魔数 + FLG(版本 01, 块独立) + BD(最大块 64 KiB) + HC；后面是未压缩块
    return b"\x04\x22\x4d\x18" + bytes([0x60, 0x40, 0x82]) + (len(payload) | 0x80000000).to_bytes(4, "little") + payload


@pytest.mark.parametrize("kind", ["gzip", "zip", "zip-empty", "lz4", "uasset", "macho", "macho-fat", "astc"])
def test_guard_still_refuses_real_formats(kind):
    t = make_table(PA.load_layout("ef64"), 100, seed=6)
    if kind == "gzip":
        blob = gzip.compress(t)
    elif kind == "zip":
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("Payload/eFootball.app/x.bin", t)
        blob = b.getvalue()
    elif kind == "zip-empty":
        b = io.BytesIO()
        zipfile.ZipFile(b, "w").close()
        blob = b.getvalue()
    elif kind == "lz4":
        blob = _lz4_frame(t[:4000])
    elif kind == "uasset":
        blob = b"\xc1\x83\x2a\x9e" + (-8).to_bytes(4, "little", signed=True) + t[:3000]
    elif kind == "macho":
        blob = b"\xcf\xfa\xed\xfe" + (0x0100000C).to_bytes(4, "little") + t[:3000]
    elif kind == "macho-fat":
        blob = b"\xca\xfe\xba\xbe" + (2).to_bytes(4, "big") + t[:3000]
    else:
        blob = b"\x13\xab\xa1\x5c" + bytes([6, 6, 1]) + (256).to_bytes(3, "little") * 3 + t[:3000]
    with pytest.raises(PA.NotDecodedError):
        PA.check_decoded(blob, kind)


def test_guard_zlib_rule_needs_stream_end_or_full_block():
    """修复前的规则是"解出 > 0 字节就算 zlib"：固定哈夫曼块能把普通字节解成几个字面量、输入耗尽前不报错。
    下面的抽样里确实存在这种记录（先断言存在，证明测试走到了这条路径），它们现在都不应被拒。"""
    rng = random.Random(1)
    partial = 0
    for _ in range(20000):
        rec = b"\x78\x9c" + bytes(rng.choice([0x77, 0x77, 0x78, 0x87, 0x76, 0x67, 0, 1, 3, 5, rng.randrange(256)])
                                   for _ in range(62))
        try:
            d = zlib.decompressobj()
            if d.decompress(rec, 4096) and not d.eof:
                partial += 1
        except zlib.error:
            continue
        PA.check_decoded(rec, "record")
    assert partial >= 1
    # 真正的 zlib 流（整段、截断成 64 KiB、空流）照样拒绝
    t = make_table(PA.load_layout("ef64"), 2000, seed=8)
    for blob in (zlib.compress(t), zlib.compress(t + random.Random(2).randbytes(200000))[:65536], zlib.compress(b"")):
        with pytest.raises(PA.NotDecodedError):
            PA.check_decoded(blob, "zlib")


# =============================================================================================
# 5. find 提示：u32 PID + u32 cardType（06 §8 第 4 步要区分的情形）
# =============================================================================================

def test_find_hint_for_u32_pid_plus_cardtype(tmp_path):
    lay = PA.load_layout("ef64")
    t = bytearray(make_table(lay, 30, seed=5, pids=[15038 + i * 100 for i in range(30)]))
    for i in range(30):
        t[i * 64 + 4:i * 64 + 8] = (0x00310090).to_bytes(4, "little")   # 非零的"cardType"
    p = tmp_path / "ct.dec"
    p.write_bytes(bytes(t))
    rc, out, _ = run_cli("find", "--layout", "ef64", "--table", p, "--pid", 15038)
    rep = json.loads(out)
    assert rc == 1 and rep["pattern_hits_u32"][0]["aligned"]
    assert any("cardType" in h for h in rep["hint"]) and not any("非记录起点" in h for h in rep["hint"])
    rc, out, _ = run_cli("roundtrip-check", "--layout", "ef64", "--table", p)
    assert json.loads(out)["structure"]["bytes_4_7_zero_fraction"] == 0.0


# =============================================================================================
# 6. PES 存档 data.dat：--count 限定球员区
# =============================================================================================

def test_count_bounds_records_in_pes_data_dat(tmp_path):
    lay = PA.load_layout("edit72")
    rng = random.Random(3)
    n = 12
    blocks = make_table(lay, n, seed=9)
    tail = bytearray(rng.getrandbits(3) for _ in range(0x138 * 4))   # 球员区之后的"球队区"
    start = 0x7C + 0xF0
    dat = bytearray(bytes(rng.getrandbits(3) for _ in range(0x7C)) + bytes(n * 0x138) + tail)
    for i in range(n):
        o = 0x7C + i * 0x138
        dat[o + 0xF0:o + 0x138] = blocks[i * 72:(i + 1) * 72]
    victim = PA.Table(bytes(dat), lay, start, 0x138).pid(5)
    target = 4 if PA.Table(bytes(dat), lay, start, 0x138).layout.get(blocks[5 * 72:6 * 72], "NeckLength") != 4 else 5
    junk = start + (n + 1) * 0x138                                      # 球员区之外、恰好落在步长上的位置
    dat[junk:junk + 4] = victim.to_bytes(4, "little")
    p = tmp_path / "data.dat"
    p.write_bytes(bytes(dat))
    rc, _, err = run_cli("patch", "--layout", "edit72", "--table", p, "--start", start, "--stride", 0x138,
                         "--pid", victim, "--set", f"NeckLength={target}", "--dry-run")
    assert rc == 3 and "有歧义" in err                                   # 不限条数：球队区的巧合命中
    out = tmp_path / "d2.dat"
    rc, _, err = run_cli("patch", "--layout", "edit72", "--table", p, "--start", start, "--stride", 0x138,
                         "--count", n, "--pid", victim, "--set", f"NeckLength={target}", "--out", out)
    assert rc == 0, err
    d2 = out.read_bytes()
    diff = [i for i in range(len(dat)) if dat[i] != d2[i]]
    assert diff == [start + 5 * 0x138 + 0x0C]
    rc, _, err = run_cli("roundtrip-check", "--layout", "edit72", "--table", p, "--start", start, "--stride", 0x138,
                         "--count", n + 99)
    assert rc == 3 and "--count" in err


# =============================================================================================
# 7. 第二个独立参考：4ccEditor 的逐位读写与读取顺序
# =============================================================================================

def read_data_4cc(data, start_bit, bits, cur):
    """逐行移植 4ccEditor data_util.cpp read_data（L6-30）。返回 (值, 新游标)。"""
    pow2 = [1, 2, 4, 8, 16, 32, 64, 128]
    out, adv, bit = 0, 0, start_bit
    for ii in range(bits):
        if bit == 8:
            bit, cur = 0, cur + 1
        if ii % 8 == 0 and ii > 0:
            adv += 1
        shift = bit - ii % 8
        out += (((data[cur] >> shift) if shift >= 0 else (data[cur] << -shift)) & pow2[ii % 8]) << adv * 8
        bit += 1
    if bit == 8:
        cur += 1
    return out, cur


def write_data_4cc(data, value, start_bit, bits, cur):
    """逐行移植 4ccEditor data_util.cpp write_data（L66-95）。原地改 data，返回新游标。"""
    pow2 = [1, 2, 4, 8, 16, 32, 64, 128]
    adv, bit = 0, start_bit
    bv = data[cur]
    for ii in range(bits):
        if bit == 8:
            bit = 0
            data[cur] = bv
            cur += 1
            bv = data[cur]
        if ii % 8 == 0 and ii > 0:
            adv += 1
        shift = ii % 8 - bit
        src = value >> adv * 8
        bv = (bv & ~pow2[bit] & 0xFF) + (((src >> shift) if shift >= 0 else (src << -shift)) & pow2[bit])
        bit += 1
    data[cur] = bv
    if bit == 8:
        cur += 1
    return cur


# 4ccEditor pes20.cpp L146-201 外观块的读取顺序：(我们的字段名, read_data 的 start_bit, 位数) 或 ("+", 跳过字节数)。
# 这里只抄"调用序列"，字段位置由下面的游标模拟推出，不抄任何偏移。4ccEditor 的变量名写在注释里。
PES20_READ_SEQUENCE = [
    ("+", 4),                                   # playerID（current_byte += 4）
    ("edited_face_flag", 0, 1), ("edited_hair_flag", 1, 1), ("edited_physique_flag", 2, 1),
    ("edited_strip_flag", 3, 1),                # b_edit_face/hair/phys/strip
    ("boots_id", 4, 14), ("gk_gloves_id", 2, 14), ("base_copy_player_id", 0, 32),   # boot_id, glove_id, copy_id
    ("NeckLength", 0, 4), ("NeckSize", 4, 4), ("ShoulderHeight", 0, 4), ("ShoulderWidth", 4, 4),
    ("ChestMeasurement", 0, 4), ("WaistSize", 4, 4), ("ArmSize", 0, 4), ("ArmLength", 4, 4),
    ("ThighSize", 0, 4), ("CalfSize", 4, 4), ("LegLength", 0, 4), ("HeadLength", 4, 4),
    ("HeadWidth", 0, 4), ("HeadDepth", 4, 4),
    ("WristTapeColorLeft", 0, 3), ("WristTapeColorRight", 3, 3), ("WristTaping", 6, 2),
    ("SpectaclesColor", 0, 3), ("Spectacles", 3, 3), ("Sleeves", 6, 2),
    ("LongSleevedInners", 0, 2), ("SockLength", 2, 2), ("Undershorts", 4, 2), ("Shirttail", 6, 1),
    ("AnkleTaping", 7, 1),
    ("PlayerGloves", 0, 1), ("PlayerGlovesColor", 1, 3),   # gloves, gloves_col
    ("+", 23),                                  # Skip face settings
    ("SkinColor", 0, 3),                        # skin_col
    ("+", 19),                                  # Skip additional face settings
    ("IrisColor", 0, 4),                        # iris_col
]


def _simulate_4cc_positions():
    """按 4ccEditor 游标语义推出每个字段的 (字节, 起始位, 宽度)。"""
    cur, pos = 0, {}
    for item in PES20_READ_SEQUENCE:
        if item[0] == "+":
            cur += item[1]
            continue
        name, sb, w = item
        pos[name] = (cur, sb, w)
        # 用全 0 数据走一遍 read_data，只为得到游标前进量（与值无关）
        _, cur = read_data_4cc(bytes(80), sb, w, cur)
    return pos


def test_4cceditor_read_sequence_positions_match_edit72():
    lay = PA.load_layout("edit72")
    pos = _simulate_4cc_positions()
    assert len(pos) == 36
    for name, (b, sb, w) in pos.items():
        f = lay.by_name[name]
        assert (f.byte, f.bit, f.width) == (b, sb, w), (name, (f.byte, f.bit, f.width), (b, sb, w))
    # 4ccEditor 源码注释里的三个锚点
    assert pos["PlayerGloves"][:2] == (0x16, 0) and pos["SkinColor"][:2] == (0x2D, 0) and pos["IrisColor"][:2] == (0x40, 0)


@pytest.mark.parametrize("lid", PA.LAYOUT_IDS)
def test_4cceditor_bit_reader_and_writer_agree_with_codec(lid):
    lay = PA.load_layout(lid)
    rng = random.Random(0x4CC + len(lid))
    for _ in range(40):
        rec = rng.randbytes(lay.record_size)
        vals = lay.decode_values(rec)
        for f in lay.fields:
            got, _ = read_data_4cc(rec, f.bit, f.width, f.byte)
            assert got == vals[f.name], (lid, f.name)
        # 写：4ccEditor 写入一个随机值 == 本工具写入同一个值（逐字节）
        f = rng.choice(lay.fields)
        v = rng.getrandbits(f.width)
        buf = bytearray(rec + b"\0")              # write_data 结尾可能读到下一字节，多留 1 字节
        write_data_4cc(buf, v, f.bit, f.width, f.byte)
        assert bytes(buf[:lay.record_size]) == lay.set_raw(rec, f.name, v), (lid, f.name)


# =============================================================================================
# 8. 与 4cc-studio 源码逐字段对照（只在本地有克隆时运行）
# =============================================================================================

_STUDIO = os.environ.get("R9_4CC_STUDIO") or os.path.join(
    "/tmp/claude-0/-home-user-wdxac/53d2b8c2-6abb-5fab-91db-f6077147a038/scratchpad/fc06/repos/4cc-studio")
_SCHEMA = os.path.join(_STUDIO, "crates", "libs", "pes_savefile", "src", "schema")


@pytest.mark.skipif(not os.path.isdir(_SCHEMA), reason="没有 4cc-studio 克隆（设 R9_4CC_STUDIO 指向它）")
def test_edit72_matches_4cc_studio_sources():
    lay = PA.load_layout("edit72")
    at = {f.pos: f for f in lay.fields}
    src = open(os.path.join(_SCHEMA, "pes20.rs"), encoding="utf-8").read()
    specs = re.findall(r"field:\s*PlayerField::(\w+),\s*bit_offset:\s*(\d+),\s*bit_width:\s*(\d+)", src)
    app = [(n, int(o) - 0xF0 * 8, int(w)) for n, o, w in specs if 0xF0 * 8 <= int(o) < 0xF0 * 8 + 72 * 8]
    assert len(app) == 33
    for n, p, w in app:
        assert p in at and at[p].width == w, (n, p, w)
    face = open(os.path.join(_SCHEMA, "ingame_face.rs"), encoding="utf-8").read()
    tab = face[face.index("pub const INGAME_FACE_FIELDS"):face.index("pub(crate) struct UnusedBits")]
    rows = re.findall(r"IngameFaceField::(\w+),\s*bit_offset:\s*(\d+),\s*bit_width:\s*(\d+)", tab)
    rows = [r for r in rows if not r[0].startswith("Pes15Hair")]   # 3 个 PES 2015 专用发型字段
    assert len(rows) == 89
    for n, o, w in rows:
        f = at.get(22 * 8 + int(o))
        assert f is not None and f.name == n and f.width == int(w), (n, o, w)
