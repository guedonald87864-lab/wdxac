#!/usr/bin/env python3
"""PlayerAppearance 记录的 schema 驱动位域编解码器（命令行 + Python API）。

布局不写死在代码里，全部来自 data/body/layouts/*.json（由 tools/data/gen_appearance_layouts.py
从 06 号档案 §7 生成）：
  edit72   PES 2016–2021 EDIT 存档外观块，72 B（社区实测，高置信）
  pesdb60  PES 2016/2017–2021 数据库表 PlayerAppearance.bin 行，60 B（社区实测，高置信）
  ef64     eFootball PC PlayerAppearance.bin 行，64 B（**候选**，中置信；对 iOS 11.1.1 **未验证**）
每个字段：字节偏移、位偏移、位宽、半字节顺序、值变换、置信度。位编号：字节内低位在前，跨字节小端拼接
（06 §1.2）：value = (int.from_bytes(record, 'little') >> (byte*8+bit)) & ((1<<width)-1)。

子命令
  decode           记录 → JSON（每个字段的原始值 raw 和显示值 display，外加未分配位、原记录 hex）
  encode           JSON → 字节。**从原记录出发**，只写 JSON 里给出的字段；没有给出的字段和未分配位保持原样
  patch            只改列出的字段（--set 名=值，可多次；或 --set-json），拒绝未知字段和非法值，打印逐位差异
  find             在解码后的表里按 PID 查找（对齐查找 + 非对齐字节模式扫描，用来发现布局/行长选错）
  roundtrip-check  整表 解码 → JSON → 编码 往返，要求逐字节相同；并做 06 §8 的结构检验
  fields           列出某个布局的全部字段

输入要求（硬性）：只接受**解码后的表数据**（PC 版 = WESYS 解密 + zlib 解压之后；PES 存档 = pesXdecrypter
导出的 data.dat；或单条记录）。看起来是加密/压缩/容器的文件（高熵，或带 CPK、WESYS、zlib、gzip、zip、zstd、
LZ4、UE .utoc/.uasset、Mach-O 等特征）一律拒绝：布局里的偏移是"表内偏移"，不是 IPA/CPK/.utoc/.ucas/
加密 .bin 的文件偏移，也不是内存结构里的位置（任务书第五节：严禁把内存字段的 bit 位置直接当成 IPA 文件偏移写入）。

写入规则：输出永远写到新文件（--out 不能等于输入），并在旁边写 <out>.patch.json（输入/输出 SHA-256、
改动的字段与逐位差异）。写入后自检：新旧记录的异或只能落在被改字段的位上，表的其余字节必须完全相同。

用法示例：
  python3 tools/data/player_appearance.py decode --layout ef64 --table PlayerAppearance.dec --pid 15038
  python3 tools/data/player_appearance.py patch  --layout ef64 --table PlayerAppearance.dec --pid 15038 \\
          --set ThighSize=11 --set WaistSize=8 --out PlayerAppearance.r9.dec
  python3 tools/data/player_appearance.py patch  ... --set-json data/body/r9_body_params.json   # 用其中的 codec_patch
  python3 tools/data/player_appearance.py roundtrip-check --layout ef64 --table PlayerAppearance.dec

退出码：0 成功；1 检查失败（往返不一致、找不到 PID、--strict 下结构检验失败）；2 参数错误；
3 拒绝（输入不像解码数据、未知字段、非法值、会覆盖输入等）。
"""
import argparse
import collections
import hashlib
import json
import math
import os
import sys
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LAYOUT_DIR = os.path.join(ROOT, "data", "body", "layouts")
LAYOUT_IDS = ("edit72", "pesdb60", "ef64")

BODY_FIELDS = ("NeckLength", "NeckSize", "ShoulderHeight", "ShoulderWidth", "ChestMeasurement", "WaistSize",
               "ArmSize", "ArmLength", "ThighSize", "CalfSize", "LegLength", "HeadLength", "HeadWidth", "HeadDepth")

OFFSET_SPACE_MSG = (
    "本工具的所有偏移都是『解码后表数据』里的位置（eFootball PC：WESYS 解密 + zlib 解压之后；"
    "PES 存档：pesXdecrypter 导出的 data.dat）。它们不是 IPA、CPK、.utoc/.ucas、加密或压缩 .bin 的文件偏移，"
    "也不是内存结构中的位置（PES 2021 内存体型对象是 float[16]+u8，两项手臂参数对调）。"
    "任务书第五节：严禁把内存字段的 bit 位置直接当成 IPA 文件偏移写入。"
    "请先解壳/解压（06 号档案 §1.1、§8 第 2 步），再把解码后的数据交给本工具。")


class AppearanceError(Exception):
    """可预期的拒绝（退出码 3）。"""


class NotDecodedError(AppearanceError):
    """输入看起来不是解码后的表数据。"""


# ============================================================================= layout
class Field:
    __slots__ = ("name", "byte", "bit", "width", "pos", "mask", "group", "transform", "confidence",
                 "nibble", "note", "conflict", "u32_word")

    def __init__(self, d):
        self.name = d["name"]
        self.byte, self.bit, self.width = int(d["byte"]), int(d["bit"]), int(d["width"])
        if not (0 <= self.bit < 8 and self.width > 0):
            raise ValueError(f"bad field {d}")
        self.pos = self.byte * 8 + self.bit
        self.mask = ((1 << self.width) - 1) << self.pos
        self.group = d.get("group", "")
        self.transform = d.get("transform", {"kind": "uint"})
        self.confidence = d.get("confidence", "")
        self.nibble = d.get("nibble")
        self.note = d.get("note", "")
        self.conflict = d.get("conflict")
        self.u32_word = d.get("u32_word")

    @property
    def kind(self):
        return self.transform.get("kind", "uint")

    @property
    def max_raw(self):
        return (1 << self.width) - 1

    def legal_range(self):
        t = self.transform
        return int(t.get("valid_min", 0)), int(t.get("valid_max", self.max_raw))

    def to_display(self, raw):
        return raw + int(self.transform.get("display_offset", 0)) if self.kind == "offset" else raw

    def from_display(self, disp):
        return disp - int(self.transform.get("display_offset", 0)) if self.kind == "offset" else disp

    def is_legal(self, raw):
        lo, hi = self.legal_range()
        return lo <= raw <= hi

    def where(self):
        end = self.pos + self.width - 1
        return f"0x{self.byte:02X}:{self.bit}" + (f"..0x{end // 8:02X}:{end % 8}" if self.width > 1 else "")


class Layout:
    def __init__(self, d, path=None):
        self.id = d["layout_id"]
        self.path = path
        self.meta = {k: v for k, v in d.items() if k != "fields"}
        self.record_size = int(d["record_size"])
        self.fields = [Field(f) for f in d["fields"]]
        self.by_name = {f.name: f for f in self.fields}
        if len(self.by_name) != len(self.fields):
            raise ValueError(f"{self.id}: duplicate field names")
        self.pid_field = self.by_name.get(d.get("pid_field", ""))
        self.status = d.get("status", "")
        self.confidence = d.get("confidence", "")
        nbits = self.record_size * 8
        occ = 0
        for f in self.fields:
            if f.pos + f.width > nbits:
                raise ValueError(f"{self.id}: field {f.name} runs past the record end")
            if occ & f.mask:
                clash = [g.name for g in self.fields if g is not f and g.mask & f.mask]
                raise ValueError(f"{self.id}: field {f.name} overlaps {clash}")
            occ |= f.mask
        self.mapped_mask = occ
        self.unmapped_mask = ((1 << nbits) - 1) & ~occ
        self.unmapped_runs = _runs(self.unmapped_mask, nbits)

    # -- primitive bit access ------------------------------------------------------------------
    def to_int(self, record):
        if len(record) != self.record_size:
            raise AppearanceError(f"记录长度 {len(record)} ≠ 布局 {self.id} 的行长 {self.record_size}")
        return int.from_bytes(record, "little")

    def to_bytes(self, value):
        return value.to_bytes(self.record_size, "little")

    def field(self, name):
        try:
            return self.by_name[name]
        except KeyError:
            near = [n for n in self.by_name if n.lower() == str(name).lower()
                    or str(name).lower() in n.lower()][:5]
            raise AppearanceError(f"未知字段 '{name}'（布局 {self.id}）。" +
                                  (f"是否指：{', '.join(near)}？" if near else "用 `fields --layout "
                                   f"{self.id}` 查看全部字段。")) from None

    def get(self, record, name):
        f = self.field(name)
        return (self.to_int(record) >> f.pos) & f.max_raw

    def set_raw(self, record, name, raw):
        """低层写入：只检查位宽（不检查取值是否合法），只改这一个字段的位。"""
        f = self.field(name)
        if not isinstance(raw, int) or isinstance(raw, bool) or not 0 <= raw <= f.max_raw:
            raise AppearanceError(f"{f.name}: 值 {raw!r} 超出 {f.width} 位的范围 0..{f.max_raw}")
        v = self.to_int(record)
        v = (v & ~f.mask) | (raw << f.pos)
        return self.to_bytes(v)

    # -- decode -----------------------------------------------------------------------------------
    def decode_values(self, record):
        v = self.to_int(record)
        return {f.name: (v >> f.pos) & f.max_raw for f in self.fields}

    def decode(self, record, source=None):
        v = self.to_int(record)
        fields, warnings = {}, []
        for f in self.fields:
            raw = (v >> f.pos) & f.max_raw
            e = {"raw": raw, "display": f.to_display(raw), "byte": f.byte, "bit": f.bit, "width": f.width,
                 "nibble": f.nibble, "group": f.group, "confidence": f.confidence}
            if f.kind == "offset" and "valid_max" in f.transform:
                e["legal"] = f.is_legal(raw)
                if not e["legal"]:
                    warnings.append(f"{f.name} = {raw} 不在合法范围 {f.legal_range()}（PES 2021 会把 15 当 0）")
            fields[f.name] = e
        out = {
            "tool": "tools/data/player_appearance.py",
            "layout": self.id, "layout_status": self.status, "layout_confidence": self.confidence,
            "offset_space": self.meta.get("offset_space", ""),
            "record_size": self.record_size,
            "record_hex": bytes(record).hex(),
            "source": source or {},
            "pid": ((v >> self.pid_field.pos) & self.pid_field.max_raw) if self.pid_field else None,
            "body14": {n: fields[n]["raw"] for n in BODY_FIELDS if n in fields},
            "fields": fields,
            "unmapped": [{"byte": p // 8, "bit": p % 8, "width": w, "raw": (v >> p) & ((1 << w) - 1)}
                         for p, w in self.unmapped_runs],
            "warnings": warnings,
            "encode_rule": "encode 从 record_hex（原记录）出发，只写 fields 中给出的字段；其余位保持原值",
        }
        return out

    # -- encode -----------------------------------------------------------------------------------
    def resolve_value(self, name, spec, display=False):
        """spec: int（存储值；display=True 时为显示值）或 {"raw": r} / {"display": d} / 两者都给（必须一致）。"""
        f = self.field(name)
        if isinstance(spec, dict):
            raw = spec.get("raw")
            disp = spec.get("display")
            if raw is None and disp is None:
                raise AppearanceError(f"{name}: 需要 raw 或 display")
            if raw is not None and disp is not None and f.to_display(int(raw)) != int(disp):
                raise AppearanceError(f"{name}: raw={raw} 与 display={disp} 不一致（display = raw "
                                      f"{f.transform.get('display_offset', 0):+d}），无法判断要写哪一个，拒绝")
            raw = int(raw) if raw is not None else f.from_display(int(disp))
        else:
            if isinstance(spec, bool) or not isinstance(spec, int):
                try:
                    spec = int(str(spec), 0)
                except ValueError:
                    raise AppearanceError(f"{name}: 值 {spec!r} 不是整数") from None
            raw = f.from_display(spec) if display else spec
        if not 0 <= raw <= f.max_raw:
            raise AppearanceError(f"{name}: 存储值 {raw} 超出 {f.width} 位范围 0..{f.max_raw}")
        return raw

    def check_legal(self, name, raw):
        f = self.field(name)
        if not f.is_legal(raw):
            lo, hi = f.legal_range()
            raise AppearanceError(f"{name}: 存储值 {raw} 不合法（合法 {lo}..{hi}；"
                                  f"{f.transform.get('rule', '')}）")

    def apply(self, original, changes, allow_groups=()):
        """从原记录出发写入 changes {name: raw}；返回 (新记录, 实际改动列表)。只改列出字段的位。
        与原值相同的字段不算改动（所以原记录里即使有不合法的值，原样往返也不会被拒绝）；
        真正改动的字段必须在位宽内且取值合法。"""
        v0 = self.to_int(original)
        v = v0
        changed = []
        for name, raw in changes.items():
            f = self.field(name)
            if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= f.max_raw:
                raise AppearanceError(f"{name}: 值 {raw!r} 超出 {f.width} 位的范围 0..{f.max_raw}")
            old = (v0 >> f.pos) & f.max_raw
            if old == raw:
                continue
            self.check_legal(name, raw)
            if f.group in ("header", "unused") and f.group not in allow_groups:
                raise AppearanceError(
                    f"{name} 属于 {f.group} 区（{'PID/标志/引用' if f.group == 'header' else '实测未用的位'}），"
                    f"默认禁止修改；确有需要请加 --allow-{f.group}")
            v = (v & ~f.mask) | (raw << f.pos)
            changed.append(name)
        allowed = 0
        for n in changed:
            allowed |= self.by_name[n].mask
        if (v ^ v0) & ~allowed:  # 自检：异或只能落在被改字段上
            raise AssertionError("internal: bits outside the patched fields changed")
        return self.to_bytes(v), changed


def _runs(mask, nbits):
    runs, p = [], 0
    while p < nbits:
        if mask >> p & 1:
            s = p
            while p < nbits and mask >> p & 1:
                p += 1
            runs.append((s, p - s))
        else:
            p += 1
    return runs


_LAYOUT_CACHE = {}


def load_layout(name_or_path):
    key = name_or_path
    if key in _LAYOUT_CACHE:
        return _LAYOUT_CACHE[key]
    path = name_or_path if os.path.sep in name_or_path or name_or_path.endswith(".json") \
        else os.path.join(LAYOUT_DIR, name_or_path + ".json")
    if not os.path.exists(path):
        raise AppearanceError(f"找不到布局 {name_or_path}（可用：{', '.join(LAYOUT_IDS)}，或给出 JSON 路径）")
    with open(path, encoding="utf-8") as fh:
        lay = Layout(json.load(fh), path)
    _LAYOUT_CACHE[key] = lay
    return lay


# ============================================================================= input guard
def shannon_entropy(data):
    """每字节香农熵（bit/byte）。"""
    n = len(data)
    if not n:
        return 0.0
    return -sum(c / n * math.log2(c / n) for c in collections.Counter(data).values())


def _zlib_inflates(data, off):
    try:
        d = zlib.decompressobj()
        out = d.decompress(data[off:off + 65536], 4096)
        return len(out) > 0
    except zlib.error:
        return False


def _magic_hits(data):
    """返回命中的容器/压缩/加密特征列表（尽量避免误报：zlib 还要求能真正解压出数据）。"""
    hits = []
    h = bytes(data[:64])
    if h[:4] == b"CPK " and data[16:20] == b"@UTF":
        hits.append("CPK 容器（'CPK ' + '@UTF'）")
    if b"WESYS" in h[:16]:
        hits.append("WESYS 外壳头（加密 + zlib，06 §1.1 第③层）")
    if len(h) >= 2 and h[0] == 0x78 and ((h[0] << 8) | h[1]) % 31 == 0 and _zlib_inflates(data, 0):
        hits.append("zlib 压缩流（偏移 0）")
    if len(data) > 18 and data[16] == 0x78 and ((data[16] << 8) | data[17]) % 31 == 0 and _zlib_inflates(data, 16):
        hits.append("16 字节头之后是 zlib 压缩流（像 WESYS 外壳）")
    if h[:3] == b"\x1f\x8b\x08":
        hits.append("gzip")
    if h[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        hits.append("zip（IPA 也是 zip）")
    if h[:4] == b"\x28\xb5\x2f\xfd":
        hits.append("zstd")
    if h[:4] == b"\x04\x22\x4d\x18":
        hits.append("LZ4 frame")
    if h[:6] == b"\xfd7zXZ\x00":
        hits.append("xz")
    if h[:16] == b"-==--==--==--==-":
        hits.append("UE IoStore .utoc")
    if h[:4] == b"\xc1\x83\x2a\x9e":
        hits.append("UE 包（.uasset/.uexp 标记 0x9E2A83C1）")
    if h[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe"):
        hits.append("Mach-O 可执行文件（不是数据表；也不能把其中/内存中的位置当表偏移）")
    if h[:4] == b"\x13\xab\xa1\x5c":
        hits.append("ASTC 纹理")
    return hits


def check_decoded(data, label="input"):
    """输入守卫：拒绝看起来加密/压缩/容器化的数据。"""
    hits = _magic_hits(data)
    if hits:
        raise NotDecodedError(f"拒绝：{label} 看起来不是解码后的表数据（{'；'.join(hits)}）。\n" + OFFSET_SPACE_MSG)
    n = len(data)
    if n >= 1024:
        H = shannon_entropy(data)
        if H > 7.2:
            raise NotDecodedError(
                f"拒绝：{label} 的字节熵为 {H:.3f} bit/byte（> 7.2），像加密或压缩数据；解码后的外观表大多是"
                f"小整数与中性值 7，熵明显更低。\n" + OFFSET_SPACE_MSG)
        if n >= 65536:
            win = 4096
            hi = sum(1 for i in range(0, n - win + 1, win) if shannon_entropy(data[i:i + win]) > 7.6)
            tot = (n - win) // win + 1
            if hi * 2 >= tot:
                raise NotDecodedError(
                    f"拒绝：{label} 有 {hi}/{tot} 个 4 KiB 窗口熵 > 7.6 bit/byte，像加密或压缩数据。\n" + OFFSET_SPACE_MSG)


def read_input(path, layout, mode, start=0, stride=None):
    """mode = 'table' | 'record'。返回 bytes；不符合要求时抛 AppearanceError。"""
    with open(path, "rb") as fh:
        data = fh.read()
    check_decoded(data, path)
    rs = layout.record_size
    if mode == "record":
        if len(data) != rs:
            raise AppearanceError(f"拒绝：{path} 长 {len(data)} B，单条 {layout.id} 记录应为 {rs} B。" +
                                  _size_hint(len(data)))
    else:
        if not data:
            raise AppearanceError(f"拒绝：{path} 是空文件")
        if start == 0 and (stride is None or stride == rs) and len(data) % rs:
            raise AppearanceError(
                f"拒绝：{path} 长 {len(data)} B，不是 {layout.id} 行长 {rs} 的整数倍（余 {len(data) % rs}）。"
                f"可能原因：仍带 16 字节 WESYS 头、布局选错、或不是这张表。" + _size_hint(len(data)) +
                "\n" + OFFSET_SPACE_MSG)
        if len(data) < start + rs:
            raise AppearanceError(f"拒绝：{path} 太短（{len(data)} B），--start {start} 之后放不下一条记录")
    return data


def _size_hint(n):
    hints = [f"{lid}({lay_rs} B)" for lid, lay_rs in (("edit72", 72), ("pesdb60", 60), ("ef64", 64))
             if n % lay_rs == 0 and n > 0]
    return f" 该长度能被这些行长整除：{', '.join(hints)}。" if hints else ""


# ============================================================================= table helpers
class Table:
    """解码后的表：第 i 条记录从 start + i*stride 开始，长 record_size。默认 start=0、stride=record_size。"""

    def __init__(self, data, layout, start=0, stride=None):
        self.data = bytes(data)
        self.layout = layout
        self.start = int(start)
        self.stride = int(stride or layout.record_size)
        if self.stride < layout.record_size:
            raise AppearanceError("--stride 不能小于记录长度")
        n = len(self.data) - self.start
        self.n = 0 if n < layout.record_size else (n - layout.record_size) // self.stride + 1

    def offset(self, i):
        if not 0 <= i < self.n:
            raise AppearanceError(f"记录序号 {i} 超出范围 0..{self.n - 1}")
        return self.start + i * self.stride

    def record(self, i):
        o = self.offset(i)
        return self.data[o:o + self.layout.record_size]

    def pid(self, i):
        f = self.layout.pid_field
        return int.from_bytes(self.record(i), "little") >> f.pos & f.max_raw

    def pids(self):
        return [self.pid(i) for i in range(self.n)]

    def replaced(self, i, rec):
        o = self.offset(i)
        return self.data[:o] + bytes(rec) + self.data[o + self.layout.record_size:]


def find_pid(table, pid):
    lay = table.layout
    if lay.pid_field is None:
        raise AppearanceError(f"布局 {lay.id} 没有 PID 字段")
    pids = table.pids()
    hits = [i for i, p in enumerate(pids) if p == pid]
    asc = all(a < b for a, b in zip(pids, pids[1:]))
    out = {"layout": lay.id, "pid": pid, "records": table.n, "start": table.start, "stride": table.stride,
           "sorted_strictly_ascending": asc,
           "aligned_hits": [{"index": i, "table_offset": table.offset(i)} for i in hits]}
    # 非对齐扫描：PID 的小端字节模式出现在哪里（帮助发现行长/头宽/起点选错）
    pw = lay.pid_field.width // 8
    pats = {f"u{pw * 8}": pid.to_bytes(pw, "little")} if pid < 1 << (pw * 8) else {}
    if pw == 8 and pid < 1 << 32:
        pats["u32"] = pid.to_bytes(4, "little")
    for k, pat in pats.items():
        offs, o = [], table.data.find(pat)
        while o >= 0 and len(offs) < 64:
            offs.append(o)
            o = table.data.find(pat, o + 1)
        out[f"pattern_hits_{k}"] = [{"file_offset": o, "aligned": (o - table.start) % table.stride == 0 and o >= table.start,
                                     "offset_mod_stride": (o - table.start) % table.stride} for o in offs]
    hint = []
    if not hits and any(h["file_offset"] >= 0 for k in pats for h in out[f"pattern_hits_{k}"]):
        hint.append("PID 的字节模式出现在非记录起点：行长、PID 宽度或 --start 可能选错（06 §8 第 4 步）")
    if not asc:
        hint.append("PID 不是严格递增：不是按 PID 排序的表，或布局/行长不对")
    out["hint"] = hint
    for h in out["aligned_hits"]:
        h["body14"] = {n: table.layout.get(table.record(h["index"]), n) for n in BODY_FIELDS
                       if n in table.layout.by_name}
    return out


def roundtrip_check(table):
    """整表往返 + 结构检验。返回报告 dict；report['roundtrip_identical'] 是硬性结论。"""
    lay = table.layout
    mismatches = []
    rebuilt = bytearray(table.data)
    hist = collections.Counter()
    all_legal = 0
    body_names = [n for n in BODY_FIELDS if n in lay.by_name]
    hi32_zero = 0
    for i in range(table.n):
        rec = table.record(i)
        dec = lay.decode(rec)
        js = json.loads(json.dumps(dec))  # 走一遍 JSON 序列化
        # (a) 从全零记录出发，写全部字段 + 未分配位 → 必须还原原记录（证明字段覆盖完整且不重叠）
        v = 0
        for name, e in js["fields"].items():
            f = lay.by_name[name]
            v |= int(e["raw"]) << f.pos
        for u in js["unmapped"]:
            v |= int(u["raw"]) << (u["byte"] * 8 + u["bit"])
        rec_a = lay.to_bytes(v)
        # (b) 从原记录出发、写全部字段（encode 的实际路径）→ 必须不变
        rec_b, changed = lay.apply(bytes.fromhex(js["record_hex"]),
                                   {n: e["raw"] for n, e in js["fields"].items()}, allow_groups=("header", "unused"))
        if rec_a != rec or rec_b != rec or changed:
            mismatches.append(i)
        o = table.offset(i)
        rebuilt[o:o + lay.record_size] = rec_a
        body = [js["fields"][n]["raw"] for n in body_names]
        hist.update(body)
        all_legal += all(b <= 14 for b in body)
        if lay.id == "ef64":
            hi32_zero += rec[4:8] == b"\0\0\0\0"
    pids = table.pids() if lay.pid_field else []
    asc_viol = sum(1 for a, b in zip(pids, pids[1:]) if not a < b)
    n = max(table.n, 1)
    rep = {
        "layout": lay.id, "layout_status": lay.status, "records": table.n, "bytes": len(table.data),
        "start": table.start, "stride": table.stride,
        "roundtrip_identical": not mismatches and bytes(rebuilt) == table.data,
        "roundtrip_mismatch_records": mismatches[:50],
        "structure": {
            "pid_strictly_ascending": asc_viol == 0, "pid_order_violations": asc_viol,
            "body_all_nibbles_le_14_fraction": all_legal / n,
            "body_nibble_histogram": {str(k): hist.get(k, 0) for k in range(16)},
            "body_modal_value": hist.most_common(1)[0][0] if hist else None,
        },
    }
    if lay.id == "ef64":
        rep["structure"]["bytes_4_7_zero_fraction"] = hi32_zero / n
    checks = {
        "body_nibbles_le_14_ge_99pct": rep["structure"]["body_all_nibbles_le_14_fraction"] >= 0.99,
        "body_modal_value_is_7": rep["structure"]["body_modal_value"] == 7,
    }
    if lay.id in ("ef64",):
        checks["pid_strictly_ascending"] = asc_viol == 0
    rep["structure_checks"] = checks
    rep["structure_ok"] = all(checks.values())
    rep["note"] = ("往返一致只证明本工具对这份数据无损；字段**含义**是否正确要靠 06 §8 第 5–7 步"
                   "（测试向量 + 单点修改 + 实机标定）。")
    return rep


# ============================================================================= diff
def field_changes(layout, old, new):
    vo, vn = layout.to_int(old), layout.to_int(new)
    out = []
    for f in layout.fields:
        a, b = (vo >> f.pos) & f.max_raw, (vn >> f.pos) & f.max_raw
        if a != b:
            out.append({"name": f.name, "where": f.where(), "old_raw": a, "new_raw": b,
                        "old_display": f.to_display(a), "new_display": f.to_display(b)})
    return out


def bit_diff(layout, old, new, table_offset=0):
    """逐字节、逐位差异。table_offset = 该记录在表中的起点（只用于显示表内偏移）。"""
    out = []
    owners = {}
    for f in layout.fields:
        for p in range(f.pos, f.pos + f.width):
            owners[p] = f.name
    for i, (a, b) in enumerate(zip(old, new)):
        if a == b:
            continue
        bits = [k for k in range(8) if (a ^ b) >> k & 1]
        out.append({"record_offset": i, "table_offset": table_offset + i, "old": f"{a:08b}", "new": f"{b:08b}",
                    "changed_bits": bits,
                    "fields": sorted({owners.get(i * 8 + k, "<unmapped>") for k in bits})})
    return out


def format_diff(layout, old, new, table_offset=0):
    lines = []
    for c in field_changes(layout, old, new):
        f = layout.by_name[c["name"]]
        disp = f" (显示 {c['old_display']:+d} → {c['new_display']:+d})" if f.kind == "offset" else ""
        lines.append(f"  字段 {c['name']:<18} {c['old_raw']} → {c['new_raw']}{disp}   [记录 {c['where']}]")
    for d in bit_diff(layout, old, new, table_offset):
        mask = "".join("1" if (7 - k) in d["changed_bits"] else "0" for k in range(8))
        lines.append(f"  字节 记录+0x{d['record_offset']:02X} / 表内 0x{d['table_offset']:08X}: "
                     f"{d['old']} → {d['new']}  改动掩码 {mask}（bit {d['changed_bits']}，"
                     f"{', '.join(d['fields'])}）")
    return "\n".join(lines) if lines else "  （没有改动）"


# ============================================================================= CLI
def _sha(b):
    return hashlib.sha256(b).hexdigest()


def _int(s):
    return int(str(s), 0)


def _locate(args, layout):
    """返回 (data, table|None, index|None, record, source_dict)。"""
    if args.record:
        data = read_input(args.record, layout, "record")
        return data, None, None, data, {"file": args.record, "sha256": _sha(data), "kind": "record"}
    if not args.table:
        raise AppearanceError("需要 --table（解码后的整表）或 --record（单条记录）")
    data = read_input(args.table, layout, "table", args.start, args.stride)
    tab = Table(data, layout, args.start, args.stride)
    if args.index is not None and args.pid is not None:
        raise AppearanceError("--index 与 --pid 只能给一个")
    if args.pid is not None:
        hits = find_pid(tab, args.pid)["aligned_hits"]
        if not hits:
            raise AppearanceError(f"表中没有 PID {args.pid}（用 find 子命令看非对齐命中和提示）")
        if len(hits) > 1:
            raise AppearanceError(f"PID {args.pid} 命中 {len(hits)} 条记录 {[h['index'] for h in hits]}，"
                                  f"有歧义；请用 --index 指定")
        idx = hits[0]["index"]
    elif args.index is not None:
        idx = args.index
    else:
        raise AppearanceError("整表输入需要 --index 或 --pid")
    rec = tab.record(idx)
    return data, tab, idx, rec, {"file": args.table, "sha256": _sha(data), "kind": "table", "record_index": idx,
                                 "table_offset": tab.offset(idx), "start": tab.start, "stride": tab.stride}


def _write_out(args, payload, manifest, inputs):
    out = os.path.abspath(args.out)
    for p in inputs:
        if not p:
            continue
        same = os.path.abspath(p) == out or (os.path.exists(out) and os.path.exists(p) and os.path.samefile(p, out))
        if same:
            raise AppearanceError(f"拒绝：--out {args.out} 与输入是同一个文件；输出必须写到新文件（保留原表用于回退）")
    if os.path.exists(out) and not args.overwrite:
        raise AppearanceError(f"拒绝：{args.out} 已存在（加 --overwrite 才覆盖；输入文件永远不能被覆盖）")
    with open(out, "wb") as fh:
        fh.write(payload)
    manifest["output"] = {"file": args.out, "sha256": _sha(payload), "bytes": len(payload)}
    with open(out + ".patch.json", "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, ensure_ascii=False)


def _apply_and_emit(args, layout, data, tab, idx, rec, src, changes, cmd):
    groups = tuple(g for g, flag in (("header", getattr(args, "allow_header", False)),
                                     ("unused", getattr(args, "allow_unused", False))) if flag)
    new, changed = layout.apply(rec, changes, allow_groups=groups)
    toff = src.get("table_offset", 0)
    print(f"[{cmd}] 布局 {layout.id}（{layout.status}）记录 "
          f"{'#%d 表内 0x%X' % (idx, toff) if tab else '(单条记录文件)'}；改动 {len(changed)} 个字段：")
    print(format_diff(layout, rec, new, toff))
    if tab is not None:
        payload = tab.replaced(idx, new)
        # 自检：表的其余字节完全不变
        o = tab.offset(idx)
        assert payload[:o] == data[:o] and payload[o + layout.record_size:] == data[o + layout.record_size:]
        if layout.pid_field and layout.pid_field.name in changed:
            p0 = tab.pids()
            p1 = Table(payload, layout, tab.start, tab.stride).pids()
            if all(a < b for a, b in zip(p0, p0[1:])) and not all(a < b for a, b in zip(p1, p1[1:])):
                raise AppearanceError("拒绝：改 PID 会破坏表的严格递增顺序（游戏可能按 PID 二分查找）")
    else:
        payload = new
    manifest = {"tool": "tools/data/player_appearance.py", "command": cmd, "layout": layout.id,
                "layout_status": layout.status, "layout_confidence": layout.confidence,
                "offset_space": layout.meta.get("offset_space", ""), "input": src,
                "record_before_hex": rec.hex(), "record_after_hex": new.hex(),
                "field_changes": field_changes(layout, rec, new), "bit_diff": bit_diff(layout, rec, new, toff),
                "preserved": "除上列字段外的所有位（含未分配位）与原记录逐位相同；表的其余字节逐字节相同"}
    if getattr(args, "dry_run", False):
        print("[dry-run] 未写文件")
        return 0
    _write_out(args, payload, manifest, [args.table, args.record, getattr(args, "json", None)])
    print(f"已写入 {args.out}（sha256 {manifest['output']['sha256'][:16]}…）及 {args.out}.patch.json")
    return 0


def cmd_decode(args):
    lay = load_layout(args.layout)
    if args.all:
        if not args.table:
            raise AppearanceError("--all 需要 --table")
        data = read_input(args.table, lay, "table", args.start, args.stride)
        tab = Table(data, lay, args.start, args.stride)
        out = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout
        for i in range(tab.n):
            d = lay.decode(tab.record(i), {"file": args.table, "record_index": i, "table_offset": tab.offset(i)})
            out.write(json.dumps({"index": i, "pid": d["pid"], "record_hex": d["record_hex"],
                                  "values": {n: e["raw"] for n, e in d["fields"].items()}}, ensure_ascii=False) + "\n")
        if args.out:
            out.close()
        return 0
    data, tab, idx, rec, src = _locate(args, lay)
    d = lay.decode(rec, src)
    text = json.dumps(d, indent=1, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"已写入 {args.out}；body14 = {d['body14']}")
    else:
        print(text)
    for w in d["warnings"]:
        print("警告:", w, file=sys.stderr)
    return 0


def cmd_encode(args):
    with open(args.json, encoding="utf-8") as fh:
        js = json.load(fh)
    lay = load_layout(args.layout or js.get("layout") or "")
    if js.get("layout") and js["layout"] != lay.id:
        raise AppearanceError(f"JSON 的布局是 {js['layout']}，命令行给的是 {lay.id}，拒绝")
    if args.table or args.record:
        data, tab, idx, rec, src = _locate(args, lay)
        if js.get("record_hex") and bytes.fromhex(js["record_hex"]) != rec:
            raise AppearanceError("拒绝：JSON 的 record_hex 与表中这条记录不同（JSON 过期或指错记录）。"
                                  "encode 必须从当前原记录出发。")
    else:
        if not js.get("record_hex"):
            raise AppearanceError("JSON 里没有 record_hex（原记录）；请给 --table/--record，或用 decode 的输出")
        data, tab, idx, src = None, None, None, {"kind": "json record_hex", "file": args.json}
        rec = bytes.fromhex(js["record_hex"])
        check_decoded(rec, "record_hex")
        if len(rec) != lay.record_size:
            raise AppearanceError(f"record_hex 长 {len(rec)} B，布局 {lay.id} 需要 {lay.record_size} B")
    spec = js.get("set", js.get("fields", {}))
    if not isinstance(spec, dict) or not spec:
        raise AppearanceError("JSON 需要 fields（decode 输出）或 set {名: 值}")
    changes = {name: lay.resolve_value(name, v, display=False) for name, v in spec.items()}
    return _apply_and_emit(args, lay, data, tab, idx, rec, src, changes, "encode")


def _parse_sets(args, lay):
    pairs = []
    for s in args.set or []:
        if "=" not in s:
            raise AppearanceError(f"--set 需要 名=值，收到 {s!r}")
        k, v = s.split("=", 1)
        pairs.append((k.strip(), v.strip()))
    if args.set_json:
        with open(args.set_json, encoding="utf-8") as fh:
            j = json.load(fh)
        j = j.get("codec_patch", j)
        if isinstance(j, dict) and "fields" in j and isinstance(j["fields"], dict):
            j = j["fields"]
        if not isinstance(j, dict):
            raise AppearanceError("--set-json 需要 {名: 值} 或含 codec_patch 的 JSON")
        pairs += [(k, v) for k, v in j.items() if not k.startswith("_")]
    if not pairs:
        raise AppearanceError("没有要改的字段（--set 名=值 或 --set-json）")
    changes = {}
    for k, v in pairs:
        f = lay.field(k)  # 未知字段在这里被拒绝
        if f.name in changes:
            raise AppearanceError(f"字段 {k} 给了两次")
        changes[f.name] = lay.resolve_value(f.name, v, display=args.display)
        lay.check_legal(f.name, changes[f.name])  # patch 明确要写的值：即使与原值相同也必须合法
    return changes


def cmd_patch(args):
    lay = load_layout(args.layout)
    changes = _parse_sets(args, lay)  # 先校验字段与取值，再读文件
    data, tab, idx, rec, src = _locate(args, lay)
    return _apply_and_emit(args, lay, data, tab, idx, rec, src, changes, "patch")


def cmd_find(args):
    lay = load_layout(args.layout)
    data = read_input(args.table, lay, "table", args.start, args.stride)
    rep = find_pid(Table(data, lay, args.start, args.stride), args.pid)
    print(json.dumps(rep, indent=1, ensure_ascii=False))
    return 0 if rep["aligned_hits"] else 1


def cmd_roundtrip(args):
    lay = load_layout(args.layout)
    data = read_input(args.table, lay, "table", args.start, args.stride)
    rep = roundtrip_check(Table(data, lay, args.start, args.stride))
    rep["input"] = {"file": args.table, "sha256": _sha(data)}
    print(json.dumps(rep, indent=1, ensure_ascii=False))
    if not rep["roundtrip_identical"]:
        return 1
    if args.strict and not rep["structure_ok"]:
        return 1
    return 0


def cmd_fields(args):
    lay = load_layout(args.layout)
    print(f"# {lay.id}: {lay.meta.get('title', '')}\n# 状态 {lay.status}；{lay.confidence}\n"
          f"# 偏移空间：{lay.meta.get('offset_space', '')}")
    print(f"{'name':<26}{'byte:bit':>9}{'w':>4}  {'nibble':<6} {'transform':<8} group   confidence")
    for f in lay.fields:
        print(f"{f.name:<26}{'0x%02X:%d' % (f.byte, f.bit):>9}{f.width:>4}  {str(f.nibble or '-'):<6} "
              f"{f.kind:<8} {f.group:<7} {f.confidence}" + (f"  [冲突: {f.conflict}]" if f.conflict else ""))
    if lay.unmapped_runs:
        print("# 未分配位：" + ", ".join(f"0x{p // 8:02X}:{p % 8} ×{w}" for p, w in lay.unmapped_runs))
    return 0


def build_parser():
    ap = argparse.ArgumentParser(description="PlayerAppearance 位域编解码（schema 见 data/body/layouts/；"
                                             "只接受解码后的表数据）",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def src_opts(p, need_locate=True, layout_required=True):
        p.add_argument("--layout", choices=LAYOUT_IDS, required=layout_required,
                       help="布局 id" + ("" if layout_required else "（省略时取 JSON 里的 layout）"))
        p.add_argument("--table", help="解码后的整表（第 i 条在 start + i*stride）")
        p.add_argument("--start", type=_int, default=0, help="第 0 条记录的表内起点（默认 0）")
        p.add_argument("--stride", type=_int, default=None, help="记录间距（默认 = 行长；PES 20/21 存档里外观块"
                                                                   "嵌在 0x138 字节的球员记录中）")
        if need_locate:
            p.add_argument("--record", help="单条记录文件（长度必须等于行长）")
            p.add_argument("--index", type=_int, help="记录序号")
            p.add_argument("--pid", type=_int, help="按 PID 定位（对齐匹配，必须唯一）")

    def out_opts(p):
        p.add_argument("--out", help="输出文件（必须是新文件）")
        p.add_argument("--overwrite", action="store_true", help="允许覆盖已存在的输出（输入永远不能被覆盖）")
        p.add_argument("--dry-run", action="store_true", help="只打印差异，不写文件")
        p.add_argument("--allow-header", action="store_true", help="允许改 PID/标志等头部字段")
        p.add_argument("--allow-unused", action="store_true", help="允许改实测未用的位")

    p = sub.add_parser("decode", help="记录 → JSON")
    src_opts(p)
    p.add_argument("--all", action="store_true", help="整表逐条输出 JSON Lines")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_decode)

    p = sub.add_parser("encode", help="JSON → 字节（从原记录出发，只写给出的字段）")
    src_opts(p, layout_required=False)
    p.add_argument("json", help="decode 的输出（可编辑 fields 中的 raw/display），或 {layout, record_hex, set:{名:存储值}}")
    out_opts(p)
    p.set_defaults(fn=cmd_encode)

    p = sub.add_parser("patch", help="只改列出的字段，打印逐位差异")
    src_opts(p)
    p.add_argument("--set", action="append", metavar="名=值", help="存储值（加 --display 则为显示值）")
    p.add_argument("--set-json", help="{名: 值} 或含 codec_patch 的 JSON（如 data/body/r9_body_params.json）")
    p.add_argument("--display", action="store_true", help="--set/--set-json 的值按显示值解释（身体项 = 存储值 − 7）")
    out_opts(p)
    p.set_defaults(fn=cmd_patch)

    p = sub.add_parser("find", help="按 PID 扫描解码后的表")
    src_opts(p, need_locate=False)
    p.add_argument("--pid", type=_int, required=True)
    p.set_defaults(fn=cmd_find)

    p = sub.add_parser("roundtrip-check", help="整表往返 + 结构检验")
    src_opts(p, need_locate=False)
    p.add_argument("--strict", action="store_true", help="结构检验不通过也返回 1")
    p.set_defaults(fn=cmd_roundtrip)

    p = sub.add_parser("fields", help="列出布局字段")
    p.add_argument("--layout", choices=LAYOUT_IDS, required=True)
    p.set_defaults(fn=cmd_fields)
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.cmd in ("encode", "patch") and not getattr(args, "dry_run", False) and not args.out:
        ap.error("需要 --out（或 --dry-run）")
    try:
        return args.fn(args)
    except AppearanceError as e:
        print(str(e), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
