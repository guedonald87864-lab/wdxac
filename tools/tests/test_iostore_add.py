"""Tests for adding NEW chunks / paths / package store entries and for clone-package
(tools/native/iostore.py `add`, `clone-package`, `container-header`, `package-id`).

Independence of the checks
--------------------------
* Containers come from test_iostore.build_container (written from the CUE4Parse layouts, no
  iostore.py serialisation code). Container headers come from `build_container_header` below,
  written from CUE4Parse FIoContainerHeader.cs / FFilePackageStoreEntry.cs and retoc
  container_header.rs, not from iostore.ContainerHeaderUE4.
* Zen packages come from zen_package.build_legacy_package (the fixture builder, not the renamer).
* Every output is re-read by our reader (verify) AND, when the harness is built, by CUE4Parse:
  its IoStoreReader (chunk ids, TryResolve, directory index), its FIoContainerHeader with the
  game-specific FPackageStoreEntry layout (GAME_eFootball = 16 bytes, GAME_UE4_26 = 32 bytes) and
  its IoPackage loader through a StreamedFileProvider (load by FPackageId and by path).
* Real data where it exists locally: package ids written by Unreal Engine 5.8 itself (CUE4Parse
  test fixtures), Google's CityHash test vectors (city-test.cc), and two real container headers
  shipped as retoc test data: issue #18 (PesMobile = eFootball Android, 16-byte entries) and a
  UE4.27 game (32-byte entries). Those tests skip when the reference checkouts are absent.
"""

import hashlib
import json
import os
import random
import re
import struct
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
NATIVE = os.path.join(os.path.dirname(HERE), "native")
sys.path.insert(0, NATIVE)
sys.path.insert(0, HERE)
import iostore  # noqa: E402
import zen_package as zp  # noqa: E402
from test_iostore import (BS, CUE4PARSE_CHECK, DOTNET_ROOT, SChunk, _payload, build_container,  # noqa: E402
                          fnv1_seed, make_chunk_id)

IOSTORE = os.path.join(NATIVE, "iostore.py")
CITY_TEST = os.environ.get("CITYHASH_TEST_CC", "/opt/tools/ref/cityhash/src/city-test.cc")
UE58_FIXTURES = os.environ.get("CUE4PARSE_FIXTURES", "/opt/tools/ref/CUE4Parse/CUE4Parse.Tests/Fixtures/UE5_8/IoStore")
RETOC_TESTS = os.environ.get("RETOC_TESTS", "/opt/tools/ref/retoc/retoc/tests")
REAL_EFOOTBALL_HEADER = os.path.join(RETOC_TESTS, "issues", "issue18", "header.bin")   # PesMobile (eFootball Android)
REAL_UE427_HEADER = os.path.join(RETOC_TESTS, "UE4.27", "ContainerHeader_1.bin")
EFOOTBALL_ISSUE18_CONTAINER_ID = 0x8FA7AE270B85F529  # = bytes 29f5850b27aea78f quoted in retoc issue #18

GAME_FOR_LAYOUT = {"efootball-16": "GAME_eFootball", "ue4-32": "GAME_UE4_26"}


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# =============================================================================================
# CityHash64 / FPackageId
# =============================================================================================

def city_test_data(n: int) -> bytes:
    """city-test.cc setup(): the pseudo-random buffer its test vectors are computed over."""
    k0 = 0xC3A5C85C97CB3127
    m = (1 << 64) - 1
    a, b = 9, 777
    out = bytearray(n)
    for i in range(n):
        a = (a + b) & m
        b = (b + a) & m
        a = ((a ^ (a >> 41)) * k0) & m
        b = (((b ^ (b >> 41)) * k0) + i) & m
        out[i] = (b >> 37) & 0xFF
    return bytes(out)


# testdata[i][0] of google/cityhash city-test.cc (CityHash64(data + i*i, i)); copied from the file
CITY_TEST_ROWS = {0: 0x9AE16A3B2F90404F, 1: 0x541150E87F415E96, 2: 0x0F3786A4B25827C1, 3: 0xEF923A7A1AF78EAB,
                  4: 0x11DF592596F41D88, 5: 0x831F448BDC5600B3, 8: 0xA0F10149A0E538D6, 13: 0x851FFF285561DCA0,
                  16: 0x03EAD5F21D344056, 17: 0x6ABBFDE37EE03B5B, 31: 0x55BDB0E71E3EDEBD, 32: 0x0782FA1B08B475E7,
                  33: 0xC5DC19B876D37A80, 64: 0xE88419922B87176F, 65: 0x105191E0EC8F7F60, 100: 0x6369163565814DE6,
                  200: 0x07FC98006E25CAC9, 298: 0x74C0B8A6821FAAFE}


def test_cityhash64_google_vectors_embedded():
    data = city_test_data(298 * 298 + 298)
    for i, want in CITY_TEST_ROWS.items():
        assert iostore.cityhash64(data[i * i:i * i + i]) == want, i


@pytest.mark.skipif(not os.path.exists(CITY_TEST), reason="google/cityhash checkout not found")
def test_cityhash64_all_google_vectors():
    """All 300 CityHash64 rows of city-test.cc, including the 1 MiB one."""
    src = open(CITY_TEST).read()
    start = src.index("static const uint64 testdata[kTestSize][16] = {")
    rows = re.findall(r"\{(C\([0-9a-f]+\)(?:,\s*C\([0-9a-f]+\))*)\}", src[start:src.index("};", start)])
    expected = [int(re.findall(r"C\(([0-9a-f]+)\)", r)[0], 16) for r in rows]
    assert len(expected) == 300
    data = city_test_data(1 << 20)
    for i in range(299):
        assert iostore.cityhash64(data[i * i:i * i + i]) == expected[i], i
    assert iostore.cityhash64(data) == expected[299]


@pytest.mark.parametrize("mode", ["Unversioned", "Tagged"])
def test_package_ids_match_ids_written_by_unreal(mode):
    """The 97 package chunks of CUE4Parse's UE 5.8 fixture containers (cooked and packaged by
    Unreal Engine itself): chunk id = FPackageId::FromName('/Game/' + path below Content)."""
    utoc = os.path.join(UE58_FIXTURES, mode, "Uncompressed", "CUE4ParseFixtures-Minimal-Uncompressed-Windows.utoc")
    if not os.path.exists(utoc):
        pytest.skip("CUE4Parse UE5.8 fixtures not found")
    n = 0
    with iostore.IoStoreContainer(utoc) as c:
        for p, i in c.paths():
            cid = c.toc.chunk_ids[i]
            m = re.match(r"CUE4ParseFixtures/Content/(.*)\.(uasset|umap)$", iostore.strip_mount(p))
            if cid.type != 1 or not m:
                continue
            assert iostore.package_id_from_name("/Game/" + m.group(1)) == cid.id, p
            # case-insensitive: FromName lower-cases first
            assert iostore.package_id_from_name("/GAME/" + m.group(1).upper()) == cid.id
            n += 1
    assert n == 97


# =============================================================================================
# Independent container-header builder (CUE4Parse FIoContainerHeader / FFilePackageStoreEntry,
# retoc container_header.rs). NOT iostore.ContainerHeaderUE4.
# =============================================================================================

def build_container_header(container_id: int, packages: List[Tuple[int, dict]], layout: str,
                           culture_map: Optional[Dict[str, List[Tuple[int, int]]]] = None,
                           redirects: List[Tuple[int, int]] = (), trailing: bytes = b"") -> bytes:
    """packages: [(FPackageId, {"export_count", "export_bundle_count", "imported", "ebs", "load_order"})]"""
    entry_size, member = {"efootball-16": (16, 8), "ue4-32": (32, 24)}[layout]
    n = len(packages)
    records, arrays = b"", b""
    for i, (pid, e) in enumerate(packages):
        imported = e.get("imported", [])
        if imported:
            view = struct.pack("<II", len(imported), n * entry_size + len(arrays) - (i * entry_size + member))
            arrays += b"".join(struct.pack("<Q", x) for x in imported)
        else:
            view = struct.pack("<II", 0, 0)
        if layout == "ue4-32":
            records += struct.pack("<QiiII", e["ebs"], e["export_count"], e["export_bundle_count"],
                                   e["load_order"], 0) + view
        else:
            records += struct.pack("<ii", e["export_count"], e["export_bundle_count"]) + view
    store = records + arrays
    out = struct.pack("<QI", container_id, n)
    out += struct.pack("<i", 0)                                      # Names (empty in UE4)
    out += struct.pack("<i", 8) + struct.pack("<Q", 0xC1640000)       # NameHashes: algorithm id only
    out += struct.pack("<i", n) + b"".join(struct.pack("<Q", p) for p, _ in packages)
    out += struct.pack("<i", len(store)) + store
    culture_map = culture_map or {}
    out += struct.pack("<i", len(culture_map))
    for k, pairs in culture_map.items():
        kb = k.encode("ascii") + b"\0"
        out += struct.pack("<i", len(kb)) + kb + struct.pack("<i", len(pairs))
        out += b"".join(struct.pack("<QQ", a, b) for a, b in pairs)
    out += struct.pack("<i", len(redirects)) + b"".join(struct.pack("<QQ", a, b) for a, b in redirects)
    return out + trailing


def decode_store_independently(raw: bytes, layout: str) -> Dict[int, dict]:
    """Second, independent decoder (CUE4Parse ReadCArrayView semantics) for test assertions."""
    entry_size, member = {"efootball-16": (16, 8), "ue4-32": (32, 24)}[layout]
    p = 12
    n, = struct.unpack_from("<i", raw, p); p += 4 + n
    n, = struct.unpack_from("<i", raw, p); p += 4 + n
    n, = struct.unpack_from("<i", raw, p); p += 4
    ids = struct.unpack_from(f"<{n}Q", raw, p); p += 8 * n
    size, = struct.unpack_from("<i", raw, p); p += 4
    st = raw[p:p + size]
    out = {}
    for i, pid in enumerate(ids):
        b = i * entry_size
        if layout == "ue4-32":
            ebs, ec, ebc, lo, pad = struct.unpack_from("<QiiII", st, b)
            e = {"ebs": ebs, "load_order": lo}
        else:
            ec, ebc = struct.unpack_from("<ii", st, b)
            e = {}
        cnt, off = struct.unpack_from("<ii", st, b + member)
        e.update(export_count=ec, export_bundle_count=ebc,
                 imported=list(struct.unpack_from(f"<{cnt}Q", st, b + member + off)) if cnt else [])
        out[pid] = e
    return out


# =============================================================================================
# Synthetic game content: a shared donor face used by two players (task brief section 8)
# =============================================================================================

SKM = zp.script_import_index("/Script/Engine.SkeletalMesh")
MIC = zp.script_import_index("/Script/Engine.MaterialInstanceConstant")
TEX = zp.script_import_index("/Script/Engine.Texture2D")
OBJ = zp.script_import_index("/Script/CoreUObject.Object")

FACE = "/Game/Face/pc3600/face_110718"
MI = "/Game/Face/pc3600/MI_face_110718"
TEXP = "/Game/Face/pc3611/T_face_110718"
P1, P2, OTHER = "/Game/Player/p_000001", "/Game/Player/p_000002", "/Game/Misc/other"
ROOT = "PesMobile/Content"


def gidx(path: str) -> int:
    return zp.legacy_package_import_index(path)


def short(pkg: str) -> str:
    return pkg.rsplit("/", 1)[1]


def pkg_path(pkg: str, ext: str = ".uasset") -> str:
    return f"{ROOT}/{pkg.split('/', 2)[2]}{ext}"


def make_package(name: str, cls: int, imports_from: List[str], sub_exports: int = 0, seed: int = 0,
                 extra_names=()) -> Tuple[bytes, dict]:
    """A UE4.26-style Zen package: main public export (GlobalImportIndex = hash of its object
    path, as UE4.26/retoc write it), optional public sub-objects, one private export; imports of
    the main export of each package in `imports_from`; graph data lists those packages."""
    obj = short(name)
    exps = [zp.ExportSpec(obj, _payload(seed, 300 + 50 * seed), cls, global_import_index=gidx(f"{name}.{obj}"),
                          object_flags=0x3)]
    for k in range(sub_exports):
        exps.append(zp.ExportSpec(f"{obj}_Sub{k}", _payload(seed + 10 + k, 120), OBJ,
                                  outer_index=zp.make_obj_index("Export", 0),
                                  global_import_index=gidx(f"{name}.{obj}:{obj}_Sub{k}"), object_flags=0x1))
    exps.append(zp.ExportSpec(f"{obj}_Private", _payload(seed + 50, 64), OBJ, object_flags=0))
    imports = [cls, OBJ] + [gidx(f"{p}.{short(p)}") for p in imports_from]
    graph = [(zp.package_id_from_name(p), [(0, 0)]) for p in imports_from]
    data = zp.build_legacy_package(name, exps, imports=imports, imported_packages=graph, extra_names=list(extra_names))
    entry = {"export_count": len(exps), "export_bundle_count": 1,
             "imported": [zp.package_id_from_name(p) for p in imports_from]}
    return data, entry


def game_content() -> List[dict]:
    """[{name, data, entry, bulk}] - face -> MI -> texture; two players share the donor face."""
    pk = []
    for name, cls, imps, subs, seed, bulk, extra in [
        (TEXP, TEX, [], 0, 1, _payload(71, 70_000, compressible=False), ()),
        (MI, MIC, [TEXP], 0, 2, None, ()),
        (FACE, SKM, [MI], 2, 3, _payload(72, 3 * BS + 77), (f"{FACE}.face_110718",)),
        (P1, OBJ, [FACE], 0, 4, None, ()),
        (P2, OBJ, [FACE], 0, 5, None, ()),
        (OTHER, OBJ, [], 0, 6, _payload(73, 5000), ()),
    ]:
        data, entry = make_package(name, cls, imps, subs, seed, extra)
        pk.append({"name": name, "data": data, "entry": entry, "bulk": bulk})
    return pk


def build_game_container(out_dir: str, layout: str, *, version: int = 2, key: Optional[bytes] = None,
                         partition_size: Optional[int] = None, ebs_rule: str = "chunk") -> dict:
    content = game_content()
    container_id = 0x0123456789ABCDEF ^ (version << 8) ^ (len(layout) << 16)
    chunks: List[SChunk] = []
    header_pkgs = []
    for k, p in enumerate(content):
        pid = zp.package_id_from_name(p["name"])
        chunks.append(SChunk(make_chunk_id(pid, 0, 2), p["data"], pkg_path(p["name"]), "zlib"))
        if p["bulk"] is not None:
            chunks.append(SChunk(make_chunk_id(pid, 0, 3), p["bulk"], pkg_path(p["name"], ".ubulk"), "alt"))
        e = dict(p["entry"])
        e["ebs"] = len(p["data"]) if ebs_rule == "chunk" else 1000 + 7 * k
        e["load_order"] = k
        header_pkgs.append((pid, e))
    if layout == "ue4-32":   # the UE4.27 sample keeps PackageIds ascending
        header_pkgs.sort(key=lambda t: t[0])
    header = build_container_header(container_id, header_pkgs, layout)
    chunks.append(SChunk(make_chunk_id(container_id, 0, 10), header, None, "zlib"))
    info = build_container(out_dir, "pc3600_mobile_ios", chunks, version, key=key, partition_size=partition_size,
                           meta_algo="sha1", container_id=container_id)
    info.update(content=content, header_pkgs=dict(header_pkgs), container_id=container_id, layout=layout)
    return info


def build_global(out_dir: str) -> str:
    """Minimal UE4 global.utoc for CUE4Parse's IoGlobalData: no global names, no script objects."""
    chunks = [SChunk(make_chunk_id(0, 0, 9), struct.pack("<Q", 0xC1640000), None, "none"),   # LoaderGlobalNameHashes
              SChunk(make_chunk_id(0, 0, 8), b"", None, "none"),                            # LoaderGlobalNames
              SChunk(make_chunk_id(0, 0, 7), struct.pack("<i", 0), None, "none")]           # LoaderInitialLoadMeta
    return build_container(out_dir, "global", chunks, 2, meta_algo="sha1")["utoc"]


# =============================================================================================
# CUE4Parse helpers
# =============================================================================================

def _cue4parse_available():
    return os.path.exists(CUE4PARSE_CHECK) and os.path.exists(os.path.join(DOTNET_ROOT, "dotnet"))


cue4parse = pytest.mark.skipif(not _cue4parse_available(), reason="CUE4Parse harness / .NET runtime not built")


def run_cue4parse(args: List[str], key: Optional[bytes] = None, expect_ok: bool = True) -> dict:
    env = dict(os.environ, DOTNET_ROOT=DOTNET_ROOT)
    env.pop("R9_AES_KEY_HEX", None)
    if key is not None:
        env["R9_AES_KEY_HEX"] = key.hex()
    r = subprocess.run([CUE4PARSE_CHECK, *args], capture_output=True, text=True, env=env, timeout=900)
    assert r.returncode in (0, 1), r.stderr
    j = json.loads(r.stdout)
    if key is not None:
        assert key.hex() not in r.stdout and key.hex().upper() not in r.stdout
    if expect_ok:
        assert j.get("errors", []) == [], j.get("errors")
    return j


def cue4parse_full(utoc: str, game: str, key: Optional[bytes] = None, global_utoc: Optional[str] = None,
                   expect_ok: bool = True) -> dict:
    args = [utoc, "--game", game, "--container-header"]
    if global_utoc:
        args += ["--global", global_utoc]
    return run_cue4parse(args, key, expect_ok)


def assert_misread(bad_header: dict, truth: Dict[int, dict]):
    """CUE4Parse with the wrong FPackageStoreEntry size must not reproduce the true entries."""
    if not bad_header.get("parsed"):
        return
    wrong = 0
    for p in bad_header["packages"]:
        t = truth.get(int(p["package_id"], 16))
        if t is None or [int(x, 16) for x in p["imported_packages"]] != t["imported"] or \
                p["export_count"] != t["export_count"] or p["export_bundle_count"] != t["export_bundle_count"]:
            wrong += 1
    assert wrong > 0, "the wrong layout reproduced every entry - the layouts are not being told apart"


def assert_cue4parse_chunks_agree(j: dict, utoc: str, key: Optional[bytes]):
    """Every chunk CUE4Parse sees equals what our reader sees; every chunk id resolves."""
    with iostore.IoStoreContainer(utoc, key) as c:
        assert len(j["chunks"]) == len(c.toc.chunk_ids)
        paths = c.path_by_index()
        for e in j["chunks"]:
            i = e["index"]
            assert e["chunk_id"] == c.toc.chunk_ids[i].hex
            assert e["resolve_ok"], e
            assert e["sha256"] == sha(c.read_chunk(i))
            if i in paths:
                assert e.get("path") == iostore.strip_mount(paths[i])


# =============================================================================================
# Real container headers (retoc test data)
# =============================================================================================

def _real(path):
    if not os.path.exists(path):
        pytest.skip(f"{path} not found (retoc checkout)")
    return open(path, "rb").read()


def test_real_efootball_header_is_16_byte_layout():
    raw = _real(REAL_EFOOTBALL_HEADER)
    ch = iostore.ContainerHeaderUE4.parse(raw)
    assert ch.serialize() == raw
    assert ch.container_id == EFOOTBALL_ISSUE18_CONTAINER_ID
    det = ch.detect_layouts()
    assert det["efootball-16"]["ok"] and det["efootball-16"]["tightly_packed"]
    assert not det["ue4-32"]["ok"]
    assert ch.resolve_layout("auto")[0] == "efootball-16"
    with pytest.raises(iostore.IoStoreError):
        ch.resolve_layout("ue4-32")
    # independent decoder agrees with ours
    ind = decode_store_independently(raw, "efootball-16")
    ours = dict(zip(ch.package_ids, ch.entries("efootball-16")))
    assert len(ours) == 182
    for pid, e in ind.items():
        assert (ours[pid].export_count, ours[pid].export_bundle_count, ours[pid].imported_packages) == \
               (e["export_count"], e["export_bundle_count"], e["imported"])
    # UE's own packing is what our canonical encoder produces
    assert iostore.ContainerHeaderUE4.encode_store("efootball-16", list(ours.items())) == ch.store_raw
    tail = ch.tail_info()
    assert tail["package_redirects"] == 0 and tail["trailing_all_zero"]


def test_real_ue427_header_is_32_byte_layout():
    raw = _real(REAL_UE427_HEADER)
    ch = iostore.ContainerHeaderUE4.parse(raw)
    assert ch.serialize() == raw
    det = ch.detect_layouts()
    assert det["ue4-32"]["ok"] and det["ue4-32"]["tightly_packed"] and not det["efootball-16"]["ok"]
    ents = ch.entries("ue4-32")
    assert all(e.pad == 0 and e.export_bundles_size > 0 for e in ents)
    assert iostore.ContainerHeaderUE4.encode_store("ue4-32", list(zip(ch.package_ids, ents))) == ch.store_raw
    ind = decode_store_independently(raw, "ue4-32")
    for pid, e in list(zip(ch.package_ids, ents))[:2000]:
        assert ind[pid]["imported"] == e.imported_packages and ind[pid]["ebs"] == e.export_bundles_size


@pytest.mark.parametrize("which,layout,order", [("efootball", "efootball-16", "append"),
                                                ("ue427", "ue4-32", "sorted")])
def test_real_headers_add_packages(which, layout, order):
    raw = _real(REAL_EFOOTBALL_HEADER if which == "efootball" else REAL_UE427_HEADER)
    ch = iostore.ContainerHeaderUE4.parse(raw)
    old = dict(zip(ch.package_ids, ch.entries(layout)))
    new_items = []
    for k in range(3):
        pid = zp.package_id_from_name(f"/Game/R9/Clone_{k}")
        e = iostore.StoreEntry(2 + k, 1, [ch.package_ids[k], ch.package_ids[-1]] if k else [])
        if layout == "ue4-32":
            e.export_bundles_size, e.load_order, e.pad = 4096 + k, 7, 0
        new_items.append((pid, e))
    ch2 = ch.copy()
    rec = ch2.add_packages(layout, new_items)
    assert rec["order"] == order   # auto: keep ascending ids ascending, else append
    out = ch2.serialize()
    back = iostore.ContainerHeaderUE4.parse(out)
    assert back.check_layout(layout)["ok"] and back.detect_layouts()[
        "ue4-32" if layout == "efootball-16" else "efootball-16"]["ok"] is False
    got = dict(zip(back.package_ids, back.entries(layout)))
    assert back.package_count == len(back.package_ids) == len(old) + 3
    for pid, e in old.items():
        assert got[pid] == e
    for pid, e in new_items:
        assert got[pid] == e
    ind = decode_store_independently(out, layout)
    for pid, e in new_items:
        assert ind[pid]["imported"] == e.imported_packages
    assert back.tail == ch.tail
    if order == "append":
        assert back.package_ids[:len(old)] == ch.package_ids
        # old records: only the ImportedPackages offset moved, by 3 x 16 bytes
        for i in range(len(old)):
            a, b = ch.store_raw[i * 16:i * 16 + 16], back.store_raw[i * 16:i * 16 + 16]
            assert a[:12] == b[:12]
            cnt, off = struct.unpack_from("<II", a, 8)
            assert struct.unpack_from("<II", b, 8) == ((cnt, off + 48) if cnt else (0, 0))
    else:
        assert back.package_ids == sorted(back.package_ids)


def test_store_order_sorted_needs_canonical_buffer():
    """A buffer whose arrays are not in record order cannot be re-sorted (append still works)."""
    ids = [10, 20, 30]
    pkgs = [(10, {"export_count": 1, "export_bundle_count": 1, "imported": [20]}),
            (20, {"export_count": 1, "export_bundle_count": 1, "imported": [30]}),
            (30, {"export_count": 1, "export_bundle_count": 1, "imported": []})]
    raw = build_container_header(5, pkgs, "efootball-16")
    ch = iostore.ContainerHeaderUE4.parse(raw)
    # swap the two arrays and fix the offsets -> valid but not canonical
    st = bytearray(ch.store_raw)            # records 0..47, arrays [20] at 48 and [30] at 56
    st[48:56], st[56:64] = struct.pack("<Q", 30), struct.pack("<Q", 20)
    struct.pack_into("<II", st, 8, 1, 56 - 8)     # entry 0 view (at 8) -> 56
    struct.pack_into("<II", st, 24, 1, 48 - 24)   # entry 1 view (at 24) -> 48
    ch.store_raw = bytes(st)
    assert ch.check_layout("efootball-16")["ok"]
    assert [e.imported_packages for e in ch.entries("efootball-16")] == [[20], [30], []]
    item = [(40, iostore.StoreEntry(1, 1, [10]))]
    for order in ("sorted", "auto"):   # ids are ascending, so auto means sorted
        with pytest.raises(iostore.IoStoreError, match="canonical"):
            ch.copy().add_packages("efootball-16", item, order)
    c3 = ch.copy()
    assert c3.add_packages("efootball-16", item, "append")["order"] == "append"
    assert c3.package_ids == ids + [40]
    assert [e.imported_packages for e in c3.entries("efootball-16")] == [[20], [30], [], [10]]


def test_ue5_container_header_is_refused():
    raw = struct.pack("<II", iostore.CONTAINER_HEADER_UE5_MAGIC, 5) + b"\0" * 64
    with pytest.raises(iostore.IoStoreError, match="UE5 container header"):
        iostore.ContainerHeaderUE4.parse(raw)


def test_empty_header_layout_is_ambiguous():
    ch = iostore.ContainerHeaderUE4.parse(build_container_header(1, [], "efootball-16"))
    with pytest.raises(iostore.IoStoreError, match="cannot determine"):
        ch.resolve_layout("auto")
    assert ch.resolve_layout("efootball-16")[0] == "efootball-16"


# =============================================================================================
# Unit tests: directory index edit, perfect hash, package rename
# =============================================================================================

def test_dir_index_add_file():
    from test_iostore import build_directory_index
    files = [("PesMobile/Content/Face/pc3600/a.uasset", 0), ("PesMobile/Content/Face/pc3600/b.uasset", 1),
             ("PesMobile/Content/Tex/c.uasset", 2)]
    raw = build_directory_index("../../../", files)
    d = iostore._dir_index_copy(iostore.DirectoryIndex.parse(raw))
    before = d.walk()
    nstr = len(d.strings)
    r1 = iostore.dir_index_add_file(d, "PesMobile/Content/Face/pc3600/z_r9.uasset", 3)
    assert r1["created_directories"] == [] and r1["new_strings"] == ["z_r9.uasset"]
    r2 = iostore.dir_index_add_file(d, "PesMobile/content/face/R9/new.ubulk", 4)  # case-insensitive dir match
    assert r2["created_directories"] == ["R9"]
    with pytest.raises(iostore.IoStoreError, match="already exists"):
        iostore.dir_index_add_file(d, "PesMobile/Content/Face/pc3600/A.UASSET", 5)
    d2 = iostore.DirectoryIndex.parse(d.serialize())
    after = d2.walk()
    assert set(before) <= set(after) and len(after) == len(before) + 2
    got = dict(after)
    assert got["PesMobile/Content/Face/pc3600/z_r9.uasset"] == 3
    assert got["PesMobile/Content/Face/R9/new.ubulk"] == 4
    assert len(d.strings) == nstr + 3
    # file appended at the END of its directory list: old order kept
    order = [p for p, _ in after if p.startswith("PesMobile/Content/Face/pc3600/")]
    old_order = [p for p, _ in before if p.startswith("PesMobile/Content/Face/pc3600/")]
    assert order == old_order + ["PesMobile/Content/Face/pc3600/z_r9.uasset"]
    for bad in ("../x", "a//b", "a/./b"):
        with pytest.raises(iostore.IoStoreError):
            iostore.normalize_new_path(bad, "../../../")
    assert iostore.normalize_new_path("../../../X/y.uasset", "../../../") == "X/y.uasset"


@pytest.mark.parametrize("n", [1, 2, 3, 17, 200])
def test_build_perfect_hash_resolves(n):
    rnd = random.Random(n)
    ids = [iostore.ChunkId(make_chunk_id(rnd.getrandbits(64), rnd.choice([0, 0, 1]), rnd.choice([2, 3, 10])))
           for _ in range(n)]
    for allow_overflow, max_seed in ((False, 1 << 16), (True, 0), (True, 3)):
        try:
            slot_of, seeds, overflow = iostore.build_perfect_hash(ids, allow_overflow, max_seed)
        except iostore.IoStoreError:
            assert not allow_overflow
            continue
        table = [None] * n
        for i, s in enumerate(slot_of):
            table[s] = ids[i]
        # resolve exactly like IoStoreReader.TryResolve (independent re-implementation here)
        for cid in ids:
            seed = seeds[fnv1_seed(cid.raw, 0) % len(seeds)]
            assert seed != 0
            if seed < 0 and -seed - 1 >= n:
                slot = next(s for s in overflow if table[s] == cid)
            else:
                slot = -seed - 1 if seed < 0 else fnv1_seed(cid.raw, seed) % n
            assert table[slot] == cid
        if max_seed == 0 and n > 2:
            assert overflow, "max_seed=0 must push every multi-entry bucket to the overflow list"


def test_rename_legacy_package_fields():
    data, _ = make_package(FACE, SKM, [MI], sub_exports=2, seed=3, extra_names=(f"{FACE}.face_110718",))
    old = zp.parse_legacy(data)
    new_name = "/Game/Face/pc3600/face_r9_2002_world_cup_final_ronaldo"   # longer: sections shift
    mi_map = {gidx(f"{MI}.MI_face_110718"): gidx("/Game/Face/r9/MI_r9.MI_r9")}
    pid_map = {zp.package_id_from_name(MI): zp.package_id_from_name("/Game/Face/r9/MI_r9")}
    out, rep = zp.rename_legacy_package(data, new_name, old_name=FACE, import_index_map=mi_map,
                                        package_id_map=pid_map, name_renames={"face_110718": "face_r9"})
    new = zp.parse_legacy(out)
    assert new.package_name == new_name and new.source_name == new_name
    assert zp.package_id_from_name(new.package_name) == int(rep["new_package_id"], 16)
    # documented field list
    assert set(rep["summary_changes"]) <= {"NameMapNamesSize", "NameMapHashesOffset", "NameMapHashesSize",
                                           "ImportMapOffset", "ExportMapOffset", "ExportBundlesOffset",
                                           "GraphDataOffset"}
    assert rep["summary_changes"]["ImportMapOffset"]["new"] > rep["summary_changes"]["ImportMapOffset"]["old"]
    for k in ("PackageFlags", "CookedHeaderSize", "GraphDataSize", "Pad", "NameMapNamesOffset"):
        assert new.summary[k] == old.summary[k]
    # export data byte-identical, export map identical except GlobalImportIndex (and object name text)
    for a, b in zip(old.exports, new.exports):
        assert data[a.abs_start:a.abs_end] == out[b.abs_start:b.abs_end]
        assert (a.cooked_serial_offset, a.cooked_serial_size, a.class_index, a.outer_index, a.object_flags) == \
               (b.cooked_serial_offset, b.cooked_serial_size, b.class_index, b.outer_index, b.object_flags)
    paths = zp.legacy_export_object_paths(new, new_name)
    assert paths[0] == f"{new_name}.face_r9" and paths[1] == f"{new_name}.face_r9:face_110718_Sub0"
    for e, p in zip(new.exports, paths):
        if e.object_flags & 1:
            assert e.global_import_index_or_hash == gidx(p)
        else:
            assert e.global_import_index_or_hash == zp.NULL_OBJECT_INDEX
    assert gidx("/Game/Face/r9/MI_r9.MI_r9") in new.imports and gidx(f"{MI}.MI_face_110718") not in new.imports
    assert [p["package_id"] for p in new.imported_packages] == [zp.package_id_from_name("/Game/Face/r9/MI_r9")]
    assert rep["names_mentioning_old_package"] == [f"{FACE}.face_110718"] and rep["warnings"]
    # names other than the renamed ones are identical
    changed = {x["index"] for x in rep["name_map_changes"]}
    assert [n for i, n in enumerate(new.names) if i not in changed] == \
           [n for i, n in enumerate(old.names) if i not in changed]


def test_rename_handles_utf16_names_and_refusals():
    exps = [zp.ExportSpec("Équipe", b"x" * 50, OBJ, global_import_index=gidx("/Game/T/pkg.Équipe"), object_flags=1)]
    data = zp.build_legacy_package("/Game/T/pkg", exps, imports=[OBJ], extra_names=["Ñandú", "abc"])
    for new_name in ("/Game/T/p", "/Game/T/pkg_renamed_to_something_longer"):
        out, rep = zp.rename_legacy_package(data, new_name)
        p = zp.parse_legacy(out)
        assert p.package_name == new_name and "Ñandú" in p.names
        assert p.exports[0].global_import_index_or_hash == gidx(f"{new_name}.Équipe")
    with pytest.raises(zp.RenameError, match="duplicate"):
        zp.rename_legacy_package(data, "abc" if False else "/Game/T/pkg", name_renames={"abc": "Ñandú"})
    with pytest.raises(zp.RenameError, match="names"):
        zp.rename_legacy_package(data, "/Game/T/x", old_name="/Game/T/other")
    bad = zp.build_legacy_package("/Game/T/bad", [zp.ExportSpec("A", b"y" * 9, OBJ, global_import_index=0x1234,
                                                                object_flags=1)], imports=[OBJ])
    with pytest.raises(zp.RenameError, match="GlobalImportIndex"):
        zp.rename_legacy_package(bad, "/Game/T/bad2")


# =============================================================================================
# add: raw chunks + paths + store entries (synthetic containers, both layouts)
# =============================================================================================

LAYOUTS = ["efootball-16", "ue4-32"]


@pytest.fixture(scope="module")
def global_utoc(tmp_path_factory):
    return build_global(str(tmp_path_factory.mktemp("global")))


def _new_package_chunks(name: str, imports_from=(FACE,)):
    data, entry = make_package(name, OBJ, list(imports_from), seed=9)
    pid = zp.package_id_from_name(name)
    bulk = _payload(91, 2 * BS + 5)
    chunks = [iostore.NewChunk(iostore.ChunkId.from_parts(pid, 0, 2), data, pkg_path(name)),
              iostore.NewChunk(iostore.ChunkId.from_parts(pid, 0, 3), bulk, pkg_path(name, ".ubulk"))]
    se = iostore.StoreEntry(entry["export_count"], entry["export_bundle_count"], entry["imported"])
    return chunks, se, data


@pytest.mark.parametrize("layout", LAYOUTS)
def test_add_package_chunks_paths_and_store_entry(layout, tmp_path, global_utoc):
    info = build_game_container(str(tmp_path / "in"), layout)
    name = "/Game/Face/r9/face_r9_2002"
    chunks, se, data = _new_package_chunks(name)
    if layout == "ue4-32":
        se.export_bundles_size, se.load_order, se.pad = len(data), 99, 0
    with iostore.IoStoreContainer(info["utoc"]) as c:
        before = iostore.hash_all(c)
        m = iostore.add_to_container(c, chunks, str(tmp_path / "out"), store_entries=[(zp.package_id_from_name(name), se)],
                                     verify=True)
    assert m["verify"]["ok"], m["verify"]["failures"]
    assert m["container_header"]["layout"] == layout
    assert m["container_header"]["order"] == ("sorted" if layout == "ue4-32" else "append")
    out_utoc = str(tmp_path / "out" / "pc3600_mobile_ios.utoc")
    with iostore.IoStoreContainer(out_utoc) as o:
        after = iostore.hash_all(o)
        d = iostore.diff_tables(before, after)
        # only the container header chunk changed; two chunks added; nothing removed
        assert [x["chunk_id"] for x in d["changed"]] == [make_chunk_id(info["container_id"], 0, 10).hex()]
        assert sorted(iostore.strip_mount(x["path"]) for x in d["added"]) == \
            sorted([pkg_path(name), pkg_path(name, ".ubulk")])
        assert not d["removed"] and d["unchanged"] == len(before["chunks"]) - 1
        # resolve by id and by path
        i = o.toc.resolve(chunks[0].chunk_id)
        assert o.read_chunk(i) == data
        assert iostore.resolve_selector(o, pkg_path(name)) == i
        raw_h = o.read_chunk(iostore.find_container_header_index(o.toc))
    ind = decode_store_independently(raw_h, layout)
    pid = zp.package_id_from_name(name)
    assert ind[pid]["imported"] == [zp.package_id_from_name(FACE)]
    for p, e in info["header_pkgs"].items():
        assert ind[p]["imported"] == e["imported"] and ind[p]["export_count"] == e["export_count"]
        if layout == "ue4-32":
            assert ind[p]["ebs"] == e["ebs"]
    if _cue4parse_available():
        j = cue4parse_full(out_utoc, GAME_FOR_LAYOUT[layout], global_utoc=global_utoc)
        assert_cue4parse_chunks_agree(j, out_utoc, None)
        ch = j["container_header"]
        assert ch["parsed"] and ch["package_count"] == len(info["header_pkgs"]) + 1
        by_id = {int(p["package_id"], 16): p for p in ch["packages"]}
        assert by_id[pid]["chunk_resolve_ok"] and by_id[pid]["path"] == pkg_path(name)
        assert by_id[pid]["imported_packages"] == [f"0x{zp.package_id_from_name(FACE):016X}"]
        for p, e in info["header_pkgs"].items():
            assert [int(x, 16) for x in by_id[p]["imported_packages"]] == e["imported"]
            assert by_id[p]["export_count"] == e["export_count"] and by_id[p]["chunk_resolve_ok"]
        prov = {int(p["package_id"], 16): p for p in j["provider"]["packages"]}
        assert j["provider"]["global_data_loaded"]
        assert prov[pid]["load_by_id_ok"] and prov[pid]["load_by_path_ok"] and prov[pid]["name"] == name
        assert prov[pid]["name_hash_matches_id"]
        # negative control: the other layout makes CUE4Parse fail or misread the entries
        other = "GAME_UE4_26" if layout == "efootball-16" else "GAME_eFootball"
        assert_misread(cue4parse_full(out_utoc, other, expect_ok=False)["container_header"], ind)


@pytest.mark.parametrize("layout", LAYOUTS)
def test_original_game_container_reads_in_cue4parse(layout, tmp_path, global_utoc):
    """Control: the synthetic input itself is read correctly by CUE4Parse with the matching game."""
    if not _cue4parse_available():
        pytest.skip("CUE4Parse harness not built")
    info = build_game_container(str(tmp_path / "in"), layout)
    j = cue4parse_full(info["utoc"], GAME_FOR_LAYOUT[layout], global_utoc=global_utoc)
    assert_cue4parse_chunks_agree(j, info["utoc"], None)
    ch = j["container_header"]
    assert ch["parsed"] and ch["package_count"] == 6
    for p in ch["packages"]:
        e = info["header_pkgs"][int(p["package_id"], 16)]
        assert [int(x, 16) for x in p["imported_packages"]] == e["imported"]
    for p in j["provider"]["packages"]:
        assert p["load_by_id_ok"] and p["load_by_path_ok"] and p["name_hash_matches_id"]


def test_add_encrypted_container_reencrypts_directory_index(tmp_path, global_utoc):
    key = os.urandom(32)
    info = build_game_container(str(tmp_path / "in"), "efootball-16", version=3, key=key)
    name = "/Game/Face/r9/face_r9_2002"
    chunks, se, data = _new_package_chunks(name)
    with iostore.IoStoreContainer(info["utoc"], key) as c:
        m = iostore.add_to_container(c, chunks, str(tmp_path / "out"), store_entries=[(zp.package_id_from_name(name), se)],
                                     verify=True)
    assert m["verify"]["ok"] and m["directory_index"]["encrypted"]
    out_utoc = str(tmp_path / "out" / "pc3600_mobile_ios.utoc")
    with iostore.IoStoreContainer(out_utoc, key) as o:
        assert o.key_status() == "accepted"
        assert o.read_chunk(iostore.resolve_selector(o, pkg_path(name))) == data
    with iostore.IoStoreContainer(out_utoc, None) as o:
        with pytest.raises(iostore.KeyRequired):
            o.paths()
    if _cue4parse_available():
        j = cue4parse_full(out_utoc, "GAME_eFootball", key=key, global_utoc=global_utoc)
        assert_cue4parse_chunks_agree(j, out_utoc, key)
        assert j["aes_key_accepted"]
        prov = {int(p["package_id"], 16): p for p in j["provider"]["packages"]}
        assert prov[zp.package_id_from_name(name)]["load_by_path_ok"]


PH_VARIANTS = {
    "v4_perfecthash": dict(version=4, perfect_hash=True, meta_algo="blake3"),
    "v5_overflow_in": dict(version=5, perfect_hash=True, max_seed=0, meta_algo="blake3"),
    "v5_aes": dict(version=5, perfect_hash=True, aes=True, meta_algo="blake3"),
    "v8_iohash": dict(version=8, perfect_hash=True, meta_algo="blake3"),
    "v10_aes_ctr": dict(version=10, perfect_hash=True, aes=True, ctr=True, meta_algo="blake3"),
}


@pytest.mark.parametrize("variant", list(PH_VARIANTS))
@pytest.mark.parametrize("force_overflow", [False, True])
def test_add_rebuilds_perfect_hash(variant, force_overflow, tmp_path):
    from test_iostore import standard_chunks
    spec = dict(PH_VARIANTS[variant])
    key = os.urandom(32) if spec.pop("aes", False) else None
    info = build_container(str(tmp_path / "in"), "pakchunk_r9", standard_chunks(), key=key, **spec)
    rnd = random.Random(7)
    new = [iostore.NewChunk(iostore.ChunkId(make_chunk_id(rnd.getrandbits(64), 0, rnd.choice([1, 2]))),
                            _payload(200 + k, rnd.choice([0, 10, BS + 3])),
                            f"eFootball/Content/R9/new_{k}.uasset" if k % 2 == 0 else None,
                            2 if k == 3 else 0) for k in range(5)]
    max_seed = 0 if force_overflow else 1 << 20
    with iostore.IoStoreContainer(info["utoc"], key) as c:
        if force_overflow and spec["version"] < 5:
            with pytest.raises(iostore.IoStoreError, match="overflow"):
                iostore.add_to_container(c, new, str(tmp_path / "out"), max_seed=max_seed)
            return
        m = iostore.add_to_container(c, new, str(tmp_path / "out"), max_seed=max_seed, verify=True)
    assert m["verify"]["ok"], m["verify"]["failures"]
    assert m["perfect_hash"]["rebuilt"]
    if force_overflow:
        assert m["perfect_hash"]["overflow"] > 0
    out_utoc = str(tmp_path / "out" / "pakchunk_r9.utoc")
    with iostore.IoStoreContainer(out_utoc, key) as o:
        assert o.toc.validate_perfect_hash()["ok"]
        for nc in new:
            assert o.read_chunk(o.toc.resolve(nc.chunk_id)) == nc.data
        for ch in info["table"]:
            assert o.read_chunk(o.toc.resolve(iostore.ChunkId(ch.cid))) == ch.data
        for k, nc in enumerate(new):
            if nc.path:
                assert iostore.resolve_selector(o, nc.path) == o.toc.resolve(nc.chunk_id)
    if _cue4parse_available():
        game = "GAME_UE6_0" if spec["version"] >= 10 else "GAME_eFootball"
        j = run_cue4parse([out_utoc, "--game", game], key)
        assert_cue4parse_chunks_agree(j, out_utoc, key)


def test_add_refusals(tmp_path):
    info = build_game_container(str(tmp_path / "in"), "efootball-16")
    face_id = zp.package_id_from_name(FACE)
    with iostore.IoStoreContainer(info["utoc"]) as c:
        dup = iostore.NewChunk(iostore.ChunkId.from_parts(face_id, 0, 2), b"x")
        with pytest.raises(iostore.IoStoreError, match="already exists"):
            iostore.add_to_container(c, [dup], str(tmp_path / "o1"))
        clash = iostore.NewChunk(iostore.ChunkId.from_parts(1, 0, 2), b"x", pkg_path(FACE).upper())
        with pytest.raises(iostore.IoStoreError, match="already exists"):
            iostore.add_to_container(c, [clash], str(tmp_path / "o2"))
        outside = iostore.NewChunk(iostore.ChunkId.from_parts(1, 0, 2), b"x", "../elsewhere/x.uasset")
        with pytest.raises(iostore.IoStoreError, match="mount point"):
            iostore.add_to_container(c, [outside], str(tmp_path / "o3"))
        with pytest.raises(iostore.IoStoreError, match="already in the container header"):
            iostore.add_to_container(c, [], str(tmp_path / "o4"),
                                     store_entries=[(face_id, iostore.StoreEntry(1, 1, []))])
        with pytest.raises(iostore.IoStoreError, match="ue4-32"):
            iostore.add_to_container(c, [], str(tmp_path / "o5"), store_layout="ue4-32",
                                     store_entries=[(2, iostore.StoreEntry(1, 1, []))])
        with pytest.raises(iostore.IoStoreError, match="nothing to add"):
            iostore.add_to_container(c, [], str(tmp_path / "o6"))
        twice = [iostore.NewChunk(iostore.ChunkId.from_parts(3, 0, 2), b"a"),
                 iostore.NewChunk(iostore.ChunkId.from_parts(3, 0, 2), b"b")]
        with pytest.raises(iostore.IoStoreError, match="twice"):
            iostore.add_to_container(c, twice, str(tmp_path / "o7"))
    for d in ("o1", "o2", "o3", "o4", "o5", "o6", "o7"):
        assert not (tmp_path / d).exists() or not os.listdir(tmp_path / d), "a refused add must not write"
    noidx = build_container(str(tmp_path / "noidx"), "noidx", [SChunk(make_chunk_id(5, 0, 2), b"abc", None, "none")], 2,
                            indexed=False)
    with iostore.IoStoreContainer(noidx["utoc"]) as c:
        with pytest.raises(iostore.IoStoreError, match="no directory index"):
            iostore.add_to_container(c, [iostore.NewChunk(iostore.ChunkId.from_parts(6, 0, 2), b"x", "a/b.uasset")],
                                     str(tmp_path / "o8"))
        m = iostore.add_to_container(c, [iostore.NewChunk(iostore.ChunkId.from_parts(6, 0, 2), b"x")],
                                     str(tmp_path / "o9"), verify=True)
        assert m["verify"]["ok"]


def test_add_signed_needs_unsign(tmp_path):
    from test_iostore import standard_chunks
    info = build_container(str(tmp_path / "in"), "signed", standard_chunks()[:4], 3, signed=True)
    nc = [iostore.NewChunk(iostore.ChunkId.from_parts(77, 0, 2), b"new", "eFootball/Content/x.uasset")]
    with iostore.IoStoreContainer(info["utoc"]) as c:
        with pytest.raises(iostore.IoStoreError, match="Signed"):
            iostore.add_to_container(c, nc, str(tmp_path / "o1"))
        m = iostore.add_to_container(c, nc, str(tmp_path / "o2"), unsign=True, verify=True)
    assert m["verify"]["ok"] and any("Signed flag cleared" in w for w in m["warnings"])


# =============================================================================================
# clone-package: the independent R9 face (brief section 8)
# =============================================================================================

CLONE_SET = [(FACE, "/Game/Face/pc3600/face_r9_2002"), (MI, "/Game/Face/pc3600/MI_face_r9_2002"),
             (TEXP, "/Game/Face/pc3611/T_face_r9_2002")]


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("version", [2, 3])
def test_clone_face_set_independent(layout, version, tmp_path, global_utoc):
    info = build_game_container(str(tmp_path / "in"), layout, version=version,
                                partition_size=160 * 1024 if version == 3 else None)
    out_dir = str(tmp_path / "out")
    with iostore.IoStoreContainer(info["utoc"]) as c:
        before = iostore.hash_all(c)
        old_paths = {p: c.toc.chunk_ids[i].hex for p, i in c.paths()}
        m = iostore.clone_packages(c, CLONE_SET, out_dir, name_renames={"face_110718": "face_r9_2002"}, verify=True)
    assert m["verify"]["ok"], m["verify"]["failures"]
    out_utoc = os.path.join(out_dir, "pc3600_mobile_ios.utoc")
    reps = {r["old_package_name"]: r for r in m["clone"]["packages"]}
    new_of = dict(CLONE_SET)
    ids = {n: zp.package_id_from_name(n) for n in [x for pair in CLONE_SET for x in pair] + [P1, P2, OTHER]}
    with iostore.IoStoreContainer(out_utoc) as o:
        after = iostore.hash_all(o)
        d = iostore.diff_tables(before, after)
        # every old chunk byte-identical except the container header; 3 packages + 2 bulk chunks added
        assert [x["chunk_id"] for x in d["changed"]] == [make_chunk_id(info["container_id"], 0, 10).hex()]
        assert len(d["added"]) == 5 and not d["removed"]
        new_paths = {p: o.toc.chunk_ids[i].hex for p, i in o.paths()}
        assert all(new_paths[p] == cid for p, cid in old_paths.items())   # old paths -> same chunk ids
        assert len(new_paths) == len(old_paths) + 5
        pkgs = {}
        for old, new in CLONE_SET:
            nid = ids[new]
            j = o.toc.resolve(iostore.ChunkId.from_parts(nid, 0, 2))
            assert j is not None and iostore.resolve_selector(o, pkg_path(new)) == j   # by id and by path
            pkgs[new] = zp.parse_legacy(o.read_chunk(j))
            assert pkgs[new].package_name == new
            src = [p for p in info["content"] if p["name"] == old][0]
            if src["bulk"] is not None:   # bulk data copied byte for byte under the new id
                jb = o.toc.resolve(iostore.ChunkId.from_parts(nid, 0, 3))
                assert o.read_chunk(jb) == src["bulk"]
                assert iostore.resolve_selector(o, pkg_path(new, ".ubulk")) == jb
            # export data identical to the donor's
            sp = zp.parse_legacy(src["data"])
            for a, b in zip(sp.exports, pkgs[new].exports):
                assert src["data"][a.abs_start:a.abs_end] == o.read_chunk(j)[b.abs_start:b.abs_end]
        face, mi, tex = (pkgs[n] for _, n in CLONE_SET)
        # rewiring inside the clone set: face -> MI clone -> texture clone
        assert gidx(f"{new_of[MI]}.MI_face_r9_2002") not in face.imports  # MI object name was not renamed
        assert gidx(f"{new_of[MI]}.MI_face_110718") in face.imports and gidx(f"{MI}.MI_face_110718") not in face.imports
        assert [p["package_id"] for p in face.imported_packages] == [ids[new_of[MI]]]
        assert gidx(f"{new_of[TEXP]}.T_face_110718") in mi.imports
        assert [p["package_id"] for p in mi.imported_packages] == [ids[new_of[TEXP]]]
        assert face.exports[0].object_name == "face_r9_2002"   # --rename-name
        assert face.exports[0].global_import_index_or_hash == gidx(f"{new_of[FACE]}.face_r9_2002")
        hraw = o.read_chunk(iostore.find_container_header_index(o.toc))
    ind = decode_store_independently(hraw, layout)
    assert ind[ids[new_of[FACE]]]["imported"] == [ids[new_of[MI]]]
    assert ind[ids[new_of[MI]]]["imported"] == [ids[new_of[TEXP]]]
    assert ind[ids[new_of[TEXP]]]["imported"] == []
    for p, e in info["header_pkgs"].items():   # donors and players unchanged: they keep the shared donor
        assert ind[p]["imported"] == e["imported"]
    assert ind[ids[P1]]["imported"] == [ids[FACE]] and ind[ids[P2]]["imported"] == [ids[FACE]]
    assert reps[FACE]["packages_in_this_container_importing_the_source"]["count"] == 2
    if layout == "ue4-32":
        assert m["clone"]["export_bundles_size_measurement"]["relation"] == "chunk_size"
        for old, new in CLONE_SET:
            r = reps[old]["store_entry_export_bundles_size"]
            assert ind[ids[new]]["ebs"] == r["new"]
            if r["rule"].startswith("new ExportBundleData chunk size"):
                with iostore.IoStoreContainer(out_utoc) as o:
                    assert r["new"] == o.toc.offsets[o.toc.resolve(iostore.ChunkId.from_parts(ids[new], 0, 2))][1]
    if _cue4parse_available():
        j = cue4parse_full(out_utoc, GAME_FOR_LAYOUT[layout], global_utoc=global_utoc)
        assert_cue4parse_chunks_agree(j, out_utoc, None)
        hdr = {int(p["package_id"], 16): p for p in j["container_header"]["packages"]}
        prov = {int(p["package_id"], 16): p for p in j["provider"]["packages"]}
        assert j["container_header"]["package_count"] == 9
        for old, new in CLONE_SET:
            assert hdr[ids[new]]["chunk_resolve_ok"] and hdr[ids[new]]["path"] == pkg_path(new)
            assert [int(x, 16) for x in hdr[ids[new]]["imported_packages"]] == ind[ids[new]]["imported"]
            p = prov[ids[new]]
            assert p["load_by_id_ok"] and p["load_by_path_ok"] and p["name"] == new and p["name_hash_matches_id"]
            assert [int(x["global_import_index"], 16) for x in p["exports"]] == \
                   [e.global_import_index_or_hash for e in pkgs[new].exports]
            assert [int(x, 16) for x in p["imports"]] == pkgs[new].imports
            assert p["exports"][0]["name"] == pkgs[new].exports[0].object_name
        # the donors are untouched for CUE4Parse too
        j0 = cue4parse_full(info["utoc"], GAME_FOR_LAYOUT[layout], global_utoc=global_utoc)
        prov0 = {int(p["package_id"], 16): p for p in j0["provider"]["packages"]}
        for pid, p in prov0.items():
            for k in ("name", "names", "imports", "exports", "path"):
                assert prov[pid][k] == p[k], (pid, k)
        hdr0 = {int(p["package_id"], 16): p for p in j0["container_header"]["packages"]}
        for pid, p in hdr0.items():
            assert hdr[pid] == p


def test_clone_ue4_32_export_bundles_size_rules(tmp_path):
    info = build_game_container(str(tmp_path / "in"), "ue4-32", ebs_rule="arbitrary")
    longer = [(FACE, "/Game/Face/pc3600/face_r9_2002_world_cup_final_version_with_a_long_name")]
    same_len = [(OTHER, "/Game/Misc/oth3r")]
    with iostore.IoStoreContainer(info["utoc"]) as c:
        with pytest.raises(iostore.IoStoreError, match="ExportBundlesSize"):
            iostore.clone_packages(c, longer, str(tmp_path / "o1"))
        m = iostore.clone_packages(c, longer, str(tmp_path / "o2"), export_bundles_size="chunk-size", verify=True)
        assert m["verify"]["ok"]
        r = m["clone"]["packages"][0]["store_entry_export_bundles_size"]
        assert r["rule"] == "new ExportBundleData chunk size" and r["new"] == m["added_chunks"][0]["size"]
        m = iostore.clone_packages(c, same_len, str(tmp_path / "o3"), verify=True)
        r = m["clone"]["packages"][0]["store_entry_export_bundles_size"]
        assert r["rule"].startswith("copied") and r["new"] == r["old"]
        assert m["clone"]["export_bundles_size_measurement"]["relation"] == "unknown"
        m = iostore.clone_packages(c, longer, str(tmp_path / "o4"), export_bundles_size="4242", verify=True)
        assert m["clone"]["packages"][0]["store_entry_export_bundles_size"]["new"] == 4242


def test_clone_refusals(tmp_path):
    info = build_game_container(str(tmp_path / "in"), "efootball-16")
    with iostore.IoStoreContainer(info["utoc"]) as c:
        with pytest.raises(iostore.IoStoreError, match="already has chunks"):
            iostore.clone_packages(c, [(FACE, P1)], str(tmp_path / "o1"))
        with pytest.raises(iostore.IoStoreError, match="expected one ExportBundleData"):
            iostore.clone_packages(c, [("/Game/Nope/x", "/Game/Nope/y")], str(tmp_path / "o2"))
        with pytest.raises(iostore.IoStoreError, match="ASCII"):
            iostore.clone_packages(c, [(FACE, "/Game/Face/rónaldo")], str(tmp_path / "o3"))
        with pytest.raises(iostore.IoStoreError, match="not found in any cloned package"):
            iostore.clone_packages(c, [(FACE, "/Game/Face/r9")], str(tmp_path / "o4"), name_renames={"zzz": "y"})
        with pytest.raises(iostore.IoStoreError, match="twice"):
            iostore.clone_packages(c, [(FACE, "/Game/Face/r9"), (FACE, "/Game/Face/r9b")], str(tmp_path / "o5"))
    # a package whose GlobalImportIndex does not follow the formula is refused
    bad = zp.build_legacy_package("/Game/Bad/b", [zp.ExportSpec("b", b"z" * 30, OBJ, global_import_index=0x77,
                                                                object_flags=1)], imports=[OBJ])
    pid = zp.package_id_from_name("/Game/Bad/b")
    cid = 0xBADBADBAD
    hdr = build_container_header(cid, [(pid, {"export_count": 1, "export_bundle_count": 1})], "efootball-16")
    b = build_container(str(tmp_path / "bad"), "bad", [SChunk(make_chunk_id(pid, 0, 2), bad, f"{ROOT}/Bad/b.uasset"),
                                                       SChunk(make_chunk_id(cid, 0, 10), hdr)], 2, container_id=cid)
    with iostore.IoStoreContainer(b["utoc"]) as c:
        with pytest.raises(iostore.IoStoreError, match="GlobalImportIndex"):
            iostore.clone_packages(c, [("/Game/Bad/b", "/Game/Bad/c")], str(tmp_path / "o6"))
    for d in ("o1", "o2", "o3", "o4", "o5", "o6"):
        assert not (tmp_path / d).exists() or not os.listdir(tmp_path / d)


# =============================================================================================
# The real eFootball / UE4.27 container headers inside a container, read back by CUE4Parse
# =============================================================================================

@pytest.mark.parametrize("which", ["efootball", "ue427"])
def test_real_header_in_container_add_and_cue4parse(which, tmp_path, global_utoc):
    path = REAL_EFOOTBALL_HEADER if which == "efootball" else REAL_UE427_HEADER
    raw = _real(path)
    real = iostore.ContainerHeaderUE4.parse(raw)
    layout = "efootball-16" if which == "efootball" else "ue4-32"
    name = "/Game/Face/r9/face_r9_2002"
    pkg_data, entry = make_package(name, SKM, [], seed=11)
    pid = zp.package_id_from_name(name)
    other, _ = make_package(OTHER, OBJ, [], seed=6)   # one unrelated chunk to measure the meta hash algorithm on
    chunks = [SChunk(make_chunk_id(real.container_id, 0, 10), raw, None, "zlib"),
              SChunk(make_chunk_id(zp.package_id_from_name(OTHER), 0, 2), other, pkg_path(OTHER), "zlib")]
    info = build_container(str(tmp_path / "in"), "real_" + which, chunks, 2, meta_algo="sha1",
                           container_id=real.container_id)
    se = iostore.StoreEntry(entry["export_count"], 1, [real.package_ids[0]])
    if layout == "ue4-32":
        se.export_bundles_size, se.load_order, se.pad = len(pkg_data), 0, 0
    new = [iostore.NewChunk(iostore.ChunkId.from_parts(pid, 0, 2), pkg_data, pkg_path(name))]
    with iostore.IoStoreContainer(info["utoc"]) as c:
        m = iostore.add_to_container(c, new, str(tmp_path / "out"), store_entries=[(pid, se)], verify=True)
    assert m["verify"]["ok"] and m["container_header"]["layout"] == layout
    out_utoc = str(tmp_path / "out" / f"real_{which}.utoc")
    with iostore.IoStoreContainer(out_utoc) as o:
        hraw = o.read_chunk(iostore.find_container_header_index(o.toc))
    ind = decode_store_independently(hraw, layout)
    orig = decode_store_independently(raw, layout)
    assert len(ind) == len(orig) + 1 and all(ind[p] == e for p, e in orig.items())
    assert ind[pid]["imported"] == [real.package_ids[0]]
    if not _cue4parse_available():
        return
    game = "GAME_eFootball" if which == "efootball" else "GAME_UE4_27"
    j = cue4parse_full(out_utoc, game, global_utoc=global_utoc)
    ch = j["container_header"]
    assert ch["parsed"] and ch["package_count"] == len(orig) + 1
    assert ch["container_id"] == f"0x{real.container_id:016X}"
    got = {int(p["package_id"], 16): p for p in ch["packages"]}
    for p, e in orig.items():
        assert [int(x, 16) for x in got[p]["imported_packages"]] == e["imported"]
        assert got[p]["export_count"] == e["export_count"] and got[p]["export_bundle_count"] == e["export_bundle_count"]
    assert got[pid]["chunk_resolve_ok"] and got[pid]["path"] == pkg_path(name)
    prov = {int(p["package_id"], 16): p for p in j["provider"]["packages"]}
    assert prov[pid]["load_by_id_ok"] and prov[pid]["name"] == name
    # negative control: reading with the other entry layout fails or misreads
    other = "GAME_UE4_26" if which == "efootball" else "GAME_eFootball"
    assert_misread(cue4parse_full(out_utoc, other, expect_ok=False)["container_header"], ind)


# =============================================================================================
# CLI end to end (also: the AES key never appears in any output)
# =============================================================================================

def run_cli(*args, env_key=None, check=True):
    env = dict(os.environ)
    env.pop("R9_AES_KEY_HEX", None)
    if env_key is not None:
        env["R9_AES_KEY_HEX"] = env_key
    r = subprocess.run([sys.executable, IOSTORE, *args], capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        raise AssertionError(f"{args}: rc={r.returncode}\n{r.stdout}\n{r.stderr}")
    return r


def test_cli_add_clone_header_package_id(tmp_path):
    key = os.urandom(32)
    info = build_game_container(str(tmp_path / "in"), "efootball-16", version=3, key=key)
    kh = key.hex()
    outputs = []
    r = run_cli("package-id", FACE, "/Game/Face/pc3600/face_r9_2002")
    rows = json.loads(r.stdout)
    assert rows[0]["package_id"] == f"0x{zp.package_id_from_name(FACE):016X}"
    assert rows[0]["chunk_ids"]["ExportBundleData"] == make_chunk_id(zp.package_id_from_name(FACE), 0, 2).hex()
    r = run_cli("container-header", info["utoc"], env_key=kh)
    outputs.append(r.stdout + r.stderr)
    h = json.loads(r.stdout)
    assert h["layout"] == "efootball-16" and h["package_count"] == 6 and h["reserialize_identical"]
    # clone through the CLI
    r = run_cli("clone-package", info["utoc"], "--out-dir", str(tmp_path / "c"), "--package",
                f"{FACE}=/Game/Face/pc3600/face_r9_2002", "--package", f"{MI}=/Game/Face/pc3600/MI_face_r9_2002",
                "--rename-name", "face_110718=face_r9_2002", "--verify", env_key=kh)
    outputs.append(r.stdout + r.stderr)
    cj = json.loads(r.stdout)
    assert cj["verify"] is True and cj["store_entry_layout"] == "efootball-16"
    man = open(tmp_path / "c" / "pc3600_mobile_ios.manifest.json").read()
    outputs.append(man)
    assert json.loads(man)["operation"] == "clone-package"
    # add through the CLI (JSON list), on top of the clone output
    name = "/Game/Extra/r9_hair"
    data, entry = make_package(name, OBJ, ["/Game/Face/pc3600/face_r9_2002"], seed=12)
    (tmp_path / "hair.uasset").write_bytes(data)
    spec = {"chunks": [{"file": "hair.uasset", "package": name, "type": "ExportBundleData", "path": pkg_path(name)}],
            "store_entries": [{"package": name, "export_count": entry["export_count"], "export_bundle_count": 1,
                               "imported_packages": ["/Game/Face/pc3600/face_r9_2002"]}]}
    (tmp_path / "add.json").write_text(json.dumps(spec))
    r = run_cli("add", str(tmp_path / "c" / "pc3600_mobile_ios.utoc"), "--out-dir", str(tmp_path / "a"),
                "--add-list", str(tmp_path / "add.json"), "--verify", env_key=kh)
    outputs.append(r.stdout + r.stderr)
    aj = json.loads(r.stdout)
    assert aj["verify"] is True and aj["added"][0][2] == pkg_path(name)
    r = run_cli("container-header", str(tmp_path / "a" / "pc3600_mobile_ios.utoc"), "--all", env_key=kh)
    h2 = json.loads(r.stdout)
    assert h2["package_count"] == 9
    e = [x for x in h2["entries"] if x["package_id"] == f"0x{zp.package_id_from_name(name):016X}"][0]
    assert e["imported_packages"] == [f"0x{zp.package_id_from_name('/Game/Face/pc3600/face_r9_2002'):016X}"]
    # failing invocations are clean (exit 2, no traceback)
    r = run_cli("clone-package", info["utoc"], "--out-dir", str(tmp_path / "x"), "--package", f"{FACE}={P1}",
                env_key=kh, check=False)
    assert r.returncode == 2 and "Traceback" not in r.stderr and "already has chunks" in r.stderr
    r = run_cli("add", info["utoc"], "--out-dir", str(tmp_path / "y"), "--add-list", str(tmp_path / "add.json"),
                check=False)
    assert r.returncode == 2 and "encrypted" in r.stderr
    blob = "\n".join(outputs)
    for form in (kh, kh.upper()):
        assert form not in blob
