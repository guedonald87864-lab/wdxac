#!/usr/bin/env python3
"""Unreal Engine IoStore container (.utoc + .ucas[, _sN.ucas]) reader and patch writer.

Subcommands (all JSON-capable):
  info       header, flags, compression methods, partition/dir-index/meta stats, round-trip check
  list       one line per TOC entry: index, chunk id, type, size, blocks, method, path
  extract    decrypt + decompress chunks (by path glob / chunk id / index) into a new directory
  hash-all   SHA-256 of every decompressed chunk -> JSON (before/after diffs)
  patch      replace chunk payloads -> NEW .utoc/.ucas pair (append mode, or --rebuild)
  add        append NEW chunks, directory-index paths and package-store entries -> NEW container
  clone-package  copy a package (header + bulk chunks) under a new name/FPackageId -> NEW container
  container-header  decode the UE4.26 container header / package store (stock 32-byte or eFootball
             16-byte FPackageStoreEntry, detected structurally)
  package-id FPackageId::FromName (CityHash64 of the lower-cased UTF-16LE name) and chunk ids
  find-refs  read-only: which paths / store entries / package headers mention a package or a text
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
        """True when chunk types follow EIoChunkType5 (UE5), False for EIoChunkType (UE4); see
        chunk_type_naming() for the evidence and whether it is certain."""
        return self.chunk_type_naming()["ue5"]

    def chunk_type_naming(self) -> dict:
        """Which chunk-type enum this TOC uses. v1-v3 are written by UE4.25-4.27 only (EIoChunkType);
        v5+ by UE5 (EIoChunkType5: retoc lib.rs `is_new = version > PerfectHash`, CUE4Parse
        `Game >= GAME_UE5_0`). For v4 the two references DISAGREE (retoc: old enum; CUE4Parse: by engine
        version, and no shipped engine is mapped to v4 by retoc version.rs), so v4 is decided from the
        container's own ContainerHeader chunk (ContainerId, 0, type): 10 = UE4 enum, 6 = UE5 enum;
        without it the naming is reported as uncertain and writers that need a type code refuse."""
        cached = self.__dict__.get("_naming_cache")
        if cached is not None and cached[0] == (self.header.version, len(self.chunk_ids)):
            return cached[1]
        v = self.header.version
        if v < TocVersion.PerfectHash:
            res = {"ue5": False, "certain": True, "source": f"TOC v{v} (<= PartitionSize) is UE4.25-4.27: EIoChunkType"}
        elif v > TocVersion.PerfectHash:
            res = {"ue5": True, "certain": True, "source": f"TOC v{v} (> PerfectHash) is UE5+: EIoChunkType5"}
        else:
            cid = self.header.container_id
            types = {x.type for x in self.chunk_ids if x.id == cid and x.index == 0 and x.padding == 0}
            if 10 in types and 6 not in types:
                res = {"ue5": False, "certain": True, "source": "TOC v4: ContainerHeader chunk has the UE4 type 10"}
            elif 6 in types and 10 not in types:
                res = {"ue5": True, "certain": True, "source": "TOC v4: ContainerHeader chunk has the UE5 type 6"}
            else:
                res = {"ue5": True, "certain": False,
                       "source": "TOC v4 without a recognisable ContainerHeader chunk: retoc reads v4 with the UE4 "
                                 "enum, CUE4Parse by engine version - chunk type names are a guess (UE5 shown)"}
        self.__dict__["_naming_cache"] = ((v, len(self.chunk_ids)), res)
        return res

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
                "chunk_type_naming_evidence": toc.chunk_type_naming(),
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
# Package ids (FPackageId::FromName)
# ---------------------------------------------------------------------------------------------

def _zen():
    """zen_package.py lives next to this file (CityHash64, Zen package parser/renamer)."""
    try:
        import zen_package  # type: ignore
        return zen_package
    except ImportError:
        here = os.path.dirname(os.path.abspath(__file__))
        spec = importlib.util.spec_from_file_location("zen_package", os.path.join(here, "zen_package.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["zen_package"] = mod
        spec.loader.exec_module(mod)
        return mod


def cityhash64(data: bytes) -> int:
    """Google CityHash v1.1 CityHash64 (pure-Python port in zen_package.py; tested against the
    city-test.cc vectors, against CUE4Parse's C# CityHash and against package ids written by UE)."""
    return _zen().cityhash64(data)


def package_id_from_name(package_name: str) -> int:
    """FPackageId::FromName (UE4.26+): CityHash64(UTF-16LE(name with A-Z lower-cased)).
    Sources: CUE4Parse UE4/IO/Objects/FPackageId.cs FromName/ToLower, retoc lib.rs
    FPackageId::from_name (to_ascii_lowercase). Only ASCII letters are lowered (UE4 FChar::ToLower
    as ported by CUE4Parse); non-ASCII names are hashed as given."""
    return _zen().package_id_from_name(package_name)


# EIoChunkType (UE4, TOC <= v3) / EIoChunkType5 (UE5) values for the package chunk types
PACKAGE_CHUNK_TYPES_UE4 = {"ExportBundleData": 2, "BulkData": 3, "OptionalBulkData": 4, "MemoryMappedBulkData": 5}
PACKAGE_CHUNK_TYPES_UE5 = {"ExportBundleData": 1, "BulkData": 2, "OptionalBulkData": 3, "MemoryMappedBulkData": 4}


def chunk_type_code(name_or_number, ue5: bool) -> int:
    if isinstance(name_or_number, int):
        return name_or_number
    s = str(name_or_number).strip()
    if s.isdigit():
        return int(s)
    names = UE5_CHUNK_TYPES if ue5 else UE4_CHUNK_TYPES
    if s not in names:
        raise IoStoreError(f"unknown chunk type {s!r} for {'UE5' if ue5 else 'UE4'} naming (one of {names})")
    return names.index(s)


def container_header_type(ue5: bool) -> int:
    return chunk_type_code("ContainerHeader", ue5)


# ---------------------------------------------------------------------------------------------
# UE4.26/4.27 container header (ContainerHeader chunk) and its package store entries
# ---------------------------------------------------------------------------------------------

CONTAINER_HEADER_UE5_MAGIC = 0x496F436E
# layout name -> (FPackageStoreEntry size, offset of the ImportedPackages CArrayView inside it)
STORE_ENTRY_LAYOUTS = {
    # stock UE4.26/4.27: u64 ExportBundlesSize, i32 ExportCount, i32 ExportBundleCount, u32 LoadOrder,
    # u32 Pad, CArrayView ImportedPackages (CUE4Parse FFilePackageStoreEntry.cs default branch;
    # retoc container_header.rs EIoContainerHeaderVersion::Initial => (24, 32))
    "ue4-32": (32, 24),
    # eFootball: i32 ExportCount, i32 ExportBundleCount, CArrayView ImportedPackages
    # (CUE4Parse FFilePackageStoreEntry.cs `Ar.Game is GAME_eFootball`; retoc PreInitial => (8, 16))
    "efootball-16": (16, 8),
}


@dataclass
class StoreEntry:
    """One package's FPackageStoreEntry. export_bundles_size / load_order / pad exist only in the
    ue4-32 layout (None in efootball-16)."""
    export_count: int
    export_bundle_count: int
    imported_packages: List[int]
    export_bundles_size: Optional[int] = None
    load_order: Optional[int] = None
    pad: Optional[int] = None

    def to_json(self) -> dict:
        d = {"export_count": self.export_count, "export_bundle_count": self.export_bundle_count,
             "imported_packages": [f"0x{x:016X}" for x in self.imported_packages]}
        if self.export_bundles_size is not None:
            d.update(export_bundles_size=self.export_bundles_size, load_order=self.load_order, pad=self.pad)
        return d

    @classmethod
    def from_json(cls, d: dict) -> "StoreEntry":
        imp = d.get("imported_packages", [])
        if not isinstance(imp, list):
            raise IoStoreError("imported_packages must be a list of /Root/... package names or 0x ids")
        # _pid_arg refuses anything that is neither /Root/... nor 0x-hex (a missing leading '/' used to
        # be hashed silently into an id that matches no package)
        return cls(int(d["export_count"]), int(d["export_bundle_count"]), [_pid_arg(x) for x in imp],
                   d.get("export_bundles_size"), d.get("load_order"), d.get("pad", 0 if "load_order" in d else None))


class ContainerHeaderUE4:
    """FContainerHeader as serialised by UE4.26/4.27 (no magic, no version; CUE4Parse
    FIoContainerHeader.cs `BeforeVersionWasAdded`, retoc container_header.rs `version <= Initial`):

        u64 ContainerId | u32 PackageCount | TArray<u8> Names | TArray<u8> NameHashes |
        TArray<FPackageId> PackageIds | TArray<u8> StoreEntries | tail

    StoreEntries holds PackageCount fixed-size FPackageStoreEntry records followed by the
    ImportedPackages arrays; each CArrayView is (u32 ArrayNum, u32 OffsetToDataFromThis) with the
    offset relative to the CArrayView itself. The tail (TMap CulturePackageMap, TArray
    PackageRedirects, then any padding) is kept byte for byte. Everything is parsed so that
    serialize() is the exact inverse of parse()."""

    def __init__(self, container_id, package_count, names_raw, name_hashes_raw, package_ids, store_raw, tail):
        self.container_id = container_id
        self.package_count = package_count
        self.names_raw = names_raw
        self.name_hashes_raw = name_hashes_raw
        self.package_ids = list(package_ids)
        self.store_raw = bytes(store_raw)
        self.tail = tail

    @classmethod
    def parse(cls, data: bytes) -> "ContainerHeaderUE4":
        if len(data) >= 4 and struct.unpack_from("<I", data, 0)[0] == CONTAINER_HEADER_UE5_MAGIC:
            raise IoStoreError("this is a UE5 container header (magic 0x496F436E + version); only the UE4.26/4.27 "
                               "layout (eFootball) is supported for package registration")
        c = _Cursor(data, 0, "container header")
        container_id = struct.unpack("<Q", c.take(8, "ContainerId"))[0]
        package_count = c.u32("PackageCount")

        def u8_array(label):
            n = c.i32(label + " count")
            if n < 0:
                raise IoStoreError(f"container header: negative {label} size {n}")
            return c.take(n, label)
        names_raw = u8_array("Names")
        hashes_raw = u8_array("NameHashes")
        n = c.i32("PackageIds count")
        if n < 0 or 8 * n > c.remaining():
            raise IoStoreError(f"container header: bad PackageIds count {n}")
        ids = list(struct.unpack(f"<{n}Q", c.take(8 * n, "PackageIds")))
        store = u8_array("StoreEntries")
        tail = c.take(c.remaining(), "tail")
        return cls(container_id, package_count, names_raw, hashes_raw, ids, store, tail)

    def serialize(self) -> bytes:
        return b"".join([struct.pack("<QI", self.container_id, self.package_count),
                         struct.pack("<i", len(self.names_raw)), self.names_raw,
                         struct.pack("<i", len(self.name_hashes_raw)), self.name_hashes_raw,
                         struct.pack("<i", len(self.package_ids)),
                         struct.pack(f"<{len(self.package_ids)}Q", *self.package_ids),
                         struct.pack("<i", len(self.store_raw)), self.store_raw, self.tail])

    def copy(self) -> "ContainerHeaderUE4":
        return ContainerHeaderUE4(self.container_id, self.package_count, self.names_raw, self.name_hashes_raw,
                                  self.package_ids, self.store_raw, self.tail)

    # --- store entry layouts -------------------------------------------------------------------
    def check_layout(self, layout: str) -> dict:
        """Structural test of one entry layout against the stored bytes. A layout is accepted when
        the entry table fits, every ImportedPackages array lies inside the data area that follows
        the table, 8-byte aligned, counts are not negative and (ue4-32) Pad is 0."""
        E, m = STORE_ENTRY_LAYOUTS[layout]
        st, n = self.store_raw, len(self.package_ids)
        problems: List[str] = []
        res = {"layout": layout, "entry_size": E, "entries": n, "store_bytes": len(st)}
        if n * E > len(st):
            problems.append(f"{n} x {E}-byte entries need {n * E} bytes, StoreEntries has {len(st)}")
            res.update(ok=False, problems=problems)
            return res
        total = 0
        empty_with_offset = 0
        spans = []
        for i in range(n):
            b = i * E
            if E == 32:
                _ebs, ec, ebc, _lo, pad = struct.unpack_from("<QiiII", st, b)
                if pad:
                    problems.append(f"entry {i}: Pad = {pad} (expected 0)")
            else:
                ec, ebc = struct.unpack_from("<ii", st, b)
            if ec < 0 or ebc < 0:
                problems.append(f"entry {i}: negative ExportCount/ExportBundleCount ({ec}, {ebc})")
            cnt, off = struct.unpack_from("<II", st, b + m)
            if cnt:
                t = b + m + off
                if t < n * E or t + 8 * cnt > len(st) or (t - n * E) % 8:
                    problems.append(f"entry {i}: ImportedPackages ({cnt} at +{off}) points to {t}, outside the "
                                    f"8-aligned data area [{n * E}, {len(st)})")
                else:
                    spans.append((t, t + 8 * cnt))
                total += cnt
            elif off:
                empty_with_offset += 1
            if len(problems) > 8:
                break
        spans.sort()
        overlaps = sum(1 for (a0, e0), (a1, e1) in zip(spans, spans[1:]) if a1 < e0)
        res.update(ok=not problems, problems=problems[:8], imported_package_refs=total,
                   tightly_packed=(n * E + 8 * total == len(st)), overlapping_arrays=overlaps,
                   empty_arrays_with_offset=empty_with_offset)
        return res

    def detect_layouts(self) -> Dict[str, dict]:
        return {name: self.check_layout(name) for name in STORE_ENTRY_LAYOUTS}

    def resolve_layout(self, requested: str = "auto") -> Tuple[str, dict]:
        det = self.detect_layouts()
        if requested != "auto":
            if requested not in STORE_ENTRY_LAYOUTS:
                raise IoStoreError(f"unknown store entry layout {requested!r} ({', '.join(STORE_ENTRY_LAYOUTS)})")
            if not det[requested]["ok"]:
                raise IoStoreError(f"store entries do not fit the {requested} layout: {det[requested]['problems'][:3]}")
            return requested, {"requested": requested, "checks": det}
        ok = [k for k, v in det.items() if v["ok"]]
        if len(ok) != 1:
            raise IoStoreError(f"cannot determine the FPackageStoreEntry layout automatically ({len(ok)} layouts fit"
                               f"{': ' + ', '.join(ok) if ok else ''}); pass --store-entry-layout "
                               f"{'|'.join(STORE_ENTRY_LAYOUTS)}. Checks: {json.dumps(det)[:1500]}")
        return ok[0], {"detected": ok[0], "checks": det}

    def entries(self, layout: str) -> List[StoreEntry]:
        E, m = STORE_ENTRY_LAYOUTS[layout]
        st = self.store_raw
        out = []
        for i in range(len(self.package_ids)):
            b = i * E
            if E == 32:
                ebs, ec, ebc, lo, pad = struct.unpack_from("<QiiII", st, b)
            else:
                (ec, ebc), ebs, lo, pad = struct.unpack_from("<ii", st, b), None, None, None
            cnt, off = struct.unpack_from("<II", st, b + m)
            imp = list(struct.unpack_from(f"<{cnt}Q", st, b + m + off)) if cnt else []
            out.append(StoreEntry(ec, ebc, imp, ebs, lo, pad))
        return out

    @staticmethod
    def _record(layout: str, e: StoreEntry, cnt: int, off: int) -> bytes:
        if STORE_ENTRY_LAYOUTS[layout][0] == 32:
            if e.export_bundles_size is None or e.load_order is None:
                raise IoStoreError("the ue4-32 layout needs export_bundles_size and load_order for every entry")
            return struct.pack("<QiiIIII", e.export_bundles_size, e.export_count, e.export_bundle_count,
                               e.load_order, e.pad or 0, cnt, off)
        return struct.pack("<iiII", e.export_count, e.export_bundle_count, cnt, off)

    @classmethod
    def encode_store(cls, layout: str, items: Sequence[Tuple[int, StoreEntry]]) -> bytes:
        """Canonical StoreEntries buffer: all records, then the ImportedPackages arrays in record
        order (the form found in the real eFootball and UE4.27 headers, and retoc's writer)."""
        E, m = STORE_ENTRY_LAYOUTS[layout]
        n = len(items)
        table, data_area = bytearray(), bytearray()
        for j, (_, e) in enumerate(items):
            if e.imported_packages:
                cnt = len(e.imported_packages)
                off = n * E + len(data_area) - (j * E + m)
                data_area += struct.pack(f"<{cnt}Q", *e.imported_packages)
            else:
                cnt, off = 0, 0
            table += cls._record(layout, e, cnt, off)
        return bytes(table + data_area)

    def add_packages(self, layout: str, items: Sequence[Tuple[int, StoreEntry]], order: str = "auto") -> dict:
        """Register packages. order="append" (minimal change): the old records keep their bytes except
        the ImportedPackages offset, which grows by K x entry size because the data area moves behind
        the K new records; old arrays are copied verbatim, new arrays appended. order="sorted": the
        whole buffer is re-encoded canonically with PackageIds ascending (allowed only when the old
        buffer IS canonical, so old entries decode identically). order="auto": "sorted" when the
        existing PackageIds are already ascending (stock UE4.27 samples), else "append" (the real
        eFootball header is unsorted)."""
        E, m = STORE_ENTRY_LAYOUTS[layout]
        chk = self.check_layout(layout)
        if not chk["ok"]:
            raise IoStoreError(f"store entries do not fit the {layout} layout: {chk['problems'][:3]}")
        if self.package_count != len(self.package_ids):
            raise IoStoreError(f"PackageCount {self.package_count} != {len(self.package_ids)} PackageIds; refusing to edit")
        n, k = len(self.package_ids), len(items)
        seen = set(self.package_ids)
        for pid, _ in items:
            if pid in seen:
                raise IoStoreError(f"package id 0x{pid:016X} is already in the container header")
            seen.add(pid)
        ids_sorted = n >= 2 and all(a < b for a, b in zip(self.package_ids, self.package_ids[1:]))
        if order == "auto":
            order = "sorted" if ids_sorted else "append"
            reason = "existing PackageIds are ascending" if ids_sorted else "existing PackageIds are not sorted"
        else:
            reason = "requested"
        if order == "sorted":
            old = list(zip(self.package_ids, self.entries(layout)))
            if self.encode_store(layout, old) != self.store_raw:
                raise IoStoreError("StoreEntries are not in the canonical packed form, so they cannot be re-sorted "
                                   "safely; use --store-order append")
            merged = sorted(old + list(items), key=lambda t: t[0])
            self.store_raw = self.encode_store(layout, merged)
            self.package_ids = [p for p, _ in merged]
        elif order == "append":
            st = self.store_raw
            table = bytearray(st[:n * E])
            data_area = bytearray(st[n * E:])
            for i in range(n):
                cnt, off = struct.unpack_from("<II", table, i * E + m)
                if cnt:
                    struct.pack_into("<II", table, i * E + m, cnt, off + k * E)
            new_records = bytearray()
            for j, (pid, e) in enumerate(items):
                pos = (n + j) * E
                if e.imported_packages:
                    target = (n + k) * E + len(data_area)
                    cnt, off = len(e.imported_packages), target - (pos + m)
                    data_area += struct.pack(f"<{cnt}Q", *e.imported_packages)
                else:
                    cnt, off = 0, 0
                new_records += self._record(layout, e, cnt, off)
            self.store_raw = bytes(table + new_records + data_area)
            self.package_ids += [pid for pid, _ in items]
        else:
            raise IoStoreError(f"unknown store order {order!r} (auto|append|sorted)")
        self.package_count += k
        return {"layout": layout, "added": k, "package_count": self.package_count, "order": order,
                "order_reason": reason, "store_bytes": len(self.store_raw)}

    def tail_info(self) -> dict:
        """Best-effort decode of the tail (CulturePackageMap, PackageRedirects; retoc
        FCulturePackageMap / LegacyContainerHeaderPackageRedirect) - for reports only."""
        c = _Cursor(self.tail, 0, "container header tail")
        try:
            cultures = {}
            for _ in range(c.i32("CulturePackageMap count")):
                key = FString.read(c, "culture").value
                n = c.i32("culture entries")
                c.take(16 * n, "culture pairs")
                cultures[key] = n
            nred = c.i32("PackageRedirects count")
            c.take(16 * nred, "PackageRedirects")
            rest = self.tail[c.pos:]
            return {"culture_package_map": cultures, "package_redirects": nred, "trailing_bytes": len(rest),
                    "trailing_all_zero": not any(rest)}
        except IoStoreError as e:
            return {"unparsed": str(e), "tail_bytes": len(self.tail)}

    def summary(self, layout: Optional[str] = None, limit: int = 20) -> dict:
        d = {"container_id": f"0x{self.container_id:016X}", "package_count": self.package_count,
             "package_ids": len(self.package_ids), "names_bytes": len(self.names_raw),
             "name_hashes_bytes": len(self.name_hashes_raw), "store_bytes": len(self.store_raw),
             "tail": self.tail_info(), "layout_checks": self.detect_layouts()}
        if layout:
            ents = self.entries(layout)
            d["layout"] = layout
            d["entries"] = [{"package_id": f"0x{pid:016X}", **e.to_json()}
                            for pid, e in list(zip(self.package_ids, ents))[:limit]]
        return d


def _check_new_store_entries(c: "IoStoreContainer", ch: ContainerHeaderUE4, layout: str,
                             store_entries: Sequence[Tuple[int, StoreEntry]], new_chunks: Sequence["NewChunk"],
                             ebd: int, warnings: List[str], sample: int = 32) -> dict:
    """Cross-check every NEW store entry against the package header in its new ExportBundleData chunk.

    * FPackageId::FromName(name in the header) must equal the registered id.
    * ExportCount / ExportBundleCount must equal the header's export and bundle counts. UE4.26's
      AsyncLoading2 is reported to size its per-package export / export-bundle arrays from the store
      entry before the header is read [T4: Epic source not available here]; CUE4Parse ignores them.
    * ImportedPackages vs the graph data's imported package ids (warning only: their exact relation in
      UE4.26 is not verified).
    Each rule is first MEASURED on up to `sample` existing packages of this container; a violation is
    refused only when the container's own packages follow the rule, otherwise it is a warning."""
    zp = _zen()
    idx = {cid.id: i for i, cid in enumerate(c.toc.chunk_ids) if cid.type == ebd and cid.index == 0}
    checked, count_bad, id_bad = 0, [], []
    for pid, e in zip(ch.package_ids, ch.entries(layout)):
        if checked >= sample:
            break
        i = idx.get(pid)
        if i is None:
            continue
        try:
            p = zp.parse_legacy(c.read_chunk(i))
        except Exception:
            continue
        checked += 1
        if (e.export_count, e.export_bundle_count) != (len(p.exports), len(p.export_bundles)):
            count_bad.append(f"0x{pid:016X}")
        if package_id_from_name(p.package_name) != pid:
            id_bad.append(f"0x{pid:016X}")
    counts_rule = bool(checked) and not count_bad
    id_rule = bool(checked) and not id_bad
    data_of = {nc.chunk_id.id: nc.data for nc in new_chunks if nc.chunk_id.type == ebd and nc.chunk_id.index == 0}
    results = []
    for pid, e in store_entries:
        rec = {"package_id": f"0x{pid:016X}"}
        data = data_of.get(pid)
        if data is None:
            results.append({**rec, "checked": False, "reason": "no new ExportBundleData chunk for it in this run"})
            continue
        try:
            p = zp.parse_legacy(data)
        except Exception as ex:
            warnings.append(f"package 0x{pid:016X}: the new ExportBundleData chunk is not a UE4.26/4.27 Zen package "
                            f"this tool parses ({type(ex).__name__}: {ex}); its store entry was not cross-checked")
            results.append({**rec, "checked": False, "reason": "package header not parsed"})
            continue
        counts = (len(p.exports), len(p.export_bundles))
        name_id = package_id_from_name(p.package_name)
        for ok, rule, msg in (
                (name_id == pid, id_rule, f"the package header names {p.package_name!r}, whose FPackageId is "
                                          f"0x{name_id:016X}, not the registered 0x{pid:016X}"),
                ((e.export_count, e.export_bundle_count) == counts, counts_rule,
                 f"store entry ExportCount/ExportBundleCount ({e.export_count}, {e.export_bundle_count}) != the "
                 f"package header's {counts}")):
            if ok:
                continue
            if rule:
                raise IoStoreError(f"package 0x{pid:016X}: {msg} (all {checked} measured packages of this container "
                                   f"follow the rule). Refusing.")
            warnings.append(f"package 0x{pid:016X}: {msg} (rule not established on this container's own packages)")
        graph = {g["package_id"] for g in p.imported_packages}
        if graph != set(e.imported_packages):
            warnings.append(f"package 0x{pid:016X}: store entry ImportedPackages {len(e.imported_packages)} id(s) and the "
                            f"package's graph data {len(graph)} id(s) are not the same set")
        results.append({**rec, "checked": True, "header_name": p.package_name, "header_counts": list(counts)})
    return {"measured_existing_packages": checked, "counts_rule_holds": counts_rule, "counts_rule_violations": count_bad[:8],
            "package_id_rule_holds": id_rule, "package_id_rule_violations": id_bad[:8], "entries": results}


def find_container_header_index(toc: TocResource) -> Optional[int]:
    """TOC index of the (ContainerId, 0, ContainerHeader) chunk (IoStoreReader.ReadContainerHeader);
    the type code follows the TOC's chunk-type naming, the other naming is tried as a fallback."""
    cid = toc.header.container_id
    for ue5 in (toc.ue5_naming, not toc.ue5_naming):
        want = ChunkId.from_parts(cid, 0, container_header_type(ue5)).raw
        for i, x in enumerate(toc.chunk_ids):
            if x.raw == want:
                return i
    return None


# ---------------------------------------------------------------------------------------------
# Directory index editing (new paths)
# ---------------------------------------------------------------------------------------------

def _dir_index_copy(d: DirectoryIndex) -> DirectoryIndex:
    return DirectoryIndex(d.mount_point, [list(x) for x in d.directories], [list(x) for x in d.files],
                          list(d.strings), b"")


def normalize_new_path(path: str, mount_point: str) -> str:
    """Path relative to the mount point; accepts it with or without the mount point prefix."""
    p = path.replace("\\", "/")
    mp = mount_point if mount_point.endswith("/") else mount_point + "/"
    if mp and p.startswith(mp):
        p = p[len(mp):]
    elif p.startswith("../") or p.startswith("/"):
        raise IoStoreError(f"path {path!r} is not below the container mount point {mount_point!r}")
    parts = p.split("/")
    if not p or any(x in ("", ".", "..") for x in parts):
        raise IoStoreError(f"invalid path {path!r} (empty, '.' or '..' components)")
    return p


def dir_index_add_file(d: DirectoryIndex, rel_path: str, user_data: int) -> dict:
    """Register one file in a (copied) directory index: walk/create the directory chain
    (FIoDirectoryIndexEntry: Name, FirstChildEntry, NextSiblingEntry, FirstFileEntry), append the
    file (FIoFileIndexEntry: Name, NextFileEntry, UserData = TOC entry index) at the END of the
    directory's file list, so the enumeration order of existing files is unchanged. Names are
    reused from the string table when an identical string exists. Directory names match
    case-insensitively (UE paths are case-insensitive); a file that already exists (in any case) is
    an error."""
    created_dirs, new_strings = [], []

    def string_index(name: str) -> int:
        for i, s in enumerate(d.strings):
            if s.value == name:
                return i
        d.strings.append(FString.from_str(name))
        new_strings.append(name)
        return len(d.strings) - 1

    if not d.directories:
        d.directories.append([INVALID_HANDLE, INVALID_HANDLE, INVALID_HANDLE, INVALID_HANDLE])
    parts = rel_path.split("/")
    cur = 0
    for comp in parts[:-1]:
        child, last, found = d.directories[cur][1], None, None
        guard = 0
        while child != INVALID_HANDLE:
            nm = d.directories[child][0]
            if nm != INVALID_HANDLE and d.strings[nm].value.lower() == comp.lower():
                found = child
                break
            last, child = child, d.directories[child][2]
            guard += 1
            if guard > len(d.directories):
                raise IoStoreError("directory index: sibling loop")
        if found is None:
            found = len(d.directories)
            d.directories.append([string_index(comp), INVALID_HANDLE, INVALID_HANDLE, INVALID_HANDLE])
            if last is None:
                d.directories[cur][1] = found
            else:
                d.directories[last][2] = found
            created_dirs.append(comp)
        cur = found
    fname = parts[-1]
    f, last = d.directories[cur][3], None
    guard = 0
    while f != INVALID_HANDLE:
        if d.strings[d.files[f][0]].value.lower() == fname.lower():
            raise IoStoreError(f"path {rel_path!r} already exists in the directory index")
        last, f = f, d.files[f][1]
        guard += 1
        if guard > len(d.files):
            raise IoStoreError("directory index: file list loop")
    d.files.append([string_index(fname), INVALID_HANDLE, user_data])
    if last is None:
        d.directories[cur][3] = len(d.files) - 1
    else:
        d.files[last][1] = len(d.files) - 1
    return {"path": rel_path, "user_data": user_data, "created_directories": created_dirs,
            "new_strings": new_strings}


# ---------------------------------------------------------------------------------------------
# Perfect hash (TOC v4+): rebuild after the chunk set changed
# ---------------------------------------------------------------------------------------------

def build_perfect_hash(chunk_ids: Sequence[ChunkId], allow_overflow: bool,
                       max_seed: int = 1 << 20) -> Tuple[List[int], List[int], List[int]]:
    """Seeds and slot layout for the lookup implemented by IoStoreReader.TryResolve (CUE4Parse;
    the reader contract this must satisfy): seedIndex = H(id, 0) % SeedCount; seed > 0 -> slot =
    H(id, seed) % ChunkCount; seed < 0 -> slot = -seed - 1, or, when that is >= ChunkCount, the
    entry is looked up linearly through ChunksWithoutPerfectHash (TOC v5+); seed 0 -> absent.
    H is FIoChunkId.HashWithSeed (FNV-1 64). Construction (CHD-style, as UE's writer is described
    to do; Epic's code itself was not available): buckets by H(id, 0), largest first, search a
    seed that maps the whole bucket to distinct free slots; single-entry buckets take the lowest
    free slot via a negative seed; buckets without a seed go to the overflow list (v5+) or fail (v4).
    Returns (slot_of[i] for every input entry, seeds, sorted overflow slots)."""
    n = len(chunk_ids)
    if n == 0:
        return [], [], []
    seed_count = max(1, (n + 1) // 2)
    buckets: List[List[int]] = [[] for _ in range(seed_count)]
    for i, cid in enumerate(chunk_ids):
        buckets[cid.hash_with_seed(0) % seed_count].append(i)
    order = sorted(range(seed_count), key=lambda b: (-len(buckets[b]), b))
    free = [True] * n
    slot_of = [-1] * n
    seeds = [0] * seed_count
    overflow_items: List[int] = []
    for b in order:
        items = buckets[b]
        if len(items) <= 1:
            break
        for seed in range(1, max_seed + 1):
            slots = [chunk_ids[i].hash_with_seed(seed) % n for i in items]
            if len(set(slots)) == len(slots) and all(free[s] for s in slots):
                for i, s in zip(items, slots):
                    slot_of[i] = s
                    free[s] = False
                seeds[b] = seed
                break
        else:
            if not allow_overflow:
                raise IoStoreError(f"no perfect-hash seed <= {max_seed} for a bucket of {len(items)} chunk ids and "
                                   f"this TOC version has no overflow list")
            seeds[b] = -(n + 1)
            overflow_items.extend(items)
    free_slots = (s for s in range(n) if free[s])
    for b in order:
        if len(buckets[b]) == 1:
            s = next(free_slots)
            free[s] = False
            slot_of[buckets[b][0]] = s
            seeds[b] = -s - 1
    overflow = []
    for i in overflow_items:
        s = next(free_slots)
        slot_of[i] = s
        overflow.append(s)
    if any(s < 0 for s in slot_of) or len(set(slot_of)) != n:
        raise IoStoreError("internal error: perfect hash slots are not a permutation")
    return slot_of, seeds, sorted(overflow)


# ---------------------------------------------------------------------------------------------
# add: new chunks, new paths, new package store entries
# ---------------------------------------------------------------------------------------------

@dataclass
class NewChunk:
    """A chunk to append. `path` is relative to the mount point (or includes it); `meta_flags` are
    FIoStoreTocEntryMetaFlags (Compressed is always cleared: new data is stored uncompressed;
    MemoryMapped keeps the chunk page-aligned and contiguous)."""
    chunk_id: ChunkId
    data: bytes
    path: Optional[str] = None
    meta_flags: int = 0
    label: str = ""


def add_to_container(c: IoStoreContainer, new_chunks: Sequence[NewChunk], out_dir: str, *,
                     store_entries: Sequence[Tuple[int, StoreEntry]] = (), store_layout: str = "auto",
                     store_order: str = "auto", replacements: Optional[Dict[int, bytes]] = None,
                     hash_algo: str = "auto",
                     align: int = AES_BLOCK, unsign: bool = False, verify: bool = False,
                     allow_unverified_key: bool = False, extra_inputs: Sequence[str] = (),
                     argv: Optional[List[str]] = None, key_source: str = "none", operation: str = "add",
                     extra_manifest: Optional[dict] = None, max_seed: int = 1 << 20,
                     allow_toc_trailing: bool = False) -> dict:
    """Write a NEW container (same base name) into out_dir with chunks appended.

    * New chunks are appended to the TOC (FIoChunkId, FIoOffsetAndLength, meta) and their data to
      the last partition as uncompressed blocks (method 0), encrypted like the rest (ECB, or CTR
      with fresh IVs), exactly like `patch` append mode. Old blocks are not touched.
    * New paths go into the directory index (re-serialised, re-encrypted when Encrypted).
    * TOC v4+ (perfect hash): seeds/overflow list are rebuilt for the new chunk set and all
      per-chunk arrays (ids, offsets, metas) are permuted into their slots; directory-index
      UserData follows the permutation. v1-v3 (UE4.26 = v2): new entries are simply appended.
    * `store_entries`: (FPackageId, StoreEntry) pairs are registered in the container header chunk
      (ContainerId, 0, ContainerHeader), in the given or detected FPackageStoreEntry layout. The
      header chunk is rewritten like a `patch` replacement.
    * `replacements`: optional {TOC index: bytes} applied in the same run (as `patch` does).
    Nothing existing is deleted or reordered (except the perfect-hash permutation)."""
    toc_in = c.toc
    h_in = c.header
    warnings: List[str] = []
    replacements = dict(replacements or {})
    if not new_chunks and not store_entries:
        raise IoStoreError("nothing to add")
    if align < 1 or align % AES_BLOCK:
        raise IoStoreError("--align must be a positive multiple of 16")
    key_check = _writer_preflight(c, unsign, allow_unverified_key, warnings, "adding chunks")
    if h_in.source_hash_count:
        raise IoStoreError(f"TOC v{h_in.version} declares {h_in.source_hash_count} source hashes (AddedSourceHashes); "
                           f"their layout is not known to this tool (CUE4Parse does not read them either), so they "
                           f"could not be kept consistent with a changed chunk set. Refusing.")
    if toc_in.trailing and not allow_toc_trailing:
        raise IoStoreError(f"{len(toc_in.trailing)} unparsed bytes follow the TOC meta section. If they are per-chunk "
                           f"data they would no longer match once chunks are added (or permuted by the perfect hash). "
                           f"Refusing; pass --keep-toc-trailing to copy them verbatim anyway.")
    if toc_in.trailing:
        warnings.append(f"{len(toc_in.trailing)} unparsed bytes after the TOC meta copied verbatim (--keep-toc-trailing)")
    n_in = len(toc_in.chunk_ids)
    ue5 = toc_in.ue5_naming
    for i in replacements:
        if not 0 <= i < n_in:
            raise IoStoreError(f"replacement index {i} out of range")
    existing = {cid.raw: i for i, cid in enumerate(toc_in.chunk_ids)}
    id_type = {(cid.id, cid.type): i for i, cid in enumerate(toc_in.chunk_ids)}
    seen = set()
    for nc in new_chunks:
        r = nc.chunk_id.raw
        if len(r) != 12:
            raise IoStoreError("chunk id must be 12 bytes")
        if r in existing:
            raise IoStoreError(f"chunk id {nc.chunk_id.hex} already exists (TOC index {existing[r]}); use `patch` "
                               f"to replace an existing chunk")
        if r in seen:
            raise IoStoreError(f"chunk id {nc.chunk_id.hex} given twice")
        seen.add(r)
        if nc.chunk_id.padding:
            raise IoStoreError(f"chunk id {nc.chunk_id.hex}: padding byte (offset 10) must be 0")
        if (nc.chunk_id.id, nc.chunk_id.type) in id_type:
            warnings.append(f"chunk {nc.chunk_id.hex} has the id and type of TOC entry {id_type[(nc.chunk_id.id, nc.chunk_id.type)]} "
                            f"with another ChunkIndex; CUE4Parse's FIoChunkId.Equals ignores ChunkIndex")
        if len(nc.data) > MAX_U40:
            raise IoStoreError("chunk larger than 2^40 bytes")
        if nc.meta_flags & ~0xFF:
            raise IoStoreError("meta flags must fit in 8 bits")

    # --- directory index (validated now; user data = pre-permutation TOC index) ---------------
    d_new: Optional[DirectoryIndex] = None
    paths_added: List[dict] = []
    if any(nc.path for nc in new_chunks):
        if h_in.version < TocVersion.DirectoryIndex or not (h_in.flags & ContainerFlags.Indexed):
            raise IoStoreError("the container has no directory index (Indexed flag); new paths cannot be registered "
                               "(add without paths, or use a container that has one)")
        d_in = c.directory_index()  # KeyRequired when encrypted without key
        if d_in is None:
            raise IoStoreError("the container is Indexed but its directory index is empty; refusing to invent one")
        d_new = _dir_index_copy(d_in)
        for k, nc in enumerate(new_chunks):
            if nc.path:
                rel = normalize_new_path(nc.path, d_new.mount_point.value)
                rec = dir_index_add_file(d_new, rel, n_in + k)
                rec["chunk_id"] = nc.chunk_id.hex
                paths_added.append(rec)

    if h_in.version >= TocVersion.PerfectHash and toc_in.perfect_hash_seeds and toc_in.directory_index_raw \
            and (h_in.flags & ContainerFlags.Indexed):
        d_chk = c.directory_index()
        bad = [f[2] for f in (d_chk.files if d_chk else []) if not 0 <= f[2] < n_in]
        if bad:   # checked before anything is written: the perfect-hash rebuild must remap every UserData
            raise IoStoreError(f"directory index file entries point at TOC indices {bad[:5]} (only {n_in} entries); "
                               f"they cannot be remapped for the rebuilt perfect hash")

    # --- container header (package store) ------------------------------------------------------
    header_info = None
    if store_entries:
        hidx = find_container_header_index(toc_in)
        if hidx is None:
            raise IoStoreError(f"no ContainerHeader chunk (ContainerId 0x{h_in.container_id:016X}, 0, ContainerHeader) "
                               f"in this container; cannot register packages")
        if hidx in replacements:
            raise IoStoreError("the container header chunk is both replaced and edited")
        raw = c.read_chunk(hidx)
        ch = ContainerHeaderUE4.parse(raw)
        if ch.serialize() != raw:
            raise IoStoreError("container header does not re-serialise byte-identically; refusing to edit it")
        layout, layout_evidence = ch.resolve_layout(store_layout)
        if ch.container_id != h_in.container_id:
            warnings.append(f"container header ContainerId 0x{ch.container_id:016X} != TOC ContainerId "
                            f"0x{h_in.container_id:016X}")
        ids = [pid for pid, _ in store_entries]
        if len(set(ids)) != len(ids):
            raise IoStoreError("a package id is registered twice")
        ebd = chunk_type_code("ExportBundleData", ue5)
        new_ebd = {nc.chunk_id.id for nc in new_chunks if nc.chunk_id.type == ebd and nc.chunk_id.index == 0}
        old_ebd = {cid.id for cid in toc_in.chunk_ids if cid.type == ebd and cid.index == 0}
        for pid in ids:
            if pid not in new_ebd and pid not in old_ebd:
                warnings.append(f"package 0x{pid:016X} is registered in the container header but has no "
                                f"ExportBundleData chunk in this container")
        entry_check = _check_new_store_entries(c, ch, layout, store_entries, new_chunks, ebd, warnings)
        ch2 = ch.copy()
        add_rec = ch2.add_packages(layout, store_entries, store_order)
        new_raw = ch2.serialize()
        # read back (by package id): old entries identical, new entries as requested
        back = ContainerHeaderUE4.parse(new_raw)
        if not back.check_layout(layout)["ok"]:
            raise IoStoreError("internal error: edited container header fails its own layout check")
        old_map = dict(zip(ch.package_ids, ch.entries(layout)))
        new_map = dict(zip(back.package_ids, back.entries(layout)))
        if len(new_map) != len(old_map) + len(store_entries) or back.package_count != len(back.package_ids):
            raise IoStoreError("internal error: package count after the edit is wrong")
        if any(new_map.get(p) != e for p, e in old_map.items()):
            raise IoStoreError("internal error: existing store entries changed")
        for p, e in store_entries:
            want = StoreEntry(e.export_count, e.export_bundle_count, list(e.imported_packages),
                              e.export_bundles_size if layout == "ue4-32" else None,
                              e.load_order if layout == "ue4-32" else None,
                              (e.pad or 0) if layout == "ue4-32" else None)
            if new_map.get(p) != want:
                raise IoStoreError("internal error: new store entries do not read back")
        if back.tail != ch.tail or back.names_raw != ch.names_raw or back.name_hashes_raw != ch.name_hashes_raw:
            raise IoStoreError("internal error: container header tail/names changed")
        replacements[hidx] = new_raw
        header_info = {"toc_index": hidx, "chunk_id": toc_in.chunk_ids[hidx].hex, "layout": layout,
                       "layout_evidence": layout_evidence, "entry_vs_package_header": entry_check,
                       "old_size": len(raw), "new_size": len(new_raw),
                       "old_package_count": ch.package_count, **add_rec,
                       "added_entries": [{"package_id": f"0x{p:016X}", **e.to_json()} for p, e in store_entries],
                       "tail": ch.tail_info()}

    algo, algo_info = _hash_algo_for(c, hash_algo, replacements.keys())
    inputs = c.input_files() + list(extra_inputs)
    out = prepare_out_dir(out_dir, inputs)
    utoc_out = os.path.join(out, os.path.basename(c.utoc_path))
    for p in [utoc_out] + [partition_path(utoc_out, i) for i in range(h_in.effective_partition_count + 8)]:
        assert_not_input(p, inputs)
    input_records = [file_record(p) for p in inputs]

    toc = toc_in.clone()
    h = toc.header
    bs = h.compression_block_size
    psize = h.effective_partition_size
    allow_new = h.version >= TocVersion.PartitionSize
    ctr = h.encryption_method == EncryptionMethod.AES_CTR
    aes = c.aes if c.encrypted else None
    dir_iv = toc.encryption_ivs[-1:] if ctr and (h.flags & ContainerFlags.Indexed) else []
    blocks = list(toc.blocks)
    new_ivs = list(toc.encryption_ivs[:len(toc.blocks)]) if ctr else []
    mm_align, mm_info = observed_mmap_alignment(toc_in)
    t0 = time.time()

    for i, p in enumerate(c.partition_paths):
        if not os.path.exists(p):
            raise IoStoreError(f"missing input partition {p}")
        copy_file_new(p, partition_path(utoc_out, i), inputs)
    last = h.effective_partition_count - 1
    start = os.path.getsize(partition_path(utoc_out, last))
    for b in toc_in.blocks:
        pidx, local = divmod(b.offset, psize)
        if pidx == last:
            start = max(start, local + align_up(b.compressed_size, AES_BLOCK))
    pw = _PartitionWriter(utoc_out, psize, last, start, allow_new)

    def write_chunk(data: bytes, mmapped: bool) -> Tuple[int, int]:
        first_block = len(blocks)
        pieces = [data[s:s + bs] for s in range(0, len(data), bs)]
        a = max(align, mm_align) if mmapped else align
        if mmapped and pieces:
            pw.reserve(sum(align_up(len(p), AES_BLOCK) for p in pieces), a)
        for k, piece in enumerate(pieces):
            iv = os.urandom(12) if ctr else None
            disk = _disk_block(piece, aes, iv)
            goff = pw.write(disk, a if k == 0 else AES_BLOCK)
            blocks.append(CompressedBlock(goff, len(piece), len(piece), 0))
            if ctr:
                new_ivs.append(iv)
        return first_block, len(blocks)

    replaced_records, added_records = [], []
    try:
        for i in sorted(replacements):
            mm = toc_in.metas is not None and bool(toc_in.metas[i].flags & MetaFlags.MemoryMapped)
            fb, lb = write_chunk(replacements[i], mm)
            toc.offsets[i] = (fb * bs, len(replacements[i]))
            replaced_records.append((i, fb, lb))
        for k, nc in enumerate(new_chunks):
            fb, lb = write_chunk(nc.data, bool(nc.meta_flags & MetaFlags.MemoryMapped))
            toc.chunk_ids.append(nc.chunk_id)
            toc.offsets.append((fb * bs, len(nc.data)))
            if toc.metas is not None:
                flags = nc.meta_flags & ~int(MetaFlags.Compressed) & 0xFF
                mh = compute_meta_hash(algo, nc.data, h.meta_uses_iohash) if algo in HASH_ALGOS else \
                    (b"\0" * (20 if h.meta_uses_iohash else 32))
                toc.metas.append(TocEntryMeta(mh, flags, b"\0\0\0" if h.meta_uses_iohash else b""))
            added_records.append((k, fb, lb))
    finally:
        pw.close()
    if toc.metas is None and new_chunks:
        warnings.append("the input TOC has no meta section; new chunks have no meta entries either")
    toc.blocks = blocks
    if pw.created:
        h.partition_count = max(h.partition_count, pw.pidx + 1)
        warnings.append(f"created new partition file(s) {pw.created}")
    if toc.metas is not None:
        for i in replacements:
            m = toc.metas[i]
            new_flags = int(m.flags) & ~int(MetaFlags.Compressed) & 0xFF
            toc.metas[i] = TocEntryMeta(compute_meta_hash(algo, replacements[i], h.meta_uses_iohash)
                                        if algo != "none" else m.hash, new_flags, m.pad)

    # --- perfect hash ----------------------------------------------------------------------------
    n_out = len(toc.chunk_ids)
    perm = list(range(n_out))
    ph_info: dict = {"used": bool(toc_in.perfect_hash_seeds), "rebuilt": False}
    if h.version >= TocVersion.PerfectHash and toc_in.perfect_hash_seeds:
        slot_of, seeds, overflow = build_perfect_hash(toc.chunk_ids, h.version >= TocVersion.PerfectHashWithOverflow,
                                                      max_seed)
        ids2, offs2 = [None] * n_out, [None] * n_out
        metas2 = [None] * n_out if toc.metas is not None else None
        for i, s in enumerate(slot_of):
            ids2[s] = toc.chunk_ids[i]
            offs2[s] = toc.offsets[i]
            if metas2 is not None:
                metas2[s] = toc.metas[i]
        toc.chunk_ids, toc.offsets = ids2, offs2
        if metas2 is not None:
            toc.metas = metas2
        toc.perfect_hash_seeds, toc.chunks_without_perfect_hash = seeds, overflow
        perm = slot_of
        ph_info.update(rebuilt=True, seeds=len(seeds), overflow=len(overflow),
                       entries_moved=sum(1 for i in range(n_in) if slot_of[i] != i))
    elif h.version >= TocVersion.PerfectHash and n_in:
        ph_info["note"] = "v4+ TOC without perfect-hash seeds: lookup stays linear"

    # --- directory index -------------------------------------------------------------------------
    dir_info = None
    if (d_new is not None or perm != list(range(n_out))) and toc_in.directory_index_raw and \
            (h.flags & ContainerFlags.Indexed):
        if d_new is None:
            d_new = _dir_index_copy(c.directory_index())
        for f in d_new.files:
            if not 0 <= f[2] < n_out:
                raise IoStoreError(f"directory index file entry points at TOC index {f[2]} (only {n_out} entries); "
                                   f"cannot remap it for the rebuilt perfect hash")
            f[2] = perm[f[2]]
        plain = d_new.serialize()
        if c.encrypted:
            plain += b"\0" * (-len(plain) % AES_BLOCK)
            if ctr:
                dir_iv = [os.urandom(12)]
                raw_dir = aes.ctr_crypt(plain, dir_iv[0])
            else:
                raw_dir = aes.ecb_encrypt(plain)
        else:
            raw_dir = plain
        toc.directory_index_raw = raw_dir
        dir_info = {"rewritten": True, "paths_added": paths_added, "old_bytes": len(toc_in.directory_index_raw),
                    "new_bytes": len(raw_dir), "user_data_remapped": perm != list(range(n_out)),
                    "encrypted": c.encrypted, "old_trailing_padding_dropped": len(c.directory_index().trailing)}
    if ctr:
        toc.encryption_ivs = new_ivs + dir_iv
    if toc.on_demand_block_hashes is not None:
        raise IoStoreError("OnDemand meta present; not supported by the writer")
    if toc.signatures is not None:
        toc.signatures = None
        warnings.append("Signed flag cleared and signature section removed (--unsign); a runtime that enforces "
                        "IoStore signatures will reject this container")
    toc.sync_header_counts()
    with open(utoc_out, "xb") as f:
        f.write(toc.serialize())

    added = []
    for k, fb, lb in added_records:
        nc = new_chunks[k]
        j = perm[n_in + k]
        added.append({"index": j, "chunk_id": nc.chunk_id.hex, "id": f"0x{nc.chunk_id.id:016X}",
                      "type_name": nc.chunk_id.type_name(ue5), "path": nc.path, "size": len(nc.data),
                      "sha256": sha256_bytes(nc.data), "blocks": [fb, lb - 1] if lb > fb else [],
                      "meta_flags": toc.metas[j].flags if toc.metas is not None else None,
                      "meta_hash": toc.metas[j].hash.hex() if toc.metas is not None else None, "label": nc.label})
    replaced = [{"index": perm[i], "old_index": i, "chunk_id": toc_in.chunk_ids[i].hex,
                 "old_size": toc_in.offsets[i][1], "new_size": len(replacements[i]),
                 "new_sha256": sha256_bytes(replacements[i])} for i, _, _ in replaced_records]
    old_hj, new_hj = h_in.to_json(), h.to_json()
    out_files = [utoc_out] + [partition_path(utoc_out, i) for i in range(h.effective_partition_count)]
    manifest = {
        "tool": TOOL_NAME, "tool_version": TOOL_VERSION, "operation": operation, "created_utc": now_iso(),
        "argv": argv, "aes_key_source": key_source, "aes_key_check": key_check, "inputs": input_records,
        "outputs": [file_record(p) for p in out_files if os.path.exists(p)],
        "container": {"name": c.name, "toc_version": h.version, "toc_version_name": h.version_name,
                      "encrypted": c.encrypted, "encryption_method": h.encryption_method.name,
                      "chunk_type_naming": "UE5" if ue5 else "UE4",
                      "chunk_type_naming_evidence": toc_in.chunk_type_naming()},
        "meta_hash_algorithm": algo, "meta_hash_algorithm_info": algo_info,
        "header_changes": {k: {"old": v, "new": new_hj[k]} for k, v in old_hj.items() if new_hj[k] != v},
        "added_chunks": added, "replaced_chunks": replaced, "container_header": header_info,
        "directory_index": dir_info, "perfect_hash": ph_info, "memory_mapped_alignment": mm_info,
        "unchanged": ["existing chunk ids, payloads and on-disk blocks", "existing paths", "existing package store "
                      "entries (except the ImportedPackages offset shift)", "compression method table",
                      "container id / key guid"],
        "warnings": warnings, "elapsed_s": round(time.time() - t0, 3),
    }
    if extra_manifest:
        manifest.update(extra_manifest)
    if verify:
        manifest["verify"] = verify_add(c, utoc_out, new_chunks, replacements, algo, input_records,
                                        header_info, paths_added)
    write_json_new(os.path.join(out, c.name + ".manifest.json"), manifest, inputs)
    if verify and not manifest["verify"]["ok"]:
        raise IoStoreError(f"verification FAILED: {json.dumps(manifest['verify']['failures'][:5])}")
    return manifest


def verify_add(c_in: IoStoreContainer, utoc_out: str, new_chunks: Sequence[NewChunk], replacements: Dict[int, bytes],
               algo: str, input_records: Optional[List[dict]], header_info: Optional[dict],
               paths_added: Sequence[dict]) -> dict:
    """Re-read the output of add_to_container with the reader: every old chunk is found BY ID with
    identical length/meta/raw blocks/payload, every new chunk resolves by id (and by path) and reads
    back, old paths still map to the same chunk ids, the perfect hash resolves everything, the
    package store keeps its old entries and has the new ones."""
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
                 "compression_methods": (c_in.toc.method_names_raw, o.toc.method_names_raw)}
        changed_fixed = [k for k, (a, b) in fixed.items() if a != b]
        checks["fixed_header_fields_unchanged"] = not changed_fixed
        if changed_fixed:
            failures.append(f"fields that must not change did: {changed_fixed}")
        n_in = len(c_in.toc.chunk_ids)
        checks["entry_count"] = {"old": n_in, "new": len(o.toc.chunk_ids), "added": len(new_chunks)}
        if len(o.toc.chunk_ids) != n_in + len(new_chunks):
            failures.append("entry count is not old + added")
        where: Dict[bytes, int] = {}
        for j, cid in enumerate(o.toc.chunk_ids):
            if cid.raw in where:
                failures.append(f"duplicate chunk id {cid.hex} in output")
            where[cid.raw] = j
        ph = o.toc.validate_perfect_hash()
        checks["perfect_hash"] = {"present": ph["present"], "ok": ph.get("ok", True)}
        if ph["present"] and not ph.get("ok"):
            failures.append(f"perfect hash lookup failures: {ph['failures'][:3]}")
        ctr_in = hi.encryption_method == EncryptionMethod.AES_CTR
        ctr_out = ho.encryption_method == EncryptionMethod.AES_CTR
        bs = hi.compression_block_size
        raw_same = deep_same = deep_skipped = 0
        for i, cid in enumerate(c_in.toc.chunk_ids):
            j = where.get(cid.raw)
            if j is None or o.toc.resolve(cid) != j:
                failures.append(f"old chunk {cid.hex} not found / not resolvable in the output")
                continue
            if i in replacements:
                try:
                    got = o.read_chunk(j)
                    ok = got == replacements[i]
                    if ok and o.toc.metas is not None and algo in HASH_ALGOS:
                        ok = o.toc.metas[j].hash == compute_meta_hash(algo, got, ho.meta_uses_iohash)
                    if not ok:
                        failures.append(f"replaced chunk {cid.hex} reads back wrong")
                except Exception as e:
                    failures.append(f"replaced chunk {cid.hex}: {type(e).__name__}: {e}")
                continue
            if o.toc.offsets[j][1] != c_in.toc.offsets[i][1] or o.toc.offsets[j][0] % bs != c_in.toc.offsets[i][0] % bs:
                failures.append(f"old chunk {cid.hex}: length or in-block offset changed")
                continue
            if o.toc.metas is not None and c_in.toc.metas is not None and o.toc.metas[j] != c_in.toc.metas[i]:
                failures.append(f"old chunk {cid.hex}: meta changed")
            try:
                fa, _, _ = c_in.toc.chunk_block_range(i)
                fo, _, _ = o.toc.chunk_block_range(j)
                a = [(x.compressed_size, x.uncompressed_size, x.method_index, r,
                      c_in.toc.encryption_ivs[fa + k] if ctr_in else None)
                     for k, (x, r) in enumerate(c_in.chunk_raw_blocks(i))]
                b = [(x.compressed_size, x.uncompressed_size, x.method_index, r,
                      o.toc.encryption_ivs[fo + k] if ctr_out else None)
                     for k, (x, r) in enumerate(o.chunk_raw_blocks(j))]
            except Exception as e:
                failures.append(f"old chunk {cid.hex}: raw blocks unreadable: {type(e).__name__}: {e}")
                continue
            if a != b:
                failures.append(f"old chunk {cid.hex}: on-disk blocks (or AES-CTR IVs) differ")
                continue
            raw_same += 1
            if all(c_in.decompressor.availability(m)[0] for m in c_in.chunk_methods(i)):
                try:
                    if sha256_bytes(o.read_chunk(j)) != sha256_bytes(c_in.read_chunk(i)):
                        failures.append(f"old chunk {cid.hex}: payload differs")
                    else:
                        deep_same += 1
                except Exception as e:
                    failures.append(f"old chunk {cid.hex}: cannot be decoded: {type(e).__name__}: {e}")
            else:
                deep_skipped += 1
        checks["old_chunks_raw_blocks_identical"] = raw_same
        checks["old_chunks_payload_identical"] = deep_same
        checks["old_chunks_payload_check_skipped_no_decoder"] = deep_skipped
        new_ok = 0
        for nc in new_chunks:
            j = where.get(nc.chunk_id.raw)
            if j is None or o.toc.resolve(nc.chunk_id) != j:
                failures.append(f"new chunk {nc.chunk_id.hex} not resolvable by id")
                continue
            try:
                got = o.read_chunk(j)
            except Exception as e:
                failures.append(f"new chunk {nc.chunk_id.hex}: {type(e).__name__}: {e}")
                continue
            ok = got == nc.data
            if ok and o.toc.metas is not None:
                if algo in HASH_ALGOS and o.toc.metas[j].hash != compute_meta_hash(algo, got, ho.meta_uses_iohash):
                    ok = False
                if o.toc.metas[j].flags != nc.meta_flags & ~int(MetaFlags.Compressed) & 0xFF:
                    ok = False
            if not ok:
                failures.append(f"new chunk {nc.chunk_id.hex} reads back wrong (data/meta)")
            else:
                new_ok += 1
        checks["new_chunks_read_back"] = new_ok
        if c_in.toc.directory_index_raw and (hi.flags & ContainerFlags.Indexed):
            try:
                old_map = {strip_mount(p): c_in.toc.chunk_ids[i].raw for p, i in c_in.paths()}
                new_map = {strip_mount(p): o.toc.chunk_ids[i].raw for p, i in o.paths()}
                lost = [p for p, r in old_map.items() if new_map.get(p) != r]
                extra = {p: r for p, r in new_map.items() if p not in old_map}
                mp = c_in.mount_point if c_in.mount_point.endswith("/") or not c_in.mount_point else c_in.mount_point + "/"
                want = {strip_mount(mp + rec["path"]): bytes.fromhex(rec["chunk_id"]) for rec in paths_added}
                checks["paths"] = {"old": len(old_map), "new": len(new_map), "old_paths_unchanged": not lost,
                                   "added_as_requested": extra == want}
                if lost:
                    failures.append(f"old paths changed: {lost[:5]}")
                if extra != want:
                    failures.append(f"added paths differ from the request: {sorted(extra)[:5]} vs {sorted(want)[:5]}")
                checks["mount_point_unchanged"] = o.mount_point == c_in.mount_point
                if not checks["mount_point_unchanged"]:
                    failures.append("mount point changed")
            except KeyRequired as e:
                failures.append(str(e))
        if header_info is not None:
            try:
                hidx = find_container_header_index(o.toc)
                ch_old = ContainerHeaderUE4.parse(c_in.read_chunk(header_info["toc_index"]))
                ch_new = ContainerHeaderUE4.parse(o.read_chunk(hidx))
                lay = header_info["layout"]
                det = ch_new.detect_layouts()
                old_e = dict(zip(ch_old.package_ids, ch_old.entries(lay)))
                new_e = dict(zip(ch_new.package_ids, ch_new.entries(lay)))
                same_old = all(new_e.get(p) == e for p, e in old_e.items())
                added_ids = sorted(p for p in ch_new.package_ids if p not in old_e)
                want_ids = sorted(int(x["package_id"], 16) for x in header_info["added_entries"])
                checks["container_header"] = {"layout": lay, "layout_still_detected": det[lay]["ok"],
                                              "other_layouts_fit": [k for k, v in det.items() if v["ok"] and k != lay],
                                              "old_entries_identical": same_old,
                                              "added": [f"0x{p:016X}" for p in added_ids],
                                              "package_count": ch_new.package_count}
                if not det[lay]["ok"] or not same_old or added_ids != want_ids or \
                        ch_new.package_count != len(ch_new.package_ids):
                    failures.append(f"container header check failed: {checks['container_header']}")
            except Exception as e:
                failures.append(f"container header unreadable in output: {type(e).__name__}: {e}")
        layout = o.toc.layout_report()
        lin = c_in.toc.layout_report()
        checks["layout"] = {k: layout[k] for k in ("shared_blocks_count", "chunks_misaligned_count",
                                                   "chunks_out_of_range_count", "orphan_blocks")}
        if layout["chunks_out_of_range_count"] or layout["shared_blocks_count"] > lin["shared_blocks_count"]:
            failures.append(f"layout problem: {checks['layout']}")
        np_ = ho.effective_partition_count
        missing = [p for p in o.partition_paths if not os.path.exists(p)]
        stray = os.path.exists(partition_path(o.utoc_path, np_))
        checks["partition_files"] = {"declared": np_, "missing": len(missing), "stray_extra": stray}
        if missing or stray:
            failures.append(f"partition files inconsistent with PartitionCount: {checks['partition_files']}")
        spans, spans_in = disk_span_report(o), disk_span_report(c_in)
        checks["disk_spans"] = spans
        worse = [k for k in ("beyond_partition_eof", "crossing_partition_size", "overlapping_block_pairs")
                 if spans[k] > spans_in[k]]
        if worse:
            failures.append(f"block disk spans got worse than the input ({worse}): {spans}")
    return {"ok": not failures, "checks": checks, "failures": failures}


# ---------------------------------------------------------------------------------------------
# clone-package: an independent copy of a package under a new name/id
# ---------------------------------------------------------------------------------------------

def _package_root_path(chunk_path: str, package_name: str) -> Tuple[str, str]:
    """Split a chunk path like ../../../PesMobile/Content/Face/f1.uasset for package /Game/Face/f1
    into (content root '../../../PesMobile/Content', extension '.uasset'): the package name minus
    its first segment (the mount root, e.g. /Game) must be the tail of the path stem."""
    pkg_rest = package_name.split("/", 2)[2] if package_name.count("/") >= 2 else ""
    if not pkg_rest:
        raise IoStoreError(f"package name {package_name!r} has no path below its root")
    low = chunk_path.lower()
    k = low.rfind("/" + pkg_rest.lower() + ".")
    if k < 0:
        raise IoStoreError(f"chunk path {chunk_path!r} does not end with the package path {pkg_rest!r} + extension")
    return chunk_path[:k], chunk_path[k + 1 + len(pkg_rest):]


def _new_chunk_path(chunk_path: str, old_name: str, new_name: str) -> str:
    root, ext = _package_root_path(chunk_path, old_name)
    if old_name.split("/")[1].lower() != new_name.split("/")[1].lower():
        raise IoStoreError(f"{old_name} and {new_name} have different roots; give the new path explicitly")
    return f"{root}/{new_name.split('/', 2)[2]}{ext}"


def _export_bundles_size_relation(c: IoStoreContainer, ch: ContainerHeaderUE4, layout: str, entries: List[StoreEntry],
                                  prefer: Sequence[int], sample: int = 32) -> dict:
    """ue4-32 only: measure how ExportBundlesSize relates to the package chunk in THIS container
    (equal to the ExportBundleData chunk size, or to the chunk size minus the Zen header)."""
    zp = _zen()
    ebd = chunk_type_code("ExportBundleData", c.toc.ue5_naming)
    idx = {cid.id: i for i, cid in enumerate(c.toc.chunk_ids) if cid.type == ebd and cid.index == 0}
    pos = {p: k for k, p in enumerate(ch.package_ids)}
    order = [p for p in prefer if p in pos] + [p for p in ch.package_ids if p not in prefer]
    counts = {"chunk_size": 0, "export_data_size": 0, "other": 0}
    evidence = []
    for p in order:
        if len(evidence) >= sample:
            break
        i = idx.get(p)
        if i is None:
            continue
        ebs, ln = entries[pos[p]].export_bundles_size, c.toc.offsets[i][1]
        if ebs == ln:
            kind = "chunk_size"
        else:
            try:
                hdr = zp.parse_legacy(c.read_chunk(i)).export_data_offset
                kind = "export_data_size" if ebs == ln - hdr else "other"
            except Exception:
                kind = "other"
        counts[kind] += 1
        evidence.append({"package_id": f"0x{p:016X}", "export_bundles_size": ebs, "chunk_size": ln, "matches": kind})
    if counts["chunk_size"] and not counts["export_data_size"] and not counts["other"]:
        rel = "chunk_size"
    elif counts["export_data_size"] and not counts["chunk_size"] and not counts["other"]:
        rel = "export_data_size"
    else:
        rel = "unknown"
    return {"relation": rel, "counts": counts, "evidence": evidence[:8]}


def clone_packages(c: IoStoreContainer, renames: Sequence[Tuple[str, str]], out_dir: str, *,
                   name_renames: Optional[Dict[str, str]] = None, store_layout: str = "auto",
                   store_order: str = "auto", export_bundles_size: str = "auto", new_paths: Optional[Dict[str, str]] = None,
                   rewire: Sequence[Tuple[str, str]] = (), rewire_containers: Sequence[IoStoreContainer] = (),
                   hash_algo: str = "auto", align: int = AES_BLOCK, unsign: bool = False, verify: bool = False,
                   allow_unverified_key: bool = False, argv: Optional[List[str]] = None,
                   key_source: str = "none", extra_inputs: Sequence[str] = (),
                   allow_toc_trailing: bool = False) -> dict:
    """Copy each package OLD -> NEW (UE4.26/4.27 Zen packages): its ExportBundleData chunk is
    renamed with zen_package.rename_legacy_package (see there for the exact field list), its other
    chunks with the same package id (BulkData / OptionalBulkData / MemoryMappedBulkData) are copied
    verbatim under the new id (same ChunkIndex and type, same meta flags minus Compressed), each
    gets the path of its source with the package part replaced, and the package is registered in
    the container header with a copy of the source's store entry. Packages cloned in the same run
    are rewired to each other: import-map hashes of their public exports, graph-data package ids
    and store-entry ImportedPackages that pointed at a source now point at its clone. Nothing
    else in the container changes; in particular every package that imported a source keeps
    importing the source (that is the point: the donor stays shared, the clone is private).

    `rewire` (OLD=NEW pairs cloned EARLIER, e.g. textures cloned in pc3611 before the mesh in
    pc3600) extends the rewiring to packages outside this run: both OLD and NEW must be readable from
    this container or one of `rewire_containers`; their public exports are matched index by index
    (a clone keeps the export order), giving the import-hash and package-id maps."""
    zp = _zen()
    if not renames:
        raise IoStoreError("no package to clone")
    name_renames = dict(name_renames or {})
    new_paths = dict(new_paths or {})
    naming = c.toc.chunk_type_naming()
    if not naming["certain"]:
        raise IoStoreError(f"cannot tell which chunk-type numbering this container uses ({naming['source']}); "
                           f"clone-package needs it to find the ExportBundleData chunks")
    ue5 = naming["ue5"]
    ebd = chunk_type_code("ExportBundleData", ue5)
    olds = [o for o, _ in renames]
    news = [n for _, n in renames]
    old_ids = [package_id_from_name(o) for o in olds]
    new_ids = [package_id_from_name(n) for n in news]
    if len(set(old_ids)) != len(old_ids) or len(set(new_ids)) != len(new_ids):
        raise IoStoreError("a package appears twice in the clone list")
    if set(old_ids) & set(new_ids):
        raise IoStoreError("a new package name hashes to the id of a source package")
    for n in news:
        if not n.isascii():
            raise IoStoreError(f"new package name {n!r} is not ASCII; UE4's lower-casing of non-ASCII "
                               f"characters for FPackageId is not verified - use an ASCII name")
    existing_ids = {cid.id for cid in c.toc.chunk_ids}
    for n, nid in zip(news, new_ids):
        if nid in existing_ids:
            raise IoStoreError(f"{n} (id 0x{nid:016X}) already has chunks in this container")
        for other in rewire_containers:   # the runtime package store is global: an id must be unique
            if any(cid.id == nid for cid in other.toc.chunk_ids):
                raise IoStoreError(f"{n} (id 0x{nid:016X}) already has chunks in {other.utoc_path}")
    hidx = find_container_header_index(c.toc)
    if hidx is None:
        raise IoStoreError("no ContainerHeader chunk in this container; cannot register clones")
    ch = ContainerHeaderUE4.parse(c.read_chunk(hidx))
    layout, layout_ev = ch.resolve_layout(store_layout)
    entries = ch.entries(layout)
    pos = {p: k for k, p in enumerate(ch.package_ids)}
    for n, nid in zip(news, new_ids):
        if nid in pos:
            raise IoStoreError(f"{n} (id 0x{nid:016X}) is already registered in the container header")
    paths = c.path_by_index()
    pkg_id_map = dict(zip(old_ids, new_ids))
    gidx_map: Dict[int, int] = {}
    rewire_info = []
    for old, new in rewire:
        oid, nid = package_id_from_name(old), package_id_from_name(new)
        if oid in pkg_id_map or nid in new_ids:
            raise IoStoreError(f"--rewire {old}={new} overlaps the packages cloned in this run")

        def find(pid, label):
            for cc in [c] + list(rewire_containers):
                t = chunk_type_code("ExportBundleData", cc.toc.ue5_naming)
                for i, cid in enumerate(cc.toc.chunk_ids):
                    if cid.id == pid and cid.type == t and cid.index == 0:
                        try:
                            return zp.parse_legacy(cc.read_chunk(i)), cc.utoc_path
                        except zp.ZenFormatError as e:
                            raise IoStoreError(f"--rewire {label}: not a UE4.26/4.27 package ({e})") from None
            raise IoStoreError(f"--rewire {old}={new}: {label} (0x{pid:016X}) not found in this container or the "
                               f"--rewire-with containers")
        (po, src_o), (pn, src_n) = find(oid, old), find(nid, new)
        if zp.lower_ascii(pn.package_name) != zp.lower_ascii(new) or len(po.exports) != len(pn.exports):
            raise IoStoreError(f"--rewire {old}={new}: {new} is not a clone of {old} (name/export count differ)")
        for a, b in zip(po.exports, pn.exports):
            ga, gb = a.global_import_index_or_hash, b.global_import_index_or_hash
            if (ga == zp.NULL_OBJECT_INDEX) != (gb == zp.NULL_OBJECT_INDEX):
                raise IoStoreError(f"--rewire {old}={new}: export {a.index} is public in only one of them")
            if ga != zp.NULL_OBJECT_INDEX:
                gidx_map[ga] = gb
        pkg_id_map[oid] = nid
        rewire_info.append({"old": old, "new": new, "old_found_in": src_o, "new_found_in": src_n,
                            "public_exports_mapped": sum(1 for e in po.exports
                                                         if e.global_import_index_or_hash != zp.NULL_OBJECT_INDEX)})

    # pass 1: parse sources, compute the export GlobalImportIndex maps
    sources = []
    renames_used = set()
    for (old, new), oid, nid in zip(renames, old_ids, new_ids):
        chunks = [i for i, cid in enumerate(c.toc.chunk_ids) if cid.id == oid]
        main = [i for i in chunks if c.toc.chunk_ids[i].type == ebd and c.toc.chunk_ids[i].index == 0]
        if len(main) != 1:
            raise IoStoreError(f"{old} (id 0x{oid:016X}): expected one ExportBundleData chunk, found {len(main)}")
        data = c.read_chunk(main[0])
        try:
            pkg = zp.parse_legacy(data)
        except zp.ZenFormatError as e:
            raise IoStoreError(f"{old}: not a UE4.26/4.27 Zen package ({e}); clone-package supports only that layout") from None
        if package_id_from_name(pkg.package_name) != oid:
            raise IoStoreError(f"{old}: the package header names {pkg.package_name!r} (id "
                               f"0x{package_id_from_name(pkg.package_name):016X}), which does not match the chunk id")
        mine = {o: n for o, n in name_renames.items() if o in pkg.names}
        renames_used |= set(mine)
        try:
            _, rep0 = zp.rename_legacy_package(data, new, old_name=old, name_renames=mine)
        except zp.RenameError as e:
            raise IoStoreError(f"{old}: {e}") from None
        for k, v in rep0["global_import_index_map"].items():
            ki, vi = int(k, 16), int(v, 16)
            if gidx_map.get(ki, vi) != vi:
                raise IoStoreError("conflicting GlobalImportIndex mapping between cloned packages")
            gidx_map[ki] = vi
        if oid not in pos:
            raise IoStoreError(f"{old}: no store entry in this container's header; cannot copy it")
        sources.append({"old": old, "new": new, "old_id": oid, "new_id": nid, "chunks": chunks, "main": main[0],
                        "data": data, "pkg": pkg, "name_renames": mine})
    unused = set(name_renames) - renames_used
    if unused:
        hints = []
        for o in sorted(unused):
            for s in sources:
                e = next((e for e in s["pkg"].exports if e.object_name == o and e.name_ref[1]), None)
                if e is not None:
                    hints.append(f"{o!r} in {s['old']} is name-map entry {s['pkg'].names[e.name_ref[0]]!r} with FName "
                                 f"number {e.name_ref[1]}; --rename-name works on entry text only")
                    break
        raise IoStoreError(f"--rename-name entries not found in any cloned package: {sorted(unused)}"
                           + (f" ({'; '.join(hints)})" if hints else ""))

    ebs_info = None
    if layout == "ue4-32":
        ebs_info = _export_bundles_size_relation(c, ch, layout, entries, old_ids)

    # pass 2: rename with the rewiring maps, build chunks and store entries
    new_chunks: List[NewChunk] = []
    store_items: List[Tuple[int, StoreEntry]] = []
    reports = []
    for s in sources:
        new_data, rep = zp.rename_legacy_package(s["data"], s["new"], old_name=s["old"], import_index_map=gidx_map,
                                                 package_id_map=pkg_id_map, name_renames=s["name_renames"])
        src_entry = entries[pos[s["old_id"]]]
        if src_entry.export_count != len(s["pkg"].exports):
            rep["warnings"].append(f"store entry ExportCount {src_entry.export_count} != {len(s['pkg'].exports)} "
                                   f"exports in the package header")
        if src_entry.export_bundle_count != len(s["pkg"].export_bundles):
            rep["warnings"].append(f"store entry ExportBundleCount {src_entry.export_bundle_count} != "
                                   f"{len(s['pkg'].export_bundles)} bundles in the package header")
        e = StoreEntry(src_entry.export_count, src_entry.export_bundle_count,
                       [pkg_id_map.get(x, x) for x in src_entry.imported_packages])
        if layout == "ue4-32":
            old_len, new_len = len(s["data"]), len(new_data)
            npk = zp.parse_legacy(new_data)
            if export_bundles_size not in ("auto", "copy", "chunk-size", "export-data-size"):
                if len(sources) != 1:
                    raise IoStoreError("a numeric --export-bundles-size is only allowed when cloning one package")
                ebs, how = int(export_bundles_size, 0), "given"
            elif export_bundles_size == "copy" or (export_bundles_size == "auto" and old_len == new_len and
                                                     s["pkg"].export_data_offset == npk.export_data_offset):
                ebs, how = src_entry.export_bundles_size, ("copied (--export-bundles-size copy)"
                                                           if export_bundles_size == "copy"
                                                           else "copied (package size unchanged)")
            elif export_bundles_size == "chunk-size" or (export_bundles_size == "auto" and
                                                         ebs_info["relation"] == "chunk_size"):
                ebs, how = new_len, "new ExportBundleData chunk size"
            elif export_bundles_size == "export-data-size" or (export_bundles_size == "auto" and
                                                               ebs_info["relation"] == "export_data_size"):
                ebs, how = new_len - npk.export_data_offset, "new export data size"
            else:
                raise IoStoreError(f"{s['old']}: the package size changed ({old_len} -> {new_len}) and the meaning of "
                                   f"ExportBundlesSize could not be measured in this container ({ebs_info}); pass "
                                   f"--export-bundles-size copy|chunk-size|export-data-size|N")
            e.export_bundles_size, e.load_order, e.pad = ebs, src_entry.load_order, 0
            rep["store_entry_export_bundles_size"] = {"old": src_entry.export_bundles_size, "new": ebs, "rule": how}
            rep["store_entry_load_order"] = {"copied": src_entry.load_order}
        store_items.append((s["new_id"], e))
        importers = [f"0x{p:016X}" for p, en in zip(ch.package_ids, entries) if s["old_id"] in en.imported_packages]
        rep["packages_in_this_container_importing_the_source"] = {"count": len(importers), "first": importers[:20],
                                                                  "note": "unchanged: they keep using the donor"}
        rep["store_entry"] = {"source": src_entry.to_json(), "clone": e.to_json()}
        rep["chunks"] = []
        for i in s["chunks"]:
            cid = c.toc.chunk_ids[i]
            src_path = paths.get(i)
            key = f"{s['old']}|{cid.type_name(ue5)}"
            if key in new_paths:
                path = new_paths[key]
            elif s["new"] in new_paths and cid.type == ebd:
                path = new_paths[s["new"]]
            elif src_path:
                path = _new_chunk_path(src_path, s["old"], s["new"])
            else:
                path = None
                rep["warnings"].append(f"source chunk {cid.hex} has no path; the clone gets none either "
                                       f"(pass --path {s['new']}=... for the package chunk)")
            data = new_data if i == s["main"] else c.read_chunk(i)
            flags = c.toc.metas[i].flags if c.toc.metas is not None else 0
            nc = NewChunk(ChunkId.from_parts(s["new_id"], cid.index, cid.type), data, path,
                          flags & ~int(MetaFlags.Compressed) & 0xFF, f"clone of {cid.hex} ({s['old']})")
            new_chunks.append(nc)
            rep["chunks"].append({"source_chunk_id": cid.hex, "source_path": src_path, "new_chunk_id": nc.chunk_id.hex,
                                  "new_path": path, "type_name": cid.type_name(ue5), "size": len(data),
                                  "content": "renamed package header" if i == s["main"] else "copied byte for byte"})
        reports.append(rep)
    extra = {"clone": {"packages": reports, "store_entry_layout": layout, "store_entry_layout_evidence": layout_ev,
                       "export_bundles_size_measurement": ebs_info,
                       "package_id_map": {f"0x{k:016X}": f"0x{v:016X}" for k, v in pkg_id_map.items()},
                       "import_index_map_size": len(gidx_map), "rewire": rewire_info}}
    return add_to_container(c, new_chunks, out_dir, store_entries=store_items, store_layout=layout,
                            store_order=store_order, hash_algo=hash_algo, align=align, unsign=unsign, verify=verify,
                            allow_unverified_key=allow_unverified_key, argv=argv, key_source=key_source,
                            operation="clone-package", extra_manifest=extra, extra_inputs=extra_inputs,
                            allow_toc_trailing=allow_toc_trailing)


# ---------------------------------------------------------------------------------------------
# find-refs: who uses a donor package? (read-only; the first thing to run on real data)
# ---------------------------------------------------------------------------------------------

def find_references(c: IoStoreContainer, query: str, limit: int = 0) -> dict:
    """Where does a package (name or 0x id) or a text (e.g. a face number like "110718") appear?

    * paths in the directory index containing the text (case-insensitive);
    * package store entries whose ImportedPackages contain the package id;
    * UE4.26/4.27 package headers: name-map entries containing the text (e.g. soft object paths or
      data-table rows naming the package) and full FNames of the header (package name, export object
      names: a trailing _<number> is stored as an FName number, not in the entry text), import-map
      entries equal to the GlobalImportIndex of a public export of the package (= packages that
      hard-reference it). FNames inside serialized export data are not decoded: a number stored as
      an FName number there is invisible to the text search.
    Packages are read and decoded one by one (`limit` caps how many); undecodable ones are counted."""
    zp = _zen()
    ue5 = c.toc.ue5_naming
    ebd = chunk_type_code("ExportBundleData", ue5)
    q = query.strip()
    if not q:
        raise IoStoreError("find-refs needs a package name, a 0x id or a text")
    pid = None
    if q.lower().startswith("0x") or (q.startswith("/") and len(q) > 1):
        pid = _pid_arg(q)
    text = q.lower() if not q.lower().startswith("0x") else None
    res: Dict[str, object] = {"query": q, "package_id": f"0x{pid:016X}" if pid is not None else None}
    paths = c.path_by_index()
    if text:
        res["paths"] = sorted(p for p in paths.values() if text in p.lower())
    if pid is not None:
        res["chunks_of_package"] = [{"index": i, "chunk_id": cid.hex, "type": cid.type_name(ue5), "path": paths.get(i)}
                                    for i, cid in enumerate(c.toc.chunk_ids) if cid.id == pid]
    hidx = find_container_header_index(c.toc)
    if hidx is not None and pid is not None:
        try:
            ch = ContainerHeaderUE4.parse(c.read_chunk(hidx))
            layout, _ = ch.resolve_layout("auto")
            ents = ch.entries(layout)
            res["store_entry_layout"] = layout
            res["registered_in_container_header"] = pid in ch.package_ids
            res["store_entries_importing_it"] = [f"0x{p:016X}" for p, e in zip(ch.package_ids, ents)
                                                 if pid in e.imported_packages]
        except IoStoreError as e:
            res["container_header_error"] = str(e)
    gidx = set()
    pkg_chunks = [i for i, cid in enumerate(c.toc.chunk_ids) if cid.type == ebd and cid.index == 0]
    if pid is not None:
        own = [i for i in pkg_chunks if c.toc.chunk_ids[i].id == pid]
        if own:
            try:
                p0 = zp.parse_legacy(c.read_chunk(own[0]))
                gidx = {e.global_import_index_or_hash for e in p0.exports
                        if e.global_import_index_or_hash != zp.NULL_OBJECT_INDEX}
                res["package_name_in_header"] = p0.package_name
            except Exception as e:  # reported, not fatal
                res["own_package_error"] = f"{type(e).__name__}: {e}"
    name_hits, import_hits, graph_hits = [], [], []
    scanned = unparsed = 0
    for i in pkg_chunks[:limit or None]:
        try:
            p = zp.parse_legacy(c.read_chunk(i))
        except Exception:
            unparsed += 1
            continue
        scanned += 1
        who = {"chunk_id": c.toc.chunk_ids[i].hex, "package": p.package_name, "path": paths.get(i)}
        if text:
            hits = [n for n in p.names if text in n.lower()]
            # Unreal stores "face_110718" as entry "face" + FName number 110719, so a number is only
            # visible in the FULL names: the package name and the export object names of the header
            full = sorted({x for x in [p.package_name] + [e.object_name for e in p.exports]
                           if text in x.lower() and x not in hits})
            if hits or full:
                name_hits.append({**who, "names": hits[:10], "header_fnames": full[:10]})
        if gidx and c.toc.chunk_ids[i].id != pid:
            k = [f"0x{x:016X}" for x in p.imports if x in gidx]
            if k:
                import_hits.append({**who, "imports": k})
        if pid is not None and any(g["package_id"] == pid for g in p.imported_packages):
            graph_hits.append(who)
    res.update(packages_scanned=scanned, packages_unparsed=unparsed, name_map_hits=name_hits,
               import_map_hits=import_hits, graph_data_hits=graph_hits)
    return res


def cmd_find_refs(args) -> int:
    c, _ = _open(args)
    with c:
        _print_json(find_references(c, args.query, args.limit))
    return 0


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


def _pid_arg(value) -> int:
    """FPackageId from a package name, or a 0x-prefixed hex id, or an int."""
    if isinstance(value, int) and not isinstance(value, bool):
        if not 0 <= value <= MASK64:
            raise IoStoreError(f"{value}: an FPackageId is an unsigned 64-bit value")
        return value
    v = str(value).strip()
    if v.lower().startswith("0x"):
        try:
            x = int(v, 16)
        except ValueError:
            raise IoStoreError(f"{value!r}: not a hex FPackageId") from None
        return _pid_arg(x)
    if v.startswith("/") and len(v) > 1:
        return package_id_from_name(v)
    raise IoStoreError(f"{value!r}: expected a package name (/Root/...) or a 0x-hex FPackageId")


def load_add_list(path: str, ue5: bool, naming_certain: bool = True
                  ) -> Tuple[List[NewChunk], List[Tuple[int, StoreEntry]], List[str]]:
    """JSON: {"chunks": [...], "store_entries": [...]} (or just the chunk list). Chunk:
    {"file": F, "chunk_id": HEX24 | "package": NAME | "package_id": 0xHEX, "type": TYPE, "chunk_index": 0,
     "path": P, "meta_flags": 0}. Store entry: {"package": NAME | "package_id": 0xHEX, "export_count": N,
     "export_bundle_count": N, "imported_packages": [NAME | 0xHEX ...], "export_bundles_size": N,
     "load_order": N} (the last two only for the ue4-32 layout). Relative files are relative to the JSON.
    A type given by NAME needs a certain chunk-type naming (TocResource.chunk_type_naming); a numeric
    type or a full chunk_id is always accepted."""
    try:
        with open(path, encoding="utf-8") as f:
            spec = json.load(f)
    except (ValueError, UnicodeDecodeError) as e:
        raise IoStoreError(f"{path}: not valid JSON ({e})") from None
    if isinstance(spec, list):
        spec = {"chunks": spec}
    if not isinstance(spec, dict):
        raise IoStoreError(f"{path}: expected a JSON object with 'chunks' / 'store_entries'")
    base = os.path.dirname(os.path.abspath(path))
    chunks, files = [], [path]
    try:
        for k, e in enumerate(spec.get("chunks", [])):
            where = f"{path}: chunks[{k}]"
            if not isinstance(e, dict) or "file" not in e:
                raise IoStoreError(f"{where}: needs at least a \"file\"")
            fpath = e["file"] if os.path.isabs(e["file"]) else os.path.join(base, e["file"])
            with open(fpath, "rb") as f:
                data = f.read()
            files.append(fpath)
            if "chunk_id" in e:
                cid = ChunkId.from_hex(e["chunk_id"])
            else:
                pid = _pid_arg(e.get("package") or e.get("package_id"))
                t = e.get("type", "ExportBundleData")
                if not naming_certain and not (isinstance(t, int) or str(t).strip().isdigit()):
                    raise IoStoreError(f"{where}: chunk type {t!r} given by name, but this TOC's chunk-type numbering "
                                       f"is not certain (TOC v4 without a ContainerHeader chunk); give the type as a "
                                       f"number or a full chunk_id")
                idx = int(e.get("chunk_index", 0))
                tcode = chunk_type_code(t, ue5)
                if not 0 <= idx <= 0xFFFF or not 0 <= tcode <= 0xFF:
                    raise IoStoreError(f"{where}: chunk_index must fit 16 bits and the type 8 bits")
                cid = ChunkId.from_parts(pid, idx, tcode)
            chunks.append(NewChunk(cid, data, e.get("path"), int(e.get("meta_flags", 0)), e.get("label", fpath)))
        entries = []
        for k, e in enumerate(spec.get("store_entries", [])):
            if not isinstance(e, dict):
                raise IoStoreError(f"{path}: store_entries[{k}] must be an object")
            entries.append((_pid_arg(e.get("package") or e.get("package_id")), StoreEntry.from_json(e)))
    except (KeyError, TypeError, ValueError) as e:
        raise IoStoreError(f"{path}: malformed add list ({type(e).__name__}: {e})") from None
    return chunks, entries, files


def cmd_add(args) -> int:
    c, src = _open(args)
    with c:
        naming = c.toc.chunk_type_naming()
        chunks, entries, files = load_add_list(args.add_list, naming["ue5"], naming["certain"])
        m = add_to_container(c, chunks, args.out_dir, store_entries=entries, store_layout=args.store_entry_layout,
                             store_order=args.store_order, hash_algo=args.hash_algo, align=args.align, unsign=args.unsign, verify=args.verify,
                             allow_unverified_key=args.allow_unverified_key, extra_inputs=files, argv=sys.argv,
                             key_source=src, max_seed=args.max_seed, allow_toc_trailing=args.keep_toc_trailing)
        for w in m["warnings"]:
            print("WARNING:", w, file=sys.stderr)
        print(json.dumps({"out_dir": os.path.abspath(args.out_dir),
                          "added": [(r["index"], r["chunk_id"], r["path"]) for r in m["added_chunks"]],
                          "container_header": (m["container_header"] or {}).get("added_entries"),
                          "perfect_hash": m["perfect_hash"],
                          "verify": m.get("verify", {}).get("ok") if args.verify else "not run (use --verify)"},
                         indent=2))
    return 0


def _pairs(values, what) -> List[Tuple[str, str]]:
    out = []
    for v in values or []:
        if "=" not in v:
            raise IoStoreError(f"{what} needs OLD=NEW, got {v!r}")
        a, b = v.split("=", 1)
        out.append((a.strip(), b.strip()))
    return out


def cmd_clone_package(args) -> int:
    c, src = _open(args)
    others = [IoStoreContainer(p, c.aes_key, c.decompressor) for p in (args.rewire_with or [])]
    try:
        m = clone_packages(c, _pairs(args.package, "--package"), args.out_dir,
                           name_renames=dict(_pairs(args.rename_name, "--rename-name")),
                           store_layout=args.store_entry_layout, store_order=args.store_order,
                           export_bundles_size=args.export_bundles_size,
                           new_paths=dict(_pairs(args.path, "--path")), rewire=_pairs(args.rewire, "--rewire"),
                           rewire_containers=others, hash_algo=args.hash_algo, align=args.align,
                           unsign=args.unsign, verify=args.verify, allow_unverified_key=args.allow_unverified_key,
                           argv=sys.argv, key_source=src,
                           extra_inputs=[f for o in others for f in o.input_files()],
                           allow_toc_trailing=args.keep_toc_trailing)
    finally:
        c.close()
        for o in others:
            o.close()
    if m:
        for w in m["warnings"] + [w for p in m["clone"]["packages"] for w in p["warnings"]]:
            print("WARNING:", w, file=sys.stderr)
        print(json.dumps({"out_dir": os.path.abspath(args.out_dir),
                          "packages": [{"old": p["old_package_name"], "new": p["new_package_name"],
                                        "new_package_id": p["new_package_id"],
                                        "chunks": [(x["new_chunk_id"], x["new_path"]) for x in p["chunks"]]}
                                       for p in m["clone"]["packages"]],
                          "store_entry_layout": m["clone"]["store_entry_layout"],
                          "verify": m.get("verify", {}).get("ok") if args.verify else "not run (use --verify)"},
                         indent=2))
    return 0


def cmd_container_header(args) -> int:
    if not args.raw and not args.utoc:
        raise IoStoreError("give a .utoc or --raw HEADER.bin")
    if args.raw:
        with open(args.raw, "rb") as f:
            raw = f.read()
        origin = {"raw_file": os.path.abspath(args.raw)}
    else:
        c, _ = _open(args)
        with c:
            i = find_container_header_index(c.toc)
            if i is None:
                raise IoStoreError("no ContainerHeader chunk in this container")
            raw = c.read_chunk(i)
            origin = {"container": c.utoc_path, "toc_index": i, "chunk_id": c.toc.chunk_ids[i].hex,
                      "toc_container_id": f"0x{c.header.container_id:016X}"}
    ch = ContainerHeaderUE4.parse(raw)
    out = {**origin, "size": len(raw), "reserialize_identical": ch.serialize() == raw}
    try:
        layout, ev = ch.resolve_layout(args.layout)
        out.update(ch.summary(layout, limit=len(ch.package_ids) if args.all else 10))
    except IoStoreError as e:
        out.update(ch.summary(None))
        out["layout_error"] = str(e)
    _print_json(out)
    return 0 if "layout_error" not in out else 1


def cmd_package_id(args) -> int:
    rows = []
    for name in args.names:
        pid = package_id_from_name(name)
        types = PACKAGE_CHUNK_TYPES_UE5 if args.ue5 else PACKAGE_CHUNK_TYPES_UE4
        rows.append({"name": name, "package_id": f"0x{pid:016X}", "package_id_decimal": str(pid),
                     "chunk_ids": {t: ChunkId.from_parts(pid, 0, v).hex for t, v in types.items()}})
    _print_json(rows)
    return 0


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

    def writer_opts(sp):
        sp.add_argument("--out-dir", required=True, help="new (or empty) directory; output keeps the input file names")
        sp.add_argument("--store-entry-layout", default="auto", choices=("auto",) + tuple(STORE_ENTRY_LAYOUTS),
                        help="FPackageStoreEntry layout of the container header (auto = measured)")
        sp.add_argument("--store-order", default="auto", choices=("auto", "append", "sorted"),
                        help="where new PackageIds go (auto: keep an ascending list sorted, else append)")
        sp.add_argument("--unsign", action="store_true", help="allow writing a Signed container by dropping signatures")
        sp.add_argument("--hash-algo", default="auto", choices=("auto",) + HASH_ALGOS)
        sp.add_argument("--align", type=int, default=AES_BLOCK, help="alignment of new chunk data in the .ucas")
        sp.add_argument("--allow-unverified-key", action="store_true")
        sp.add_argument("--keep-toc-trailing", action="store_true",
                        help="copy unparsed bytes after the TOC meta verbatim (refused by default: if they are "
                             "per-chunk data they stop matching once chunks are added)")
        sp.add_argument("--verify", action="store_true", help="re-read the output and compare with the input")

    sp = sub.add_parser("add", help="append NEW chunks / paths / package store entries -> new container")
    common(sp)
    writer_opts(sp)
    sp.add_argument("--add-list", required=True, help="JSON with chunks and store_entries (see load_add_list)")
    sp.add_argument("--max-seed", type=int, default=1 << 20, help="perfect-hash seed search limit (TOC v4+)")
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("clone-package", help="copy package(s) under new names/ids (independent copy)")
    common(sp)
    writer_opts(sp)
    sp.add_argument("--package", action="append", required=True, metavar="OLD=NEW",
                    help="/Game/Old/Pkg=/Game/New/Pkg; repeat to clone several packages and rewire them to each other")
    sp.add_argument("--rename-name", action="append", metavar="OLD=NEW",
                    help="also rename this exact name-map entry (e.g. the main export's object name)")
    sp.add_argument("--path", action="append", metavar="NEWPKG=PATH",
                    help="directory-index path of the new package's .uasset chunk (default: derived from the source)")
    sp.add_argument("--export-bundles-size", default="auto",
                    help="ue4-32 layout only: auto | copy | chunk-size | export-data-size | N")
    sp.add_argument("--rewire", action="append", metavar="OLD=NEW",
                    help="a package cloned EARLIER (e.g. a texture in pc3611): imports of OLD become imports of NEW")
    sp.add_argument("--rewire-with", action="append", metavar="OTHER.utoc",
                    help="container(s) holding the --rewire packages (opened with the same AES key)")
    sp.set_defaults(func=cmd_clone_package)

    sp = sub.add_parser("container-header", help="decode the container header / package store (UE4.26 layouts)")
    sp.add_argument("utoc", nargs="?", help="container (or use --raw)")
    sp.add_argument("--raw", help="a raw ContainerHeader chunk file instead of a container")
    sp.add_argument("--layout", default="auto", choices=("auto",) + tuple(STORE_ENTRY_LAYOUTS))
    sp.add_argument("--all", action="store_true", help="list every entry (default: first 10)")
    sp.add_argument("--aes-key-file")
    sp.add_argument("--oodle-lib")
    sp.set_defaults(func=cmd_container_header)

    sp = sub.add_parser("find-refs", help="read-only: paths / store entries / package headers that mention a "
                                          "package (name or 0x id) or a text such as a face number")
    common(sp)
    sp.add_argument("query", help="/Game/... package name, 0x<FPackageId>, or plain text (case-insensitive)")
    sp.add_argument("--limit", type=int, default=0, help="decode at most N package headers (0 = all)")
    sp.set_defaults(func=cmd_find_refs)

    sp = sub.add_parser("package-id", help="FPackageId (CityHash64 of the lower-cased UTF-16 name) and chunk ids")
    sp.add_argument("names", nargs="+")
    sp.add_argument("--ue5", action="store_true", help="UE5 chunk type numbering")
    sp.set_defaults(func=cmd_package_id)

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
