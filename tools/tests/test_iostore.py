"""Tests for tools/native/iostore.py on synthetic IoStore containers.

The containers are produced by `SynthBuilder` below, which is written directly from the CUE4Parse
layouts (UE4/IO/Objects/FIoStoreTocHeader.cs, FIoStoreTocResource.cs, FIoChunkId.cs,
FIoOffsetAndLength.cs, FIoStoreTocCompressedBlockEntry.cs, FIoStoreTocEntryMeta.cs,
FIoDirectoryIndexEntry.cs, FIoFileIndexEntry.cs, IoStoreReader.cs) and deliberately does NOT use any
serialisation code from iostore.py, so reader and builder are independent implementations.

Optional independent cross-check: if the CUE4Parse harness (tools/native/cue4parse_check, built to
$CUE4PARSE_CHECK or /opt/tools/cue4parse_check_bin/Cue4ParseCheck) and a .NET runtime are present,
original and patched containers are also opened by CUE4Parse's IoStoreReader.
"""

import hashlib
import json
import os
import random
import struct
import subprocess
import sys
import zlib
from dataclasses import dataclass
from typing import List, Optional, Tuple

import pytest

blake3 = pytest.importorskip("blake3")
lz4_block = pytest.importorskip("lz4.block")
AES = pytest.importorskip("Crypto.Cipher.AES")

HERE = os.path.dirname(os.path.abspath(__file__))
NATIVE = os.path.join(os.path.dirname(HERE), "native")
IOSTORE = os.path.join(NATIVE, "iostore.py")
sys.path.insert(0, NATIVE)
import iostore  # noqa: E402

M64 = (1 << 64) - 1
INVALID = 0xFFFFFFFF
BS = 65536  # compression block size used by the synthetic containers

DOTNET_ROOT = os.environ.get("DOTNET_ROOT", "/opt/tools/dotnet")
CUE4PARSE_CHECK = os.environ.get("CUE4PARSE_CHECK", "/opt/tools/cue4parse_check_bin/Cue4ParseCheck")
OOZ_SRC = os.environ.get("OOZ_SRC", "/opt/tools/ref/ooz")


# =============================================================================================
# Independent builder
# =============================================================================================

def fnv1_seed(raw12: bytes, seed: int) -> int:
    """FIoChunkId.HashWithSeed (FIoChunkId.cs)."""
    h = seed if seed != 0 else 0xCBF29CE484222325
    for b in raw12:
        h = ((h * 0x00000100000001B3) & M64) ^ b
    return h


def make_chunk_id(cid: int, index: int, ctype: int) -> bytes:
    return struct.pack("<Q", cid) + struct.pack(">H", index) + bytes([0, ctype])


def fstr(s: str) -> bytes:
    """UE FString archive form: pure-ASCII -> positive length + ANSI bytes + NUL; anything else ->
    negative length (UTF-16 code units incl. NUL) + UTF-16LE (FString operator<< / IsPureAnsi)."""
    if not s:
        return struct.pack("<i", 0)
    if all(ord(ch) < 128 for ch in s):
        b = s.encode("ascii") + b"\0"
        return struct.pack("<i", len(b)) + b
    b = s.encode("utf-16-le") + b"\0\0"
    return struct.pack("<i", -(len(b) // 2)) + b


def build_directory_index(mount: str, files: List[Tuple[str, int]]) -> bytes:
    """FIoDirectoryIndexResource: mount FString, dir entries, file entries, string table."""
    strings: List[str] = []
    sidx = {}

    def string(name):
        if name not in sidx:
            sidx[name] = len(strings)
            strings.append(name)
        return sidx[name]

    dirs = [[INVALID, INVALID, INVALID, INVALID]]  # root: Name, FirstChild, NextSibling, FirstFile
    children = {0: {}}
    fentries = []
    for rel, toc_index in files:
        parts = rel.split("/")
        d = 0
        for comp in parts[:-1]:
            if comp not in children[d]:
                new = len(dirs)
                dirs.append([string(comp), INVALID, dirs[d][1], INVALID])  # prepend to sibling list
                dirs[d][1] = new
                children[d][comp] = new
                children[new] = {}
            d = children[d][comp]
        fentries.append([string(parts[-1]), dirs[d][3], toc_index])
        dirs[d][3] = len(fentries) - 1
    out = fstr(mount)
    out += struct.pack("<i", len(dirs)) + b"".join(struct.pack("<4I", *x) for x in dirs)
    out += struct.pack("<i", len(fentries)) + b"".join(struct.pack("<3I", *x) for x in fentries)
    out += struct.pack("<i", len(strings)) + b"".join(fstr(s) for s in strings)
    return out


def perfect_hash_layout(cids: List[bytes], max_seed: int):
    """Place chunk ids in slots the way UE's GeneratePerfectHashes does (buckets by hash(0) % seeds,
    largest bucket first, seed search; singletons get -slot-1; failures go to the overflow list
    with a seed whose index is >= chunk count). Returns (slot_of_chunk, seeds, overflow_slots)."""
    n = len(cids)
    seed_count = max(1, int(n / 2 + 0.5))
    buckets = [[] for _ in range(seed_count)]
    for i, c in enumerate(cids):
        buckets[fnv1_seed(c, 0) % seed_count].append(i)
    order = sorted(range(seed_count), key=lambda b: -len(buckets[b]))
    seeds = [0] * seed_count
    free = set(range(n))
    slot_of = {}
    overflow_items = []
    for b in order:
        items = buckets[b]
        if len(items) <= 1:
            continue
        for seed in range(1, max_seed + 1):
            slots = [fnv1_seed(cids[i], seed) % n for i in items]
            if len(set(slots)) == len(slots) and all(s in free for s in slots):
                for i, s in zip(items, slots):
                    slot_of[i] = s
                    free.discard(s)
                seeds[b] = seed
                break
        else:
            overflow_items.extend(items)
            seeds[b] = -(n + 1)
    for b in order:
        if len(buckets[b]) == 1:
            s = min(free)
            free.discard(s)
            slot_of[buckets[b][0]] = s
            seeds[b] = -s - 1
    overflow_slots = []
    for i in overflow_items:
        s = min(free)
        free.discard(s)
        slot_of[i] = s
        overflow_slots.append(s)
    return slot_of, seeds, sorted(overflow_slots)


@dataclass
class SChunk:
    cid: bytes
    data: bytes
    path: Optional[str] = None          # relative to the mount point
    policy: str = "zlib"                # zlib | none | lz4 | alt | pre
    meta_flags: int = 0
    pre_blocks: Optional[List[Tuple[bytes, int]]] = None  # (compressed bytes, uncompressed size), method 1


def build_container(out_dir, name, chunks: List[SChunk], version: int, *, key: Optional[bytes] = None,
                    ctr: bool = False, partition_size: Optional[int] = None, perfect_hash: bool = False,
                    max_seed: int = 1 << 16, meta_algo: str = "sha1", signed: bool = False,
                    methods=("Zlib", "LZ4"), block_size: int = BS, mount: str = "../../../",
                    indexed: bool = True, container_id: Optional[int] = None):
    os.makedirs(out_dir, exist_ok=True)
    rnd = random.Random(hash(name) & 0xFFFF)
    cids = [c.cid for c in chunks]
    seeds, overflow = [], []
    if perfect_hash:
        slot_of, seeds, overflow = perfect_hash_layout(cids, max_seed)
        if version < 5 and overflow:
            raise ValueError("TOC version 4 (PerfectHash) cannot store overflow entries")
        table = [None] * len(chunks)
        for i, s in slot_of.items():
            table[s] = chunks[i]
    else:
        table = list(chunks)

    partitions = [bytearray()]
    blocks, block_sigs, ivs = [], [], []
    offsets, metas = [], []
    any_compressed = False

    def place(disk: bytes) -> int:
        cur = partitions[-1]
        start = (len(cur) + 15) // 16 * 16
        if partition_size is not None and start + len(disk) > partition_size:
            partitions.append(bytearray())
            cur = partitions[-1]
            start = 0
        cur.extend(b"\0" * (start - len(cur)))
        cur.extend(disk)
        psize = partition_size if partition_size is not None else 0
        return (len(partitions) - 1) * psize + start

    for k, ch in enumerate(table):
        start_block = len(blocks)
        if ch.pre_blocks is not None:
            pieces = [(comp, usize, 1) for comp, usize in ch.pre_blocks]
        else:
            pieces = []
            for bi, s in enumerate(range(0, len(ch.data), block_size)):
                piece = ch.data[s:s + block_size]
                pol = ch.policy if ch.policy != "alt" else ("zlib" if bi % 2 == 0 else "none")
                if pol == "zlib":
                    comp, m = zlib.compress(piece, 6), 1 + list(methods).index("Zlib")
                elif pol == "lz4":
                    comp, m = lz4_block.compress(piece, store_size=False), 1 + list(methods).index("LZ4")
                else:
                    comp, m = piece, 0
                if m and len(comp) >= len(piece):  # UE stores incompressible blocks raw
                    comp, m = piece, 0
                pieces.append((comp, len(piece), m))
        for comp, usize, m in pieces:
            any_compressed |= m != 0
            disk = comp + b"\0" * (-len(comp) % 16)
            if key is not None:
                if ctr:
                    iv = rnd.randbytes(12)
                    ivs.append(iv)
                    disk = AES.new(key, AES.MODE_CTR, nonce=iv, initial_value=0).encrypt(disk)
                else:
                    disk = AES.new(key, AES.MODE_ECB).encrypt(disk)
            off = place(disk)
            blocks.append((off, len(comp), usize, m))
            block_sigs.append(hashlib.sha1(disk).digest())
        offsets.append((start_block * block_size, len(ch.data)))
        if meta_algo == "sha1":
            digest = hashlib.sha1(ch.data).digest()
        elif meta_algo == "blake3":
            digest = blake3.blake3(ch.data).digest(length=20)
        else:
            digest = b"\0" * 20
        if version >= 8:
            metas.append(digest + bytes([ch.meta_flags | (1 if any(p[2] for p in pieces) else 0)]) + b"\0\0\0")
        else:
            metas.append(digest + b"\0" * 12 + bytes([ch.meta_flags | (1 if any(p[2] for p in pieces) else 0)]))

    files = [(c.path, k) for k, c in enumerate(table) if c.path]
    dir_index = build_directory_index(mount, files) if indexed else b""
    if key is not None and indexed:
        dir_index += b"\0" * (-len(dir_index) % 16)
        if ctr:
            div = rnd.randbytes(12)
            ivs.append(div)
            dir_index = AES.new(key, AES.MODE_CTR, nonce=div, initial_value=0).encrypt(dir_index)
        else:
            dir_index = AES.new(key, AES.MODE_ECB).encrypt(dir_index)

    flags = 8 if indexed else 0  # Indexed
    if any_compressed:
        flags |= 1
    if key is not None:
        flags |= 2
    if signed:
        flags |= 4
    if version >= 3:
        pcount = len(partitions)
        psize = partition_size if partition_size is not None else M64
    else:
        pcount, psize = 0, 0
    reserved3 = (2 if ctr else (1 if key else 0)) if version >= 10 else 0
    reserved8 = (struct.pack("<I", len(ivs)) if version >= 10 else b"\0" * 4) + b"\0" * 36
    random_container_id = rnd.getrandbits(64)  # always drawn, so the random stream is unchanged
    container_id = random_container_id if container_id is None else container_id
    guid = rnd.randbytes(16) if key else b"\0" * 16
    header = struct.pack("<16sBBHIIIIIIIIIQ16sBBHIQII40s", b"-==--==--==--==-", version, 0, 0, 144,
                         len(table), len(blocks), 12, len(methods), 32, block_size, len(dir_index), pcount,
                         container_id, guid, flags, reserved3, 0,
                         len(seeds) if version >= 4 else 0, psize,
                         len(overflow) if version >= 5 else 0, 0, reserved8)
    assert len(header) == 144
    body = b"".join(c.cid for c in table)
    body += b"".join(o.to_bytes(5, "big") + n.to_bytes(5, "big") for o, n in offsets)
    if version >= 4:
        body += struct.pack(f"<{len(seeds)}i", *seeds)
    if version >= 5:
        body += struct.pack(f"<{len(overflow)}i", *overflow)
    body += b"".join(struct.pack("<QI", o | (cs << 40), us | (m << 24)) for o, cs, us, m in blocks)
    if version >= 10:
        body += b"".join(ivs)
    body += b"".join(m.encode().ljust(32, b"\0") for m in methods)
    if signed:
        body += struct.pack("<i", 256) + rnd.randbytes(256) + rnd.randbytes(256) + b"".join(block_sigs)
    body += dir_index
    body += b"".join(metas)

    utoc = os.path.join(out_dir, name + ".utoc")
    with open(utoc, "wb") as f:
        f.write(header + body)
    for i, p in enumerate(partitions):
        pp = os.path.join(out_dir, name + (".ucas" if i == 0 else f"_s{i}.ucas"))
        with open(pp, "wb") as f:
            f.write(bytes(p))
    return {"utoc": utoc, "table": table, "version": version, "key": key, "meta_algo": meta_algo,
            "partitions": len(partitions), "seeds": seeds, "overflow": overflow, "signed": signed,
            "blocks": len(blocks), "ctr": ctr}


# =============================================================================================
# Corpus
# =============================================================================================

def _payload(seed: int, size: int, compressible: bool = True) -> bytes:
    r = random.Random(seed)
    if not compressible:
        return r.randbytes(size)
    words = [b"ronaldo", b"r9", b"brasil", b"2002", b"pc3600", b"pc3611", b"astc", b"mesh", b"lod0", b"lod1"]
    out = bytearray()
    while len(out) < size:
        out += r.choice(words) + bytes([r.randrange(256)])
    return bytes(out[:size])


def pid(path: str) -> int:
    """Hash-like 64-bit package id (real FPackageIds are CityHash64 of the lower-cased name)."""
    return struct.unpack("<Q", hashlib.sha1(path.encode()).digest()[:8])[0]


def standard_chunks() -> List[SChunk]:
    def mk(n, index, ctype):
        return make_chunk_id(pid(f"pkg{n:x}"), index, ctype)
    return [
        SChunk(mk(0x1111, 0, 2), _payload(1, 1500), "eFootball/Content/Face/pc3600/face_r9.uasset"),
        SChunk(mk(0x1111, 0, 3), _payload(2, 200_000), "eFootball/Content/Face/pc3600/face_r9.ubulk", "alt"),
        SChunk(mk(0x2222, 0, 2), _payload(3, 65536), "eFootball/Content/Tex/pc3611/hair_r9.uasset"),
        SChunk(mk(0x2222, 0, 3), _payload(4, 70_000, compressible=False),
               "eFootball/Content/Tex/pc3611/hair_r9.ubulk", "zlib"),
        SChunk(mk(0x3333, 0, 2), _payload(5, 65537), "eFootball/Content/Tex/pc3611/sub/deep/skin.uasset", "none"),
        SChunk(mk(0x4444, 0, 2), b"", "eFootball/Content/Empty/zero.uasset"),
        SChunk(mk(0x5555, 0, 2), _payload(6, 9000), "Engine/Content/Shared/base.uasset", "lz4"),
        SChunk(mk(0x5555, 0, 5), _payload(7, 140_000), "Engine/Content/Shared/base.m.ubulk", "zlib", meta_flags=2),
        SChunk(mk(0x6666, 1, 4), _payload(8, 33_333), "eFootball/Content/Face/pc3600/face_r9.uptnl", "zlib"),
        SChunk(mk(0x7777, 0, 2), _payload(9, 300), "eFootball/Content/x.uasset", "zlib"),
        SChunk(mk(0x8888, 0, 2), _payload(10, 4096), "eFootball/Content/Face/pc3600/face_other.uasset"),
        SChunk(mk(0xC0FFEE, 0, 10), _payload(11, 777), None, "zlib"),  # container header, no path
    ]


VARIANTS = {
    "v2_dirindex": dict(version=2, meta_algo="sha1"),
    "v3_partitions": dict(version=3, partition_size=192 * 1024, meta_algo="sha1"),
    "v3_aes": dict(version=3, aes=True, meta_algo="sha1"),
    "v4_perfecthash": dict(version=4, perfect_hash=True, meta_algo="blake3"),
    "v5_overflow": dict(version=5, perfect_hash=True, max_seed=0, meta_algo="blake3"),
    "v5_aes_perfecthash": dict(version=5, perfect_hash=True, aes=True, meta_algo="blake3"),
    "v8_iohash": dict(version=8, perfect_hash=True, meta_algo="blake3"),
    "v10_aes_ctr": dict(version=10, perfect_hash=True, aes=True, ctr=True, meta_algo="blake3"),
    "v3_signed": dict(version=3, signed=True, meta_algo="sha1"),
}
PATCHABLE = [v for v in VARIANTS if v != "v3_signed"]


@pytest.fixture(scope="session")
def aes_key() -> bytes:
    return os.urandom(32)


@pytest.fixture(scope="session")
def corpus(tmp_path_factory, aes_key):
    root = tmp_path_factory.mktemp("corpus")
    out = {}
    for name, spec in VARIANTS.items():
        spec = dict(spec)
        use_aes = spec.pop("aes", False)
        out[name] = build_container(str(root / name), "pakchunk_r9", standard_chunks(),
                                    key=aes_key if use_aes else None, **spec)
    return out


@pytest.fixture(scope="session")
def key_file(tmp_path_factory, aes_key):
    p = tmp_path_factory.mktemp("key") / "aes.key"
    p.write_text("0x" + aes_key.hex() + "\n")
    return str(p)


def open_c(info, **kw):
    return iostore.IoStoreContainer(info["utoc"], info["key"], **kw)


def sha(b):
    return hashlib.sha256(b).hexdigest()


def files_digest(utoc):
    base = utoc[:-5]
    d = os.path.dirname(utoc)
    return {f: sha(open(os.path.join(d, f), "rb").read()) for f in sorted(os.listdir(d))
            if f.startswith(os.path.basename(base))}


# =============================================================================================
# Reader tests
# =============================================================================================

@pytest.mark.parametrize("variant", list(VARIANTS))
def test_read_extract_matches_builder(corpus, variant):
    info = corpus[variant]
    with open_c(info) as c:
        assert c.header.version == info["version"]
        assert len(c.toc.chunk_ids) == len(info["table"])
        assert c.header.effective_partition_count == info["partitions"]
        paths = c.path_by_index()
        for i, ch in enumerate(info["table"]):
            assert c.toc.chunk_ids[i].raw == ch.cid
            assert c.read_chunk(i) == ch.data, f"chunk {i}"
            if ch.path:
                assert paths[i] == "../../../" + ch.path
            else:
                assert i not in paths
        assert c.mount_point == "../../../"
        st = c.stats()
        names = [m["name"] for m in st["compression_methods"]]
        assert names == ["None", "Zlib", "LZ4"]


@pytest.mark.parametrize("variant", list(VARIANTS))
def test_toc_roundtrip_byte_identical(corpus, variant):
    data = open(corpus[variant]["utoc"], "rb").read()
    toc = iostore.TocResource.parse(data)
    assert toc.serialize() == data
    assert toc.trailing == b""
    with open_c(corpus[variant]) as c:
        plain = c.directory_index_plain()
        assert iostore.DirectoryIndex.parse(plain).serialize() == plain
        assert iostore.TocHeader.parse(data).serialize() == data[:144]


@pytest.mark.parametrize("variant", list(VARIANTS))
def test_selfcheck_and_hash_detection(corpus, variant):
    with open_c(corpus[variant]) as c:
        r = iostore.selfcheck(c, deep=True)
        assert r["ok"], r["failures"]
        assert r["chunks_readable"] == len(corpus[variant]["table"])
        assert r["meta_hash_detection"]["algo"] == corpus[variant]["meta_algo"]
        assert r["meta_hash_mismatches"]["count"] == 0
        if corpus[variant]["signed"]:
            assert r["block_signature_sha1_match"]["aligned_raw"] == corpus[variant]["blocks"]


@pytest.mark.parametrize("variant", ["v4_perfecthash", "v5_overflow", "v5_aes_perfecthash", "v8_iohash",
                                     "v10_aes_ctr"])
def test_perfect_hash_lookup(corpus, variant):
    info = corpus[variant]
    with open_c(info) as c:
        assert c.toc.perfect_hash_seeds == info["seeds"]
        assert c.toc.chunks_without_perfect_hash == info["overflow"]
        v = c.toc.validate_perfect_hash()
        assert v["present"] and v["ok"], v
        missing = iostore.ChunkId.from_parts(0xDEAD, 0, 2)
        assert c.toc.resolve(missing) is None
    if variant == "v5_overflow":
        assert info["overflow"], "test corpus should exercise the overflow list"


def test_chunk_id_fields():
    cid = iostore.ChunkId(make_chunk_id(0x0123456789ABCDEF, 0x0102, 3))
    assert cid.id == 0x0123456789ABCDEF and cid.index == 0x0102 and cid.type == 3
    assert cid.raw[8:10] == b"\x01\x02"  # network order
    assert cid.type_name(False) == "BulkData" and cid.type_name(True) == "OptionalBulkData"
    assert iostore.ChunkId.from_hex(cid.hex) == cid
    assert cid.hash_with_seed(0) == fnv1_seed(cid.raw, 0)


def test_block_entry_and_offset_bit_layout():
    b = iostore.CompressedBlock((1 << 40) - 17, 0xABCDEF, 0x123456, 7)
    raw = b.pack()
    lo, hi = struct.unpack("<QI", raw)
    assert lo & ((1 << 40) - 1) == (1 << 40) - 17 and lo >> 40 == 0xABCDEF
    assert hi & 0xFFFFFF == 0x123456 and hi >> 24 == 7
    with pytest.raises(iostore.IoStoreError):
        iostore.CompressedBlock(1 << 40, 0, 0, 0).pack()


def test_blake3_pure_matches_package():
    for n in (0, 1, 63, 64, 65, 1023, 1024, 1025, 2048, 3073, 5000, 9 * 1024 + 7):
        d = _payload(n, n, compressible=False)
        assert iostore.blake3_pure(d, 32) == blake3.blake3(d).digest(), n


def test_wrong_key_and_missing_key(corpus):
    info = corpus["v3_aes"]
    with iostore.IoStoreContainer(info["utoc"], None) as c:
        assert c.key_status() == "missing"
        with pytest.raises(iostore.KeyRequired):
            c.read_chunk(0)
    with iostore.IoStoreContainer(info["utoc"], bytes(32)) as c:
        assert c.key_status() == "rejected"


# =============================================================================================
# Patch tests
# =============================================================================================

def _replacement_target(info):
    """The multi-block .ubulk chunk."""
    for i, ch in enumerate(info["table"]):
        if ch.path and ch.path.endswith("face_r9.ubulk"):
            return i
    raise AssertionError


def _check_patched(info, out_utoc, target, new_data, algo):
    with open_c(info) as a, iostore.IoStoreContainer(out_utoc, info["key"]) as b:
        assert b.toc.serialize() == b.toc_bytes
        d = iostore.diff_tables(iostore.hash_all(a), iostore.hash_all(b))
        assert [x["index_b"] for x in d["changed"]] == [target]
        assert d["unchanged"] == len(info["table"]) - 1 and not d["added"] and not d["removed"]
        assert b.read_chunk(target) == new_data
        assert b.paths() == a.paths()
        meta = b.toc.metas[target]
        want = hashlib.sha1(new_data).digest() if algo == "sha1" else blake3.blake3(new_data).digest(length=20)
        assert meta.hash[:20] == want
        assert meta.flags & 1 == 0  # Compressed flag cleared: payload stored uncompressed
        assert meta.flags & 2 == a.toc.metas[target].flags & 2
        for i in range(len(info["table"])):
            if i != target:
                assert b.toc.metas[i] == a.toc.metas[i]
        if a.toc.perfect_hash_seeds:
            assert b.toc.validate_perfect_hash()["ok"]
        r = iostore.selfcheck(b, deep=True)
        assert r["ok"], r["failures"]
        assert r["meta_hash_mismatches"]["count"] == 0


@pytest.mark.parametrize("variant", PATCHABLE)
@pytest.mark.parametrize("rebuild", [False, True])
def test_patch_roundtrip(corpus, variant, rebuild, tmp_path):
    info = corpus[variant]
    target = _replacement_target(info)
    new_data = _payload(99, 3 * BS + 1234, compressible=True)
    before = files_digest(info["utoc"])
    with open_c(info) as c:
        m = iostore.patch_container(c, {target: new_data}, str(tmp_path / "out"), rebuild=rebuild, verify=True)
    assert files_digest(info["utoc"]) == before, "inputs must never change"
    assert m["verify"]["ok"], m["verify"]
    assert m["meta_hash_algorithm"] == info["meta_algo"]
    assert m["mode"]["mode"] == ("rebuild" if rebuild else "append")
    out_utoc = str(tmp_path / "out" / "pakchunk_r9.utoc")
    manifest = json.load(open(tmp_path / "out" / "pakchunk_r9.manifest.json"))
    outs = {os.path.basename(o["path"]): o["sha256"] for o in manifest["outputs"]}
    for f, digest in outs.items():
        assert sha(open(tmp_path / "out" / f, "rb").read()) == digest
    ins = {os.path.basename(o["path"]): o["sha256"] for o in manifest["inputs"]}
    assert ins == {k: v for k, v in before.items()}
    assert manifest["replaced_chunks"][0]["index"] == target
    assert manifest["replaced_chunks"][0]["new_sha256"] == sha(new_data)
    _check_patched(info, out_utoc, target, new_data, info["meta_algo"])
    if rebuild:
        total_in = sum(os.path.getsize(os.path.join(os.path.dirname(info["utoc"]), f)) for f in before
                       if f.endswith(".ucas"))
        total_out = sum(os.path.getsize(tmp_path / "out" / f) for f in outs if f.endswith(".ucas"))
        # the old payload is gone: rebuilt partitions are smaller than "input + appended payload"
        assert total_out < total_in + len(new_data)
        with iostore.IoStoreContainer(out_utoc, info["key"]) as b:
            assert b.toc.layout_report()["orphan_blocks"] == 0


def test_patch_multiple_and_zero_length(corpus, tmp_path):
    info = corpus["v2_dirindex"]
    with open_c(info) as c:
        repl = {0: b"", 5: b"now-not-empty" * 1000, 2: _payload(5, BS)}
        m = iostore.patch_container(c, repl, str(tmp_path / "o"), verify=True)
    assert m["verify"]["ok"]
    with iostore.IoStoreContainer(str(tmp_path / "o" / "pakchunk_r9.utoc")) as b:
        for i, d in repl.items():
            assert b.read_chunk(i) == d


def test_patch_creates_new_partition(corpus, tmp_path):
    info = corpus["v3_partitions"]
    with open_c(info) as c:
        nparts = c.header.effective_partition_count
        m = iostore.patch_container(c, {1: _payload(42, 600_000, compressible=False)}, str(tmp_path / "o"),
                                    verify=True)
    assert m["verify"]["ok"]
    with iostore.IoStoreContainer(str(tmp_path / "o" / "pakchunk_r9.utoc")) as b:
        assert b.header.effective_partition_count > nparts
        assert b.read_chunk(1) == _payload(42, 600_000, compressible=False)
        for bi in range(len(b.toc.blocks)):
            pidx, local, n = b.block_disk_span(bi)
            assert local + n <= 192 * 1024


def test_signed_refused_then_unsigned(corpus, tmp_path):
    info = corpus["v3_signed"]
    with open_c(info) as c:
        with pytest.raises(iostore.IoStoreError, match="Signed"):
            iostore.patch_container(c, {0: b"x"}, str(tmp_path / "o1"))
        assert not os.path.exists(tmp_path / "o1") or not os.listdir(tmp_path / "o1")
        m = iostore.patch_container(c, {0: b"x" * 10}, str(tmp_path / "o2"), unsign=True, verify=True)
    assert m["verify"]["ok"]
    assert any("Signed flag cleared" in w for w in m["warnings"])
    with iostore.IoStoreContainer(str(tmp_path / "o2" / "pakchunk_r9.utoc")) as b:
        assert not b.header.flags & iostore.ContainerFlags.Signed
        assert b.toc.signatures is None
        assert b.read_chunk(0) == b"x" * 10


def test_refuses_to_overwrite_inputs(corpus, tmp_path):
    info = corpus["v2_dirindex"]
    with open_c(info) as c:
        with pytest.raises(iostore.IoStoreError, match="refusing"):
            iostore.patch_container(c, {0: b"x"}, os.path.dirname(info["utoc"]))
        busy = tmp_path / "busy"
        busy.mkdir()
        (busy / "something").write_text("x")
        with pytest.raises(iostore.IoStoreError, match="not empty"):
            iostore.patch_container(c, {0: b"x"}, str(busy))
        with pytest.raises(iostore.IoStoreError, match="refusing"):
            iostore.extract(c, [0], os.path.dirname(info["utoc"]))


def test_patch_requires_key(corpus, tmp_path):
    info = corpus["v3_aes"]
    with iostore.IoStoreContainer(info["utoc"], None) as c:
        with pytest.raises(iostore.KeyRequired):
            iostore.patch_container(c, {0: b"x"}, str(tmp_path / "o"), hash_algo="sha1")


def test_hash_algo_override_and_unknown(corpus, tmp_path):
    info = corpus["v2_dirindex"]
    with open_c(info) as c:
        m = iostore.patch_container(c, {0: b"abc"}, str(tmp_path / "o"), hash_algo="blake3", verify=True)
    assert m["meta_hash_algorithm"] == "blake3"
    # a container whose meta hashes match neither algorithm must not be guessed
    d = tmp_path / "weird"
    w = build_container(str(d), "weird", standard_chunks()[:3], 3, meta_algo="sha1")
    raw = bytearray(open(w["utoc"], "rb").read())
    toc = iostore.TocResource.parse(bytes(raw))
    toc.metas = [iostore.TocEntryMeta(bytes([7]) * 32, m.flags) for m in toc.metas]
    open(w["utoc"], "wb").write(toc.serialize())
    with iostore.IoStoreContainer(w["utoc"]) as c:
        with pytest.raises(iostore.IoStoreError, match="--hash-algo"):
            iostore.patch_container(c, {0: b"abc"}, str(tmp_path / "o2"))


# =============================================================================================
# CLI tests (subprocess) - also checks the key never appears in any output
# =============================================================================================

def run_cli(*args, env_key=None, check=True):
    env = dict(os.environ)
    env.pop("R9_AES_KEY_HEX", None)
    if env_key is not None:
        env["R9_AES_KEY_HEX"] = env_key
    r = subprocess.run([sys.executable, IOSTORE, *args], capture_output=True, text=True, env=env)
    if check and r.returncode not in (0, 3):
        raise AssertionError(f"{args}: rc={r.returncode}\n{r.stdout}\n{r.stderr}")
    return r


def test_cli_end_to_end_encrypted(corpus, tmp_path, aes_key, key_file):
    info = corpus["v3_aes"]
    keyhex = aes_key.hex()
    outputs = []
    r = run_cli("info", info["utoc"], env_key=keyhex)
    outputs.append(r.stdout + r.stderr)
    j = json.loads(r.stdout)
    assert j["key"] == "accepted" and j["toc_reserialize_identical"]
    assert j["header"]["container_flag_names"] == ["Compressed", "Encrypted", "Indexed"]
    assert [m["name"] for m in j["compression_methods"]] == ["None", "Zlib", "LZ4"]
    r = run_cli("list", info["utoc"], "--json", "--aes-key-file", key_file)
    outputs.append(r.stdout + r.stderr)
    rows = json.loads(r.stdout)
    assert len(rows) == len(info["table"])
    r = run_cli("extract", info["utoc"], "--aes-key-file", key_file, "--glob", "*/pc3600/*", "--out-dir",
                str(tmp_path / "ex"), "--verify")
    outputs.append(r.stdout + r.stderr)
    want = {ch.path: ch.data for ch in info["table"] if ch.path and "/pc3600/" in ch.path}
    for p, d in want.items():
        assert open(tmp_path / "ex" / p, "rb").read() == d
    exm = json.load(open(tmp_path / "ex" / "extract_manifest.json"))
    assert exm["verify"]["ok"] and len(exm["outputs"]) == len(want)
    outputs.append(json.dumps(exm))
    newfile = tmp_path / "new.bin"
    newfile.write_bytes(_payload(123, 100_000))
    r = run_cli("patch", info["utoc"], "--out-dir", str(tmp_path / "p"), "--replace",
                "eFootball/Content/Face/pc3600/face_r9.ubulk", str(newfile), "--verify", env_key=keyhex)
    outputs.append(r.stdout + r.stderr)
    pj = json.loads(r.stdout)
    assert pj["verify"] is True
    outputs.append(open(tmp_path / "p" / "pakchunk_r9.manifest.json").read())
    r = run_cli("hash-all", info["utoc"], "--out", str(tmp_path / "a.json"), env_key=keyhex)
    r = run_cli("hash-all", str(tmp_path / "p" / "pakchunk_r9.utoc"), "--out", str(tmp_path / "b.json"),
                env_key=keyhex)
    outputs += [open(tmp_path / "a.json").read(), open(tmp_path / "b.json").read()]
    r = run_cli("diff", str(tmp_path / "a.json"), str(tmp_path / "b.json"))
    d = json.loads(r.stdout)
    assert r.returncode == 3 and len(d["changed"]) == 1
    assert d["changed"][0]["path"].endswith("face_r9.ubulk")
    r = run_cli("diff", info["utoc"], str(tmp_path / "p" / "pakchunk_r9.utoc"), "--aes-key-file", key_file)
    assert len(json.loads(r.stdout)["changed"]) == 1
    r = run_cli("selfcheck", str(tmp_path / "p" / "pakchunk_r9.utoc"), "--deep", env_key=keyhex)
    outputs.append(r.stdout + r.stderr)
    assert json.loads(r.stdout)["ok"]
    # refuse to overwrite an existing hash-all output
    r = run_cli("hash-all", info["utoc"], "--out", str(tmp_path / "a.json"), env_key=keyhex, check=False)
    assert r.returncode == 2 and "refusing" in r.stderr
    blob = "\n".join(outputs)
    for form in (keyhex, keyhex.upper(), aes_key.hex(" ")):
        assert form not in blob


def test_cli_patch_without_key_fails_cleanly(corpus, tmp_path):
    info = corpus["v3_aes"]
    f = tmp_path / "x.bin"
    f.write_bytes(b"1")
    r = run_cli("patch", info["utoc"], "--out-dir", str(tmp_path / "o"), "--replace", "index:0", str(f),
                check=False)
    assert r.returncode == 2 and "encrypted" in r.stderr


# =============================================================================================
# Oodle path (open-source ooz decoder + Oodle-compressed corpus shipped with ooz)
# =============================================================================================

@pytest.fixture(scope="session")
def ooz_lib(tmp_path_factory):
    """Oodle tests are opt-in because they run third-party native code:
      R9_OODLE_LIB=<liboo2core*.so | libooz.so>   use an Oodle decoder you already have, or
      R9_TEST_BUILD_OOZ=1                           compile OOZ_SRC (powzix/ooz) with `build-ooz` first.
    The Oodle-compressed test data always comes from OOZ_SRC/testdata."""
    if not os.path.isdir(os.path.join(OOZ_SRC, "testdata")):
        pytest.skip("ooz checkout with testdata not found (set OOZ_SRC)")
    lib = os.environ.get("R9_OODLE_LIB")
    if lib:
        if not os.path.exists(lib):
            pytest.fail(f"R9_OODLE_LIB={lib} does not exist")
        return lib
    if os.environ.get("R9_TEST_BUILD_OOZ") != "1":
        pytest.skip("Oodle tests are opt-in: set R9_OODLE_LIB=<decoder lib> or R9_TEST_BUILD_OOZ=1")
    out = tmp_path_factory.mktemp("ooz") / "lib"
    m = iostore.build_ooz(OOZ_SRC, str(out), verify=True)
    return m["outputs"][0]["path"]


def _ooz_file(name):
    """ooz test files: 8-byte (or legacy 4-byte) little-endian uncompressed size, then the Oodle stream."""
    d = open(os.path.join(OOZ_SRC, "testdata", name), "rb").read()
    n = struct.unpack_from("<Q", d)[0]
    if n >= 0x10000000000:
        return d[4:], struct.unpack_from("<I", d)[0]
    return d[8:], n


@pytest.fixture(scope="session")
def oodle_container(tmp_path_factory, ooz_lib):
    k, n = _ooz_file("xml.kraken")
    mm, n2 = _ooz_file("xml.mermaid")
    lv, n3 = _ooz_file("xml.leviathan")
    sk, n4 = _ooz_file("xml.selkie")
    assert n == n2 == n3 == n4
    chunks = [
        SChunk(make_chunk_id(0xA1, 0, 2), b"\0" * (2 * n), "Game/Content/oodle_two_blocks.uasset", "pre",
               pre_blocks=[(k, n), (mm, n)]),
        SChunk(make_chunk_id(0xA2, 0, 2), b"\0" * n, "Game/Content/oodle_leviathan.uasset", "pre",
               pre_blocks=[(lv, n)]),
        SChunk(make_chunk_id(0xA3, 0, 2), b"\0" * n, "Game/Content/oodle_selkie.uasset", "pre",
               pre_blocks=[(sk, n)]),
        SChunk(make_chunk_id(0xA4, 0, 2), _payload(77, 5000), "Game/Content/plain.uasset", "none"),
    ]
    d = tmp_path_factory.mktemp("oodle")
    info = build_container(str(d), "oodle_r9", chunks, 3, methods=("Oodle",), block_size=n, meta_algo="zero")
    return info, n


def test_oodle_without_decoder_is_reported(tmp_path, monkeypatch):
    """No third-party code: blocks are labelled Oodle (contents opaque); the tool must say what is
    missing, extract must record failures in its manifest, and patch --verify must still prove the
    untouched chunks via raw block equality."""
    monkeypatch.delenv("R9_OODLE_LIB", raising=False)
    fake = [SChunk(make_chunk_id(pid("o1"), 0, 2), b"\0" * 1000, "Game/Content/o1.uasset", "pre",
                   pre_blocks=[(os.urandom(300), 1000)]),
            SChunk(make_chunk_id(pid("o2"), 0, 2), _payload(3, 500), "Game/Content/o2.uasset", "none")]
    info = build_container(str(tmp_path / "in"), "oodle_fake", fake, 3, methods=("Oodle",), meta_algo="zero")
    with iostore.IoStoreContainer(info["utoc"]) as c:
        m = [x for x in c.stats()["compression_methods"] if x["name"] == "Oodle"][0]
        assert m["blocks"] == 1 and not m["decoder_available"] and "build-ooz" in m["decoder"]
        with pytest.raises(iostore.UnsupportedCompression):
            c.read_chunk(0)
        with pytest.raises(iostore.IoStoreError, match="could not be extracted"):
            iostore.extract(c, [0, 1], str(tmp_path / "ex"))
        ex = json.load(open(tmp_path / "ex" / "extract_manifest.json"))
        assert [f["index"] for f in ex["failed"]] == [0] and [o["index"] for o in ex["outputs"]] == [1]
        with pytest.raises(iostore.IoStoreError, match="--hash-algo"):
            iostore.patch_container(c, {1: b"new"}, str(tmp_path / "p0"))
        r = iostore.patch_container(c, {1: b"new"}, str(tmp_path / "p"), hash_algo="zero", verify=True)
    assert r["verify"]["ok"]
    assert r["verify"]["checks"]["unchanged_chunks_raw_blocks_identical"] == 1
    assert r["verify"]["checks"]["unchanged_chunks_payload_check_skipped_no_decoder"] == 1


def test_oodle_container_read_and_patch(oodle_container, ooz_lib, tmp_path):
    info, n = oodle_container
    dec_none = iostore.Decompressor(None, use_env=False)
    with iostore.IoStoreContainer(info["utoc"], None, dec_none) as c:
        st = c.stats()
        oodle = [m for m in st["compression_methods"] if m["name"] == "Oodle"][0]
        assert oodle["blocks"] == 4 and oodle["decoder_available"] is False
        assert "R9_OODLE_LIB" in oodle["decoder"]
        with pytest.raises(iostore.UnsupportedCompression):
            c.read_chunk(0)
    dec = iostore.Decompressor(ooz_lib)
    with iostore.IoStoreContainer(info["utoc"], None, dec) as c:
        a = c.read_chunk(0)
        b = c.read_chunk(1)
        s = c.read_chunk(2)
        assert len(a) == 2 * n and a[:n] == a[n:] == b == s
        assert c.read_chunk(3) == _payload(77, 5000)
        # Silesia "xml" is a tar archive: check the POSIX tar magic of the first member header
        assert b.startswith(b"elts.xml\0") and b[257:262] == b"ustar"
        m = iostore.patch_container(c, {3: b"patched" * 100}, str(tmp_path / "o"), verify=True)
    assert m["meta_hash_algorithm"] == "zero"
    assert m["verify"]["ok"] and m["verify"]["checks"]["unchanged_chunks_payload_identical"] == 3
    # without the decoder the verify still proves unchanged chunks via raw block equality
    with iostore.IoStoreContainer(info["utoc"], None, dec_none) as c:
        m2 = iostore.patch_container(c, {3: b"patched" * 100}, str(tmp_path / "o2"), hash_algo="zero",
                                     verify=True)
    assert m2["verify"]["ok"]
    assert m2["verify"]["checks"]["unchanged_chunks_raw_blocks_identical"] == 3
    assert m2["verify"]["checks"]["unchanged_chunks_payload_check_skipped_no_decoder"] == 3


# =============================================================================================
# Independent third-party reader: CUE4Parse
# =============================================================================================

def _cue4parse_available():
    return os.path.exists(CUE4PARSE_CHECK) and os.path.exists(os.path.join(DOTNET_ROOT, "dotnet"))


def run_cue4parse(utoc, key: Optional[bytes] = None, game="GAME_eFootball"):
    env = dict(os.environ, DOTNET_ROOT=DOTNET_ROOT)
    env.pop("R9_AES_KEY_HEX", None)
    if key is not None:
        env["R9_AES_KEY_HEX"] = key.hex()
    r = subprocess.run([CUE4PARSE_CHECK, utoc, "--game", game], capture_output=True, text=True, env=env,
                       timeout=600)
    assert r.returncode in (0, 1), r.stderr
    j = json.loads(r.stdout)
    if key is not None:
        assert key.hex() not in r.stdout and key.hex().upper() not in r.stdout
    return j


def _assert_cue4parse_agrees(utoc, key, game="GAME_eFootball"):
    j = run_cue4parse(utoc, key, game)
    assert j["errors"] == [], j["errors"]
    with iostore.IoStoreContainer(utoc, key, iostore.Decompressor(os.environ.get("R9_OODLE_LIB"))) as c:
        assert j["header"]["version"] == c.header.version
        assert j["header"]["entry_count"] == len(c.toc.chunk_ids)
        assert j["header"]["compressed_block_entry_count"] == len(c.toc.blocks)
        paths = c.path_by_index()
        for e in j["chunks"]:
            i = e["index"]
            assert e["chunk_id"] == c.toc.chunk_ids[i].hex
            assert e["resolve_ok"], e
            assert e["sha256"] == sha(c.read_chunk(i)), f"chunk {i}"
            if i in paths:
                assert e.get("path") == iostore.strip_mount(paths[i]), (e.get("path"), paths[i])
            if c.toc.metas is not None:
                assert e["meta_hash20"] == c.toc.metas[i].hash[:20].hex()
    return j


cue4parse = pytest.mark.skipif(not _cue4parse_available(), reason="CUE4Parse harness / .NET runtime not built")


@cue4parse
@pytest.mark.parametrize("variant", [v for v in PATCHABLE if v != "v10_aes_ctr"])
def test_cue4parse_reads_original_synthetic(corpus, variant):
    _assert_cue4parse_agrees(corpus[variant]["utoc"], corpus[variant]["key"])


@cue4parse
@pytest.mark.parametrize("variant", [v for v in PATCHABLE if v != "v10_aes_ctr"])
@pytest.mark.parametrize("rebuild", [False, True])
def test_cue4parse_reads_our_patched_output(corpus, variant, rebuild, tmp_path):
    info = corpus[variant]
    target = _replacement_target(info)
    new_data = _payload(1234, 2 * BS + 99)
    with open_c(info) as c:
        iostore.patch_container(c, {target: new_data, 0: b"tiny"}, str(tmp_path / "o"), rebuild=rebuild,
                                verify=True)
    out = str(tmp_path / "o" / "pakchunk_r9.utoc")
    j = _assert_cue4parse_agrees(out, info["key"])
    by_index = {e["index"]: e for e in j["chunks"]}
    assert by_index[target]["sha256"] == sha(new_data)
    assert by_index[0]["sha256"] == sha(b"tiny")


@cue4parse
def test_cue4parse_reads_unsigned_output(corpus, tmp_path):
    info = corpus["v3_signed"]
    _assert_cue4parse_agrees(info["utoc"], None)
    with open_c(info) as c:
        iostore.patch_container(c, {1: b"unsigned!" * 50}, str(tmp_path / "o"), unsign=True, verify=True)
    _assert_cue4parse_agrees(str(tmp_path / "o" / "pakchunk_r9.utoc"), None)


@cue4parse
def test_cue4parse_reads_aes_ctr_v10(corpus, tmp_path):
    info = corpus["v10_aes_ctr"]
    _assert_cue4parse_agrees(info["utoc"], info["key"], game="GAME_UE6_0")
    with open_c(info) as c:
        iostore.patch_container(c, {_replacement_target(info): _payload(5, 150_000)}, str(tmp_path / "o"),
                                verify=True)
    _assert_cue4parse_agrees(str(tmp_path / "o" / "pakchunk_r9.utoc"), info["key"], game="GAME_UE6_0")


@cue4parse
def test_cue4parse_reads_oodle_container(oodle_container, ooz_lib, tmp_path, monkeypatch):
    info, n = oodle_container
    monkeypatch.setenv("R9_OODLE_LIB", ooz_lib)
    j = run_cue4parse(info["utoc"])
    if j["errors"]:
        pytest.xfail(f"CUE4Parse could not decode Oodle without its native library: {j['errors'][:2]}")
    _assert_cue4parse_agrees(info["utoc"], None)


# =============================================================================================
# Adversarial review tests (independent reviewer). Each test names the defect class it probes; the
# ones marked "REGRESSION" failed (or silently produced a broken container) before the review fixes.
# =============================================================================================

SMALL_BS = 4096
EDGE_SIZES = [0, 1, 15, 16, 17, SMALL_BS - 1, SMALL_BS, SMALL_BS + 1,
              25 * SMALL_BS - 1, 25 * SMALL_BS, 25 * SMALL_BS + 1]


def _edge_chunks(tag: str) -> List[SChunk]:
    out = []
    for k, n in enumerate(EDGE_SIZES):
        out.append(SChunk(make_chunk_id(pid(f"{tag}{k}"), 0, 2 + (k % 2)),
                          _payload(k, n, compressible=(k % 3 != 0)),
                          f"Game/Content/edge/{tag}_{k}.uasset", ["zlib", "none", "alt", "lz4"][k % 4]))
    return out


EDGE_CONFIGS = {
    "v3_plain": dict(version=3, meta_algo="sha1"),
    "v3_aes_ecb": dict(version=3, aes=True, meta_algo="sha1"),
    "v5_aes_perfecthash": dict(version=5, aes=True, perfect_hash=True, meta_algo="blake3"),
    "v10_aes_ctr": dict(version=10, aes=True, ctr=True, perfect_hash=True, meta_algo="blake3"),
}


def _maybe_cue4parse(utoc, key, version):
    if _cue4parse_available():
        _assert_cue4parse_agrees(utoc, key, game="GAME_UE6_0" if version >= 10 else "GAME_eFootball")


@pytest.mark.parametrize("cfg", list(EDGE_CONFIGS))
def test_adv_block_math_edges(cfg, tmp_path, aes_key):
    """Off-by-one in block math: chunk sizes 0, 1, 15/16/17 (AES), bs-1/bs/bs+1 (last partial block),
    25 blocks +-1 (chunk spanning many blocks), on a 4 KiB block size; read, then patch every size
    into chunks of every other size (append and rebuild), read back, and let CUE4Parse read it."""
    spec = dict(EDGE_CONFIGS[cfg])
    key = aes_key if spec.pop("aes", False) else None
    info = build_container(str(tmp_path / "in"), "edge", _edge_chunks(cfg), block_size=SMALL_BS, key=key, **spec)
    with open_c(info) as c:
        for i, ch in enumerate(info["table"]):
            assert c.read_chunk(i) == ch.data, i
            assert c.chunk_info(i)["blocks"] == -(-len(ch.data) // SMALL_BS), i
        n = len(info["table"])
        for rebuild in (False, True):
            for j, size in enumerate(EDGE_SIZES):
                tgt, other = j % n, (j + 5) % n
                repl = {tgt: _payload(500 + j, size, compressible=False),
                        other: _payload(600 + j, EDGE_SIZES[-1 - j])}
                out = tmp_path / f"o_{int(rebuild)}_{j}"
                m = iostore.patch_container(c, repl, str(out), rebuild=rebuild, verify=True)
                assert m["verify"]["ok"], m["verify"]["failures"]
                with iostore.IoStoreContainer(str(out / "edge.utoc"), key) as o:
                    for i, ch in enumerate(info["table"]):
                        assert o.read_chunk(i) == repl.get(i, ch.data), (rebuild, j, i)
                    for i, d in repl.items():
                        assert o.chunk_info(i)["blocks"] == -(-len(d) // SMALL_BS)
                    r = iostore.selfcheck(o, deep=True)
                    assert r["ok"] and r["meta_hash_mismatches"]["count"] == 0, r["failures"]
                    assert not r["disk_spans_aligned16"]["overlapping_block_pairs"]
                    assert not r["disk_spans_aligned16"]["beyond_partition_eof"]
                if j in (2, 7, 10):
                    _maybe_cue4parse(str(out / "edge.utoc"), key, spec["version"])


def test_adv_chunk_spanning_hundreds_of_blocks(tmp_path):
    """One chunk over 300 blocks with alternating zlib/raw blocks, replaced by 401 blocks + 1 byte."""
    big = _payload(77, 300 * SMALL_BS + 123)
    chunks = [SChunk(make_chunk_id(pid("big"), 0, 3), big, "Game/Content/big.ubulk", "alt"),
              SChunk(make_chunk_id(pid("tail"), 0, 2), _payload(78, 999), "Game/Content/tail.uasset")]
    info = build_container(str(tmp_path / "in"), "big", chunks, 3, block_size=SMALL_BS)
    new = _payload(79, 401 * SMALL_BS + 1, compressible=False)
    with open_c(info) as c:
        assert c.read_chunk(0) == big and c.chunk_info(0)["blocks"] == 301
        for rebuild in (False, True):
            out = tmp_path / f"o{int(rebuild)}"
            m = iostore.patch_container(c, {0: new}, str(out), rebuild=rebuild, verify=True)
            assert m["verify"]["ok"]
            assert m["replaced_chunks"][0]["new_blocks"][1] - m["replaced_chunks"][0]["new_blocks"][0] + 1 == 402
            with iostore.IoStoreContainer(str(out / "big.utoc")) as o:
                assert o.read_chunk(0) == new and o.read_chunk(1) == chunks[1].data
            _maybe_cue4parse(str(out / "big.utoc"), None, 3)


@pytest.mark.parametrize("variant", ["v3_aes", "v5_overflow", "v10_aes_ctr", "v3_partitions"])
@pytest.mark.parametrize("rebuild", [False, True])
def test_adv_zero_length_both_directions(corpus, variant, rebuild, tmp_path):
    """Zero-length chunks: non-empty -> empty and empty -> non-empty, encrypted (ECB/CTR),
    perfect hash with overflow, multi-partition."""
    info = corpus[variant]
    empty = [i for i, ch in enumerate(info["table"]) if not ch.data][0]
    full = _replacement_target(info)
    repl = {empty: _payload(31, 3 * BS + 17), full: b""}
    with open_c(info) as c:
        m = iostore.patch_container(c, repl, str(tmp_path / "o"), rebuild=rebuild, verify=True)
    assert m["verify"]["ok"], m["verify"]["failures"]
    out = str(tmp_path / "o" / "pakchunk_r9.utoc")
    with iostore.IoStoreContainer(out, info["key"]) as o:
        assert o.read_chunk(empty) == repl[empty] and o.read_chunk(full) == b""
        assert o.chunk_info(full)["blocks"] == 0
        assert iostore.selfcheck(o, deep=True)["ok"]
    _maybe_cue4parse(out, info["key"], info["version"])


def test_adv_multi_partition_layouts(tmp_path):
    """Multiple partitions: chunks that straddle partition files, a patch of a chunk that lives in a
    middle partition, appends that open two new partitions, and a rebuild that shrinks the partition
    count (no stray _sN.ucas left, header PartitionCount consistent, no block crosses PartitionSize)."""
    psize = 3 * SMALL_BS + 512
    chunks = [SChunk(make_chunk_id(pid(f"p{k}"), 0, 2), _payload(40 + k, n, compressible=False),
                     f"Game/Content/part/p{k}.uasset", "none")
              for k, n in enumerate([5 * SMALL_BS + 7, SMALL_BS, 7 * SMALL_BS, 100, 2 * SMALL_BS + 1])]
    info = build_container(str(tmp_path / "in"), "parts", chunks, 3, block_size=SMALL_BS,
                           partition_size=psize)
    assert info["partitions"] >= 5
    with open_c(info) as c:
        for i, ch in enumerate(info["table"]):
            assert c.read_chunk(i) == ch.data
        mid = 2  # 7 blocks, lives in the middle partitions
        assert c.block_disk_span(c.toc.chunk_block_range(mid)[0])[0] not in (0, info["partitions"] - 1)
        new = _payload(90, 6 * SMALL_BS + 3, compressible=False)
        m = iostore.patch_container(c, {mid: new}, str(tmp_path / "a"), verify=True)
        assert m["verify"]["ok"] and any("new partition" in w for w in m["warnings"])
        small = {0: b"s" * 10, 2: b"t" * 20, 4: b"u" * 30}
        m2 = iostore.patch_container(c, small, str(tmp_path / "r"), rebuild=True, verify=True)
        assert m2["verify"]["ok"]
    for d, repl in (("a", {mid: new}), ("r", small)):
        utoc = str(tmp_path / d / "parts.utoc")
        with iostore.IoStoreContainer(utoc) as o:
            n = o.header.effective_partition_count
            assert o.header.partition_count == n
            assert sorted(os.path.basename(p) for p in os.listdir(tmp_path / d) if p.endswith(".ucas")) == \
                sorted(os.path.basename(p) for p in o.partition_paths)
            for bi in range(len(o.toc.blocks)):
                pidx, local, _ = o.block_disk_span(bi)
                assert local + iostore.align_up(o.toc.blocks[bi].compressed_size, 16) <= psize
            for i, ch in enumerate(info["table"]):
                assert o.read_chunk(i) == repl.get(i, ch.data)
        _maybe_cue4parse(utoc, None, 3)
    with iostore.IoStoreContainer(str(tmp_path / "r" / "parts.utoc")) as o:
        assert o.header.effective_partition_count < info["partitions"]


NON_ASCII = ["eFootball/Content/Ronaldo_Nazário/face_r9.uasset",
             "eFootball/Content/球员/罗纳尔多/hair_r9.ubulk",
             "eFootball/Content/émoji_😀/x.uasset",
             "eFootball/Content/plain/a.uasset"]


def test_adv_non_ascii_paths(tmp_path, aes_key):
    """Non-ASCII directory index names are UTF-16 FStrings (negative length); they must parse,
    re-serialise byte-exactly, be selectable from the CLI, extract to the right files, and match
    CUE4Parse's paths. Includes a non-BMP character (surrogate pair)."""
    chunks = [SChunk(make_chunk_id(pid(p), 0, 2 if p.endswith("uasset") else 3), _payload(i, 3000 + i), p)
              for i, p in enumerate(NON_ASCII)]
    info = build_container(str(tmp_path / "in"), "uni", chunks, 3, key=aes_key)
    keyhex = aes_key.hex()
    with open_c(info) as c:
        d = c.directory_index()
        assert any(s.wide for s in d.strings) and not d.mount_point.wide
        assert d.serialize() == c.directory_index_plain()
        assert sorted(p for p, _ in c.paths()) == sorted("../../../" + p for p in NON_ASCII)
        assert iostore.resolve_selector(c, NON_ASCII[1]) == 1
    newf = tmp_path / "new.bin"
    newf.write_bytes(b"R9" * 4000)
    r = run_cli("patch", info["utoc"], "--out-dir", str(tmp_path / "p"), "--replace", NON_ASCII[1], str(newf),
                "--verify", env_key=keyhex)
    assert json.loads(r.stdout)["verify"] is True
    out = str(tmp_path / "p" / "uni.utoc")
    r = run_cli("extract", out, "--glob", "*罗纳尔多*", "--glob", "*😀*", "--out-dir", str(tmp_path / "ex"),
                "--verify", env_key=keyhex)
    assert (tmp_path / "ex" / NON_ASCII[1]).read_bytes() == b"R9" * 4000
    assert (tmp_path / "ex" / NON_ASCII[2]).read_bytes() == chunks[2].data
    blob = r.stdout + r.stderr + (tmp_path / "p" / "uni.manifest.json").read_text(encoding="utf-8")
    assert keyhex not in blob and keyhex.upper() not in blob
    _maybe_cue4parse(out, aes_key, 3)


@pytest.mark.parametrize("variant", ["v3_aes", "v5_aes_perfecthash"])
@pytest.mark.parametrize("rebuild", [False, True])
def test_adv_encrypted_and_compressed(corpus, variant, rebuild, tmp_path):
    """Encrypted + compressed: replace a zlib chunk and the LZ4 chunk with sizes that are not
    multiples of 16; every untouched (compressed, encrypted) block must be copied byte-for-byte, the
    new blocks must occupy whole AES blocks on disk and decrypt with the same key."""
    info = corpus[variant]
    lz4_idx = [i for i, ch in enumerate(info["table"]) if ch.policy == "lz4"][0]
    zlib_idx = [i for i, ch in enumerate(info["table"]) if ch.path and ch.path.endswith("face_r9.uptnl")][0]
    repl = {lz4_idx: _payload(61, 12_345), zlib_idx: _payload(62, BS + 33, compressible=False)}
    with open_c(info) as c:
        m = iostore.patch_container(c, repl, str(tmp_path / "o"), rebuild=rebuild, verify=True)
        assert m["verify"]["checks"]["unchanged_chunks_raw_blocks_identical"] == len(info["table"]) - 2
        assert m["verify"]["checks"]["unchanged_chunks_payload_identical"] == len(info["table"]) - 2
    out = str(tmp_path / "o" / "pakchunk_r9.utoc")
    with iostore.IoStoreContainer(out, info["key"]) as o:
        for i, d in repl.items():
            first, last, _ = o.toc.chunk_block_range(i)
            for bi in range(first, last + 1):
                b = o.toc.blocks[bi]
                assert b.method_index == 0 and b.offset % 16 == 0
                assert len(o.read_block_raw(bi)) % 16 == 0
            assert o.read_chunk(i) == d
        assert any(o.toc.blocks[bi].method_index for bi in range(len(o.toc.blocks)))  # still compressed elsewhere
    with iostore.IoStoreContainer(out, bytes(32)) as wrong:
        assert wrong.key_status() == "rejected"
    _maybe_cue4parse(out, info["key"], info["version"])


@pytest.mark.parametrize("ctr", [False, True])
def test_adv_wrong_key_without_directory_index_is_refused(tmp_path, aes_key, ctr):
    """REGRESSION: an encrypted container WITHOUT a directory index (no Indexed flag) gave the writer
    no mount point to test the key on, so a wrong key was accepted, new blocks were encrypted with it
    and the patch 'succeeded' (verify then crashed with an uncaught zlib.error)."""
    version = 10 if ctr else 8
    info = build_container(str(tmp_path / "in"), "noidx", standard_chunks(), version, key=aes_key, ctr=ctr,
                           meta_algo="blake3", indexed=False, perfect_hash=True)
    wrong = bytes(b ^ 0x5A for b in aes_key)
    with iostore.IoStoreContainer(info["utoc"], wrong) as c:
        assert c.directory_index() is None and c.key_status() == "rejected"
        with pytest.raises(iostore.KeyRequired):
            iostore.patch_container(c, {1: b"x" * 100}, str(tmp_path / "bad"), verify=True)
        with pytest.raises(iostore.KeyRequired):
            iostore.patch_container(c, {1: b"x" * 100}, str(tmp_path / "bad2"), hash_algo="blake3")
    assert not (tmp_path / "bad").exists() or not os.listdir(tmp_path / "bad")
    with open_c(info) as c:
        assert c.key_status().startswith("accepted")
        m = iostore.patch_container(c, {1: b"x" * 100}, str(tmp_path / "ok"), verify=True)
    assert m["verify"]["ok"] and m["aes_key_check"]["status"].startswith("accepted")
    # CLI: wrong key -> clean refusal (exit 2), key never echoed
    r = run_cli("patch", info["utoc"], "--out-dir", str(tmp_path / "cli"), "--replace", "index:1", info["utoc"],
                env_key=wrong.hex(), check=False)
    assert r.returncode == 2 and "Traceback" not in r.stderr
    assert wrong.hex() not in r.stdout + r.stderr and aes_key.hex() not in r.stdout + r.stderr


def test_adv_unverifiable_key_needs_explicit_opt_in(tmp_path, aes_key):
    """Encrypted, no directory index, only uncompressed chunks and all-zero meta hashes: nothing can
    prove the key, so the writer must refuse unless --allow-unverified-key is given."""
    chunks = [SChunk(make_chunk_id(pid(f"u{k}"), 0, 2), _payload(k, 5000), None, "none") for k in range(3)]
    info = build_container(str(tmp_path / "in"), "unv", chunks, 3, key=aes_key, meta_algo="zero", indexed=False)
    with open_c(info) as c:
        assert c.key_status().startswith("unverifiable")
        with pytest.raises(iostore.IoStoreError, match="allow-unverified-key"):
            iostore.patch_container(c, {0: b"y" * 64}, str(tmp_path / "o1"))
        m = iostore.patch_container(c, {0: b"y" * 64}, str(tmp_path / "o2"), allow_unverified_key=True, verify=True)
    assert m["verify"]["ok"] and any("could not be verified" in w for w in m["warnings"])


def test_adv_meta_hash_tail_is_compared(tmp_path):
    """REGRESSION: detection compared only the first 20 bytes of the 32-byte FIoChunkHash, so a
    layout with a non-zero tail was reported as plain 'sha1' and the writer then wrote 20 bytes +
    12 zeros, i.e. a different form than every other entry, and selfcheck could not see it."""
    info = build_container(str(tmp_path / "in"), "tail", standard_chunks()[:4], 3, meta_algo="sha1")
    toc = iostore.TocResource.parse(open(info["utoc"], "rb").read())
    toc.metas = [iostore.TocEntryMeta(m.hash[:20] + b"\x01" * 12, m.flags) for m in toc.metas]
    open(info["utoc"], "wb").write(toc.serialize())
    with iostore.IoStoreContainer(info["utoc"]) as c:
        det = c.detect_hash_algorithm()
        assert det["algo"] == "unknown" and not any(e.get("tail12_zero", True) for e in det["evidence"])
        with pytest.raises(iostore.IoStoreError, match="--hash-algo"):
            iostore.patch_container(c, {0: b"abc"}, str(tmp_path / "o"))


@pytest.mark.parametrize("rebuild", [False, True])
def test_adv_memory_mapped_chunk_alignment_and_contiguity(corpus, rebuild, tmp_path):
    """The MemoryMapped chunk (meta flag 0x2) is replaced by 3 blocks: its first block must start on a
    16 KiB boundary of its partition file, all its blocks must be contiguous in ONE partition (UE maps
    it as one range) and the MemoryMapped flag must survive. v3_partitions has 192 KiB partitions."""
    info = corpus["v3_partitions"]
    mm = [i for i, ch in enumerate(info["table"]) if ch.meta_flags & 2][0]
    new = _payload(71, 2 * BS + 999, compressible=False)
    with open_c(info) as c:
        m = iostore.patch_container(c, {mm: new}, str(tmp_path / "o"), rebuild=rebuild, verify=True)
    assert m["verify"]["ok"] and m["mode"]["memory_mapped_alignment"]["used"] >= 16384
    with iostore.IoStoreContainer(str(tmp_path / "o" / "pakchunk_r9.utoc")) as o:
        assert o.toc.metas[mm].flags & 2
        first, last, _ = o.toc.chunk_block_range(mm)
        spans = [o.block_disk_span(bi) for bi in range(first, last + 1)]
        assert len({p for p, _, _ in spans}) == 1
        assert spans[0][1] % 16384 == 0
        for (p0, l0, _), (p1, l1, _) in zip(spans, spans[1:]):
            assert l1 == l0 + BS  # contiguous, no gap between full blocks
        assert o.read_chunk(mm) == new


def test_adv_observed_mmap_alignment_is_honoured(corpus):
    """If the cooker aligned memory-mapped chunks more coarsely (e.g. 64 KiB), new ones must be too."""
    info = corpus["v2_dirindex"]
    toc = iostore.TocResource.parse(open(info["utoc"], "rb").read())
    mm = [i for i, m in enumerate(toc.metas) if m.flags & 2][0]
    first = toc.chunk_block_range(mm)[0]
    for off, want in ((0x30000, 65536), (0x4000, 16384), (0x1010, 16384), (0x200000, 65536)):
        b = toc.blocks[first]
        toc.blocks[first] = iostore.CompressedBlock(off, b.compressed_size, b.uncompressed_size, b.method_index)
        assert iostore.observed_mmap_alignment(toc)[0] == want, hex(off)


def test_adv_shared_block_layout_append(tmp_path):
    """Two chunks sharing one compression block (legal for the reader math; --rebuild must refuse).
    REGRESSION: append-mode --verify FAILED on such inputs whenever the shared block survived into
    the output (any shared block counted as a layout error, although the input already had it), and
    the manifest counted a block that another chunk still uses as orphaned."""
    a = _payload(11, 300)
    chunks = [SChunk(make_chunk_id(pid("A"), 0, 2), a, "Game/Content/A.uasset", "none"),
              SChunk(make_chunk_id(pid("B"), 0, 2), b"-" * 50, "Game/Content/B.uasset", "none"),
              SChunk(make_chunk_id(pid("C"), 0, 2), _payload(12, 5000), "Game/Content/C.uasset", "zlib")]
    info = build_container(str(tmp_path / "in"), "shared", chunks, 3)
    toc = iostore.TocResource.parse(open(info["utoc"], "rb").read())
    toc.offsets[1] = (100, 200)  # B = bytes 100..300 of A's block 0
    toc.metas[1] = iostore.TocEntryMeta(hashlib.sha1(a[100:300]).digest() + b"\0" * 12, 0)
    open(info["utoc"], "wb").write(toc.serialize())
    with iostore.IoStoreContainer(info["utoc"]) as c:
        assert c.read_chunk(1) == a[100:300]
        lay = c.toc.layout_report()
        assert lay["shared_blocks_count"] == 1 and lay["chunks_misaligned_count"] == 1
        assert iostore.selfcheck(c, deep=True)["ok"]
        with pytest.raises(iostore.IoStoreError, match="--rebuild needs"):
            iostore.patch_container(c, {2: b"new C"}, str(tmp_path / "r"), rebuild=True)
        # patch C: A and B keep sharing block 0 in the output
        m = iostore.patch_container(c, {2: b"new C" * 99}, str(tmp_path / "o"), verify=True)
        assert m["verify"]["ok"], m["verify"]["failures"]
        assert m["verify"]["checks"]["layout"]["shared_blocks_count"] == 1
        # patch A: block 0 is still B's, so it is NOT orphaned
        m2 = iostore.patch_container(c, {0: b"new A" * 99}, str(tmp_path / "o2"), verify=True)
        assert m2["verify"]["ok"] and m2["mode"]["orphaned_blocks"] == 0
    with iostore.IoStoreContainer(str(tmp_path / "o2" / "shared.utoc")) as o:
        assert o.read_chunk(0) == b"new A" * 99 and o.read_chunk(1) == a[100:300]
    for d in ("o", "o2"):
        _maybe_cue4parse(str(tmp_path / d / "shared.utoc"), None, 3)


def test_adv_unpadded_input_append_does_not_overlap(tmp_path):
    """An unencrypted input whose last block is not padded to 16 bytes (file ends at CompressedSize).
    Readers fetch Align(CompressedSize, 16) bytes, so appended data must start after that aligned span,
    and selfcheck must point the unpadded span out."""
    chunks = [SChunk(make_chunk_id(pid("p"), 0, 2), _payload(4, 64), "Game/p.uasset", "none"),
              SChunk(make_chunk_id(pid("q"), 0, 2), _payload(5, 1001, compressible=False), "Game/q.uasset", "none")]
    info = build_container(str(tmp_path / "in"), "unpad", chunks, 2)
    ucas = info["utoc"][:-5] + ".ucas"
    with iostore.IoStoreContainer(info["utoc"]) as c:
        last_block = c.toc.blocks[c.toc.chunk_block_range(1)[0]]
    with open(ucas, "r+b") as f:
        f.truncate(last_block.offset + 1001)
    with iostore.IoStoreContainer(info["utoc"]) as c:
        assert c.read_chunk(1) == chunks[1].data
        r = iostore.selfcheck(c)
        assert r["disk_spans_aligned16"]["beyond_partition_eof"] == 1 and r["warnings"]
        m = iostore.patch_container(c, {0: b"z" * 40}, str(tmp_path / "o"), verify=True)
    assert m["verify"]["ok"]
    with iostore.IoStoreContainer(str(tmp_path / "o" / "unpad.utoc")) as o:
        new_first = o.toc.chunk_block_range(0)[0]
        assert o.toc.blocks[new_first].offset >= last_block.offset + iostore.align_up(1001, 16)
        assert iostore.disk_span_report(o)["overlapping_block_pairs"] == 0
        assert iostore.disk_span_report(o)["beyond_partition_eof"] == 0


def test_adv_verify_is_not_vacuous(corpus, tmp_path):
    """--verify must actually catch damage: flip one byte inside an UNCHANGED chunk's block of a
    written output, and swap two AES-CTR IVs of unchanged blocks; both must be reported."""
    info = corpus["v2_dirindex"]
    target = _replacement_target(info)
    repl = {target: b"payload" * 10}
    with open_c(info) as c:
        iostore.patch_container(c, repl, str(tmp_path / "o"))
        out = str(tmp_path / "o" / "pakchunk_r9.utoc")
        with iostore.IoStoreContainer(out) as o:
            victim = [i for i in range(len(info["table"])) if i != target and o.toc.offsets[i][1]][0]
            pidx, local, _ = o.block_disk_span(o.toc.chunk_block_range(victim)[0])
        with open(iostore.partition_path(out, pidx), "r+b") as f:
            f.seek(local)
            byte = f.read(1)
            f.seek(local)
            f.write(bytes([byte[0] ^ 0xFF]))
        v = iostore.verify_patch(c, out, repl, "sha1")
    assert not v["ok"] and any(f"chunk {victim}" in x for x in v["failures"])
    info = corpus["v10_aes_ctr"]
    target = _replacement_target(info)
    repl = {target: b"payload" * 10}
    with open_c(info) as c:
        iostore.patch_container(c, repl, str(tmp_path / "c"))
        out = str(tmp_path / "c" / "pakchunk_r9.utoc")
        toc = iostore.TocResource.parse(open(out, "rb").read())
        toc.encryption_ivs[0], toc.encryption_ivs[1] = toc.encryption_ivs[1], toc.encryption_ivs[0]
        os.chmod(out, 0o644)
        open(out, "wb").write(toc.serialize())
        v = iostore.verify_patch(c, out, repl, "blake3")
    assert not v["ok"] and any("IV" in x or "decode" in x or "differs" in x for x in v["failures"])


def test_adv_output_safety(corpus, tmp_path):
    """Unsafe writes: an --out-dir that is a symlink to the input directory, an --out-dir that is a
    file, a replacement source inside --out-dir; copy_file_new never truncates; input hashes in the
    manifest are taken before writing and re-checked by --verify."""
    info = corpus["v2_dirindex"]
    in_dir = os.path.dirname(info["utoc"])
    link = tmp_path / "link"
    os.symlink(in_dir, link)
    before = files_digest(info["utoc"])
    with open_c(info) as c:
        with pytest.raises(iostore.IoStoreError, match="refusing"):
            iostore.patch_container(c, {0: b"x"}, str(link))
        afile = tmp_path / "afile"
        afile.write_text("x")
        with pytest.raises(iostore.IoStoreError, match="not a directory"):
            iostore.patch_container(c, {0: b"x"}, str(afile))
        od = tmp_path / "od"
        od.mkdir()
        src = od / "src.bin"
        src.write_bytes(b"x")
        with pytest.raises(iostore.IoStoreError, match="refusing|not empty"):
            iostore.patch_container(c, {0: src.read_bytes()}, str(od), extra_inputs=[str(src)])
        assert sorted(os.listdir(od)) == ["src.bin"]
        m = iostore.patch_container(c, {0: b"x"}, str(tmp_path / "ok"), verify=True)
    assert files_digest(info["utoc"]) == before
    assert m["verify"]["checks"]["inputs_unchanged"] is True
    assert {os.path.basename(r["path"]): r["sha256"] for r in m["inputs"]} == before
    dst = tmp_path / "exists.bin"
    dst.write_bytes(b"keep me")
    with pytest.raises(FileExistsError):
        iostore.copy_file_new(info["utoc"], str(dst))
    assert dst.read_bytes() == b"keep me"


def test_adv_cli_errors_are_clean(corpus, tmp_path, aes_key):
    """Bad selector, missing input, unknown key encodings: exit code 2 and no traceback; a key file
    with a UTF-8 BOM (Windows Notepad) is accepted."""
    info = corpus["v2_dirindex"]
    f = tmp_path / "f.bin"
    f.write_bytes(b"1")
    r = run_cli("patch", info["utoc"], "--out-dir", str(tmp_path / "o"), "--replace", "index:zz", str(f), check=False)
    assert r.returncode == 2 and "Traceback" not in r.stderr and "index" in r.stderr
    r = run_cli("info", str(tmp_path / "missing.utoc"), check=False)
    assert r.returncode == 2 and "Traceback" not in r.stderr
    bom = tmp_path / "bom.key"
    bom.write_bytes(b"\xef\xbb\xbf" + aes_key.hex().encode() + b"\r\n")
    r = run_cli("info", corpus["v3_aes"]["utoc"], "--aes-key-file", str(bom), "--no-detect")
    assert json.loads(r.stdout)["key"] == "accepted"
    assert aes_key.hex() not in r.stdout + r.stderr


def test_adv_reserved_fields_reported(corpus, tmp_path):
    """Version-gated header fields that are still 'reserved' in the file's TOC version (UE4.26 v3:
    PerfectHashSeedsCount/ChunksWithoutPerfectHashCount are Reserved5/Reserved6) must not be
    interpreted, must round-trip, and must be flagged in reserved_nonzero."""
    info = corpus["v3_partitions"]
    raw = bytearray(open(info["utoc"], "rb").read())
    struct.pack_into("<I", raw, 84, 7)   # TocChunkPerfectHashSeedsCount in a v3 file
    struct.pack_into("<I", raw, 96, 3)   # TocChunksWithoutPerfectHashCount in a v3 file
    toc = iostore.TocResource.parse(bytes(raw))
    assert toc.perfect_hash_seeds == [] and toc.chunks_without_perfect_hash == []
    assert toc.serialize() == bytes(raw)
    assert toc.header.to_json()["reserved_nonzero"] is True
    assert iostore.TocResource.parse(open(info["utoc"], "rb").read()).header.to_json()["reserved_nonzero"] is False


def test_adv_aes_backends_agree(monkeypatch):
    """The `cryptography` fallback must produce the same ECB and Unreal-CTR bytes as pycryptodome."""
    pytest.importorskip("cryptography")
    key, iv = os.urandom(32), os.urandom(12)
    data = os.urandom(16 * 1000)
    a = iostore.AesCipher(key)
    assert "pycryptodome" in repr(a) and key.hex() not in repr(a)
    monkeypatch.setitem(sys.modules, "Crypto", None)
    monkeypatch.setitem(sys.modules, "Crypto.Cipher", None)
    b = iostore.AesCipher(key)
    assert "cryptography" in repr(b)
    assert a.ecb_encrypt(data) == b.ecb_encrypt(data) and b.ecb_decrypt(a.ecb_encrypt(data)) == data
    assert b.ctr_crypt(data[:1000 * 16 - 5], iv) == AES.new(key, AES.MODE_CTR, nonce=iv, initial_value=0).encrypt(
        data[:1000 * 16 - 5])


def test_adv_blake3_pure_tree_shapes():
    """Pure-Python BLAKE3: power-of-two chunk counts and multi-level trees (8, 16, 64 chunks +-1)."""
    for n in (4096, 8192, 16 * 1024 - 1, 16 * 1024 + 1, 64 * 1024, 64 * 1024 + 1000):
        d = _payload(n, n, compressible=False)
        assert iostore.blake3_pure(d, 20) == blake3.blake3(d).digest(length=20), n


def test_adv_iohash_zero_convention_kept(tmp_path):
    """A v8+ container that stores all-zero FIoHash values keeps that convention for replaced chunks
    (previously a BLAKE3 hash was written next to all-zero ones)."""
    info = build_container(str(tmp_path / "in"), "z8", standard_chunks()[:5], 8, meta_algo="zero", perfect_hash=True)
    with open_c(info) as c:
        m = iostore.patch_container(c, {1: b"q" * 3000}, str(tmp_path / "o"), verify=True)
    assert m["meta_hash_algorithm"] == "zero" and m["verify"]["ok"]
    assert m["replaced_chunks"][0]["new_meta_hash"] == "00" * 20


def test_adv_corrupt_header_values_fail_cleanly(corpus):
    """CompressionBlockSize 0 and an unknown v10 EncryptionMethod raise IoStoreError (exit code 2),
    not ZeroDivisionError / ValueError tracebacks."""
    raw = bytearray(open(corpus["v3_aes"]["utoc"], "rb").read())
    struct.pack_into("<I", raw, 44, 0)
    with pytest.raises(iostore.IoStoreError, match="CompressionBlockSize"):
        iostore.TocResource.parse(bytes(raw))
    raw = bytearray(open(corpus["v10_aes_ctr"]["utoc"], "rb").read())
    raw[81] = 7
    with pytest.raises(iostore.IoStoreError, match="EncryptionMethod"):
        iostore.TocResource.parse(bytes(raw))
