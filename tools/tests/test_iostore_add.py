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


@pytest.mark.skipif(not (os.path.exists(CUE4PARSE_CHECK) and os.path.exists(os.path.join(DOTNET_ROOT, "dotnet"))),
                    reason="CUE4Parse harness / .NET runtime not built")
def test_package_ids_match_cue4parse_fromname():
    """Our FPackageId::FromName vs CUE4Parse's C# FPackageId.FromName (CityHash64 + ToLower)."""
    names = ["/Game/Face/pc3600/face_110718", "/Game/Face/pc3600/Face_R9_2002", "/GAME/X", "/Game/a",
             "/Game/" + "VeryLongPackageName_" * 4,                       # > 64 bytes of UTF-16: CityHash long path
             "/Game/" + "x" * 300,                                         # > 256 chars: CUE4Parse heap branch
             "/Game/Équipe/Ñandú", "/Game/Mixed/AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-"]
    args = []
    for n in names:
        args += ["--package-id", n]
    env = dict(os.environ, DOTNET_ROOT=DOTNET_ROOT)
    r = subprocess.run([CUE4PARSE_CHECK, *args], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stderr
    got = {x["name"]: int(x["package_id"], 16) for x in json.loads(r.stdout)["package_ids_from_names"]}
    for n in names:
        assert iostore.package_id_from_name(n) == got[n], n


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
                 extra_names=(), split: bool = False) -> Tuple[bytes, dict]:
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
    data = zp.build_legacy_package(name, exps, imports=imports, imported_packages=graph, extra_names=list(extra_names),
                                   split_numbers=split)
    entry = {"export_count": len(exps), "export_bundle_count": 1,
             "imported": [zp.package_id_from_name(p) for p in imports_from]}
    return data, entry


def game_content(split: bool = False) -> List[dict]:
    """[{name, data, entry, bulk}] - face -> MI -> texture; two players share the donor face.
    split=True stores numbered names the way Unreal does (face_110718 -> entry 'face' + number 110719)."""
    pk = []
    for name, cls, imps, subs, seed, bulk, extra in [
        (TEXP, TEX, [], 0, 1, _payload(71, 70_000, compressible=False), ()),
        (MI, MIC, [TEXP], 0, 2, None, ()),
        (FACE, SKM, [MI], 2, 3, _payload(72, 3 * BS + 77), (f"{FACE}.face_110718",)),
        (P1, OBJ, [FACE], 0, 4, None, ()),
        (P2, OBJ, [FACE], 0, 5, None, ()),
        (OTHER, OBJ, [], 0, 6, _payload(73, 5000), ()),
    ]:
        data, entry = make_package(name, cls, imps, subs, seed, extra, split)
        pk.append({"name": name, "data": data, "entry": entry, "bulk": bulk})
    return pk


def build_game_container(out_dir: str, layout: str, *, version: int = 2, key: Optional[bytes] = None,
                         partition_size: Optional[int] = None, ebs_rule: str = "chunk", split: bool = False) -> dict:
    content = game_content(split)
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
    # documented field list (Name/SourceName now point at the appended new package-name entry)
    assert set(rep["summary_changes"]) <= {"Name", "SourceName", "NameMapNamesSize", "NameMapHashesOffset",
                                           "NameMapHashesSize", "ImportMapOffset", "ExportMapOffset",
                                           "ExportBundlesOffset", "GraphDataOffset"}
    assert rep["summary_changes"]["Name"]["new"] == [len(old.names), 0]
    assert new.names[len(old.names):] == [new_name] and new.names[0] == FACE   # old entry kept, new appended
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
    # names other than the renamed ones are identical (same index, same text)
    changed = {x["index"] for x in rep["name_map_changes"]}
    assert [n for i, n in enumerate(new.names[:len(old.names)]) if i not in changed] == \
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
        zp.rename_legacy_package(data, "/Game/T/pkg2", name_renames={"abc": "Ñandú"})
    with pytest.raises(zp.RenameError, match="names"):
        zp.rename_legacy_package(data, "/Game/T/x", old_name="/Game/T/other")
    bad = zp.build_legacy_package("/Game/T/bad", [zp.ExportSpec("A", b"y" * 9, OBJ, global_import_index=0x1234,
                                                                object_flags=1)], imports=[OBJ])
    with pytest.raises(zp.RenameError, match="GlobalImportIndex"):
        zp.rename_legacy_package(bad, "/Game/T/bad2")


# =============================================================================================
# FName numbers (review finding): Unreal stores "face_110718" as name-map entry "face" + FName
# number 110719. The fixtures above store every text whole; these use the Unreal form.
# =============================================================================================

def _ue58_packages():
    out = []
    for mode in ("Tagged", "Unversioned"):
        utoc = os.path.join(UE58_FIXTURES, mode, "Uncompressed", "CUE4ParseFixtures-Minimal-Uncompressed-Windows.utoc")
        if not os.path.exists(utoc):
            pytest.skip("CUE4Parse UE5.8 fixtures not found")
        with iostore.IoStoreContainer(utoc) as c:
            for p, i in c.paths():
                if c.toc.chunk_ids[i].type == 1:   # UE5 ExportBundleData
                    out.append(zp.parse_package(c.read_chunk(i)))
    return out


def test_split_fname_number_matches_unreal_cooked_data():
    """Real data: in packages cooked by Unreal Engine 5.8 itself, no name-map entry ends in a
    splittable _<number>, and every numbered export name is exactly what split_fname_number makes
    of its text (BodySetup_1 -> ('BodySetup', 2))."""
    entries = numbered = 0
    for p in _ue58_packages():
        for n in p.names:
            entries += 1
            assert zp.split_fname_number(n) == (n, 0), n
        for e in p.exports:
            idx, num = e.name_ref
            assert zp.split_fname_number(zp.format_fname(p.names[idx], num)) == (p.names[idx], num)
            numbered += bool(num)
    assert entries > 4000 and numbered > 500


def test_split_fname_number_rules():
    s = zp.split_fname_number
    assert s("face_110718") == ("face", 110719)
    assert s("/Game/Face/pc3600/face_110718") == ("/Game/Face/pc3600/face", 110719)
    assert s("x_0") == ("x", 1) and s("Rocket_04") == ("Rocket_04", 0) and s("face") == ("face", 0)
    assert s("a_b") == ("a_b", 0) and s("a_") == ("a_", 0) and s("a_1_2") == ("a_1", 3)
    assert s("x_2147483646") == ("x", 2147483647)
    assert s("x_٣") == ("x_٣", 0)   # a non-ASCII digit is not an FName number
    for bad in ("x_2147483647", "x_99999999999"):   # boundary not verified against UE: refused
        with pytest.raises(ValueError):
            s(bad)


def test_rename_numbered_package_names_like_unreal():
    data, _ = make_package(FACE, SKM, [MI], sub_exports=1, seed=3, split=True)
    old = zp.parse_legacy(data)
    assert old.names[0] == "/Game/Face/pc3600/face" and old.package_name == FACE
    out, rep = zp.rename_legacy_package(data, "/Game/Face/pc3600/face_r9_2002", old_name=FACE)
    new = zp.parse_legacy(out)
    idx, num = struct.unpack_from("<II", out, 0)
    assert new.names[idx] == "/Game/Face/pc3600/face_r9" and num == 2003 and new.package_name.endswith("face_r9_2002")
    assert struct.unpack_from("<II", out, 8) == (idx, num)                      # SourceName follows
    assert new.names[:len(old.names)] == old.names                              # nothing edited, one entry added
    assert rep["name_map_added"] == [{"index": len(old.names), "text": "/Game/Face/pc3600/face_r9"}]
    assert [e.name_ref for e in new.exports] == [e.name_ref for e in old.exports]
    assert new.exports[0].global_import_index_or_hash == gidx("/Game/Face/pc3600/face_r9_2002.face_110718")
    # x_N -> x_M reuses the existing entry (retoc FNameMap::store): no entry added
    out2, rep2 = zp.rename_legacy_package(data, "/Game/Face/pc3600/face_7")
    assert rep2["name_map_added"] == [] and zp.parse_legacy(out2).package_name == "/Game/Face/pc3600/face_7"
    assert struct.unpack_from("<II", out2, 0) == (0, 8)
    # a name with a leading-zero number is NOT split (Rocket_04 rule)
    out3, _ = zp.rename_legacy_package(data, "/Game/Face/pc3600/face_r9_02")
    assert zp.parse_legacy(out3).names[-1] == "/Game/Face/pc3600/face_r9_02" and struct.unpack_from("<I", out3, 4)[0] == 0
    # --rename-name: entry text only; a numbered target is refused, a numbered full name gets a hint
    with pytest.raises(zp.RenameError, match=r"ends in _<number>"):
        zp.rename_legacy_package(data, "/Game/F/r9", name_renames={"face": "face_r9_2002"})
    with pytest.raises(zp.RenameError, match="FName number 110719"):
        zp.rename_legacy_package(data, "/Game/F/r9", name_renames={"face_110718": "r9"})
    out4, _ = zp.rename_legacy_package(data, "/Game/F/r9", name_renames={"face": "r9face"})
    assert zp.parse_legacy(out4).exports[0].object_name == "r9face_110718"   # the number stays in the reference


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


def test_add_and_clone_with_deep_mount_point(tmp_path):
    """Mount point ../../../PesMobile/Content/: paths are stored below it; given with or without it."""
    content = {p["name"]: p for p in game_content()}
    cid = 0x51
    chunks, hp = [], []
    for k, n in enumerate([TEXP, OTHER]):
        pid = zp.package_id_from_name(n)
        chunks.append(SChunk(make_chunk_id(pid, 0, 2), content[n]["data"], pkg_path(n)[len(ROOT) + 1:], "zlib"))
        hp.append((pid, content[n]["entry"]))
    chunks.append(SChunk(make_chunk_id(cid, 0, 10), build_container_header(cid, hp, "efootball-16"), None, "zlib"))
    info = build_container(str(tmp_path / "in"), "deep", chunks, 2, mount=f"../../../{ROOT}/", container_id=cid)
    with iostore.IoStoreContainer(info["utoc"]) as c:
        assert c.mount_point == f"../../../{ROOT}/"
        m = iostore.clone_packages(c, [(TEXP, "/Game/Face/pc3611/T_r9")], str(tmp_path / "o1"), verify=True)
        assert m["verify"]["ok"], m["verify"]["failures"]
        assert m["clone"]["packages"][0]["chunks"][0]["new_path"] == f"../../../{pkg_path('/Game/Face/pc3611/T_r9')}"
        n1 = iostore.NewChunk(iostore.ChunkId.from_parts(1, 0, 2), b"a", "R9/a.uasset")                    # below the mount
        n2 = iostore.NewChunk(iostore.ChunkId.from_parts(2, 0, 2), b"b", f"../../../{ROOT}/R9/b.uasset")   # with it
        m = iostore.add_to_container(c, [n1, n2], str(tmp_path / "o2"), verify=True)
        assert m["verify"]["ok"], m["verify"]["failures"]
    with iostore.IoStoreContainer(str(tmp_path / "o2" / "deep.utoc")) as o:
        got = {iostore.strip_mount(p) for p, _ in o.paths()}
        assert {f"{ROOT}/R9/a.uasset", f"{ROOT}/R9/b.uasset"} <= got


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
        m = iostore.clone_packages(c, CLONE_SET, out_dir, name_renames={"face_110718": "face_r9"}, verify=True)
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
        assert face.exports[0].object_name == "face_r9"   # --rename-name (a trailing _<number> is refused)
        assert face.exports[0].global_import_index_or_hash == gidx(f"{new_of[FACE]}.face_r9")
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


def build_split_containers(root: str, layout: str = "efootball-16") -> Tuple[str, str]:
    """pc3611 (textures) and pc3600 (meshes, materials, players) as two containers."""
    content = {p["name"]: p for p in game_content()}
    out = []
    for cname, names, cid in (("pc3611_mobile_ios", [TEXP], 0x3611), ("pc3600_mobile_ios", [MI, FACE, P1, P2], 0x3600)):
        chunks, hp = [], []
        for k, n in enumerate(names):
            p = content[n]
            pid = zp.package_id_from_name(n)
            chunks.append(SChunk(make_chunk_id(pid, 0, 2), p["data"], pkg_path(n), "zlib"))
            if p["bulk"] is not None:
                chunks.append(SChunk(make_chunk_id(pid, 0, 3), p["bulk"], pkg_path(n, ".ubulk"), "alt"))
            hp.append((pid, dict(p["entry"], ebs=len(p["data"]), load_order=k)))
        chunks.append(SChunk(make_chunk_id(cid, 0, 10), build_container_header(cid, hp, layout), None, "zlib"))
        out.append(build_container(os.path.join(root, cname), cname, chunks, 2, meta_algo="sha1",
                                   container_id=cid)["utoc"])
    return out[0], out[1]


def test_clone_across_containers_with_rewire(tmp_path):
    """Textures live in pc3611 and meshes in pc3600 (brief section 7): clone the texture first, then
    the mesh + material with --rewire, so the material clone imports the TEXTURE CLONE."""
    tex_utoc, mesh_utoc = build_split_containers(str(tmp_path / "in"))
    t_new = "/Game/Face/pc3611/T_face_r9_2002"
    with iostore.IoStoreContainer(tex_utoc) as c:
        assert iostore.clone_packages(c, [(TEXP, t_new)], str(tmp_path / "t"), verify=True)["verify"]["ok"]
    tex_out = str(tmp_path / "t" / "pc3611_mobile_ios.utoc")
    pairs = [(FACE, "/Game/Face/pc3600/face_r9_2002"), (MI, "/Game/Face/pc3600/MI_face_r9_2002")]
    with iostore.IoStoreContainer(mesh_utoc) as c, iostore.IoStoreContainer(tex_out) as other:
        m = iostore.clone_packages(c, pairs, str(tmp_path / "m"), rewire=[(TEXP, t_new)], rewire_containers=[other],
                                   verify=True, extra_inputs=other.input_files())
        assert m["verify"]["ok"] and m["clone"]["rewire"][0]["public_exports_mapped"] == 1
        # without --rewire the material clone still points at the SHARED donor texture
        m0 = iostore.clone_packages(c, pairs, str(tmp_path / "m0"), verify=True)
    with iostore.IoStoreContainer(str(tmp_path / "m" / "pc3600_mobile_ios.utoc")) as o:
        mi = zp.parse_legacy(o.read_chunk(o.toc.resolve(iostore.ChunkId.from_parts(
            zp.package_id_from_name(pairs[1][1]), 0, 2))))
        hdr = decode_store_independently(o.read_chunk(iostore.find_container_header_index(o.toc)), "efootball-16")
    assert gidx(f"{t_new}.T_face_110718") in mi.imports and gidx(f"{TEXP}.T_face_110718") not in mi.imports
    assert [p["package_id"] for p in mi.imported_packages] == [zp.package_id_from_name(t_new)]
    assert hdr[zp.package_id_from_name(pairs[1][1])]["imported"] == [zp.package_id_from_name(t_new)]
    with iostore.IoStoreContainer(str(tmp_path / "m0" / "pc3600_mobile_ios.utoc")) as o:
        mi0 = zp.parse_legacy(o.read_chunk(o.toc.resolve(iostore.ChunkId.from_parts(
            zp.package_id_from_name(pairs[1][1]), 0, 2))))
    assert gidx(f"{TEXP}.T_face_110718") in mi0.imports
    with iostore.IoStoreContainer(mesh_utoc) as c:
        with pytest.raises(iostore.IoStoreError, match="not found"):
            iostore.clone_packages(c, pairs, str(tmp_path / "m1"), rewire=[(TEXP, t_new)])
    # CLI form
    r = run_cli("clone-package", mesh_utoc, "--out-dir", str(tmp_path / "m2"), "--package", f"{pairs[0][0]}={pairs[0][1]}",
                "--package", f"{pairs[1][0]}={pairs[1][1]}", "--rewire", f"{TEXP}={t_new}", "--rewire-with", tex_out,
                "--verify")
    assert json.loads(r.stdout)["verify"] is True


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
        # the new package name is APPENDED to the name map (the old entry is kept), so even a same-length
        # name changes the package size: auto cannot copy and the relation is unknown here -> refused
        with pytest.raises(iostore.IoStoreError, match="ExportBundlesSize"):
            iostore.clone_packages(c, same_len, str(tmp_path / "o3a"))
        m = iostore.clone_packages(c, same_len, str(tmp_path / "o3"), export_bundles_size="copy", verify=True)
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


@pytest.mark.parametrize("layout", LAYOUTS)
def test_clone_numbered_donor_stored_like_unreal(layout, tmp_path, global_utoc):
    """Donor face named like the brief's slot (face_110718) and stored as Unreal stores it: entry
    '/Game/Face/pc3600/face' + FName number 110719. Before the review fix, clone-package refused such
    a donor, and wrote a numbered new name (face_r9_2002) whole with number 0, which Unreal never does."""
    info = build_game_container(str(tmp_path / "in"), layout, split=True)
    with iostore.IoStoreContainer(info["utoc"]) as c:
        src = zp.parse_legacy(c.read_chunk(c.toc.resolve(iostore.ChunkId.from_parts(zp.package_id_from_name(FACE), 0, 2))))
        assert "/Game/Face/pc3600/face" in src.names and FACE not in src.names and src.package_name == FACE
        with pytest.raises(iostore.IoStoreError, match="entry text only"):
            iostore.clone_packages(c, CLONE_SET, str(tmp_path / "bad"), name_renames={"face_110718": "face_r9"})
        m = iostore.clone_packages(c, CLONE_SET, str(tmp_path / "out"), verify=True)
    assert m["verify"]["ok"], m["verify"]["failures"]
    out_utoc = str(tmp_path / "out" / "pc3600_mobile_ios.utoc")
    new_of = dict(CLONE_SET)
    pkgs = {}
    with iostore.IoStoreContainer(out_utoc) as o:
        for old, new in CLONE_SET:
            raw = o.read_chunk(o.toc.resolve(iostore.ChunkId.from_parts(zp.package_id_from_name(new), 0, 2)))
            p = pkgs[new] = zp.parse_legacy(raw)
            base, num = zp.split_fname_number(new)            # e.g. ('/Game/Face/pc3600/face_r9', 2003)
            idx, number = struct.unpack_from("<II", raw, 0)
            assert p.names[idx & 0x3FFFFFFF] == base and number == num == 2003 and p.package_name == new
            assert zp.split_fname_number(old)[0] in p.names    # donor entry kept, not edited
            assert all(zp.split_fname_number(n) == (n, 0) for n in p.names)   # nothing Unreal would not write
            assert p.exports[0].object_name == short(old)
            assert p.exports[0].global_import_index_or_hash == gidx(f"{new}.{short(old)}")
        assert gidx(f"{new_of[MI]}.MI_face_110718") in pkgs[new_of[FACE]].imports
        assert gidx(f"{new_of[TEXP]}.T_face_110718") in pkgs[new_of[MI]].imports
    if _cue4parse_available():
        j = cue4parse_full(out_utoc, GAME_FOR_LAYOUT[layout], global_utoc=global_utoc)
        assert_cue4parse_chunks_agree(j, out_utoc, None)
        prov = {int(p["package_id"], 16): p for p in j["provider"]["packages"]}
        for old, new in CLONE_SET:
            p = prov[zp.package_id_from_name(new)]
            assert p["load_by_id_ok"] and p["load_by_path_ok"] and p["name"] == new and p["name_hash_matches_id"]
            assert p["exports"][0]["name"] == short(old)
            assert [int(x["global_import_index"], 16) for x in p["exports"]] == \
                   [e.global_import_index_or_hash for e in pkgs[new].exports]
    # find-refs by face number still finds the donor set although "110718" is only an FName number
    with iostore.IoStoreContainer(info["utoc"]) as c:
        t = iostore.find_references(c, "110718")
    assert {x["package"] for x in t["name_map_hits"]} == {FACE, MI, TEXP}
    assert any(FACE in x["header_fnames"] for x in t["name_map_hits"])
    if _cue4parse_available():
        j0 = cue4parse_full(info["utoc"], GAME_FOR_LAYOUT[layout], global_utoc=global_utoc)
        for p0 in j0["provider"]["packages"]:     # donors and players identical for CUE4Parse too
            assert p0["name_hash_matches_id"]
            p1 = prov[int(p0["package_id"], 16)]
            assert all(p1[k] == p0[k] for k in ("name", "names", "imports", "exports", "path"))


def test_find_refs_shows_shared_donor_before_and_after_clone(tmp_path):
    info = build_game_container(str(tmp_path / "in"), "efootball-16")
    face_id, p1, p2 = (zp.package_id_from_name(x) for x in (FACE, P1, P2))
    with iostore.IoStoreContainer(info["utoc"]) as c:
        r = iostore.find_references(c, FACE)
        assert r["registered_in_container_header"] and r["package_name_in_header"] == FACE
        assert sorted(r["store_entries_importing_it"]) == sorted([f"0x{p1:016X}", f"0x{p2:016X}"])
        assert sorted(x["package"] for x in r["import_map_hits"]) == [P1, P2]
        assert sorted(x["package"] for x in r["graph_data_hits"]) == [P1, P2]
        assert [x["type"] for x in r["chunks_of_package"]] == ["ExportBundleData", "BulkData"]
        t = iostore.find_references(c, "110718")   # a face number as plain text
        assert sorted(iostore.strip_mount(p) for p in t["paths"]) == sorted(
            [pkg_path(FACE), pkg_path(FACE, ".ubulk"), pkg_path(MI), pkg_path(TEXP), pkg_path(TEXP, ".ubulk")])
        assert {x["package"] for x in t["name_map_hits"]} == {FACE, MI, TEXP}
        iostore.clone_packages(c, CLONE_SET, str(tmp_path / "out"))
    with iostore.IoStoreContainer(str(tmp_path / "out" / "pc3600_mobile_ios.utoc")) as o:
        donor = iostore.find_references(o, FACE)
        clone = iostore.find_references(o, CLONE_SET[0][1])
        mi_clone = iostore.find_references(o, CLONE_SET[1][1])
    assert sorted(x["package"] for x in donor["import_map_hits"]) == [P1, P2]   # players keep the donor
    assert clone["store_entries_importing_it"] == [] and clone["import_map_hits"] == []   # nobody uses the clone yet
    assert [x["package"] for x in mi_clone["import_map_hits"]] == [CLONE_SET[0][1]]   # only the face clone uses it
    r = run_cli("find-refs", info["utoc"], FACE)
    assert json.loads(r.stdout)["store_entries_importing_it"]


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
                "--rename-name", "face_110718=face_r9", "--verify", env_key=kh)
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


# =============================================================================================
# Second adversarial review (registration track): untouched bits, cross-checks, refusals
# =============================================================================================

HEADER_FIELDS_ADD_MAY_CHANGE = {          # byte ranges of FIoStoreTocHeader that `add` may rewrite
    (24, 28): "TocEntryCount", (28, 32): "TocCompressedBlockEntryCount", (48, 52): "DirectoryIndexSize",
    (52, 56): "PartitionCount (only when a partition is created)",
}


def _poison_reserved_header_fields(utoc: str, version: int) -> bytes:
    """Write non-zero values into every header field the reader ignores for this TOC version
    (game-specific data must survive a write) plus an unknown container-flag bit."""
    raw = bytearray(open(utoc, "rb").read())
    raw[17] = 0xA5                                   # Reserved0
    raw[18:20] = b"\x5A\xC3"                         # Reserved1
    raw[80] |= 0x40                                  # unknown EIoContainerFlags bit
    raw[82:84] = b"\x11\x22"                         # Reserved4
    if version < 4:
        raw[84:88] = b"\x33\x44\x55\x66"             # TocChunkPerfectHashSeedsCount (reserved before v4)
    if version < 3:
        raw[88:96] = b"\x77" * 8                     # PartitionSize (reserved before v3)
    if version < 5:
        raw[96:100] = b"\x88\x99\xAA\xBB"            # TocChunksWithoutPerfectHashCount (reserved before v5)
    raw[104:144] = bytes(range(1, 41))               # Reserved8 (EncryptionIVCount only from v10)
    open(utoc, "wb").write(raw)
    return bytes(raw)


@pytest.mark.parametrize("version", [2, 3])
def test_add_changes_only_documented_header_bytes_and_appends_tables(version, tmp_path):
    """Bit-level: after `add`, every byte of the 144-byte TOC header outside the documented count
    fields is identical (including reserved fields holding junk and an unknown flag bit), and the
    chunk-id, offset/length, compression-block and meta tables of the input are byte-identical
    PREFIXES of the output tables (v2/v3 have no perfect hash, so nothing is permuted)."""
    info = build_game_container(str(tmp_path / "in"), "efootball-16", version=version,
                                partition_size=160 * 1024 if version == 3 else None)
    before = _poison_reserved_header_fields(info["utoc"], version)
    name = "/Game/Face/r9/face_r9_2002"
    chunks, se, _ = _new_package_chunks(name)
    with iostore.IoStoreContainer(info["utoc"]) as c:
        assert c.toc.serialize() == before
        n, nb = len(c.toc.chunk_ids), len(c.toc.blocks)
        m = iostore.add_to_container(c, chunks, str(tmp_path / "out"),
                                     store_entries=[(zp.package_id_from_name(name), se)], verify=True)
    assert m["verify"]["ok"], m["verify"]["failures"]
    after = open(tmp_path / "out" / "pc3600_mobile_ios.utoc", "rb").read()
    allowed = set()
    for (a, b) in HEADER_FIELDS_ADD_MAY_CHANGE:
        allowed |= set(range(a, b))
    changed = {i for i in range(144) if before[i] != after[i]}
    assert changed <= allowed, sorted(changed - allowed)
    assert after[80] & 0x40 and after[17] == 0xA5 and after[104:144] == bytes(range(1, 41))
    t_in, t_out = iostore.TocResource.parse(before), iostore.TocResource.parse(after)
    h = iostore.find_container_header_index(t_in)      # the one replaced chunk: new offset/length/meta hash
    keep = [i for i in range(n) if i != h]
    assert [x.raw for x in t_out.chunk_ids[:n]] == [x.raw for x in t_in.chunk_ids]
    assert [t_out.offsets[i] for i in keep] == [t_in.offsets[i] for i in keep]
    assert [b.pack() for b in t_out.blocks[:nb]] == [b.pack() for b in t_in.blocks]
    assert [t_out.metas[i].pack() for i in keep] == [t_in.metas[i].pack() for i in keep]
    assert t_out.metas[h].flags == t_in.metas[h].flags & ~1 and t_out.offsets[h][0] >= nb * iostore.TocResource.parse(
        before).header.compression_block_size   # header chunk re-appended after all old blocks
    assert t_out.method_names_raw == t_in.method_names_raw


def test_add_refuses_store_entry_contradicting_its_package_header(tmp_path):
    info = build_game_container(str(tmp_path / "in"), "efootball-16")
    name = "/Game/Face/r9/face_r9_2002"
    pid_ = zp.package_id_from_name(name)
    with iostore.IoStoreContainer(info["utoc"]) as c:
        chunks, se, _ = _new_package_chunks(name)
        bad = iostore.StoreEntry(se.export_count + 1, se.export_bundle_count, se.imported_packages)
        with pytest.raises(iostore.IoStoreError, match="ExportCount/ExportBundleCount"):
            iostore.add_to_container(c, chunks, str(tmp_path / "o1"), store_entries=[(pid_, bad)])
        # chunk registered under one name, package header naming another
        data_other, entry_other = make_package("/Game/Face/r9/someone_else", OBJ, [FACE], seed=9)
        wrong = [iostore.NewChunk(iostore.ChunkId.from_parts(pid_, 0, 2), data_other, pkg_path(name))]
        with pytest.raises(iostore.IoStoreError, match="names '/Game/Face/r9/someone_else'"):
            iostore.add_to_container(c, wrong, str(tmp_path / "o2"), store_entries=[(pid_, se)])
        # graph data vs ImportedPackages mismatch is reported, not refused
        odd = iostore.StoreEntry(se.export_count, se.export_bundle_count, [zp.package_id_from_name(OTHER)])
        m = iostore.add_to_container(c, chunks, str(tmp_path / "o3"), store_entries=[(pid_, odd)], verify=True)
        assert m["verify"]["ok"] and any("graph data" in w for w in m["warnings"])
        chk = m["container_header"]["entry_vs_package_header"]
        assert chk["counts_rule_holds"] and chk["package_id_rule_holds"] and chk["measured_existing_packages"] == 6
    for d in ("o1", "o2"):
        assert not (tmp_path / d).exists() or not os.listdir(tmp_path / d)


def test_add_count_rule_only_warns_when_container_does_not_follow_it(tmp_path):
    """If the container's OWN packages do not follow ExportCount == header export count (unknown
    Konami semantics), a mismatch on a new entry is a warning, not a refusal."""
    content = {p["name"]: p for p in game_content()}
    cid = 0x77
    chunks, hp = [], []
    for n in (TEXP, OTHER):
        pid_ = zp.package_id_from_name(n)
        chunks.append(SChunk(make_chunk_id(pid_, 0, 2), content[n]["data"], pkg_path(n), "zlib"))
        hp.append((pid_, dict(content[n]["entry"], export_count=content[n]["entry"]["export_count"] + 5)))
    chunks.append(SChunk(make_chunk_id(cid, 0, 10), build_container_header(cid, hp, "efootball-16"), None, "zlib"))
    info = build_container(str(tmp_path / "in"), "odd", chunks, 2, container_id=cid)
    name = "/Game/Face/r9/face_r9_2002"
    new_chunks, se, _ = _new_package_chunks(name)
    se.export_count += 5
    with iostore.IoStoreContainer(info["utoc"]) as c:
        m = iostore.add_to_container(c, new_chunks, str(tmp_path / "o"), store_entries=[(zp.package_id_from_name(name), se)],
                                     verify=True)
    assert m["verify"]["ok"] and not m["container_header"]["entry_vs_package_header"]["counts_rule_holds"]
    assert any("rule not established" in w for w in m["warnings"])


def test_add_list_malformed_inputs_fail_cleanly(tmp_path):
    info = build_game_container(str(tmp_path / "in"), "efootball-16")
    (tmp_path / "x.bin").write_bytes(b"abc")
    cases = {
        "not_json.json": "{chunks: [",
        "no_file.json": json.dumps({"chunks": [{"package": "/Game/A/b"}]}),
        "bad_hex.json": json.dumps({"chunks": [{"file": "x.bin", "package_id": "0xZZ"}]}),
        "no_slash.json": json.dumps({"store_entries": [{"package": "/Game/A/b", "export_count": 1,
                                                        "export_bundle_count": 1, "imported_packages": ["Game/A/c"]}]}),
        "big_index.json": json.dumps({"chunks": [{"file": "x.bin", "package": "/Game/A/b", "chunk_index": 70000}]}),
        "imports_not_list.json": json.dumps({"store_entries": [{"package": "/Game/A/b", "export_count": 1,
                                                                "export_bundle_count": 1, "imported_packages": "/Game/A/c"}]}),
        "missing_count.json": json.dumps({"store_entries": [{"package": "/Game/A/b"}]}),
    }
    for fname, text in cases.items():
        (tmp_path / fname).write_text(text)
        r = run_cli("add", info["utoc"], "--out-dir", str(tmp_path / ("o_" + fname)), "--add-list",
                    str(tmp_path / fname), check=False)
        assert r.returncode == 2 and "Traceback" not in r.stderr and r.stderr.startswith("error:"), (fname, r.stderr)
        assert not (tmp_path / ("o_" + fname)).exists() or not os.listdir(tmp_path / ("o_" + fname))
    r = run_cli("find-refs", info["utoc"], "0xZZ", check=False)
    assert r.returncode == 2 and "Traceback" not in r.stderr


@pytest.mark.parametrize("header_type,expect", [(10, (False, True)), (6, (True, True)), (None, (True, False))])
def test_v4_chunk_type_naming_comes_from_the_container(header_type, expect, tmp_path):
    """TOC v4: retoc reads the UE4 enum, CUE4Parse decides by engine version. The container header
    chunk's own type settles it; without one, writing a type by NAME is refused."""
    from test_iostore import standard_chunks
    cid = 0xABCDEF0123
    chunks = standard_chunks()[:-1]
    if header_type is not None:
        chunks.append(SChunk(make_chunk_id(cid, 0, header_type), _payload(11, 777), None, "zlib"))
    info = build_container(str(tmp_path / "in"), "v4", chunks, 4, perfect_hash=True, meta_algo="blake3", container_id=cid)
    with iostore.IoStoreContainer(info["utoc"]) as c:
        nm = c.toc.chunk_type_naming()
        assert (nm["ue5"], nm["certain"]) == expect
    (tmp_path / "d.bin").write_bytes(b"payload")
    by_name = {"chunks": [{"file": "d.bin", "package": "/Game/R9/x", "type": "ExportBundleData", "path": "R9/x.uasset"}]}
    by_number = {"chunks": [{"file": "d.bin", "package": "/Game/R9/x", "type": 2, "path": "R9/x.uasset"}]}
    (tmp_path / "n.json").write_text(json.dumps(by_name))
    (tmp_path / "k.json").write_text(json.dumps(by_number))
    r = run_cli("add", info["utoc"], "--out-dir", str(tmp_path / "o1"), "--add-list", str(tmp_path / "n.json"),
                "--verify", check=False)
    if expect[1]:
        assert r.returncode == 0, r.stderr
        with iostore.IoStoreContainer(str(tmp_path / "o1" / "v4.utoc")) as o:
            t = o.toc.chunk_ids[iostore.resolve_selector(o, "R9/x.uasset")].type
        assert t == (1 if expect[0] else 2)
    else:
        assert r.returncode == 2 and "not certain" in r.stderr
        r = run_cli("add", info["utoc"], "--out-dir", str(tmp_path / "o2"), "--add-list", str(tmp_path / "k.json"),
                    "--verify")
        assert json.loads(r.stdout)["verify"] is True


def test_add_refuses_unparsed_toc_tail_and_source_hashes(tmp_path):
    from test_iostore import standard_chunks
    info = build_container(str(tmp_path / "in"), "tail", standard_chunks(), 2, meta_algo="sha1")
    with open(info["utoc"], "ab") as f:
        f.write(b"\x01\x02\x03\x04")
    nc = [iostore.NewChunk(iostore.ChunkId.from_parts(77, 0, 2), b"new", "eFootball/Content/n.uasset")]
    with iostore.IoStoreContainer(info["utoc"]) as c:
        assert c.toc.trailing == b"\x01\x02\x03\x04"
        with pytest.raises(iostore.IoStoreError, match="keep-toc-trailing"):
            iostore.add_to_container(c, nc, str(tmp_path / "o1"))
        m = iostore.add_to_container(c, nc, str(tmp_path / "o2"), allow_toc_trailing=True, verify=True)
        assert m["verify"]["ok"] and any("copied verbatim" in w for w in m["warnings"])
    v9 = build_container(str(tmp_path / "v9"), "v9", standard_chunks(), 9, meta_algo="blake3")
    raw = bytearray(open(v9["utoc"], "rb").read())
    raw[100:104] = struct.pack("<I", 3)                 # TocSourceHashCount
    open(v9["utoc"], "wb").write(raw)
    with iostore.IoStoreContainer(v9["utoc"]) as c:
        with pytest.raises(iostore.IoStoreError, match="source hashes"):
            iostore.add_to_container(c, nc, str(tmp_path / "o3"))
    assert not (tmp_path / "o1").exists() or not os.listdir(tmp_path / "o1")


def test_clone_refuses_new_id_already_in_a_rewire_container(tmp_path):
    tex_utoc, mesh_utoc = build_split_containers(str(tmp_path / "in"))
    t_new = "/Game/Face/pc3611/T_face_r9_2002"
    with iostore.IoStoreContainer(tex_utoc) as c:
        iostore.clone_packages(c, [(TEXP, t_new)], str(tmp_path / "t"))
    with iostore.IoStoreContainer(mesh_utoc) as c, \
            iostore.IoStoreContainer(str(tmp_path / "t" / "pc3611_mobile_ios.utoc")) as other:
        # cloning the material under the name of a package that lives in pc3611 would give two
        # packages with one FPackageId in the global package store
        with pytest.raises(iostore.IoStoreError, match="already has chunks in"):
            iostore.clone_packages(c, [(MI, TEXP)], str(tmp_path / "m"), rewire=[(TEXP, t_new)],
                                   rewire_containers=[other])


def test_perfect_hash_verify_is_not_vacuous(tmp_path, monkeypatch):
    """A wrong seed table written by the builder must be caught by --verify (and refused)."""
    from test_iostore import standard_chunks
    info = build_container(str(tmp_path / "in"), "ph", standard_chunks(), 5, perfect_hash=True, meta_algo="blake3")
    real = iostore.build_perfect_hash

    def broken(ids, allow_overflow, max_seed=1 << 20):
        slot_of, seeds, overflow = real(ids, allow_overflow, max_seed)
        k = next(i for i, s in enumerate(seeds) if s > 0)
        seeds[k] += 1                                    # one bucket now hashes to the wrong slots
        return slot_of, seeds, overflow
    monkeypatch.setattr(iostore, "build_perfect_hash", broken)
    nc = [iostore.NewChunk(iostore.ChunkId.from_parts(77, 0, 1), b"new", "eFootball/Content/n.uasset")]
    with iostore.IoStoreContainer(info["utoc"]) as c:
        with pytest.raises(iostore.IoStoreError, match="perfect hash"):
            iostore.add_to_container(c, nc, str(tmp_path / "o"), verify=True)


def test_append_keeps_noncanonical_empty_array_offsets():
    """Old entries with ArrayNum 0 but a non-zero offset (allowed by CArrayView) keep their bytes."""
    pkgs = [(10, {"export_count": 1, "export_bundle_count": 1, "imported": [20]}),
            (20, {"export_count": 2, "export_bundle_count": 1, "imported": []})]
    raw = build_container_header(5, pkgs, "efootball-16")
    ch = iostore.ContainerHeaderUE4.parse(raw)
    st = bytearray(ch.store_raw)
    struct.pack_into("<II", st, 16 + 8, 0, 1234)       # entry 1: empty array with a stray offset
    ch.store_raw = bytes(st)
    assert ch.check_layout("efootball-16")["empty_arrays_with_offset"] == 1
    ch2 = ch.copy()
    ch2.add_packages("efootball-16", [(30, iostore.StoreEntry(1, 1, [10, 20]))], "append")
    assert ch2.store_raw[16:32] == ch.store_raw[16:32]                     # untouched, offset not shifted
    assert struct.unpack_from("<II", ch2.store_raw, 8) == (1, struct.unpack_from("<II", ch.store_raw, 8)[1] + 16)
    assert [e.imported_packages for e in ch2.entries("efootball-16")] == [[20], [], [10, 20]]
    with pytest.raises(iostore.IoStoreError, match="canonical"):
        ch.copy().add_packages("efootball-16", [(30, iostore.StoreEntry(1, 1, []))], "sorted")
