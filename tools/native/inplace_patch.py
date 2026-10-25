#!/usr/bin/env python3
"""Size-preserving in-place patchers for cooked UE assets: Texture2D mips and mesh vertex streams.

Low-risk strategy: keep the donor asset's topology, vertex count, texture size, pixel format and
every serialized length; only overwrite payload bytes with new bytes of IDENTICAL size. Nothing is
inserted or removed, so no offset, size, SkipOffset, bulk-data map or IoStore index has to change
inside the package (the container still has to be rebuilt by the IoStore tool afterwards).

Subcommands
  texture-scan          find FTexturePlatformData blocks (pixel-format FString + mips) and report
                        every mip: dims, bulk flags, payload location (inline offset or bulk chunk)
  texture-extract       copy mip payloads (and optional PNG decodes) to a new directory
  texture-replace       write new ASTC payloads (astc_tool.py encode manifest) of identical size
  mesh-locate           content-addressed search for position / tangent / UV streams and bounds
                        using a reference mesh exported from the SAME asset (GLB/glTF/OBJ/PSK)
  mesh-patch-positions  overwrite positions (+ optional tangent frames and bounds) from an edited
                        mesh with identical vertex order; one --pair per LOD (LOD0 and LOD1 are
                        separate streams and BOTH must be patched)

Every write command refuses to overwrite inputs, writes into a NEW output directory, emits
``manifest.json`` (input/output SHA-256, every changed byte range) and supports ``--verify``,
which re-reads the output with the same reader and checks the result.

Format sources (CUE4Parse = github.com/FabianFG/CUE4Parse, paths under CUE4Parse/UE4/):
* Assets/Exports/Texture/UTexture.cs ``DeserializeCookedPlatformData``: FName pixel format,
  int64 SkipOffset (UE4.20-4.27: absolute offset in legacy .uasset+.uexp coordinates; UE5:
  relative to the position after the field), then FTexturePlatformData.
* Assets/Exports/Texture/FTexturePlatformData.cs: [UE5: 16-byte derived-data placeholder] SizeX,
  SizeY, PackedData (NumSlices | HasCpuCopy<<29 | HasOptData<<30 | IsCubemap<<31), FString
  PixelFormat, [FOptTexturePlatformData 8 bytes], FirstMipToSerialize, NumMips, mips,
  [bool bIsVirtual].
* Assets/Exports/Texture/FTexture2DMipMap.cs: [UE4 only: bool bCooked (uint32)] FByteBulkData,
  int32 SizeX, SizeY, SizeZ.
* Assets/Objects/FByteBulkDataHeader.cs, EBulkDataFlags.cs, TBulkData.cs: uint32 flags,
  int32|int64 ElementCount, SizeOnDisk, int64 OffsetInFile; payload inline right after the header
  unless BULKDATA_PayloadAtEndOfFile; PayloadInSeperateFile -> .ubulk (IoStore BulkData chunk),
  +OptionalPayload -> .uptnl (OptionalBulkData), +MemoryMappedPayload -> .m.ubulk.
  UE5.2+ (DATA_RESOURCES): the header is an int32 index into the package bulk data map.
* Objects/Meshes/FPositionVertexBuffer.cs: int32 Stride, int32 NumVertices, bulk array
  (int32 elemSize, int32 count, FVector3f data).
* Assets/Exports/StaticMesh/FStaticMeshVertexBuffer.cs, FStaticMeshUVItem.cs: NumTexCoords,
  NumVertices, bUseFullPrecisionUVs, bUseHighPrecisionTangentBasis, tangents bulk array
  (TangentX, TangentZ per vertex), texcoord bulk array (NumTexCoords UVs per vertex).
* Objects/RenderCore/FPackedNormal.cs (UE4.20+: int8 x/127), FPackedRGBA16N.cs (int16 x/32767).
* Assets/Exports/SkeletalMesh/FStaticLODModel.cs (cooked LOD: Indices, ..., PositionVertexBuffer,
  StaticMeshVertexBuffer, SkinWeightVertexBuffer, ...; each LOD has its own buffers) and
  USkeletalMesh.cs (ImportedBounds FBoxSphereBounds right after FStripDataFlags).
* Objects/Core/Math/FBoxSphereBounds.cs: Origin, BoxExtent, SphereRadius (float32 in UE4).
* CUE4Parse-Conversion Writers/Gltf/Gltf.cs (FModel glTF: position = SwapYZ(pos*0.01), vertices
  re-ordered per section by first use) and Writers/ActorX/ActorXMesh.cs (PSK: Y mirrored, one
  point/wedge per vertex in buffer order). glTF 2.0 specification (GLB container, accessors).
"""
from __future__ import annotations

import argparse
import base64
import datetime
import json
import math
import os
import re
import struct
import sys
from dataclasses import dataclass, field

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zen_package as zp  # noqa: E402

TOOL_VERSION = "1.0"

# ============================================================================================
# Common helpers
# ============================================================================================


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def base_manifest(command, inputs: dict, params: dict) -> dict:
    return {"tool": "inplace_patch.py", "tool_version": TOOL_VERSION, "command": command,
            "created_utc": now_utc(),
            "inputs": {k: zp.file_record(v) for k, v in inputs.items() if v},
            "parameters": params, "outputs": {}, "changes": []}


@dataclass
class Target:
    """An input file plus the byte range to scan (whole file or one Zen export)."""
    path: str
    data: bytes
    start: int
    end: int
    package: object = None      # zen_package.ZenPackage when the file parsed as a Zen package
    export: object = None       # zen_package.ExportInfo when --zen-export selected one

    def export_at(self, pos):
        if self.export is not None:
            return self.export
        if self.package is None:
            return None
        for e in self.package.exports:
            if e.abs_start <= pos < e.abs_end:
                return e
        return None

    def describe(self):
        d = {"path": os.path.abspath(self.path), "scan_range": [self.start, self.end]}
        if self.package is not None:
            d["zen_layout"] = self.package.layout
            d["ue_version_guess"] = self.package.ue_version_guess
            d["package_name"] = self.package.package_name
        if self.export is not None:
            d["export"] = {"index": self.export.index, "name": self.export.object_name,
                           "class": self.export.class_display}
        return d


def load_target(path, zen_export=None, raw=False, layout="auto") -> Target:
    """Load ``path``; unless ``raw``, try to parse it as a Zen package so exports and the legacy
    offset delta are known. ``zen_export`` (index or name) restricts scanning to that export."""
    data = read_bytes(path)
    pkg = None
    if not raw:
        try:
            pkg = zp.parse_package(data, None, layout)
        except zp.ZenFormatError:
            pkg = None
    if zen_export is not None:
        if pkg is None:
            raise SystemExit(f"--zen-export given but {path} does not parse as a Zen package")
        e = pkg.find_export(zen_export)
        return Target(path, data, e.abs_start, e.abs_end, pkg, e)
    return Target(path, data, 0, len(data), pkg, None)


class Patch:
    """Collects equal-size byte replacements for one file and applies them to a copy."""

    def __init__(self, label, path, data):
        self.label = label
        self.path = path
        self.orig = data
        self.buf = bytearray(data)
        self.changes = []

    def write(self, offset, new, what):
        new = bytes(new)
        old = self.orig[offset:offset + len(new)]
        if offset < 0 or offset + len(new) > len(self.orig):
            raise SystemExit(f"refusing: write {what} at {offset}+{len(new)} outside {self.label} ({len(self.orig)} bytes)")
        for c in self.changes:
            if offset < c["offset"] + c["length"] and c["offset"] < offset + len(new):
                raise SystemExit(f"refusing: overlapping writes ({what} vs {c['what']})")
        self.buf[offset:offset + len(new)] = new
        self.changes.append({"file": self.label, "offset": offset, "length": len(new), "what": what,
                             "old_sha256": zp.sha256_bytes(old), "new_sha256": zp.sha256_bytes(new),
                             "identical": old == new})

    def result(self):
        assert len(self.buf) == len(self.orig), "size-preserving invariant violated"
        return bytes(self.buf)


def check_untouched(orig, new, changes):
    """True when every byte outside the recorded change ranges is identical."""
    if len(orig) != len(new):
        return False
    mask = np.ones(len(orig), dtype=bool)
    for c in changes:
        mask[c["offset"]:c["offset"] + c["length"]] = False
    a = np.frombuffer(orig, dtype=np.uint8)
    b = np.frombuffer(new, dtype=np.uint8)
    return bool(np.array_equal(a[mask], b[mask]))


def emit_outputs(out_dir, patches, manifest, input_paths):
    used = {"manifest.json"}  # reserved: written last, must not collide with an output
    for p in patches:
        name = os.path.basename(p.path)
        if name in used:
            name = f"{p.label}.{name}"
        used.add(name)
        outp = zp.write_output(out_dir, name, p.result(), input_paths)
        manifest["outputs"][p.label] = zp.file_record(outp)
        manifest["changes"].extend(p.changes)
    return manifest


# ============================================================================================
# Bulk data (FByteBulkData header)
# ============================================================================================
BULK_FLAGS = {
    0x1: "PayloadAtEndOfFile", 0x2: "SerializeCompressedZLIB", 0x4: "ForceSingleElementSerialization",
    0x8: "SingleUse", 0x10: "CompressedLZO", 0x20: "Unused", 0x40: "ForceInlinePayload",
    0x80: "ForceStreamPayload", 0x100: "PayloadInSeperateFile", 0x200: "SerializeCompressedBitWindow",
    0x400: "Force_NOT_InlinePayload", 0x800: "OptionalPayload", 0x1000: "MemoryMappedPayload",
    0x2000: "Size64Bit", 0x4000: "DuplicateNonOptionalPayload", 0x8000: "BadDataVersion",
    0x10000: "NoOffsetFixUp", 0x20000: "WorkspaceDomainPayload", 0x40000: "LazyLoadable",
}
BULKDATA_PayloadAtEndOfFile = 0x1
BULKDATA_COMPRESSED = 0x2 | 0x10 | 0x200
BULKDATA_Unused = 0x20
BULKDATA_ForceInlinePayload = 0x40
BULKDATA_PayloadInSeperateFile = 0x100
BULKDATA_OptionalPayload = 0x800
BULKDATA_MemoryMappedPayload = 0x1000
BULKDATA_Size64Bit = 0x2000
BULKDATA_DuplicateNonOptionalPayload = 0x4000
BULKDATA_BadDataVersion = 0x8000
BULKDATA_NoOffsetFixUp = 0x10000
KNOWN_BULK_MASK = sum(BULK_FLAGS)


def bulk_flag_names(flags):
    return [n for b, n in BULK_FLAGS.items() if flags & b] or ["None"]


@dataclass
class BulkHeader:
    pos: int                 # absolute position of the header
    end: int                 # absolute position after the header (and after an inline payload: see payload_end)
    flags: int
    element_count: int
    size_on_disk: int
    offset_in_file: int
    location: str            # inline | BulkData | OptionalBulkData | MemoryMappedBulkData | package_end | none | data_resource
    payload_pos: int | None  # absolute offset in THIS file for inline payloads
    payload_end: int         # position after the header+inline payload
    data_resource_index: int | None = None

    def to_json(self):
        d = {"header_pos": self.pos, "flags": f"0x{self.flags:08X}", "flag_names": bulk_flag_names(self.flags),
             "element_count": self.element_count, "size_on_disk": self.size_on_disk,
             "offset_in_file": self.offset_in_file, "location": self.location}
        if self.location == "inline":
            d["payload_offset"] = self.payload_pos
            d["note"] = "inline payload starts right after the bulk header (OffsetInFile is informational)"
        elif self.location in ("BulkData", "OptionalBulkData", "MemoryMappedBulkData"):
            d["bulk_chunk_offset"] = self.offset_in_file
            d["offset_fixup_needed"] = not bool(self.flags & BULKDATA_NoOffsetFixUp)
        if self.data_resource_index is not None:
            d["data_resource_index"] = self.data_resource_index
        return d


def bulk_location(flags):
    """Payload location from EBulkDataFlags (CUE4Parse TBulkData.GetBulkArchive, UE4+ rules)."""
    if flags & BULKDATA_Unused:
        return "none"
    if flags & BULKDATA_ForceInlinePayload or not flags & BULKDATA_PayloadAtEndOfFile:
        return "inline"
    if flags & BULKDATA_PayloadInSeperateFile:
        if flags & BULKDATA_OptionalPayload:
            return "OptionalBulkData"
        if flags & BULKDATA_MemoryMappedPayload:
            return "MemoryMappedBulkData"
        return "BulkData"
    return "package_end"


def parse_bulk_header(data, pos, limit=None) -> BulkHeader:
    """Parse a legacy (UE4.x / UE5.0-5.1) FByteBulkData header at ``pos``."""
    limit = len(data) if limit is None else limit
    r = zp.Reader(data, pos)
    flags = r.u32()
    if flags & ~KNOWN_BULK_MASK:
        raise zp.ZenFormatError(f"unknown bulk flags 0x{flags:08X}")
    if flags & BULKDATA_Size64Bit:
        count, size = r.i64(), r.i64()
    else:
        count, size = r.i32(), r.i32()
    offset = r.i64()
    if count < 0 or size < 0:
        raise zp.ZenFormatError("negative bulk size")
    if flags & BULKDATA_BadDataVersion:
        r.u16()
    if flags & BULKDATA_DuplicateNonOptionalPayload:
        r.u32()
        r.i64() if flags & BULKDATA_Size64Bit else r.u32()
        r.i64()
    loc = bulk_location(flags)
    hdr_end = r.pos
    payload_pos = None
    payload_end = hdr_end
    if loc == "inline":
        payload_pos = hdr_end
        payload_end = hdr_end + size
        if payload_end > limit:
            raise zp.ZenFormatError("inline payload past end of range")
    return BulkHeader(pos, hdr_end, flags, count, size, offset, loc, payload_pos, payload_end)


def parse_bulk_index(data, pos, bulk_map, limit=None) -> BulkHeader:
    """UE5.2+ (DATA_RESOURCES): int32 index into the package bulk data map (FBulkDataMapEntry)."""
    limit = len(data) if limit is None else limit
    r = zp.Reader(data, pos)
    idx = r.i32()
    if not (0 <= idx < len(bulk_map)):
        raise zp.ZenFormatError(f"bulk data map index {idx} out of range")
    e = bulk_map[idx]
    flags = int(e["flags"], 16)
    size = int(e["serial_size"])
    loc = bulk_location(flags)
    payload_pos = None
    end = r.pos
    if loc == "inline":
        payload_pos = r.pos
        end = r.pos + size
        if end > limit:
            raise zp.ZenFormatError("inline payload past end of range")
    return BulkHeader(pos, r.pos, flags, size, size, int(e["serial_offset"]), loc, payload_pos, end, idx)


# ============================================================================================
# Pixel formats
# ============================================================================================
# EPixelFormat name -> (block x, block y, bytes per block). ASTC blocks are always 16 bytes.
PIXEL_FORMATS = {
    "PF_B8G8R8A8": (1, 1, 4), "PF_R8G8B8A8": (1, 1, 4), "PF_A8R8G8B8": (1, 1, 4), "PF_G8": (1, 1, 1),
    "PF_G16": (1, 1, 2), "PF_FloatRGBA": (1, 1, 8), "PF_A16B16G16R16": (1, 1, 8), "PF_R8G8": (1, 1, 2),
    "PF_DXT1": (4, 4, 8), "PF_DXT3": (4, 4, 16), "PF_DXT5": (4, 4, 16), "PF_BC4": (4, 4, 8),
    "PF_BC5": (4, 4, 16), "PF_BC6H": (4, 4, 16), "PF_BC7": (4, 4, 16),
    "PF_ETC1": (4, 4, 8), "PF_ETC2_RGB": (4, 4, 8), "PF_ETC2_RGBA": (4, 4, 16),
    "PF_ETC2_R11_EAC": (4, 4, 8), "PF_ETC2_RG11_EAC": (4, 4, 16),
}
_ASTC_RE = re.compile(r"^PF_ASTC_(\d+)x(\d+)(_HDR|_NORM_RG|_NORM)?$")


def pixel_format_block(fmt):
    m = _ASTC_RE.match(fmt)
    if m:
        return int(m.group(1)), int(m.group(2)), 16
    return PIXEL_FORMATS.get(fmt)


def expected_mip_size(fmt, w, h, d=1):
    blk = pixel_format_block(fmt)
    if blk is None:
        return None
    bx, by, bpb = blk
    return math.ceil(w / bx) * math.ceil(h / by) * bpb * max(1, d)


# ============================================================================================
# Texture: FTexturePlatformData locator
# ============================================================================================
_PF_STRING_RE = re.compile(rb"PF_[A-Za-z0-9_]{1,40}\x00")


@dataclass
class MipInfo:
    index: int
    size_x: int
    size_y: int
    size_z: int
    entry_start: int
    entry_end: int
    bulk: BulkHeader
    expected_size: int | None
    dims_rule: str

    def to_json(self):
        return {"mip": self.index, "size": [self.size_x, self.size_y, self.size_z],
                "entry": [self.entry_start, self.entry_end], "expected_payload_size": self.expected_size,
                "dims_rule": self.dims_rule, "bulk": self.bulk.to_json()}


@dataclass
class PlatformData:
    start: int                  # absolute offset of SizeX
    end: int                    # absolute offset after the last parsed field
    size_x: int
    size_y: int
    packed: int
    pixel_format: str
    variant: str                # ue4 (bool bCooked per mip) | ue5 | ue5-data-resources
    opt_data: tuple | None
    first_mip: int
    mips: list
    is_virtual: int | None
    skip_offset: dict | None
    notes: list = field(default_factory=list)

    @property
    def num_slices(self):
        return self.packed & ((1 << 30) - 1) & ~(1 << 29)

    def to_json(self):
        return {"platform_data_offset": self.start, "end": self.end, "size": [self.size_x, self.size_y],
                "packed_data": f"0x{self.packed:08X}", "num_slices": self.num_slices,
                "is_cubemap": bool(self.packed >> 31), "pixel_format": self.pixel_format,
                "block": list(pixel_format_block(self.pixel_format) or []), "variant": self.variant,
                "opt_data": self.opt_data, "first_mip_to_serialize": self.first_mip,
                "num_mips": len(self.mips), "is_virtual": self.is_virtual, "skip_offset": self.skip_offset,
                "mips": [m.to_json() for m in self.mips], "notes": self.notes}


def _dims_rule(w, h, exp_w, exp_h, fmt):
    if (w, h) == (exp_w, exp_h):
        return "exact"
    blk = pixel_format_block(fmt)
    if blk and (w, h) == (max(exp_w, blk[0]), max(exp_h, blk[1])):
        return "block_padded"
    return None


def _try_parse_mips(data, pos, limit, fmt, top_x, top_y, slices, cooked_bool, has_first_mip,
                    bulk_map=None):
    """Parse [FirstMipToSerialize] NumMips + mips; returns (first_mip, mips, end) or raises."""
    r = zp.Reader(data, pos)
    first = r.i32() if has_first_mip else 0
    if not (0 <= first < 32):
        raise zp.ZenFormatError("FirstMipToSerialize implausible")
    n = r.i32()
    if not (1 <= n <= 32):
        raise zp.ZenFormatError("NumMips implausible")
    mips = []
    p = r.pos
    for i in range(n):
        start = p
        if cooked_bool:
            b = struct.unpack_from("<I", data, p)[0] if p + 4 <= limit else None
            if b not in (0, 1):
                raise zp.ZenFormatError("bCooked not a bool")
            p += 4
        bh = parse_bulk_index(data, p, bulk_map, limit) if bulk_map is not None else parse_bulk_header(data, p, limit)
        p = bh.payload_end
        if p + 12 > limit:
            raise zp.ZenFormatError("mip dims past end")
        sx, sy, sz = struct.unpack_from("<iii", data, p)
        p += 12
        if not (1 <= sx <= 16384 and 1 <= sy <= 16384 and 1 <= sz <= 2048):
            raise zp.ZenFormatError("mip dims implausible")
        lvl = first + i
        exp_w, exp_h = max(1, top_x >> lvl), max(1, top_y >> lvl)
        rule = _dims_rule(sx, sy, exp_w, exp_h, fmt)
        if rule is None:
            raise zp.ZenFormatError(f"mip {i} dims {sx}x{sy} do not follow {top_x}x{top_y} >> {lvl}")
        exp = expected_mip_size(fmt, sx, sy, sz)
        if bh.location != "none" and bh.size_on_disk != 0:
            # SizeOnDisk 0 = no payload stored (CUE4Parse TBulkData: SizeOnDisk == 0 -> empty)
            if bh.flags & BULKDATA_COMPRESSED:
                pass  # compressed payloads are reported but cannot be size-checked
            elif bh.element_count != bh.size_on_disk:
                raise zp.ZenFormatError("ElementCount != SizeOnDisk for an uncompressed payload")
            elif exp is not None and bh.size_on_disk != exp:
                raise zp.ZenFormatError(f"mip {i} payload {bh.size_on_disk} != expected {exp}")
        mips.append(MipInfo(i, sx, sy, sz, start, p, bh, exp, rule))
    if not any(m.bulk.location != "none" and m.bulk.size_on_disk for m in mips):
        raise zp.ZenFormatError("no mip carries a payload")
    return first, mips, p


def scan_texture_platform_data(target: Target, unsupported: list | None = None) -> list:
    """Find every FTexturePlatformData in the target range by its pixel-format FString.

    For each ``PF_*`` FString with a matching int32 length prefix, the 12 bytes before it must be
    plausible SizeX/SizeY/PackedData and the bytes after it must parse as a mip array with dims
    halving from SizeX/SizeY and payload sizes matching the format's block math. Layout variants
    tried: UE4 (per-mip bool bCooked), UE5 (no bCooked), UE5.2+ bulk-data-map indices (only when
    the Zen header with its bulk data map is available).

    Candidates this scanner recognises but cannot handle (HasCpuCopy / FSharedImage, UE5.4+) are
    appended to ``unsupported`` (when given) instead of being dropped silently.
    """
    data, lo, hi = target.data, target.start, target.end
    results = []
    bulk_map = target.package.bulk_data_map if target.package is not None and target.package.bulk_data_map else None
    is_ue5 = target.package is not None and target.package.layout.startswith("ue5")
    for m in _PF_STRING_RE.finditer(data, lo, hi):
        spos = m.start()
        strlen = len(m.group())
        if spos - 16 < lo or struct.unpack_from("<i", data, spos - 4)[0] != strlen:
            continue
        fmt = m.group()[:-1].decode("ascii")
        start = spos - 16
        sx, sy, packed = struct.unpack_from("<iiI", data, start)
        slices = packed & ((1 << 29) - 1)
        if not (1 <= sx <= 16384 and 1 <= sy <= 16384 and 1 <= slices <= 2048):
            continue
        p = m.end()
        opt = None
        notes = []
        if packed & (1 << 30):
            if p + 8 > hi:
                continue  # FOptTexturePlatformData would run past the scan range: not a real block
            opt = struct.unpack_from("<II", data, p)
            p += 8
        if packed & (1 << 29):
            if unsupported is not None:
                unsupported.append({"platform_data_offset": start, "pixel_format": fmt, "size": [sx, sy],
                                    "packed_data": f"0x{packed:08X}",
                                    "reason": "HasCpuCopy (UE5.4+ FSharedImage) is not supported; refusing to guess"})
            continue
        variants = []
        if bulk_map is not None:
            variants.append(("ue5-data-resources", False, True, bulk_map))
        order = [("ue5", False, True, None), ("ue4", True, True, None)] if is_ue5 else \
                [("ue4", True, True, None), ("ue5", False, True, None)]
        variants += order + [("ue4-no-first-mip", True, False, None)]
        parsed = None
        errors = []
        for name, cooked_bool, has_first, bmap in variants:
            try:
                first, mips, end = _try_parse_mips(data, p, hi, fmt, sx, sy, slices, cooked_bool, has_first, bmap)
                parsed = (name, first, mips, end)
                break
            except (zp.ZenFormatError, struct.error) as e:
                errors.append(f"{name}: {e}")
        if parsed is None:
            continue
        name, first, mips, end = parsed
        is_virtual = None
        if end + 4 <= hi and struct.unpack_from("<I", data, end)[0] in (0, 1):
            is_virtual = struct.unpack_from("<I", data, end)[0]
            end += 4
        pd = PlatformData(start, end, sx, sy, packed, fmt, name, opt, first, mips, is_virtual, None, notes)
        pd.skip_offset = _check_skip_offset(target, pd)
        results.append(pd)
    return results


def _check_skip_offset(target, pd):
    """UTexture::SerializeCookedPlatformData writes FName PixelFormat + int64 SkipOffset before the
    platform data (UE5 adds a 16-byte placeholder in between). UE4.20-4.27: SkipOffset is the
    absolute legacy offset of the platform data end; UE5: relative to the end of the field."""
    data = target.data
    cands = {}
    names = target.package.names if target.package is not None else None
    for label, gap in (("ue4", 0), ("ue5", 16)):
        fpos = pd.start - gap - 16
        if fpos < target.start:
            continue
        name_idx, name_num, skip = struct.unpack_from("<IIq", data, fpos)
        name_text = names[name_idx] if names and name_idx < len(names) else None
        if names is not None and name_text != pd.pixel_format:
            continue
        if label == "ue5":
            # CUE4Parse: Ar.AbsolutePosition + Ar.Read<long>() -> relative to the field's start
            cands[label] = {"field_offset": fpos + 8, "value": skip, "layout": "ue5-relative",
                            "consistent": fpos + 8 + skip == pd.end, "pixel_format_fname": name_text}
            continue
        delta = skip - pd.end
        e = target.export_at(pd.start)
        expected = e.legacy_to_chunk_delta if e is not None else None
        cands[label] = {"field_offset": fpos + 8, "value": skip, "layout": "ue4-absolute-legacy",
                        "legacy_minus_chunk": delta, "export_legacy_delta": expected,
                        "consistent": (delta == expected) if expected is not None else None,
                        "pixel_format_fname": name_text}
        if expected is None:
            cands[label]["note"] = ("no Zen export around this block: the legacy offset of the export is "
                                    "unknown, so the absolute SkipOffset cannot be checked")
    # a consistent reading wins; otherwise report the reading that matches the mip layout that
    # parsed (UE4 per-mip bCooked -> absolute legacy SkipOffset, UE5 -> relative)
    for c in cands.values():
        if c["consistent"] is True:
            return c
    prefer = "ue4" if pd.variant.startswith("ue4") else "ue5"
    return cands.get(prefer) or next(iter(cands.values()), None)


# ============================================================================================
# ASTC manifest (tools/native/astc_tool.py encode output)
# ============================================================================================
def load_astc_manifest(manifest_path, bin_path=None):
    with open(manifest_path, "r", encoding="utf-8") as f:
        man = json.load(f)
    for k in ("block", "mips"):
        if k not in man:
            raise SystemExit(f"refusing: {manifest_path} is not an astc_tool.py encode manifest (missing {k!r})")
    if bin_path is None:
        bx, by = man["block"]
        stem = os.path.splitext(manifest_path)[0]
        bin_path = stem + ".bin"
        if not os.path.exists(bin_path):
            cand = [f for f in os.listdir(os.path.dirname(os.path.abspath(manifest_path)))
                    if f.endswith(f".astc{bx}x{by}.bin")]
            if len(cand) == 1:
                bin_path = os.path.join(os.path.dirname(os.path.abspath(manifest_path)), cand[0])
    if not os.path.isfile(bin_path):
        raise SystemExit(f"refusing: ASTC payload file {bin_path} not found (pass --astc-bin)")
    blob = read_bytes(bin_path)
    if "total_size" in man and len(blob) != man["total_size"]:
        raise SystemExit(f"refusing: {bin_path} size {len(blob)} != manifest total_size {man['total_size']}")
    if "sha256" in man and zp.sha256_bytes(blob) != man["sha256"]:
        raise SystemExit(f"refusing: {bin_path} sha256 does not match the manifest")
    mips = []
    for i, m in enumerate(man["mips"]):
        # plan_texture_replace maps manifest entry i onto serialized mip i: the list must be the
        # mip chain in order (astc_tool.py writes level 0..n-1)
        if m["level"] != i:
            raise SystemExit(f"refusing: manifest mip entry {i} has level {m['level']} (levels must be 0..n-1 in order)")
        top = man["mips"][0]
        if _dims_rule(m["w"], m["h"], max(1, top["w"] >> i), max(1, top["h"] >> i),
                      f"PF_ASTC_{man['block'][0]}x{man['block'][1]}") is None:
            raise SystemExit(f"refusing: manifest mip {i} is {m['w']}x{m['h']}, not mip 0 >> {i} "
                             "(exact or block padded)")
        payload = blob[m["offset"]:m["offset"] + m["size"]]
        if len(payload) != m["size"] or zp.sha256_bytes(payload) != m["sha256"]:
            raise SystemExit(f"refusing: manifest mip {m['level']} payload hash/size mismatch")
        mips.append({"level": m["level"], "w": m["w"], "h": m["h"], "size": m["size"], "payload": payload})
    return man, mips, bin_path


def plan_texture_replace(pd: PlatformData, man, man_mips, strict_dims=False, allow_extra_mips=False):
    """Map manifest mips onto texture mips; refuse on any format/dimension/size difference."""
    bx, by = man["block"]
    want = f"PF_ASTC_{bx}x{by}"
    if pd.pixel_format != want:
        raise SystemExit(f"refusing: texture pixel format {pd.pixel_format} != manifest {want} "
                         f"(re-encode with --block {pd.pixel_format[8:] if pd.pixel_format.startswith('PF_ASTC_') else '?'})")
    plan = []
    used = set()
    top = man_mips[0]
    if (top["w"], top["h"]) != (max(1, pd.size_x >> pd.first_mip), max(1, pd.size_y >> pd.first_mip)):
        raise SystemExit(f"refusing: manifest mip0 {top['w']}x{top['h']} != texture top serialized mip "
                         f"{max(1, pd.size_x >> pd.first_mip)}x{max(1, pd.size_y >> pd.first_mip)} (texture size must not change)")
    for mip in pd.mips:
        if mip.bulk.location == "none" or mip.bulk.size_on_disk == 0:
            continue
        if mip.bulk.flags & BULKDATA_COMPRESSED:
            raise SystemExit(f"refusing: mip {mip.index} payload is compressed ({bulk_flag_names(mip.bulk.flags)})")
        if mip.bulk.location == "package_end":
            raise SystemExit(f"refusing: mip {mip.index} payload is PayloadAtEndOfFile inside the package; "
                             "location not verified on real data yet")
        if mip.index >= len(man_mips):
            raise SystemExit(f"refusing: manifest has {len(man_mips)} mips, texture needs mip {mip.index}")
        mm = man_mips[mip.index]
        rule = _dims_rule(mip.size_x, mip.size_y, mm["w"], mm["h"], pd.pixel_format)
        if rule is None or (strict_dims and rule != "exact"):
            raise SystemExit(f"refusing: mip {mip.index} dims {mip.size_x}x{mip.size_y} != manifest {mm['w']}x{mm['h']}")
        if mm["size"] != mip.bulk.size_on_disk:
            raise SystemExit(f"refusing: mip {mip.index} size {mip.bulk.size_on_disk} != manifest {mm['size']}")
        plan.append((mip, mm, rule))
        used.add(mip.index)
    extra = [m["level"] for m in man_mips if m["level"] not in used]
    if extra and not allow_extra_mips:
        tex_levels = [m.index for m in pd.mips]
        if len(man_mips) != len(pd.mips):
            raise SystemExit(f"refusing: manifest has {len(man_mips)} mips, texture serializes {len(pd.mips)} "
                             f"(levels {tex_levels}); use --allow-extra-mips only if that is expected")
    return plan


def select_platform_data(pds, index, unsupported=()):
    if not pds:
        if unsupported:
            raise SystemExit("refusing: only unsupported FTexturePlatformData found: "
                             + "; ".join(f"@{u['platform_data_offset']} {u['pixel_format']}: {u['reason']}"
                                         for u in unsupported))
        raise SystemExit("no FTexturePlatformData found (is this a cooked Texture2D export?)")
    if index is not None and not 0 <= index < len(pds):
        raise SystemExit(f"--index {index} out of range: {len(pds)} platform data block(s) found")
    if index is None:
        if len(pds) > 1:
            raise SystemExit(f"{len(pds)} platform data blocks found; pick one with --index")
        return pds[0]
    return pds[index]


def _bulk_inputs(a):
    return {"BulkData": a.bulk, "OptionalBulkData": a.optional_bulk, "MemoryMappedBulkData": a.mmap_bulk}


# ============================================================================================
# Reference meshes (GLB / glTF / OBJ / PSK)
# ============================================================================================
@dataclass
class RefMesh:
    positions: np.ndarray
    normals: np.ndarray | None
    uvs: list
    faces: np.ndarray | None
    source: str
    notes: list = field(default_factory=list)

    @property
    def count(self):
        return len(self.positions)


_GLTF_COMP = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
_GLTF_NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def _gltf_accessor(doc, buffers, idx):
    """Read one glTF accessor (strided bufferView, optional normalization) into an (count, n) array."""
    acc = doc["accessors"][idx]
    if "sparse" in acc:
        raise SystemExit("sparse glTF accessors are not supported")
    dt = np.dtype(_GLTF_COMP[acc["componentType"]]).newbyteorder("<")
    nc = _GLTF_NCOMP[acc["type"]]
    count = acc["count"]
    bv = doc["bufferViews"][acc["bufferView"]]
    buf = buffers[bv["buffer"]]
    base = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = bv.get("byteStride", 0) or dt.itemsize * nc
    if count and base + (count - 1) * stride + nc * dt.itemsize > len(buf):
        raise SystemExit(f"glTF accessor {idx} runs past its buffer")
    out = np.ndarray((count, nc), dtype=dt, buffer=buf, offset=base, strides=(stride, dt.itemsize)).copy()
    if acc.get("normalized"):
        out = np.maximum(out.astype(np.float64) / np.iinfo(dt).max, -1.0)
    return out


def load_gltf(path, mesh_index="0"):
    """glTF 2.0 / GLB: concatenates the primitives of one mesh (or all meshes with 'all')."""
    raw = read_bytes(path)
    buffers = []
    if raw[:4] == b"glTF":
        _, _, length = struct.unpack_from("<III", raw, 0)
        pos = 12
        doc = None
        while pos < length:
            clen, ctype = struct.unpack_from("<II", raw, pos)
            chunk = raw[pos + 8:pos + 8 + clen]
            if ctype == 0x4E4F534A:
                doc = json.loads(chunk.decode("utf-8"))
            elif ctype == 0x004E4942:
                buffers.append(chunk)
            pos += 8 + clen
    else:
        doc = json.loads(raw.decode("utf-8"))
        for b in doc.get("buffers", []):
            uri = b.get("uri", "")
            if uri.startswith("data:"):
                buffers.append(base64.b64decode(uri.split(",", 1)[1]))
            else:
                buffers.append(read_bytes(os.path.join(os.path.dirname(os.path.abspath(path)), uri)))
    if raw[:4] == b"glTF" and doc.get("buffers") and len(buffers) < len(doc["buffers"]):
        for b in doc["buffers"][len(buffers):]:
            buffers.append(read_bytes(os.path.join(os.path.dirname(os.path.abspath(path)), b["uri"])))
    meshes = doc.get("meshes", [])
    if not meshes:
        raise SystemExit(f"{path}: no meshes")
    sel = range(len(meshes)) if mesh_index == "all" else [int(mesh_index)]
    pos_l, nrm_l, uv_l, face_l = [], [], {}, []
    notes = []
    base = 0
    has_nrm = True
    for mi in sel:
        for pi, prim in enumerate(meshes[mi]["primitives"]):
            if prim.get("mode", 4) != 4:
                raise SystemExit("only triangle primitives are supported")
            at = prim["attributes"]
            p = _gltf_accessor(doc, buffers, at["POSITION"]).astype(np.float64)
            pos_l.append(p)
            if "NORMAL" in at:
                nrm_l.append(_gltf_accessor(doc, buffers, at["NORMAL"]).astype(np.float64))
            else:
                has_nrm = False
            k = 0
            while f"TEXCOORD_{k}" in at:
                uv_l.setdefault(k, []).append(_gltf_accessor(doc, buffers, at[f"TEXCOORD_{k}"]).astype(np.float64))
                k += 1
            if "indices" in prim:
                face_l.append(_gltf_accessor(doc, buffers, prim["indices"]).reshape(-1, 3).astype(np.int64) + base)
            base += len(p)
    nodes_with_xform = [n.get("name", i) for i, n in enumerate(doc.get("nodes", []))
                        if any(k in n for k in ("matrix", "rotation", "scale", "translation"))]
    if nodes_with_xform:
        notes.append(f"node transforms present (ignored; the locator fits the transform): {nodes_with_xform[:4]}")
    uvs = [np.concatenate(uv_l[k]) for k in sorted(uv_l) if len(uv_l[k]) == len(pos_l)]
    return RefMesh(np.concatenate(pos_l), np.concatenate(nrm_l) if has_nrm and nrm_l else None, uvs,
                   np.concatenate(face_l) if face_l else None, f"gltf:{os.path.basename(path)}", notes)


def load_obj(path):
    """Wavefront OBJ: each distinct v/vt/vn corner becomes one vertex (first-appearance order).
    Without faces the raw ``v`` list is used. Polygons are fan-triangulated."""
    v, vt, vn = [], [], []
    corners = {}
    order = []
    faces = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            t = line.split()
            if not t:
                continue
            if t[0] == "v":
                v.append([float(x) for x in t[1:4]])
            elif t[0] == "vt":
                vt.append([float(x) for x in t[1:3]])
            elif t[0] == "vn":
                vn.append([float(x) for x in t[1:4]])
            elif t[0] == "f":
                idx = []
                for c in t[1:]:
                    parts = (c.split("/") + ["", ""])[:3]
                    key = []
                    for k, lst in zip(parts, (v, vt, vn)):
                        key.append(None if k == "" else (int(k) - 1 if int(k) > 0 else len(lst) + int(k)))
                    key = tuple(key)
                    if key not in corners:
                        corners[key] = len(order)
                        order.append(key)
                    idx.append(corners[key])
                for i in range(1, len(idx) - 1):
                    faces.append([idx[0], idx[i], idx[i + 1]])
    if not order:
        return RefMesh(np.array(v, np.float64), None, [], None, f"obj:{os.path.basename(path)}",
                       ["no faces: raw v list used"])
    P = np.array([v[k[0]] for k in order], np.float64)
    N = np.array([vn[k[2]] for k in order], np.float64) if all(k[2] is not None for k in order) else None
    U = [np.array([vt[k[1]] for k in order], np.float64)] if all(k[1] is not None for k in order) else []
    return RefMesh(P, N, U, np.array(faces, np.int64), f"obj:{os.path.basename(path)}",
                   ["OBJ V is usually flipped (v' = 1 - v); the UV locator tries both"])


def load_psk(path):
    """ActorX PSK: per-vertex arrays follow the wedge list (UE buffer order for FModel/umodel)."""
    raw = read_bytes(path)
    pos = 0
    chunks = {}
    while pos + 32 <= len(raw):
        cid = raw[pos:pos + 20].split(b"\0")[0].decode("ascii", "replace")
        _, dsize, dcount = struct.unpack_from("<iii", raw, pos + 20)
        chunks[cid] = (pos + 32, dsize, dcount)
        pos += 32 + dsize * dcount
    if "PNTS0000" not in chunks:
        raise SystemExit(f"{path}: not a PSK (no PNTS0000)")
    p0, ps, pc = chunks["PNTS0000"]
    pts = np.frombuffer(raw, np.float32, pc * 3, p0).reshape(-1, 3).astype(np.float64)
    wk = "VTXW0000" if "VTXW0000" in chunks else "VTXW3200"
    w0, wsz, wc = chunks[wk]
    rec = np.frombuffer(raw, np.uint8, wc * wsz, w0).reshape(wc, wsz)
    pidx = rec[:, 0:4].copy().view(np.uint32).ravel().astype(np.int64)
    if pidx.max(initial=0) >= pc:
        pidx = rec[:, 0:2].copy().view(np.uint16).ravel().astype(np.int64)
    uv = rec[:, 4:12].copy().view(np.float32).reshape(-1, 2).astype(np.float64)
    faces = None
    for fk, fsz, it in (("FACE0000", 12, np.uint16), ("FACE3200", 18, np.uint32)):
        if fk in chunks:
            f0, _, fc = chunks[fk]
            frec = np.frombuffer(raw, np.uint8, fc * fsz, f0).reshape(fc, fsz)
            nb = np.dtype(it).itemsize
            faces = frec[:, :3 * nb].copy().view(it).reshape(-1, 3).astype(np.int64)
    normals = None
    if "VTXNORMS" in chunks:
        n0, _, ncnt = chunks["VTXNORMS"]
        nrm = np.frombuffer(raw, np.float32, ncnt * 3, n0).reshape(-1, 3).astype(np.float64)
        normals = nrm[pidx] if ncnt == pc else (nrm if ncnt == wc else None)
    uvs = [uv]
    if "EXTRAUVS0" in chunks:
        e0, _, ec = chunks["EXTRAUVS0"]
        if ec == wc:
            uvs.append(np.frombuffer(raw, np.float32, ec * 2, e0).reshape(-1, 2).astype(np.float64))
    return RefMesh(pts[pidx], normals, uvs, faces, f"psk:{os.path.basename(path)}",
                   ["PSK positions are Y-mirrored by the exporter (handled by the locator)"])


def load_ref_mesh(path, mesh_index="0") -> RefMesh:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".glb", ".gltf"):
        return load_gltf(path, mesh_index)
    if ext == ".obj":
        return load_obj(path)
    if ext in (".psk", ".pskx"):
        return load_psk(path)
    raise SystemExit(f"unsupported reference mesh format {ext} (use .glb/.gltf/.obj/.psk)")


# ============================================================================================
# Geometry helpers
# ============================================================================================
_KEY_BITS = 21
_KEY_BIAS = 1 << 20


def _combine_keys(ints):
    b = ints + _KEY_BIAS
    valid = np.all((b >= 0) & (b < (1 << _KEY_BITS)), axis=1)
    b = np.where(valid[:, None], b, 0)
    k = (b[:, 0] << 42) | (b[:, 1] << 21) | b[:, 2]
    k[~valid] = -1
    return k


def _round_keys(pts, cell):
    q = pts / cell
    ok = np.all(np.isfinite(q), axis=1) & np.all(np.abs(q) < _KEY_BIAS - 2, axis=1)
    q = np.where(ok[:, None], q, 0.0)
    k = _combine_keys(np.rint(q).astype(np.int64))
    k[~ok] = -1
    return k


class PointIndex:
    """Grid hash: query returns reference indices within ``tol`` (per axis) of each query point.
    Each reference point is stored under its 8 neighbouring cell keys (floor/floor+1 per axis) and
    queries use the rounded key, which finds any point closer than cell/2 (cell = 4*tol)."""

    def __init__(self, pts, tol):
        self.pts = np.asarray(pts, np.float64)
        self.tol = float(tol)
        self.cell = 4.0 * self.tol
        base = np.floor(self.pts / self.cell).astype(np.int64)
        keys, idx = [], []
        n = len(self.pts)
        for dx in (0, 1):
            for dy in (0, 1):
                for dz in (0, 1):
                    keys.append(_combine_keys(base + np.array([dx, dy, dz])))
                    idx.append(np.arange(n))
        keys = np.concatenate(keys)
        idx = np.concatenate(idx)
        if np.any(keys < 0):
            raise ValueError("reference coordinates out of key range (tolerance too small for this scale)")
        o = np.argsort(keys, kind="stable")
        self.keys, self.idx = keys[o], idx[o]
        self.ukeys = np.unique(self.keys)

    def contains_key(self, qkeys):
        pos = np.searchsorted(self.ukeys, qkeys)
        pos = np.minimum(pos, len(self.ukeys) - 1)
        return (self.ukeys[pos] == qkeys) & (qkeys >= 0)

    def query(self, q):
        """Returns (best, nmatch, qi, ci): nearest ref index or -1, number of refs within tol, and
        the flat (query, ref) candidate pairs within tol."""
        q = np.asarray(q, np.float64)
        kq = _round_keys(q, self.cell)
        lo = np.searchsorted(self.keys, kq, "left")
        hi = np.searchsorted(self.keys, kq, "right")
        cnt = np.where(kq >= 0, hi - lo, 0)
        tot = int(cnt.sum())
        best = np.full(len(q), -1, np.int64)
        nmatch = np.zeros(len(q), np.int64)
        if tot == 0:
            return best, nmatch, np.zeros(0, np.int64), np.zeros(0, np.int64)
        qi = np.repeat(np.arange(len(q)), cnt)
        offs = np.arange(tot) - np.repeat(np.cumsum(cnt) - cnt, cnt)
        ci = self.idx[np.repeat(lo, cnt) + offs]
        d = np.max(np.abs(q[qi] - self.pts[ci]), axis=1)
        ok = d <= self.tol
        qi, ci, d = qi[ok], ci[ok], d[ok]
        np.add.at(nmatch, qi, 1)
        o = np.lexsort((ci, d, qi))
        qs, cs = qi[o], ci[o]
        first = np.ones(len(qs), bool)
        first[1:] = qs[1:] != qs[:-1]
        best[qs[first]] = cs[first]
        return best, nmatch, qi, ci


def _signed_perms():
    out = []
    for perm in ((0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0)):
        for sx in (1, -1):
            for sy in (1, -1):
                for sz in (1, -1):
                    m = np.zeros((3, 3))
                    for row, (col, s) in enumerate(zip(perm, (sx, sy, sz))):
                        m[row, col] = s
                    out.append(m)
    return out


SIGNED_PERMS = _signed_perms()


def describe_transform(A, t):
    """Human readable name for u = r @ A + t (r = reference coordinates, u = UE cm)."""
    s = float(np.cbrt(abs(np.linalg.det(A)))) if np.linalg.det(A) != 0 else 0.0
    P = A / s if s else A
    known = {
        "UE native (cm)": np.eye(3),
        "glTF from FModel/CUE4Parse (ref = (X, Z, Y) * 0.01 m)": np.array([[1, 0, 0], [0, 0, 1], [0, 1, 0]]),
        "PSK/ActorX (Y mirrored)": np.diag([1, -1, 1]),
        "Y-up right-handed (ref = (X, Z, -Y))": np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]]),
    }
    for name, M in known.items():
        if np.allclose(P, M, atol=1e-6):
            return f"{name}; scale x{s:g}" + ("" if np.allclose(t, 0, atol=1e-6) else f"; translation {t.tolist()}")
    return f"matrix {np.round(A, 6).tolist()} translation {np.round(t, 6).tolist()}"


def _float_view(data, start, end, align):
    """float32 view of data[start+align:end] (junk bytes may decode to NaN/inf; callers mask them)."""
    a = start + align
    n = (end - a) // 4
    if n <= 0:
        return np.zeros(0, np.float32)
    return np.frombuffer(data, dtype="<f4", count=n, offset=a)


def _quiet_float_errors(fn):
    """Scanners reinterpret arbitrary bytes as floats; NaN/inf/overflow there is expected."""
    def wrapper(*args, **kwargs):
        with np.errstate(invalid="ignore", over="ignore", divide="ignore"):
            return fn(*args, **kwargs)
    wrapper.__name__, wrapper.__doc__ = fn.__name__, fn.__doc__
    return wrapper


def _runs(mask, min_len):
    """(start, length) of True runs with length >= min_len."""
    if len(mask) == 0:
        return []
    d = np.diff(np.concatenate(([0], mask.view(np.int8), [0])))
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1)
    return [(int(s), int(e - s)) for s, e in zip(starts, ends) if e - s >= min_len]


# ============================================================================================
# Mesh: position stream locator
# ============================================================================================
@dataclass
class PositionMatch:
    offset: int
    stride: int
    count: int
    A: np.ndarray            # u = r @ A + t
    t: np.ndarray
    mode: str
    mapping: np.ndarray      # per UE vertex: reference vertex index (-1 = unmatched)
    nmatch: np.ndarray       # per UE vertex: number of reference vertices at that position
    pairs: tuple             # (qi, ci) all (UE vertex, ref vertex) pairs within tolerance
    max_residual: float
    tol: float
    header: dict | None
    vb_meta: dict | None = None
    alternatives: list = field(default_factory=list)   # other axis permutations that fit the positions
    symmetry_tie: bool = False                          # True when an alternative explains the data equally well
    ref_count: int = 0

    @property
    def primary(self):
        """A complete stream for this reference: every stream vertex matched and at least as many
        vertices as the reference (subsets such as collision hulls or another LOD are secondary)."""
        return self.unmatched == 0 and self.count >= 0.98 * self.ref_count

    @property
    def unmatched(self):
        return int(np.sum(self.mapping < 0))

    @property
    def ambiguous(self):
        return int(np.sum(self.nmatch > 1))

    def positions_ue(self, data):
        return read_positions(data, self.offset, self.stride, self.count)

    def to_json(self):
        return {"offset": self.offset, "stride": self.stride, "count": self.count, "end": self.offset + self.stride * self.count,
                "mode": self.mode, "convention": describe_transform(self.A, self.t),
                "matrix_ref_to_ue": np.round(self.A, 9).tolist(), "translation": np.round(self.t, 9).tolist(),
                "unmatched_vertices": self.unmatched, "ambiguous_vertices": self.ambiguous,
                "identity_order": bool(np.array_equal(self.mapping, np.arange(self.count))),
                "max_residual_cm": self.max_residual, "tolerance_cm": self.tol,
                "fposition_vertex_buffer_header": self.header, "static_mesh_vertex_buffer_meta": self.vb_meta,
                "alternative_transforms": self.alternatives, "symmetry_tie": self.symmetry_tie,
                "primary": self.primary, "reference_vertices": self.ref_count}


def read_positions(data, offset, stride, count):
    if stride == 12:
        return np.frombuffer(data, "<f4", count * 3, offset).reshape(-1, 3).astype(np.float64)
    out = np.empty((count, 3))
    for j in range(count):
        out[j] = struct.unpack_from("<fff", data, offset + j * stride)
    return out


def _check_position_header(data, offset, stride, count):
    """FPositionVertexBuffer (UE4.19+): int32 Stride, int32 NumVertices, int32 elemSize, int32 count."""
    if offset < 16:
        return None
    s, n, es, c = struct.unpack_from("<iiii", data, offset - 16)
    if s == stride and es == stride and n == c and n >= 1:
        return {"offset": offset - 16, "stride": s, "num_vertices": n, "matches_run": n == count}
    return None


def _read_vb_meta(data, pos_end, count):
    """FStaticMeshVertexBuffer right after the positions in cooked LODs: FStripDataFlags (2 bytes),
    NumTexCoords, NumVertices, bUseFullPrecisionUVs, bUseHighPrecisionTangentBasis, then the
    tangent bulk array header."""
    if pos_end + 26 > len(data):
        return None
    ntc, nv, full_uv, high_tan, tsize, tcount = struct.unpack_from("<iiIIii", data, pos_end + 2)
    if nv != count or not (1 <= ntc <= 8) or full_uv > 1 or high_tan > 1:
        return None
    return {"offset": pos_end, "num_tex_coords": ntc, "num_vertices": nv, "full_precision_uvs": bool(full_uv),
            "high_precision_tangents": bool(high_tan), "tangents_offset": pos_end + 26,
            "tangent_item_size": tsize, "tangent_count": tcount}


def _finish_match(data, offset, stride, count, A, t, ref, tol, mode):
    U = read_positions(data, offset, stride, count)
    Q = ref.positions @ A + t
    idx = PointIndex(Q, tol)
    best, nmatch, qi, ci = idx.query(U)
    ok = best >= 0
    resid = float(np.max(np.abs(U[ok] - Q[best[ok]]))) if ok.any() else float("inf")
    hdr = _check_position_header(data, offset, stride, count)
    meta = _read_vb_meta(data, offset + stride * count, count) if stride == 12 else None
    return PositionMatch(offset, stride, count, A, t, mode, best, nmatch, (qi, ci), resid, tol, hdr, meta,
                         ref_count=ref.count)


def _detect_perm(U_sample, R_scaled, tol):
    """Vote for the signed permutation P with U = R_scaled @ P using sorted-|xyz| correspondences."""
    absidx = PointIndex(np.sort(np.abs(R_scaled), axis=1), tol)
    best, _, qi, ci = absidx.query(np.sort(np.abs(U_sample), axis=1))
    votes = np.zeros(len(SIGNED_PERMS), np.int64)
    for k, P in enumerate(SIGNED_PERMS):
        d = np.max(np.abs(U_sample[qi] - R_scaled[ci] @ P), axis=1)
        votes[k] = len(np.unique(qi[d <= tol]))
    return int(np.argmax(votes)), votes


def _order_score(mapping):
    """Fraction of consecutive stream vertices whose reference indices increase (1.0 = same order)."""
    m = mapping[mapping >= 0]
    return float(np.mean(np.diff(m) > 0)) if len(m) > 1 else 0.0


def _rank_candidates(data, cands, ref, start, end):
    """Pick the axis permutation when several fit the positions (symmetric meshes: a mirrored
    permutation maps left vertices onto right ones). Ranking: UV + tangent agreement, then vertex
    order correlation. An unresolved tie is flagged (patching then refuses)."""
    if len(cands) == 1:
        return cands[0]
    scored = []
    for pm in cands:
        uv = locate_uvs(data, pm, ref, start, end, n_samples=16, max_channels=2) if ref.uvs else []
        tg = locate_tangents(data, pm, ref, start, end, n_samples=16) if ref.normals is not None else []
        attr = max([m.score for m in uv], default=0.0) + max([m.score for m in tg], default=0.0)
        scored.append((round(attr, 6), round(_order_score(pm.mapping), 6), pm))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    best = scored[0][2]
    best.alternatives = [{"matrix_ref_to_ue": np.round(p.A, 6).tolist(), "attribute_score": a, "order_score": o}
                         for a, o, p in scored]
    best.symmetry_tie = scored[1][0] == scored[0][0] and abs(scored[1][1] - scored[0][1]) < 0.02
    return best


@_quiet_float_errors
def locate_positions(data, ref: RefMesh, start=0, end=None, strides=(12,), scales=(100.0, 1.0, 0.01),
                     min_fraction=0.5, tol_rel=1e-4, tol_abs=None, try_ordered=True, exhaustive=False):
    """Find float32 xyz streams whose points are (a transformed copy of) the reference points.

    Set mode (handles re-ordered/duplicated vertices, e.g. FModel glTF): for each scale, the sorted
    absolute coordinates of every float triplet in the file are hashed and looked up in the hashed
    reference set (invariant to axis swaps and sign flips); runs of hits at a fixed stride are
    candidate streams. The signed axis permutation is then voted from the hits and every stream
    vertex is mapped to the reference vertex at the same position.
    Ordered mode (fallback for arbitrary rotations): vertex i of the stream must be reference vertex
    i; candidates are found with similarity-invariant distance ratios and fitted by least squares.
    """
    end = len(data) if end is None else end
    R = ref.positions
    found = {}
    uniq = len(np.unique(np.round(R, 6), axis=0))
    min_run = max(8, int(min_fraction * uniq))
    for c in scales:
        if not exhaustive and any(m.primary and m.header for m in found.values()):
            break  # a complete, header-confirmed stream was found with an earlier scale
        Rc = R * c
        diag = float(np.linalg.norm(Rc.max(0) - Rc.min(0))) or 1.0
        tol = tol_abs if tol_abs is not None else max(tol_rel * diag, 1e-6)
        try:
            absidx = PointIndex(np.sort(np.abs(Rc), axis=1), tol)
        except ValueError:
            continue
        for al in range(4):
            F = _float_view(data, start, end, al)
            if len(F) < 3:
                continue
            W = np.lib.stride_tricks.sliding_window_view(F, 3)
            hits = np.zeros(len(W), bool)
            for s0 in range(0, len(W), 1 << 20):
                blk = np.sort(np.abs(W[s0:s0 + (1 << 20)].astype(np.float64)), axis=1)
                hits[s0:s0 + len(blk)] = absidx.contains_key(_round_keys(blk, absidx.cell))
            for stride in strides:
                if stride % 4:
                    continue
                q = stride // 4
                for r in range(q):
                    for s, ln in _runs(hits[r::q], min_run):
                        off = start + al + 4 * (r + q * s)
                        key = (off, stride)
                        if key in found:
                            continue
                        U = read_positions(data, off, stride, ln)
                        sample = U[np.linspace(0, ln - 1, min(ln, 256)).astype(int)]
                        k, votes = _detect_perm(sample, Rc, tol)
                        if votes[k] < 0.5 * len(sample):
                            continue
                        hdr = _check_position_header(data, off, stride, ln)
                        count = hdr["num_vertices"] if hdr and hdr["num_vertices"] >= ln and off + stride * hdr["num_vertices"] <= end else ln
                        cands = []
                        for kk in np.flatnonzero(votes >= votes[k] - max(1, 0.02 * len(sample))):
                            pm = _finish_match(data, off, stride, count, SIGNED_PERMS[kk] * c, np.zeros(3), ref, tol, "set")
                            if pm.unmatched <= 0.02 * count:
                                cands.append(pm)
                        if cands:
                            found[key] = _rank_candidates(data, cands, ref, start, end)
    if not any(pm.primary for pm in found.values()) and try_ordered:
        for pm in _locate_ordered(data, ref, start, end, strides, tol_rel):
            found[(pm.offset, pm.stride)] = pm
    # primary (complete) streams first, then FPositionVertexBuffer-header confirmed, then larger;
    # a candidate overlapping a better one is a shifted/partial re-read of the same bytes: drop it
    ranked = sorted(found.values(), key=lambda m: (not m.primary, m.header is None, -m.count, m.offset))
    kept = []
    for m in ranked:
        if all(m.offset + m.stride * m.count <= k.offset or k.offset + k.stride * k.count <= m.offset for k in kept):
            kept.append(m)
    return kept


@_quiet_float_errors
def _locate_ordered(data, ref, start, end, strides, tol_rel):
    R = ref.positions
    N = len(R)
    if N < 4:
        return []
    i0 = 0
    ia = int(np.argmax(np.linalg.norm(R - R[i0], axis=1)))
    d0a = R[ia] - R[i0]
    cr = np.linalg.norm(np.cross(R - R[i0], d0a), axis=1)
    ib = int(np.argmax(cr))
    d1 = np.linalg.norm(R[ia] - R[i0])
    if d1 == 0 or cr[ib] == 0:
        return []
    r1 = np.linalg.norm(R[ib] - R[i0]) / d1
    r2 = np.linalg.norm(R[ib] - R[ia]) / d1
    out = []
    for al in range(4):
        F = _float_view(data, start, end, al).astype(np.float64)
        for stride in strides:
            q = stride // 4
            K = len(F) - (N - 1) * q - 2
            if K <= 0:
                continue

            cand_list = []
            for k0 in range(0, K, 1 << 20):
                k1 = min(K, k0 + (1 << 20))

                def vec(i, k0=k0, k1=k1):
                    return np.stack([F[k0 + i * q + c:k1 + i * q + c] for c in range(3)], axis=1)
                v0, va, vb = vec(i0), vec(ia), vec(ib)
                e1 = np.linalg.norm(va - v0, axis=1)
                e2 = np.linalg.norm(vb - v0, axis=1) / e1
                e3 = np.linalg.norm(vb - va, axis=1) / e1
                cand_list.append(k0 + np.flatnonzero(np.isfinite(e2) & (np.abs(e2 - r1) < 1e-4 * max(1, r1)) &
                                                     (np.abs(e3 - r2) < 1e-4 * max(1, r2)) & (e1 > 0)))
            cand = np.concatenate(cand_list)
            for k in cand[:64]:
                off = start + al + 4 * int(k)
                U = read_positions(data, off, stride, N)
                X = np.hstack([R, np.ones((N, 1))])
                sol, *_ = np.linalg.lstsq(X, U, rcond=None)
                A, t = sol[:3], sol[3]
                resid = np.max(np.abs(X @ sol - U))
                diag = float(np.linalg.norm(U.max(0) - U.min(0))) or 1.0
                if resid <= max(tol_rel * diag, 1e-6):
                    pm = _finish_match(data, off, stride, N, A, t, ref, max(tol_rel * diag, 1e-6), "ordered")
                    out.append(pm)
    return out


# ============================================================================================
# Mesh: tangent / UV streams and bounds
# ============================================================================================
@dataclass
class TangentMatch:
    offset: int
    stride: int
    encoding: str            # int8 | int16 | uint8_biased
    normal_field: int        # byte offset of TangentZ inside the vertex
    score: float
    header: dict | None

    def to_json(self):
        return {"offset": self.offset, "stride": self.stride, "encoding": self.encoding,
                "tangent_x_field": 0, "tangent_z_field": self.normal_field,
                "match_fraction": round(self.score, 5),
                "bulk_array_header": self.header}


TANGENT_VARIANTS = [("int8", 8, 4), ("int16", 16, 8), ("uint8_biased", 8, 4)]


def _strided(data, base, dtype, ncomp):
    """(len(base), ncomp) array of ``dtype`` read at byte offsets ``base``. A contiguous run of
    offsets (the first full-range filter) uses a 1-byte-strided view instead of a gather."""
    base = np.asarray(base, np.int64)
    dt = np.dtype(dtype)
    if len(base) > 1 and base[-1] - base[0] == len(base) - 1 and np.all(np.diff(base[:2]) == 1):
        return np.ndarray((len(base), ncomp), dtype=dt, buffer=data, offset=int(base[0]),
                          strides=(1, dt.itemsize)).copy()
    raw = np.frombuffer(data, np.uint8)
    nb = dt.itemsize * ncomp
    return raw[base[:, None] + np.arange(nb)].copy().view(dt).reshape(len(base), ncomp)


def _decode_packed(data, base, encoding):
    """Decode 4 packed tangent components at absolute byte offsets ``base`` -> (n, 4) float.
    int8 / int16 are UE4.20+ FPackedNormal / FPackedRGBA16N; uint8_biased is the pre-4.20 form."""
    if encoding == "int16":
        return _strided(data, base, "<i2", 4).astype(np.float64) / 32767.0
    if encoding == "int8":
        return _strided(data, base, np.int8, 4).astype(np.float64) / 127.0
    return _strided(data, base, np.uint8, 4).astype(np.float64) / 127.5 - 1.0


def _encode_packed(v, encoding):
    v = np.clip(v, -1, 1)
    if encoding == "int16":
        return np.rint(v * 32767).astype("<i2").tobytes()
    if encoding == "int8":
        return np.rint(v * 127).astype(np.int8).tobytes()
    return np.clip(np.rint((v + 1) * 127.5), 0, 255).astype(np.uint8).tobytes()


def normal_matrix(A):
    """Normals transform with the inverse transpose (row vectors: n_ue ~ n_ref @ inv(A).T)."""
    return np.linalg.inv(A).T


def _candidate_lists(pm: PositionMatch):
    """(has_candidate mask, {ue vertex: array of reference vertices at its position})."""
    qi, ci = pm.pairs
    has = np.zeros(pm.count, bool)
    has[qi] = True
    order = np.argsort(qi, kind="stable")
    qs, cs = qi[order], ci[order]
    bounds = np.searchsorted(qs, np.arange(pm.count + 1))
    return has, (lambda j: cs[bounds[j]:bounds[j + 1]]), (qi, ci)


def _sample_vertices(pm: PositionMatch, has, n):
    """Spread-out sample of stream vertices, preferring few candidate reference vertices (cheap and
    selective tests), ordered by candidate count so the first full-range filter is the cheapest."""
    ncand = np.bincount(pm.pairs[0], minlength=pm.count)
    vidx = np.flatnonzero(has & (ncand <= 4))
    if len(vidx) < min(8, int(has.sum())):
        vidx = np.flatnonzero(has)
    if not len(vidx):
        return vidx
    pick = vidx[np.linspace(0, len(vidx) - 1, min(n, len(vidx))).astype(int)]
    return pick[np.argsort(ncand[pick], kind="stable")]


def _best_per_vertex(n, qi, values, init):
    best = np.full(n, init, np.float64)
    if init < 0:
        np.maximum.at(best, qi, values)
    else:
        np.minimum.at(best, qi, values)
    return best


def _search_windows(pm: PositionMatch, lo, hi):
    """Where to look for the other vertex streams of the LOD: first right after the positions
    (cooked LOD order: FPositionVertexBuffer, FStaticMeshVertexBuffer = meta + tangents +
    texcoords; CUE4Parse FStaticLODModel.SerializeRenderItem), then the whole range."""
    near_lo = pm.offset + pm.stride * pm.count
    near_hi = min(hi, near_lo + 64 + pm.count * (16 + 4 * 8) + 64)
    return [(max(lo, near_lo), near_hi), (lo, hi)]


def _progressive_search(data, lo, hi, count, stride, field, samples, test, max_cands=64, chunk=1 << 18):
    """Offsets o in [lo, hi - count*stride] such that ``test(j, decode at o + j*stride + field)``
    holds for every sample vertex j. Byte offsets are scanned in chunks (bounded memory); each
    chunk is filtered by the first sample, then narrowed by the others."""
    last = hi - count * stride
    out = []
    for c0 in range(lo, last + 1, chunk):
        cand = np.arange(c0, min(c0 + chunk, last + 1), dtype=np.int64)
        for j in samples:
            if len(cand) == 0:
                break
            cand = cand[test(j, cand + j * stride + field)]
        out.extend(int(x) for x in cand)
        if len(out) >= max_cands:
            break
    return np.array(out[:max_cands], np.int64)


@_quiet_float_errors
def locate_tangents(data, pm: PositionMatch, ref: RefMesh, start=0, end=None, n_samples=24):
    """Find the tangent stream (TangentX, TangentZ per vertex) by matching TangentZ against the
    reference normals transformed into UE space. A stream vertex matches when its normal agrees
    with ANY reference vertex at its position (seams, hard edges and poles share positions)."""
    if ref.normals is None:
        return []
    end = len(data) if end is None else end
    n_ue = ref.normals @ normal_matrix(pm.A)
    n_ue /= np.maximum(np.linalg.norm(n_ue, axis=1, keepdims=True), 1e-12)
    has, cands, (qi, ci) = _candidate_lists(pm)
    samples = _sample_vertices(pm, has, n_samples)
    if len(samples) < min(8, pm.count):
        return []
    for lo_w, hi_w in _search_windows(pm, start, end):
        out = _search_tangents_in(data, pm, n_ue, has, cands, qi, ci, samples, lo_w, hi_w)
        if out:
            return out
    return []


def _search_tangents_in(data, pm, n_ue, has, cands, qi, ci, samples, start, end):
    out = []
    for enc, stride, nfield in TANGENT_VARIANTS:
        def test(j, pos, enc=enc):
            v = _decode_packed(data, pos, enc)[:, :3]
            ln = np.linalg.norm(v, axis=1)
            dot = np.max((v @ n_ue[cands(j)].T), axis=1) / ln
            return (np.abs(ln - 1) < 0.06) & (dot > 0.97)
        for o in _progressive_search(data, start, end, pm.count, stride, nfield, samples, test):
            v = _decode_packed(data, o + np.arange(pm.count) * stride + nfield, enc)[:, :3]
            v /= np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-12)
            best = _best_per_vertex(pm.count, qi, np.sum(v[qi] * n_ue[ci], axis=1), -np.inf)
            score = float(np.mean(best[has] > 0.95))
            if score >= 0.95:
                hdr = None
                if o >= 8:
                    isz, icnt = struct.unpack_from("<ii", data, o - 8)
                    if isz == stride and icnt == pm.count:
                        hdr = {"offset": o - 8, "item_size": isz, "count": icnt}
                out.append(TangentMatch(int(o), stride, enc, nfield, score, hdr))
    # prefer header-confirmed, then exact encodings over the biased legacy one
    out.sort(key=lambda m: (m.header is None, m.encoding == "uint8_biased", -m.score))
    return out


@dataclass
class UVMatch:
    offset: int              # absolute offset of this channel's first UV
    stride: int
    precision: str           # half | float
    channel: int             # channel index inside the vertex (field offset = channel * elem)
    ref_channel: int
    flip_v: bool
    score: float
    header: dict | None

    def to_json(self):
        return {"offset": self.offset, "stride": self.stride, "precision": self.precision,
                "ue_channel": self.channel, "ref_channel": self.ref_channel, "flip_v": self.flip_v,
                "num_tex_coords": self.stride // (4 if self.precision == "half" else 8),
                "match_fraction": round(self.score, 5), "bulk_array_header": self.header}


def _decode_uv(data, pos, precision):
    """FVector2DHalf (half) or FVector2D (float) at absolute byte offsets ``pos`` -> (n, 2)."""
    with np.errstate(invalid="ignore", over="ignore"):
        return _strided(data, pos, "<f2" if precision == "half" else "<f4", 2).astype(np.float64)


@_quiet_float_errors
def locate_uvs(data, pm: PositionMatch, ref: RefMesh, start=0, end=None, n_samples=24, max_channels=4):
    """Find texcoord streams: half (FVector2DHalf) or float UVs, NumTexCoords 1..max_channels
    interleaved per vertex; V may be flipped by the exporter (OBJ). Any-candidate matching as for
    tangents."""
    end = len(data) if end is None else end
    has, cands, (qi, ci) = _candidate_lists(pm)
    samples = _sample_vertices(pm, has, n_samples)
    if len(samples) < min(8, pm.count):
        return []
    for lo_w, hi_w in _search_windows(pm, start, end):
        out = _search_uvs_in(data, pm, ref, has, cands, qi, ci, samples, lo_w, hi_w, max_channels)
        if out:
            return out
    return []


def _search_uvs_in(data, pm, ref, has, cands, qi, ci, samples, start, end, max_channels):
    out = []
    for rc, uv in enumerate(ref.uvs):
        for flip in (False, True):
            u = uv.copy()
            if flip:
                u[:, 1] = 1.0 - u[:, 1]
            for prec, elem, tol in (("half", 4, 2e-3), ("float", 8, 1e-5)):
                for k in range(1, max_channels + 1):
                    stride = k * elem
                    for c in range(k):
                        def test(j, pos, prec=prec, tol=tol, u=u):
                            v = _decode_uv(data, pos, prec)
                            e = u[cands(j)]
                            d = np.abs(v[:, None, :] - e[None, :, :]) <= tol + tol * np.abs(e[None, :, :])
                            return np.any(np.all(d, axis=2), axis=1)
                        for o in _progressive_search(data, start, end, pm.count, stride, c * elem, samples, test):
                            v = _decode_uv(data, o + c * elem + np.arange(pm.count) * stride, prec)
                            err = np.max(np.abs(v[qi] - u[ci]) / (1 + np.abs(u[ci])), axis=1)
                            best = _best_per_vertex(pm.count, qi, err, np.inf)
                            score = float(np.mean(best[has] <= 2 * tol))
                            if score < 0.95:
                                continue
                            hdr = None
                            if o >= 8:
                                isz, icnt = struct.unpack_from("<ii", data, o - 8)
                                if isz == elem and icnt == pm.count * k:
                                    hdr = {"offset": o - 8, "item_size": isz, "count": icnt}
                            out.append(UVMatch(int(o + c * elem), stride, prec, c, rc, flip, score, hdr))
    # keep the most specific explanation per offset: header confirmed first, then the smallest k
    best = {}
    for m in sorted(out, key=lambda m: (m.header is None, -m.score, m.stride)):
        best.setdefault((m.offset, m.ref_channel), m)
    return sorted(best.values(), key=lambda m: (m.header is None, m.offset))


@dataclass
class BoundsMatch:
    offset: int
    origin: list
    extent: list
    radius: float
    score: float
    method: str

    def to_json(self):
        return {"offset": self.offset, "origin": self.origin, "box_extent": self.extent,
                "sphere_radius": self.radius, "score": round(self.score, 6), "method": self.method}


@_quiet_float_errors
def locate_bounds(data, pts_ue, start=0, end=None, slack=0.25, old_bounds=None, tol_rel=1e-4):
    """FBoxSphereBounds (7 x float32: Origin, BoxExtent, SphereRadius).

    With ``old_bounds`` (e.g. ImportedBounds from an FModel JSON dump) the exact values are
    searched. Otherwise every 7-float window is tested: extents > 0, max(extent) <= radius <=
    |extent|, the box contains the vertex AABB and is at most ``slack`` larger per axis.
    """
    end = len(data) if end is None else end
    mn, mx = pts_ue.min(0), pts_ue.max(0)
    size = mx - mn
    diag = float(np.linalg.norm(size)) or 1.0
    eps = 1e-3 * diag
    out = []
    for al in range(4):
        F = _float_view(data, start, end, al)
        if len(F) < 7:
            continue
        for s0 in range(0, len(F) - 6, 1 << 20):
            W = np.lib.stride_tricks.sliding_window_view(F[s0:s0 + (1 << 20) + 6], 7)[:1 << 20].astype(np.float64)
            with np.errstate(all="ignore"):
                if old_bounds is not None:
                    ob = np.asarray(old_bounds, np.float64)
                    ok = np.all(np.abs(W - ob) <= tol_rel * np.maximum(np.abs(ob), 1.0), axis=1)
                    method = "explicit"
                else:
                    O, E, Rr = W[:, 0:3], W[:, 3:6], W[:, 6]
                    ok = np.all(E > 0, axis=1) & np.isfinite(Rr)
                    ok &= Rr >= E.max(1) * (1 - 1e-4)
                    ok &= Rr <= np.linalg.norm(E, axis=1) * (1 + 1e-4)
                    ok &= np.all(O - E <= mn + eps, axis=1) & np.all(O + E >= mx - eps, axis=1)
                    ok &= np.all(2 * E <= size * (1 + slack) + eps, axis=1)
                    method = "aabb"
            for k in np.flatnonzero(ok):
                w = W[k]
                score = float(np.sum(np.abs((w[0:3] - w[3:6]) - mn)) + np.sum(np.abs((w[0:3] + w[3:6]) - mx))) / diag
                out.append(BoundsMatch(start + al + 4 * (s0 + int(k)), w[0:3].tolist(), w[3:6].tolist(),
                                       float(w[6]), score, method))
    return sorted(out, key=lambda b: b.score)


def new_bounds(old: BoundsMatch, old_pts, new_pts, policy):
    """New (origin, extent, radius) as float32-ready lists."""
    o_old = np.array(old.origin)
    e_old = np.array(old.extent)
    if policy == "union":
        if np.all(o_old - e_old <= new_pts.min(0)) and np.all(o_old + e_old >= new_pts.max(0)) and \
                float(np.max(np.linalg.norm(new_pts - o_old, axis=1))) <= old.radius:
            return list(old.origin), list(old.extent), float(old.radius)  # already encloses: keep bytes
        mn = np.minimum(o_old - e_old, new_pts.min(0))
        mx = np.maximum(o_old + e_old, new_pts.max(0))
        o = (mn + mx) / 2
        e = (mx - mn) / 2
        r = max(old.radius + float(np.linalg.norm(o - o_old)), float(np.max(np.linalg.norm(new_pts - o, axis=1))))
    elif policy == "recompute":
        margin_lo = (old_pts.min(0)) - (o_old - e_old)
        margin_hi = (o_old + e_old) - old_pts.max(0)
        mn = new_pts.min(0) - margin_lo
        mx = new_pts.max(0) + margin_hi
        o = (mn + mx) / 2
        e = (mx - mn) / 2
        r_margin = old.radius - float(np.max(np.linalg.norm(old_pts - o_old, axis=1)))
        r = float(np.max(np.linalg.norm(new_pts - o, axis=1))) + max(r_margin, 0.0)
    else:
        raise ValueError(policy)
    r = min(max(r, float(e.max())), float(np.linalg.norm(e)))  # keep the struct invariants
    return o.tolist(), e.tolist(), float(r)


def refine_mapping(pm: PositionMatch, data, ref: RefMesh, tm: TangentMatch | None, uvm: list):
    """Resolve UE vertices whose position matches several reference vertices using the located
    UV and normal streams. Returns the number of vertices that stay ambiguous."""
    qi, ci = pm.pairs
    amb = np.flatnonzero(pm.nmatch > 1)
    if len(amb) == 0:
        return 0
    cost = np.zeros(len(qi))
    used = False
    for m in uvm:
        uv = ref.uvs[m.ref_channel].copy()
        if m.flip_v:
            uv[:, 1] = 1 - uv[:, 1]
        dec = _decode_uv(data, m.offset + np.arange(pm.count) * m.stride, m.precision)
        cost += np.sum(np.abs(dec[qi] - uv[ci]), axis=1)
        used = True
    if tm is not None and ref.normals is not None:
        n_ue = ref.normals @ normal_matrix(pm.A)
        n_ue /= np.maximum(np.linalg.norm(n_ue, axis=1, keepdims=True), 1e-12)
        dec = _decode_packed(data, tm.offset + np.arange(pm.count) * tm.stride + tm.normal_field, tm.encoding)[:, :3]
        dec /= np.maximum(np.linalg.norm(dec, axis=1, keepdims=True), 1e-12)
        cost += 1 - np.sum(dec[qi] * n_ue[ci], axis=1)
        used = True
    if not used:
        return len(amb)
    o = np.lexsort((cost, qi))
    qs, cs, cc = qi[o], ci[o], cost[o]
    first = np.ones(len(qs), bool)
    first[1:] = qs[1:] != qs[:-1]
    best_cost = np.full(pm.count, np.inf)
    best_cost[qs[first]] = cc[first]
    pm.mapping[qs[first]] = cs[first]
    # a vertex stays ambiguous when two candidates explain it equally well
    second = np.zeros(len(qs), bool)
    second[1:] = (~first[1:]) & first[:-1]
    still = np.zeros(pm.count, bool)
    still[qs[second]] = np.abs(cc[second] - best_cost[qs[second]]) < 1e-6
    # identical reference vertices (same position/UV/normal) are not ambiguous for patching
    pm.nmatch = np.where(still, pm.nmatch, 1)
    return int(still.sum())


def locate_all(data, ref, start=0, end=None, strides=(12,), scales=(100.0, 1.0, 0.01), tol_rel=1e-4,
               want_tangents=True, want_uvs=True, want_bounds=True, old_bounds=None, bounds_data=None):
    pms = locate_positions(data, ref, start, end, strides, scales, tol_rel=tol_rel)
    report = []
    for pm in pms:
        tms = locate_tangents(data, pm, ref, start, end) if want_tangents else []
        uvs = locate_uvs(data, pm, ref, start, end) if want_uvs else []
        best_uvs = [m for m in uvs if m.header] or uvs[:1]
        remaining = refine_mapping(pm, data, ref, tms[0] if tms else None, best_uvs)
        entry = {"positions": pm, "tangents": tms, "uvs": uvs, "ambiguous_after_refine": remaining}
        report.append(entry)
    bounds = []
    if want_bounds and pms:
        allpts = np.concatenate([pm.positions_ue(data) for pm in pms])
        bd = data if bounds_data is None else bounds_data
        bounds = locate_bounds(bd, allpts, 0, len(bd), old_bounds=old_bounds)
    return report, bounds


def report_json(report, bounds, ref):
    streams = []
    for i, e in enumerate(report):
        d = {"stream": i, "positions": e["positions"].to_json(),
             "tangents": [t.to_json() for t in e["tangents"]], "uvs": [u.to_json() for u in e["uvs"]],
             "ambiguous_after_refine": e["ambiguous_after_refine"]}
        for t in d["tangents"]:
            t["end"] = t["offset"] + t["stride"] * e["positions"].count
        streams.append(d)
    return {"reference": {"source": ref.source, "vertices": ref.count,
                          "faces": None if ref.faces is None else int(len(ref.faces)),
                          "has_normals": ref.normals is not None, "uv_channels": len(ref.uvs), "notes": ref.notes},
            "streams": streams, "bounds_candidates": [b.to_json() for b in bounds[:16]],
            "lod_note": "Each LOD has its own position/tangent/UV buffers. Locate and patch LOD0 and LOD1 "
                        "separately with a reference exported from that LOD; a stream found here belongs to "
                        "the LOD the reference was exported from."}


# ============================================================================================
# Normal / tangent recompute (delta method)
# ============================================================================================
def vertex_normals(P, F, weld_groups):
    """Area-weighted vertex normals, averaged across vertices that share a welded position."""
    v0, v1, v2 = P[F[:, 0]], P[F[:, 1]], P[F[:, 2]]
    fn = np.cross(v1 - v0, v2 - v0)
    acc = np.zeros_like(P)
    for k in range(3):
        np.add.at(acc, F[:, k], fn)
    g = np.zeros((weld_groups.max() + 1, 3))
    np.add.at(g, weld_groups, acc)
    n = g[weld_groups]
    return n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-20)


def weld_groups_by_position(P):
    """Group vertices with bit-identical float32 positions (UV seams / hard edges split a vertex
    into copies that exporters write with identical coordinates)."""
    _, inv = np.unique(np.ascontiguousarray(P, dtype=np.float32), axis=0, return_inverse=True)
    return inv.ravel()


# ============================================================================================
# Commands: texture
# ============================================================================================
def cmd_texture_scan(a):
    t = load_target(a.input, a.zen_export, a.raw)
    unsupported = []
    pds = scan_texture_platform_data(t, unsupported)
    doc = {"target": t.describe(), "platform_data": [p.to_json() for p in pds], "unsupported": unsupported}
    _emit_report(doc, a.json)
    if not a.json:
        print(f"{len(pds)} FTexturePlatformData block(s) in {a.input} [{t.start}, {t.end})")
        for u in unsupported:
            print(f"UNSUPPORTED @{u['platform_data_offset']} {u['pixel_format']} {u['size']}: {u['reason']}")
        for i, p in enumerate(pds):
            print(f"[{i}] {p.pixel_format} {p.size_x}x{p.size_y} slices={p.num_slices} variant={p.variant} "
                  f"first_mip={p.first_mip} mips={len(p.mips)} at {p.start}..{p.end} skip={p.skip_offset}")
            for m in p.mips:
                b = m.bulk
                loc = f"inline@{b.payload_pos}" if b.location == "inline" else f"{b.location}+{b.offset_in_file}"
                print(f"    mip{m.index}: {m.size_x}x{m.size_y}x{m.size_z} size={b.size_on_disk} "
                      f"flags={'|'.join(bulk_flag_names(b.flags))} -> {loc} ({m.dims_rule})")


def _emit_report(doc, json_path):
    if json_path:
        text = json.dumps(doc, indent=1, default=_json_default)
        if json_path == "-":
            print(text)
        else:
            if os.path.exists(json_path):
                raise SystemExit(f"refusing to overwrite {json_path}")
            with open(json_path, "x", encoding="utf-8") as f:
                f.write(text)
            print("wrote", json_path)


def _json_default(o):
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(type(o))


def _mip_payload(pd, mip, t, bulk_files):
    b = mip.bulk
    if b.location == "inline":
        return t.data[b.payload_pos:b.payload_pos + b.size_on_disk], ("package", b.payload_pos)
    if b.location in bulk_files:
        path = bulk_files[b.location]
        if not path:
            return None, (b.location, b.offset_in_file)
        if not b.flags & BULKDATA_NoOffsetFixUp:
            raise SystemExit(f"refusing: mip {mip.index} bulk offset needs fix-up (no NoOffsetFixUp flag); "
                             "location not verified on real data")
        data = bulk_files["_data"][b.location]
        end = b.offset_in_file + b.size_on_disk
        if end > len(data):
            raise SystemExit(f"refusing: mip {mip.index} bulk range {b.offset_in_file}..{end} past end of {path}")
        return data[b.offset_in_file:end], (b.location, b.offset_in_file)
    return None, (b.location, b.offset_in_file)


def _load_bulk_files(a):
    files = _bulk_inputs(a)
    files["_data"] = {k: read_bytes(v) for k, v in files.items() if v}
    return files


def cmd_texture_extract(a):
    t = load_target(a.input, a.zen_export, a.raw)
    unsupported = []
    pd = select_platform_data(scan_texture_platform_data(t, unsupported), a.index, unsupported)
    bulk = _load_bulk_files(a)
    inputs = {"package": a.input, **{k: v for k, v in _bulk_inputs(a).items() if v}}
    # resolve every payload first: a refusal (e.g. offset fix-up) must not leave a half-written dir
    payloads = [_mip_payload(pd, m, t, bulk) for m in pd.mips]
    out = zp.prepare_out_dir(a.out_dir, list(inputs.values()))
    man = base_manifest("texture-extract", inputs, {"index": a.index, "zen_export": a.zen_export})
    man["platform_data"] = pd.to_json()
    blk = pixel_format_block(pd.pixel_format)
    for m, (payload, where) in zip(pd.mips, payloads):
        if payload is None:
            man.setdefault("missing", []).append({"mip": m.index, "location": where})
            continue
        p = zp.write_output(out, f"mip{m.index}_{m.size_x}x{m.size_y}.bin", payload, list(inputs.values()))
        rec = zp.file_record(p)
        rec["source"] = {"file": where[0], "offset": where[1]}
        if a.decode and pd.pixel_format.startswith("PF_ASTC_") and not pd.pixel_format.endswith("_HDR"):
            import astc_tool
            img = astc_tool.decode_raw(payload, m.size_x, m.size_y, blk[0], blk[1], srgb=not a.linear)
            pp = os.path.join(out, f"mip{m.index}_{m.size_x}x{m.size_y}.png")
            with open(pp, "xb") as f:  # exclusive create, like every other output
                img.save(f, format="PNG")
            rec["png"] = zp.file_record(pp)
        man["outputs"][f"mip{m.index}"] = rec
    astc = _ASTC_RE.match(pd.pixel_format)
    if astc and not astc.group(3) and not man.get("missing"):
        # astc_tool.py-compatible manifest of the donor's own mips: feeding it back to
        # texture-replace must reproduce the input byte for byte (identity test)
        blob = bytearray()
        mips_meta = []
        for m in pd.mips:
            payload, _ = _mip_payload(pd, m, t, bulk)
            mips_meta.append({"level": m.index, "w": m.size_x, "h": m.size_y, "offset": len(blob),
                              "size": len(payload), "sha256": zp.sha256_bytes(payload)})
            blob += payload
        stem = f"donor.astc{blk[0]}x{blk[1]}"
        bp = zp.write_output(out, stem + ".bin", bytes(blob), list(inputs.values()))
        amj = {"source": os.path.basename(a.input), "block": [blk[0], blk[1]], "srgb": None, "normal": None,
               "mips": mips_meta, "total_size": len(blob), "sha256": zp.sha256_bytes(bytes(blob)),
               "note": "extracted by inplace_patch.py texture-extract (sRGB/normal flags live in UTexture properties)"}
        jp = os.path.join(out, stem + ".json")
        with open(jp, "x", encoding="utf-8") as f:
            json.dump(amj, f, indent=1)
        man["outputs"]["astc_manifest"] = zp.file_record(jp)
        man["outputs"]["astc_bin"] = zp.file_record(bp)
    if a.verify:
        ok = all(read_bytes(r["path"]) == _mip_payload(pd, pd.mips[int(k[3:])], t, bulk)[0]
                 for k, r in man["outputs"].items() if k.startswith("mip"))
        man["verify"] = {"payloads_match_source": ok}
        if not ok:
            raise SystemExit("verify failed")
    zp.write_manifest(out, man)
    print(json.dumps({"out_dir": out, "mips": len(man["outputs"]), "missing": man.get("missing", [])}))


def cmd_texture_replace(a):
    t = load_target(a.input, a.zen_export, a.raw)
    unsupported = []
    pds = scan_texture_platform_data(t, unsupported)
    pd = select_platform_data(pds, a.index, unsupported)
    man_json, man_mips, bin_path = load_astc_manifest(a.astc_manifest, a.astc_bin)
    plan = plan_texture_replace(pd, man_json, man_mips, a.strict_dims, a.allow_extra_mips)
    bulk = _load_bulk_files(a)
    inputs = {"package": a.input, "astc_manifest": a.astc_manifest, "astc_bin": bin_path,
              **{k: v for k, v in _bulk_inputs(a).items() if v}}
    patches = {"package": Patch("package", a.input, t.data)}
    for loc, path in _bulk_inputs(a).items():
        if path:
            patches[loc] = Patch(loc, path, bulk["_data"][loc])
    for mip, mm, rule in plan:
        old, where = _mip_payload(pd, mip, t, bulk)
        if old is None:
            raise SystemExit(f"refusing: mip {mip.index} lives in {where[0]} but no --{_bulk_flag_for(where[0])} file was given")
        if len(old) != len(mm["payload"]):
            raise SystemExit("refusing: size mismatch (internal)")
        patches[where[0]].write(where[1], mm["payload"], f"mip {mip.index} payload {mip.size_x}x{mip.size_y} ({rule})")
    out = zp.prepare_out_dir(a.out_dir, list(inputs.values()))
    man = base_manifest("texture-replace", inputs, {"index": a.index, "zen_export": a.zen_export,
                                                     "strict_dims": a.strict_dims})
    man["platform_data_before"] = pd.to_json()
    used = [p for p in patches.values() if p.changes or p.label == "package"]
    emit_outputs(out, used, man, list(inputs.values()))
    if a.verify:
        man["verify"] = verify_texture_output(man, pd, plan, a, out)
        if not man["verify"]["ok"]:
            zp.write_manifest(out, man)
            raise SystemExit("verify failed: " + json.dumps(man["verify"]))
    zp.write_manifest(out, man)
    print(json.dumps({"out_dir": out, "mips_written": len(plan),
                      "verify": man.get("verify", {}).get("ok")}))


def _bulk_flag_for(loc):
    return {"BulkData": "bulk", "OptionalBulkData": "optional-bulk", "MemoryMappedBulkData": "mmap-bulk"}.get(loc, loc)


def verify_texture_output(man, pd_before, plan, a, out):
    """Re-scan the written package with the reader and check payloads, headers and untouched bytes."""
    res = {"ok": True, "checks": []}

    def check(name, ok, detail=None):
        res["checks"].append({"check": name, "ok": bool(ok), **({"detail": detail} if detail else {})})
        res["ok"] &= bool(ok)
    pkg_out = man["outputs"]["package"]["path"]
    t2 = load_target(pkg_out, a.zen_export, a.raw)
    pds2 = scan_texture_platform_data(t2)
    check("platform data still found", len(pds2) > 0)
    if not pds2:
        return res
    pd2 = min(pds2, key=lambda p: abs(p.start - pd_before.start))
    check("same structure (offsets, dims, bulk headers)",
          [(m.entry_start, m.entry_end, m.size_x, m.size_y, m.bulk.flags, m.bulk.size_on_disk) for m in pd2.mips] ==
          [(m.entry_start, m.entry_end, m.size_x, m.size_y, m.bulk.flags, m.bulk.size_on_disk) for m in pd_before.mips])
    outs = {"package": pkg_out}
    for loc in ("BulkData", "OptionalBulkData", "MemoryMappedBulkData"):
        if loc in man["outputs"]:
            outs[loc] = man["outputs"][loc]["path"]
    bulk2 = {"BulkData": outs.get("BulkData"), "OptionalBulkData": outs.get("OptionalBulkData"),
             "MemoryMappedBulkData": outs.get("MemoryMappedBulkData")}
    bulk2["_data"] = {k: read_bytes(v) for k, v in bulk2.items() if v and k != "_data"}
    for mip, mm, _ in plan:
        m2 = pd2.mips[mip.index]
        payload, _ = _mip_payload(pd2, m2, t2, bulk2)
        check(f"mip {mip.index} payload == new ASTC", payload == mm["payload"])
    for label, path in outs.items():
        orig = read_bytes(man["inputs"]["package" if label == "package" else label]["path"])
        new = read_bytes(path)
        ch = [c for c in man["changes"] if c["file"] == label]
        check(f"{label}: size unchanged and bytes outside patched mips untouched", check_untouched(orig, new, ch))
    if getattr(a, "decode_check", False):
        import astc_tool
        bx, by = man_block(pd2)
        for mip, mm, _ in plan:
            try:
                astc_tool.decode_raw(mm["payload"], mm["w"], mm["h"], bx, by)
                check(f"mip {mip.index} decodes with astcenc", True)
            except Exception as e:  # astcenc failure is a verification failure
                check(f"mip {mip.index} decodes with astcenc", False, str(e))
    return res


def man_block(pd):
    blk = pixel_format_block(pd.pixel_format)
    return blk[0], blk[1]


# ============================================================================================
# Commands: mesh
# ============================================================================================
def _parse_floats(s, n=None):
    if s is None:
        return None
    v = [float(x) for x in s.split(",")]
    if n is not None and len(v) != n:
        raise SystemExit(f"expected {n} comma separated numbers, got {len(v)}")
    return v


def cmd_mesh_locate(a):
    t = load_target(a.input, a.zen_export, a.raw)
    ref = load_ref_mesh(a.ref, a.ref_mesh)
    bounds_data = read_bytes(a.bounds_file) if a.bounds_file else None
    report, bounds = locate_all(t.data, ref, t.start, t.end, tuple(int(x) for x in a.strides.split(",")),
                                tuple(_parse_floats(a.scales)), a.tol_rel, not a.no_tangents, not a.no_uvs,
                                not a.no_bounds, _parse_floats(a.old_bounds, 7), bounds_data)
    doc = {"target": t.describe(), **report_json(report, bounds, ref)}
    _emit_report(doc, a.json)
    if not a.json:
        print(f"reference {ref.source}: {ref.count} vertices; {len(report)} position stream(s)")
        for i, e in enumerate(report):
            pm = e["positions"]
            print(f"[{i}] {'PRIMARY' if pm.primary else 'partial'} positions @{pm.offset} stride {pm.stride} count {pm.count} mode {pm.mode} "
                  f"unmatched {pm.unmatched} ambiguous {e['ambiguous_after_refine']} header {bool(pm.header)}")
            print(f"     convention: {describe_transform(pm.A, pm.t)}")
            for tm in e["tangents"][:2]:
                print(f"     tangents @{tm.offset} stride {tm.stride} {tm.encoding} match {tm.score:.3f} header {bool(tm.header)}")
            for um in e["uvs"][:4]:
                print(f"     uv(ref ch {um.ref_channel}) @{um.offset} stride {um.stride} {um.precision} "
                      f"ue ch {um.channel} flipV {um.flip_v} header {bool(um.header)}")
        for b in bounds[:4]:
            print(f"bounds candidate @{b.offset} origin {np.round(b.origin, 3).tolist()} extent "
                  f"{np.round(b.extent, 3).tolist()} r {b.radius:.3f} score {b.score:.4f}")
        print("NOTE: LOD0 and LOD1 are separate streams; patch each with its own reference/new pair.")


def _faces_equal(a, b):
    if a is None or b is None:
        return None
    return a.shape == b.shape and bool(np.array_equal(a, b))


def _choose_stream(report, offset):
    """The stream to patch: the one at ``offset`` or the single primary (complete) match."""
    if offset is not None:
        for e in report:
            if e["positions"].offset == offset:
                return e
        raise SystemExit(f"no position stream at offset {offset}; candidates: {[e['positions'].offset for e in report]}")
    primary = [e for e in report if e["positions"].primary]
    with_header = [e for e in primary if e["positions"].header]
    if len(with_header) == 1:
        return with_header[0]
    if len(primary) != 1:
        raise SystemExit(f"refusing: {len(primary)} complete position streams match the reference "
                         f"(all candidates: {[(e['positions'].offset, e['positions'].count) for e in report]}); "
                         "choose one with --stream-offset")
    return primary[0]


def plan_position_patch(data, ref, new, entry, allow_ambiguous=False, allow_partial=False, allow_symmetric_tie=False):
    """New UE positions for the located stream (float32 bytes) + per-vertex change mask."""
    pm = entry["positions"]
    if pm.symmetry_tie and not allow_symmetric_tie:
        raise SystemExit("refusing: the mesh is symmetric and a mirrored axis mapping fits as well as the chosen one "
                         "(UVs/tangents/order do not disambiguate); an asymmetric edit could be applied mirrored. "
                         "Export the reference as PSK (buffer order) or pass --allow-symmetric-tie after checking "
                         "'alternative_transforms' in mesh-locate --json")
    if new.count != ref.count:
        raise SystemExit(f"refusing: new mesh has {new.count} vertices, reference has {ref.count} "
                         "(vertex count and order must be identical)")
    if pm.unmatched and not allow_partial:
        raise SystemExit(f"refusing: {pm.unmatched} stream vertices have no reference vertex")
    old_ref_same = np.all(new.positions == ref.positions, axis=1)
    qi, ci = pm.pairs
    if entry["ambiguous_after_refine"] and not allow_ambiguous:
        amb = np.flatnonzero(pm.nmatch > 1)
        sel = np.isin(qi, amb)
        q, c = qi[sel], ci[sel]
        diverge = np.max(np.abs(new.positions[c] - new.positions[pm.mapping[q]]), axis=1) > 1e-7 if len(q) else np.zeros(0, bool)
        if np.any(diverge):
            raise SystemExit(f"refusing: {len(np.unique(q[diverge]))} stream vertices are ambiguous (several reference "
                             "vertices share the old position but move differently); use --allow-ambiguous to take "
                             "the best UV/normal match")
    old_ue = pm.positions_ue(data)
    m = pm.mapping
    valid = m >= 0
    new_ue = old_ue.copy()
    new_ue[valid] = new.positions[m[valid]] @ pm.A + pm.t
    changed = valid.copy()
    changed[valid] = ~old_ref_same[m[valid]]
    out = np.frombuffer(data, np.uint8, pm.stride * pm.count, pm.offset).copy().reshape(pm.count, pm.stride)
    new_bytes = new_ue.astype("<f4").view(np.uint8).reshape(pm.count, 12)
    out[changed, 0:12] = new_bytes[changed]
    return out.tobytes(), old_ue, np.where(changed[:, None], new_ue.astype(np.float32).astype(np.float64), old_ue), changed


def plan_tangent_patch(data, ref, new, entry, tm: TangentMatch):
    """Delta method: n' = normalize(n_stored + R(n_new_ref) - R(n_old_ref)); TangentX is
    re-orthogonalised against n'; the W components (binormal sign) are kept."""
    pm = entry["positions"]
    if ref.faces is None or new.faces is None:
        raise SystemExit("refusing: --tangents needs faces in both reference and new mesh")
    groups = weld_groups_by_position(ref.positions)
    n_old = vertex_normals(ref.positions, ref.faces, groups)
    n_new = vertex_normals(new.positions, new.faces, groups)
    Nm = normal_matrix(pm.A)
    n_old_ue = n_old @ Nm
    n_new_ue = n_new @ Nm
    n_old_ue /= np.maximum(np.linalg.norm(n_old_ue, axis=1, keepdims=True), 1e-20)
    n_new_ue /= np.maximum(np.linalg.norm(n_new_ue, axis=1, keepdims=True), 1e-20)
    base = tm.offset + np.arange(pm.count) * tm.stride
    tx = _decode_packed(data, base, tm.encoding)
    tz = _decode_packed(data, base + tm.normal_field, tm.encoding)
    m = pm.mapping
    valid = m >= 0
    stored_n = tz[:, :3] / np.maximum(np.linalg.norm(tz[:, :3], axis=1, keepdims=True), 1e-12)
    sign = 1.0
    if np.mean(np.sum(stored_n[valid] * n_old_ue[m[valid]], axis=1)) < 0:
        sign = -1.0  # face winding vs. mirrored export: computed normals point inwards
    delta = np.zeros((pm.count, 3))
    delta[valid] = sign * (n_new_ue[m[valid]] - n_old_ue[m[valid]])
    changed = np.linalg.norm(delta, axis=1) > 1e-6
    n2 = stored_n + delta
    n2 /= np.maximum(np.linalg.norm(n2, axis=1, keepdims=True), 1e-12)
    t = tx[:, :3] - np.sum(tx[:, :3] * n2, axis=1, keepdims=True) * n2
    tn = np.linalg.norm(t, axis=1, keepdims=True)
    t = np.where(tn > 1e-6, t / np.maximum(tn, 1e-12), tx[:, :3])
    raw = np.frombuffer(data, np.uint8, tm.stride * pm.count, tm.offset).copy().reshape(pm.count, tm.stride)
    half = tm.stride // 2
    for j in np.flatnonzero(changed):
        raw[j, 0:half] = np.frombuffer(_encode_packed(np.append(t[j], tx[j, 3]), tm.encoding), np.uint8)
        raw[j, tm.normal_field:tm.normal_field + half] = np.frombuffer(_encode_packed(np.append(n2[j], tz[j, 3]), tm.encoding), np.uint8)
    return raw.tobytes(), changed, sign


def cmd_mesh_patch_positions(a):
    t = load_target(a.input, a.zen_export, a.raw)
    strides = tuple(int(x) for x in a.strides.split(","))
    scales = tuple(_parse_floats(a.scales))
    inputs = {"input": a.input}
    for i, (r, n) in enumerate(a.pair):
        inputs[f"ref{i}"] = r
        inputs[f"new{i}"] = n
    if a.bounds_file:
        inputs["bounds_file"] = a.bounds_file
    patch = Patch("input", a.input, t.data)
    same_file = bool(a.bounds_file) and os.path.realpath(a.bounds_file) == os.path.realpath(a.input)
    # --bounds-file naming the input itself must patch ONE output; two Patch objects on the same
    # bytes would split positions and bounds over two incomplete output files
    bounds_patch = Patch("bounds_file", a.bounds_file, read_bytes(a.bounds_file)) \
        if a.bounds_file and not same_file else patch
    if same_file:
        inputs.pop("bounds_file", None)
    offsets = list(a.stream_offset or [])
    plans = []
    all_old, all_new = [], []
    for i, (ref_path, new_path) in enumerate(a.pair):
        ref = load_ref_mesh(ref_path, a.ref_mesh)
        new = load_ref_mesh(new_path, a.ref_mesh)
        topo = _faces_equal(ref.faces, new.faces)
        if topo is False and not a.skip_topology_check:
            raise SystemExit(f"refusing: pair {i}: face lists differ (vertex order/topology changed)")
        report, _ = locate_all(t.data, ref, t.start, t.end, strides, scales, a.tol_rel,
                               want_tangents=a.tangents, want_uvs=True, want_bounds=False)
        if not report:
            raise SystemExit(f"refusing: pair {i}: reference {ref_path} not found in {a.input}")
        entry = _choose_stream(report, offsets[i] if i < len(offsets) else None)
        pm = entry["positions"]
        pos_bytes, old_ue, new_ue, changed = plan_position_patch(t.data, ref, new, entry, a.allow_ambiguous,
                                                                 a.allow_partial, a.allow_symmetric_tie)
        patch.write(pm.offset, pos_bytes, f"pair {i} positions ({pm.count} verts, {int(changed.sum())} moved)")
        info = {"pair": i, "reference": ref.source, "new": new.source, "topology_identical": topo,
                "positions": pm.to_json(), "moved_vertices": int(changed.sum()),
                "other_matches_not_patched": [{"offset": e["positions"].offset, "count": e["positions"].count,
                                               "complete": e["positions"].primary,
                                               "vertex_buffer_header": e["positions"].header is not None}
                                              for e in report if e is not entry]}
        if a.tangents:
            if not entry["tangents"]:
                raise SystemExit(f"refusing: pair {i}: --tangents given but no tangent stream was located")
            tm = entry["tangents"][0]
            tan_bytes, tchanged, sign = plan_tangent_patch(t.data, ref, new, entry, tm)
            patch.write(tm.offset, tan_bytes, f"pair {i} tangents ({int(tchanged.sum())} updated)")
            info["tangents"] = {**tm.to_json(), "updated": int(tchanged.sum()), "computed_normal_sign": sign}
        plans.append({"entry": entry, "ref": ref, "new": new, "info": info, "new_ue": new_ue})
        all_old.append(old_ue)
        all_new.append(new_ue)
    bounds_info = None
    if a.bounds != "none":
        allold = np.concatenate(all_old)
        b_lo, b_hi = (t.start, t.end) if bounds_patch is patch and not same_file else (0, len(bounds_patch.orig))
        cands = locate_bounds(bounds_patch.orig, allold, b_lo, b_hi, old_bounds=_parse_floats(a.old_bounds, 7))
        if not cands:
            raise SystemExit("refusing: --bounds requested but no FBoxSphereBounds candidate was found "
                             "(pass --old-bounds with the ImportedBounds values from FModel)")
        chosen = [c for c in cands if c.score <= cands[0].score + 1e-9] if not a.bounds_all else cands
        bounds_info = []
        for c in chosen:
            o, e, r = new_bounds(c, allold, np.concatenate(all_new), a.bounds)
            bounds_patch.write(c.offset, struct.pack("<7f", *o, *e, r), f"FBoxSphereBounds @{c.offset} ({a.bounds})")
            bounds_info.append({"before": c.to_json(), "after": {"origin": o, "box_extent": e, "sphere_radius": r}})
    out = zp.prepare_out_dir(a.out_dir, list(inputs.values()))
    man = base_manifest("mesh-patch-positions", inputs, {
        "strides": strides, "scales": scales, "tangents": a.tangents, "bounds": a.bounds,
        "stream_offset": offsets, "zen_export": a.zen_export})
    man["pairs"] = [p["info"] for p in plans]
    man["bounds"] = bounds_info
    man["lod_note"] = ("LOD0 and LOD1 are separate vertex streams; give one --pair per LOD. LODs not listed "
                       "here were NOT modified.")
    patches = [patch] + ([bounds_patch] if bounds_patch is not patch else [])
    emit_outputs(out, patches, man, list(inputs.values()))
    if a.verify:
        man["verify"] = verify_mesh_output(man, plans, a)
        if not man["verify"]["ok"]:
            zp.write_manifest(out, man)
            raise SystemExit("verify failed: " + json.dumps(man["verify"]))
    zp.write_manifest(out, man)
    print(json.dumps({"out_dir": out, "pairs": len(plans), "changes": len(man["changes"]),
                      "verify": man.get("verify", {}).get("ok")}))


def verify_mesh_output(man, plans, a):
    """Re-locate every patched stream in the output using the NEW mesh as reference."""
    res = {"ok": True, "checks": []}

    def check(name, ok, detail=None):
        res["checks"].append({"check": name, "ok": bool(ok), **({"detail": detail} if detail is not None else {})})
        res["ok"] &= bool(ok)
    out_path = man["outputs"]["input"]["path"]
    t2 = load_target(out_path, a.zen_export, a.raw)
    orig = read_bytes(man["inputs"]["input"]["path"])
    check("input: size unchanged, bytes outside patched ranges untouched",
          check_untouched(orig, t2.data, [c for c in man["changes"] if c["file"] == "input"]))
    for p in plans:
        pm = p["entry"]["positions"]
        found = locate_positions(t2.data, p["new"], t2.start, t2.end, (pm.stride,),
                                 (float(np.cbrt(abs(np.linalg.det(pm.A)))) or 1.0,), tol_rel=a.tol_rel)
        hit = [f for f in found if f.offset == pm.offset and f.count == pm.count]
        check(f"pair {p['info']['pair']}: new mesh re-located at the patched offset", bool(hit),
              [f.offset for f in found])
        got = read_positions(t2.data, pm.offset, pm.stride, pm.count)
        check(f"pair {p['info']['pair']}: stored positions == planned float32 values",
              np.array_equal(got.astype(np.float32), p["new_ue"].astype(np.float32)))
        if a.tangents and "tangents" in p["info"]:
            ti = p["info"]["tangents"]
            base = ti["offset"] + np.arange(pm.count) * ti["stride"]
            nz = _decode_packed(t2.data, base + ti["tangent_z_field"], ti["encoding"])[:, :3]
            ln = np.linalg.norm(nz, axis=1)
            check(f"pair {p['info']['pair']}: tangent normals unit length", bool(np.all(np.abs(ln - 1) < 0.03)))
    if "bounds_file" in man["outputs"]:
        check("bounds_file: size unchanged, bytes outside patched ranges untouched",
              check_untouched(read_bytes(man["inputs"]["bounds_file"]["path"]),
                              read_bytes(man["outputs"]["bounds_file"]["path"]),
                              [c for c in man["changes"] if c["file"] == "bounds_file"]))
    if man.get("bounds"):
        bd = read_bytes(man["outputs"]["bounds_file"]["path"]) if "bounds_file" in man["outputs"] else t2.data
        allnew = np.concatenate([p["new_ue"] for p in plans])
        for b in man["bounds"]:
            off = b["before"]["offset"]
            o = np.array(struct.unpack_from("<3f", bd, off))
            e = np.array(struct.unpack_from("<3f", bd, off + 12))
            check(f"bounds @{off} contain every patched vertex",
                  bool(np.all(o - e <= allnew.min(0) + 1e-3) and np.all(o + e >= allnew.max(0) - 1e-3)))
    return res


# ============================================================================================
# CLI
# ============================================================================================
def _add_target_args(p):
    p.add_argument("input", help="package chunk (.uasset/.uzenasset from IoStore), raw export blob or bulk chunk")
    p.add_argument("--zen-export", help="restrict to this export (index or name) of a Zen package")
    p.add_argument("--raw", action="store_true", help="do not try to parse the input as a Zen package")


def _add_bulk_args(p):
    p.add_argument("--bulk", help="BulkData chunk (.ubulk) holding PayloadInSeperateFile mips")
    p.add_argument("--optional-bulk", help="OptionalBulkData chunk (.uptnl)")
    p.add_argument("--mmap-bulk", help="MemoryMappedBulkData chunk (.m.ubulk)")
    p.add_argument("--index", type=int, help="which platform data block when several are found")


def _add_mesh_args(p):
    p.add_argument("--ref-mesh", default="0", help="glTF mesh index or 'all' (default 0)")
    p.add_argument("--strides", default="12", help="position strides to try, bytes (default 12 = UE4.19+ SOA)")
    p.add_argument("--scales", default="100,1,0.01",
                   help="reference->UE scale factors to try, in order (100 = metres, e.g. FModel glTF)")
    p.add_argument("--tol-rel", type=float, default=1e-4, help="position tolerance relative to the mesh diagonal")
    p.add_argument("--old-bounds", help="ox,oy,oz,ex,ey,ez,r of the FBoxSphereBounds to find (UE cm)")
    p.add_argument("--bounds-file", help="search/patch bounds in this file instead of the input")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] + "\n\n" + __doc__.split("\n\n")[2],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)

    p = sp.add_parser("texture-scan", help="locate FTexturePlatformData and report mips")
    _add_target_args(p)
    p.add_argument("--json", help="write the report to this new file ('-' = stdout)")
    p.set_defaults(fn=cmd_texture_scan)

    p = sp.add_parser("texture-extract", help="copy mip payloads to a new directory")
    _add_target_args(p)
    _add_bulk_args(p)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--decode", action="store_true", help="also decode ASTC mips to PNG with astcenc")
    p.add_argument("--linear", action="store_true", help="decode as linear (not sRGB)")
    p.add_argument("--verify", action="store_true")
    p.set_defaults(fn=cmd_texture_extract)

    p = sp.add_parser("texture-replace", help="overwrite ASTC mip payloads with same-size new ones")
    _add_target_args(p)
    _add_bulk_args(p)
    p.add_argument("--astc-manifest", required=True, help="JSON written by astc_tool.py encode --mips")
    p.add_argument("--astc-bin", help="matching .bin (default: next to the manifest)")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--strict-dims", action="store_true", help="require exact mip dims (no block padding rule)")
    p.add_argument("--allow-extra-mips", action="store_true", help="manifest may contain mips the texture lacks")
    p.add_argument("--decode-check", action="store_true", help="with --verify: decode every new mip with astcenc")
    p.add_argument("--verify", action="store_true")
    p.set_defaults(fn=cmd_texture_replace)

    p = sp.add_parser("mesh-locate", help="find position/tangent/UV streams and bounds from a reference mesh")
    _add_target_args(p)
    p.add_argument("--ref", required=True, help="reference mesh (.glb/.gltf/.obj/.psk) exported from the SAME LOD")
    _add_mesh_args(p)
    p.add_argument("--no-tangents", action="store_true")
    p.add_argument("--no-uvs", action="store_true")
    p.add_argument("--no-bounds", action="store_true")
    p.add_argument("--json", help="write the report to this new file ('-' = stdout)")
    p.set_defaults(fn=cmd_mesh_locate)

    p = sp.add_parser("mesh-patch-positions", help="overwrite positions (+tangents, bounds) in place")
    _add_target_args(p)
    p.add_argument("--pair", nargs=2, action="append", required=True, metavar=("REF", "NEW"),
                   help="reference export of one LOD and the edited mesh with identical vertex order; repeat per LOD")
    _add_mesh_args(p)
    p.add_argument("--stream-offset", type=int, action="append", help="position stream offset per pair when several match")
    p.add_argument("--tangents", action="store_true", help="also update packed TangentX/TangentZ (delta method)")
    p.add_argument("--bounds", choices=["none", "union", "recompute"], default="none",
                   help="update FBoxSphereBounds located by matching the old bounds (default none)")
    p.add_argument("--bounds-all", action="store_true", help="update every bounds candidate, not only the best")
    p.add_argument("--allow-ambiguous", action="store_true")
    p.add_argument("--allow-symmetric-tie", action="store_true",
                   help="accept the chosen axis mapping even if a mirrored one fits equally well")
    p.add_argument("--allow-partial", action="store_true", help="allow stream vertices with no reference vertex")
    p.add_argument("--skip-topology-check", action="store_true")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--verify", action="store_true")
    p.set_defaults(fn=cmd_mesh_patch_positions)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
