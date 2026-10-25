"""tools/data/player_appearance.py 的测试（全部用合成数据；没有任何游戏文件）。

覆盖：
  * 布局文件：与 06 号档案 §7 schema 逐字段一致、不重叠、位覆盖、三种布局的共享 56 字节互相对齐、生成器输出未过期；
  * 穷举半字节往返：每个布局、每个 4 位字段、每个取值 0..15、多条随机记录；记录内每个半字节位置 × 16 个取值；
    解码结果与一个**独立的参考实现**（06 §1.2 公式）逐字段比对；
  * 未改动位保持：随机改若干字段后，新旧记录的异或只落在被改字段的位上；decode→JSON→encode 原样往返；
  * 06 §8 测试向量：期望字节直接从档案正文解析（§5.2 的数值表 + §8 的期望字节表），不在测试里手抄；
  * 拒绝：CPK/WESYS/zlib/gzip/zip/zstd/.utoc/UE 包/Mach-O 特征、高熵数据、长度不对、未知字段、非法值、
    改头部、输出覆盖输入、JSON 过期、raw/display 矛盾、改 PID 破坏排序；
  * find / roundtrip-check / decode / encode / patch 命令行；PES 20/21 存档中嵌在 0x138 球员记录里的外观块（--start/--stride）。
注意：这些测试只证明编解码器对**给定布局**无损且只改该改的位；布局本身是否适用于 eFootball iOS 11.1.1
必须用真实解码数据按 06 §8 验证（见 data/body/BODY_PARAMS.md）。
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

import player_appearance as PA  # noqa: E402

CLI = [sys.executable, os.path.join(DATA, "player_appearance.py")]
DOSSIER = os.path.join(ROOT, "research", "notes", "06_player_appearance_format.md")
LAYOUTS = PA.LAYOUT_IDS
BODY_BASE = {"edit72": 0x0C, "pesdb60": 0x04, "ef64": 0x08}   # 06 §0 #1–#3
SHIFT_FROM_EDIT = {"edit72": 0, "pesdb60": 8, "ef64": 4}       # 布局偏移 = edit72 偏移 − shift


def _dossier():
    with open(DOSSIER, encoding="utf-8") as fh:
        return fh.read()


def _schema():
    txt = _dossier()
    sec = txt[txt.index("## 7. "):txt.index("## 8. ")]
    return json.loads(re.search(r"```json\n(.*?)\n```", sec, re.S).group(1))


def ref_get(rec, byte, bit, width):
    """独立参考实现（06 §1.2）：只取覆盖该字段的那几个字节，小端拼接后移位取位。"""
    k = (bit + width + 7) // 8
    return (int.from_bytes(rec[byte:byte + k], "little") >> bit) & ((1 << width) - 1)


def rec_int(b):
    return int.from_bytes(b, "little")


def make_table(lay, n, seed=1, pid0=1000, pids=None):
    """合成的"像真的"解码表：身体项多为 7，其余字段取小整数，PID 严格递增。"""
    rnd = random.Random(seed)
    recs, pid = [], pid0
    for i in range(n):
        pid = pids[i] if pids else pid + rnd.randint(1, 5000)
        ch = {lay.pid_field.name: pid}
        for f in lay.fields:
            if f is lay.pid_field:
                continue
            if f.kind == "offset" and f.transform.get("valid_max") == 14:
                ch[f.name] = min(14, max(0, round(rnd.gauss(7, 1.5)))) if rnd.random() < 0.4 else 7
            elif f.width >= 6:
                ch[f.name] = rnd.randint(0, min(f.max_raw, 40))
            else:
                ch[f.name] = rnd.randint(0, min(f.max_raw, 5))
        r, _ = lay.apply(bytes(lay.record_size), ch, allow_groups=("header", "unused"))
        recs.append(r)
    return b"".join(recs)


def run_cli(*args):
    p = subprocess.run(CLI + [str(a) for a in args], capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


# =============================================================================================
# 布局文件
# =============================================================================================

@pytest.mark.parametrize("lid", LAYOUTS)
def test_layout_structure(lid):
    lay = PA.load_layout(lid)  # 构造函数本身检查：越界、重叠、重名
    assert lay.record_size == {"edit72": 72, "pesdb60": 60, "ef64": 64}[lid]
    names = [f.name for f in lay.fields]
    base = BODY_BASE[lid]
    for i, n in enumerate(PA.BODY_FIELDS):  # 14 项连续半字节，低半字节在前，顺序同 06 §2.2
        f = lay.by_name[n]
        assert (f.byte, f.bit, f.width) == (base + i // 2, 4 * (i % 2), 4), n
        assert f.nibble == ("low" if i % 2 == 0 else "high")
        assert f.transform["kind"] == "offset" and f.transform["display_offset"] == -7
        assert (f.transform["valid_min"], f.transform["valid_max"], f.transform["neutral"]) == (0, 14, 7)
        assert f.confidence
    assert names.index("ArmSize") < names.index("ArmLength")  # 文件顺序：臂围在低半字节（06 §2.2）
    for f in lay.fields:  # 每个字段都带 6 个要素
        assert f.width > 0 and 0 <= f.bit < 8 and f.confidence and f.transform.get("kind")
        assert f.nibble in (None, "low", "high")
    # 三种布局的每一位都有归属。edit72 的门将手套 ID 按 4ccEditor/4cc-studio 读写 14 bit，覆盖维基的 "Unknown A"（0x07:4）
    assert lay.unmapped_runs == []
    assert "DECODED" in lay.meta["offset_space"] or "decrypted" in lay.meta["offset_space"]
    assert "NOT memory" in lay.meta["offset_space"]


def test_layout_status_and_confidence_labels():
    ef = PA.load_layout("ef64")
    assert ef.status == "candidate"
    assert "UNVERIFIED for eFootball iOS 11.1.1" in ef.confidence
    assert all("UNVERIFIED for iOS 11.1.1" in f.confidence for f in ef.fields if f.name != "pid")
    assert PA.load_layout("edit72").status == PA.load_layout("pesdb60").status == "measured-community"


@pytest.mark.parametrize("lid", LAYOUTS)
def test_layout_matches_dossier_schema(lid):
    S = _schema()
    lay = PA.load_layout(lid)
    n = 0
    for f in S["fields"]:
        if lid not in f:
            continue
        g = lay.by_name[f["name"]]
        assert [g.byte, g.bit] == f[lid], f["name"]
        assert g.width == f["width"], f["name"]
        n += 1
        if lid == "ef64" and "ef64_u32" in f:  # u32 字写法与字节:位写法是同一个位置（06 §1.2）
            w, b = int(f["ef64_u32"][0], 16), f["ef64_u32"][1]
            assert w * 8 + b == g.pos and g.u32_word == [w, b]
    extra = {"edit72": 0, "pesdb60": 1, "ef64": 1}[lid]  # pesdb60/ef64 额外有 pid 字段
    assert len(lay.fields) == n + extra


def test_layouts_share_the_same_56_bytes():
    E, P, F = (PA.load_layout(x) for x in LAYOUTS)
    shared = [f.name for f in F.fields if f.name != "pid"]
    assert set(shared) == {f.name for f in P.fields if f.name != "pid"}
    for name in shared:
        e, p, f = E.by_name[name], P.by_name[name], F.by_name[name]
        assert e.pos - 64 == p.pos and e.pos - 32 == f.pos and e.width == p.width == f.width, name


def test_generator_output_is_up_to_date():
    p = subprocess.run([sys.executable, os.path.join(DATA, "gen_appearance_layouts.py"), "--check"],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr


# =============================================================================================
# 穷举半字节往返
# =============================================================================================

@pytest.mark.parametrize("lid", LAYOUTS)
def test_every_4bit_field_every_value_on_random_records(lid):
    lay = PA.load_layout(lid)
    rng = random.Random(0xA11 + len(lid))
    recs = [rng.randbytes(lay.record_size) for _ in range(6)]
    nib = [f for f in lay.fields if f.width == 4]
    assert len(nib) >= 14 + 50  # 14 项身体 + 编辑脸滑杆等
    for rec in recs:
        v0 = rec_int(rec)
        for f in nib:
            for v in range(16):
                new = lay.set_raw(rec, f.name, v)
                assert lay.get(new, f.name) == v == ref_get(new, f.byte, f.bit, 4)
                assert (rec_int(new) ^ v0) & ~f.mask == 0  # 其他位一个都没动
                if f.kind == "offset" and f.transform.get("valid_max") == 14:
                    if v == 15 and lay.get(rec, f.name) != 15:  # 15 不合法：高层写入拒绝
                        with pytest.raises(PA.AppearanceError):
                            lay.apply(rec, {f.name: 15})
                    else:
                        out, _ = lay.apply(rec, {f.name: v})
                        assert out == new
                        assert lay.decode(out)["fields"][f.name]["display"] == v - 7


@pytest.mark.parametrize("lid", LAYOUTS)
def test_every_nibble_position_every_value_matches_reference(lid):
    """记录里每个半字节位置（不论属于哪个字段）× 16 个值：逐字段与独立参考实现一致，且从零重建逐字节相同。"""
    lay = PA.load_layout(lid)
    rng = random.Random(7)
    for rec in (rng.randbytes(lay.record_size), bytes(lay.record_size)):
        for k in range(lay.record_size * 2):
            for v in range(16):
                b = bytearray(rec)
                sh = 4 * (k % 2)
                b[k // 2] = (b[k // 2] & ~(0xF << sh) & 0xFF) | (v << sh)
                b = bytes(b)
                vals = lay.decode_values(b)
                for f in lay.fields:
                    assert vals[f.name] == ref_get(b, f.byte, f.bit, f.width), (k, v, f.name)
                d = lay.decode(b)
                x = 0
                for name, e in d["fields"].items():
                    x |= e["raw"] << lay.by_name[name].pos
                for u in d["unmapped"]:
                    x |= u["raw"] << (u["byte"] * 8 + u["bit"])
                assert lay.to_bytes(x) == b


@pytest.mark.parametrize("lid", LAYOUTS)
def test_all_fields_all_or_edge_values(lid):
    lay = PA.load_layout(lid)
    rng = random.Random(11)
    rec = rng.randbytes(lay.record_size)
    for f in lay.fields:
        vals = range(1 << f.width) if f.width <= 8 else \
            {0, 1, f.max_raw, f.max_raw - 1, 1 << (f.width - 1)} | {rng.getrandbits(f.width) for _ in range(64)}
        for v in vals:
            new = lay.set_raw(rec, f.name, v)
            assert lay.get(new, f.name) == v == ref_get(new, f.byte, f.bit, f.width)
            assert (rec_int(new) ^ rec_int(rec)) & ~f.mask == 0
        with pytest.raises(PA.AppearanceError):
            lay.set_raw(rec, f.name, f.max_raw + 1)
        with pytest.raises(PA.AppearanceError):
            lay.set_raw(rec, f.name, -1)


# =============================================================================================
# 未改动位保持 / 原样往返
# =============================================================================================

@pytest.mark.parametrize("lid", LAYOUTS)
def test_patch_changes_only_listed_fields(lid):
    lay = PA.load_layout(lid)
    rng = random.Random(3)
    editable = [f for f in lay.fields if f.group not in ("header", "unused")]
    for _ in range(300):
        rec = rng.randbytes(lay.record_size)
        pick = rng.sample(editable, rng.randint(1, 12))
        changes = {}
        for f in pick:
            lo, hi = f.legal_range()
            changes[f.name] = rng.randint(lo, hi)
        new, changed = lay.apply(rec, changes)
        allowed = 0
        for n in changed:
            allowed |= lay.by_name[n].mask
        assert (rec_int(new) ^ rec_int(rec)) & ~allowed == 0
        assert set(changed) <= set(changes)
        for f in lay.fields:
            want = changes.get(f.name, lay.get(rec, f.name))
            assert lay.get(new, f.name) == want
        assert (rec_int(new) & lay.unmapped_mask) == (rec_int(rec) & lay.unmapped_mask)


@pytest.mark.parametrize("lid", LAYOUTS)
def test_decode_json_encode_identity_even_with_illegal_values(lid):
    """decode → JSON 文本 → encode（从原记录出发写全部字段）逐字节相同；原记录里的非法值（如身体项 15）原样保留。"""
    lay = PA.load_layout(lid)
    rng = random.Random(5)
    for _ in range(200):
        rec = rng.randbytes(lay.record_size)
        js = json.loads(json.dumps(lay.decode(rec)))
        assert bytes.fromhex(js["record_hex"]) == rec
        spec = {n: lay.resolve_value(n, e) for n, e in js["fields"].items()}  # e 同时含 raw 与 display
        out, changed = lay.apply(bytes.fromhex(js["record_hex"]), spec)
        assert out == rec and changed == []


def test_body_patch_on_ef64_touches_only_body_nibbles():
    lay = PA.load_layout("ef64")
    rng = random.Random(9)
    rec = rng.randbytes(64)
    vals = {n: rng.randint(0, 14) for n in PA.BODY_FIELDS}
    new, _ = lay.apply(rec, vals)
    assert new[:8] == rec[:8] and new[15:] == rec[15:]  # 只有 0x08–0x0E 可能变
    assert {n: lay.get(new, n) for n in PA.BODY_FIELDS} == vals


# =============================================================================================
# 06 号档案 §8 测试向量（从档案正文解析）
# =============================================================================================

def _dossier_vectors():
    txt = _dossier()
    sec5 = txt[txt.index("### 5.2"):txt.index("### 5.3")]
    sec8 = txt[txt.index("## 8. "):txt.index("## 9. ")]
    body11 = PA.BODY_FIELDS[:11]  # pesdb 只显示前 11 项（NL..LL）
    vals = {}
    for line in sec5.splitlines():
        m = re.match(r"\|\s*(Hulk|Karim Benzema|Rivaldo)\b[^|]*\|\s*\d+ / \d+\s*\|(.*)", line)
        if m:
            cells = [c.strip().strip("*") for c in m.group(2).split("|")][:11]
            vals[m.group(1).split()[-1]] = dict(zip(body11, map(int, cells)))
    exp = {}
    for line in sec8.splitlines():
        m = re.match(r"\|\s*(\d+)（(Hulk|Benzema|Rivaldo)[^|]*\|\s*`([0-9A-F ]+)`\s*\|\s*`([0-9A-F])`", line)
        if m:
            exp[m.group(2)] = (int(m.group(1)), bytes.fromhex(m.group(3)), int(m.group(4), 16))
    assert set(vals) == set(exp) == {"Hulk", "Benzema", "Rivaldo"}, (vals, exp)
    return [(k, exp[k][0], vals[k], exp[k][1], exp[k][2]) for k in sorted(vals)]


VECTORS = _dossier_vectors()


@pytest.mark.parametrize("lid", LAYOUTS)
@pytest.mark.parametrize("who,pid,vals,body5,low13", VECTORS, ids=[v[0] for v in VECTORS])
def test_dossier_vectors(lid, who, pid, vals, body5, low13):
    lay = PA.load_layout(lid)
    base = BODY_BASE[lid]
    # 编码：在随机记录上写 11 项 → 期望字节（头部 3 项不在 pesdb 中，保持原值）
    rng = random.Random(pid)
    rec = rng.randbytes(lay.record_size)
    rec = lay.set_raw(rec, "HeadLength", 7)  # 让 0x0D 高半字节合法，避免与向量无关的非法值
    new, _ = lay.apply(rec, vals)
    assert new[base:base + 5] == body5
    assert new[base + 5] & 0xF == low13
    assert new[base + 5] >> 4 == rec[base + 5] >> 4  # 同一字节的另一半保留原值
    # 解码：手工拼出的字节 → 期望数值
    b = bytearray(rec)
    b[base:base + 5] = body5
    b[base + 5] = (b[base + 5] & 0xF0) | low13
    got = {n: lay.get(bytes(b), n) for n in vals}
    assert got == vals
    # 当前 ef64 假设下 PID 在 0x00（u64）
    if lid == "ef64":
        new2, _ = lay.apply(new, {"pid": pid}, allow_groups=("header",))
        assert lay.decode(new2)["pid"] == pid and new2[:8] == pid.to_bytes(8, "little")


def test_arm_pair_order_detects_swap():
    """Hulk ArmSize 8 / ArmLength 1：0x0B 必须是 0x18（臂围在低半字节）；若实际数据是 0x81 说明两项对调（06 §8 第 5 步）。"""
    lay = PA.load_layout("ef64")
    new, _ = lay.apply(bytes(64), {"ArmSize": 8, "ArmLength": 1})
    assert new[0x0B] == 0x18
    swapped = bytearray(new)
    swapped[0x0B] = 0x81
    assert (lay.get(bytes(swapped), "ArmSize"), lay.get(bytes(swapped), "ArmLength")) == (1, 8)


def test_pes_db_generator_template_header():
    """pes-db-generator 的 PlayerAppearance_Base_16.bin：ID 70101（d5 11 01 00）+ 7 字节 0x77（06 §2.5）。
    档案只记录了这 11 字节，其余字节这里用零填充（只验证头部与身体段）。"""
    lay = PA.load_layout("pesdb60")
    rec = bytes.fromhex("d5110100") + b"\x77" * 7 + bytes(60 - 11)
    d = lay.decode(rec)
    assert d["pid"] == 70101
    assert d["body14"] == {n: 7 for n in PA.BODY_FIELDS}
    assert all(d["fields"][n]["display"] == 0 for n in PA.BODY_FIELDS)


def test_layout_shift_rule_on_same_body_bytes():
    """同一组 56 字节放进三种布局：所有共享字段解码相同（edit72 = pesdb60 + 8 = ef64 + 4）。"""
    rng = random.Random(21)
    body56 = rng.randbytes(56)
    recs = {"edit72": rng.randbytes(12) + body56 + rng.randbytes(4),
            "pesdb60": rng.randbytes(4) + body56,
            "ef64": rng.randbytes(8) + body56}
    dec = {k: PA.load_layout(k).decode_values(v) for k, v in recs.items()}
    for name in dec["ef64"]:
        if name == "pid":
            continue
        assert dec["edit72"][name] == dec["pesdb60"][name] == dec["ef64"][name], name


# =============================================================================================
# 输入守卫与拒绝
# =============================================================================================

@pytest.fixture(scope="module")
def ef_table():
    return make_table(PA.load_layout("ef64"), 300, seed=4)


def _wrap_variants(t):
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as z:
        z.writestr("Payload/x.bin", t)
    return {
        "zlib": zlib.compress(t),
        "wesys": b"\x00\x01\x02WESYS\x00\x00" + len(t).to_bytes(4, "little") + bytes(3) + zlib.compress(t),
        "hdr16+zlib": bytes(16) + zlib.compress(t),
        "gzip": gzip.compress(t),
        "zip/ipa": zbuf.getvalue(),
        "cpk": b"CPK " + bytes(12) + b"@UTF" + t[:4000],
        "zstd": b"\x28\xb5\x2f\xfd" + t[:2000],
        "utoc": b"-==--==--==--==-" + t[:2000],
        # 真实 UE 包：标记后是 LegacyFileVersion（UE4.26 = −7）；真实 Mach-O：魔数后是 cputype（arm64 = 0x0100000C）。
        # 只有魔数、后面直接是表字节的情形与"PID 恰好等于魔数"无法区分，不应拒绝（见 test_player_appearance_adversarial.py）
        "uasset": b"\xc1\x83\x2a\x9e" + (-7).to_bytes(4, "little", signed=True) + t[:2000],
        "macho": b"\xcf\xfa\xed\xfe" + (0x0100000C).to_bytes(4, "little") + t[:2000],
        "random": random.Random(1).randbytes(64 * 300),
    }


def test_guard_accepts_realistic_decoded_table(ef_table):
    assert PA.shannon_entropy(ef_table) < 7.0
    PA.check_decoded(ef_table, "t")


def test_guard_refuses_encrypted_or_compressed(ef_table):
    for name, blob in _wrap_variants(ef_table).items():
        with pytest.raises(PA.NotDecodedError) as e:
            PA.check_decoded(blob, name)
        msg = str(e.value)
        assert "解码后表数据" in msg and "不是 IPA" in msg and "任务书第五节" in msg, name


def test_guard_no_false_positive_for_pid_that_looks_like_zlib():
    lay = PA.load_layout("ef64")
    t = make_table(lay, 50, pids=[0x9C78 + i for i in range(50)])  # 首字节 78 9C，像 zlib 头但不是
    assert t[:2] == b"\x78\x9c"
    PA.check_decoded(t, "t")


def test_cli_refuses_each_wrapped_variant(tmp_path, ef_table):
    for name, blob in _wrap_variants(ef_table).items():
        p = tmp_path / ("in_" + name.replace("/", "_"))
        p.write_bytes(blob)
        rc, out, err = run_cli("roundtrip-check", "--layout", "ef64", "--table", p)
        assert rc == 3, (name, out, err)
        assert "严禁把内存字段的 bit 位置直接当成 IPA 文件偏移写入" in err


def test_cli_refuses_wrong_length(tmp_path, ef_table):
    p = tmp_path / "t.bin"
    p.write_bytes(ef_table + bytes(16))
    rc, _, err = run_cli("roundtrip-check", "--layout", "ef64", "--table", p)
    assert rc == 3 and "不是 ef64 行长 64 的整数倍" in err and "WESYS" in err
    r = tmp_path / "r.bin"
    r.write_bytes(ef_table[:60])
    rc, _, err = run_cli("decode", "--layout", "ef64", "--record", r)
    assert rc == 3 and "单条 ef64 记录应为 64 B" in err


@pytest.fixture()
def table_file(tmp_path, ef_table):
    p = tmp_path / "PlayerAppearance.dec"
    p.write_bytes(ef_table)
    return p


def _pid(tbl, i, lid="ef64"):
    return PA.Table(tbl, PA.load_layout(lid)).pid(i)


def test_cli_patch_refusals(tmp_path, table_file, ef_table):
    pid = _pid(ef_table, 5)
    cases = [
        (["--set", "ThighSiz=11"], "未知字段"),
        (["--set", "ThighSize=15"], "不合法"),
        (["--set", "ThighSize=16"], "超出 4 位"),
        (["--set", "ThighSize=-1"], "超出 4 位"),
        (["--set", "ThighSize=8", "--display"], "不合法"),          # 显示 +8 → 存储 15
        (["--set", "ThighSize=x"], "不是整数"),
        (["--set", "ThighSize=9", "--set", "ThighSize=10"], "两次"),
        (["--set", "pid=1"], "header"),
        (["--set", "IrisColor=16"], "超出 4 位"),
    ]
    for extra, needle in cases:
        rc, out, err = run_cli("patch", "--layout", "ef64", "--table", table_file, "--pid", pid, "--dry-run", *extra)
        assert rc == 3 and needle in err, (extra, out, err)
    # 输出不能覆盖输入；已存在的输出需要 --overwrite
    rc, _, err = run_cli("patch", "--layout", "ef64", "--table", table_file, "--pid", pid, "--set", "ThighSize=9",
                         "--out", table_file)
    assert rc == 3 and "同一个文件" in err
    other = tmp_path / "exists.dec"
    other.write_bytes(b"x")
    rc, _, err = run_cli("patch", "--layout", "ef64", "--table", table_file, "--pid", pid, "--set", "ThighSize=9",
                         "--out", other)
    assert rc == 3 and "已存在" in err
    assert table_file.read_bytes() == ef_table  # 原表从未被改


def test_cli_patch_refuses_ambiguous_or_missing_pid(tmp_path):
    lay = PA.load_layout("ef64")
    t = make_table(lay, 10, pids=[5, 6, 7, 7, 8, 9, 10, 11, 12, 13])
    p = tmp_path / "dup.dec"
    p.write_bytes(t)
    rc, _, err = run_cli("patch", "--layout", "ef64", "--table", p, "--pid", 7, "--set", "ThighSize=9", "--dry-run")
    assert rc == 3 and "有歧义" in err
    rc, _, err = run_cli("patch", "--layout", "ef64", "--table", p, "--pid", 999, "--set", "ThighSize=9", "--dry-run")
    assert rc == 3 and "没有 PID 999" in err


def test_pid_change_that_breaks_order_is_refused(tmp_path, table_file, ef_table):
    pid5, pid9 = _pid(ef_table, 5), _pid(ef_table, 9)
    rc, _, err = run_cli("patch", "--layout", "ef64", "--table", table_file, "--pid", pid5, "--allow-header",
                         "--set", f"pid={pid9 + 1}", "--out", tmp_path / "o.dec")
    assert rc == 3 and "严格递增" in err
    rc, out, err = run_cli("patch", "--layout", "ef64", "--table", table_file, "--pid", pid5, "--allow-header",
                           "--set", f"pid={pid5 + 0}", "--dry-run")  # 原值：无改动，允许
    assert rc == 0, err


def test_encode_refusals(tmp_path, table_file, ef_table):
    lay = PA.load_layout("ef64")
    rec = PA.Table(ef_table, lay).record(3)
    js = lay.decode(rec)
    bad = json.loads(json.dumps(js))
    bad["fields"]["ThighSize"] = {"raw": 9, "display": 0}  # 9 应显示 +2
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad), encoding="utf-8")
    rc, _, err = run_cli("encode", p, "--out", tmp_path / "o.bin")
    assert rc == 3 and "不一致" in err
    # JSON 是另一条记录的（过期/指错）
    stale = json.loads(json.dumps(js))
    p2 = tmp_path / "stale.json"
    p2.write_text(json.dumps(stale), encoding="utf-8")
    rc, _, err = run_cli("encode", p2, "--layout", "ef64", "--table", table_file, "--index", 4, "--out", tmp_path / "o2.bin")
    assert rc == 3 and "过期" in err
    # 未知字段
    unk = {"layout": "ef64", "record_hex": rec.hex(), "set": {"FaceNumber": 1}}
    p3 = tmp_path / "unk.json"
    p3.write_text(json.dumps(unk), encoding="utf-8")
    rc, _, err = run_cli("encode", p3, "--out", tmp_path / "o3.bin")
    assert rc == 3 and "未知字段 'FaceNumber'" in err
    # 布局不符
    p4 = tmp_path / "lay.json"
    p4.write_text(json.dumps({**unk, "set": {"ThighSize": 9}}), encoding="utf-8")
    rc, _, err = run_cli("encode", p4, "--layout", "pesdb60", "--out", tmp_path / "o4.bin")
    assert rc == 3 and "布局" in err


# =============================================================================================
# 命令行正常路径
# =============================================================================================

def test_cli_decode_edit_encode_cycle(tmp_path, table_file, ef_table):
    lay = PA.load_layout("ef64")
    idx = 7
    pid = _pid(ef_table, idx)
    rc, out, err = run_cli("decode", "--layout", "ef64", "--table", table_file, "--pid", pid)
    assert rc == 0, err
    js = json.loads(out)
    assert js["source"]["record_index"] == idx and js["source"]["table_offset"] == idx * 64 and js["pid"] == pid
    old = js["fields"]["ThighSize"]["raw"]
    new_val = 11 if old != 11 else 10
    js["fields"]["ThighSize"] = {"raw": new_val}                    # 改 raw
    js["fields"]["WaistSize"] = {"display": 1}                       # 改 display（存储 8）
    jp = tmp_path / "edit.json"
    jp.write_text(json.dumps(js, ensure_ascii=False), encoding="utf-8")
    outp = tmp_path / "out.dec"
    rc, out, err = run_cli("encode", jp, "--layout", "ef64", "--table", table_file, "--index", idx, "--out", outp)
    assert rc == 0, err
    t2 = outp.read_bytes()
    assert len(t2) == len(ef_table)
    assert t2[:idx * 64] == ef_table[:idx * 64] and t2[(idx + 1) * 64:] == ef_table[(idx + 1) * 64:]
    r0, r1 = ef_table[idx * 64:(idx + 1) * 64], t2[idx * 64:(idx + 1) * 64]
    assert lay.get(r1, "ThighSize") == new_val and lay.get(r1, "WaistSize") == 8
    mask = lay.by_name["ThighSize"].mask | lay.by_name["WaistSize"].mask
    assert (rec_int(r0) ^ rec_int(r1)) & ~mask == 0
    man = json.loads((tmp_path / "out.dec.patch.json").read_text(encoding="utf-8"))
    assert man["input"]["sha256"] != man["output"]["sha256"]
    assert {c["name"] for c in man["field_changes"]} <= {"ThighSize", "WaistSize"}
    assert all(set(b["fields"]) <= {"ThighSize", "WaistSize"} for b in man["bit_diff"])
    # 单条记录：只用 JSON 中的 record_hex 也能编码
    rc, out, err = run_cli("encode", jp, "--out", tmp_path / "rec.bin")
    assert rc == 0 and (tmp_path / "rec.bin").read_bytes() == r1


def test_cli_patch_prints_bit_diff_and_preserves(tmp_path, table_file, ef_table):
    lay = PA.load_layout("ef64")
    pid = _pid(ef_table, 12)
    r0 = ef_table[12 * 64:13 * 64]
    target = 3 if lay.get(r0, "CalfSize") != 3 else 4
    outp = tmp_path / "p.dec"
    rc, out, err = run_cli("patch", "--layout", "ef64", "--table", table_file, "--pid", pid,
                           "--set", f"CalfSize={target}", "--out", outp)
    assert rc == 0, err
    assert "CalfSize" in out and "改动掩码" in out and f"表内 0x{12 * 64 + 0x0C:08X}" in out
    t2 = outp.read_bytes()
    r1 = t2[12 * 64:13 * 64]
    assert (rec_int(r0) ^ rec_int(r1)) & ~lay.by_name["CalfSize"].mask == 0
    assert r1[0x0C] & 0x0F == r0[0x0C] & 0x0F  # ThighSize（同一字节低半字节）不变
    assert t2[:12 * 64] == ef_table[:12 * 64] and t2[13 * 64:] == ef_table[13 * 64:]


def test_cli_patch_with_body_params_json(tmp_path, table_file, ef_table):
    """data/body/r9_body_params.json 的 codec_patch 可以直接喂给 patch；只改 14 个身体半字节。"""
    bp = os.path.join(ROOT, "data", "body", "r9_body_params.json")
    with open(bp, encoding="utf-8") as fh:
        params = json.load(fh)
    cp = params["codec_patch"]
    assert set(k for k in cp if not k.startswith("_")) == set(PA.BODY_FIELDS)
    lay = PA.load_layout("ef64")
    outp = tmp_path / "r9.dec"
    pid = _pid(ef_table, 20)
    rc, out, err = run_cli("patch", "--layout", "ef64", "--table", table_file, "--pid", pid,
                           "--set-json", bp, "--out", outp)
    assert rc == 0, err
    r0, r1 = ef_table[20 * 64:21 * 64], outp.read_bytes()[20 * 64:21 * 64]
    assert {n: lay.get(r1, n) for n in PA.BODY_FIELDS} == {n: cp[n] for n in PA.BODY_FIELDS}
    assert r1[:8] == r0[:8] and r1[15:] == r0[15:]


def test_cli_find(tmp_path, table_file, ef_table):
    pid = _pid(ef_table, 33)
    rc, out, _ = run_cli("find", "--layout", "ef64", "--table", table_file, "--pid", pid)
    rep = json.loads(out)
    assert rc == 0 and rep["aligned_hits"][0]["index"] == 33 and rep["sorted_strictly_ascending"]
    assert rep["aligned_hits"][0]["body14"].keys() == set(PA.BODY_FIELDS)
    rc, out, _ = run_cli("find", "--layout", "ef64", "--table", table_file, "--pid", 1)
    assert rc == 1 and json.loads(out)["aligned_hits"] == []
    # PID 的字节出现在非记录起点（模拟行长/起点选错）→ 给出提示
    t = bytearray(ef_table)
    t[64 * 40 + 24:64 * 40 + 32] = (123456789).to_bytes(8, "little")
    p = tmp_path / "u.dec"
    p.write_bytes(bytes(t))
    rc, out, _ = run_cli("find", "--layout", "ef64", "--table", p, "--pid", 123456789)
    rep = json.loads(out)
    assert rc == 1 and rep["pattern_hits_u64"][0]["file_offset"] == 64 * 40 + 24
    assert not rep["pattern_hits_u64"][0]["aligned"] and rep["hint"]


def test_cli_roundtrip_check_and_wrong_layout_detection(tmp_path, table_file):
    rc, out, err = run_cli("roundtrip-check", "--layout", "ef64", "--table", table_file, "--strict")
    rep = json.loads(out)
    assert rc == 0 and rep["roundtrip_identical"] and rep["structure_ok"], err
    assert rep["structure"]["body_modal_value"] == 7 and rep["structure"]["bytes_4_7_zero_fraction"] == 1.0
    # 用错布局（把 64 B 表当 60 B 表读）：往返仍无损，但结构检验失败
    t = table_file.read_bytes()[:64 * 15 * 20]  # 长度同时是 60 和 64 的倍数
    p = tmp_path / "w.dec"
    p.write_bytes(t)
    rc, out, _ = run_cli("roundtrip-check", "--layout", "pesdb60", "--table", p, "--strict")
    rep = json.loads(out)
    assert rep["roundtrip_identical"] and not rep["structure_ok"] and rc == 1


def test_edit72_blocks_inside_pes2021_player_records(tmp_path):
    """PES 20/21 data.dat：球员区从 0x7C 开始，每条 0x138，外观块在 +0xF0（06 §2.1）。用 --start/--stride 直接处理。"""
    lay = PA.load_layout("edit72")
    rng = random.Random(77)
    n = 40
    blocks = make_table(lay, n, seed=8)
    dat = bytearray(rng.randbytes(0x7C) + bytes(n * 0x138) + rng.randbytes(100))
    for i in range(n):
        o = 0x7C + i * 0x138
        dat[o:o + 0xF0] = bytes(rng.getrandbits(3) for _ in range(0xF0))  # 低熵的"球员数据"
        dat[o + 0xF0:o + 0x138] = blocks[i * 72:(i + 1) * 72]
    p = tmp_path / "data.dat"
    p.write_bytes(bytes(dat))
    start = 0x7C + 0xF0
    rc, out, err = run_cli("roundtrip-check", "--layout", "edit72", "--table", p, "--start", start, "--stride", 0x138)
    rep = json.loads(out)
    assert rc == 0 and rep["roundtrip_identical"] and rep["records"] == n, err
    outp = tmp_path / "data2.dat"
    rc, out, err = run_cli("patch", "--layout", "edit72", "--table", p, "--start", start, "--stride", 0x138,
                           "--index", 3, "--set", "NeckLength=4", "--out", outp)
    assert rc == 0, err
    d2 = outp.read_bytes()
    o = start + 3 * 0x138
    diff = [i for i in range(len(dat)) if dat[i] != d2[i]]
    assert diff and all(i == o + 0x0C for i in diff)  # 只有 NeckLength 所在字节
    assert d2[o + 0x0C] & 0x0F == 4 and d2[o + 0x0C] >> 4 == dat[o + 0x0C] >> 4


def test_cli_fields_lists_every_field():
    rc, out, _ = run_cli("fields", "--layout", "ef64")
    assert rc == 0
    for n in PA.BODY_FIELDS + ("pid", "HairStyle", "SkinColor"):
        assert n in out
    assert "UNVERIFIED" in out and "冲突" in out
