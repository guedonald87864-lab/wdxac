#!/usr/bin/env python3
"""IoStore ("Zen") package header parser for UE4.26/4.27 and UE5.x cooked packages.

A Zen package is the ExportBundleData chunk of an IoStore container (what extractors usually save
as ``Name.uasset``/``.uzenasset``): a header (summary, name map, import map, export map, export
bundles, graph/dependency data) followed by the serialized exports. This module parses the header
and reports, for every export, its object name, class and the absolute byte range of its
serialized data inside the chunk, so the asset-level tools (``inplace_patch.py``) can work on one
export at a time.

Format sources (read these for the exact layouts; offsets below follow them):

* CUE4Parse ``UE4/IO/Objects/FPackageSummary.cs`` - UE4.26/4.27 ``FPackageSummary`` (64 bytes).
* CUE4Parse ``UE4/IO/Objects/FZenPackageSummary.cs`` - UE5 ``FZenPackageSummary`` and
  ``FZenPackageVersioningInfo``; ``FZenPackageCellOffsets`` (UE5.6+, VERSE_CELLS).
* CUE4Parse ``UE4/IO/Objects/FExportMapEntry.cs`` (72 bytes), ``FExportBundleHeader.cs``,
  ``FExportBundleEntry.cs``, ``FMappedName.cs``, ``FPackageObjectIndex.cs``,
  ``FScriptObjectEntry.cs``, ``FBulkDataMapEntry.cs`` (32 bytes).
* CUE4Parse ``UE4/Assets/IoPackage.cs`` - ``ReadRealm`` (header walk, export count derivation),
  ``LoadExportBundles`` (bundle header inference for 4.26/4.27), ``LoadGraphData`` and
  ``PopulateExports`` (export data laid out in bundle order from ``GraphDataOffset+GraphDataSize``
  for 4.26-5.2, ``HeaderSize + CookedSerialOffset`` for 5.3+).
* CUE4Parse ``UE4/Objects/UObject/FNameEntrySerialized.cs`` - ``LoadNameBatch`` (UE5 batch) and
  ``LoadNameHeader`` (4.26/4.27 per-entry ``FSerializedNameHeader``).
* CUE4Parse ``UE4/IO/IoGlobalData.cs`` - script objects from the global container
  (UE4: LoaderGlobalNames + LoaderInitialLoadMeta, UE5: ScriptObjects chunk).
* retoc (github.com/trumank/retoc) ``retoc/src/zen.rs``, ``name_map.rs`` (UTF-16 names in the
  legacy batch are 2-byte aligned relative to the names buffer; name hash = CityHash64 of the
  lower-cased name), ``script_objects.rs`` (script import index = CityHash64 of the UTF-16LE
  lower-cased object path with '.' and ':' replaced by '/', top two bits cleared).
* Google CityHash v1.1 ``city.cc`` (``CityHash64``), the hash UE uses for FName hashes,
  FPackageId and FPackageObjectIndex.

UE version detection is heuristic: the header is parsed with every known layout and the layout
whose offsets, name batch and section sizes are all self-consistent wins. The evidence is reported
so a human can double check. 4.26 and 4.27 share one package layout; the .utoc version tells them
apart (TOC version 2 = DirectoryIndex = 4.26, 3 = PartitionSize = 4.27).

This module only reads (and, for tests/fixtures, builds) package bytes. It never touches
encryption: decrypting the container is the IoStore tool's job.
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
import struct
from dataclasses import dataclass, field

MASK64 = (1 << 64) - 1

# --------------------------------------------------------------------------------------------
# CityHash64 (Google CityHash v1.1, src/city.cc) - pure Python port.
# --------------------------------------------------------------------------------------------
_K0 = 0xC3A5C85C97CB3127
_K1 = 0xB492B66FBE98F273
_K2 = 0x9AE16A3B2F90404F
_KMUL = 0x9DDFEA08EB382D69


def _f64(s, i):
    return int.from_bytes(s[i:i + 8], "little")


def _f32(s, i):
    return int.from_bytes(s[i:i + 4], "little")


def _rot(v, shift):
    return v if shift == 0 else ((v >> shift) | (v << (64 - shift))) & MASK64


def _shift_mix(v):
    return v ^ (v >> 47)


def _bswap64(v):
    return int.from_bytes(v.to_bytes(8, "little"), "big")


def _hash_len16(u, v, mul=_KMUL):
    a = ((u ^ v) * mul) & MASK64
    a ^= a >> 47
    b = ((v ^ a) * mul) & MASK64
    b ^= b >> 47
    return (b * mul) & MASK64


def _hash_len0to16(s, n):
    if n >= 8:
        mul = (_K2 + n * 2) & MASK64
        a = (_f64(s, 0) + _K2) & MASK64
        b = _f64(s, n - 8)
        c = (_rot(b, 37) * mul + a) & MASK64
        d = ((_rot(a, 25) + b) * mul) & MASK64
        return _hash_len16(c, d, mul)
    if n >= 4:
        mul = (_K2 + n * 2) & MASK64
        a = _f32(s, 0)
        return _hash_len16((n + (a << 3)) & MASK64, _f32(s, n - 4), mul)
    if n > 0:
        a, b, c = s[0], s[n >> 1], s[n - 1]
        y = a + (b << 8)
        z = n + (c << 2)
        return (_shift_mix(((y * _K2) ^ (z * _K0)) & MASK64) * _K2) & MASK64
    return _K2


def _hash_len17to32(s, n):
    mul = (_K2 + n * 2) & MASK64
    a = (_f64(s, 0) * _K1) & MASK64
    b = _f64(s, 8)
    c = (_f64(s, n - 8) * mul) & MASK64
    d = (_f64(s, n - 16) * _K2) & MASK64
    return _hash_len16((_rot((a + b) & MASK64, 43) + _rot(c, 30) + d) & MASK64,
                       (a + _rot((b + _K2) & MASK64, 18) + c) & MASK64, mul)


def _weak_hash32(w, x, y, z, a, b):
    a = (a + w) & MASK64
    b = _rot((b + a + z) & MASK64, 21)
    c = a
    a = (a + x + y) & MASK64
    b = (b + _rot(a, 44)) & MASK64
    return (a + z) & MASK64, (b + c) & MASK64


def _weak_hash32_s(s, i, a, b):
    return _weak_hash32(_f64(s, i), _f64(s, i + 8), _f64(s, i + 16), _f64(s, i + 24), a, b)


def _hash_len33to64(s, n):
    mul = (_K2 + n * 2) & MASK64
    a = (_f64(s, 0) * _K2) & MASK64
    b = _f64(s, 8)
    c = _f64(s, n - 24)
    d = _f64(s, n - 32)
    e = (_f64(s, 16) * _K2) & MASK64
    f = (_f64(s, 24) * 9) & MASK64
    g = _f64(s, n - 8)
    h = (_f64(s, n - 16) * mul) & MASK64
    u = (_rot((a + g) & MASK64, 43) + (_rot(b, 30) + c) * 9) & MASK64
    v = (((a + g) & MASK64) ^ d) + f + 1 & MASK64
    w = (_bswap64(((u + v) * mul) & MASK64) + h) & MASK64
    x = (_rot((e + f) & MASK64, 42) + c) & MASK64
    y = ((_bswap64(((v + w) * mul) & MASK64) + g) * mul) & MASK64
    z = (e + f + c) & MASK64
    a = (_bswap64(((x + z) * mul + y) & MASK64) + b) & MASK64
    b = (_shift_mix(((z + a) * mul + d + h) & MASK64) * mul) & MASK64
    return (b + x) & MASK64


def cityhash64(data: bytes) -> int:
    """CityHash64 v1.1 of ``data`` (bytes). Matches google/cityhash ``CityHash64``."""
    s = bytes(data)
    n = len(s)
    if n <= 32:
        return _hash_len0to16(s, n) if n <= 16 else _hash_len17to32(s, n)
    if n <= 64:
        return _hash_len33to64(s, n)
    x = _f64(s, n - 40)
    y = (_f64(s, n - 16) + _f64(s, n - 56)) & MASK64
    z = _hash_len16((_f64(s, n - 48) + n) & MASK64, _f64(s, n - 24))
    v = _weak_hash32_s(s, n - 64, n, z)
    w = _weak_hash32_s(s, n - 32, (y + _K1) & MASK64, x)
    x = (x * _K1 + _f64(s, 0)) & MASK64
    pos = 0
    remaining = (n - 1) & ~63
    while True:
        x = (_rot((x + y + v[0] + _f64(s, pos + 8)) & MASK64, 37) * _K1) & MASK64
        y = (_rot((y + v[1] + _f64(s, pos + 48)) & MASK64, 42) * _K1) & MASK64
        x ^= w[1]
        y = (y + v[0] + _f64(s, pos + 40)) & MASK64
        z = (_rot((z + w[0]) & MASK64, 33) * _K1) & MASK64
        v = _weak_hash32_s(s, pos, (v[1] * _K1) & MASK64, (x + w[0]) & MASK64)
        w = _weak_hash32_s(s, pos + 32, (z + w[1]) & MASK64, (y + _f64(s, pos + 16)) & MASK64)
        z, x = x, z
        pos += 64
        remaining -= 64
        if remaining == 0:
            break
    return _hash_len16((_hash_len16(v[0], w[0]) + _shift_mix(y) * _K1 + z) & MASK64,
                       (_hash_len16(v[1], w[1]) + x) & MASK64)


# --------------------------------------------------------------------------------------------
# Small binary reader
# --------------------------------------------------------------------------------------------
class ZenFormatError(ValueError):
    """Raised when bytes do not form a valid package of the layout being tried."""


class Reader:
    """Little-endian cursor over a bytes object; every read is bounds checked."""

    def __init__(self, data, pos=0):
        self.data = data
        self.pos = pos

    def need(self, n):
        if n < 0 or self.pos + n > len(self.data):
            raise ZenFormatError(f"read of {n} bytes at {self.pos} past end ({len(self.data)})")

    def take(self, n):
        self.need(n)
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return bytes(b)

    def _unpack(self, fmt, n):
        self.need(n)
        v = struct.unpack_from(fmt, self.data, self.pos)[0]
        self.pos += n
        return v

    def u8(self):
        return self._unpack("<B", 1)

    def u16(self):
        return self._unpack("<H", 2)

    def i32(self):
        return self._unpack("<i", 4)

    def u32(self):
        return self._unpack("<I", 4)

    def i64(self):
        return self._unpack("<q", 8)

    def u64(self):
        return self._unpack("<Q", 8)


# --------------------------------------------------------------------------------------------
# Names (FNameEntrySerialized / name batches)
# --------------------------------------------------------------------------------------------
FNAME_HASH_ALGORITHM_ID = 0xC1640000


def name_hash(name: str) -> int:
    """FName hash stored in name batches: CityHash64 of the lower-cased name.

    ASCII names hash their bytes; wide names hash UTF-16LE (retoc ``name_map.rs::name_hash``).
    """
    lower = "".join(c.lower() if "A" <= c <= "Z" else c for c in name)
    if lower.isascii():
        return cityhash64(lower.encode("ascii"))
    return cityhash64(lower.encode("utf-16-le"))


def _name_header(name: str) -> bytes:
    """FSerializedNameHeader: 2 bytes big-endian, bit 15 = UTF-16, low 15 bits = length."""
    if name.isascii():
        n = len(name)
        return bytes([(n >> 8) & 0x7F, n & 0xFF])
    n = len(name.encode("utf-16-le")) // 2
    return bytes([0x80 | ((n >> 8) & 0x7F), n & 0xFF])


def read_name_batch_legacy(buf: bytes, count: int | None = None) -> list[str]:
    """UE4.26/4.27 package name map (and LoaderGlobalNames): header+string per entry.

    Source: CUE4Parse ``FNameEntrySerialized.LoadNameHeader``; UTF-16 strings are aligned to two
    bytes relative to the start of ``buf`` (retoc ``read_name_batch_parts``).
    """
    names = []
    pos = 0
    while pos < len(buf) and (count is None or len(names) < count):
        if pos + 2 > len(buf):
            raise ZenFormatError("truncated name header")
        b0, b1 = buf[pos], buf[pos + 1]
        pos += 2
        length = ((b0 & 0x7F) << 8) | b1
        if b0 & 0x80:
            if pos & 1:
                pos += 1
            raw = buf[pos:pos + 2 * length]
            if len(raw) != 2 * length:
                raise ZenFormatError("truncated UTF-16 name")
            names.append(raw.decode("utf-16-le"))
            pos += 2 * length
        else:
            raw = buf[pos:pos + length]
            if len(raw) != length:
                raise ZenFormatError("truncated name")
            try:
                names.append(raw.decode("ascii"))
            except UnicodeDecodeError as e:
                raise ZenFormatError(f"non-ASCII byte in ANSI name: {e}") from None
            pos += length
    if count is not None and len(names) != count:
        raise ZenFormatError(f"expected {count} names, decoded {len(names)}")
    return names


def read_name_batch(r: Reader, max_names: int = 1_000_000, strict_hash_id: bool = True) -> list[str]:
    """UE5 name batch: num, numStringBytes, hashVersion, hashes[num], headers[num], strings.

    Source: CUE4Parse ``FNameEntrySerialized.LoadNameBatch(FArchive)`` (skips the hash version
    without checking it); retoc ``read_name_batch`` (commit 885a8da "Do not panic on non-standard
    FName hash algo": some shipped games write another id). ``strict_hash_id`` requires the stock
    0xC1640000, which is one of the layout-detection checks; callers relax it explicitly.
    """
    num = r.i32()
    if num == 0:
        return []
    if num < 0 or num > max_names:
        raise ZenFormatError(f"implausible name count {num}")
    num_string_bytes = r.u32()
    hash_version = r.u64()
    if hash_version != FNAME_HASH_ALGORITHM_ID and strict_hash_id:
        raise ZenFormatError(f"unexpected name hash version {hash_version:#x}")
    r.need(num * 8 + num * 2 + num_string_bytes)
    r.pos += num * 8
    headers = [r.take(2) for _ in range(num)]
    names = []
    start = r.pos
    for h in headers:
        length = ((h[0] & 0x7F) << 8) | h[1]
        if h[0] & 0x80:
            names.append(r.take(2 * length).decode("utf-16-le"))
        else:
            raw = r.take(length)
            try:
                names.append(raw.decode("ascii"))
            except UnicodeDecodeError:
                names.append(raw.decode("utf-8", "replace"))
    if r.pos - start != num_string_bytes:
        raise ZenFormatError(f"name batch string bytes {r.pos - start} != declared {num_string_bytes}")
    return names


def write_name_batch_legacy(names: list[str]) -> tuple[bytes, bytes]:
    """Build (names_buffer, hashes_buffer) in the 4.26/4.27 layout (for fixtures/tests)."""
    out = bytearray()
    hashes = bytearray(struct.pack("<Q", FNAME_HASH_ALGORITHM_ID))
    for n in names:
        out += _name_header(n)
        if n.isascii():
            out += n.encode("ascii")
        else:
            if len(out) & 1:
                out += b"\0"
            out += n.encode("utf-16-le")
        hashes += struct.pack("<Q", name_hash(n))
    return bytes(out), bytes(hashes)


def write_name_batch(names: list[str]) -> bytes:
    """Build a UE5 name batch (for fixtures/tests)."""
    out = bytearray(struct.pack("<i", len(names)))
    if not names:
        return bytes(out)
    strings = b"".join(n.encode("ascii") if n.isascii() else n.encode("utf-16-le") for n in names)
    out += struct.pack("<IQ", len(strings), FNAME_HASH_ALGORITHM_ID)
    for n in names:
        out += struct.pack("<Q", name_hash(n))
    for n in names:
        out += _name_header(n)
    out += strings
    return bytes(out)


# --------------------------------------------------------------------------------------------
# FMappedName / FPackageObjectIndex
# --------------------------------------------------------------------------------------------
MAPPED_NAME_TYPES = {0: "Package", 1: "Container", 2: "Global"}


@dataclass(frozen=True)
class MappedName:
    """FMappedName: 30-bit index + 2-bit type (Package/Container/Global), then the FName number."""
    raw_index: int
    number: int

    @property
    def index(self):
        return self.raw_index & ((1 << 30) - 1)

    @property
    def kind(self):
        return MAPPED_NAME_TYPES.get(self.raw_index >> 30, "Invalid")

    @staticmethod
    def read(r: Reader) -> "MappedName":
        return MappedName(r.u32(), r.u32())

    def pack(self) -> bytes:
        return struct.pack("<II", self.raw_index, self.number)

    @staticmethod
    def make(index: int, number: int = 0, kind: int = 0) -> "MappedName":
        return MappedName((index & ((1 << 30) - 1)) | (kind << 30), number)


def format_fname(base: str, number: int) -> str:
    """FName text: number 0 means no suffix, otherwise ``base_<number-1>``."""
    return base if number == 0 else f"{base}_{number - 1}"


OBJ_INDEX_TYPES = {0: "Export", 1: "ScriptImport", 2: "PackageImport", 3: "Null"}
NULL_OBJECT_INDEX = MASK64


def obj_index_kind(raw: int) -> str:
    """Type of a raw FPackageObjectIndex (top two bits)."""
    if raw == NULL_OBJECT_INDEX:
        return "Null"
    return OBJ_INDEX_TYPES[raw >> 62]


def make_obj_index(kind: str, value: int) -> int:
    code = {v: k for k, v in OBJ_INDEX_TYPES.items()}[kind]
    if kind == "Null":
        return NULL_OBJECT_INDEX
    return (code << 62) | (value & ((1 << 62) - 1))


def object_path_hash(object_path: str) -> int:
    """GenerateImportHashFromObjectPath (retoc ``script_objects.rs``): 62-bit CityHash64 of the
    UTF-16LE, lower-cased path with '.' and ':' replaced by '/'."""
    p = "".join("/" if c in ".:" else (c.lower() if "A" <= c <= "Z" else c) for c in object_path)
    return cityhash64(p.encode("utf-16-le")) & ~(3 << 62) & MASK64


def script_import_index(object_path: str) -> int:
    """Raw FPackageObjectIndex of a script import such as ``/Script/Engine.Texture2D``."""
    return make_obj_index("ScriptImport", object_path_hash(object_path))


def legacy_package_import_index(object_path: str) -> int:
    """UE4.26/4.27 package import / GlobalImportIndex of a package object path."""
    return make_obj_index("PackageImport", object_path_hash(object_path))


def describe_obj_index(raw: int) -> str:
    kind = obj_index_kind(raw)
    if kind == "Null":
        return "Null"
    if kind == "Export":
        return f"Export({raw & 0xFFFFFFFF})"
    return f"{kind}(0x{raw & ((1 << 62) - 1):015X})"


# Common engine classes. Their script import indices are pure hashes of the path, so they can be
# resolved even when the global script object table (global.utoc) is not available.
_KNOWN_SCRIPT_PACKAGES = {
    "/Script/CoreUObject": ["Class", "Package", "Object", "ScriptStruct", "Function", "Enum",
                            "MetaData", "ObjectRedirector"],
    "/Script/Engine": [
        "Texture2D", "TextureCube", "Texture2DArray", "VolumeTexture", "TextureRenderTarget2D",
        "SkeletalMesh", "StaticMesh", "Skeleton", "PhysicsAsset", "SkeletalBodySetup", "BodySetup",
        "Material", "MaterialInstanceConstant", "MaterialFunction",
        "MaterialParameterCollection", "AnimSequence", "AnimMontage", "AnimComposite",
        "AnimBlueprint", "AnimBlueprintGeneratedClass", "BlendSpace", "BlendSpace1D",
        "AimOffsetBlendSpace", "PoseAsset", "MorphTarget", "SkeletalMeshSocket", "SkeletalMeshLODSettings",
        "Blueprint", "BlueprintGeneratedClass", "DataTable", "CurveTable", "CompositeDataTable",
        "CurveFloat", "CurveLinearColor", "CurveVector", "UserDefinedStruct", "UserDefinedEnum",
        "SoundWave", "SoundCue", "Font", "FontFace", "World", "Level", "Actor", "ActorComponent",
        "SceneComponent", "StaticMeshComponent", "SkeletalMeshComponent", "AssetImportData",
        "AssetUserData", "SimpleConstructionScript", "SCS_Node", "ObjectLibrary", "DataAsset",
        "PrimaryDataAsset", "ThumbnailInfo", "AnimNotify", "AnimNotifyState", "AnimCurveMetaData",
        "BlendProfile",
    ],
    "/Script/PhysicsCore": ["PhysicalMaterial"],
    "/Script/NavigationSystem": ["NavCollision"],
    "/Script/Paper2D": ["PaperSprite", "PaperFlipbook", "PaperSpriteAtlas"],
    "/Script/UMG": ["WidgetBlueprintGeneratedClass", "WidgetTree", "Widget", "UserWidget"],
    "/Script/ClothingSystemRuntimeCommon": ["ClothingAssetCommon"],
}


@functools.lru_cache(maxsize=1)
def builtin_script_objects() -> dict[int, str]:
    """Map raw FPackageObjectIndex -> object path for well-known engine classes and their CDOs."""
    table = {}
    for pkg, classes in _KNOWN_SCRIPT_PACKAGES.items():
        table[script_import_index(pkg)] = pkg
        for c in classes:
            for obj in (c, "Default__" + c):
                path = f"{pkg}.{obj}"
                table[script_import_index(path)] = path
    return table


# --------------------------------------------------------------------------------------------
# Global script objects (global.utoc)
# --------------------------------------------------------------------------------------------
@dataclass
class ScriptObjects:
    """Resolved script object table: raw FPackageObjectIndex -> full object path."""
    paths: dict = field(default_factory=dict)
    source: str = "builtin"
    hash_mismatches: int = 0

    def resolve(self, raw: int) -> str | None:
        return self.paths.get(raw)


def _script_entries_to_paths(entries, names):
    by_index = {e[1]: e for e in entries}
    cache = {}

    def path_of(raw, depth=0):
        if raw in cache:
            return cache[raw]
        e = by_index.get(raw)
        if e is None or depth > 32:
            return None
        name_mn, _, outer, _ = e
        base = format_fname(names[name_mn.index], name_mn.number) if name_mn.index < len(names) else f"<name#{name_mn.index}>"
        if outer == NULL_OBJECT_INDEX or outer not in by_index:
            p = base
        else:
            op = path_of(outer, depth + 1)
            p = f"{op}.{base}" if op and op.count(".") == 0 else f"{op}:{base}" if op else base
        cache[raw] = p
        return p

    return {raw: path_of(raw) for raw in by_index}


def _read_script_entries(r: Reader, count: int):
    entries = []
    for _ in range(count):
        entries.append((MappedName.read(r), r.u64(), r.u64(), r.u64()))
    return entries


def load_script_objects_ue4(global_names: bytes, initial_load_meta: bytes,
                            global_name_hashes: bytes | None = None) -> ScriptObjects:
    """UE4.26/4.27: LoaderGlobalNames (+ LoaderGlobalNameHashes) + LoaderInitialLoadMeta chunks.

    Source: CUE4Parse ``IoGlobalData`` (UE4.26+ branch); retoc ``ZenScriptObjects::deserialize_old``.
    LoaderInitialLoadMeta = int32 count + FScriptObjectEntry[count] (32 bytes each).
    """
    count = None
    if global_name_hashes is not None:
        count = len(global_name_hashes) // 8 - 1
    names = read_name_batch_legacy(global_names, count)
    r = Reader(initial_load_meta)
    n = r.i32()
    entries = _read_script_entries(r, n)
    so = ScriptObjects(_script_entries_to_paths(entries, names), "LoaderInitialLoadMeta")
    so.hash_mismatches = sum(1 for raw, p in so.paths.items() if p and script_import_index(p) != raw)
    return so


def load_script_objects_ue5(script_objects_chunk: bytes) -> ScriptObjects:
    """UE5: ScriptObjects chunk = name batch + int32 count + FScriptObjectEntry[count].
    The name batch hash id is not checked here (no layout to tell apart; retoc tolerates it)."""
    r = Reader(script_objects_chunk)
    names = read_name_batch(r, strict_hash_id=False)
    n = r.i32()
    entries = _read_script_entries(r, n)
    so = ScriptObjects(_script_entries_to_paths(entries, names), "ScriptObjects")
    so.hash_mismatches = sum(1 for raw, p in so.paths.items() if p and script_import_index(p) != raw)
    return so


def load_script_map_json(path: str) -> ScriptObjects:
    """User-provided JSON {"0x<raw index hex>": "/Script/Pkg.Object", ...} or a list of paths."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    paths = {}
    if isinstance(data, list):
        for p in data:
            paths[script_import_index(p)] = p
    else:
        for k, v in data.items():
            paths[int(k, 16) if isinstance(k, str) else int(k)] = v
    return ScriptObjects(paths, f"json:{os.path.basename(path)}")


# --------------------------------------------------------------------------------------------
# Package model
# --------------------------------------------------------------------------------------------
EXPORT_MAP_ENTRY_SIZE = 72
BULK_DATA_MAP_ENTRY_SIZE = 32
LEGACY_SUMMARY_SIZE = 64

OBJECT_FLAG_NAMES = {0x1: "Public", 0x2: "Standalone", 0x4: "MarkAsNative", 0x8: "Transactional",
                     0x10: "ClassDefaultObject", 0x20: "ArchetypeObject", 0x40: "Transient"}


def object_flags_text(flags):
    return "|".join(n for b, n in OBJECT_FLAG_NAMES.items() if flags & b) or "0"


@dataclass
class ExportInfo:
    index: int
    object_name: str
    class_index: int
    class_name: str | None
    outer_index: int
    super_index: int
    template_index: int
    global_import_index_or_hash: int
    object_flags: int
    filter_flags: int
    cooked_serial_offset: int
    cooked_serial_size: int
    abs_start: int = -1
    abs_end: int = -1
    bundle: int = -1
    legacy_offset: int = -1   # offset of this export in the legacy .uasset+.uexp coordinate system
    name_ref: tuple = (-1, 0)  # ObjectName FMappedName (index into the package name map, number)

    @property
    def legacy_to_chunk_delta(self):
        """legacy (uasset+uexp) absolute offset minus chunk offset; constant inside one export.
        UE4.20+ texture SkipOffset and similar absolute offsets use legacy coordinates."""
        return self.legacy_offset - self.abs_start if self.abs_start >= 0 and self.legacy_offset >= 0 else None

    @property
    def class_display(self):
        return self.class_name or describe_obj_index(self.class_index)

    def to_json(self):
        return {
            "index": self.index, "object_name": self.object_name,
            "class": self.class_display, "class_index_raw": f"0x{self.class_index:016X}",
            "class_resolved": self.class_name is not None,
            "outer": describe_obj_index(self.outer_index),
            "super": describe_obj_index(self.super_index),
            "template": describe_obj_index(self.template_index),
            "global_import_index_or_public_export_hash": f"0x{self.global_import_index_or_hash:016X}",
            "object_flags": f"0x{self.object_flags:08X} ({object_flags_text(self.object_flags)})",
            "filter_flags": self.filter_flags,
            "cooked_serial_offset": self.cooked_serial_offset,
            "cooked_serial_size": self.cooked_serial_size,
            "abs_start": self.abs_start, "abs_end": self.abs_end,
            "legacy_offset": self.legacy_offset,
            "legacy_to_chunk_delta": self.legacy_to_chunk_delta,
            "bundle": self.bundle,
        }


@dataclass
class ZenPackage:
    layout: str                     # "ue4.26-4.27", "ue5.0-5.1", "ue5.2", "ue5.3", "ue5.4-5.5", "ue5.6+"
    ue_version_guess: str
    evidence: list
    data_size: int
    summary: dict
    names: list
    package_name: str
    source_name: str | None
    imports: list                    # raw FPackageObjectIndex values
    exports: list                    # ExportInfo
    export_bundles: list             # [{"first": i, "count": n, "serial_offset": x|None}]
    export_bundle_entries: list      # [(local_export_index, command_type)]
    imported_packages: list          # 4.26/4.27 graph data: [{"package_id": int, "arcs": [(from,to)]}]
    imported_public_export_hashes: list
    imported_package_names: list
    bulk_data_map: list
    versioning: dict | None
    header_size: int
    export_data_offset: int
    export_data_end: int
    warnings: list

    def find_export(self, key) -> ExportInfo:
        """Find an export by index (int or digit string) or by object name (case-insensitive)."""
        if isinstance(key, int) or (isinstance(key, str) and key.isdigit()):
            i = int(key)
            if 0 <= i < len(self.exports):
                return self.exports[i]
            raise KeyError(f"export index {i} out of range (0..{len(self.exports) - 1})")
        matches = [e for e in self.exports if e.object_name.lower() == str(key).lower()]
        if len(matches) != 1:
            raise KeyError(f"export name {key!r} matched {len(matches)} exports")
        return matches[0]

    def to_json(self):
        return {
            "layout": self.layout, "ue_version_guess": self.ue_version_guess,
            "evidence": self.evidence, "data_size": self.data_size,
            "package_name": self.package_name, "source_name": self.source_name,
            "summary": self.summary, "header_size": self.header_size,
            "cooked_header_size": self.summary.get("CookedHeaderSize"),
            "export_data_offset": self.export_data_offset, "export_data_end": self.export_data_end,
            "names": self.names,
            "imports": [describe_obj_index(i) for i in self.imports],
            "exports": [e.to_json() for e in self.exports],
            "export_bundles": self.export_bundles,
            "export_bundle_entries": [list(x) for x in self.export_bundle_entries],
            "imported_packages": [{"package_id": f"0x{p['package_id']:016X}", "arcs": p["arcs"]}
                                  for p in self.imported_packages],
            "imported_public_export_hashes": [f"0x{h:016X}" for h in self.imported_public_export_hashes],
            "imported_package_names": self.imported_package_names,
            "bulk_data_map": self.bulk_data_map, "versioning": self.versioning,
            "warnings": self.warnings,
        }


def _mapped_text(mn: MappedName, names, global_names=None):
    if mn.kind == "Package":
        if mn.index < len(names):
            return format_fname(names[mn.index], mn.number)
        return f"<bad name index {mn.index}>"
    if mn.kind == "Global" and global_names is not None and mn.index < len(global_names):
        return format_fname(global_names[mn.index], mn.number)
    return f"<{mn.kind} name #{mn.index}_{mn.number}>"


def _read_export_map(data, offset, count, is_ue4):
    r = Reader(data, offset)
    out = []
    for i in range(count):
        start = r.pos
        off = r.u64()
        size = r.u64()
        name = MappedName.read(r)
        outer, cls, sup, tmpl, gidx = r.u64(), r.u64(), r.u64(), r.u64(), r.u64()
        flags = r.u32()
        filt = r.u8()
        r.pos = start + EXPORT_MAP_ENTRY_SIZE
        out.append((off, size, name, outer, cls, sup, tmpl, gidx, flags, filt))
    return out


def _load_export_bundles_legacy(data, offset, end):
    """CUE4Parse ``IoPackage.LoadExportBundles``: infer the header count from the region size."""
    r = Reader(data, offset)
    remaining = (end - offset) // 8
    found = 0
    headers = []
    while found < remaining:
        remaining -= 1
        first, count = r.u32(), r.u32()
        if count > 1_000_000:
            raise ZenFormatError("implausible export bundle entry count")
        headers.append({"first": first, "count": count, "serial_offset": None})
        found += count
    if found != remaining:
        raise ZenFormatError(f"export bundle entries {found} != remaining slots {remaining}")
    entries = [(r.u32(), r.u32()) for _ in range(found)]
    return headers, entries


def _read_graph_data_legacy(data, offset, size):
    """4.26/4.27 graph data: int32 ImportedPackageCount, then per package FPackageId (u64),
    int32 ArcCount, ArcCount x (int32 FromExportBundle, int32 ToExportBundle).
    Source: CUE4Parse ``IoPackage.LoadGraphData``; retoc zen.rs (old style graph data)."""
    r = Reader(data, offset)
    count = r.i32()
    if count < 0 or count > 100000:
        raise ZenFormatError("implausible imported package count in graph data")
    pkgs = []
    for _ in range(count):
        pid = r.u64()
        n = r.i32()
        if n < 0 or n > 1_000_000:
            raise ZenFormatError("implausible arc count")
        arcs = [(r.i32(), r.i32()) for _ in range(n)]
        pkgs.append({"package_id": pid, "arcs": arcs})
    if r.pos - offset != size:
        raise ZenFormatError(f"graph data consumed {r.pos - offset} of {size} bytes")
    return pkgs


def _resolve_class(raw, names_exports, script_objects, imported_pkg_names, imported_hashes, layout):
    kind = obj_index_kind(raw)
    if kind == "Null":
        return None
    if kind == "Export":
        i = raw & 0xFFFFFFFF
        return f"(export {i}) {names_exports[i]}" if i < len(names_exports) else None
    if kind == "ScriptImport":
        if script_objects is not None:
            p = script_objects.resolve(raw)
            if p:
                return p
        return builtin_script_objects().get(raw)
    if kind == "PackageImport" and layout.startswith("ue5"):
        pkg_i = (raw & ((1 << 62) - 1)) >> 32
        hash_i = raw & 0xFFFFFFFF
        pkg = imported_pkg_names[pkg_i] if pkg_i < len(imported_pkg_names) else f"package#{pkg_i}"
        h = imported_hashes[hash_i] if hash_i < len(imported_hashes) else None
        return f"{pkg}:export_hash=0x{h:016X}" if h is not None else None
    return None


PACKAGE_FILE_TAG = b"\xC1\x83\x2A\x9E"


def _note_trailer(data, end, ev, warnings):
    """Exports must end at the chunk end; tools that convert from .uasset/.uexp sometimes keep the
    4-byte PACKAGE_FILE_TAG (0x9E2A83C1) that terminates a legacy .uexp."""
    n = len(data)
    if end == n:
        ev.append("export ranges end exactly at the chunk end")
    elif n - end == 4 and data[end:] == PACKAGE_FILE_TAG:
        ev.append("export ranges end at the chunk end (+4-byte PACKAGE_FILE_TAG trailer)")
    else:
        warnings.append(f"{n - end} trailing bytes after the last export")


def _check_bundle_entries(entries, export_count):
    """Every export needs exactly one Create (0) and one Serialize (1) command
    (EExportCommandType; retoc ``FZenPackageHeader::deserialize`` bails otherwise). An export
    without a Serialize entry would have no byte range at all."""
    seen = {0: [0] * export_count, 1: [0] * export_count}
    for idx, cmd in entries:
        if cmd not in (0, 1):
            raise ZenFormatError(f"export bundle entry command {cmd} is not Create/Serialize")
        if idx >= export_count:
            raise ZenFormatError(f"bundle entry references export {idx} of {export_count}")
        seen[cmd][idx] += 1
    bad = [i for i in range(export_count) if seen[0][i] != 1 or seen[1][i] != 1]
    if bad:
        raise ZenFormatError(f"exports {bad[:8]} do not have exactly one Create and one Serialize entry")


def _assign_export_ranges_bundled(exports, bundles, entries, start, warnings=None):
    """Exports are laid out back to back in bundle order (CUE4Parse ``PopulateExports``). UE5.0-5.2
    bundle headers also carry SerialOffset (relative to the header end); it is only cross-checked."""
    pos = start
    covered = 0
    for bi, b in enumerate(bundles):
        if b.get("serial_offset") is not None and start + b["serial_offset"] != pos and warnings is not None:
            warnings.append(f"bundle {bi} SerialOffset {b['serial_offset']} != sequential {pos - start}")
        if b["first"] + b["count"] > len(entries):
            raise ZenFormatError(f"bundle {bi} entries {b['first']}+{b['count']} past {len(entries)}")
        covered += b["count"]
        for k in range(b["first"], b["first"] + b["count"]):
            idx, cmd = entries[k]
            if cmd != 1:
                continue
            if idx >= len(exports):
                raise ZenFormatError(f"bundle entry references export {idx} of {len(exports)}")
            e = exports[idx]
            if e.abs_start >= 0:
                raise ZenFormatError(f"export {idx} is serialized twice")
            e.abs_start = pos
            e.abs_end = pos + e.cooked_serial_size
            e.bundle = bi
            pos = e.abs_end
    if covered != len(entries):
        raise ZenFormatError(f"export bundles cover {covered} of {len(entries)} entries")
    return pos


def _check_export_ranges(exports, data_start, n, ev, warnings, cells_note=False):
    """Export byte ranges must lie inside [data_start, n) and must not overlap. Gaps are reported
    (UE5.6+ Verse cell exports live in the same region, so gaps there are expected when the
    package has cells)."""
    rs = sorted((e.abs_start, e.abs_end, e.index) for e in exports)
    for a, b, i in rs:
        if a < data_start or b > n or b < a:
            raise ZenFormatError(f"export {i} range [{a}, {b}) outside the export data [{data_start}, {n})")
    for (a1, b1, i1), (a2, b2, i2) in zip(rs, rs[1:]):
        if a2 < b1:
            raise ZenFormatError(f"exports {i1} [{a1}, {b1}) and {i2} [{a2}, {b2}) overlap")
    gaps = [(b1, a2) for (a1, b1, _), (a2, b2, _) in zip(rs, rs[1:]) if a2 > b1]
    if rs and rs[0][0] > data_start:
        gaps.insert(0, (data_start, rs[0][0]))
    if gaps:
        msg = f"{len(gaps)} gap(s) between export ranges, first {gaps[0]}"
        warnings.append(msg + (" (Verse cell exports may occupy them)" if cells_note else ""))
    else:
        ev.append("export ranges are contiguous and non-overlapping from the export data start")


def _build_exports(raw_exports, names, script_objects, layout, imported_pkg_names=(), imported_hashes=()):
    export_names = [_mapped_text(x[2], names) for x in raw_exports]
    out = []
    for i, (off, size, name, outer, cls, sup, tmpl, gidx, flags, filt) in enumerate(raw_exports):
        out.append(ExportInfo(
            index=i, object_name=export_names[i], class_index=cls,
            class_name=_resolve_class(cls, export_names, script_objects, imported_pkg_names, imported_hashes, layout),
            outer_index=outer, super_index=sup, template_index=tmpl,
            global_import_index_or_hash=gidx, object_flags=flags, filter_flags=filt,
            cooked_serial_offset=off, cooked_serial_size=size,
            name_ref=(name.index, name.number) if name.kind == "Package" else (-1, 0)))
    return out


def parse_legacy(data: bytes, script_objects: ScriptObjects | None = None,
                 strict_hash_id: bool = True) -> ZenPackage:
    """Parse a UE4.26/4.27 Zen package (``FPackageSummary`` layout, EIoContainerHeaderVersion::Initial).

    ``strict_hash_id=False`` accepts a name-hash algorithm id other than 0xC1640000 (reported in
    ``warnings``); ``parse_package`` only does that when no layout parses strictly."""
    if len(data) < LEGACY_SUMMARY_SIZE:
        raise ZenFormatError("too small for FPackageSummary")
    r = Reader(data)
    name = MappedName.read(r)
    source_name = MappedName.read(r)
    s = {
        "PackageFlags": r.u32(), "CookedHeaderSize": r.u32(),
        "NameMapNamesOffset": r.i32(), "NameMapNamesSize": r.i32(),
        "NameMapHashesOffset": r.i32(), "NameMapHashesSize": r.i32(),
        "ImportMapOffset": r.i32(), "ExportMapOffset": r.i32(), "ExportBundlesOffset": r.i32(),
        "GraphDataOffset": r.i32(), "GraphDataSize": r.i32(), "Pad": r.i32(),
    }
    ev = []
    n = len(data)
    if s["NameMapNamesOffset"] < LEGACY_SUMMARY_SIZE:
        raise ZenFormatError("NameMapNamesOffset inside the summary")
    order = [s["NameMapNamesOffset"], s["NameMapNamesOffset"] + s["NameMapNamesSize"], s["NameMapHashesOffset"],
             s["NameMapHashesOffset"] + s["NameMapHashesSize"], s["ImportMapOffset"], s["ExportMapOffset"],
             s["ExportBundlesOffset"], s["GraphDataOffset"], s["GraphDataOffset"] + s["GraphDataSize"]]
    if any(b < a for a, b in zip(order, order[1:])) or order[-1] > n or min(order) < 0:
        raise ZenFormatError(f"legacy summary offsets not monotonic / out of range: {order}")
    if s["NameMapHashesSize"] % 8 or (s["ExportMapOffset"] - s["ImportMapOffset"]) % 8:
        raise ZenFormatError("misaligned name hash / import map sizes")
    if (s["ExportBundlesOffset"] - s["ExportMapOffset"]) % EXPORT_MAP_ENTRY_SIZE:
        raise ZenFormatError("export map size is not a multiple of 72")
    ev.append("FPackageSummary offsets monotonic and within the chunk")
    name_count = s["NameMapHashesSize"] // 8 - 1 if s["NameMapHashesSize"] else 0
    hr = Reader(data, s["NameMapHashesOffset"])
    hashes = []
    warnings = []
    if s["NameMapHashesSize"]:
        algo = hr.u64()
        if algo != FNAME_HASH_ALGORITHM_ID:
            if strict_hash_id:
                raise ZenFormatError(f"name hash algorithm {algo:#x} != 0xC1640000")
            warnings.append(f"non-standard FName hash algorithm id {algo:#x} (stock UE: 0xC1640000)")
        else:
            ev.append("name hash algorithm id 0xC1640000")
        hashes = [hr.u64() for _ in range(name_count)]
    names_buf = data[s["NameMapNamesOffset"]:s["NameMapNamesOffset"] + s["NameMapNamesSize"]]
    names = read_name_batch_legacy(names_buf, name_count)
    bad = sum(1 for nm, h in zip(names, hashes) if name_hash(nm) != h)
    if bad:
        warnings.append(f"{bad}/{len(names)} name hashes do not match CityHash64(lower(name))")
    else:
        ev.append(f"all {len(names)} name hashes match CityHash64(lower(name))")
    imports = list(struct.unpack_from(f"<{(s['ExportMapOffset'] - s['ImportMapOffset']) // 8}Q", data, s["ImportMapOffset"]))
    export_count = (s["ExportBundlesOffset"] - s["ExportMapOffset"]) // EXPORT_MAP_ENTRY_SIZE
    raw_exports = _read_export_map(data, s["ExportMapOffset"], export_count, True)
    bundles, entries = _load_export_bundles_legacy(data, s["ExportBundlesOffset"], s["GraphDataOffset"])
    if len(entries) != 2 * export_count:
        raise ZenFormatError(f"{len(entries)} export bundle entries for {export_count} exports (expected 2 per export)")
    _check_bundle_entries(entries, export_count)
    ev.append("export bundle entries = 2 x export count, one Create + one Serialize per export")
    graph = _read_graph_data_legacy(data, s["GraphDataOffset"], s["GraphDataSize"])
    ev.append("graph data consumed exactly GraphDataSize bytes")
    exports = _build_exports(raw_exports, names, script_objects, "ue4")
    for e in exports:
        e.legacy_offset = e.cooked_serial_offset
    start = s["GraphDataOffset"] + s["GraphDataSize"]
    end = _assign_export_ranges_bundled(exports, bundles, entries, start)
    if end > n:
        raise ZenFormatError(f"export data runs past the chunk end ({end} > {n})")
    _check_export_ranges(exports, start, n, ev, warnings)
    _note_trailer(data, end, ev, warnings)
    if exports and exports[0].cooked_serial_offset == s["CookedHeaderSize"]:
        ev.append("first export CookedSerialOffset == CookedHeaderSize")
    return ZenPackage(
        layout="ue4.26-4.27",
        ue_version_guess="UE4.26 or UE4.27 (legacy Zen FPackageSummary; .utoc version 2 => 4.26, 3 => 4.27)",
        evidence=ev, data_size=n, summary=s, names=names,
        package_name=_mapped_text(name, names), source_name=_mapped_text(source_name, names),
        imports=imports, exports=exports, export_bundles=bundles, export_bundle_entries=entries,
        imported_packages=graph, imported_public_export_hashes=[], imported_package_names=[],
        bulk_data_map=[], versioning=None, header_size=start, export_data_offset=start,
        export_data_end=end, warnings=warnings)


# UE5 layout variants, oldest first: (name, summary_size, has_bulk_map, has_pad, has_cells)
UE5_LAYOUTS = [
    ("ue5.0-5.1", 44, False, False, False),
    ("ue5.2", 44, True, False, False),
    ("ue5.3", 52, True, False, False),
    ("ue5.4-5.5", 52, True, True, False),
    ("ue5.6+", 52, True, True, True),
]

# EUnrealEngineObjectUE5Version -> engine release. Shipped values (retoc ``version.rs``
# EngineVersion::object_ue5_version): 5.0 = 1004 LARGE_WORLD_COORDINATES, 5.1 = 1008
# ADD_SOFTOBJECTPATH_LIST, 5.2 AND 5.3 = 1009 DATA_RESOURCES (told apart by the 44/52-byte summary),
# 5.4 = 1012, 5.5 = 1013, 5.6 = 1017, 5.7 = 1018; values in between come from development builds
# of the next release.
UE5_VERSION_NAMES = {1000: "5.0EA", 1001: "5.0", 1002: "5.0", 1003: "5.0", 1004: "5.0", 1005: "5.1",
                     1006: "5.1", 1007: "5.1", 1008: "5.1", 1009: "5.2-5.3", 1010: "5.4", 1011: "5.4",
                     1012: "5.4", 1013: "5.5", 1014: "5.6", 1015: "5.6", 1016: "5.6", 1017: "5.6",
                     1018: "5.7"}


def ue5_layout_for_version(summary_size: int, file_version_ue5: int) -> str:
    """The only ``UE5_LAYOUTS`` entry consistent with a versioned package: the summary size picks
    44 (5.0-5.2) vs 52 bytes (5.3+); DATA_RESOURCES=1009 adds the bulk data map,
    PROPERTY_TAG_COMPLETE_TYPE_NAME=1012 the pad before it, VERSE_CELLS=1015 the cell offsets
    (retoc ``zen.rs`` FZenPackageHeader::serialize, CUE4Parse ``IoPackage.ReadRealm``)."""
    if summary_size == 44:
        return "ue5.2" if file_version_ue5 >= 1009 else "ue5.0-5.1"
    if file_version_ue5 >= 1015:
        return "ue5.6+"
    return "ue5.4-5.5" if file_version_ue5 >= 1012 else "ue5.3"


def parse_ue5(data: bytes, layout: str, script_objects: ScriptObjects | None = None,
              strict_hash_id: bool = True) -> ZenPackage:
    """Parse a UE5 Zen package with one of ``UE5_LAYOUTS``; raises ZenFormatError when inconsistent.

    With versioning info present the object version decides bulk map / padding / cells
    (DATA_RESOURCES=1009, PROPERTY_TAG_COMPLETE_TYPE_NAME=1012, VERSE_CELLS=1015); ``layout`` must
    then be the one ``ue5_layout_for_version`` derives, so exactly one layout is accepted.
    """
    spec = {x[0]: x for x in UE5_LAYOUTS}[layout]
    _, ssize, has_bulk, has_pad, has_cells = spec
    n = len(data)
    r = Reader(data)
    s = {"bHasVersioningInfo": r.u32(), "HeaderSize": r.u32()}
    name = MappedName.read(r)
    s.update({"PackageFlags": r.u32(), "CookedHeaderSize": r.u32(),
              "ImportedPublicExportHashesOffset": r.i32(), "ImportMapOffset": r.i32(),
              "ExportMapOffset": r.i32(), "ExportBundleEntriesOffset": r.i32()})
    if ssize == 52:
        s.update({"DependencyBundleHeadersOffset": r.i32(), "DependencyBundleEntriesOffset": r.i32(),
                  "ImportedPackageNamesOffset": r.i32()})
    else:
        s["GraphDataOffset"] = r.i32()
    ev = []
    if s["bHasVersioningInfo"] not in (0, 1):
        raise ZenFormatError("bHasVersioningInfo not 0/1")
    hs = s["HeaderSize"]
    if not (ssize <= hs <= n):
        raise ZenFormatError("HeaderSize out of range")
    order = [ssize, s["ImportedPublicExportHashesOffset"], s["ImportMapOffset"], s["ExportMapOffset"],
             s["ExportBundleEntriesOffset"]]
    if ssize == 52:
        order += [s["DependencyBundleHeadersOffset"], s["DependencyBundleEntriesOffset"], s["ImportedPackageNamesOffset"]]
    else:
        order += [s["GraphDataOffset"]]
    order.append(hs)
    if any(b < a for a, b in zip(order, order[1:])):
        raise ZenFormatError(f"UE5 summary offsets not monotonic: {order}")
    if (s["ImportMapOffset"] - s["ImportedPublicExportHashesOffset"]) % 8 or (s["ExportMapOffset"] - s["ImportMapOffset"]) % 8:
        raise ZenFormatError("misaligned import hash / import map sizes")
    versioning = None
    if s["bHasVersioningInfo"]:
        zen_ver = r.u32()
        ue4v, ue5v, lic = r.i32(), r.i32(), r.i32()
        cnt = r.i32()
        if cnt < 0 or cnt > 10000:
            raise ZenFormatError("implausible custom version count")
        customs = []
        for _ in range(cnt):
            guid = r.take(16)
            customs.append({"guid": guid.hex(), "version": r.i32()})
        versioning = {"zen_version": zen_ver, "file_version_ue4": ue4v, "file_version_ue5": ue5v,
                      "licensee_version": lic, "custom_versions": customs}
        if not 1000 <= ue5v < 2000:
            raise ZenFormatError(f"versioned package with implausible UE5 object version {ue5v}")
        want = ue5_layout_for_version(ssize, ue5v)
        if want != layout:
            raise ZenFormatError(f"versioned package (UE5 ver {ue5v}, {ssize}-byte summary) has layout {want}")
        has_bulk = ue5v >= 1009
        has_pad = ue5v >= 1012
        has_cells = ue5v >= 1015
        ev.append(f"versioning info present: UE4 ver {ue4v}, UE5 ver {ue5v} -> layout {want}")
    cell_import = cell_export = s["ExportBundleEntriesOffset"]
    if has_cells:
        cell_import, cell_export = r.i32(), r.i32()
        if not (s["ExportMapOffset"] <= cell_import <= cell_export <= s["ExportBundleEntriesOffset"]):
            raise ZenFormatError("cell map offsets inconsistent")
    warnings = []
    name_hash_pos = r.pos + 8
    names = read_name_batch(r, strict_hash_id=strict_hash_id)
    if not names:
        raise ZenFormatError("empty package name map")
    hash_id = struct.unpack_from("<Q", data, name_hash_pos)[0]
    if hash_id != FNAME_HASH_ALGORITHM_ID:
        warnings.append(f"non-standard FName hash algorithm id {hash_id:#x} (stock UE: 0xC1640000)")
    ev.append(f"UE5 name batch decoded ({len(names)} names)")
    bulk_map = []
    if has_bulk:
        if has_pad:
            pad = r.u64()
            if pad > 7:
                raise ZenFormatError("bulk data pad > 7")
            r.take(pad)
        bsize = r.i64()
        if bsize < 0 or bsize % BULK_DATA_MAP_ENTRY_SIZE or r.pos + bsize > s["ImportedPublicExportHashesOffset"]:
            raise ZenFormatError("bulk data map size inconsistent")
        for _ in range(bsize // BULK_DATA_MAP_ENTRY_SIZE):
            off, dup, size, flags = r.i64(), r.i64(), r.i64(), r.u32()
            cooked_index = r.u8()
            r.take(3)
            bulk_map.append({"serial_offset": off, "duplicate_serial_offset": dup, "serial_size": size,
                             "flags": f"0x{flags:08X}", "cooked_index": cooked_index})
    gap = s["ImportedPublicExportHashesOffset"] - r.pos
    if not (0 <= gap < 8):
        raise ZenFormatError(f"header sections do not abut (gap {gap} before imported public export hashes)")
    ev.append("summary/name batch/bulk map abut ImportedPublicExportHashesOffset")
    h_count = (s["ImportMapOffset"] - s["ImportedPublicExportHashesOffset"]) // 8
    imported_hashes = list(struct.unpack_from(f"<{h_count}Q", data, s["ImportedPublicExportHashesOffset"]))
    imports = list(struct.unpack_from(f"<{(s['ExportMapOffset'] - s['ImportMapOffset']) // 8}Q", data, s["ImportMapOffset"]))
    if (cell_import - s["ExportMapOffset"]) % EXPORT_MAP_ENTRY_SIZE:
        raise ZenFormatError("export map size is not a multiple of 72")
    export_count = (cell_import - s["ExportMapOffset"]) // EXPORT_MAP_ENTRY_SIZE
    raw_exports = _read_export_map(data, s["ExportMapOffset"], export_count, False)
    eb_end = s["GraphDataOffset"] if ssize == 44 else s["DependencyBundleHeadersOffset"]
    if (eb_end - s["ExportBundleEntriesOffset"]) % 8:
        raise ZenFormatError("export bundle entries region not a multiple of 8")
    eb_count = (eb_end - s["ExportBundleEntriesOffset"]) // 8
    if eb_count != 2 * export_count:
        raise ZenFormatError(f"{eb_count} export bundle entries for {export_count} exports")
    er = Reader(data, s["ExportBundleEntriesOffset"])
    entries = [(er.u32(), er.u32()) for _ in range(eb_count)]
    _check_bundle_entries(entries, export_count)
    ev.append("export bundle entries = 2 x export count, one Create + one Serialize per export")
    imported_pkg_names = []
    bundles = []
    if ssize == 52:
        dep_count = (s["DependencyBundleEntriesOffset"] - s["DependencyBundleHeadersOffset"]) // 20
        if dep_count != export_count:
            raise ZenFormatError(f"{dep_count} dependency bundle headers for {export_count} exports")
        ev.append("dependency bundle headers = export count")
        if s["ImportedPackageNamesOffset"] < hs:
            pr = Reader(data, s["ImportedPackageNamesOffset"])
            base = read_name_batch(pr)
            nums = [pr.i32() for _ in base]
            imported_pkg_names = [format_fname(b, k) for b, k in zip(base, nums)]
        bundles = [{"first": 0, "count": len(entries), "serial_offset": None}]
    else:
        # 5.0-5.2: export bundle headers (SerialOffset u64, FirstEntryIndex u32, EntryCount u32) at
        # GraphDataOffset; their count lives in the container store entry, so read until all
        # entries are covered (each header continues where the previous one ended).
        gr = Reader(data, s["GraphDataOffset"])
        covered = 0
        while covered < len(entries):
            so, first, cnt = gr.u64(), gr.u32(), gr.u32()
            if first != covered or cnt == 0 or covered + cnt > len(entries):
                raise ZenFormatError("export bundle headers inconsistent")
            bundles.append({"first": first, "count": cnt, "serial_offset": so})
            covered += cnt
        ev.append(f"{len(bundles)} export bundle header(s) at GraphDataOffset cover all entries")
    exports = _build_exports(raw_exports, names, script_objects, "ue5", imported_pkg_names, imported_hashes)
    if ssize == 44:
        end = _assign_export_ranges_bundled(exports, bundles, entries, hs, warnings)
    else:
        end = hs
        for e in exports:
            e.abs_start = hs + e.cooked_serial_offset
            e.abs_end = e.abs_start + e.cooked_serial_size
            end = max(end, e.abs_end)
    if end > n:
        raise ZenFormatError(f"export data runs past the chunk end ({end} > {n})")
    _check_export_ranges(exports, hs, n, ev, warnings, cells_note=cell_export < s["ExportBundleEntriesOffset"])
    for e in exports:
        # UE5 CookedSerialOffset excludes the cooked header (retoc zen_asset_conversion.rs
        # build_zen_export_map). Verified against real 5.4-5.6 packages; for 5.0-5.2 CUE4Parse
        # treats the field as absolute instead, so legacy_offset there is unverified.
        e.legacy_offset = s["CookedHeaderSize"] + e.cooked_serial_offset
    _note_trailer(data, end, ev, warnings)
    if versioning:
        name_ = UE5_VERSION_NAMES.get(versioning["file_version_ue5"], "5.x")
        if name_ == "5.2-5.3":
            name_ = "5.2" if ssize == 44 else "5.3"
        guess = f"UE{name_}"
    else:
        guess = {"ue5.0-5.1": "UE5.0-5.1", "ue5.2": "UE5.2", "ue5.3": "UE5.3", "ue5.4-5.5": "UE5.4-5.5",
                 "ue5.6+": "UE5.6+"}[layout]
    return ZenPackage(
        layout=layout, ue_version_guess=f"{guess} (FZenPackageSummary, {'versioned' if versioning else 'unversioned: structural heuristic'})",
        evidence=ev, data_size=n, summary=s, names=names, package_name=_mapped_text(name, names),
        source_name=None, imports=imports, exports=exports, export_bundles=bundles,
        export_bundle_entries=entries, imported_packages=[], imported_public_export_hashes=imported_hashes,
        imported_package_names=imported_pkg_names, bulk_data_map=bulk_map, versioning=versioning,
        header_size=hs, export_data_offset=hs, export_data_end=end, warnings=warnings)


def parse_package(data: bytes, script_objects: ScriptObjects | None = None, layout: str = "auto") -> ZenPackage:
    """Parse a Zen package chunk, detecting the layout heuristically when ``layout == 'auto'``.

    Every layout is tried; a layout is accepted only if all its structural checks pass. When
    several pass (rare), the one with the most evidence wins and the others are listed in
    ``warnings``. If nothing parses with the stock FName hash algorithm id (0xC1640000), the
    layouts are retried accepting any id (retoc does the same for games that changed it); the
    result then carries a warning and one discriminating check fewer.
    """
    def attempt(strict):
        tries = []
        if layout in ("auto", "ue4", "ue4.26-4.27"):
            tries.append(("ue4.26-4.27", lambda: parse_legacy(data, script_objects, strict)))
        for name, *_ in UE5_LAYOUTS:
            if layout in ("auto", "ue5", name):
                tries.append((name, lambda nm=name: parse_ue5(data, nm, script_objects, strict)))
        if not tries:
            raise ValueError(f"unknown layout {layout!r}")
        ok, errors = [], []
        for name, fn in tries:
            try:
                ok.append(fn())
            except (ZenFormatError, struct.error, UnicodeDecodeError, KeyError, IndexError) as e:
                errors.append(f"{name}: {e}")
        return ok, errors

    ok, errors = attempt(True)
    if not ok:
        ok, relaxed_errors = attempt(False)
        for p in ok:
            p.warnings.append("parsed only after relaxing the FName hash algorithm id check")
        if not ok:
            raise ZenFormatError("no package layout matched:\n  " + "\n  ".join(errors))
    ok.sort(key=lambda p: len(p.evidence) - len(p.warnings), reverse=True)
    best = ok[0]
    if len(ok) > 1:
        best.warnings.append("other layouts also parsed: " + ", ".join(p.layout for p in ok[1:]))
    best.evidence.append(f"rejected layouts: {len(errors)}")
    return best


# --------------------------------------------------------------------------------------------
# Renaming a UE4.26/4.27 package (used by ``iostore.py clone-package``)
# --------------------------------------------------------------------------------------------
# FPackageSummary field offsets (CUE4Parse FPackageSummary.cs; see parse_legacy).
_SUM_NAMES_SIZE = 28
_SUM_HASHES_OFF = 32
_SUM_HASHES_SIZE = 36
_SUM_SHIFTED = (40, 44, 48, 52)   # ImportMapOffset, ExportMapOffset, ExportBundlesOffset, GraphDataOffset
_EXPORT_GLOBAL_IMPORT_OFF = 56    # FExportMapEntry: 8+8+8 + Outer/Class/Super/Template (4 x 8) -> GlobalImportIndex
_SUMMARY_FIELD_NAMES = ["Name", "SourceName", "PackageFlags", "CookedHeaderSize", "NameMapNamesOffset",
                        "NameMapNamesSize", "NameMapHashesOffset", "NameMapHashesSize", "ImportMapOffset",
                        "ExportMapOffset", "ExportBundlesOffset", "GraphDataOffset", "GraphDataSize", "Pad"]


class RenameError(ValueError):
    """The package cannot be renamed safely (layout not understood, formula not confirmed, ...)."""


def lower_ascii(text: str) -> str:
    """UE4 FChar::ToLower / CUE4Parse FPackageId.ToLower / retoc to_ascii_lowercase: only A-Z."""
    return "".join(c.lower() if "A" <= c <= "Z" else c for c in text)


def package_id_from_name(package_name: str) -> int:
    """FPackageId::FromName: CityHash64 of the UTF-16LE bytes of the name with ASCII letters
    lower-cased (CUE4Parse ``FPackageId.FromName``, retoc ``lib.rs`` ``FPackageId::from_name``).
    The same rule is used by every UE version CUE4Parse supports, UE4.26 included."""
    return cityhash64(lower_ascii(package_name).encode("utf-16-le"))


def legacy_export_object_paths(pkg: ZenPackage, package_name: str, names: list | None = None) -> list:
    """Full object path of every export of a 4.26/4.27 package, e.g. ``/Game/P.Asset`` or
    ``/Game/P.Asset:Sub``; ``None`` when the outer chain leaves the package (outer is an import).
    ``names`` overrides the name map used for the object names (to compute paths after renames).
    Only the segment boundaries matter for ``object_path_hash`` ('.' and ':' both become '/')."""
    names = pkg.names if names is None else names
    n = len(pkg.exports)
    obj_names = []
    for e in pkg.exports:
        idx, number = e.name_ref
        obj_names.append(format_fname(names[idx], number) if 0 <= idx < len(names) else None)
    paths: list = [None] * n

    def path_of(i, depth=0):
        if paths[i] is not None:
            return paths[i]
        if depth > n or obj_names[i] is None:
            return None
        e = pkg.exports[i]
        kind = obj_index_kind(e.outer_index)
        if kind == "Null":
            paths[i] = f"{package_name}.{obj_names[i]}"
        elif kind == "Export":
            parent = e.outer_index & 0xFFFFFFFF
            if parent >= n or parent == i:
                return None
            pp = path_of(parent, depth + 1)
            if pp is None:
                return None
            # UE GetPathName: ':' after a top-level object, '.' elsewhere (irrelevant for the hash)
            paths[i] = f"{pp}:{obj_names[i]}" if ":" not in pp else f"{pp}.{obj_names[i]}"
        return paths[i]

    for i in range(n):
        path_of(i)
    return paths


def legacy_public_export_index_map(pkg: ZenPackage, old_name: str, new_name: str, new_names: list | None = None) -> dict:
    """{old GlobalImportIndex: new GlobalImportIndex} for the public exports of ``pkg`` when the
    package ``old_name`` becomes ``new_name``. Raises RenameError unless every stored non-null
    GlobalImportIndex equals the recomputed hash of its old object path (the formula is taken from
    retoc ``zen_asset_conversion.rs`` build_zen_export_map: PackageImport(hash(full export path)))."""
    old_paths = legacy_export_object_paths(pkg, old_name)
    new_paths = legacy_export_object_paths(pkg, new_name, new_names if new_names is not None else pkg.names)
    out, bad = {}, []
    for e in pkg.exports:
        g = e.global_import_index_or_hash
        if g == NULL_OBJECT_INDEX:
            continue
        op, np_ = old_paths[e.index], new_paths[e.index]
        if op is None or np_ is None or legacy_package_import_index(op) != g:
            bad.append({"export": e.index, "name": e.object_name, "stored": f"0x{g:016X}",
                        "recomputed_from": op,
                        "recomputed": f"0x{legacy_package_import_index(op):016X}" if op else None})
            continue
        out[g] = legacy_package_import_index(np_)
    if bad:
        raise RenameError("GlobalImportIndex of public exports does not equal PackageImport(CityHash64(lower "
                          "object path)) for this package, so the new values cannot be derived safely: "
                          + json.dumps(bad[:4]))
    return out


def rename_legacy_package(data: bytes, new_name: str, *, old_name: str | None = None,
                          import_index_map: dict | None = None, package_id_map: dict | None = None,
                          name_renames: dict | None = None) -> tuple:
    """Rename a UE4.26/4.27 Zen package (the ExportBundleData chunk) to ``new_name``.

    Returns ``(new_bytes, report)``. Exactly these things change (everything else is copied byte
    for byte and checked afterwards):

    1. Name map: the entry that ``FPackageSummary.Name`` points at (the package name) becomes
       ``new_name``; ``SourceName`` too when it points at an entry with the same text. Optional
       ``name_renames`` {old text: new text} rename further entries (e.g. the main export's object
       name). The name batch and its CityHash64 hash table are re-encoded, which changes
       ``NameMapNamesSize`` and ``NameMapHashesSize``.
    2. Every section after the name map moves by the same byte delta, so ``NameMapHashesOffset``,
       ``ImportMapOffset``, ``ExportMapOffset``, ``ExportBundlesOffset`` and ``GraphDataOffset`` are
       shifted. ``PackageFlags``, ``CookedHeaderSize``, ``GraphDataSize``, ``Pad`` and the
       ``Name``/``SourceName`` FMappedName values are unchanged.
    3. ``FExportMapEntry.GlobalImportIndex`` of every public export is recomputed from the new
       object path (it is a hash of ``/package/path.Object``; see legacy_public_export_index_map).
       ``CookedSerialOffset``/``CookedSerialSize`` (legacy .uasset+.uexp coordinates) are unchanged:
       the export bytes do not move relative to each other, so offsets inside exports stay valid.
    4. Import map entries found in ``import_index_map`` (GlobalImportIndex of exports of other
       packages that are renamed together with this one) are replaced.
    5. Graph data: imported FPackageIds found in ``package_id_map`` are replaced (arcs unchanged).

    The serialized export data is not modified. Refuses (RenameError) when the package is not in
    the canonical layout this function understands (name map at offset 64, hashes 8-aligned right
    after it, import map right after the hashes, name batch re-encodes byte-identically)."""
    try:
        pkg = parse_legacy(data)
    except ZenFormatError as e:
        raise RenameError(f"not a UE4.26/4.27 Zen package this tool can parse: {e}") from None
    import_index_map = dict(import_index_map or {})
    package_id_map = dict(package_id_map or {})
    name_renames = dict(name_renames or {})
    s = pkg.summary
    name_mn = MappedName(*struct.unpack_from("<II", data, 0))
    src_mn = MappedName(*struct.unpack_from("<II", data, 8))
    if name_mn.kind != "Package" or name_mn.number != 0 or name_mn.index >= len(pkg.names):
        raise RenameError(f"FPackageSummary.Name is {name_mn} (expected a package-local name, number 0)")
    old = pkg.names[name_mn.index]
    if old_name is not None and lower_ascii(old) != lower_ascii(old_name):
        raise RenameError(f"package header names {old!r}, not {old_name!r}")
    if not new_name or not new_name.startswith("/") or new_name.endswith("/"):
        raise RenameError(f"new package name {new_name!r} must look like /Root/Path/Name")
    warnings = list(pkg.warnings)

    # --- canonical-layout guards -------------------------------------------------------------
    no, ns = s["NameMapNamesOffset"], s["NameMapNamesSize"]
    ho, hs = s["NameMapHashesOffset"], s["NameMapHashesSize"]
    if no != LEGACY_SUMMARY_SIZE:
        raise RenameError(f"NameMapNamesOffset {no} != {LEGACY_SUMMARY_SIZE}")
    if ho != no + ns + ((-(no + ns)) % 8) or any(data[no + ns:ho]):
        raise RenameError("name hashes do not follow the names at the next 8-byte boundary with zero padding")
    if s["ImportMapOffset"] != ho + hs:
        raise RenameError("import map does not directly follow the name hashes")
    nb, hb = write_name_batch_legacy(pkg.names)
    if nb != data[no:no + ns] or hb != data[ho:ho + hs]:
        raise RenameError("the name map does not re-encode byte-identically (unknown name encoding); refusing")

    # --- new name list -----------------------------------------------------------------------
    new_names = list(pkg.names)
    changed_names = {}
    new_names[name_mn.index] = new_name
    changed_names[name_mn.index] = (old, new_name)
    if src_mn.kind == "Package" and src_mn.index != name_mn.index and src_mn.index < len(pkg.names):
        if lower_ascii(pkg.names[src_mn.index]) == lower_ascii(old):
            new_names[src_mn.index] = new_name
            changed_names[src_mn.index] = (pkg.names[src_mn.index], new_name)
        else:
            warnings.append(f"SourceName ({pkg.names[src_mn.index]!r}) differs from Name (localized package?); "
                            "left unchanged")
    for o, n in name_renames.items():
        hits = [i for i, x in enumerate(pkg.names) if x == o]
        if not hits:
            raise RenameError(f"name {o!r} (from --rename-name) is not in the name map of {old}")
        for i in hits:
            if i in changed_names:
                raise RenameError(f"name {o!r} is the package name entry; use the package rename instead")
            new_names[i] = n
            changed_names[i] = (o, n)
    if len(set(new_names)) != len(new_names):
        dup = sorted({x for x in new_names if new_names.count(x) > 1})
        raise RenameError(f"renaming would create duplicate name map entries {dup[:4]} (the new name is "
                          f"already used inside the package)")
    mentions = [x for i, x in enumerate(pkg.names)
                if i not in changed_names and lower_ascii(old) in lower_ascii(x)]

    # --- export GlobalImportIndex ------------------------------------------------------------
    gidx_map = legacy_public_export_index_map(pkg, old, new_name, new_names)

    # --- assemble ------------------------------------------------------------------------------
    nb2, hb2 = write_name_batch_legacy(new_names)
    hb2 = data[ho:ho + 8] + hb2[8:]  # keep the stored algorithm id (parse_legacy checked it)
    new_ho = no + len(nb2) + ((-(no + len(nb2))) % 8)
    new_io = new_ho + len(hb2)
    shift = new_io - s["ImportMapOffset"]
    body = bytearray(data[s["ImportMapOffset"]:])
    rel = lambda absolute: absolute - s["ImportMapOffset"]  # noqa: E731
    imports_changed = []
    for k, imp in enumerate(pkg.imports):
        if imp in import_index_map:
            struct.pack_into("<Q", body, k * 8, import_index_map[imp])
            imports_changed.append({"import": k, "old": f"0x{imp:016X}", "new": f"0x{import_index_map[imp]:016X}"})
    exports_changed = []
    for e in pkg.exports:
        g = e.global_import_index_or_hash
        if g in gidx_map:
            pos = rel(s["ExportMapOffset"]) + e.index * EXPORT_MAP_ENTRY_SIZE + _EXPORT_GLOBAL_IMPORT_OFF
            struct.pack_into("<Q", body, pos, gidx_map[g])
            exports_changed.append({"export": e.index, "name": e.object_name, "old": f"0x{g:016X}",
                                    "new": f"0x{gidx_map[g]:016X}"})
    graph_changed = []
    gpos = rel(s["GraphDataOffset"]) + 4
    for p in pkg.imported_packages:
        if p["package_id"] in package_id_map:
            struct.pack_into("<Q", body, gpos, package_id_map[p["package_id"]])
            graph_changed.append({"old": f"0x{p['package_id']:016X}",
                                  "new": f"0x{package_id_map[p['package_id']]:016X}"})
        gpos += 12 + 8 * len(p["arcs"])
    summary = bytearray(data[:LEGACY_SUMMARY_SIZE])
    struct.pack_into("<i", summary, _SUM_NAMES_SIZE, len(nb2))
    struct.pack_into("<i", summary, _SUM_HASHES_OFF, new_ho)
    struct.pack_into("<i", summary, _SUM_HASHES_SIZE, len(hb2))
    for off in _SUM_SHIFTED:
        struct.pack_into("<i", summary, off, struct.unpack_from("<i", summary, off)[0] + shift)
    out = bytes(summary) + nb2 + b"\0" * (new_ho - no - len(nb2)) + hb2 + bytes(body)

    # --- self-check: everything not listed above is unchanged ------------------------------------
    try:
        npkg = parse_legacy(out)
    except ZenFormatError as e:
        raise RenameError(f"internal error: renamed package does not parse: {e}") from None
    problems = []
    if npkg.package_name != new_name:
        problems.append(f"package name reads back as {npkg.package_name!r}")
    if [x for i, x in enumerate(npkg.names) if i not in changed_names] != \
            [x for i, x in enumerate(pkg.names) if i not in changed_names]:
        problems.append("unrenamed names changed")
    if len(npkg.exports) != len(pkg.exports):
        problems.append("export count changed")
    for a, b in zip(pkg.exports, npkg.exports):
        if data[a.abs_start:a.abs_end] != out[b.abs_start:b.abs_end]:
            problems.append(f"export {a.index} data changed")
        if (a.cooked_serial_offset, a.cooked_serial_size, a.class_index, a.outer_index, a.super_index,
                a.template_index, a.object_flags, a.filter_flags) != \
                (b.cooked_serial_offset, b.cooked_serial_size, b.class_index, b.outer_index, b.super_index,
                 b.template_index, b.object_flags, b.filter_flags):
            problems.append(f"export {a.index} map entry changed beyond GlobalImportIndex")
    if npkg.export_bundles != pkg.export_bundles or npkg.export_bundle_entries != pkg.export_bundle_entries:
        problems.append("export bundles changed")
    if [p["arcs"] for p in npkg.imported_packages] != [p["arcs"] for p in pkg.imported_packages]:
        problems.append("graph arcs changed")
    if out[npkg.export_data_offset:] != data[pkg.export_data_offset:]:
        problems.append("export data region changed")
    keep = ("PackageFlags", "CookedHeaderSize", "GraphDataSize", "Pad", "NameMapNamesOffset")
    if any(npkg.summary[k] != s[k] for k in keep):
        problems.append("summary fields that must not change did")
    if problems:
        raise RenameError("internal error after renaming: " + "; ".join(problems))
    summary_changes = {k: {"old": s[k], "new": npkg.summary[k]} for k in s if s[k] != npkg.summary[k]}
    report = {
        "old_package_name": old, "new_package_name": new_name,
        "old_package_id": f"0x{package_id_from_name(old):016X}",
        "new_package_id": f"0x{package_id_from_name(new_name):016X}",
        "size": {"old": len(data), "new": len(out), "delta": len(out) - len(data)},
        "header_size": {"old": pkg.export_data_offset, "new": npkg.export_data_offset},
        "name_map_changes": [{"index": i, "old": o, "new": n} for i, (o, n) in sorted(changed_names.items())],
        "summary_changes": summary_changes,
        "export_global_import_index_changes": exports_changed,
        "import_map_changes": imports_changed,
        "graph_imported_package_changes": graph_changed,
        "unchanged": ["FPackageSummary.Name/SourceName (FMappedName indices)", "PackageFlags", "CookedHeaderSize",
                      "GraphDataSize", "export map except GlobalImportIndex", "export bundle headers/entries",
                      "graph data arcs", "serialized export data (byte-identical)"],
        "names_mentioning_old_package": mentions,
        "warnings": warnings + ([f"{len(mentions)} other name(s) contain the old package name (e.g. soft object "
                                 f"paths); they were NOT changed: {mentions[:4]}"] if mentions else []),
        "global_import_index_map": {f"0x{k:016X}": f"0x{v:016X}" for k, v in gidx_map.items()},
    }
    return out, report


# --------------------------------------------------------------------------------------------
# Synthetic package builders (fixtures for tests; also documents the layouts byte by byte)
# --------------------------------------------------------------------------------------------
@dataclass
class ExportSpec:
    name: str
    data: bytes
    class_index: int
    outer_index: int = NULL_OBJECT_INDEX
    super_index: int = NULL_OBJECT_INDEX
    template_index: int = NULL_OBJECT_INDEX
    global_import_index: int = NULL_OBJECT_INDEX
    object_flags: int = 0x3
    filter_flags: int = 0


def _names_index(names, text):
    if text not in names:
        names.append(text)
    return names.index(text)


def build_legacy_package(package_name: str, exports: list[ExportSpec], imports: list[int] = (),
                         extra_names: list[str] = (), imported_packages: list[tuple[int, list]] = (),
                         bundles: list[list[int]] | None = None, cooked_header_size: int = 1024,
                         package_flags: int = 0xA0000000) -> bytes:
    """Build a UE4.26/4.27 Zen package (FPackageSummary layout) around the given export blobs.

    ``bundles``: list of lists of export indices; each bundle gets Create+Serialize entries per
    export (default: one bundle with all exports). Export data is laid out in bundle order, as
    the loader expects (CUE4Parse ``PopulateExports``).
    """
    names = [package_name]
    for e in exports:
        _names_index(names, e.name)
    for x in extra_names:
        _names_index(names, x)
    names_buf, hashes_buf = write_name_batch_legacy(names)
    if bundles is None:
        bundles = [list(range(len(exports)))]
    order = [i for b in bundles for i in b]
    legacy_off = {}
    off = cooked_header_size
    for i in order:
        legacy_off[i] = off
        off += len(exports[i].data)

    out = bytearray(LEGACY_SUMMARY_SIZE)
    names_off = len(out)
    out += names_buf
    out += b"\0" * ((-len(out)) % 8)
    hashes_off = len(out)
    out += hashes_buf
    import_off = len(out)
    for imp in imports:
        out += struct.pack("<Q", imp)
    export_off = len(out)
    for i, e in enumerate(exports):
        out += struct.pack("<QQ", legacy_off[i], len(e.data))
        out += MappedName.make(names.index(e.name)).pack()
        out += struct.pack("<QQQQQIB3x", e.outer_index, e.class_index, e.super_index, e.template_index,
                           e.global_import_index, e.object_flags, e.filter_flags)
    bundles_off = len(out)
    entry_index = 0
    for b in bundles:
        out += struct.pack("<II", entry_index, 2 * len(b))
        entry_index += 2 * len(b)
    for b in bundles:
        for i in b:
            out += struct.pack("<II", i, 0)
            out += struct.pack("<II", i, 1)
    graph_off = len(out)
    out += struct.pack("<i", len(imported_packages))
    for pid, arcs in imported_packages:
        out += struct.pack("<Qi", pid, len(arcs))
        for a, b in arcs:
            out += struct.pack("<ii", a, b)
    graph_size = len(out) - graph_off
    struct.pack_into("<IIIIIIiiiiiiiiii", out, 0,
                     MappedName.make(0).raw_index, 0, MappedName.make(0).raw_index, 0,
                     package_flags, cooked_header_size,
                     names_off, len(names_buf), hashes_off, len(hashes_buf),
                     import_off, export_off, bundles_off, graph_off, graph_size, 0)
    for i in order:
        out += exports[i].data
    return bytes(out)


def build_ue5_package(package_name: str, exports: list[ExportSpec], imports: list[int] = (),
                      layout: str = "ue5.3", extra_names: list[str] = (),
                      imported_package_names: list[str] = (), cooked_header_size: int = 1024,
                      versioning: tuple[int, int] | None = None, licensee_version: int = 0,
                      bulk_data_map: list[tuple] = ()) -> bytes:
    """Build a UE5 Zen package (any of ``UE5_LAYOUTS``), unversioned unless ``versioning`` =
    (FileVersionUE4, FileVersionUE5) is given, which writes FZenPackageVersioningInfo
    (CUE4Parse ``FZenPackageVersioningInfo``: EZenPackageVersion u32, FPackageFileVersion 2 x i32,
    LicenseeVersion i32, FCustomVersionContainer = i32 count + count x (FGuid, i32)).
    The 'ue5.6+' layout writes empty Verse cell import/export maps (FZenPackageCellOffsets).
    ``bulk_data_map``: FBulkDataMapEntry tuples (SerialOffset, DuplicateSerialOffset, SerialSize,
    Flags[, CookedIndex]) for layouts with a bulk data map (5.2+)."""
    spec = {x[0]: x for x in UE5_LAYOUTS}[layout]
    _, ssize, has_bulk, has_pad, has_cells = spec
    names = [package_name]
    for e in exports:
        _names_index(names, e.name)
    for x in extra_names:
        _names_index(names, x)
    out = bytearray(ssize)
    if versioning is not None:
        zen_version = 0 if ssize == 44 and not has_bulk else (1 if ssize == 44 else 3)
        out += struct.pack("<Iiii", zen_version, versioning[0], versioning[1], licensee_version)
        out += struct.pack("<i", 0)  # no custom versions
    cells_pos = None
    if has_cells:
        cells_pos = len(out)
        out += struct.pack("<ii", 0, 0)  # CellImportMapOffset, CellExportMapOffset (patched below)
    out += write_name_batch(names)
    if has_bulk:
        if has_pad:
            pad = (-(len(out) + 8)) % 8
            out += struct.pack("<Q", pad) + b"\0" * pad
        out += struct.pack("<q", BULK_DATA_MAP_ENTRY_SIZE * len(bulk_data_map))
        for ent in bulk_data_map:
            off, dup, size, flags, *rest = ent
            out += struct.pack("<qqqIB3x", off, dup, size, flags, rest[0] if rest else 0)
    elif bulk_data_map:
        raise ValueError(f"layout {layout} has no bulk data map")
    # no alignment here: retoc zen.rs FZenPackageHeader::serialize writes the imported public
    # export hashes directly after the name batch / bulk data map (the parser tolerates < 8 bytes)
    hashes_off = len(out)
    import_off = len(out)
    for imp in imports:
        out += struct.pack("<Q", imp)
    export_off = len(out)
    serial = 0
    for e in exports:
        out += struct.pack("<QQ", serial, len(e.data))
        out += MappedName.make(names.index(e.name)).pack()
        out += struct.pack("<QQQQQIB3x", e.outer_index, e.class_index, e.super_index, e.template_index,
                           e.global_import_index, e.object_flags, e.filter_flags)
        serial += len(e.data)
    eb_off = len(out)
    if cells_pos is not None:
        struct.pack_into("<ii", out, cells_pos, eb_off, eb_off)  # empty cell import/export maps
    for i in range(len(exports)):
        out += struct.pack("<II", i, 0)
    for i in range(len(exports)):
        out += struct.pack("<II", i, 1)
    if ssize == 44:
        graph_off = len(out)
        out += struct.pack("<QII", 0, 0, 2 * len(exports))
        out += struct.pack("<i", 0)  # internal arcs (unused by this parser)
        tail = struct.pack("<iiii", hashes_off, import_off, export_off, eb_off) + struct.pack("<i", graph_off)
    else:
        dep_h = len(out)
        for i in range(len(exports)):
            out += struct.pack("<iIIII", -1, 0, 0, 0, 0)
        dep_e = len(out)
        pn_off = len(out)
        out += write_name_batch(list(imported_package_names))
        out += b"".join(struct.pack("<i", 0) for _ in imported_package_names)
        tail = struct.pack("<iiii", hashes_off, import_off, export_off, eb_off) + struct.pack("<iii", dep_h, dep_e, pn_off)
    header_size = len(out)
    struct.pack_into("<II", out, 0, 1 if versioning is not None else 0, header_size)
    out[8:16] = MappedName.make(0).pack()
    struct.pack_into("<II", out, 16, 0, cooked_header_size)
    out[24:24 + len(tail)] = tail
    for e in exports:
        out += e.data
    return bytes(out)


# --------------------------------------------------------------------------------------------
# Output helpers shared with inplace_patch.py (safe writes + manifests)
# --------------------------------------------------------------------------------------------
def sha256_bytes(b) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def file_record(path) -> dict:
    return {"path": os.path.abspath(path), "size": os.path.getsize(path), "sha256": sha256_file(path)}


def prepare_out_dir(out_dir: str, input_paths: list[str]) -> str:
    """Create a NEW output directory. Refuses an existing non-empty dir and any dir that would
    place outputs on top of an input file. Returns the absolute path."""
    out = os.path.abspath(out_dir)
    for p in input_paths:
        if p and os.path.abspath(p) == out:
            raise SystemExit(f"refusing: output dir {out} is an input path")
    if os.path.exists(out):
        if not os.path.isdir(out):
            raise SystemExit(f"refusing: {out} exists and is not a directory")
        if os.listdir(out):
            raise SystemExit(f"refusing: output dir {out} exists and is not empty (write to a new dir)")
    os.makedirs(out, exist_ok=True)
    return out


def write_output(out_dir: str, name: str, data: bytes, input_paths: list[str]) -> str:
    """Write ``data`` to ``out_dir/name`` refusing to overwrite any input or existing file."""
    path = os.path.join(out_dir, name)
    real = os.path.realpath(path)
    for p in input_paths:
        if p and os.path.exists(p) and os.path.realpath(p) == real:
            raise SystemExit(f"refusing to overwrite input {p}")
    if os.path.exists(path):
        raise SystemExit(f"refusing to overwrite existing file {path}")
    with open(path, "xb") as f:
        f.write(data)
    return path


def _json_default(o):
    """numpy scalars/arrays (and tuples of them) in manifests."""
    if hasattr(o, "tolist"):
        return o.tolist()
    if hasattr(o, "item"):
        return o.item()
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


def write_manifest(out_dir: str, manifest: dict, name: str = "manifest.json") -> str:
    path = os.path.join(out_dir, name)
    with open(path, "x", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1, ensure_ascii=False, default=_json_default)
    return path


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------
def _load_script_objects_from_args(a) -> ScriptObjects | None:
    if getattr(a, "script_objects", None):
        with open(a.script_objects, "rb") as f:
            return load_script_objects_ue5(f.read())
    if getattr(a, "global_names", None) or getattr(a, "initial_load_meta", None):
        if not (a.global_names and a.initial_load_meta):
            raise SystemExit("--global-names and --initial-load-meta must be given together")
        with open(a.global_names, "rb") as f:
            gn = f.read()
        with open(a.initial_load_meta, "rb") as f:
            meta = f.read()
        hashes = None
        if a.global_name_hashes:
            with open(a.global_name_hashes, "rb") as f:
                hashes = f.read()
        return load_script_objects_ue4(gn, meta, hashes)
    if getattr(a, "script_map", None):
        return load_script_map_json(a.script_map)
    return None


def _add_script_args(p):
    g = p.add_argument_group("script objects (optional; resolves class indices)")
    g.add_argument("--global-names", help="UE4.26/4.27 LoaderGlobalNames chunk from global.utoc")
    g.add_argument("--global-name-hashes", help="UE4.26/4.27 LoaderGlobalNameHashes chunk")
    g.add_argument("--initial-load-meta", help="UE4.26/4.27 LoaderInitialLoadMeta chunk")
    g.add_argument("--script-objects", help="UE5 ScriptObjects chunk from global.utoc")
    g.add_argument("--script-map", help='JSON {"0x<raw index>": "/Script/Pkg.Obj"} or list of paths')
    p.add_argument("--layout", default="auto",
                   help="auto | ue4 | ue5 | " + " | ".join(x[0] for x in UE5_LAYOUTS))


def cmd_info(a):
    with open(a.package, "rb") as f:
        data = f.read()
    so = _load_script_objects_from_args(a)
    pkg = parse_package(data, so, a.layout)
    if a.json:
        doc = pkg.to_json()
        doc["input"] = file_record(a.package)
        doc["script_objects_source"] = so.source if so else "builtin"
        text = json.dumps(doc, indent=1, ensure_ascii=False)
        if a.json == "-":
            print(text)
        else:
            if os.path.exists(a.json):
                raise SystemExit(f"refusing to overwrite {a.json}")
            with open(a.json, "x", encoding="utf-8") as f:
                f.write(text)
            print("wrote", a.json)
        return
    print(f"package      {pkg.package_name}")
    print(f"layout       {pkg.layout}")
    print(f"UE version   {pkg.ue_version_guess}")
    print("evidence     " + "; ".join(pkg.evidence))
    print(f"chunk size   {pkg.data_size}  header {pkg.header_size}  cooked header {pkg.summary.get('CookedHeaderSize')}")
    print(f"names {len(pkg.names)}  imports {len(pkg.imports)}  exports {len(pkg.exports)}  bundles {len(pkg.export_bundles)}")
    for w in pkg.warnings:
        print("WARNING:", w)
    print(f"{'#':>3} {'name':32} {'class':44} {'abs_start':>10} {'abs_end':>10} {'size':>9}")
    for e in pkg.exports:
        print(f"{e.index:>3} {e.object_name[:32]:32} {e.class_display[:44]:44} {e.abs_start:>10} {e.abs_end:>10} {e.cooked_serial_size:>9}")


def cmd_extract_export(a):
    with open(a.package, "rb") as f:
        data = f.read()
    pkg = parse_package(data, _load_script_objects_from_args(a), a.layout)
    e = pkg.find_export(a.export)
    out = prepare_out_dir(a.out_dir, [a.package])
    blob = data[e.abs_start:e.abs_end]
    fname = f"export{e.index:03d}_{''.join(c if c.isalnum() or c in '-_' else '_' for c in e.object_name)}.bin"
    p = write_output(out, fname, blob, [a.package])
    manifest = {
        "tool": "zen_package.py", "command": "extract-export",
        "inputs": {"package": file_record(a.package)},
        "outputs": {"export": file_record(p)},
        "export": e.to_json(), "layout": pkg.layout, "ue_version_guess": pkg.ue_version_guess,
        "changes": [], "note": "read-only extraction; input unchanged",
    }
    if a.verify:
        again = parse_package(data, None, pkg.layout).find_export(e.index)
        with open(p, "rb") as f:
            ok = f.read() == data[again.abs_start:again.abs_end]
        manifest["verify"] = {"reparsed_range_matches": ok}
        if not ok:
            raise SystemExit("verify failed")
    write_manifest(out, manifest)
    print(json.dumps({"export": e.object_name, "range": [e.abs_start, e.abs_end], "out": p}))


def cmd_hash(a):
    if a.kind == "name":
        print(f"0x{name_hash(a.text):016X}")
    elif a.kind == "script":
        print(f"0x{script_import_index(a.text):016X}")
    elif a.kind == "package-import":
        print(f"0x{legacy_package_import_index(a.text):016X}")
    elif a.kind == "package-id":
        lower = "".join(c.lower() if "A" <= c <= "Z" else c for c in a.text)
        print(f"0x{cityhash64(lower.encode('utf-16-le')):016X}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("info", help="print header, UE version guess and export table")
    p.add_argument("package", help="Zen package chunk (ExportBundleData) extracted from .ucas")
    p.add_argument("--json", help="write JSON report to this new file ('-' for stdout)")
    _add_script_args(p)
    p.set_defaults(fn=cmd_info)
    p = sp.add_parser("extract-export", help="copy one export's serialized bytes to a new dir")
    p.add_argument("package")
    p.add_argument("--export", required=True, help="export index or object name")
    p.add_argument("--out-dir", required=True, help="new (non-existing or empty) output directory")
    p.add_argument("--verify", action="store_true", help="re-parse and compare after writing")
    _add_script_args(p)
    p.set_defaults(fn=cmd_extract_export)
    p = sp.add_parser("hash", help="compute UE hashes (FName / script import / package id)")
    p.add_argument("kind", choices=["name", "script", "package-import", "package-id"])
    p.add_argument("text")
    p.set_defaults(fn=cmd_hash)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
