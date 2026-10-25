"""Tests for tools/native/zen_package.py and tools/native/inplace_patch.py.

Everything here runs on SYNTHETIC data built from the published layouts (CUE4Parse / retoc), with
real ASTC payloads produced by tools/native/astc_tool.py (astcenc). Tests marked ``realdata`` use
the real UE4.27/UE5.x sample packages shipped with retoc (github.com/trumank/retoc, cloned under
/opt/tools/ref/retoc) and are skipped when that checkout is absent.
"""
import json
import os
import struct
import subprocess
import sys

import numpy as np
import pytest
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
NATIVE = os.path.normpath(os.path.join(HERE, "..", "native"))
sys.path.insert(0, NATIVE)

import inplace_patch as ip  # noqa: E402
import zen_package as zp  # noqa: E402

RETOC_TESTS = "/opt/tools/ref/retoc/retoc/tests"
realdata = pytest.mark.skipif(not os.path.isdir(RETOC_TESTS), reason="retoc sample packages not available")


def run_cli(mod, args):
    """Run a module's CLI in-process; returns None on success or the SystemExit message."""
    try:
        mod.main([str(a) for a in args])
    except SystemExit as e:
        if e.code in (0, None):
            return None
        return str(e.code)
    return None


def load_manifest(out_dir):
    with open(os.path.join(out_dir, "manifest.json"), encoding="utf-8") as f:
        return json.load(f)


# =============================================================================================
# CityHash64 / hashes
# =============================================================================================
# Golden values from google/cityhash v1.1.1 city.cc compiled with g++ (input byte i = (31*i+7) & 0xff).
CITY_GOLDEN = {0: 0x9ae16a3b2f90404f, 1: 0x57821efdee1b7472, 3: 0x5f043801775be181, 4: 0xdcf15d9b38b260fc,
               7: 0x545ac134b57925ff, 8: 0xd14693440d28f69a, 15: 0xc338033dfa6dca8d, 16: 0xd9b28ec31be83978,
               17: 0xffb7cd799a150d69, 31: 0x61a0625587879a75, 32: 0x5b0213e62a3ca399, 33: 0x37bd955848865317,
               63: 0x3e4dbdb0524cf872, 64: 0x98f31c3485efcafd, 65: 0xdfac987fbccbc482, 127: 0xb00bce0079122557,
               128: 0xd44aca4dd8ff4e3b, 129: 0x36353717055fa7b7, 200: 0xe7634271d2c2f86f, 1000: 0x13763b9280a42ca3}


def test_cityhash64_matches_reference():
    for n, h in CITY_GOLDEN.items():
        data = bytes(((i * 31 + 7) & 0xFF) for i in range(n))
        assert zp.cityhash64(data) == h, n
    assert zp.cityhash64(b"none") == 0x75370F5BA6352DC2
    assert zp.name_hash("None") == 0x75370F5BA6352DC2  # lower-cased before hashing


def test_script_import_index_matches_real_package_values():
    # Values read from the real UE4.27 package T_Emissive (retoc tests): class, template and the
    # /Script/Engine import. They must equal CityHash64(UTF-16LE lower path, '.'->'/') | ScriptImport.
    assert zp.script_import_index("/Script/Engine.Texture2D") == 0x5B93BCA796D1FA6F
    assert zp.script_import_index("/Script/Engine.Default__Texture2D") == 0x6BFAD34AC8B1F6D0
    assert zp.script_import_index("/Script/Engine") == 0x51ACCED3DC7C0922
    assert zp.obj_index_kind(0x5B93BCA796D1FA6F) == "ScriptImport"
    assert zp.builtin_script_objects()[0x5B93BCA796D1FA6F] == "/Script/Engine.Texture2D"


# =============================================================================================
# Zen package headers
# =============================================================================================
TEX2D = zp.script_import_index("/Script/Engine.Texture2D")
TEX2D_CDO = zp.script_import_index("/Script/Engine.Default__Texture2D")
SKMESH = zp.script_import_index("/Script/Engine.SkeletalMesh")


def _legacy_pkg(export_blobs=None, bundles=None):
    exports = [
        zp.ExportSpec("T_Face", export_blobs[0] if export_blobs else b"F" * 300, TEX2D, template_index=TEX2D_CDO,
                      global_import_index=zp.legacy_package_import_index("/Game/R9/T_Face.T_Face")),
        zp.ExportSpec("AssetImportData_0", b"I" * 41, zp.script_import_index("/Script/Engine.AssetImportData"),
                      outer_index=zp.make_obj_index("Export", 0), object_flags=0),
        zp.ExportSpec("SK_Head", b"M" * 77, SKMESH),
    ]
    return zp.build_legacy_package(
        "/Game/R9/T_Face", exports, imports=[TEX2D, TEX2D_CDO], bundles=bundles or [[1, 2], [0]],
        extra_names=["None", "PF_ASTC_4x4", "Größe_ß", "名字"],
        imported_packages=[(0x1122334455667788, [(0, 1), (1, 0)])]), exports


def test_legacy_package_parse_ranges_names_classes():
    data, exports = _legacy_pkg()
    pkg = zp.parse_package(data)
    assert pkg.layout == "ue4.26-4.27"
    assert "UE4.26 or UE4.27" in pkg.ue_version_guess
    assert pkg.package_name == "/Game/R9/T_Face"
    assert pkg.names[-1] == "名字" and "Größe_ß" in pkg.names
    assert [e.object_name for e in pkg.exports] == ["T_Face", "AssetImportData_0", "SK_Head"]
    assert pkg.exports[0].class_name == "/Script/Engine.Texture2D"
    assert pkg.exports[2].class_name == "/Script/Engine.SkeletalMesh"
    assert pkg.exports[1].class_name == "/Script/Engine.AssetImportData"
    # bundle order: [1, 2] then [0] -> export data laid out in that order after the graph data
    for spec, e in zip(exports, pkg.exports):
        assert data[e.abs_start:e.abs_end] == spec.data
    assert pkg.exports[1].abs_start == pkg.export_data_offset
    assert pkg.exports[0].abs_end == len(data)
    assert len(pkg.export_bundles) == 2
    assert pkg.imported_packages == [{"package_id": 0x1122334455667788, "arcs": [(0, 1), (1, 0)]}]
    # legacy (uasset+uexp) offsets: CookedSerialOffset; delta is constant per export
    assert pkg.exports[1].legacy_offset == pkg.summary["CookedHeaderSize"]
    assert all(e.legacy_to_chunk_delta == e.legacy_offset - e.abs_start for e in pkg.exports)
    assert not pkg.warnings
    assert pkg.find_export("t_face").index == 0 and pkg.find_export("2").object_name == "SK_Head"


def test_legacy_package_corruption_is_rejected():
    data, _ = _legacy_pkg()
    bad = bytearray(data)
    hashes_off = struct.unpack_from("<i", bad, 32)[0]
    struct.pack_into("<Q", bad, hashes_off, 0x12345678)  # wrong FName hash algorithm id
    with pytest.raises(zp.ZenFormatError):
        zp.parse_legacy(bytes(bad))
    with pytest.raises(zp.ZenFormatError):
        zp.parse_package(data[:200])
    with pytest.raises(zp.ZenFormatError):
        zp.parse_package(b"\0" * 64)


@pytest.mark.parametrize("layout", ["ue5.0-5.1", "ue5.2", "ue5.3", "ue5.4-5.5"])
def test_ue5_layout_detection(layout):
    exports = [zp.ExportSpec("T_Face", b"T" * 123, TEX2D, template_index=TEX2D_CDO),
               zp.ExportSpec("Other", b"O" * 10, zp.script_import_index("/Script/CoreUObject.Object"))]
    data = zp.build_ue5_package("/Game/R9/T_Face", exports, imports=[TEX2D, TEX2D_CDO], layout=layout,
                                imported_package_names=["/Game/Shared/M_Skin"] if layout >= "ue5.3" else [])
    pkg = zp.parse_package(data)
    assert pkg.layout == layout, pkg.evidence
    assert pkg.ue_version_guess.startswith("UE5")
    for spec, e in zip(exports, pkg.exports):
        assert data[e.abs_start:e.abs_end] == spec.data
    assert pkg.exports[0].class_name == "/Script/Engine.Texture2D"
    if layout >= "ue5.3":
        assert pkg.imported_package_names == ["/Game/Shared/M_Skin"]


def test_zen_cli_info_and_extract(tmp_path):
    data, exports = _legacy_pkg()
    pkg_path = tmp_path / "T_Face.uasset"
    pkg_path.write_bytes(data)
    rep = tmp_path / "info.json"
    assert run_cli(zp, ["info", pkg_path, "--json", rep]) is None
    doc = json.loads(rep.read_text())
    assert doc["layout"] == "ue4.26-4.27" and doc["exports"][0]["class"] == "/Script/Engine.Texture2D"
    assert doc["input"]["sha256"] == zp.sha256_bytes(data)
    out = tmp_path / "x"
    assert run_cli(zp, ["extract-export", pkg_path, "--export", "SK_Head", "--out-dir", out, "--verify"]) is None
    man = load_manifest(out)
    assert man["verify"]["reparsed_range_matches"] is True
    blob = open(man["outputs"]["export"]["path"], "rb").read()
    assert blob == exports[2].data
    # refuse to reuse a non-empty output dir and to overwrite the JSON report
    assert "not empty" in run_cli(zp, ["extract-export", pkg_path, "--export", "0", "--out-dir", out])
    assert "refusing" in run_cli(zp, ["info", pkg_path, "--json", rep])


@realdata
def test_real_ue427_packages_and_script_objects():
    t = RETOC_TESTS + "/UE4.27/"
    so = zp.load_script_objects_ue4(open(t + "LoaderGlobalNames_1.bin", "rb").read(),
                                    open(t + "LoaderInitialLoadMeta_1.bin", "rb").read(),
                                    open(t + "LoaderGlobalNameHashes_1.bin", "rb").read())
    assert len(so.paths) > 20000 and so.hash_mismatches == 0
    pkg = zp.parse_package(open(t + "T_Emissive.uasset", "rb").read(), so)
    assert pkg.layout == "ue4.26-4.27" and pkg.exports[0].class_name == "/Script/Engine.Texture2D"
    assert (pkg.exports[0].abs_start, pkg.exports[0].abs_end) == (532, 2342)
    # export ranges equal the legacy .uasset+.uexp bytes at CookedSerialOffset for all 220 exports
    zen = open(t + "TestModUI.uzenasset", "rb").read()
    leg = open(t + "TestModUI.uasset", "rb").read() + open(t + "TestModUI.uexp", "rb").read()
    pkg = zp.parse_package(zen, so)
    assert len(pkg.exports) == 220
    for e in pkg.exports:
        assert zen[e.abs_start:e.abs_end] == leg[e.legacy_offset:e.legacy_offset + e.cooked_serial_size]
    assert len(zp.parse_package(open(t + "SPR_UI_Battle.uasset", "rb").read()).export_bundles) == 2


@realdata
@pytest.mark.parametrize("name,layout", [("UE5.4/Randy", "ue5.4-5.5"), ("UE5.5/SM_Cube", "ue5.4-5.5"),
                                         ("UE5.6/SK_Mannequin", "ue5.6+"), ("UE5.6/BP_ThirdPersonCharacter", "ue5.6+")])
def test_real_ue5_packages(name, layout):
    zen = open(f"{RETOC_TESTS}/{name}.uzenasset", "rb").read()
    leg = open(f"{RETOC_TESTS}/{name}.uasset", "rb").read() + open(f"{RETOC_TESTS}/{name}.uexp", "rb").read()
    pkg = zp.parse_package(zen)
    assert pkg.layout == layout
    for e in pkg.exports:
        assert zen[e.abs_start:e.abs_end] == leg[e.legacy_offset:e.legacy_offset + e.cooked_serial_size]
    so = zp.load_script_objects_ue5(open(RETOC_TESTS + "/UE5.3/ScriptObjects.bin", "rb").read())
    assert so.hash_mismatches == 0


# =============================================================================================
# Textures
# =============================================================================================
ASTC_TOOL = os.path.join(NATIVE, "astc_tool.py")


def _make_image(path, seed, size=256):
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:size, 0:size]
    img = np.zeros((size, size, 4), np.uint8)
    img[..., 0] = (x * 255 // size + seed * 40) % 256
    img[..., 1] = (y * 255 // size) % 256
    img[..., 2] = ((x // 16 + y // 16) % 2) * 200 + rng.integers(0, 40, (size, size))
    img[..., 3] = 255
    Image.fromarray(img, "RGBA").save(path)


def _astc_encode(png, outdir, block="4x4"):
    subprocess.run([sys.executable, ASTC_TOOL, "encode", str(png), "--block", block, "--mips",
                    "--quality=-fast", "--outdir", str(outdir)], check=True, capture_output=True)
    stem = os.path.splitext(os.path.basename(png))[0]
    man = os.path.join(outdir, f"{stem}.astc{block}.json")
    return man, os.path.join(outdir, f"{stem}.astc{block}.bin")


def _mips_from_manifest(man_path, bin_path):
    man = json.load(open(man_path))
    blob = open(bin_path, "rb").read()
    return [(m["w"], m["h"], blob[m["offset"]:m["offset"] + m["size"]]) for m in man["mips"]]


def build_texture_export(names, fmt, mips, bulk_mips=(), legacy_base=1024, block=(4, 4)):
    """Cooked UTexture2D export body in the UE4.20-4.27 layout (CUE4Parse UTexture2D /
    UTexture.DeserializeCookedPlatformData / FTexturePlatformData / FTexture2DMipMap)."""
    b = bytearray()
    b += b"\x07\x00\x00\x00\x00\x00\x00\x00" * 3 + bytes(range(37))  # stand-in for tagged properties
    b += b"\x00\x00"                                   # FStripDataFlags
    b += struct.pack("<I", 1)                          # bool bCooked
    b += struct.pack("<II", names.index(fmt), 0)       # FName PixelFormat
    skip_pos = len(b)
    b += struct.pack("<q", 0)                          # int64 SkipOffset (patched below)
    pd_start = len(b)
    w0, h0 = mips[0][0], mips[0][1]
    b += struct.pack("<iiI", w0, h0, 1)                # SizeX, SizeY, PackedData (NumSlices=1)
    b += struct.pack("<i", len(fmt) + 1) + fmt.encode() + b"\0"
    b += struct.pack("<ii", 0, len(mips))              # FirstMipToSerialize, NumMips
    bulk = bytearray()
    expect = []
    for i, (w, h, payload) in enumerate(mips):
        b += struct.pack("<I", 1)                      # bool bCooked
        if i in bulk_mips:
            flags = 0x1 | 0x100 | 0x400 | 0x10000      # PayloadAtEndOfFile|InSeperateFile|NOT_Inline|NoOffsetFixUp
            b += struct.pack("<Iiiq", flags, len(payload), len(payload), len(bulk))
            expect.append(("BulkData", len(bulk)))
            bulk += payload
        else:
            flags = 0x48                               # SingleUse|ForceInlinePayload (as in real UE4.27 data)
            b += struct.pack("<Iiiq", flags, len(payload), len(payload), legacy_base + len(b) + 22)
            expect.append(("inline", len(b)))
            b += payload
        dw, dh = (max(w, block[0]), max(h, block[1])) if i >= 6 else (w, h)  # UE stores tiny mips block padded
        b += struct.pack("<iii", dw, dh, 1)
    b += struct.pack("<I", 0)                          # bIsVirtual
    pd_end = len(b)
    b += struct.pack("<II", names.index("None"), 0)    # terminating FName None
    struct.pack_into("<q", b, skip_pos, legacy_base + pd_end)
    return bytes(b), bytes(bulk), {"pd_start": pd_start, "pd_end": pd_end, "payloads": expect}


@pytest.fixture(scope="module")
def tex(tmp_path_factory):
    d = tmp_path_factory.mktemp("tex")
    for name, seed in (("donor", 1), ("r9", 2)):
        _make_image(d / f"{name}.png", seed)
    man_a, bin_a = _astc_encode(d / "donor.png", d / "enc_donor")
    man_b, bin_b = _astc_encode(d / "r9.png", d / "enc_r9")
    man_66, _ = _astc_encode(d / "r9.png", d / "enc_r9_66", "6x6")
    _make_image(d / "small.png", 3, 128)
    man_small, _ = _astc_encode(d / "small.png", d / "enc_small")
    mips = _mips_from_manifest(man_a, bin_a)
    assert len(mips) == 9 and mips[0][:2] == (256, 256) and mips[-1][:2] == (1, 1)
    names = ["/Game/R9/T_R9_Face", "T_R9_Face", "None", "PF_ASTC_4x4"]
    pkgs = {}
    for kind, bulk_mips in (("inline", ()), ("bulk", (0, 1, 2))):
        blob, bulk, exp = build_texture_export(names, "PF_ASTC_4x4", mips, bulk_mips, legacy_base=1024)
        pkg = zp.build_legacy_package("/Game/R9/T_R9_Face", [zp.ExportSpec("T_R9_Face", blob, TEX2D, template_index=TEX2D_CDO)],
                                      imports=[TEX2D, TEX2D_CDO], extra_names=names[2:], cooked_header_size=1024)
        p = d / kind / "T_R9_Face.uasset"
        p.parent.mkdir()
        p.write_bytes(pkg)
        bp = None
        if bulk:
            bp = d / kind / "T_R9_Face.ubulk"
            bp.write_bytes(bulk)
        pkgs[kind] = {"path": p, "bulk": bp, "exp": exp, "blob": blob}
    return {"dir": d, "man_a": man_a, "man_b": man_b, "bin_b": bin_b, "man_66": man_66,
            "man_small": man_small, "mips_a": mips, "mips_b": _mips_from_manifest(man_b, bin_b), "pkgs": pkgs}


def test_texture_scan_inline(tex):
    k = tex["pkgs"]["inline"]
    t = ip.load_target(str(k["path"]))
    pds = ip.scan_texture_platform_data(t)
    assert len(pds) == 1
    pd = pds[0]
    base = t.package.exports[0].abs_start
    assert pd.pixel_format == "PF_ASTC_4x4" and (pd.size_x, pd.size_y) == (256, 256)
    assert pd.variant == "ue4" and len(pd.mips) == 9 and pd.start == base + k["exp"]["pd_start"]
    assert pd.end == base + k["exp"]["pd_end"]
    assert pd.skip_offset["consistent"] is True
    for m, (w, h, payload), (loc, off) in zip(pd.mips, tex["mips_a"], k["exp"]["payloads"]):
        assert m.bulk.location == loc == "inline"
        assert m.bulk.payload_pos == base + off
        assert t.data[m.bulk.payload_pos:m.bulk.payload_pos + len(payload)] == payload
        assert m.expected_size == len(payload)
    assert [m.dims_rule for m in pd.mips][-3:] == ["exact", "block_padded", "block_padded"]  # 2x2, 1x1 stored as 4x4


def test_texture_scan_bulk_locations(tex, tmp_path):
    k = tex["pkgs"]["bulk"]
    rep = tmp_path / "scan.json"
    assert run_cli(ip, ["texture-scan", k["path"], "--zen-export", "T_R9_Face", "--json", rep]) is None
    doc = json.loads(rep.read_text())
    mips = doc["platform_data"][0]["mips"]
    assert [m["bulk"]["location"] for m in mips[:4]] == ["BulkData"] * 3 + ["inline"]
    assert [m["bulk"]["bulk_chunk_offset"] for m in mips[:3]] == [0, 65536, 65536 + 16384]
    assert mips[0]["bulk"]["offset_fixup_needed"] is False


def test_texture_replace_inline_verify(tex, tmp_path):
    k = tex["pkgs"]["inline"]
    out = tmp_path / "out"
    err = run_cli(ip, ["texture-replace", k["path"], "--astc-manifest", tex["man_b"], "--out-dir", out,
                       "--verify", "--decode-check"])
    assert err is None, err
    man = load_manifest(out)
    assert man["verify"]["ok"] is True
    assert man["inputs"]["package"]["sha256"] == zp.sha256_file(k["path"])
    assert len(man["changes"]) == 9 and all(c["file"] == "package" for c in man["changes"])
    new = open(man["outputs"]["package"]["path"], "rb").read()
    assert len(new) == os.path.getsize(k["path"]) and man["outputs"]["package"]["sha256"] == zp.sha256_bytes(new)
    pd = ip.scan_texture_platform_data(ip.load_target(man["outputs"]["package"]["path"]))[0]
    for m, (_, _, payload) in zip(pd.mips, tex["mips_b"]):
        assert new[m.bulk.payload_pos:m.bulk.payload_pos + m.bulk.size_on_disk] == payload
    # header fields (SkipOffset, bulk headers, dims) are untouched: still consistent after the patch
    assert pd.skip_offset["consistent"] is True
    # the input is unchanged
    assert zp.sha256_file(k["path"]) == man["inputs"]["package"]["sha256"]


def test_texture_replace_separate_bulk(tex, tmp_path):
    k = tex["pkgs"]["bulk"]
    out = tmp_path / "out"
    err = run_cli(ip, ["texture-replace", k["path"], "--astc-manifest", tex["man_b"], "--bulk", k["bulk"],
                       "--out-dir", out, "--verify"])
    assert err is None, err
    man = load_manifest(out)
    assert man["verify"]["ok"] is True
    assert sorted(man["outputs"]) == ["BulkData", "package"]
    bulk_new = open(man["outputs"]["BulkData"]["path"], "rb").read()
    assert bulk_new == b"".join(p for _, _, p in tex["mips_b"][:3])
    assert sum(c["file"] == "BulkData" for c in man["changes"]) == 3
    assert sum(c["file"] == "package" for c in man["changes"]) == 6


def test_texture_replace_refusals(tex, tmp_path):
    k = tex["pkgs"]["inline"]
    kb = tex["pkgs"]["bulk"]

    def replace(*extra, src=k["path"], man=tex["man_b"]):
        return run_cli(ip, ["texture-replace", src, "--astc-manifest", man, "--out-dir", tmp_path / f"o{len(os.listdir(tmp_path))}", *extra])
    assert "PF_ASTC_4x4 != manifest PF_ASTC_6x6" in replace(man=tex["man_66"])
    assert "texture size must not change" in replace(man=tex["man_small"])
    assert "no --bulk file" in replace(src=kb["path"])
    # tampered payload -> manifest hash mismatch
    bad = tmp_path / "tampered"
    bad.mkdir()
    man = json.load(open(tex["man_b"]))
    blob = bytearray(open(tex["bin_b"], "rb").read())
    blob[100] ^= 0xFF
    (bad / "r9.astc4x4.bin").write_bytes(blob)
    (bad / "r9.astc4x4.json").write_text(json.dumps(man))
    assert "sha256 does not match" in replace(man=bad / "r9.astc4x4.json")
    # non-empty output directory, and an output dir equal to an input path
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "x").write_text("x")
    assert "not empty" in run_cli(ip, ["texture-replace", k["path"], "--astc-manifest", tex["man_b"], "--out-dir", busy])
    assert "refusing" in run_cli(ip, ["texture-replace", k["path"], "--astc-manifest", tex["man_b"], "--out-dir", k["path"]])
    # strict dims: block-padded tiny mips are refused when exact dims are demanded
    assert "dims" in replace("--strict-dims")


def test_texture_extract_decode(tex, tmp_path):
    k = tex["pkgs"]["bulk"]
    out = tmp_path / "ex"
    assert run_cli(ip, ["texture-extract", k["path"], "--bulk", k["bulk"], "--out-dir", out, "--decode", "--verify"]) is None
    man = load_manifest(out)
    assert man["verify"]["payloads_match_source"] is True
    assert len([k for k in man["outputs"] if k.startswith("mip")]) == 9
    png = Image.open(man["outputs"]["mip0"]["png"]["path"])
    assert png.size == (256, 256)
    # without the bulk file the streamed mips are reported missing, not invented
    out2 = tmp_path / "ex2"
    assert run_cli(ip, ["texture-extract", k["path"], "--out-dir", out2]) is None
    assert [m["mip"] for m in load_manifest(out2)["missing"]] == [0, 1, 2]


def test_texture_identity_roundtrip(tex, tmp_path):
    """extract the donor's own mips, feed them back: output must equal the input byte for byte."""
    k = tex["pkgs"]["bulk"]
    ex = tmp_path / "ex"
    assert run_cli(ip, ["texture-extract", k["path"], "--bulk", k["bulk"], "--out-dir", ex]) is None
    man_ex = load_manifest(ex)
    out = tmp_path / "id"
    err = run_cli(ip, ["texture-replace", k["path"], "--bulk", k["bulk"], "--astc-manifest",
                       man_ex["outputs"]["astc_manifest"]["path"], "--out-dir", out, "--verify", "--strict-dims"])
    assert err is None, err
    man = load_manifest(out)
    assert man["outputs"]["package"]["sha256"] == man["inputs"]["package"]["sha256"]
    assert man["outputs"]["BulkData"]["sha256"] == man["inputs"]["BulkData"]["sha256"]
    assert all(c["identical"] for c in man["changes"]) and len(man["changes"]) == 9


def test_texture_scan_raw_export_blob_ue5_layout(tex):
    """A raw export blob (no Zen header) in the UE5.0/5.1 mip layout (no per-mip bCooked)."""
    mips = tex["mips_a"]
    b = bytearray(b"\x01" * 50)
    b += struct.pack("<iiI", 256, 256, 1) + struct.pack("<i", 12) + b"PF_ASTC_4x4\0" + struct.pack("<ii", 0, len(mips))
    for i, (w, h, p) in enumerate(mips):
        b += struct.pack("<Iiiq", 0x48, len(p), len(p), 0) + p
        b += struct.pack("<iii", max(w, 4) if i >= 6 else w, max(h, 4) if i >= 6 else h, 1)
    b += struct.pack("<I", 0) + b"\x02" * 30
    t = ip.Target("blob", bytes(b), 0, len(b))
    pds = ip.scan_texture_platform_data(t)
    assert len(pds) == 1 and pds[0].variant == "ue5" and len(pds[0].mips) == 9


@realdata
def test_real_texture_scan_ue427_and_ue5_bulk(tmp_path):
    t = ip.load_target(RETOC_TESTS + "/UE4.27/T_Emissive.uasset")
    pd = ip.scan_texture_platform_data(t)[0]
    assert (pd.pixel_format, pd.size_x, len(pd.mips), pd.variant) == ("PF_DXT5", 32, 6, "ue4")
    assert pd.skip_offset["consistent"] is True
    assert [m.dims_rule for m in pd.mips] == ["exact"] * 4 + ["block_padded"] * 2
    pkg = RETOC_TESTS + "/UE5.5/T_Test.uzenasset"
    pd = ip.scan_texture_platform_data(ip.load_target(pkg))[0]
    assert pd.variant == "ue5-data-resources" and pd.skip_offset["consistent"] is True
    out = tmp_path / "ex"
    assert run_cli(ip, ["texture-extract", pkg, "--bulk", RETOC_TESTS + "/UE5.5/T_Test.ubulk", "--out-dir", out, "--verify"]) is None
    man = load_manifest(out)
    assert "missing" not in man and man["outputs"]["mip0"]["size"] == 2048 * 2048 // 2


# =============================================================================================
# Meshes
# =============================================================================================
def make_head(nlat, nlon, radius=12.0, center=(2.0, -1.0, 160.0), bump=0.0):
    """Lat-long 'head' in UE space (cm, Z up) with UV seams and pole duplicates, plus an
    asymmetric bump so no axis permutation maps it onto itself."""
    verts, uv0, uv1, nrm, tan = [], [], [], [], []
    for i in range(nlat + 1):
        th = np.pi * i / nlat
        for j in range(nlon + 1):
            ph = 2 * np.pi * j / nlon
            d = np.array([np.sin(th) * np.cos(ph), np.sin(th) * np.sin(ph), np.cos(th)])
            r = radius * (1 + 0.15 * max(0.0, d[0]) ** 4 + 0.05 * d[1] * (d[2] > 0) + bump)
            verts.append(np.array(center) + r * d * np.array([1.0, 0.85, 1.25]))
            nrm.append(d)
            tan.append(np.array([-np.sin(ph), np.cos(ph), 0.0]))
            uv0.append([j / nlon, i / nlat])
            uv1.append([0.25 + 0.5 * j / nlon, 0.5 * i / nlat])
    faces = []
    w = nlon + 1
    for i in range(nlat):
        for j in range(nlon):
            a, b, c, e = i * w + j, i * w + j + 1, (i + 1) * w + j, (i + 1) * w + j + 1
            faces += [[a, c, b], [b, c, e]]
    P = np.array(verts, np.float32).astype(np.float64)
    return {"P": P, "N": np.array(nrm), "T": np.array(tan), "UV0": np.array(uv0), "UV1": np.array(uv1),
            "F": np.array(faces, np.int64)}


def pack_lod(m, high_precision=False, full_uv=False):
    """FPositionVertexBuffer + FStaticMeshVertexBuffer bytes (UE4.19+ cooked layout)."""
    n = len(m["P"])
    out = bytearray(struct.pack("<iiii", 12, n, 12, n)) + m["P"].astype("<f4").tobytes()
    pos_off = 16
    out += b"\x00\x00" + struct.pack("<iiII", 2, n, int(full_uv), int(high_precision))
    enc = "int16" if high_precision else "int8"
    stride = 16 if high_precision else 8
    out += struct.pack("<ii", stride, n)
    tan_off = len(out)
    for t, nn in zip(m["T"], m["N"]):
        out += ip._encode_packed(np.append(t, 0.0), enc) + ip._encode_packed(np.append(nn, 1.0), enc)
    el = 8 if full_uv else 4
    out += struct.pack("<ii", el, 2 * n)
    uv_off = len(out)
    uvs = np.stack([m["UV0"], m["UV1"]], axis=1).reshape(-1, 2)
    out += uvs.astype("<f4" if full_uv else "<f2").tobytes()
    return bytes(out), {"pos": pos_off, "tan": tan_off, "uv": uv_off, "tan_stride": stride, "uv_stride": 2 * el}


def build_mesh_blob(lod0, lod1, seed=7):
    """Fake cooked SkeletalMesh export: junk, ImportedBounds, junk, LOD0 buffers, skin-weight-like
    junk, LOD1 buffers, junk."""
    rng = np.random.default_rng(seed)
    P = lod0["P"]
    mn, mx = P.min(0), P.max(0)
    origin = (mn + mx) / 2
    extent = (mx - mn) / 2 + 0.25
    radius = float(np.max(np.linalg.norm(P - origin, axis=1))) + 0.25
    b = bytearray(rng.integers(0, 256, 333, dtype=np.uint8).tobytes())
    b += b"\x00\x00"
    bounds_off = len(b)
    b += struct.pack("<7f", *origin, *extent, radius)
    b += rng.integers(0, 256, 517, dtype=np.uint8).tobytes()
    l0, o0 = pack_lod(lod0)
    base0 = len(b)
    b += l0
    b += rng.integers(0, 256, 8 * len(P), dtype=np.uint8).tobytes()  # skin weights stand-in
    l1, o1 = pack_lod(lod1, high_precision=True, full_uv=True)
    base1 = len(b)
    b += l1
    b += rng.integers(0, 256, 211, dtype=np.uint8).tobytes()
    off0 = {k: (v + base0 if k in ("pos", "tan", "uv") else v) for k, v in o0.items()}
    off1 = {k: (v + base1 if k in ("pos", "tan", "uv") else v) for k, v in o1.items()}
    return bytes(b), {"bounds": bounds_off, "lod0": off0, "lod1": off1}


def decode_lod(blob, off, n, enc):
    """What FModel/umodel would read back from the buffers (float32 positions, packed normals, UVs)."""
    P = ip.read_positions(blob, off["pos"], 12, n)
    nz = ip._decode_packed(blob, off["tan"] + np.arange(n) * off["tan_stride"] + off["tan_stride"] // 2, enc)[:, :3]
    nz /= np.linalg.norm(nz, axis=1, keepdims=True)
    prec = "half" if off["uv_stride"] == 8 else "float"
    el = off["uv_stride"] // 2
    uv0 = ip._decode_uv(blob, off["uv"] + np.arange(n) * off["uv_stride"], prec)
    uv1 = ip._decode_uv(blob, off["uv"] + el + np.arange(n) * off["uv_stride"], prec)
    return P, nz, uv0, uv1


def write_glb(path, P, N, uvs, F):
    binb = bytearray()
    views, accs = [], []

    def add(arr, comp, typ, target=None):
        arr = np.ascontiguousarray(arr)
        off = len(binb)
        binb.extend(arr.tobytes())
        binb.extend(b"\0" * ((-len(binb)) % 4))
        views.append({"buffer": 0, "byteOffset": off, "byteLength": arr.nbytes})
        acc = {"bufferView": len(views) - 1, "componentType": comp, "count": len(arr), "type": typ}
        if typ == "VEC3" and comp == 5126:
            acc["min"], acc["max"] = arr.min(0).tolist(), arr.max(0).tolist()
        accs.append(acc)
        return len(accs) - 1
    attrs = {"POSITION": add(P.astype("<f4"), 5126, "VEC3"), "NORMAL": add(N.astype("<f4"), 5126, "VEC3")}
    for i, uv in enumerate(uvs):
        attrs[f"TEXCOORD_{i}"] = add(uv.astype("<f4"), 5126, "VEC2")
    idx = add(F.astype("<u4").ravel(), 5125, "SCALAR")
    doc = {"asset": {"version": "2.0"}, "buffers": [{"byteLength": len(binb)}], "bufferViews": views,
           "accessors": accs, "meshes": [{"primitives": [{"attributes": attrs, "indices": idx, "mode": 4}]}],
           "nodes": [{"mesh": 0}], "scenes": [{"nodes": [0]}], "scene": 0}
    js = json.dumps(doc).encode()
    js += b" " * ((-len(js)) % 4)
    total = 12 + 8 + len(js) + 8 + len(binb)
    with open(path, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, total))
        f.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
        f.write(struct.pack("<II", len(binb), 0x004E4942) + bytes(binb))


def fmodel_gltf(P, N, uv0, uv1, F):
    """Emulate CUE4Parse-Conversion Gltf.cs: pos = SwapYZ(pos * 0.01f), normal = SwapYZ(n), and
    vertices re-ordered by first use in the index buffer (SharpGLTF MeshBuilder)."""
    order, remap = [], {}
    newF = np.empty_like(F)
    for fi, tri in enumerate(F):
        for k, v in enumerate(tri):
            if v not in remap:
                remap[v] = len(order)
                order.append(v)
            newF[fi, k] = remap[v]
    order = np.array(order)
    Pg = (P.astype(np.float32) * np.float32(0.01)).astype(np.float32)[:, [0, 2, 1]]
    return Pg[order], N[order][:, [0, 2, 1]], [uv0[order], uv1[order]], newF, order


def write_psk(path, P, N, uv0, F):
    """ActorX PSK like CUE4Parse-Conversion ActorXMesh.cs: Y mirrored, one point per vertex,
    wedges in buffer order, first two face indices swapped."""
    def chunk(name, size, count, payload):
        return name.encode().ljust(20, b"\0") + struct.pack("<iii", 0, size, count) + payload
    n = len(P)
    pts = P.astype(np.float32).copy()
    pts[:, 1] *= -1
    wed = b"".join(struct.pack("<iffBBh", i, uv0[i, 0], uv0[i, 1], 0, 0, 0) for i in range(n))
    faces = b"".join(struct.pack("<HHHBBi", f[1], f[0], f[2], 0, 0, 1) for f in F)
    nn = N.astype(np.float32).copy()
    nn[:, 1] *= -1
    data = (chunk("ACTRHEAD", 0, 0, b"") + chunk("PNTS0000", 12, n, pts.tobytes()) + chunk("VTXW0000", 16, n, wed) +
            chunk("FACE0000", 12, len(F), faces) + chunk("VTXNORMS", 12, n, nn.tobytes()))
    open(path, "wb").write(data)


def write_obj(path, P, N, uv0, F):
    """Y-up metres with OBJ's flipped V: v = (X, Z, -Y) * 0.01, vt = (u, 1 - v)."""
    with open(path, "w") as f:
        for p in P:
            f.write(f"v {p[0] * 0.01:.8f} {p[2] * 0.01:.8f} {-p[1] * 0.01:.8f}\n")
        for t in uv0:
            f.write(f"vt {t[0]:.6f} {1 - t[1]:.6f}\n")
        for nn in N:
            f.write(f"vn {nn[0]:.6f} {nn[2]:.6f} {-nn[1]:.6f}\n")
        for tri in F:
            f.write("f " + " ".join(f"{v + 1}/{v + 1}/{v + 1}" for v in tri) + "\n")


@pytest.fixture(scope="module")
def mesh(tmp_path_factory):
    d = tmp_path_factory.mktemp("mesh")
    lod0 = make_head(16, 24)
    lod1 = make_head(8, 12)
    blob, offs = build_mesh_blob(lod0, lod1)
    (d / "SK_Head.export.bin").write_bytes(blob)
    res = {"dir": d, "blob": blob, "offs": offs, "lod0": lod0, "lod1": lod1, "input": d / "SK_Head.export.bin"}
    for lod, enc in (("lod0", "int8"), ("lod1", "int16")):
        m = res[lod]
        P, N, uv0, uv1 = decode_lod(blob, offs[lod], len(m["P"]), enc)
        Pg, Ng, uvg, Fg, order = fmodel_gltf(P, N, uv0, uv1, m["F"])
        write_glb(d / f"{lod}.glb", Pg, Ng, uvg, Fg)
        res[f"{lod}_gltf"] = {"P": Pg, "N": Ng, "UV": uvg, "F": Fg, "order": order}
        write_psk(d / f"{lod}.psk", P, N, uv0, m["F"])
        write_obj(d / f"{lod}.obj", P, N, uv0, m["F"])
    return res


def test_locate_glb_fmodel_convention(mesh):
    ref = ip.load_ref_mesh(str(mesh["dir"] / "lod0.glb"))
    report, bounds = ip.locate_all(mesh["blob"], ref)
    assert len(report) == 1
    e = report[0]
    pm = e["positions"]
    o = mesh["offs"]["lod0"]
    assert (pm.offset, pm.stride, pm.count) == (o["pos"], 12, len(mesh["lod0"]["P"]))
    assert pm.mode == "set" and pm.header and pm.header["matches_run"]
    assert np.allclose(pm.A, np.array([[1, 0, 0], [0, 0, 1], [0, 1, 0]]) * 100)
    assert "glTF from FModel" in ip.describe_transform(pm.A, pm.t)
    assert not pm.symmetry_tie
    # mapping: UE vertex j -> glTF vertex order.index(j); seams/poles resolved via UV + normals
    order = mesh["lod0_gltf"]["order"]
    expected = np.empty(len(order), np.int64)
    expected[order] = np.arange(len(order))
    assert e["ambiguous_after_refine"] == 0
    assert np.array_equal(pm.mapping, expected)
    tm = e["tangents"][0]
    assert (tm.offset, tm.stride, tm.encoding) == (o["tan"], 8, "int8") and tm.header
    uv = [u for u in e["uvs"] if u.header]
    assert {(u.offset, u.ref_channel, u.precision, u.stride, u.flip_v) for u in uv} == {
        (o["uv"], 0, "half", 8, False), (o["uv"] + 4, 1, "half", 8, False)}
    assert pm.vb_meta["num_tex_coords"] == 2 and pm.vb_meta["high_precision_tangents"] is False
    assert bounds and bounds[0].offset == mesh["offs"]["bounds"]


def test_locate_psk_obj_and_lod1(mesh):
    o0, o1 = mesh["offs"]["lod0"], mesh["offs"]["lod1"]
    psk = ip.load_ref_mesh(str(mesh["dir"] / "lod0.psk"))
    report, _ = ip.locate_all(mesh["blob"], psk, want_bounds=False)
    pm = report[0]["positions"]
    assert pm.offset == o0["pos"] and np.array_equal(pm.mapping, np.arange(pm.count))
    assert "PSK/ActorX (Y mirrored)" in ip.describe_transform(pm.A, pm.t)
    obj = ip.load_ref_mesh(str(mesh["dir"] / "lod0.obj"))
    report, _ = ip.locate_all(mesh["blob"], obj, want_bounds=False)
    assert report[0]["positions"].offset == o0["pos"]
    assert any(u.flip_v and u.offset == o0["uv"] for u in report[0]["uvs"])
    # LOD1 is a different stream (own buffers, high precision tangents, float UVs)
    l1 = ip.load_ref_mesh(str(mesh["dir"] / "lod1.glb"))
    report, _ = ip.locate_all(mesh["blob"], l1, want_bounds=False)
    assert len(report) == 1 and report[0]["positions"].offset == o1["pos"]
    tm = report[0]["tangents"][0]
    assert (tm.offset, tm.stride, tm.encoding) == (o1["tan"], 16, "int16")
    assert any(u.offset == o1["uv"] and u.precision == "float" and u.header for u in report[0]["uvs"])


def test_locate_ordered_fallback_arbitrary_rotation(mesh):
    """A reference in buffer order but rotated by a non-axis-aligned rotation (set mode cannot see
    it): the ordered similarity search must find it and fit the transform."""
    P = mesh["lod0"]["P"]
    ang = np.radians(33.0)
    Rz = np.array([[np.cos(ang), -np.sin(ang), 0], [np.sin(ang), np.cos(ang), 0], [0, 0, 1]])
    ref = ip.RefMesh((P @ Rz) * 0.01, None, [], None, "rotated")
    pms = ip.locate_positions(mesh["blob"], ref)
    assert len(pms) == 1 and pms[0].mode == "ordered" and pms[0].offset == mesh["offs"]["lod0"]["pos"]
    assert np.allclose(pms[0].A, np.linalg.inv(Rz) * 100, atol=1e-3)


def test_mesh_locate_cli_json(mesh, tmp_path):
    rep = tmp_path / "loc.json"
    assert run_cli(ip, ["mesh-locate", mesh["input"], "--ref", mesh["dir"] / "lod0.glb", "--json", rep]) is None
    doc = json.loads(rep.read_text())
    s = doc["streams"][0]
    assert s["positions"]["offset"] == mesh["offs"]["lod0"]["pos"] and s["tangents"][0]["end"] > s["tangents"][0]["offset"]
    assert "LOD0" in doc["lod_note"] and doc["bounds_candidates"][0]["offset"] == mesh["offs"]["bounds"]


def _edited_glb(mesh, lod, path, amount=1.5, extra_vertex=False, shuffle_faces=False):
    g = mesh[f"{lod}_gltf"]
    P = g["P"].astype(np.float64).copy()
    # glTF space: x = UE X (forward); push the 'nose' region outwards by `amount` cm
    c = P.mean(0)
    d = P - c
    wgt = np.clip((d[:, 0] / np.abs(d[:, 0]).max() - 0.6) / 0.4, 0, 1)
    P[:, 0] += amount * 0.01 * wgt
    F = g["F"].copy()
    N, UV = g["N"], g["UV"]
    if extra_vertex:
        P = np.vstack([P, P[:1]])
        N = np.vstack([N, N[:1]])
        UV = [np.vstack([u, u[:1]]) for u in UV]
    if shuffle_faces:
        F = F[::-1].copy()
    P = P.astype(np.float32)
    write_glb(path, P, N, UV, F)
    return P.astype(np.float64), wgt > 0


def test_patch_positions_two_lods_tangents_bounds_verify(mesh, tmp_path):
    d = tmp_path
    p0, moved0 = _edited_glb(mesh, "lod0", d / "new0.glb", amount=1.5)
    p1, moved1 = _edited_glb(mesh, "lod1", d / "new1.glb", amount=1.5)
    out = d / "out"
    err = run_cli(ip, ["mesh-patch-positions", mesh["input"], "--pair", mesh["dir"] / "lod0.glb", d / "new0.glb",
                       "--pair", mesh["dir"] / "lod1.glb", d / "new1.glb", "--tangents", "--bounds", "union",
                       "--out-dir", out, "--verify"])
    assert err is None, err
    man = load_manifest(out)
    assert man["verify"]["ok"] is True, man["verify"]
    assert man["inputs"]["input"]["sha256"] == zp.sha256_bytes(mesh["blob"])
    new = open(man["outputs"]["input"]["path"], "rb").read()
    assert len(new) == len(mesh["blob"])
    o0, o1 = mesh["offs"]["lod0"], mesh["offs"]["lod1"]
    order0 = mesh["lod0_gltf"]["order"]
    got0 = ip.read_positions(new, o0["pos"], 12, len(order0))
    old0 = ip.read_positions(mesh["blob"], o0["pos"], 12, len(order0))
    # UE vertex order[k] corresponds to glTF vertex k: X = x*100, Y = z*100, Z = y*100
    exp0 = (p0[:, [0, 2, 1]] * 100).astype(np.float32)
    moved_ue = np.zeros(len(order0), bool)
    moved_ue[order0[moved0]] = True
    assert np.array_equal(got0[order0[moved0]].astype(np.float32), exp0[moved0])
    # untouched vertices keep their exact original bytes (no float round-trip noise)
    assert np.array_equal(got0[~moved_ue], old0[~moved_ue])
    assert man["pairs"][0]["moved_vertices"] == int(moved0.sum()) and man["pairs"][1]["moved_vertices"] == int(moved1.sum())
    # tangent frames: normals changed only around the edit, all still unit length
    n_old = ip._decode_packed(mesh["blob"], o0["tan"] + 4 + np.arange(len(order0)) * 8, "int8")[:, :3]
    n_new = ip._decode_packed(new, o0["tan"] + 4 + np.arange(len(order0)) * 8, "int8")[:, :3]
    changed_n = np.any(np.abs(n_new - n_old) > 1e-9, axis=1)
    assert changed_n.any() and not changed_n[~moved_ue].all()
    assert man["pairs"][0]["tangents"]["computed_normal_sign"] in (1.0, -1.0)
    # the updated normals follow the edited surface better than the old ones did
    ref0 = ip.load_ref_mesh(str(mesh["dir"] / "lod0.glb"))
    newm = ip.load_ref_mesh(str(d / "new0.glb"))
    groups = ip.weld_groups_by_position(ref0.positions)
    surf = ip.vertex_normals(newm.positions, newm.faces, groups)[:, [0, 2, 1]]  # glTF -> UE axes
    surf_ue = np.empty_like(surf)
    surf_ue[order0] = surf
    surf_ue *= np.sign(np.mean(np.sum(surf_ue * n_old, axis=1)))
    unit = lambda v: v / np.linalg.norm(v, axis=1, keepdims=True)  # noqa: E731
    err_before = np.sum(unit(n_old) * surf_ue, axis=1)[changed_n]
    err_after = np.sum(unit(n_new) * surf_ue, axis=1)[changed_n]
    assert err_after.mean() > err_before.mean()
    # bounds were enlarged to contain the pushed-out vertices
    b = man["bounds"][0]
    assert b["before"]["offset"] == mesh["offs"]["bounds"]
    assert b["after"]["origin"][0] + b["after"]["box_extent"][0] >= got0[:, 0].max() - 1e-3
    # LOD1 was patched in the same run, with its own (int16) tangent stream
    assert man["pairs"][1]["positions"]["offset"] == o1["pos"] and man["pairs"][1]["tangents"]["encoding"] == "int16"
    # every changed byte range is recorded; nothing else differs
    assert ip.check_untouched(mesh["blob"], new, man["changes"])
    assert {c["what"].split(" (")[0] for c in man["changes"]} >= {"pair 0 positions", "pair 0 tangents",
                                                                   "pair 1 positions", "pair 1 tangents"}


def test_mesh_identity_patch_changes_nothing(mesh, tmp_path):
    out = tmp_path / "id"
    ref0 = mesh["dir"] / "lod0.glb"
    err = run_cli(ip, ["mesh-patch-positions", mesh["input"], "--pair", ref0, ref0, "--tangents",
                       "--bounds", "union", "--out-dir", out, "--verify"])
    assert err is None, err
    man = load_manifest(out)
    assert man["outputs"]["input"]["sha256"] == man["inputs"]["input"]["sha256"]
    assert man["pairs"][0]["moved_vertices"] == 0 and all(c["identical"] for c in man["changes"])


def test_patch_positions_refusals(mesh, tmp_path):
    d = tmp_path
    ref0 = mesh["dir"] / "lod0.glb"

    def patch(new, *extra, ref=ref0):
        return run_cli(ip, ["mesh-patch-positions", mesh["input"], "--pair", ref, new, "--out-dir",
                            d / f"o{len(os.listdir(d))}", *extra])
    _edited_glb(mesh, "lod0", d / "plus1.glb", extra_vertex=True)
    assert "vertex count and order must be identical" in patch(d / "plus1.glb")
    _edited_glb(mesh, "lod0", d / "shuf.glb", shuffle_faces=True)
    assert "face lists differ" in patch(d / "shuf.glb")
    # LOD1 edit against the LOD0 reference: counts differ -> refused
    _edited_glb(mesh, "lod1", d / "l1.glb")
    assert "refusing" in patch(d / "l1.glb")
    # a reference with an exact duplicate vertex whose copies move differently is ambiguous
    g = mesh["lod0_gltf"]
    Pd = np.vstack([g["P"], g["P"][5:6]]).astype(np.float32)
    Nd = np.vstack([g["N"], g["N"][5:6]])
    UVd = [np.vstack([u, u[5:6]]) for u in g["UV"]]
    write_glb(d / "dup_ref.glb", Pd, Nd, UVd, g["F"])
    Pn = Pd.copy()
    Pn[-1, 0] += 0.02
    write_glb(d / "dup_new.glb", Pn, Nd, UVd, g["F"])
    assert "ambiguous" in patch(d / "dup_new.glb", ref=d / "dup_ref.glb")
    assert patch(d / "dup_new.glb", "--allow-ambiguous", ref=d / "dup_ref.glb") is None
    # a reference that is not in the file
    other = make_head(10, 10, radius=30.0, center=(50, 50, 50))
    write_glb(d / "other.glb", (other["P"] * 0.01)[:, [0, 2, 1]].astype(np.float32), other["N"], [other["UV0"]], other["F"])
    assert "not found" in patch(d / "other.glb", ref=d / "other.glb")
    # output directory must be new/empty
    busy = d / "busy"
    busy.mkdir()
    (busy / "f").write_text("x")
    assert "not empty" in run_cli(ip, ["mesh-patch-positions", mesh["input"], "--pair", ref0, ref0, "--out-dir", busy])


def test_symmetric_mesh_tie_is_flagged_and_refused(tmp_path):
    """A mirror-symmetric mesh with mirror-overlapping UVs and no order information: the mirrored
    axis mapping fits equally well, so the locator flags it and patching refuses."""
    m = make_head(10, 16, center=(2.0, 0.0, 160.0))
    # exactly symmetric about the plane Y = 0 (a character's midline): keep y >= 0, mirror it
    P = m["P"].copy()
    P[:, 1] = np.abs(P[:, 1])
    P = np.vstack([P, P * np.array([1, -1, 1])])
    UV = np.vstack([m["UV0"], m["UV0"]])  # mirrored half re-uses the same UVs (overlapping islands)
    blob = bytearray(b"\x11" * 64) + struct.pack("<iiii", 12, len(P), 12, len(P)) + P.astype("<f4").tobytes() + b"\x22" * 64
    rng = np.random.default_rng(0)
    perm = rng.permutation(len(P))
    ref = ip.RefMesh((P[perm] * 0.01)[:, [0, 2, 1]], None, [UV[perm]], None, "sym")
    pms = ip.locate_positions(bytes(blob), ref)
    assert len(pms) == 1 and pms[0].symmetry_tie and len(pms[0].alternatives) >= 2
    entry = {"positions": pms[0], "tangents": [], "uvs": [], "ambiguous_after_refine": 0}
    with pytest.raises(SystemExit, match="symmetric"):
        ip.plan_position_patch(bytes(blob), ref, ref, entry)


@realdata
def test_real_ue55_static_mesh_streams():
    """Real UE5.5 SM_Cube: build an FModel-style reference from the cooked buffers (positions,
    TangentZ, UVs), shuffle its vertex order, and locate all streams at their real offsets."""
    data = open(RETOC_TESTS + "/UE5.5/SM_Cube.uzenasset", "rb").read()
    pos = 3958 + 16
    n = 24
    meta = ip._read_vb_meta(data, pos + 12 * n, n)
    assert meta is not None
    enc = "int16" if meta["high_precision_tangents"] else "int8"
    st = 16 if enc == "int16" else 8
    tan = meta["tangents_offset"]
    P = ip.read_positions(data, pos, 12, n)
    nz = ip._decode_packed(data, tan + np.arange(n) * st + st // 2, enc)[:, :3]
    uv_hdr = tan + st * n
    el, cnt = struct.unpack_from("<ii", data, uv_hdr)
    prec = "half" if el == 4 else "float"
    ntc = cnt // n
    uv0 = ip._decode_uv(data, uv_hdr + 8 + np.arange(n) * el * ntc, prec)
    perm = np.random.default_rng(1).permutation(n)
    ref = ip.RefMesh((P[perm] * 0.01)[:, [0, 2, 1]], (nz / np.linalg.norm(nz, axis=1, keepdims=True))[perm][:, [0, 2, 1]],
                     [uv0[perm]], None, "SM_Cube-fmodel-style")
    t = ip.load_target(RETOC_TESTS + "/UE5.5/SM_Cube.uzenasset")
    report, _ = ip.locate_all(t.data, ref, want_bounds=False)
    # the cube vertices also occur in BodySetup_0 (collision data, no FPositionVertexBuffer header)
    # and as 8 unique corners: those are reported but only the header-confirmed stream is auto-chosen
    e = report[0]
    assert e["positions"].offset == pos and e["positions"].header and e["positions"].primary
    assert [r for r in report if r["positions"].primary and r["positions"].header] == [e]
    body = ip.load_target(RETOC_TESTS + "/UE5.5/SM_Cube.uzenasset").package.find_export("BodySetup_0")
    assert all(body.abs_start <= r["positions"].offset < body.abs_end for r in report[1:])
    assert ip._choose_stream(report, None) is e
    assert e["tangents"][0].offset == tan and e["tangents"][0].header
    assert any(u.offset == uv_hdr + 8 and u.header for u in e["uvs"])


# =============================================================================================
# Adversarial review (independent reviewer). Tests marked REGRESSION failed, crashed or silently
# produced wrong output before the review fixes in zen_package.py / inplace_patch.py.
# =============================================================================================
import shutil  # noqa: E402
import zlib  # noqa: E402

NON_ASCII_DIR = "球员_Ronaldo_Nazário_😀"


def _two_exports():
    return [zp.ExportSpec("A", b"a" * 40, TEX2D), zp.ExportSpec("B", b"b" * 24, TEX2D)]


def test_adv_export_without_serialize_entry_is_rejected():
    """REGRESSION: an export with no Serialize command (and another serialized twice) used to be
    accepted with abs range (-1, -1); extract-export then wrote an empty file and --verify passed."""
    data = bytearray(zp.build_legacy_package("/Game/X", _two_exports()))
    s = zp.parse_package(bytes(data)).summary
    entries_off = s["ExportBundlesOffset"] + 8          # one bundle header, then 4 entries
    struct.pack_into("<II", data, entries_off + 3 * 8, 0, 1)  # B's Serialize -> A's Serialize
    with pytest.raises(zp.ZenFormatError, match="one Create and one Serialize"):
        zp.parse_package(bytes(data))
    # UE5.0-5.2 (bundle headers) and 5.3+ (entries only) get the same check
    for layout in ("ue5.0-5.1", "ue5.3"):
        d5 = bytearray(zp.build_ue5_package("/Game/X", _two_exports(), layout=layout))
        pk = zp.parse_package(bytes(d5))
        struct.pack_into("<II", d5, pk.summary["ExportBundleEntriesOffset"] + 3 * 8, 0, 1)
        with pytest.raises(zp.ZenFormatError, match="one Create and one Serialize"):
            zp.parse_package(bytes(d5))


def test_adv_ue5_overlapping_or_gapped_export_ranges():
    """REGRESSION: 5.3+ export ranges come from CookedSerialOffset; overlapping ranges used to be
    accepted silently (README claimed the exports were checked to tile the chunk)."""
    d5 = bytearray(zp.build_ue5_package("/Game/X", _two_exports(), layout="ue5.4-5.5"))
    pk = zp.parse_package(bytes(d5))
    assert any("contiguous and non-overlapping" in e for e in pk.evidence)
    b_entry = pk.summary["ExportMapOffset"] + 72
    bad = bytearray(d5)
    struct.pack_into("<Q", bad, b_entry, 10)            # B starts inside A
    with pytest.raises(zp.ZenFormatError, match="overlap"):
        zp.parse_package(bytes(bad))
    gap = bytearray(d5)
    struct.pack_into("<Q", gap, b_entry, 44)            # 4-byte hole between A and B
    gap += b"\0" * 4
    p = zp.parse_package(bytes(gap))
    assert any("gap" in w for w in p.warnings) and not any("contiguous" in e for e in p.evidence)


@pytest.mark.parametrize("layout,ue5ver,guess", [
    ("ue5.0-5.1", 1004, "UE5.0"), ("ue5.0-5.1", 1008, "UE5.1"), ("ue5.2", 1009, "UE5.2"),
    ("ue5.3", 1009, "UE5.3"), ("ue5.4-5.5", 1012, "UE5.4"), ("ue5.4-5.5", 1013, "UE5.5"),
    ("ue5.6+", 1017, "UE5.6"), ("ue5.6+", None, "UE5.6+")])
def test_adv_versioned_ue5_layout_is_unique(layout, ue5ver, guess):
    """REGRESSION: with FZenPackageVersioningInfo present every layout of the same summary size
    parsed identically, so the first one in the list won (a versioned 5.4 package was reported as
    layout 'ue5.3' with a spurious 'other layouts also parsed' warning, 1009 + 52-byte summary
    was called UE5.2)."""
    data = zp.build_ue5_package("/Game/R9/T", _two_exports(), layout=layout,
                                versioning=None if ue5ver is None else (522, ue5ver),
                                imported_package_names=["/Game/Y"] if layout >= "ue5.3" else [])
    p = zp.parse_package(data)
    assert p.layout == layout and p.ue_version_guess.startswith(guess + " ")
    assert not p.warnings, p.warnings
    assert [data[e.abs_start:e.abs_end] for e in p.exports] == [e.data for e in _two_exports()]


def test_adv_nonstandard_fname_hash_algorithm_id():
    """REGRESSION: some shipped games write a non-stock FName hash algorithm id (retoc commit
    885a8da tolerates it); such packages used to fail with 'no package layout matched'."""
    leg = bytearray(zp.build_legacy_package("/Game/X", _two_exports()))
    struct.pack_into("<Q", leg, struct.unpack_from("<i", leg, 32)[0], 0xDEADBEEF)
    p = zp.parse_package(bytes(leg))
    assert p.layout == "ue4.26-4.27" and any("0xdeadbeef" in w for w in p.warnings)
    assert any("relaxing" in w for w in p.warnings)
    with pytest.raises(zp.ZenFormatError):  # strict parse (used for layout detection) still refuses
        zp.parse_legacy(bytes(leg))
    u5 = bytearray(zp.build_ue5_package("/Game/X", _two_exports(), layout="ue5.4-5.5"))
    struct.pack_into("<Q", u5, 52 + 8, 0x1234)          # name batch: num, numStringBytes, hash id
    p = zp.parse_package(bytes(u5))
    assert p.layout == "ue5.4-5.5" and any("0x1234" in w for w in p.warnings)


def test_adv_utf16_names_at_odd_offsets_and_non_bmp():
    """UTF-16 names after odd-length ANSI names (2-byte alignment, retoc read_name_batch_parts),
    characters outside the BMP (surrogate pairs) and ASCII-only lower-casing for the hash."""
    names = ["abc", "名字", "ab", "Größe", "x", "😀Ok", "ÉQUIPE"]
    nb, hb = zp.write_name_batch_legacy(names)
    # 'abc' ends at 5; the UTF-16 header of '名字' at 5-6 leaves 7 odd -> one pad byte at 7
    assert nb[5] == 0x80 and nb[7] == 0 and zp.read_name_batch_legacy(nb, len(names)) == names
    leg = zp.build_legacy_package("/Game/X", _two_exports(), extra_names=names)
    u5 = zp.build_ue5_package("/Game/X", _two_exports(), layout="ue5.3", extra_names=names)
    for data in (leg, u5):
        p = zp.parse_package(data)
        assert p.names[-len(names):] == names and not p.warnings
    # UE lower-cases ASCII only (TChar::ToLower): 'É' keeps its case, so the hashes differ
    assert zp.name_hash("ÉQUIPE") == zp.cityhash64("Équipe".encode("utf-16-le"))
    assert zp.name_hash("ÉQUIPE") != zp.name_hash("équipe")


def test_adv_zero_length_export_many_bundles_non_ascii_cli(tmp_path):
    """Zero-length export, one bundle per export (5 bundles), non-ASCII export name and paths."""
    specs = [zp.ExportSpec(f"E{i}", bytes([65 + i]) * (i * 7), TEX2D) for i in range(4)]
    specs.append(zp.ExportSpec("面部_Nazário", b"\x00\xff" * 33, SKMESH))
    data = zp.build_legacy_package("/Game/R9/Pkg", specs, bundles=[[4], [2], [0], [3], [1]])
    p = zp.parse_package(data)
    assert len(p.export_bundles) == 5 and p.exports[0].cooked_serial_size == 0
    assert all(data[e.abs_start:e.abs_end] == s.data for e, s in zip(p.exports, specs))
    d = tmp_path / NON_ASCII_DIR
    d.mkdir()
    pkg = d / "包.uasset"
    pkg.write_bytes(data)
    for key, want in (("E0", b""), ("面部_Nazário", specs[4].data)):
        out = d / f"out_{key}"
        assert run_cli(zp, ["extract-export", pkg, "--export", key, "--out-dir", out, "--verify"]) is None
        man = load_manifest(out)
        assert man["verify"]["reparsed_range_matches"] is True
        assert open(man["outputs"]["export"]["path"], "rb").read() == want
        assert man["outputs"]["export"]["sha256"] == zp.sha256_bytes(want)
    assert zp.sha256_file(pkg) == zp.sha256_bytes(data)


@realdata
def test_adv_real_samples_match_store_entries_and_tile():
    """Every real retoc sample, not only the ones the main tests pick: UE4.27 export count,
    export bundle count and chunk size equal the container store entry (metadata.json); every
    package parses without warnings and its exports tile the export data."""
    import glob
    seen = 0
    for p in sorted(glob.glob(RETOC_TESTS + "/UE*/*.uasset") + glob.glob(RETOC_TESTS + "/UE*/*.uzenasset")):
        base = p.rsplit(".", 1)[0]
        if p.endswith(".uasset") and (os.path.exists(base + ".uexp") or os.path.exists(base + ".uzenasset")):
            continue  # legacy cooked pair, not a Zen package
        data = open(p, "rb").read()
        pkg = zp.parse_package(data)
        assert not pkg.warnings, (p, pkg.warnings)
        assert any("contiguous and non-overlapping" in e for e in pkg.evidence), p
        if os.path.exists(base + ".metadata.json"):
            se = json.load(open(base + ".metadata.json")).get("store_entry")
            if se:
                assert (len(pkg.exports), len(pkg.export_bundles), len(data)) == \
                    (se["export_count"], se["export_bundle_count"], se["export_bundles_size"]), p
        seen += 1
    assert seen >= 15


@realdata
@pytest.mark.parametrize("name,fmt,nmips,ubulk", [("UE5.4/Randy", "PF_B8G8R8A8", 1, False),
                                                 ("UE5.6/T_Quinn_01_D", "PF_DXT1", 11, True)])
def test_adv_real_ue54_ue56_textures(name, fmt, nmips, ubulk, tmp_path):
    """The README claims texture scanning / SkipOffset checks on real UE5.4 and UE5.6 data; the
    main tests only covered UE4.27 and UE5.5."""
    pkg = f"{RETOC_TESTS}/{name}.uzenasset"
    pd = ip.scan_texture_platform_data(ip.load_target(pkg))[0]
    assert (pd.pixel_format, len(pd.mips), pd.variant) == (fmt, nmips, "ue5-data-resources")
    assert pd.skip_offset["consistent"] is True
    args = ["texture-extract", pkg, "--out-dir", tmp_path / "ex", "--verify"]
    if ubulk:
        args += ["--bulk", f"{RETOC_TESTS}/{name}.ubulk"]
    assert run_cli(ip, args) is None
    assert "missing" not in load_manifest(tmp_path / "ex")


# ---------------------------------------------------------------------------------------------
# Textures: flexible synthetic cooked texture exports
# ---------------------------------------------------------------------------------------------
_FLAGS = {"inline": 0x48, "bulk": 0x1 | 0x100 | 0x400 | 0x10000, "bulk_fixup": 0x1 | 0x100 | 0x400,
          "end": 0x1, "zlib": 0x48 | 0x2, "empty": 0x48}


def tex_export(names, fmt, mips, place=None, variant="ue4", first_mip=0, full=None, packed_extra=0,
               legacy_base=1024, block=(4, 4)):
    """Cooked UTexture2D export body. variant 'ue4': per-mip bCooked + FByteBulkData header,
    absolute legacy SkipOffset (UE4.20-4.27). variant 'ue5dr': 16-byte placeholder, per-mip int32
    bulk data map index (UE5.2+ DATA_RESOURCES), relative SkipOffset. ``place[i]`` in
    inline | bulk | bulk_fixup | end | zlib | empty. Returns (blob, ubulk, bulk_map, info)."""
    place = place or ["inline"] * len(mips)
    b = bytearray(b"\x07\x00\x00\x00\x00\x00\x00\x00" * 3 + bytes(range(37)) + b"\x00\x00")
    b += struct.pack("<I", 1)
    b += struct.pack("<II", names.index(fmt), 0)
    skip_pos = len(b)
    b += struct.pack("<q", 0)
    if variant == "ue5dr":
        b += b"\0" * 16
    pd_start = len(b)
    w0, h0 = full or (mips[0][0] << first_mip, mips[0][1] << first_mip)
    b += struct.pack("<iiI", w0, h0, 1 | packed_extra)
    b += struct.pack("<i", len(fmt) + 1) + fmt.encode() + b"\0"
    b += struct.pack("<ii", first_mip, len(mips))
    bulk, bmap, where = bytearray(), [], []
    for i, ((w, h, payload), pl) in enumerate(zip(mips, place)):
        flags = _FLAGS[pl]
        if pl == "empty":
            payload = b""
        stored = zlib.compress(payload) if pl == "zlib" else payload
        if variant == "ue4":
            b += struct.pack("<I", 1)
            if pl in ("bulk", "bulk_fixup"):
                b += struct.pack("<Iiiq", flags, len(payload), len(payload), len(bulk))
                where.append(("BulkData", len(bulk)))
                bulk += payload
            elif pl == "end":
                b += struct.pack("<Iiiq", flags, len(payload), len(payload), 0)
                where.append(("package_end", None))
            else:
                b += struct.pack("<Iiiq", flags, len(payload), len(stored), legacy_base + len(b) + 22)
                where.append(("inline", len(b)))
                b += stored
        else:
            b += struct.pack("<i", len(bmap))
            if pl in ("bulk", "bulk_fixup"):
                bmap.append((len(bulk), -1, len(payload), flags))
                where.append(("BulkData", len(bulk)))
                bulk += payload
            else:
                bmap.append((len(b), -1, len(payload), flags))
                where.append(("inline", len(b)))
                b += payload
        dw, dh = (max(w, block[0]), max(h, block[1])) if (variant == "ue4" and i >= 6) else (w, h)
        b += struct.pack("<iii", dw, dh, 1)
    b += struct.pack("<I", 0)  # bIsVirtual
    pd_end = len(b)
    b += struct.pack("<II", names.index("None"), 0)
    skip = legacy_base + pd_end if variant == "ue4" else pd_end - skip_pos
    struct.pack_into("<q", b, skip_pos, skip)
    return bytes(b), bytes(bulk), bmap, {"pd_start": pd_start, "pd_end": pd_end, "where": where}


TEX_NAMES = ["/Game/R9/T_R9_Face", "T_R9_Face", "None", "PF_ASTC_4x4"]


def _write_legacy_tex(d, name, blob, bulk=b""):
    pkg = zp.build_legacy_package("/Game/R9/T_R9_Face", [zp.ExportSpec("T_R9_Face", blob, TEX2D, template_index=TEX2D_CDO)],
                                  imports=[TEX2D, TEX2D_CDO], extra_names=TEX_NAMES[2:], cooked_header_size=1024)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.uasset").write_bytes(pkg)
    if bulk:
        (d / f"{name}.ubulk").write_bytes(bulk)
    return d / f"{name}.uasset", (d / f"{name}.ubulk") if bulk else None


def test_adv_texture_scan_optdata_flag_near_end_does_not_crash():
    """REGRESSION: a PF_* FString whose PackedData has HasOptData near the end of the scan range
    raised struct.error out of texture-scan (whole command crashed)."""
    blob = b"\0" * 20 + struct.pack("<iiI", 64, 64, 1 | (1 << 30)) + struct.pack("<i", 12) + b"PF_ASTC_4x4\0" + b"\1\2"
    assert ip.scan_texture_platform_data(ip.Target("b", blob, 0, len(blob))) == []


def test_adv_raw_ue4_blob_skip_offset_not_misreported(tex):
    """REGRESSION: for a raw UE4 export blob (no Zen header -> legacy delta unknown) SkipOffset
    was reported as an inconsistent 'ue5-relative' reading, i.e. a false 'consistent: false'."""
    blob, _, _, info = tex_export(TEX_NAMES, "PF_ASTC_4x4", tex["mips_a"])
    pd = ip.scan_texture_platform_data(ip.Target("blob", blob, 0, len(blob)))[0]
    so = pd.skip_offset
    assert so["layout"] == "ue4-absolute-legacy" and so["consistent"] is None
    assert so["legacy_minus_chunk"] == 1024 and "unknown" in so["note"]


def test_adv_first_mip_to_serialize(tex, tmp_path):
    """FirstMipToSerialize > 0 (LOD bias, common on mobile): SizeX/SizeY are the full size, the
    serialized mips start at level 2. Replace with a 256 chain works; the full-size 1024 chain
    is refused because the texture size must not change."""
    blob, _, _, _ = tex_export(TEX_NAMES, "PF_ASTC_4x4", tex["mips_a"], first_mip=2)
    pkg, _ = _write_legacy_tex(tmp_path / "fm", "T_R9_Face", blob)
    pd = ip.scan_texture_platform_data(ip.load_target(str(pkg)))[0]
    assert (pd.first_mip, pd.size_x, len(pd.mips), pd.mips[0].size_x) == (2, 1024, 9, 256)
    assert pd.skip_offset["consistent"] is True
    out = tmp_path / "out"
    assert run_cli(ip, ["texture-replace", pkg, "--astc-manifest", tex["man_b"], "--out-dir", out, "--verify"]) is None
    assert load_manifest(out)["verify"]["ok"] is True
    blob2, _, _, _ = tex_export(TEX_NAMES, "PF_ASTC_4x4", tex["mips_a"][2:], first_mip=0, full=(64, 64))
    pkg2, _ = _write_legacy_tex(tmp_path / "small", "T_R9_Face", blob2)
    assert "texture size must not change" in run_cli(
        ip, ["texture-replace", pkg2, "--astc-manifest", tex["man_b"], "--out-dir", tmp_path / "o2"])


def test_adv_texture_refusals_compressed_end_fixup_truncated(tex, tmp_path):
    """README refusal claims that had no test: compressed payload, PayloadAtEndOfFile inside the
    package, .ubulk offset needing fix-up, and a .ubulk too short for the last mip."""
    mips = tex["mips_a"]
    cases = {
        "zlib": (["zlib"] + ["inline"] * 8, "compressed"),
        "end": (["end"] + ["inline"] * 8, "PayloadAtEndOfFile"),
        "fixup": (["bulk_fixup"] * 3 + ["inline"] * 6, "fix-up"),
    }
    for name, (place, msg) in cases.items():
        blob, bulk, _, _ = tex_export(TEX_NAMES, "PF_ASTC_4x4", mips, place)
        pkg, bp = _write_legacy_tex(tmp_path / name, "T_R9_Face", blob, bulk)
        pd = ip.scan_texture_platform_data(ip.load_target(str(pkg)))[0]
        assert len(pd.mips) == 9 and pd.skip_offset["consistent"] is True, name
        args = ["texture-replace", pkg, "--astc-manifest", tex["man_b"], "--out-dir", tmp_path / f"o_{name}"]
        if bp:
            args += ["--bulk", bp]
        err = run_cli(ip, args)
        assert err and msg in err, (name, err)
        assert not (tmp_path / f"o_{name}").exists(), "nothing may be written when refusing"
    assert "fix-up" in run_cli(ip, ["texture-extract", tmp_path / "fixup" / "T_R9_Face.uasset", "--bulk",
                                    tmp_path / "fixup" / "T_R9_Face.ubulk", "--out-dir", tmp_path / "ex_fix"])
    assert not (tmp_path / "ex_fix").exists(), "REGRESSION: refusal used to leave a manifest-less output dir"
    # truncated .ubulk: the last streamed mip runs past the end of the chunk
    blob, bulk, _, _ = tex_export(TEX_NAMES, "PF_ASTC_4x4", mips, ["bulk"] * 3 + ["inline"] * 6)
    pkg, bp = _write_legacy_tex(tmp_path / "trunc", "T_R9_Face", blob, bulk[:-1])
    err = run_cli(ip, ["texture-replace", pkg, "--bulk", bp, "--astc-manifest", tex["man_b"], "--out-dir", tmp_path / "o_t"])
    assert err and "past end" in err


def test_adv_zero_size_and_unsupported_cpu_copy(tex, tmp_path):
    """A mip with SizeOnDisk 0 (no payload stored) no longer hides the whole texture; a
    HasCpuCopy (UE5.4+ FSharedImage) block is reported as unsupported instead of vanishing."""
    blob, _, _, _ = tex_export(TEX_NAMES, "PF_ASTC_4x4", tex["mips_a"], ["inline"] * 8 + ["empty"])
    pd = ip.scan_texture_platform_data(ip.Target("blob", blob, 0, len(blob)))[0]
    assert pd.mips[-1].bulk.size_on_disk == 0 and len(pd.mips) == 9
    blob, _, _, _ = tex_export(TEX_NAMES, "PF_ASTC_4x4", tex["mips_a"], packed_extra=1 << 29)
    pkg, _ = _write_legacy_tex(tmp_path / "cpu", "T_R9_Face", blob)
    rep = tmp_path / "scan.json"
    assert run_cli(ip, ["texture-scan", pkg, "--json", rep]) is None
    doc = json.loads(rep.read_text())
    assert doc["platform_data"] == [] and "HasCpuCopy" in doc["unsupported"][0]["reason"]
    err = run_cli(ip, ["texture-replace", pkg, "--astc-manifest", tex["man_b"], "--out-dir", tmp_path / "o"])
    assert "HasCpuCopy" in err


def test_adv_astc_manifest_order_missing_bin_and_index(tex, tmp_path):
    """Manifest mips out of order / not a mip chain, missing .bin, and --index out of range are
    clean refusals (out-of-range --index used to raise IndexError)."""
    k = tex["pkgs"]["inline"]
    man = json.load(open(tex["man_b"]))
    d = tmp_path / "m"
    d.mkdir()
    shutil.copy(tex["bin_b"], d / "r9.astc4x4.bin")
    swapped = dict(man, mips=[man["mips"][1], man["mips"][0]] + man["mips"][2:])
    (d / "r9.astc4x4.json").write_text(json.dumps(swapped))
    assert "levels must be 0..n-1" in run_cli(ip, ["texture-replace", k["path"], "--astc-manifest", d / "r9.astc4x4.json",
                                                   "--out-dir", tmp_path / "o1"])
    (tmp_path / "lonely.json").write_text(json.dumps(man))
    assert "not found" in run_cli(ip, ["texture-replace", k["path"], "--astc-manifest", tmp_path / "lonely.json",
                                       "--out-dir", tmp_path / "o2"])
    # two platform data blocks in one export (texture cooked for two formats)
    b1, _, _, _ = tex_export(TEX_NAMES, "PF_ASTC_4x4", tex["mips_a"])
    pkg, _ = _write_legacy_tex(tmp_path / "two", "T", b1 + b1)
    assert len(ip.scan_texture_platform_data(ip.load_target(str(pkg)))) == 2
    assert "pick one with --index" in run_cli(ip, ["texture-replace", pkg, "--astc-manifest", tex["man_b"], "--out-dir", tmp_path / "o3"])
    assert "out of range" in run_cli(ip, ["texture-replace", pkg, "--astc-manifest", tex["man_b"], "--index", "5",
                                          "--out-dir", tmp_path / "o4"])
    out = tmp_path / "o5"
    assert run_cli(ip, ["texture-replace", pkg, "--astc-manifest", tex["man_b"], "--index", "1", "--out-dir", out,
                        "--verify"]) is None
    new = open(load_manifest(out)["outputs"]["package"]["path"], "rb").read()
    pds = ip.scan_texture_platform_data(ip.load_target(str(load_manifest(out)["outputs"]["package"]["path"])))
    first = [new[m.bulk.payload_pos:m.bulk.payload_pos + m.bulk.size_on_disk] for m in pds[0].mips]
    second = [new[m.bulk.payload_pos:m.bulk.payload_pos + m.bulk.size_on_disk] for m in pds[1].mips]
    assert first == [p for _, _, p in tex["mips_a"]] and second == [p for _, _, p in tex["mips_b"]]


def test_adv_ue5_data_resources_texture_replace_and_identity(tex, tmp_path):
    """UE5.2+ DATA_RESOURCES texture (per-mip bulk data map index, 16-byte placeholder, relative
    SkipOffset) in a UE5.4 package: 3 mips in .ubulk, 6 inline. Replace + verify, then the
    identity round trip must reproduce both files byte for byte."""
    blob, bulk, bmap, info = tex_export(TEX_NAMES, "PF_ASTC_4x4", tex["mips_a"], ["bulk"] * 3 + ["inline"] * 6,
                                        variant="ue5dr")
    pkg_bytes = zp.build_ue5_package("/Game/R9/T_R9_Face", [zp.ExportSpec("T_R9_Face", blob, TEX2D)],
                                     layout="ue5.4-5.5", extra_names=TEX_NAMES[2:], bulk_data_map=bmap)
    d = tmp_path / NON_ASCII_DIR
    d.mkdir()
    (d / "T.uasset").write_bytes(pkg_bytes)
    (d / "T.ubulk").write_bytes(bulk)
    t = ip.load_target(str(d / "T.uasset"))
    assert t.package.layout == "ue5.4-5.5" and len(t.package.bulk_data_map) == 9
    pd = ip.scan_texture_platform_data(t)[0]
    assert pd.variant == "ue5-data-resources" and pd.skip_offset == dict(pd.skip_offset, layout="ue5-relative", consistent=True)
    assert [m.bulk.location for m in pd.mips] == ["BulkData"] * 3 + ["inline"] * 6
    out = d / "out"
    assert run_cli(ip, ["texture-replace", d / "T.uasset", "--bulk", d / "T.ubulk", "--astc-manifest", tex["man_b"],
                        "--out-dir", out, "--verify", "--decode-check"]) is None
    man = load_manifest(out)
    assert man["verify"]["ok"] and sorted(man["outputs"]) == ["BulkData", "package"]
    assert open(man["outputs"]["BulkData"]["path"], "rb").read() == b"".join(p for _, _, p in tex["mips_b"][:3])
    ex = d / "ex"
    assert run_cli(ip, ["texture-extract", d / "T.uasset", "--bulk", d / "T.ubulk", "--out-dir", ex, "--verify"]) is None
    idn = d / "id"
    assert run_cli(ip, ["texture-replace", d / "T.uasset", "--bulk", d / "T.ubulk", "--astc-manifest",
                        load_manifest(ex)["outputs"]["astc_manifest"]["path"], "--out-dir", idn, "--verify",
                        "--strict-dims"]) is None
    m2 = load_manifest(idn)
    assert m2["outputs"]["package"]["sha256"] == zp.sha256_bytes(pkg_bytes)
    assert m2["outputs"]["BulkData"]["sha256"] == zp.sha256_bytes(bulk)


# ---------------------------------------------------------------------------------------------
# Meshes
# ---------------------------------------------------------------------------------------------
def test_adv_bounds_file_equal_to_input_gives_one_complete_output(mesh, tmp_path):
    """REGRESSION: --bounds-file naming the input itself produced TWO outputs (positions in one,
    bounds in 'bounds_file.<name>'), neither complete, and --verify still passed."""
    _edited_glb(mesh, "lod0", tmp_path / "new0.glb", amount=3.0)
    out = tmp_path / "out"
    err = run_cli(ip, ["mesh-patch-positions", mesh["input"], "--pair", mesh["dir"] / "lod0.glb", tmp_path / "new0.glb",
                       "--bounds", "union", "--bounds-file", mesh["input"], "--out-dir", out, "--verify"])
    assert err is None, err
    man = load_manifest(out)
    assert list(man["outputs"]) == ["input"] and man["verify"]["ok"]
    new = open(man["outputs"]["input"]["path"], "rb").read()
    o = mesh["offs"]
    n = len(mesh["lod0"]["P"])
    assert new[o["lod0"]["pos"]:o["lod0"]["pos"] + 12 * n] != mesh["blob"][o["lod0"]["pos"]:o["lod0"]["pos"] + 12 * n]
    assert new[o["bounds"]:o["bounds"] + 28] != mesh["blob"][o["bounds"]:o["bounds"] + 28]
    # a separate bounds file is still supported and its untouched bytes are now verified as well
    other = tmp_path / "bounds_src.bin"
    other.write_bytes(mesh["blob"])
    out2 = tmp_path / "out2"
    assert run_cli(ip, ["mesh-patch-positions", mesh["input"], "--pair", mesh["dir"] / "lod0.glb", tmp_path / "new0.glb",
                        "--bounds", "union", "--bounds-file", other, "--out-dir", out2, "--verify"]) is None
    checks = [c["check"] for c in load_manifest(out2)["verify"]["checks"]]
    assert any(c.startswith("bounds_file: size unchanged") for c in checks)


def test_adv_mesh_same_lod_twice_and_non_ascii_paths(mesh, tmp_path):
    """Listing the same LOD twice is refused (overlapping writes); all paths non-ASCII."""
    d = tmp_path / NON_ASCII_DIR
    d.mkdir()
    shutil.copy(mesh["input"], d / "头模.bin")
    shutil.copy(mesh["dir"] / "lod0.glb", d / "参考_lod0.glb")
    _edited_glb(mesh, "lod0", d / "新_lod0.glb")
    err = run_cli(ip, ["mesh-patch-positions", d / "头模.bin", "--pair", d / "参考_lod0.glb", d / "新_lod0.glb",
                       "--pair", d / "参考_lod0.glb", d / "新_lod0.glb", "--out-dir", d / "o1"])
    assert err and "overlapping" in err
    out = d / "输出"
    assert run_cli(ip, ["mesh-patch-positions", d / "头模.bin", "--pair", d / "参考_lod0.glb", d / "新_lod0.glb",
                        "--tangents", "--out-dir", out, "--verify"]) is None
    man = load_manifest(out)
    assert man["verify"]["ok"] and man["outputs"]["input"]["path"].endswith("头模.bin")
    assert zp.sha256_file(d / "头模.bin") == zp.sha256_bytes(mesh["blob"])


# ---------------------------------------------------------------------------------------------
# End to end through an IoStore container (README section 6, step 7)
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("rebuild", [False, True])
def test_adv_end_to_end_encrypted_compressed_container(tex, tmp_path, monkeypatch, rebuild):
    """The documented workflow on an AES-encrypted, compressed, multi-partition container with
    4 KiB blocks: the package chunk spans ~22 blocks with a partial last block, the .ubulk chunk
    is an exact multiple of the block size, a zero-length chunk and an unrelated chunk sit
    beside them, and the paths are non-ASCII. iostore extract -> texture-replace (inline AND
    .ubulk) -> iostore patch --verify -> extract again -> independent re-scan of the new chunks.
    The AES key must not appear in any file written along the way."""
    ti = pytest.importorskip("test_iostore")
    import iostore
    key = os.urandom(32)
    monkeypatch.setenv("R9_AES_KEY_HEX", key.hex())
    inl, blk = tex["pkgs"]["inline"], tex["pkgs"]["bulk"]
    base = f"eFootball/Content/{NON_ASCII_DIR}/"
    chunks = [
        ti.SChunk(ti.make_chunk_id(ti.pid("face"), 0, 2), inl["path"].read_bytes(), base + "T_Face.uasset", "alt"),
        ti.SChunk(ti.make_chunk_id(ti.pid("hair"), 0, 2), blk["path"].read_bytes(), base + "T_Hair.uasset", "zlib"),
        ti.SChunk(ti.make_chunk_id(ti.pid("hair"), 0, 3), blk["bulk"].read_bytes(), base + "T_Hair.ubulk", "zlib"),
        ti.SChunk(ti.make_chunk_id(ti.pid("zero"), 0, 2), b"", base + "zero.uasset"),
        ti.SChunk(ti.make_chunk_id(ti.pid("other"), 0, 2), ti._payload(5, 9999), "eFootball/Content/other.uasset", "lz4"),
    ]
    assert len(chunks[0].data) % 4096 and len(chunks[0].data) > 20 * 4096 and len(chunks[2].data) % 4096 == 0
    info = ti.build_container(str(tmp_path / "in"), "pakchunk_r9", chunks, 3, key=key, block_size=4096,
                              partition_size=40 * 1024, meta_algo="sha1")
    assert info["partitions"] > 2
    utoc = info["utoc"]

    def iost(*args):
        assert iostore.main([str(a) for a in args]) == 0

    ex1 = tmp_path / "ex1"
    iost("extract", utoc, "--all", "--out-dir", ex1, "--verify")
    xdir = ex1 / "eFootball" / "Content" / NON_ASCII_DIR
    assert (xdir / "T_Face.uasset").read_bytes() == chunks[0].data and (xdir / "zero.uasset").read_bytes() == b""
    p_face, p_hair = tmp_path / "p_face", tmp_path / "p_hair"
    assert run_cli(ip, ["texture-replace", xdir / "T_Face.uasset", "--astc-manifest", tex["man_b"],
                        "--out-dir", p_face, "--verify"]) is None
    assert run_cli(ip, ["texture-replace", xdir / "T_Hair.uasset", "--bulk", xdir / "T_Hair.ubulk",
                        "--astc-manifest", tex["man_b"], "--out-dir", p_hair, "--verify"]) is None
    rec = {os.path.basename(r["file"]): r["index"]
           for r in json.load(open(ex1 / "extract_manifest.json", encoding="utf-8"))["outputs"]}
    new_files = {"T_Face.uasset": p_face / "T_Face.uasset", "T_Hair.uasset": p_hair / "T_Hair.uasset",
                 "T_Hair.ubulk": p_hair / "T_Hair.ubulk"}
    args = ["patch", utoc, "--out-dir", tmp_path / "c2", "--verify"] + (["--rebuild"] if rebuild else [])
    for fname, path in new_files.items():
        args += ["--replace", f"index:{rec[fname]}", path]
    iost(*args)
    ex2 = tmp_path / "ex2"
    iost("extract", tmp_path / "c2" / "pakchunk_r9.utoc", "--all", "--out-dir", ex2, "--verify")
    ydir = ex2 / "eFootball" / "Content" / NON_ASCII_DIR
    for fname, path in new_files.items():
        assert (ydir / fname).read_bytes() == path.read_bytes(), fname
    assert (ydir / "zero.uasset").read_bytes() == b""
    assert (ex2 / "eFootball" / "Content" / "other.uasset").read_bytes() == chunks[4].data
    # independent read back of the chunks that came out of the NEW container
    want = [p for _, _, p in tex["mips_b"]]
    face = ip.scan_texture_platform_data(ip.load_target(str(ydir / "T_Face.uasset")))[0]
    data = (ydir / "T_Face.uasset").read_bytes()
    assert [data[m.bulk.payload_pos:m.bulk.payload_pos + m.bulk.size_on_disk] for m in face.mips] == want
    assert face.skip_offset["consistent"] is True
    ex3 = tmp_path / "ex3"
    assert run_cli(ip, ["texture-extract", ydir / "T_Hair.uasset", "--bulk", ydir / "T_Hair.ubulk",
                        "--out-dir", ex3, "--verify"]) is None
    m3 = load_manifest(ex3)
    assert [open(m3["outputs"][f"mip{i}"]["path"], "rb").read() for i in range(9)] == want
    # the key is never written
    needles = (key.hex().encode(), key.hex().upper().encode(), key)
    for root, _, files in os.walk(tmp_path):
        if os.path.abspath(root).startswith(os.path.abspath(tmp_path / "in")):
            continue
        for f in files:
            if f.endswith((".json", ".txt")):
                blob = open(os.path.join(root, f), "rb").read()
                assert not any(n in blob for n in needles), os.path.join(root, f)
