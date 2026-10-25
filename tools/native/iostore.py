#!/usr/bin/env python3
"""Unreal Engine IoStore container (.utoc + .ucas[, _sN.ucas]) reader and patch writer.

Subcommands (all JSON-capable):
  info       header, flags, compression methods, partition/dir-index/meta stats, round-trip check
  list       one line per TOC entry: index, chunk id, type, size, blocks, method, path
  extract    decrypt + decompress chunks (by path glob / chunk id / index) into a new directory
  hash-all   SHA-256 of every decompressed chunk -> JSON (before/after diffs)
  patch      replace chunk payloads -> NEW .utoc/.ucas pair (append mode, or --rebuild)
  diff       compare two containers (or two hash-all JSONs) by chunk id
  selfcheck  byte-exact TOC re-serialisation, perfect-hash lookup, layout, meta hashes, signatures
  build-ooz  build the open-source "ooz" Oodle decoder as a shared library (for Oodle containers)

Format sources
--------------
Every layout below was transcribed from CUE4Parse (https://github.com/FabianFG/CUE4Parse, checkout
at /opt/tools/ref/CUE4Parse, commit 77b94fa), paths relative to CUE4Parse/CUE4Parse/:
  UE4/IO/Objects/FIoStoreTocHeader.cs            FIoStoreTocHeader (144 bytes), EIoStoreTocVersion,
                                                 EIoContainerFlags
  UE4/IO/Objects/FIoStoreTocResource.cs          order of the .utoc sections, version gates,
                                                 signature block, directory index, TOC meta
  UE4/IO/Objects/FIoChunkId.cs                   FIoChunkId (12 bytes), EIoChunkType / EIoChunkType5,
                                                 HashWithSeed (FNV-1 64 used by the perfect hash)
  UE4/IO/Objects/FIoOffsetAndLength.cs           5+5 byte big-endian offset/length
  UE4/IO/Objects/FIoStoreTocCompressedBlockEntry.cs  12-byte block entry (40/24/24/8 bits)
  UE4/IO/Objects/FIoStoreTocEntryMeta.cs         FIoChunkHash(32) + flags, or FIoHash(20)+flags+3 pad
  UE4/IO/Objects/FIoDirectoryIndexEntry.cs, FIoFileIndexEntry.cs   directory index records
  UE4/IO/FIoStoreEncryptionIV.cs                 EIoEncryptionMethod, 12-byte AES-CTR IVs
  UE4/IO/IoStoreReader.cs                        partition naming (_sN.ucas), block read/decrypt/
                                                 decompress, TryResolve (perfect hash), ReadIndex
  UE4/VirtualFileSystem/AbstractVfsReader.cs     IsValidIndex (AES key test on the mount point)
  Encryption/Aes/Aes.cs                          AES-256-ECB, Unreal AES-CTR counter layout
  Compression/CompressionMethod.cs               compression method names
Hash of TOC meta entries: retoc (https://github.com/trumank/retoc, retoc/src/iostore_writer.rs and
lib.rs FIoChunkHash::from_blake3) writes BLAKE3 truncated to 20 bytes + 12 zero bytes. Older UE4
writers are reported to use SHA-1 for FIoChunkHash; which one a given game used cannot be known
from the format alone, so `patch --hash-algo auto` *measures* it on unmodified chunks.

Keys: AES keys come only from --aes-key-file or the R9_AES_KEY_HEX environment variable and are never
printed, logged or written to manifests. Before new blocks are encrypted the key must be proven:
by the directory-index mount point (AbstractVfsReader.IsValidIndex) or, for containers without a
directory index, by decoding unchanged chunks (IoStoreContainer.verify_key_by_chunks).

What `patch --verify` can and cannot prove: it re-reads the output with THIS module's reader, so it
finds writer/reader inconsistencies, damage to unchanged data, changed inputs, overlapping or
out-of-file 16-byte block spans, etc.; it cannot find a misunderstanding of the format shared by
reader and writer. The independent check is CUE4Parse (tools/native/cue4parse_check, used by
tools/tests/test_iostore.py) and, ultimately, the game.
"""

from __future__ import annotations

import argparse
import ctypes
import fnmatch
import hashlib
import importlib.util
import json
import os
import shutil
import struct
import subprocess
import sys
import time
import zlib
from dataclasses import dataclass
from enum import IntEnum, IntFlag
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

TOOL_NAME = "iostore.py"
TOOL_VERSION = "1.0.0"

TOC_MAGIC = b"-==--==--==--==-"
TOC_HEADER_SIZE = 144
COMPRESSED_BLOCK_ENTRY_SIZE = 12
AES_BLOCK = 16
INVALID_HANDLE = 0xFFFFFFFF
MASK32 = 0xFFFFFFFF
MASK64 = (1 << 64) - 1
MAX_U40 = (1 << 40) - 1
MAX_U24 = (1 << 24) - 1
FNV64_OFFSET = 0xCBF29CE484222325
FNV64_PRIME = 0x00000100000001B3
# iOS/arm64 page size; UE aligns memory-mapped (MemoryMappedBulkData) chunks to it.
MEMORY_MAPPED_ALIGN = 16384
KEY_ENV = "R9_AES_KEY_HEX"
OODLE_ENV = "R9_OODLE_LIB"


# ---------------------------------------------------------------------------------------------
# Enums (FIoStoreTocHeader.cs, FIoStoreTocEntryMeta.cs, FIoStoreEncryptionIV.cs, FIoChunkId.cs)
# ---------------------------------------------------------------------------------------------

class TocVersion(IntEnum):
    """EIoStoreTocVersion (FIoStoreTocHeader.cs)."""
    Invalid = 0
    Initial = 1
    DirectoryIndex = 2
    PartitionSize = 3
    PerfectHash = 4
    PerfectHashWithOverflow = 5
    OnDemandMetaData = 6
    RemovedOnDemandMetaData = 7
    ReplaceIoChunkHashWithIoHash = 8
    AddedSourceHashes = 9
    ContainerEncryptionMethod = 10


LATEST_KNOWN_VERSION = max(TocVersion)


class ContainerFlags(IntFlag):
    """EIoContainerFlags (FIoStoreTocHeader.cs)."""
    NONE = 0
    Compressed = 1 << 0
    Encrypted = 1 << 1
    Signed = 1 << 2
    Indexed = 1 << 3
    OnDemand = 1 << 4


class EncryptionMethod(IntEnum):
    """EIoEncryptionMethod (FIoStoreEncryptionIV.cs). Before TOC version 10 it is implied by the
    Encrypted flag (AES = ECB)."""
    NONE = 0
    AES = 1
    AES_CTR = 2


class MetaFlags(IntFlag):
    """FIoStoreTocEntryMetaFlags (FIoStoreTocEntryMeta.cs)."""
    NONE = 0
    Compressed = 1 << 0
    MemoryMapped = 1 << 1


UE4_CHUNK_TYPES = ["Invalid", "InstallManifest", "ExportBundleData", "BulkData", "OptionalBulkData",
                   "MemoryMappedBulkData", "LoaderGlobalMeta", "LoaderInitialLoadMeta",
                   "LoaderGlobalNames", "LoaderGlobalNameHashes", "ContainerHeader"]
UE5_CHUNK_TYPES = ["Invalid", "ExportBundleData", "BulkData", "OptionalBulkData", "MemoryMappedBulkData",
                   "ScriptObjects", "ContainerHeader", "ExternalFile", "ShaderCodeLibrary", "ShaderCode",
                   "PackageStoreEntry", "DerivedData", "EditorDerivedData", "PackageResource"]


class IoStoreError(Exception):
    """Any format / consistency / usage error."""


class KeyRequired(IoStoreError):
    """The container is encrypted and no (valid) AES key was supplied."""


class UnsupportedCompression(IoStoreError):
    """A block uses a compression method this process cannot decode."""


def align_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for piece in iter(lambda: f.read(8 << 20), b""):
            h.update(piece)
    return h.hexdigest()


# ---------------------------------------------------------------------------------------------
# Binary reader helper
# ---------------------------------------------------------------------------------------------

class _Cursor:
    def __init__(self, data: bytes, pos: int = 0, what: str = "utoc"):
        self.data = data
        self.pos = pos
        self.what = what

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def take(self, n: int, label: str) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise IoStoreError(f"{self.what}: truncated while reading {label} "
                               f"(need {n} bytes at {self.pos}, have {self.remaining()})")
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def i32(self, label: str) -> int:
        return struct.unpack("<i", self.take(4, label))[0]

    def u32(self, label: str) -> int:
        return struct.unpack("<I", self.take(4, label))[0]


# ---------------------------------------------------------------------------------------------
# FIoStoreTocHeader (144 bytes)
# ---------------------------------------------------------------------------------------------

_HEADER = struct.Struct("<16sBBHIIIIIIIIIQ16sBBHIQII40s")
assert _HEADER.size == TOC_HEADER_SIZE


@dataclass
class TocHeader:
    """FIoStoreTocHeader (FIoStoreTocHeader.cs). Field order/offsets:
    0 TocMagic[16] | 16 Version u8 | 17 _reserved0 u8 | 18 _reserved1 u16 | 20 TocHeaderSize u32 |
    24 TocEntryCount | 28 TocCompressedBlockEntryCount | 32 TocCompressedBlockEntrySize |
    36 CompressionMethodNameCount | 40 CompressionMethodNameLength | 44 CompressionBlockSize |
    48 DirectoryIndexSize | 52 PartitionCount | 56 ContainerId u64 | 64 EncryptionKeyGuid[16] |
    80 ContainerFlags u8 | 81 _reserved3 u8 (EncryptionMethod from v10) | 82 _reserved4 u16 |
    84 TocChunkPerfectHashSeedsCount u32 | 88 PartitionSize u64 | 96 TocChunksWithoutPerfectHashCount |
    100 _reserved7 u32 (TocSourceHashCount from v9) | 104 _reserved8 u64[5] (EncryptionIVCount u32 at
    104 from v10). All raw values are kept so serialize() is byte exact."""
    magic: bytes
    version: int
    reserved0: int
    reserved1: int
    toc_header_size: int
    entry_count: int
    compressed_block_entry_count: int
    compressed_block_entry_size: int
    compression_method_name_count: int
    compression_method_name_length: int
    compression_block_size: int
    directory_index_size: int
    partition_count: int
    container_id: int
    encryption_key_guid: bytes
    container_flags: int
    reserved3: int
    reserved4: int
    perfect_hash_seeds_count: int
    partition_size: int
    chunks_without_perfect_hash_count: int
    reserved7: int
    reserved8: bytes

    @classmethod
    def parse(cls, data: bytes) -> "TocHeader":
        if len(data) < TOC_HEADER_SIZE:
            raise IoStoreError(f"utoc too small for a header ({len(data)} bytes)")
        h = cls(*_HEADER.unpack_from(data, 0))
        if h.magic != TOC_MAGIC:
            raise IoStoreError(f"bad utoc magic {h.magic!r} (expected {TOC_MAGIC!r})")
        if h.toc_header_size != TOC_HEADER_SIZE:
            raise IoStoreError(f"TocHeaderSize={h.toc_header_size}, expected {TOC_HEADER_SIZE}")
        if h.compressed_block_entry_size != COMPRESSED_BLOCK_ENTRY_SIZE:
            raise IoStoreError(f"TocCompressedBlockEntrySize={h.compressed_block_entry_size}, expected 12")
        if h.compression_block_size == 0 and h.entry_count:
            raise IoStoreError("CompressionBlockSize is 0; chunk offsets cannot be mapped to blocks")
        return h

    def serialize(self) -> bytes:
        return _HEADER.pack(self.magic, self.version, self.reserved0, self.reserved1, self.toc_header_size,
                            self.entry_count, self.compressed_block_entry_count,
                            self.compressed_block_entry_size, self.compression_method_name_count,
                            self.compression_method_name_length, self.compression_block_size,
                            self.directory_index_size, self.partition_count, self.container_id,
                            self.encryption_key_guid, self.container_flags, self.reserved3, self.reserved4,
                            self.perfect_hash_seeds_count, self.partition_size,
                            self.chunks_without_perfect_hash_count, self.reserved7, self.reserved8)

    # --- version-dependent interpretation (FIoStoreTocResource.cs constructor) ---
    @property
    def version_name(self) -> str:
        try:
            return TocVersion(self.version).name
        except ValueError:
            return f"Unknown({self.version})"

    @property
    def flags(self) -> ContainerFlags:
        return ContainerFlags(self.container_flags)

    @property
    def effective_partition_count(self) -> int:
        if self.version < TocVersion.PartitionSize:
            return 1
        return max(1, self.partition_count)

    @property
    def effective_partition_size(self) -> int:
        if self.version < TocVersion.PartitionSize or self.partition_size == 0:
            return MASK64
        return self.partition_size

    @property
    def encryption_method(self) -> EncryptionMethod:
        if self.version < TocVersion.ContainerEncryptionMethod:
            return EncryptionMethod.AES if self.flags & ContainerFlags.Encrypted else EncryptionMethod.NONE
        try:
            return EncryptionMethod(self.reserved3)
        except ValueError:
            raise IoStoreError(f"unknown EIoEncryptionMethod {self.reserved3} in a v{self.version} TOC header") from None

    @property
    def encryption_iv_count(self) -> int:
        if self.version < TocVersion.ContainerEncryptionMethod:
            return 0
        return struct.unpack_from("<I", self.reserved8, 0)[0]

    def set_encryption_iv_count(self, n: int) -> None:
        self.reserved8 = struct.pack("<I", n) + self.reserved8[4:]

    @property
    def source_hash_count(self) -> int:
        return self.reserved7 if self.version >= TocVersion.AddedSourceHashes else 0

    @property
    def seeds_count(self) -> int:
        return self.perfect_hash_seeds_count if self.version >= TocVersion.PerfectHash else 0

    @property
    def overflow_count(self) -> int:
        return self.chunks_without_perfect_hash_count if self.version >= TocVersion.PerfectHashWithOverflow else 0

    @property
    def meta_uses_iohash(self) -> bool:
        return self.version >= TocVersion.ReplaceIoChunkHashWithIoHash

    @property
    def guid_hex(self) -> str:
        a, b, c, d = struct.unpack("<4I", self.encryption_key_guid)
        return f"{a:08X}{b:08X}{c:08X}{d:08X}"

    def to_json(self) -> dict:
        return {
            "version": self.version, "version_name": self.version_name,
            "known_by_this_tool": self.version <= LATEST_KNOWN_VERSION,
            "toc_header_size": self.toc_header_size, "entry_count": self.entry_count,
            "compressed_block_entry_count": self.compressed_block_entry_count,
            "compressed_block_entry_size": self.compressed_block_entry_size,
            "compression_method_name_count": self.compression_method_name_count,
            "compression_method_name_length": self.compression_method_name_length,
            "compression_block_size": self.compression_block_size,
            "directory_index_size": self.directory_index_size,
            "partition_count_raw": self.partition_count, "partition_size_raw": self.partition_size,
            "partition_count": self.effective_partition_count,
            "partition_size": self.effective_partition_size,
            "container_id": f"0x{self.container_id:016X}",
            "encryption_key_guid": self.guid_hex,
            "container_flags": self.container_flags,
            "container_flag_names": [f.name for f in ContainerFlags if f and self.flags & f],
            "encryption_method": self.encryption_method.name,
            "perfect_hash_seeds_count": self.seeds_count,
            "chunks_without_perfect_hash_count": self.overflow_count,
            "encryption_iv_count": self.encryption_iv_count,
            "source_hash_count": self.source_hash_count,
            # A field that only gets a meaning in a later TOC version is still "reserved" in this one
            # (UE4.26 FIoStoreTocHeader: Reserved3/4/5, Reserved6[6]); the reader ignores it, so a
            # non-zero value there is game-specific data worth knowing about.
            "reserved_nonzero": bool(self.reserved0 or self.reserved1 or self.reserved4
                                     or (self.version < TocVersion.PartitionSize
                                         and (self.partition_count or self.partition_size))
                                     or (self.version < TocVersion.PerfectHash and self.perfect_hash_seeds_count)
                                     or (self.version < TocVersion.PerfectHashWithOverflow
                                         and self.chunks_without_perfect_hash_count)
                                     or (self.version < TocVersion.ContainerEncryptionMethod and self.reserved3)
                                     or (self.version < TocVersion.AddedSourceHashes and self.reserved7)
                                     or (self.version < TocVersion.ContainerEncryptionMethod and any(self.reserved8))),
        }


# ---------------------------------------------------------------------------------------------
# Small records
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ChunkId:
    """FIoChunkId (FIoChunkId.cs): u64 ChunkId LE | u16 ChunkIndex stored NETWORK_ORDER16 (big
    endian) | u8 padding | u8 ChunkType. Kept as the raw 12 bytes."""
    raw: bytes

    @classmethod
    def from_parts(cls, chunk_id: int, chunk_index: int, chunk_type: int) -> "ChunkId":
        return cls(struct.pack("<Q", chunk_id) + struct.pack(">H", chunk_index) + bytes([0, chunk_type]))

    @classmethod
    def from_hex(cls, text: str) -> "ChunkId":
        t = text.strip().lower()
        if t.startswith("0x"):
            t = t[2:]
        if len(t) != 24:
            raise IoStoreError(f"chunk id must be 24 hex chars (12 raw bytes), got {text!r}")
        return cls(bytes.fromhex(t))

    @property
    def id(self) -> int:
        return struct.unpack_from("<Q", self.raw, 0)[0]

    @property
    def index(self) -> int:
        return struct.unpack_from(">H", self.raw, 8)[0]

    @property
    def padding(self) -> int:
        return self.raw[10]

    @property
    def type(self) -> int:
        return self.raw[11]

    @property
    def hex(self) -> str:
        return self.raw.hex()

    def type_name(self, ue5: bool) -> str:
        names = UE5_CHUNK_TYPES if ue5 else UE4_CHUNK_TYPES
        return names[self.type] if self.type < len(names) else f"Type{self.type}"

    def hash_with_seed(self, seed: int) -> int:
        """FIoChunkId.HashWithSeed: FNV-1 (multiply, then xor) over the 12 raw bytes."""
        h = (seed & MASK64) if seed != 0 else FNV64_OFFSET
        for b in self.raw:
            h = ((h * FNV64_PRIME) & MASK64) ^ b
        return h


@dataclass(frozen=True)
class CompressedBlock:
    """FIoStoreTocCompressedBlockEntry: u64 = Offset(40) | CompressedSize(24)<<40;
    u32 = UncompressedSize(24) | CompressionMethodIndex(8)<<24. Bijective with the 12 bytes."""
    offset: int
    compressed_size: int
    uncompressed_size: int
    method_index: int

    def pack(self) -> bytes:
        if self.offset > MAX_U40 or self.compressed_size > MAX_U24 or self.uncompressed_size > MAX_U24 \
                or self.method_index > 0xFF:
            raise IoStoreError(f"compression block field overflow: {self}")
        return struct.pack("<QI", self.offset | (self.compressed_size << 40),
                           self.uncompressed_size | (self.method_index << 24))


@dataclass(frozen=True)
class TocEntryMeta:
    """FIoStoreTocEntryMeta: versions < 8: FIoChunkHash (32 bytes) + u8 flags (33 bytes);
    versions >= 8 (ReplaceIoChunkHashWithIoHash): FIoHash (20 bytes) + u8 flags + 3 pad (24 bytes)."""
    hash: bytes
    flags: int
    pad: bytes = b""

    def pack(self) -> bytes:
        return self.hash + bytes([self.flags]) + self.pad


@dataclass
class Signatures:
    """Signed containers (FIoStoreTocResource.cs): i32 HashSize, TocSignature[HashSize],
    BlockSignature[HashSize], FSHAHash[TocCompressedBlockEntryCount] (20-byte SHA-1 per block)."""
    hash_size: int
    toc_signature: bytes
    block_signature: bytes
    block_hashes: List[bytes]

    def pack(self) -> bytes:
        return struct.pack("<i", self.hash_size) + self.toc_signature + self.block_signature + b"".join(self.block_hashes)


# ---------------------------------------------------------------------------------------------
# FIoStoreTocResource (.utoc)
# ---------------------------------------------------------------------------------------------

@dataclass
class TocResource:
    """The whole .utoc file, section by section, in the order of FIoStoreTocResource.cs:
    header | ChunkIds[N] | ChunkOffsetLengths[N] | PerfectHashSeeds[S] (v>=4) |
    ChunksWithoutPerfectHash[O] (v>=5) | CompressionBlocks[B] | EncryptionIVs[IV] (v>=10, AES-CTR) |
    CompressionMethodNames[M x L] | Signatures (Signed) | DirectoryIndex[DirectoryIndexSize] (v>=2) |
    Metas[N] | OnDemand metas (v==6 && OnDemand) | trailing bytes (anything after, kept verbatim)."""
    header: TocHeader
    chunk_ids: List[ChunkId]
    offsets: List[Tuple[int, int]]
    perfect_hash_seeds: List[int]
    chunks_without_perfect_hash: List[int]
    blocks: List[CompressedBlock]
    encryption_ivs: List[bytes]
    method_names_raw: List[bytes]
    signatures: Optional[Signatures]
    directory_index_raw: bytes
    metas: Optional[List[TocEntryMeta]]
    on_demand_chunk_hashes: Optional[List[bytes]] = None
    on_demand_block_hashes: Optional[List[bytes]] = None
    trailing: bytes = b""

    # --- parsing ---
    @classmethod
    def parse(cls, data: bytes) -> "TocResource":
        h = TocHeader.parse(data)
        c = _Cursor(data, TOC_HEADER_SIZE)
        n = h.entry_count
        ids_raw = c.take(12 * n, "ChunkIds")
        chunk_ids = [ChunkId(ids_raw[i:i + 12]) for i in range(0, len(ids_raw), 12)]
        ol_raw = c.take(10 * n, "ChunkOffsetLengths")
        offsets = [(int.from_bytes(ol_raw[i:i + 5], "big"), int.from_bytes(ol_raw[i + 5:i + 10], "big"))
                   for i in range(0, len(ol_raw), 10)]
        seeds = list(struct.unpack(f"<{h.seeds_count}i", c.take(4 * h.seeds_count, "PerfectHashSeeds")))
        overflow = list(struct.unpack(f"<{h.overflow_count}i",
                                      c.take(4 * h.overflow_count, "ChunkIndicesWithoutPerfectHash")))
        nb = h.compressed_block_entry_count
        blocks = []
        for lo, hi in struct.iter_unpack("<QI", c.take(12 * nb, "CompressionBlocks")):
            blocks.append(CompressedBlock(lo & MAX_U40, lo >> 40, hi & MAX_U24, hi >> 24))
        ivs: List[bytes] = []
        if h.version >= TocVersion.ContainerEncryptionMethod:
            expected = (nb + (1 if h.flags & ContainerFlags.Indexed else 0)) \
                if h.encryption_method == EncryptionMethod.AES_CTR else 0
            if h.encryption_iv_count != expected:
                raise IoStoreError(f"TOC has {h.encryption_iv_count} encryption IVs, "
                                   f"{h.encryption_method.name} over {nb} blocks needs {expected}")
            raw = c.take(12 * h.encryption_iv_count, "EncryptionIVs")
            ivs = [raw[i:i + 12] for i in range(0, len(raw), 12)]
        ml = h.compression_method_name_length
        names_raw = c.take(ml * h.compression_method_name_count, "CompressionMethodNames")
        method_names_raw = [names_raw[i:i + ml] for i in range(0, len(names_raw), ml)] if ml else \
            [b""] * h.compression_method_name_count
        signatures = None
        if h.flags & ContainerFlags.Signed:
            hs = c.i32("signature hash size")
            if hs < 0 or hs > 1 << 20:
                raise IoStoreError(f"implausible signature hash size {hs}")
            toc_sig = c.take(hs, "TocSignature")
            blk_sig = c.take(hs, "BlockSignature")
            bh_raw = c.take(20 * nb, "ChunkBlockSignatures")
            signatures = Signatures(hs, toc_sig, blk_sig, [bh_raw[i:i + 20] for i in range(0, len(bh_raw), 20)])
        dir_raw = b""
        if h.version >= TocVersion.DirectoryIndex:
            dir_raw = c.take(h.directory_index_size, "DirectoryIndex")
        metas = None
        meta_size = 24 if h.meta_uses_iohash else 33
        if n == 0 or c.remaining() >= meta_size * n:
            raw = c.take(meta_size * n, "TocEntryMeta")
            metas = []
            for i in range(0, len(raw), meta_size):
                if h.meta_uses_iohash:
                    metas.append(TocEntryMeta(raw[i:i + 20], raw[i + 20], raw[i + 21:i + 24]))
                else:
                    metas.append(TocEntryMeta(raw[i:i + 32], raw[i + 32], b""))
        od_chunks = od_blocks = None
        if metas is not None and h.version == TocVersion.OnDemandMetaData and h.flags & ContainerFlags.OnDemand:
            r1 = c.take(20 * n, "OnDemandChunkMeta")
            r2 = c.take(20 * nb, "OnDemandCompressedBlockMeta")
            od_chunks = [r1[i:i + 20] for i in range(0, len(r1), 20)]
            od_blocks = [r2[i:i + 20] for i in range(0, len(r2), 20)]
        trailing = c.take(c.remaining(), "trailing")
        return cls(h, chunk_ids, offsets, seeds, overflow, blocks, ivs, method_names_raw, signatures, dir_raw,
                   metas, od_chunks, od_blocks, trailing)

    # --- serialisation (exact inverse of parse) ---
    def serialize(self) -> bytes:
        h = self.header
        parts = [h.serialize(),
                 b"".join(c.raw for c in self.chunk_ids),
                 b"".join(o.to_bytes(5, "big") + ln.to_bytes(5, "big") for o, ln in self.offsets)]
        if h.version >= TocVersion.PerfectHash:
            parts.append(struct.pack(f"<{len(self.perfect_hash_seeds)}i", *self.perfect_hash_seeds))
        if h.version >= TocVersion.PerfectHashWithOverflow:
            parts.append(struct.pack(f"<{len(self.chunks_without_perfect_hash)}i", *self.chunks_without_perfect_hash))
        parts.append(b"".join(b.pack() for b in self.blocks))
        parts.append(b"".join(self.encryption_ivs))
        parts.append(b"".join(self.method_names_raw))
        if self.signatures is not None:
            parts.append(self.signatures.pack())
        parts.append(self.directory_index_raw)
        if self.metas is not None:
            parts.append(b"".join(m.pack() for m in self.metas))
        if self.on_demand_chunk_hashes is not None:
            parts.append(b"".join(self.on_demand_chunk_hashes))
            parts.append(b"".join(self.on_demand_block_hashes or []))
        parts.append(self.trailing)
        return b"".join(parts)

    def sync_header_counts(self) -> None:
        """Recompute every header count from the section contents (used by the writer)."""
        h = self.header
        h.entry_count = len(self.chunk_ids)
        h.compressed_block_entry_count = len(self.blocks)
        h.compression_method_name_count = len(self.method_names_raw)
        h.directory_index_size = len(self.directory_index_raw)
        if h.version >= TocVersion.PerfectHash:
            h.perfect_hash_seeds_count = len(self.perfect_hash_seeds)
        if h.version >= TocVersion.PerfectHashWithOverflow:
            h.chunks_without_perfect_hash_count = len(self.chunks_without_perfect_hash)
        if h.version >= TocVersion.ContainerEncryptionMethod:
            h.set_encryption_iv_count(len(self.encryption_ivs))
        if self.signatures is not None:
            h.container_flags = int(h.container_flags) | int(ContainerFlags.Signed)
        else:
            h.container_flags = int(h.container_flags) & ~int(ContainerFlags.Signed) & 0xFF

    def clone(self) -> "TocResource":
        return TocResource.parse(self.serialize())

    # --- derived information ---
    @property
    def method_names(self) -> List[str]:
        """Index 0 is always None; names[i] is the method of CompressionMethodIndex i."""
        return ["None"] + [r.split(b"\0", 1)[0].decode("ascii", "replace") for r in self.method_names_raw]

    def method_name(self, index: int) -> str:
        names = self.method_names
        return names[index] if index < len(names) else f"Invalid({index})"

    def chunk_block_range(self, index: int) -> Tuple[int, int, int]:
        """(first_block, last_block_inclusive, offset_in_first_block) - IoStoreReader.Read()."""
        off, ln = self.offsets[index]
        bs = self.header.compression_block_size
        if ln == 0:
            return off // bs, off // bs - 1, off % bs
        first = off // bs
        last = (align_up(off + ln, bs) - 1) // bs
        return first, last, off % bs

    @property
    def ue5_naming(self) -> bool:
        """EIoChunkType5 applies from UE5 (TOC v4 PerfectHash onwards); UE4.25-4.27 used v1-v3."""
        return self.header.version >= TocVersion.PerfectHash

    def resolve(self, chunk_id: ChunkId) -> Optional[int]:
        """IoStoreReader.TryResolve: perfect-hash lookup, overflow fallback, else linear search.
        Returns the TOC entry index or None."""
        n = len(self.chunk_ids)
        if self.perfect_hash_seeds:
            if n == 0:
                return None
            seed = self.perfect_hash_seeds[chunk_id.hash_with_seed(0) % len(self.perfect_hash_seeds)]
            if seed == 0:
                return None
            if seed < 0:
                slot = -seed - 1
                if slot >= n:
                    for i in self.chunks_without_perfect_hash:
                        if 0 <= i < n and self.chunk_ids[i] == chunk_id:
                            return i
                    return None
            else:
                slot = chunk_id.hash_with_seed(seed) % n
            return slot if self.chunk_ids[slot] == chunk_id else None
        for i, cid in enumerate(self.chunk_ids):
            if cid == chunk_id:
                return i
        return None

    def validate_perfect_hash(self) -> dict:
        res = {"present": bool(self.perfect_hash_seeds), "seeds": len(self.perfect_hash_seeds),
               "overflow_entries": len(self.chunks_without_perfect_hash), "failures": []}
        if not self.perfect_hash_seeds:
            return res
        for i, cid in enumerate(self.chunk_ids):
            r = self.resolve(cid)
            if r != i:
                res["failures"].append({"index": i, "chunk_id": cid.hex, "resolved": r})
                if len(res["failures"]) >= 20:
                    break
        res["ok"] = not res["failures"]
        return res

    def layout_report(self) -> dict:
        """Checks the assumptions the writer relies on: each chunk starts on a block boundary and no
        two chunks share a block."""
        bs = self.header.compression_block_size
        owner: Dict[int, int] = {}
        misaligned, shared, out_of_range = [], [], []
        for i in range(len(self.chunk_ids)):
            off, ln = self.offsets[i]
            if ln and off % bs:
                misaligned.append(i)
            first, last, _ = self.chunk_block_range(i)
            if ln and last >= len(self.blocks):
                out_of_range.append(i)
                continue
            for b in range(first, last + 1):
                if b in owner:
                    shared.append((owner[b], i, b))
                owner[b] = i
        return {"chunks_misaligned": misaligned[:20], "chunks_misaligned_count": len(misaligned),
                "shared_blocks": shared[:20], "shared_blocks_count": len(shared),
                "chunks_out_of_range": out_of_range[:20], "chunks_out_of_range_count": len(out_of_range),
                "referenced_blocks": len(owner), "orphan_blocks": len(self.blocks) - len(owner)}


# ---------------------------------------------------------------------------------------------
# FIoDirectoryIndexResource
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class FString:
    """UE FString archive form: i32 Len (incl. NUL); Len>0 -> Len ANSI bytes; Len<0 -> -Len UTF-16LE
    code units; Len==0 -> empty, no bytes. `raw` holds the bytes after the length (incl. NUL)."""
    raw: bytes
    wide: bool

    @classmethod
    def read(cls, c: _Cursor, label: str) -> "FString":
        n = c.i32(label)
        if n == 0:
            return cls(b"", False)
        if n < 0:
            if -n > 1 << 24:
                raise IoStoreError(f"implausible FString length {n} in {label}")
            return cls(c.take(-2 * n, label), True)
        if n > 1 << 24:
            raise IoStoreError(f"implausible FString length {n} in {label}")
        return cls(c.take(n, label), False)

    @classmethod
    def from_str(cls, s: str) -> "FString":
        if not s:
            return cls(b"", False)
        if all(ord(ch) < 128 for ch in s):
            return cls(s.encode("ascii") + b"\0", False)
        return cls(s.encode("utf-16-le") + b"\0\0", True)

    def pack(self) -> bytes:
        if not self.raw:
            return struct.pack("<i", 0)
        n = -(len(self.raw) // 2) if self.wide else len(self.raw)
        return struct.pack("<i", n) + self.raw

    @property
    def value(self) -> str:
        if self.wide:
            return self.raw.decode("utf-16-le", "replace").split("\0", 1)[0]
        return self.raw.decode("latin-1").split("\0", 1)[0]


@dataclass
class DirectoryIndex:
    """FIoDirectoryIndexResource as walked by IoStoreReader.ProcessIndex/ReadIndex:
    FString MountPoint | TArray<FIoDirectoryIndexEntry{Name, FirstChildEntry, NextSiblingEntry,
    FirstFileEntry}> | TArray<FIoFileIndexEntry{Name, NextFileEntry, UserData}> |
    TArray<FString> StringTable | trailing (AES padding). UserData = TOC entry index."""
    mount_point: FString
    directories: List[Tuple[int, int, int, int]]
    files: List[Tuple[int, int, int]]
    strings: List[FString]
    trailing: bytes = b""

    @classmethod
    def parse(cls, data: bytes) -> "DirectoryIndex":
        c = _Cursor(data, 0, "directory index")
        mp = FString.read(c, "MountPoint")
        nd = c.i32("DirectoryEntries count")
        if nd < 0 or nd * 16 > c.remaining():
            raise IoStoreError(f"directory index: bad directory count {nd}")
        dirs = list(struct.iter_unpack("<4I", c.take(16 * nd, "DirectoryEntries")))
        nf = c.i32("FileEntries count")
        if nf < 0 or nf * 12 > c.remaining():
            raise IoStoreError(f"directory index: bad file count {nf}")
        files = list(struct.iter_unpack("<3I", c.take(12 * nf, "FileEntries")))
        ns = c.i32("StringTable count")
        if ns < 0 or ns * 4 > c.remaining():
            raise IoStoreError(f"directory index: bad string count {ns}")
        strings = [FString.read(c, f"StringTable[{i}]") for i in range(ns)]
        return cls(mp, dirs, files, strings, c.take(c.remaining(), "trailing"))

    def serialize(self) -> bytes:
        return b"".join([self.mount_point.pack(),
                         struct.pack("<i", len(self.directories)),
                         b"".join(struct.pack("<4I", *d) for d in self.directories),
                         struct.pack("<i", len(self.files)),
                         b"".join(struct.pack("<3I", *f) for f in self.files),
                         struct.pack("<i", len(self.strings)),
                         b"".join(s.pack() for s in self.strings),
                         self.trailing])

    def walk(self) -> List[Tuple[str, int]]:
        """[(path relative to mount point, user_data)] - same traversal as IoStoreReader.ReadIndex."""
        out: List[Tuple[str, int]] = []
        if not self.directories:
            return out
        stack = [(0, "")]
        seen = set()
        while stack:
            d, parent = stack.pop()
            if d in seen or d >= len(self.directories):
                raise IoStoreError(f"directory index: loop or bad directory handle {d}")
            seen.add(d)
            name, first_child, next_sibling, first_file = self.directories[d]
            path = parent
            if name != INVALID_HANDLE:
                nm = self.strings[name].value
                if nm:
                    path = f"{parent}{nm}/"
            if next_sibling != INVALID_HANDLE:
                stack.append((next_sibling, parent))
            if first_child != INVALID_HANDLE:
                stack.append((first_child, path))
            f = first_file
            guard = 0
            while f != INVALID_HANDLE:
                fname, nxt, user = self.files[f]
                out.append((path + self.strings[fname].value, user))
                f = nxt
                guard += 1
                if guard > len(self.files):
                    raise IoStoreError("directory index: file list loop")
        return out


def is_valid_index_prefix(data: bytes) -> bool:
    """AbstractVfsReader.IsValidIndex: plausible mount-point FString (|len| <= 128, NUL terminated)."""
    if len(data) < 5:
        return False
    n = struct.unpack_from("<i", data, 0)[0]
    if n > 128 or n < -128:
        return False
    if n == 0:
        return data[4] == 0
    if n < 0:
        end = 4 + (-n) * 2
        return len(data) >= end and data[end - 2:end] == b"\0\0"
    return len(data) >= 4 + n and data[4 + n - 1] == 0


# ---------------------------------------------------------------------------------------------
# Crypto
# ---------------------------------------------------------------------------------------------

class AesCipher:
    """AES-256: ECB for EIoEncryptionMethod.AES (all UE4/UE5 containers so far); Unreal AES-CTR
    (counter block = 12-byte IV || big-endian u32 block counter, Aes.cs FillCounterBlocks)."""

    def __init__(self, key: bytes):
        if len(key) != 32:
            raise IoStoreError("AES key must be 32 bytes (64 hex characters)")
        self._key = key
        try:
            from Crypto.Cipher import AES  # pycryptodome
            self._ecb = AES.new(key, AES.MODE_ECB)
            self._backend = "pycryptodome"
        except ImportError:
            try:
                from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            except ImportError as e:
                raise IoStoreError("AES needs pycryptodome or cryptography (pip install pycryptodome)") from e
            self._ecb = None
            self._cipher = lambda mode: Cipher(algorithms.AES(key), mode)
            self._modes = modes
            self._backend = "cryptography"

    def __repr__(self) -> str:  # never reveal the key
        return f"AesCipher(<{self._backend}>)"

    def ecb_decrypt(self, data: bytes) -> bytes:
        if len(data) % AES_BLOCK:
            raise IoStoreError(f"AES-ECB input length {len(data)} is not a multiple of 16")
        if self._ecb is not None:
            return self._ecb.decrypt(data)
        d = self._cipher(self._modes.ECB()).decryptor()
        return d.update(data) + d.finalize()

    def ecb_encrypt(self, data: bytes) -> bytes:
        if len(data) % AES_BLOCK:
            raise IoStoreError(f"AES-ECB input length {len(data)} is not a multiple of 16")
        if self._ecb is not None:
            return self._ecb.encrypt(data)
        e = self._cipher(self._modes.ECB()).encryptor()
        return e.update(data) + e.finalize()

    def ctr_crypt(self, data: bytes, iv12: bytes) -> bytes:
        if len(iv12) != 12:
            raise IoStoreError("AES-CTR IV must be 12 bytes")
        if self._ecb is not None:
            from Crypto.Cipher import AES
            return AES.new(self._key, AES.MODE_CTR, nonce=iv12, initial_value=0).encrypt(data)
        e = self._cipher(self._modes.CTR(iv12 + b"\0\0\0\0")).encryptor()
        return e.update(data) + e.finalize()


def load_aes_key(key_file: Optional[str]) -> Tuple[Optional[bytes], str]:
    """Key from --aes-key-file (hex text, optional 0x, or 32 raw bytes) or env R9_AES_KEY_HEX.
    Returns (key or None, source). Error messages never contain key material."""
    if key_file:
        with open(key_file, "rb") as f:
            raw = f.read()
        if len(raw) == 32 and not all(chr(b) in "0123456789abcdefABCDEFx \r\n\t" for b in raw):
            return raw, "file(binary)"
        if raw.startswith(b"\xef\xbb\xbf"):  # UTF-8 BOM written by some Windows editors
            raw = raw[3:]
        return _parse_hex_key(raw.decode("ascii", "replace"), "--aes-key-file"), "file"
    env = os.environ.get(KEY_ENV)
    if env:
        return _parse_hex_key(env, KEY_ENV), "env"
    return None, "none"


def _parse_hex_key(text: str, origin: str) -> bytes:
    t = "".join(text.split())
    if t.lower().startswith("0x"):
        t = t[2:]
    if len(t) != 64:
        raise IoStoreError(f"AES key from {origin} must be 64 hex characters (got {len(t)} characters)")
    try:
        return bytes.fromhex(t)
    except ValueError:
        raise IoStoreError(f"AES key from {origin} is not valid hex") from None


# ---------------------------------------------------------------------------------------------
# Hashes for TOC meta
# ---------------------------------------------------------------------------------------------

_B3_IV = (0x6A09E667, 0xBB67AE85, 0x3C6EF372, 0xA54FF53A, 0x510E527F, 0x9B05688C, 0x1F83D9AB, 0x5BE0CD19)
_B3_PERM = (2, 6, 3, 10, 7, 0, 4, 13, 1, 11, 12, 5, 9, 14, 15, 8)


def _b3_compress(cv, words, counter, block_len, flags):
    v = list(cv) + list(_B3_IV[:4]) + [counter & MASK32, (counter >> 32) & MASK32, block_len, flags]
    m = list(words)

    def g(a, b, c, d, x, y):
        v[a] = (v[a] + v[b] + x) & MASK32
        t = v[d] ^ v[a]
        v[d] = ((t >> 16) | (t << 16)) & MASK32
        v[c] = (v[c] + v[d]) & MASK32
        t = v[b] ^ v[c]
        v[b] = ((t >> 12) | (t << 20)) & MASK32
        v[a] = (v[a] + v[b] + y) & MASK32
        t = v[d] ^ v[a]
        v[d] = ((t >> 8) | (t << 24)) & MASK32
        v[c] = (v[c] + v[d]) & MASK32
        t = v[b] ^ v[c]
        v[b] = ((t >> 7) | (t << 25)) & MASK32

    for r in range(7):
        g(0, 4, 8, 12, m[0], m[1]); g(1, 5, 9, 13, m[2], m[3])
        g(2, 6, 10, 14, m[4], m[5]); g(3, 7, 11, 15, m[6], m[7])
        g(0, 5, 10, 15, m[8], m[9]); g(1, 6, 11, 12, m[10], m[11])
        g(2, 7, 8, 13, m[12], m[13]); g(3, 4, 9, 14, m[14], m[15])
        if r < 6:
            m = [m[p] for p in _B3_PERM]
    return [v[i] ^ v[i + 8] for i in range(8)] + [v[i + 8] ^ cv[i] for i in range(8)]


def blake3_pure(data: bytes, out_len: int = 32) -> bytes:
    """Reference BLAKE3 (unkeyed, out_len <= 64) in pure Python; slow fallback when the `blake3`
    package is missing. Follows the BLAKE3 reference implementation (chunk 1024 B, block 64 B)."""
    CHUNK_START, CHUNK_END, PARENT, ROOT = 1, 2, 4, 8
    chunks = [data[i:i + 1024] for i in range(0, len(data), 1024)] or [b""]
    stack: List[List[int]] = []

    def chunk_output(chunk: bytes, counter: int):
        cv = list(_B3_IV)
        blocks = [chunk[i:i + 64] for i in range(0, len(chunk), 64)] or [b""]
        for bi, blk in enumerate(blocks):
            flags = (CHUNK_START if bi == 0 else 0) | (CHUNK_END if bi == len(blocks) - 1 else 0)
            words = struct.unpack("<16I", blk.ljust(64, b"\0"))
            if bi == len(blocks) - 1:
                return cv, words, counter, len(blk), flags
            cv = _b3_compress(cv, words, counter, 64, flags)[:8]

    for ci, chunk in enumerate(chunks[:-1]):
        cv, words, counter, blen, flags = chunk_output(chunk, ci)
        new_cv = _b3_compress(cv, words, counter, blen, flags)[:8]
        total = ci + 1
        while total & 1 == 0:
            new_cv = _b3_compress(_B3_IV, stack.pop() + new_cv, 0, 64, PARENT)[:8]
            total >>= 1
        stack.append(new_cv)
    out = chunk_output(chunks[-1], len(chunks) - 1)
    while stack:
        cv, words, counter, blen, flags = out
        child = _b3_compress(cv, words, counter, blen, flags)[:8]
        out = (list(_B3_IV), stack.pop() + child, 0, 64, PARENT)
    cv, words, counter, blen, flags = out
    root = _b3_compress(cv, words, 0, blen, flags | ROOT)
    return struct.pack("<16I", *root)[:out_len]


def blake3_digest(data: bytes, out_len: int = 32) -> bytes:
    try:
        import blake3  # type: ignore
        return blake3.blake3(data).digest(length=out_len)
    except ImportError:
        return blake3_pure(data, out_len)


HASH_ALGOS = ("sha1", "blake3", "zero")


def compute_meta_hash(algo: str, data: bytes, iohash: bool) -> bytes:
    """FIoChunkHash (32 bytes = 20-byte digest + 12 zero bytes) or FIoHash (20 bytes, BLAKE3-160)."""
    if algo == "sha1":
        d = hashlib.sha1(data).digest()
    elif algo == "blake3":
        d = blake3_digest(data, 20)
    elif algo == "zero":
        d = b"\0" * 20
    else:
        raise IoStoreError(f"unknown hash algorithm {algo}")
    return d if iohash else d + b"\0" * 12


# ---------------------------------------------------------------------------------------------
# Decompression
# ---------------------------------------------------------------------------------------------

class Decompressor:
    """None / Zlib / Gzip / LZ4 (python-lz4, raw LZ4 block) / Zstd (optional `zstandard`) /
    Oodle via an external shared library given by --oodle-lib or env R9_OODLE_LIB:
      * official Oodle core (liboo2core*.so / oo2core_*.dll / .dylib): symbol OodleLZ_Decompress
      * libooz built by `iostore.py build-ooz` from https://github.com/powzix/ooz: ooz_decompress
    """

    def __init__(self, oodle_lib: Optional[str] = None, use_env: bool = True):
        self.oodle_lib_path = oodle_lib or (os.environ.get(OODLE_ENV) if use_env else None)
        self._oodle = None
        self._oodle_kind = None

    def availability(self, method: str) -> Tuple[bool, str]:
        m = method.lower()
        if m in ("none", "zlib", "gzip"):
            return True, "python zlib"
        if m == "lz4":
            return (True, "python-lz4") if importlib.util.find_spec("lz4") else (False, "pip install lz4")
        if m == "zstd":
            return (True, "zstandard") if importlib.util.find_spec("zstandard") else (False, "pip install zstandard")
        if m == "oodle":
            if not self.oodle_lib_path:
                return False, (f"set {OODLE_ENV} (or --oodle-lib) to liboo2core*.so/oo2core_*.dll or to a libooz.so "
                               f"built with `iostore.py build-ooz --src <ooz checkout> --out-dir <dir>`")
            try:
                self._load_oodle()
                return True, f"{self._oodle_kind}: {self.oodle_lib_path}"
            except IoStoreError as e:
                return False, str(e)
        return False, f"compression method {method!r} is not implemented"

    def _load_oodle(self):
        if self._oodle is not None:
            return
        path = self.oodle_lib_path
        try:
            lib = ctypes.CDLL(path)
        except OSError as e:
            raise UnsupportedCompression(f"cannot load Oodle library {path}: {e}") from None
        if hasattr(lib, "OodleLZ_Decompress"):
            f = lib.OodleLZ_Decompress
            f.argtypes = [ctypes.c_void_p, ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_ssize_t, ctypes.c_int,
                          ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_ssize_t, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ssize_t, ctypes.c_int]
            f.restype = ctypes.c_ssize_t
            self._oodle_kind = "OodleLZ_Decompress"
        elif hasattr(lib, "ooz_decompress"):
            f = lib.ooz_decompress
            f.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
            f.restype = ctypes.c_int
            self._oodle_kind = "ooz"
        else:
            raise UnsupportedCompression(f"{path} exports neither OodleLZ_Decompress nor ooz_decompress")
        self._oodle = f

    def decompress(self, method: str, data: bytes, usize: int) -> bytes:
        m = method.lower()
        if m == "zlib":
            try:
                out = zlib.decompress(data)
            except zlib.error:
                out = zlib.decompress(data, -15)  # raw deflate fallback
        elif m == "gzip":
            out = zlib.decompress(data, 31)
        elif m == "lz4":
            try:
                import lz4.block
            except ImportError:
                raise UnsupportedCompression("LZ4 block needs `pip install lz4`") from None
            out = lz4.block.decompress(data, uncompressed_size=usize)
        elif m == "zstd":
            try:
                import zstandard
            except ImportError:
                raise UnsupportedCompression("Zstd block needs `pip install zstandard`") from None
            out = zstandard.ZstdDecompressor().decompress(data, max_output_size=usize)
        elif m == "oodle":
            ok, why = self.availability("Oodle")
            if not ok:
                raise UnsupportedCompression("Oodle block: " + why)
            buf = ctypes.create_string_buffer(usize + 64)
            if self._oodle_kind == "ooz":
                n = self._oodle(data, len(data), buf, usize)
            else:
                src = ctypes.create_string_buffer(data, len(data))
                n = self._oodle(src, len(data), buf, usize, 1, 0, 0, None, 0, None, None, None, 0, 3)
            if n != usize:
                raise IoStoreError(f"Oodle decompression returned {n}, expected {usize}")
            out = buf.raw[:usize]
        else:
            raise UnsupportedCompression(f"compression method {method!r} is not supported")
        if len(out) != usize:
            raise IoStoreError(f"{method}: decompressed {len(out)} bytes, block says {usize}")
        return out


# ---------------------------------------------------------------------------------------------
# Container reader
# ---------------------------------------------------------------------------------------------

def partition_path(utoc_path: str, index: int) -> str:
    """IoStoreReader: partition 0 = <base>.ucas, partition i = <base>_s<i>.ucas."""
    base = utoc_path[:-5] if utoc_path.lower().endswith(".utoc") else utoc_path
    return f"{base}.ucas" if index == 0 else f"{base}_s{index}.ucas"


class IoStoreContainer:
    """Read access to one container. `aes_key` is required for encrypted containers to read the
    directory index or chunk data (the TOC itself is never encrypted in stock UE)."""

    def __init__(self, utoc_path: str, aes_key: Optional[bytes] = None, decompressor: Optional[Decompressor] = None):
        self.utoc_path = os.path.abspath(utoc_path)
        with open(self.utoc_path, "rb") as f:
            self.toc_bytes = f.read()
        self.toc = TocResource.parse(self.toc_bytes)
        self.header = self.toc.header
        self.aes_key = aes_key
        self.aes = AesCipher(aes_key) if aes_key else None
        self.decompressor = decompressor or Decompressor()
        self._files: Dict[int, object] = {}
        self._dir_index: Optional[DirectoryIndex] = None
        self._dir_index_plain: Optional[bytes] = None
        self._paths: Optional[List[Tuple[str, int]]] = None

    # --- lifecycle ---
    def close(self):
        for f in self._files.values():
            f.close()
        self._files.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- basic properties ---
    @property
    def name(self) -> str:
        return os.path.basename(self.utoc_path)[:-5]

    @property
    def encrypted(self) -> bool:
        return bool(self.header.flags & ContainerFlags.Encrypted)

    @property
    def partition_paths(self) -> List[str]:
        return [partition_path(self.utoc_path, i) for i in range(self.header.effective_partition_count)]

    def input_files(self) -> List[str]:
        return [self.utoc_path] + [p for p in self.partition_paths if os.path.exists(p)]

    def _need_key(self, what: str):
        if self.encrypted and self.aes is None:
            raise KeyRequired(f"{what}: container is encrypted (key GUID {self.header.guid_hex}); "
                              f"pass --aes-key-file or set {KEY_ENV}")

    def _partition_file(self, index: int):
        f = self._files.get(index)
        if f is None:
            p = partition_path(self.utoc_path, index)
            if not os.path.exists(p):
                raise IoStoreError(f"missing partition file {p}")
            f = open(p, "rb")
            self._files[index] = f
        return f

    # --- directory index ---
    def directory_index_plain(self) -> Optional[bytes]:
        if self._dir_index_plain is not None:
            return self._dir_index_plain
        raw = self.toc.directory_index_raw
        if not raw or not (self.header.flags & ContainerFlags.Indexed):
            return None
        if self.encrypted:
            self._need_key("directory index")
            if self.header.encryption_method == EncryptionMethod.AES_CTR:
                plain = self.aes.ctr_crypt(raw, self.toc.encryption_ivs[-1])
            else:
                plain = self.aes.ecb_decrypt(raw)
            if not is_valid_index_prefix(plain):
                raise KeyRequired("AES key does not decrypt the directory index (mount point check failed)")
        else:
            plain = raw
        self._dir_index_plain = plain
        return plain

    def directory_index(self) -> Optional[DirectoryIndex]:
        if self._dir_index is None:
            plain = self.directory_index_plain()
            if plain is None:
                return None
            self._dir_index = DirectoryIndex.parse(plain)
        return self._dir_index

    def key_status(self) -> str:
        if not self.encrypted:
            return "not-encrypted"
        if self.aes is None:
            return "missing"
        try:
            if self.directory_index_plain() is not None:
                return "accepted"
        except KeyRequired:
            return "rejected"
        return self.verify_key_by_chunks()["status"]

    def verify_key_by_chunks(self, sample: int = 8) -> dict:
        """Key check for an encrypted container WITHOUT a usable directory index (no Indexed flag or
        DirectoryIndexSize 0), where the mount-point test of AbstractVfsReader.IsValidIndex cannot
        be applied. Positive evidence: a compressed block that decompresses to its UncompressedSize,
        or a chunk whose TOC meta hash matches its decrypted payload. A wrong key turns every block
        into noise, so decompression fails and no meta hash matches.
        status: "accepted(chunk check)" | "rejected" | "unverifiable(no directory index)"."""
        if not self.encrypted:
            return {"status": "not-encrypted", "evidence": []}
        if self.aes is None:
            return {"status": "missing", "evidence": []}
        iohash = self.header.meta_uses_iohash
        cands = sorted((ln, i) for i, (_, ln) in enumerate(self.toc.offsets) if ln)
        evidence, good, bad = [], 0, 0
        for ln, i in cands:
            if len(evidence) >= sample:
                break
            try:
                first, last, _ = self.toc.chunk_block_range(i)
                if last >= len(self.toc.blocks):
                    continue
                methods = self.chunk_methods(i)
                if not all(self.decompressor.availability(m)[0] for m in methods):
                    continue
                compressed = any(m != "None" for m in methods)
                meta = self.toc.metas[i].hash if self.toc.metas is not None else None
                meta_usable = meta is not None and meta != b"\0" * len(meta)
                if not compressed and not meta_usable:
                    continue  # an uncompressed chunk without a hash proves nothing
                data = self.read_chunk(i)
            except KeyRequired:
                raise
            except Exception as e:  # decompression of noise fails -> evidence of a wrong key
                bad += 1
                evidence.append({"index": i, "result": "decode_failed", "error": f"{type(e).__name__}"})
                continue
            meta_ok = meta_usable and any(compute_meta_hash(a, data, iohash) == meta for a in ("sha1", "blake3"))
            if not compressed and not meta_ok:
                # wrong key OR a hash algorithm we do not know: inconclusive either way
                evidence.append({"index": i, "result": "inconclusive(uncompressed, meta hash not recognised)"})
                continue
            good += 1
            evidence.append({"index": i, "result": ("decompressed" if compressed else "plain")
                             + ("+meta_hash" if meta_ok else "")})
        if bad and not good:
            status = "rejected"
        elif good and not bad:
            status = "accepted(chunk check)"
        elif good and bad:
            status = "inconsistent"
        else:
            status = "unverifiable(no directory index)"
        return {"status": status, "evidence": evidence}

    @property
    def mount_point(self) -> str:
        d = self.directory_index()
        return d.mount_point.value if d else ""

    def paths(self) -> List[Tuple[str, int]]:
        """[(full path incl. mount point, toc index)]."""
        if self._paths is None:
            d = self.directory_index()
            if d is None:
                self._paths = []
            else:
                mp = d.mount_point.value
                if mp and not mp.endswith("/"):
                    mp += "/"
                self._paths = [(mp + rel, user) for rel, user in d.walk()]
                for p, user in self._paths:
                    if user >= len(self.toc.chunk_ids):
                        raise IoStoreError(f"directory index entry {p} points at TOC index {user} "
                                           f"(only {len(self.toc.chunk_ids)} entries)")
        return self._paths

    def path_by_index(self) -> Dict[int, str]:
        try:
            return {i: p for p, i in self.paths()}
        except KeyRequired:
            return {}

    # --- block / chunk reading ---
    def block_disk_span(self, block_index: int) -> Tuple[int, int, int]:
        """(partition index, offset in partition, bytes to read) for one compression block."""
        b = self.toc.blocks[block_index]
        psize = self.header.effective_partition_size
        pidx, local = divmod(b.offset, psize)
        n = align_up(b.compressed_size, AES_BLOCK) if self.encrypted else b.compressed_size
        return pidx, local, n

    def read_block_raw(self, block_index: int, length: Optional[int] = None, allow_short: bool = False) -> bytes:
        """On-disk bytes of a block (still encrypted/compressed); aligned to 16 when encrypted.
        `length` overrides the number of bytes read (e.g. the AES-aligned span of a plain block)."""
        pidx, local, n = self.block_disk_span(block_index)
        if length is not None:
            n = length
        f = self._partition_file(pidx)
        f.seek(local)
        data = f.read(n)
        if len(data) != n and not allow_short:
            raise IoStoreError(f"block {block_index}: partition {pidx} truncated at {local}+{n}")
        return data

    def decrypt_block(self, block_index: int, raw: bytes) -> bytes:
        b = self.toc.blocks[block_index]
        if not self.encrypted:
            return raw[:b.compressed_size]
        self._need_key("chunk data")
        if self.header.encryption_method == EncryptionMethod.AES_CTR:
            return self.aes.ctr_crypt(raw, self.toc.encryption_ivs[block_index])[:b.compressed_size]
        return self.aes.ecb_decrypt(raw)[:b.compressed_size]

    def read_block(self, block_index: int) -> bytes:
        """Decrypted + decompressed block payload (UncompressedSize bytes)."""
        b = self.toc.blocks[block_index]
        data = self.decrypt_block(block_index, self.read_block_raw(block_index))
        if b.method_index == 0:
            if len(data) < b.uncompressed_size:
                raise IoStoreError(f"block {block_index}: stored {len(data)} < uncompressed {b.uncompressed_size}")
            return data[:b.uncompressed_size]
        method = self.toc.method_name(b.method_index)
        return self.decompressor.decompress(method, data, b.uncompressed_size)

    def read_chunk(self, index: int) -> bytes:
        off, ln = self.toc.offsets[index]
        if ln == 0:
            return b""
        first, last, in_block = self.toc.chunk_block_range(index)
        if last >= len(self.toc.blocks):
            raise IoStoreError(f"chunk {index}: needs block {last}, TOC has {len(self.toc.blocks)}")
        out = bytearray()
        for bi in range(first, last + 1):
            out += self.read_block(bi)
        data = bytes(out[in_block:in_block + ln])
        if len(data) != ln:
            raise IoStoreError(f"chunk {index}: got {len(data)} bytes, expected {ln}")
        return data

    def chunk_raw_blocks(self, index: int) -> List[Tuple[CompressedBlock, bytes]]:
        first, last, _ = self.toc.chunk_block_range(index)
        return [(self.toc.blocks[b], self.read_block_raw(b)) for b in range(first, last + 1)]

    def chunk_methods(self, index: int) -> List[str]:
        first, last, _ = self.toc.chunk_block_range(index)
        return sorted({self.toc.method_name(self.toc.blocks[b].method_index) for b in range(first, last + 1)})

    def chunk_info(self, index: int, path: Optional[str] = None) -> dict:
        cid = self.toc.chunk_ids[index]
        off, ln = self.toc.offsets[index]
        first, last, _ = self.toc.chunk_block_range(index)
        nblocks = max(0, last - first + 1)
        csize = sum(self.toc.blocks[b].compressed_size for b in range(first, last + 1)) \
            if last < len(self.toc.blocks) else None
        info = {"index": index, "chunk_id": cid.hex, "id": f"0x{cid.id:016X}", "chunk_index": cid.index,
                "type": cid.type, "type_name": cid.type_name(self.toc.ue5_naming),
                "type_name_ue4": cid.type_name(False), "type_name_ue5": cid.type_name(True),
                "offset": off, "size": ln, "compressed_size": csize, "blocks": nblocks,
                "first_block": first, "methods": self.chunk_methods(index) if csize is not None else []}
        if self.toc.metas is not None:
            m = self.toc.metas[index]
            info["meta_hash"] = m.hash.hex()
            info["meta_flags"] = [f.name for f in MetaFlags if f and m.flags & f] or ["None"]
        if path is not None:
            info["path"] = path
        return info

    # --- meta hash algorithm detection ---
    def detect_hash_algorithm(self, sample: int = 8, exclude: Iterable[int] = ()) -> dict:
        """Recompute SHA-1 and BLAKE3-160 for a few chunks whose blocks we can decode and compare with
        the stored meta hash. Returns {"algo": sha1|blake3|zero|unknown|none, "evidence": [...]}."""
        if self.toc.metas is None:
            return {"algo": "none", "evidence": [], "note": "TOC has no meta section"}
        excluded = set(exclude)
        candidates = []
        for i in range(len(self.toc.chunk_ids)):
            if i in excluded or self.toc.offsets[i][1] == 0:
                continue
            try:
                methods = self.chunk_methods(i)
            except Exception:
                continue
            if all(self.decompressor.availability(m)[0] for m in methods):
                candidates.append((self.toc.offsets[i][1], i))
        candidates.sort()
        evidence, verdicts = [], []
        iohash = self.header.meta_uses_iohash
        for _, i in candidates[:sample]:
            # Compare the WHOLE stored hash (32 bytes before v8: 20-byte digest + 12 zero bytes), so a
            # layout we do not know (e.g. a non-zero tail) is reported as "unknown" instead of being
            # half-matched and then rewritten in a different form.
            stored = self.toc.metas[i].hash
            try:
                data = self.read_chunk(i)
            except Exception as e:  # reported, not fatal
                evidence.append({"index": i, "error": f"{type(e).__name__}: {e}"})
                continue
            if stored == b"\0" * len(stored):
                v = "zero"
            elif compute_meta_hash("sha1", data, iohash) == stored:
                v = "sha1"
            elif compute_meta_hash("blake3", data, iohash) == stored:
                v = "blake3"
            else:
                v = "unknown"
            verdicts.append(v)
            evidence.append({"index": i, "size": len(data), "matches": v,
                             "tail12_zero": stored[20:] == b"\0" * len(stored[20:])})
        if not verdicts:
            algo = "unknown"
        elif len(set(verdicts)) == 1:
            algo = verdicts[0]
        else:
            algo = "inconsistent"
        return {"algo": algo, "evidence": evidence}

    # --- statistics ---
    def stats(self) -> dict:
        toc = self.toc
        per_method: Dict[int, dict] = {}
        for b in toc.blocks:
            s = per_method.setdefault(b.method_index, {"blocks": 0, "compressed_bytes": 0, "uncompressed_bytes": 0})
            s["blocks"] += 1
            s["compressed_bytes"] += b.compressed_size
            s["uncompressed_bytes"] += b.uncompressed_size
        methods = []
        for idx, name in enumerate(toc.method_names):
            ok, how = self.decompressor.availability(name)
            u = per_method.get(idx, {"blocks": 0, "compressed_bytes": 0, "uncompressed_bytes": 0})
            methods.append({"index": idx, "name": name, **u, "decoder_available": ok, "decoder": how})
        for idx in per_method:
            if idx >= len(toc.method_names):
                methods.append({"index": idx, "name": f"Invalid({idx})", **per_method[idx],
                                "decoder_available": False, "decoder": "method index outside the name table"})
        types: Dict[str, int] = {}
        for c in toc.chunk_ids:
            k = c.type_name(toc.ue5_naming)
            types[k] = types.get(k, 0) + 1
        mflags: Dict[str, int] = {}
        if toc.metas:
            for m in toc.metas:
                for f in MetaFlags:
                    if f and m.flags & f:
                        mflags[f.name] = mflags.get(f.name, 0) + 1
        parts = []
        for i, p in enumerate(self.partition_paths):
            parts.append({"index": i, "path": p, "exists": os.path.exists(p),
                          "size": os.path.getsize(p) if os.path.exists(p) else None})
        return {"compression_methods": methods, "chunk_types": types,
                "chunk_type_naming": "UE5 (EIoChunkType5)" if toc.ue5_naming else "UE4 (EIoChunkType)",
                "meta_flags": mflags, "partitions": parts,
                "total_uncompressed": sum(ln for _, ln in toc.offsets)}


# ---------------------------------------------------------------------------------------------
# Output-safety helpers shared by every writing command
# ---------------------------------------------------------------------------------------------

def prepare_out_dir(out_dir: str, inputs: Sequence[str]) -> str:
    """Refuse to write into an input's directory or over an input; require a new or empty dir."""
    out = os.path.abspath(out_dir)
    for p in inputs:
        if os.path.realpath(os.path.dirname(os.path.abspath(p))) == os.path.realpath(out):
            raise IoStoreError(f"--out-dir {out} is the directory of input {p}; refusing (inputs are never overwritten)")
    if os.path.exists(out):
        if not os.path.isdir(out):
            raise IoStoreError(f"--out-dir {out} exists and is not a directory")
        if os.listdir(out):
            raise IoStoreError(f"--out-dir {out} is not empty; refusing to overwrite (use a new directory)")
    os.makedirs(out, exist_ok=True)
    return out


def assert_not_input(path: str, inputs: Sequence[str]) -> None:
    for p in inputs:
        if os.path.exists(path) and os.path.exists(p) and os.path.samefile(path, p):
            raise IoStoreError(f"refusing to overwrite input {p}")
        if os.path.abspath(path) == os.path.abspath(p):
            raise IoStoreError(f"refusing to overwrite input {p}")


def copy_file_new(src: str, dst: str, inputs: Sequence[str] = ()) -> None:
    """Copy src to a NEW file dst (exclusive create: never truncates an existing file)."""
    assert_not_input(dst, inputs)
    with open(src, "rb") as fi, open(dst, "xb") as fo:
        shutil.copyfileobj(fi, fo, 16 << 20)


def file_record(path: str) -> dict:
    return {"path": os.path.abspath(path), "size": os.path.getsize(path), "sha256": sha256_file(path)}


def write_json_new(path: str, obj, inputs: Sequence[str] = ()) -> None:
    assert_not_input(path, inputs)
    if os.path.exists(path):
        raise IoStoreError(f"{path} already exists; refusing to overwrite")
    with open(path, "x", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------------------------
# Selection helpers
# ---------------------------------------------------------------------------------------------

def strip_mount(path: str) -> str:
    p = path
    while p.startswith("../"):
        p = p[3:]
    return p.lstrip("/")


def resolve_selector(c: IoStoreContainer, selector: str) -> int:
    """Selector forms: index:N | id:<24 hex> | <24 hex> | path:<path> | <path> (with or without the
    mount point). Exactly one TOC entry must match."""
    s = selector.strip()
    if s.startswith("index:"):
        try:
            i = int(s[6:], 0)
        except ValueError:
            raise IoStoreError(f"selector {selector}: index:N needs a TOC index (decimal or 0x hex)") from None
        if not 0 <= i < len(c.toc.chunk_ids):
            raise IoStoreError(f"selector {selector}: index out of range")
        return i
    hexpart = s[3:] if s.startswith("id:") else s
    if s.startswith("id:") or (len(hexpart) == 24 and all(ch in "0123456789abcdefABCDEF" for ch in hexpart)):
        cid = ChunkId.from_hex(hexpart)
        hits = [i for i, x in enumerate(c.toc.chunk_ids) if x == cid]
        if len(hits) != 1:
            raise IoStoreError(f"selector {selector}: {len(hits)} TOC entries have this chunk id")
        return hits[0]
    p = s[5:] if s.startswith("path:") else s
    hits = sorted({i for full, i in c.paths() if full == p or strip_mount(full) == strip_mount(p)})
    if len(hits) != 1:
        raise IoStoreError(f"selector {selector}: {len(hits)} paths match")
    return hits[0]


def select_indices(c: IoStoreContainer, globs: Sequence[str], ids: Sequence[str], indices: Sequence[int],
                   select_all: bool, ignore_case: bool = False) -> List[int]:
    if select_all:
        return list(range(len(c.toc.chunk_ids)))
    chosen = set()
    if globs:
        for full, i in c.paths():
            for g in globs:
                a, b = (full.lower(), g.lower()) if ignore_case else (full, g)
                if fnmatch.fnmatchcase(a, b) or fnmatch.fnmatchcase(strip_mount(a), strip_mount(b)):
                    chosen.add(i)
    for h in ids:
        cid = ChunkId.from_hex(h)
        chosen.update(i for i, x in enumerate(c.toc.chunk_ids) if x == cid)
    for i in indices:
        if not 0 <= i < len(c.toc.chunk_ids):
            raise IoStoreError(f"index {i} out of range")
        chosen.add(i)
    return sorted(chosen)


# ---------------------------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------------------------

def _disk_block(payload: bytes, aes: Optional[AesCipher], iv: Optional[bytes]) -> bytes:
    """Pad an (uncompressed) block to the 16-byte AES alignment and encrypt it if needed. Readers
    (IoStoreReader.Read) always fetch Align(CompressedSize, 16) bytes, so the padding must exist on disk;
    its content is never decoded. We repeat the start of the block (what UE's pak writer is reported to
    do; not verifiable here, and irrelevant to readers)."""
    padded_len = align_up(len(payload), AES_BLOCK)
    if padded_len != len(payload):
        pad = bytearray()
        while len(pad) < padded_len - len(payload):
            pad += payload or b"\0"
        payload = payload + bytes(pad[:padded_len - len(payload)])
    if aes is None:
        return payload
    if iv is not None:
        return aes.ctr_crypt(payload, iv)
    return aes.ecb_encrypt(payload)


class _PartitionWriter:
    """Appends bytes to partition files honouring PartitionSize (blocks never straddle partitions)."""

    def __init__(self, utoc_out: str, psize: int, start_partition: int, start_offset: int, allow_new: bool):
        self.utoc_out = utoc_out
        self.psize = psize
        self.pidx = start_partition
        self.pos = start_offset
        self.allow_new = allow_new
        self.created: List[int] = []
        self._f = None

    def _file(self):
        if self._f is None:
            p = partition_path(self.utoc_out, self.pidx)
            self._f = open(p, "r+b" if os.path.exists(p) else "x+b")
        return self._f

    def _next_partition(self) -> None:
        if not self.allow_new:
            raise IoStoreError("data does not fit in the partition size and this TOC version has no partitions")
        self.close()
        self.pidx += 1
        self.created.append(self.pidx)
        self.pos = 0

    def reserve(self, total: int, align: int) -> None:
        """Make sure `total` bytes starting at the next `align` boundary fit in ONE partition (used to
        keep a memory-mapped chunk contiguous: UE maps it as a single file range)."""
        if total > self.psize:
            raise IoStoreError(f"{total} bytes cannot be kept in one partition of PartitionSize {self.psize}")
        if align_up(self.pos, align) + total > self.psize:
            self._next_partition()

    def write(self, data: bytes, align: int) -> int:
        """Write `data` at the next `align`-aligned position; return the global offset."""
        if len(data) > self.psize:
            raise IoStoreError(f"block of {len(data)} bytes exceeds PartitionSize {self.psize}")
        start = align_up(self.pos, align)
        if start + len(data) > self.psize:
            self._next_partition()
            start = 0
        f = self._file()
        f.seek(0, os.SEEK_END)
        end = f.tell()
        if end < start:
            f.write(b"\0" * (start - end))
        f.seek(start)
        f.write(data)
        self.pos = start + len(data)
        return self.pidx * self.psize + start

    def close(self):
        if self._f is not None:
            self._f.close()
            self._f = None


def _hash_algo_for(c: IoStoreContainer, requested: str, replaced: Iterable[int]) -> Tuple[str, dict]:
    if c.toc.metas is None:
        return "none", {"note": "no TOC meta section; nothing to update"}
    if requested != "auto":
        return requested, {"source": "user (--hash-algo)"}
    det = c.detect_hash_algorithm(exclude=replaced)
    if c.header.meta_uses_iohash:
        # UE defines FIoHash as BLAKE3 truncated to 20 bytes (per retoc; Epic source not checkable here).
        # A container that stores all-zero hashes keeps that convention; otherwise BLAKE3 is written.
        if det["algo"] == "zero":
            return "zero", {"source": "measured: unmodified chunks store all-zero FIoHash", "detection": det}
        info = {"source": "TOC version >= ReplaceIoChunkHashWithIoHash (FIoHash = BLAKE3-160)", "detection": det}
        if det["algo"] not in ("blake3", "unknown"):
            info["note"] = f"stored hashes of unmodified chunks look like {det['algo']!r}, not BLAKE3"
        return "blake3", info
    if det["algo"] in ("sha1", "blake3", "zero"):
        return det["algo"], {"source": "measured on unmodified chunks", "detection": det}
    raise IoStoreError(f"cannot determine the TOC meta hash algorithm automatically (result: {det['algo']}); "
                       f"rerun with --hash-algo sha1|blake3|zero. Evidence: {json.dumps(det['evidence'])}")


def observed_mmap_alignment(toc: TocResource) -> Tuple[int, dict]:
    """Alignment to use for MemoryMapped chunks: the platform value we assume for iOS (16 KiB page),
    raised to the coarsest power of two that ALL existing memory-mapped chunks of the input already
    satisfy in their partition file (capped at 64 KiB), so a rebuilt/appended chunk is never less
    aligned than the cooker made them. Returns (alignment, evidence)."""
    if toc.metas is None:
        return MEMORY_MAPPED_ALIGN, {"memory_mapped_chunks": 0}
    psize = toc.header.effective_partition_size
    lowbits = []
    n = 0
    for i, m in enumerate(toc.metas):
        if not m.flags & MetaFlags.MemoryMapped or not toc.offsets[i][1]:
            continue
        first, last, _ = toc.chunk_block_range(i)
        if last >= len(toc.blocks):
            continue
        n += 1
        local = toc.blocks[first].offset % psize
        if local:
            lowbits.append(local & -local)
    obs = min(lowbits) if lowbits else None
    a = MEMORY_MAPPED_ALIGN if obs is None else max(MEMORY_MAPPED_ALIGN, min(obs, 65536))
    return a, {"memory_mapped_chunks": n, "observed_min_alignment": obs, "used": a}


def _writer_preflight(c: IoStoreContainer, unsign: bool, allow_unverified_key: bool, warnings: List[str],
                      what: str) -> dict:
    """Checks every writing command (patch, add, clone-package) runs before touching anything:
    signatures, OnDemand, TOC version, AES key proven, TOC re-serialises byte-identically.
    Returns the key-check record for the manifest."""
    h_in = c.header
    flags = h_in.flags
    if flags & ContainerFlags.Signed and not unsign:
        raise IoStoreError("container is Signed: its RSA TOC/block signatures cannot be regenerated without the "
                           "publisher's private key. Refusing. Pass --unsign to clear the Signed flag and drop the "
                           "signature section (the game may then reject the container if it enforces signatures).")
    if flags & ContainerFlags.OnDemand:
        raise IoStoreError("OnDemand containers are not supported by the writer")
    if h_in.version > LATEST_KNOWN_VERSION:
        raise IoStoreError(f"TOC version {h_in.version} is newer than this tool knows ({int(LATEST_KNOWN_VERSION)})")
    key_check: dict = {"status": "not-encrypted"}
    if c.encrypted:
        c._need_key(what)
        if c.directory_index_plain() is not None:  # raises KeyRequired on a wrong key
            key_check = {"status": "accepted", "method": "directory index mount point (IsValidIndex)"}
        else:
            # No directory index to test the key on. New blocks are encrypted with this key, so a wrong
            # key would silently produce a container the game cannot read: demand positive evidence.
            key_check = c.verify_key_by_chunks()
            key_check["method"] = "decode/meta-hash of unchanged chunks (no directory index)"
            if key_check["status"] in ("rejected", "inconsistent"):
                raise KeyRequired(f"AES key check on chunk data: {key_check['status']} "
                                  f"(evidence: {json.dumps(key_check['evidence'][:4])})")
            if not key_check["status"].startswith("accepted"):
                if not allow_unverified_key:
                    raise IoStoreError("the container has no directory index and no chunk could prove the AES key "
                                       "(nothing compressed with an available decoder, no recognised meta hash). "
                                       "Refusing to encrypt new blocks with an unverified key; pass "
                                       "--allow-unverified-key only if you are sure the key is right")
                warnings.append("AES key could not be verified (no directory index); written with "
                                "--allow-unverified-key")
    if c.toc.serialize() != c.toc_bytes:
        raise IoStoreError("input TOC does not re-serialise byte-identically; refusing to write from it")
    return key_check


def patch_container(c: IoStoreContainer, replacements: Dict[int, bytes], out_dir: str, rebuild: bool = False,
                    unsign: bool = False, hash_algo: str = "auto", align: int = AES_BLOCK,
                    verify: bool = False, extra_inputs: Sequence[str] = (), argv: Optional[List[str]] = None,
                    key_source: str = "none", allow_unverified_key: bool = False) -> dict:
    """Write a NEW container (same base name) into out_dir with the given chunk payloads replaced.

    Append mode (default): partition files are copied byte for byte, replaced chunks are appended to
    the last partition as uncompressed blocks (method index 0), their FIoOffsetAndLength points at the
    new block range, TOC meta hash/flags are updated; everything else (chunk ids, perfect hash,
    directory index, other blocks) is untouched, so the old payload stays as unreferenced bytes.
    Rebuild mode: every partition is rewritten; unchanged chunks' on-disk blocks are copied verbatim
    (still compressed/encrypted), replaced chunks are written uncompressed, orphan blocks are dropped.
    """
    toc_in = c.toc
    h_in = c.header
    warnings: List[str] = []
    if not replacements:
        raise IoStoreError("nothing to replace")
    if align < 1 or align % AES_BLOCK:
        raise IoStoreError("--align must be a positive multiple of 16")
    key_check = _writer_preflight(c, unsign, allow_unverified_key, warnings, "patching")
    layout = toc_in.layout_report()
    if rebuild and (layout["shared_blocks_count"] or layout["chunks_misaligned_count"] or layout["chunks_out_of_range_count"]):
        raise IoStoreError(f"--rebuild needs block-aligned, non-overlapping chunks; layout: {json.dumps(layout)}")
    for i in replacements:
        if not 0 <= i < len(toc_in.chunk_ids):
            raise IoStoreError(f"replacement index {i} out of range")

    algo, algo_info = _hash_algo_for(c, hash_algo, replacements.keys())
    inputs = c.input_files() + list(extra_inputs)
    out = prepare_out_dir(out_dir, inputs)
    utoc_out = os.path.join(out, os.path.basename(c.utoc_path))
    for p in [utoc_out] + [partition_path(utoc_out, i) for i in range(h_in.effective_partition_count + 8)]:
        assert_not_input(p, inputs)
    # input hashes are taken BEFORE anything is written; --verify re-hashes them afterwards
    input_records = [file_record(p) for p in inputs]

    toc = toc_in.clone()
    h = toc.header
    bs = h.compression_block_size
    psize = h.effective_partition_size
    allow_new = h.version >= TocVersion.PartitionSize
    ctr = h.encryption_method == EncryptionMethod.AES_CTR
    aes = c.aes if c.encrypted else None
    dir_iv = toc.encryption_ivs[-1:] if ctr and (h.flags & ContainerFlags.Indexed) else []
    block_ivs = toc.encryption_ivs[:len(toc.blocks)] if ctr else []
    path_of = c.path_by_index()
    changes = []
    t0 = time.time()
    mm_align, mm_info = observed_mmap_alignment(toc_in)

    def is_mmapped(i: int) -> bool:
        return toc_in.metas is not None and bool(toc_in.metas[i].flags & MetaFlags.MemoryMapped)

    def chunk_align(i: int) -> int:
        return max(align, mm_align) if is_mmapped(i) else align

    def write_new_chunk(i: int, data: bytes, pw: _PartitionWriter, new_blocks: List[CompressedBlock],
                        new_ivs: List[bytes]) -> Tuple[int, int]:
        first_block = len(new_blocks)
        pieces = [data[s:s + bs] for s in range(0, len(data), bs)]
        if is_mmapped(i) and pieces:
            # UE maps a memory-mapped chunk as ONE contiguous file range: keep it in one partition
            pw.reserve(sum(align_up(len(p), AES_BLOCK) for p in pieces), chunk_align(i))
        for k, piece in enumerate(pieces):
            iv = os.urandom(12) if ctr else None
            disk = _disk_block(piece, aes, iv)
            goff = pw.write(disk, chunk_align(i) if k == 0 else AES_BLOCK)
            new_blocks.append(CompressedBlock(goff, len(piece), len(piece), 0))
            if ctr:
                new_ivs.append(iv)
        return first_block, len(new_blocks)

    if not rebuild:
        # copy all partitions verbatim, then append to the last one
        for i, p in enumerate(c.partition_paths):
            if not os.path.exists(p):
                raise IoStoreError(f"missing input partition {p}")
            copy_file_new(p, partition_path(utoc_out, i), inputs)
        last = h.effective_partition_count - 1
        # Start after the file end AND after the 16-byte-aligned span of every block in that partition
        # (readers fetch Align(CompressedSize, 16) bytes, even when the input file stops earlier).
        start = os.path.getsize(partition_path(utoc_out, last))
        for b in toc_in.blocks:
            pidx, local = divmod(b.offset, psize)
            if pidx == last:
                start = max(start, local + align_up(b.compressed_size, AES_BLOCK))
        pw = _PartitionWriter(utoc_out, psize, last, start, allow_new)
        blocks = list(toc.blocks)
        new_ivs = list(block_ivs)
        try:
            for i in sorted(replacements):
                data = replacements[i]
                fb, lb = write_new_chunk(i, data, pw, blocks, new_ivs)
                toc.offsets[i] = (fb * bs, len(data))
                changes.append((i, fb, lb))
        finally:
            pw.close()
        toc.blocks = blocks
        if ctr:
            toc.encryption_ivs = new_ivs + dir_iv
        if pw.created:
            h.partition_count = max(h.partition_count, pw.pidx + 1)
            warnings.append(f"created new partition file(s) {pw.created}")
        # blocks that nothing references any more (a block still used by another chunk is not orphaned)
        orphaned = toc.layout_report()["orphan_blocks"] - layout["orphan_blocks"]
        mode_info = {"mode": "append", "orphaned_blocks": orphaned,
                     "orphan_blocks_total": layout["orphan_blocks"] + orphaned}
    else:
        order = sorted(range(len(toc.chunk_ids)),
                       key=lambda i: (toc_in.blocks[toc_in.chunk_block_range(i)[0]].offset
                                      if toc_in.offsets[i][1] and toc_in.chunk_block_range(i)[0] < len(toc_in.blocks)
                                      else -1, i))
        pw = _PartitionWriter(utoc_out, psize, 0, 0, allow_new)
        open(partition_path(utoc_out, 0), "xb").close()
        blocks: List[CompressedBlock] = []
        new_ivs: List[bytes] = []
        try:
            for i in order:
                if i in replacements:
                    data = replacements[i]
                    fb, lb = write_new_chunk(i, data, pw, blocks, new_ivs)
                    toc.offsets[i] = (fb * bs, len(data))
                    changes.append((i, fb, lb))
                    continue
                off, ln = toc_in.offsets[i]
                if ln == 0:
                    toc.offsets[i] = (len(blocks) * bs, 0)
                    continue
                first, last, in_block = toc_in.chunk_block_range(i)
                fb = len(blocks)
                if is_mmapped(i):
                    pw.reserve(sum(align_up(toc_in.blocks[bi].compressed_size, AES_BLOCK)
                                   for bi in range(first, last + 1)), chunk_align(i))
                for k, bi in enumerate(range(first, last + 1)):
                    b = toc_in.blocks[bi]
                    # the whole 16-byte-aligned on-disk span, padding included, copied verbatim
                    span = align_up(b.compressed_size, AES_BLOCK)
                    raw = c.read_block_raw(bi, span, allow_short=not c.encrypted)
                    if len(raw) < b.compressed_size:
                        raise IoStoreError(f"block {bi}: partition truncated (need {b.compressed_size} bytes)")
                    raw = raw + b"\0" * (span - len(raw))
                    goff = pw.write(raw, chunk_align(i) if k == 0 else AES_BLOCK)
                    blocks.append(CompressedBlock(goff, b.compressed_size, b.uncompressed_size, b.method_index))
                    if ctr:
                        new_ivs.append(toc_in.encryption_ivs[bi])
                toc.offsets[i] = (fb * bs + in_block, ln)
        finally:
            pw.close()
        toc.blocks = blocks
        if ctr:
            toc.encryption_ivs = new_ivs + dir_iv
        if h.version >= TocVersion.PartitionSize:
            h.partition_count = pw.pidx + 1
        mode_info = {"mode": "rebuild", "blocks_before": len(toc_in.blocks), "blocks_after": len(blocks),
                     "orphan_blocks_dropped": layout["orphan_blocks"]}
    mode_info["memory_mapped_alignment"] = mm_info

    # TOC meta for replaced chunks
    if toc.metas is not None:
        for i in replacements:
            m = toc.metas[i]
            new_flags = int(m.flags) & ~int(MetaFlags.Compressed) & 0xFF
            toc.metas[i] = TocEntryMeta(compute_meta_hash(algo, replacements[i], h.meta_uses_iohash)
                                        if algo != "none" else m.hash, new_flags, m.pad)
    if toc.on_demand_block_hashes is not None:
        raise IoStoreError("OnDemand meta present; not supported by the writer")

    # signatures
    if toc.signatures is not None:
        toc.signatures = None
        warnings.append("Signed flag cleared and signature section removed (--unsign); a runtime that enforces "
                        "IoStore signatures will reject this container")
    toc.sync_header_counts()
    toc_bytes = toc.serialize()
    with open(utoc_out, "xb") as f:
        f.write(toc_bytes)

    # change records
    change_records = []
    for i, fb, lb in changes:
        data = replacements[i]
        cid = toc.chunk_ids[i]
        ofb, olb, _ = toc_in.chunk_block_range(i)
        rec = {"index": i, "chunk_id": cid.hex, "type_name": cid.type_name(toc.ue5_naming), "path": path_of.get(i),
               "old_size": toc_in.offsets[i][1], "new_size": len(data), "new_sha256": sha256_bytes(data),
               "old_offset": toc_in.offsets[i][0], "new_offset": toc.offsets[i][0],
               "old_blocks": [ofb, olb], "new_blocks": [fb, lb - 1] if lb > fb else [],
               "new_block_method": "None (index 0, uncompressed)"}
        if toc_in.metas is not None:
            rec["old_meta_hash"] = toc_in.metas[i].hash.hex()
            rec["new_meta_hash"] = toc.metas[i].hash.hex()
            rec["old_meta_flags"] = toc_in.metas[i].flags
            rec["new_meta_flags"] = toc.metas[i].flags
        if cid.type_name(toc.ue5_naming) == "ExportBundleData" and len(data) != toc_in.offsets[i][1]:
            warnings.append(f"chunk {i} ({cid.hex}) is package data and changed size; stock UE4.26 FPackageStoreEntry "
                            f"(container header) stores ExportBundlesSize, eFootball's layout (CUE4Parse "
                            f"GAME_eFootball) does not - check the container header on real data")
        change_records.append(rec)
    old_hj, new_hj = h_in.to_json(), h.to_json()
    header_changes = {k: {"old": v, "new": new_hj[k]} for k, v in old_hj.items() if new_hj[k] != v}

    out_files = [utoc_out] + [partition_path(utoc_out, i) for i in range(h.effective_partition_count)]
    manifest = {
        "tool": TOOL_NAME, "tool_version": TOOL_VERSION, "operation": "patch", "created_utc": now_iso(),
        "argv": argv, "aes_key_source": key_source, "aes_key_check": key_check,
        "inputs": input_records,
        "outputs": [file_record(p) for p in out_files if os.path.exists(p)],
        "container": {"name": c.name, "toc_version": h.version, "toc_version_name": h.version_name,
                      "encrypted": c.encrypted, "encryption_method": h.encryption_method.name},
        "mode": mode_info, "meta_hash_algorithm": algo, "meta_hash_algorithm_info": algo_info,
        "header_changes": header_changes, "replaced_chunks": change_records,
        "unchanged": ["chunk ids and order", "perfect hash seeds/overflow", "directory index bytes",
                      "compression method table", "container id / key guid"],
        "warnings": warnings, "elapsed_s": round(time.time() - t0, 3),
    }
    if verify:
        manifest["verify"] = verify_patch(c, utoc_out, replacements, algo, input_records)
    write_json_new(os.path.join(out, c.name + ".manifest.json"), manifest, inputs)
    if verify and not manifest["verify"]["ok"]:
        raise IoStoreError(f"verification FAILED: {json.dumps(manifest['verify']['failures'][:5])}")
    return manifest


def disk_span_report(c: IoStoreContainer) -> dict:
    """Every block referenced by a chunk occupies [offset, offset + Align(CompressedSize, 16)) in its
    partition file (IoStoreReader.Read always reads the aligned size). Check that these spans lie
    inside their partition file, do not cross PartitionSize, and do not overlap each other."""
    psize = c.header.effective_partition_size
    sizes: Dict[int, Optional[int]] = {}
    spans: Dict[int, List[Tuple[int, int, int]]] = {}
    referenced = set()
    for i, (_, ln) in enumerate(c.toc.offsets):
        if not ln:
            continue
        first, last, _ = c.toc.chunk_block_range(i)
        referenced.update(range(first, min(last, len(c.toc.blocks) - 1) + 1))
    beyond_eof, straddle, overlaps = [], [], []
    for bi in sorted(referenced):
        b = c.toc.blocks[bi]
        pidx, local = divmod(b.offset, psize)
        n = align_up(b.compressed_size, AES_BLOCK)
        if pidx not in sizes:
            p = partition_path(c.utoc_path, pidx)
            sizes[pidx] = os.path.getsize(p) if os.path.exists(p) else None
        if sizes[pidx] is None or local + n > sizes[pidx]:
            beyond_eof.append(bi)
        if psize != MASK64 and local + n > psize:
            straddle.append(bi)
        if n:
            spans.setdefault(pidx, []).append((local, local + n, bi))
    for pidx, lst in spans.items():
        lst.sort()
        for (s0, e0, b0), (s1, e1, b1) in zip(lst, lst[1:]):
            if s1 < e0:
                overlaps.append((b0, b1))
    return {"referenced_blocks": len(referenced), "beyond_partition_eof": len(beyond_eof),
            "beyond_partition_eof_first": beyond_eof[:10], "crossing_partition_size": len(straddle),
            "overlapping_block_pairs": len(overlaps), "overlapping_first": overlaps[:10]}


def verify_patch(c_in: IoStoreContainer, utoc_out: str, replacements: Dict[int, bytes], algo: str,
                 input_records: Optional[List[dict]] = None) -> dict:
    """Re-read the written container with the reader and check it against the input."""
    failures: List[str] = []
    checks: Dict[str, object] = {}
    if input_records is not None:
        now = [file_record(r["path"]) if os.path.exists(r["path"]) else None for r in input_records]
        same = [a == b for a, b in zip(input_records, now)]
        checks["inputs_unchanged"] = all(same)
        if not all(same):
            failures.append(f"INPUT FILES CHANGED: {[r['path'] for r, ok in zip(input_records, same) if not ok]}")
    with IoStoreContainer(utoc_out, c_in.aes_key, c_in.decompressor) as o:
        checks["toc_reserialize_identical"] = o.toc.serialize() == o.toc_bytes
        if not checks["toc_reserialize_identical"]:
            failures.append("output TOC does not re-serialise byte-identically")
        hi, ho = c_in.header, o.header
        fixed = {"version": (hi.version, ho.version), "compression_block_size": (hi.compression_block_size,
                 ho.compression_block_size), "container_id": (hi.container_id, ho.container_id),
                 "encryption_key_guid": (hi.encryption_key_guid, ho.encryption_key_guid),
                 "compression_methods": (c_in.toc.method_names_raw, o.toc.method_names_raw),
                 "perfect_hash": ((c_in.toc.perfect_hash_seeds, c_in.toc.chunks_without_perfect_hash),
                                  (o.toc.perfect_hash_seeds, o.toc.chunks_without_perfect_hash))}
        changed_fixed = [k for k, (a, b) in fixed.items() if a != b]
        checks["fixed_header_fields_unchanged"] = not changed_fixed
        if changed_fixed:
            failures.append(f"fields that must not change did: {changed_fixed}")
        same_ids = [x.raw for x in o.toc.chunk_ids] == [x.raw for x in c_in.toc.chunk_ids]
        checks["chunk_ids_unchanged"] = same_ids
        if not same_ids:
            failures.append("chunk id table changed")
        checks["directory_index_bytes_unchanged"] = o.toc.directory_index_raw == c_in.toc.directory_index_raw
        if not checks["directory_index_bytes_unchanged"]:
            failures.append("directory index bytes changed")
        if o.toc.directory_index_raw and (o.header.flags & ContainerFlags.Indexed):
            try:
                checks["paths_unchanged"] = o.paths() == c_in.paths()
                if not checks["paths_unchanged"]:
                    failures.append("path map changed")
            except KeyRequired as e:
                failures.append(str(e))
        ph = o.toc.validate_perfect_hash()
        checks["perfect_hash"] = {"present": ph["present"], "ok": ph.get("ok", True)}
        if ph["present"] and not ph.get("ok"):
            failures.append(f"perfect hash lookup failures: {ph['failures'][:3]}")
        layout = o.toc.layout_report()
        checks["layout"] = {k: layout[k] for k in ("shared_blocks_count", "chunks_misaligned_count",
                                                   "chunks_out_of_range_count", "orphan_blocks")}
        lin = c_in.toc.layout_report()
        if layout["chunks_out_of_range_count"] or layout["shared_blocks_count"] > lin["shared_blocks_count"]:
            failures.append(f"layout problem: {checks['layout']}")
        # partition files: every one the header declares exists, no stray extra one
        np_ = o.header.effective_partition_count
        missing = [p for p in o.partition_paths if not os.path.exists(p)]
        stray = os.path.exists(partition_path(o.utoc_path, np_))
        checks["partition_files"] = {"declared": np_, "missing": len(missing), "stray_extra": stray}
        if missing or stray:
            failures.append(f"partition files inconsistent with PartitionCount: {checks['partition_files']}")
        spans = disk_span_report(o)
        spans_in = disk_span_report(c_in)
        checks["disk_spans"] = spans
        # relative to the input: a container written by another tool may already have unpadded or
        # unaligned blocks; what we must never do is ADD such problems
        worse = [k for k in ("beyond_partition_eof", "crossing_partition_size", "overlapping_block_pairs")
                 if spans[k] > spans_in[k]]
        if worse:
            failures.append(f"block disk spans got worse than the input ({worse}): {spans}")
        replaced_ok = {}
        for i, data in replacements.items():
            try:
                got = o.read_chunk(i)
                ok = got == data
                if o.toc.metas is not None and algo in HASH_ALGOS:
                    ok = ok and o.toc.metas[i].hash == compute_meta_hash(algo, data, o.header.meta_uses_iohash)
                replaced_ok[i] = ok
                if not ok:
                    failures.append(f"replaced chunk {i} reads back wrong")
            except Exception as e:
                replaced_ok[i] = False
                failures.append(f"replaced chunk {i}: {type(e).__name__}: {e}")
        checks["replaced_chunks_read_back"] = {str(k): v for k, v in replaced_ok.items()}
        ctr_in = c_in.header.encryption_method == EncryptionMethod.AES_CTR
        ctr_out = o.header.encryption_method == EncryptionMethod.AES_CTR
        bs = c_in.header.compression_block_size
        raw_same = deep_same = deep_skipped = 0
        for i in range(len(c_in.toc.chunk_ids)):
            if i in replacements:
                continue
            if o.toc.offsets[i][1] != c_in.toc.offsets[i][1]:
                failures.append(f"chunk {i}: length changed")
                continue
            if o.toc.offsets[i][0] % bs != c_in.toc.offsets[i][0] % bs:
                failures.append(f"chunk {i}: offset inside the first block changed")
                continue
            if o.toc.metas is not None and c_in.toc.metas is not None and o.toc.metas[i] != c_in.toc.metas[i]:
                failures.append(f"chunk {i}: meta changed")
            try:
                fa, la, _ = c_in.toc.chunk_block_range(i)
                fo, lo, _ = o.toc.chunk_block_range(i)
                a = [(x.compressed_size, x.uncompressed_size, x.method_index, r,
                      c_in.toc.encryption_ivs[fa + k] if ctr_in else None)
                     for k, (x, r) in enumerate(c_in.chunk_raw_blocks(i))]
                b = [(x.compressed_size, x.uncompressed_size, x.method_index, r,
                      o.toc.encryption_ivs[fo + k] if ctr_out else None)
                     for k, (x, r) in enumerate(o.chunk_raw_blocks(i))]
            except Exception as e:
                failures.append(f"chunk {i}: raw blocks unreadable: {type(e).__name__}: {e}")
                continue
            if a != b:
                failures.append(f"chunk {i}: on-disk blocks (or their AES-CTR IVs) differ")
                continue
            raw_same += 1
            methods = c_in.chunk_methods(i)
            if all(c_in.decompressor.availability(m)[0] for m in methods):
                try:
                    if sha256_bytes(o.read_chunk(i)) != sha256_bytes(c_in.read_chunk(i)):
                        failures.append(f"chunk {i}: decompressed payload differs")
                    else:
                        deep_same += 1
                except Exception as e:  # e.g. a wrong key: decompression of noise fails
                    failures.append(f"chunk {i}: cannot be decoded: {type(e).__name__}: {e}")
            else:
                deep_skipped += 1
        checks["unchanged_chunks_raw_blocks_identical"] = raw_same
        checks["unchanged_chunks_payload_identical"] = deep_same
        checks["unchanged_chunks_payload_check_skipped_no_decoder"] = deep_skipped
    return {"ok": not failures, "checks": checks, "failures": failures}


# ---------------------------------------------------------------------------------------------
# Higher level operations used by the CLI (and tests)
# ---------------------------------------------------------------------------------------------

def hash_all(c: IoStoreContainer) -> dict:
    paths = c.path_by_index()
    entries = []
    errors = []
    for i, cid in enumerate(c.toc.chunk_ids):
        e = {"index": i, "chunk_id": cid.hex, "type_name": cid.type_name(c.toc.ue5_naming),
             "size": c.toc.offsets[i][1], "path": paths.get(i)}
        try:
            e["sha256"] = sha256_bytes(c.read_chunk(i))
        except Exception as ex:
            e["error"] = str(ex)
            errors.append(i)
        entries.append(e)
    return {"tool": TOOL_NAME, "tool_version": TOOL_VERSION, "operation": "hash-all",
            "container": c.utoc_path, "utoc_sha256": sha256_bytes(c.toc_bytes),
            "toc_version": c.header.version, "chunk_count": len(entries), "errors": len(errors),
            "chunks": entries}


def _load_hash_table(path: str, aes_key: Optional[bytes], dec: Decompressor) -> dict:
    if path.lower().endswith(".json"):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    with IoStoreContainer(path, aes_key, dec) as c:
        return hash_all(c)


def diff_tables(a: dict, b: dict) -> dict:
    """Compare two hash-all tables keyed by (chunk id, occurrence)."""
    def keyed(t):
        out, seen = {}, {}
        for e in t["chunks"]:
            n = seen.get(e["chunk_id"], 0)
            seen[e["chunk_id"]] = n + 1
            out[(e["chunk_id"], n)] = e
        return out
    ka, kb = keyed(a), keyed(b)
    changed, added, removed, unreadable = [], [], [], []
    same = 0
    for k, ea in ka.items():
        eb = kb.get(k)
        if eb is None:
            removed.append({"chunk_id": k[0], "path": ea.get("path")})
            continue
        if "sha256" not in ea or "sha256" not in eb:
            unreadable.append({"chunk_id": k[0], "path": ea.get("path") or eb.get("path")})
            continue
        if ea["sha256"] != eb["sha256"]:
            changed.append({"chunk_id": k[0], "path": ea.get("path") or eb.get("path"),
                            "index_a": ea["index"], "index_b": eb["index"],
                            "size_a": ea["size"], "size_b": eb["size"],
                            "sha256_a": ea["sha256"], "sha256_b": eb["sha256"]})
        else:
            same += 1
    for k, eb in kb.items():
        if k not in ka:
            added.append({"chunk_id": k[0], "path": eb.get("path")})
    return {"identical": not (changed or added or removed or unreadable), "unchanged": same,
            "changed": changed, "added": added, "removed": removed, "unreadable": unreadable}


def extract(c: IoStoreContainer, indices: Sequence[int], out_dir: str, verify: bool = False,
            argv: Optional[List[str]] = None, key_source: str = "none") -> dict:
    inputs = c.input_files()
    out = prepare_out_dir(out_dir, inputs)
    paths = c.path_by_index()
    records: List[dict] = []
    failed: List[dict] = []
    for i in indices:
        cid = c.toc.chunk_ids[i]
        p = paths.get(i)
        if p:
            parts = [x for x in strip_mount(p).split("/") if x not in ("", ".", "..")]
            rel = os.path.join(*parts) if parts else f"_unnamed/{i:06d}.bin"
        else:
            rel = os.path.join("_chunks", f"{i:06d}_{cid.hex}.{cid.type_name(c.toc.ue5_naming)}.bin")
        dst = os.path.join(out, rel)
        if not os.path.realpath(dst).startswith(os.path.realpath(out) + os.sep):
            raise IoStoreError(f"refusing to write outside {out}: {rel}")
        try:
            data = c.read_chunk(i)
        except Exception as e:  # e.g. Oodle block without decoder, corrupt block: record, keep going
            failed.append({"index": i, "chunk_id": cid.hex, "path": p, "error": f"{type(e).__name__}: {e}"})
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "xb") as f:
                f.write(data)
        except OSError as e:  # e.g. two TOC paths that collide on a case-insensitive file system
            failed.append({"index": i, "chunk_id": cid.hex, "path": p, "error": f"cannot create {rel}: {e}"})
            continue
        records.append({"index": i, "chunk_id": cid.hex, "path": p, "file": rel, "size": len(data),
                        "sha256": sha256_bytes(data)})
    manifest = {"tool": TOOL_NAME, "tool_version": TOOL_VERSION, "operation": "extract",
                "created_utc": now_iso(), "argv": argv, "aes_key_source": key_source,
                "inputs": [file_record(p) for p in inputs], "outputs": records, "failed": failed}
    bad: List[int] = []
    if verify:
        for r in records:
            with open(os.path.join(out, r["file"]), "rb") as f:
                d = f.read()
            if sha256_bytes(d) != r["sha256"] or sha256_bytes(c.read_chunk(r["index"])) != r["sha256"]:
                bad.append(r["index"])
        manifest["verify"] = {"ok": not bad and not failed, "re_read": len(records), "mismatches": bad}
    write_json_new(os.path.join(out, "extract_manifest.json"), manifest, inputs)
    if failed:
        raise IoStoreError(f"{len(failed)} chunk(s) could not be extracted (see extract_manifest.json 'failed'); "
                           f"first: {failed[0]['error']}")
    if bad:
        raise IoStoreError(f"extract verification failed for {bad[:10]}")
    return manifest


def selfcheck(c: IoStoreContainer, deep: bool = False) -> dict:
    """The checks to run first on real game files."""
    res: Dict[str, object] = {"container": c.utoc_path, "header": c.header.to_json()}
    failures: List[str] = []
    warnings: List[str] = []
    again = c.toc.serialize()
    res["toc_reserialize_identical"] = again == c.toc_bytes
    if again != c.toc_bytes:
        n = next((k for k in range(min(len(again), len(c.toc_bytes))) if again[k] != c.toc_bytes[k]),
                 min(len(again), len(c.toc_bytes)))
        failures.append(f"TOC re-serialisation differs at byte {n}")
    res["trailing_bytes"] = len(c.toc.trailing)
    if c.toc.trailing:
        warnings.append(f"{len(c.toc.trailing)} unparsed trailing bytes after the TOC meta (kept verbatim)")
    if c.toc.metas is None and c.header.entry_count:
        warnings.append("no TOC meta section found (Initial-version TOC?)")
    if c.header.version > LATEST_KNOWN_VERSION:
        warnings.append(f"TOC version {c.header.version} newer than this tool knows")
    res["key"] = c.key_status()
    try:
        plain = c.directory_index_plain()
        if plain is not None:
            d = c.directory_index()
            res["directory_index"] = {"mount_point": d.mount_point.value, "directories": len(d.directories),
                                      "files": len(d.files), "strings": len(d.strings),
                                      "padding_bytes": len(d.trailing),
                                      "reserialize_identical": d.serialize() == plain,
                                      "mapped_paths": len(c.paths())}
            if d.serialize() != plain:
                failures.append("directory index re-serialisation differs")
    except KeyRequired as e:
        res["directory_index"] = {"error": str(e)}
        warnings.append(str(e))
    res["perfect_hash"] = c.toc.validate_perfect_hash()
    if res["perfect_hash"]["present"] and not res["perfect_hash"].get("ok"):
        failures.append("perfect hash lookup fails for some chunk ids")
    res["layout"] = c.toc.layout_report()
    sizes = {}
    for i, p in enumerate(c.partition_paths):
        sizes[i] = os.path.getsize(p) if os.path.exists(p) else None
        if sizes[i] is None:
            failures.append(f"missing partition {p}")
    beyond = 0
    for bi in range(len(c.toc.blocks)):
        pidx, local, n = c.block_disk_span(bi)
        if sizes.get(pidx) is None or local + n > sizes[pidx]:
            beyond += 1
    res["blocks_beyond_partition_end"] = beyond
    if beyond:
        failures.append(f"{beyond} blocks extend beyond their partition file")
    spans = disk_span_report(c)
    res["disk_spans_aligned16"] = spans
    if spans["beyond_partition_eof"] or spans["overlapping_block_pairs"] or spans["crossing_partition_size"]:
        warnings.append("16-byte-aligned block spans (what UE/CUE4Parse actually read) overlap, cross PartitionSize "
                        f"or run past the partition file end: {spans}")
    res["stats"] = c.stats()
    if deep:
        readable, errs = 0, {}
        for i in range(len(c.toc.chunk_ids)):
            try:
                c.read_chunk(i)
                readable += 1
            except Exception as e:
                k = type(e).__name__ + ": " + str(e)[:120]
                errs[k] = errs.get(k, 0) + 1
        res["chunks_readable"] = readable
        res["chunk_read_errors"] = errs
        det = c.detect_hash_algorithm(sample=8)
        res["meta_hash_detection"] = det
        algo = "blake3" if c.header.meta_uses_iohash else det["algo"]
        if c.toc.metas is not None and algo in ("sha1", "blake3"):
            bad = []
            for i in range(len(c.toc.chunk_ids)):
                try:
                    data = c.read_chunk(i)
                except Exception:
                    continue
                if compute_meta_hash(algo, data, c.header.meta_uses_iohash) != c.toc.metas[i].hash:
                    bad.append(i)
            res["meta_hash_mismatches"] = {"algorithm": algo, "count": len(bad), "first": bad[:20]}
        if c.toc.signatures is not None:
            v = {"aligned_raw": 0, "compressed_size_raw": 0, "neither": 0}
            for bi, want in enumerate(c.toc.signatures.block_hashes):
                b = c.toc.blocks[bi]
                raw = c.read_block_raw(bi, align_up(b.compressed_size, AES_BLOCK), allow_short=True)
                if hashlib.sha1(raw).digest() == want:
                    v["aligned_raw"] += 1
                elif hashlib.sha1(raw[:b.compressed_size]).digest() == want:
                    v["compressed_size_raw"] += 1
                else:
                    v["neither"] += 1
            res["block_signature_sha1_match"] = v
    else:
        res["meta_hash_detection"] = c.detect_hash_algorithm(sample=4)
    res["failures"] = failures
    res["warnings"] = warnings
    res["ok"] = not failures
    return res


# ---------------------------------------------------------------------------------------------
# build-ooz: compile https://github.com/powzix/ooz as a shared library with a POSIX shim
# ---------------------------------------------------------------------------------------------

OOZ_STDAFX_SHIM = r"""// POSIX shim replacing ooz's Windows stdafx.h (generated by iostore.py build-ooz)
#pragma once
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <assert.h>
#include <sys/stat.h>
#include <x86intrin.h>
typedef unsigned char byte;
typedef unsigned char uint8;
typedef unsigned int uint32;
typedef uint64_t uint64;
typedef int64_t int64;
typedef signed int int32;
typedef unsigned short uint16;
typedef signed short int16;
typedef unsigned int uint;
typedef int64_t __int64;
typedef void *HINSTANCE;
typedef union { int64_t QuadPart; } LARGE_INTEGER;
#define WINAPI
#define __forceinline inline __attribute__((always_inline))
static inline int QueryPerformanceCounter(LARGE_INTEGER *p) { p->QuadPart = 0; return 1; }
static inline int QueryPerformanceFrequency(LARGE_INTEGER *p) { p->QuadPart = 1; return 1; }
static inline HINSTANCE LoadLibraryA(const char *) { return 0; }
static inline void *GetProcAddress(HINSTANCE, const char *) { return 0; }
static inline unsigned char _BitScanReverse(unsigned long *index, unsigned long mask) { if (!mask) return 0; *index = 31 - __builtin_clz((unsigned)mask); return 1; }
static inline unsigned char _BitScanForward(unsigned long *index, unsigned long mask) { if (!mask) return 0; *index = __builtin_ctz((unsigned)mask); return 1; }
#define _byteswap_ulong(x) __builtin_bswap32(x)
#define _byteswap_uint64(x) __builtin_bswap64(x)
#define _byteswap_ushort(x) __builtin_bswap16(x)
"""

OOZ_EXPORT = r"""#include "stdafx.h"
int Kraken_Decompress(const byte *src, size_t src_len, byte *dst, size_t dst_len);
extern "C" int ooz_decompress(const unsigned char *src, size_t src_len, unsigned char *dst, size_t dst_len) {
  return Kraken_Decompress(src, src_len, dst, dst_len);
}
"""


def build_ooz(src: str, out_dir: str, verify: bool = False, argv=None) -> dict:
    sources = ["kraken.cpp", "bitknit.cpp", "lzna.cpp"]
    for s in sources:
        if not os.path.exists(os.path.join(src, s)):
            raise IoStoreError(f"{src} does not look like an ooz checkout (missing {s})")
    inputs = [os.path.join(src, s) for s in sources]
    out = prepare_out_dir(out_dir, inputs)
    build = os.path.join(out, "build")
    os.makedirs(build)
    for s in sources:
        copy_file_new(os.path.join(src, s), os.path.join(build, s), inputs)
    with open(os.path.join(build, "stdafx.h"), "x") as f:
        f.write(OOZ_STDAFX_SHIM)
    with open(os.path.join(build, "ooz_export.cpp"), "x") as f:
        f.write(OOZ_EXPORT)
    lib = os.path.join(out, "libooz.dylib" if sys.platform == "darwin" else "libooz.so")
    cxx = os.environ.get("CXX", "g++")
    cmd = [cxx, "-O2", "-fPIC", "-shared", "-w", "-fpermissive", "-Dmain=ooz_cli_main", "-o", lib] + \
        [os.path.join(build, s) for s in sources + ["ooz_export.cpp"]]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise IoStoreError(f"ooz build failed ({' '.join(cmd)}):\n{r.stderr[-4000:]}")
    manifest = {"tool": TOOL_NAME, "tool_version": TOOL_VERSION, "operation": "build-ooz", "created_utc": now_iso(),
                "argv": argv, "inputs": [file_record(p) for p in inputs], "outputs": [file_record(lib)],
                "compiler_command": cmd, "usage": f"export {OODLE_ENV}={lib}"}
    if verify:
        manifest["verify"] = ooz_selftest(lib, os.path.join(src, "testdata"))
    write_json_new(os.path.join(out, "build_manifest.json"), manifest, inputs)
    if verify and not manifest["verify"]["ok"]:
        raise IoStoreError(f"ooz self-test failed: {manifest['verify']}")
    return manifest


def ooz_selftest(lib: str, testdata: str) -> dict:
    """Decode ooz's own Kraken/Mermaid/Selkie/Leviathan corpus files; all codecs of one source file
    must yield identical bytes of the size stated in the 8-byte header."""
    dec = Decompressor(lib)
    results, ok = [], True
    if not os.path.isdir(testdata):
        return {"ok": False, "error": f"no testdata dir {testdata}"}
    for base in ("xml", "sao"):
        digests = set()
        for codec in ("kraken", "mermaid", "selkie", "leviathan"):
            p = os.path.join(testdata, f"{base}.{codec}")
            if not os.path.exists(p):
                continue
            with open(p, "rb") as f:
                d = f.read()
            n = struct.unpack_from("<Q", d)[0]
            hdr = 8
            if n >= 0x10000000000:
                n, hdr = struct.unpack_from("<I", d)[0], 4
            try:
                out = dec.decompress("Oodle", d[hdr:], n)
                digests.add(sha256_bytes(out))
                results.append({"file": p, "size": n, "sha256": sha256_bytes(out)})
            except Exception as e:
                ok = False
                results.append({"file": p, "error": str(e)})
        if len(digests) > 1:
            ok = False
    return {"ok": ok and bool(results), "decoded": results}


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------

def _open(args) -> Tuple[IoStoreContainer, str]:
    key, src = load_aes_key(getattr(args, "aes_key_file", None))
    dec = Decompressor(getattr(args, "oodle_lib", None))
    return IoStoreContainer(args.utoc, key, dec), src


def _print_json(obj) -> None:
    json.dump(obj, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def cmd_info(args) -> int:
    c, src = _open(args)
    with c:
        info = {"container": c.utoc_path, "utoc_size": len(c.toc_bytes), "header": c.header.to_json(),
                "aes_key_source": src, "key": c.key_status(),
                "sections": {"chunk_ids": len(c.toc.chunk_ids), "blocks": len(c.toc.blocks),
                             "perfect_hash_seeds": len(c.toc.perfect_hash_seeds),
                             "chunks_without_perfect_hash": len(c.toc.chunks_without_perfect_hash),
                             "encryption_ivs": len(c.toc.encryption_ivs),
                             "signed": c.toc.signatures is not None,
                             "signature_hash_size": c.toc.signatures.hash_size if c.toc.signatures else None,
                             "directory_index_bytes": len(c.toc.directory_index_raw),
                             "meta_entries": len(c.toc.metas) if c.toc.metas is not None else None,
                             "meta_entry_size": (24 if c.header.meta_uses_iohash else 33),
                             "on_demand_meta": c.toc.on_demand_chunk_hashes is not None,
                             "trailing_bytes": len(c.toc.trailing)},
                "toc_reserialize_identical": c.toc.serialize() == c.toc_bytes}
        info.update(c.stats())
        try:
            d = c.directory_index()
            if d is not None:
                info["directory_index"] = {"mount_point": d.mount_point.value, "directories": len(d.directories),
                                           "files": len(d.files), "strings": len(d.strings)}
        except KeyRequired as e:
            info["directory_index"] = {"error": str(e)}
        if not args.no_detect:
            info["meta_hash_detection"] = c.detect_hash_algorithm(sample=4)
        _print_json(info)
    return 0


def cmd_list(args) -> int:
    c, _ = _open(args)
    with c:
        paths = c.path_by_index()
        if c.encrypted and c.aes is None:
            print("note: encrypted container without key - paths unavailable", file=sys.stderr)
        idx = select_indices(c, args.glob or [], args.chunk_id or [], args.index or [], not (args.glob or args.chunk_id or args.index),
                             args.ignore_case)
        rows = [c.chunk_info(i, paths.get(i)) for i in idx]
        if args.type:
            rows = [r for r in rows if r["type_name"] == args.type or r["type_name_ue4"] == args.type
                    or r["type_name_ue5"] == args.type or str(r["type"]) == args.type]
        if args.json:
            _print_json(rows)
        else:
            for r in rows:
                print(f"{r['index']:6d}  {r['chunk_id']}  {r['type_name']:<22} {r['size']:>11} "
                      f"{(r['compressed_size'] if r['compressed_size'] is not None else -1):>11} {r['blocks']:>5}  "
                      f"{','.join(r['methods']):<10} {r.get('path') or ''}")
    return 0


def cmd_extract(args) -> int:
    c, src = _open(args)
    with c:
        idx = select_indices(c, args.glob or [], args.chunk_id or [], args.index or [], args.all, args.ignore_case)
        if not idx:
            raise IoStoreError("no chunks selected (use --glob/--chunk-id/--index/--all)")
        m = extract(c, idx, args.out_dir, args.verify, sys.argv, src)
        print(json.dumps({"extracted": len(m["outputs"]), "out_dir": os.path.abspath(args.out_dir),
                          "verify": m.get("verify")}, indent=2))
    return 0


def cmd_hash_all(args) -> int:
    c, _ = _open(args)
    with c:
        t = hash_all(c)
        if args.out:
            write_json_new(args.out, t, c.input_files())
            print(json.dumps({"written": os.path.abspath(args.out), "chunks": t["chunk_count"], "errors": t["errors"]}))
        else:
            _print_json(t)
    return 1 if t["errors"] else 0


def cmd_patch(args) -> int:
    c, src = _open(args)
    with c:
        pairs = list(args.replace or [])
        if args.replace_list:
            with open(args.replace_list, encoding="utf-8") as f:
                for e in json.load(f):
                    pairs.append([e["selector"], e["file"]])
        if not pairs:
            raise IoStoreError("no --replace given")
        repl: Dict[int, bytes] = {}
        extra_inputs = []
        for sel, path in pairs:
            i = resolve_selector(c, sel)
            if i in repl:
                raise IoStoreError(f"chunk {i} selected twice")
            with open(path, "rb") as f:
                repl[i] = f.read()
            extra_inputs.append(path)
        if args.replace_list:
            extra_inputs.append(args.replace_list)
        m = patch_container(c, repl, args.out_dir, rebuild=args.rebuild, unsign=args.unsign, hash_algo=args.hash_algo,
                            align=args.align, verify=args.verify, extra_inputs=extra_inputs, argv=sys.argv,
                            key_source=src, allow_unverified_key=args.allow_unverified_key)
        for w in m["warnings"]:
            print("WARNING:", w, file=sys.stderr)
        print(json.dumps({"out_dir": os.path.abspath(args.out_dir), "mode": m["mode"],
                          "meta_hash_algorithm": m["meta_hash_algorithm"],
                          "replaced": [(r["index"], r["chunk_id"], r["path"]) for r in m["replaced_chunks"]],
                          "verify": m.get("verify", {}).get("ok") if args.verify else "not run (use --verify)"},
                         indent=2))
    return 0


def cmd_diff(args) -> int:
    key, _ = load_aes_key(args.aes_key_file)
    dec = Decompressor(args.oodle_lib)
    a = _load_hash_table(args.a, key, dec)
    b = _load_hash_table(args.b, key, dec)
    d = diff_tables(a, b)
    d["a"], d["b"] = os.path.abspath(args.a), os.path.abspath(args.b)
    if args.out:
        write_json_new(args.out, d, [args.a, args.b])
    _print_json(d)
    return 0 if d["identical"] else 3


def cmd_selfcheck(args) -> int:
    c, _ = _open(args)
    with c:
        r = selfcheck(c, args.deep)
    _print_json(r)
    return 0 if r["ok"] else 1


def cmd_build_ooz(args) -> int:
    m = build_ooz(args.src, args.out_dir, args.verify, sys.argv)
    _print_json({"library": m["outputs"][0]["path"], "usage": m["usage"], "verify": m.get("verify")})
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="iostore.py", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, utoc=True):
        if utoc:
            sp.add_argument("utoc", help="path to the .utoc (partitions <base>.ucas/_sN.ucas next to it)")
        sp.add_argument("--aes-key-file", help=f"file with the 64-hex AES-256 key (else env {KEY_ENV})")
        sp.add_argument("--oodle-lib", help=f"Oodle decoder shared library (else env {OODLE_ENV})")

    sp = sub.add_parser("info", help="header/stats JSON")
    common(sp)
    sp.add_argument("--no-detect", action="store_true", help="skip meta-hash algorithm detection")
    sp.set_defaults(func=cmd_info)

    for name, fn, hlp in (("list", cmd_list, "list chunks"), ("extract", cmd_extract, "extract chunks")):
        sp = sub.add_parser(name, help=hlp)
        common(sp)
        sp.add_argument("--glob", action="append", help="fnmatch pattern on the path (mount point optional)")
        sp.add_argument("--chunk-id", action="append", help="24-hex FIoChunkId")
        sp.add_argument("--index", action="append", type=int, help="TOC entry index")
        sp.add_argument("--ignore-case", action="store_true")
        if name == "list":
            sp.add_argument("--type", help="filter by chunk type name or number")
            sp.add_argument("--json", action="store_true")
        else:
            sp.add_argument("--all", action="store_true")
            sp.add_argument("--out-dir", required=True, help="new (or empty) directory")
            sp.add_argument("--verify", action="store_true", help="re-read every written file and chunk")
        sp.set_defaults(func=fn)

    sp = sub.add_parser("hash-all", help="sha256 of every decompressed chunk")
    common(sp)
    sp.add_argument("--out", help="write JSON here (must not exist)")
    sp.set_defaults(func=cmd_hash_all)

    sp = sub.add_parser("patch", help="replace chunk payloads -> new container")
    common(sp)
    sp.add_argument("--out-dir", required=True, help="new (or empty) directory; output keeps the input file names")
    sp.add_argument("--replace", nargs=2, action="append", metavar=("SELECTOR", "FILE"),
                    help="SELECTOR = index:N | id:HEX24 | HEX24 | path:P | P")
    sp.add_argument("--replace-list", help='JSON list of {"selector": ..., "file": ...}')
    sp.add_argument("--rebuild", action="store_true", help="rewrite all partitions compactly")
    sp.add_argument("--unsign", action="store_true", help="allow patching a Signed container by dropping signatures")
    sp.add_argument("--hash-algo", default="auto", choices=("auto",) + HASH_ALGOS,
                    help="TOC meta hash for replaced chunks (auto = FIoHash/BLAKE3 for v>=8, else measured)")
    sp.add_argument("--align", type=int, default=AES_BLOCK, help="alignment of new chunk data in the .ucas")
    sp.add_argument("--allow-unverified-key", action="store_true",
                    help="encrypted container WITHOUT directory index: write even if no chunk could prove the key")
    sp.add_argument("--verify", action="store_true", help="re-read the output and compare with the input")
    sp.set_defaults(func=cmd_patch)

    sp = sub.add_parser("diff", help="compare two containers or two hash-all JSON files")
    sp.add_argument("a")
    sp.add_argument("b")
    sp.add_argument("--aes-key-file")
    sp.add_argument("--oodle-lib")
    sp.add_argument("--out", help="also write the diff JSON here (must not exist)")
    sp.set_defaults(func=cmd_diff)

    sp = sub.add_parser("selfcheck", help="round-trip + consistency checks (run first on real files)")
    common(sp)
    sp.add_argument("--deep", action="store_true", help="also read every chunk, verify meta hashes and block SHA-1s")
    sp.set_defaults(func=cmd_selfcheck)

    sp = sub.add_parser("build-ooz", help="build libooz (open-source Oodle decoder) for --oodle-lib")
    sp.add_argument("--src", required=True, help="checkout of https://github.com/powzix/ooz")
    sp.add_argument("--out-dir", required=True)
    sp.add_argument("--verify", action="store_true", help="decode ooz's testdata corpus after building")
    sp.set_defaults(func=cmd_build_ooz)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except IoStoreError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except BrokenPipeError:  # output piped into e.g. `head`
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return 0
    except OSError as e:  # missing/unreadable input, existing output file, ... (messages carry paths only)
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
