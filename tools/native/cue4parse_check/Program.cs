// Cue4ParseCheck: open an IoStore container with CUE4Parse (third-party reference reader) and
// dump header facts plus SHA-256 of every chunk as JSON on stdout.
//
// Usage:
//   Cue4ParseCheck <file.utoc> [--game GAME_eFootball] [--aes-key-file PATH] [--extract-dir DIR]
//
// The AES key (64 hex chars, optional 0x prefix) is read from --aes-key-file or the environment
// variable R9_AES_KEY_HEX. It is never printed.
//
// What is exercised in CUE4Parse:
//   * FIoStoreTocResource (header, chunk ids, offsets, perfect-hash seeds, compression blocks,
//     method names, signatures skip, directory index; TOC meta via a separate ReadTocMeta parse)
//   * IoStoreReader.Mount (directory index decrypt + walk -> file paths)
//   * IoStoreReader.Extract(FIoStoreEntry) for every TOC entry (block decrypt + decompress)
//   * IoStoreReader.TryResolve(FIoChunkId) for every chunk id (perfect hash lookup)
// Exit code 0 = everything read; 2 = usage error; 1 = a read failed (details in JSON "errors").

using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using CUE4Parse.Encryption.Aes;
using CUE4Parse.UE4.IO;
using CUE4Parse.UE4.IO.Objects;
using CUE4Parse.UE4.Readers;
using CUE4Parse.UE4.Versions;

internal static class Program
{
    private static int Main(string[] args)
    {
        string? utoc = null, keyFile = null, extractDir = null;
        var gameName = "GAME_eFootball";
        for (var i = 0; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--game": gameName = args[++i]; break;
                case "--aes-key-file": keyFile = args[++i]; break;
                case "--extract-dir": extractDir = args[++i]; break;
                default:
                    if (utoc != null || args[i].StartsWith("--"))
                    {
                        Console.Error.WriteLine($"unexpected argument {args[i]}");
                        return 2;
                    }
                    utoc = args[i];
                    break;
            }
        }
        if (utoc == null)
        {
            Console.Error.WriteLine("usage: Cue4ParseCheck <file.utoc> [--game GAME_x] [--aes-key-file PATH] [--extract-dir DIR]");
            return 2;
        }
        if (!Enum.TryParse<EGame>(gameName, out var game))
        {
            Console.Error.WriteLine($"unknown EGame {gameName}");
            return 2;
        }

        CUE4Parse.Globals.LogVfsMounts = false;
        var errors = new List<string>();
        var ms = new MemoryStream();
        using (var w = new Utf8JsonWriter(ms, new JsonWriterOptions { Indented = true }))
        {
            w.WriteStartObject();
            w.WriteString("reader", "CUE4Parse IoStoreReader");
            w.WriteString("game", game.ToString());
            w.WriteString("utoc", Path.GetFullPath(utoc));

            var versions = new VersionContainer(game);
            // Directory index for Mount(): ReadDirectoryIndex (CUE4Parse's default).
            var reader = new IoStoreReader(utoc, EIoStoreTocReadOptions.ReadDirectoryIndex, versions);
            var toc = reader.TocResource;
            var h = toc.Header;
            // TOC meta: a second parse with ReadTocMeta only. With ReadAll, FIoStoreTocResource records
            // DirectoryIndexBufferOffset but does not skip DirectoryIndexSize, so ChunkMetas would be read
            // from the directory index bytes (observed with CUE4Parse 77b94fa).
            var metaToc = new FIoStoreTocResource(new FByteArchive(utoc, File.ReadAllBytes(utoc), versions),
                EIoStoreTocReadOptions.ReadTocMeta);

            w.WriteStartObject("header");
            w.WriteNumber("version", (int) h.Version);
            w.WriteString("version_name", h.Version.ToString());
            w.WriteNumber("entry_count", h.TocEntryCount);
            w.WriteNumber("compressed_block_entry_count", h.TocCompressedBlockEntryCount);
            w.WriteNumber("compression_block_size", h.CompressionBlockSize);
            w.WriteNumber("directory_index_size", h.DirectoryIndexSize);
            w.WriteNumber("partition_count", h.PartitionCount);
            w.WriteNumber("partition_size", h.PartitionSize);
            w.WriteString("container_flags", h.ContainerFlags.ToString());
            w.WriteNumber("container_id", h.ContainerId.Id);
            w.WriteNumber("perfect_hash_seeds", toc.ChunkPerfectHashSeeds?.Length ?? 0);
            w.WriteNumber("chunks_without_perfect_hash", toc.ChunkIndicesWithoutPerfectHash?.Length ?? 0);
            w.WriteStartArray("compression_methods");
            foreach (var m in toc.CompressionMethods) w.WriteStringValue(m.ToString());
            w.WriteEndArray();
            w.WriteEndObject();

            if (reader.IsEncrypted)
            {
                var hex = keyFile != null ? File.ReadAllText(keyFile).Trim() : Environment.GetEnvironmentVariable("R9_AES_KEY_HEX")?.Trim();
                if (string.IsNullOrEmpty(hex))
                {
                    Console.Error.WriteLine("container is encrypted: provide --aes-key-file or R9_AES_KEY_HEX");
                    return 2;
                }
                reader.AesKey = new FAesKey(hex.StartsWith("0x") ? hex : "0x" + hex);
                w.WriteBoolean("aes_key_accepted", reader.TestAesKey(reader.AesKey));
            }

            // Directory index -> paths
            var pathByIndex = new Dictionary<uint, string>();
            try
            {
                if (reader.HasDirectoryIndex)
                {
                    reader.Mount(StringComparer.Ordinal);
                    w.WriteString("mount_point", reader.MountPoint);
                    foreach (var (path, gameFile) in reader.Files)
                    {
                        if (gameFile is FIoStoreEntry e)
                        {
                            // FIoChunkId.Equals ignores the chunk-index bytes, so match on offset/length too
                            var idx = uint.MaxValue;
                            for (uint i = 0; i < toc.ChunkIds.Length; i++)
                            {
                                if ((long) toc.ChunkOffsetLengths[i].Offset == e.Offset &&
                                    (long) toc.ChunkOffsetLengths[i].Length == e.Size &&
                                    toc.ChunkIds[i].ChunkId == e.ChunkId.ChunkId &&
                                    toc.ChunkIds[i].ChunkType == e.ChunkId.ChunkType)
                                {
                                    idx = i;
                                    break;
                                }
                            }
                            pathByIndex[idx] = path;
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                errors.Add($"mount: {ex.GetType().Name}: {ex.Message}");
            }

            w.WriteStartArray("chunks");
            for (uint i = 0; i < toc.ChunkIds.Length; i++)
            {
                var id = toc.ChunkIds[i];
                var raw = new byte[12];
                BitConverter.TryWriteBytes(raw.AsSpan(0, 8), id.ChunkId);
                BitConverter.TryWriteBytes(raw.AsSpan(8, 2), id._chunkIndex);
                raw[10] = id._padding;
                raw[11] = id.ChunkType;

                w.WriteStartObject();
                w.WriteNumber("index", i);
                w.WriteString("chunk_id", Convert.ToHexString(raw).ToLowerInvariant());
                w.WriteNumber("chunk_type", id.ChunkType);
                w.WriteNumber("offset", toc.ChunkOffsetLengths[i].Offset);
                w.WriteNumber("length", toc.ChunkOffsetLengths[i].Length);
                if (pathByIndex.TryGetValue(i, out var p)) w.WriteString("path", p);
                if (metaToc.ChunkMetas != null)
                {
                    // CUE4Parse keeps the first 20 bytes (FSHAHash) of FIoChunkHash / FIoHash
                    w.WriteString("meta_hash20", metaToc.ChunkMetas[i].ChunkHash.ToString().ToLowerInvariant());
                    w.WriteNumber("meta_flags", (int) metaToc.ChunkMetas[i].Flags);
                }

                try
                {
                    var entry = new FIoStoreEntry(reader, i);
                    var data = reader.Extract(entry);
                    w.WriteNumber("size", data.Length);
                    w.WriteString("sha256", Convert.ToHexString(SHA256.HashData(data)).ToLowerInvariant());
                    if (extractDir != null)
                    {
                        Directory.CreateDirectory(extractDir);
                        File.WriteAllBytes(Path.Combine(extractDir, $"{i:D6}_{Convert.ToHexString(raw).ToLowerInvariant()}.bin"), data);
                    }
                }
                catch (Exception ex)
                {
                    errors.Add($"extract[{i}]: {ex.GetType().Name}: {ex.Message}");
                    w.WriteString("extract_error", ex.Message);
                }

                try
                {
                    var ok = reader.TryResolve(id, out var ol);
                    w.WriteBoolean("resolve_ok", ok && ol.Offset == toc.ChunkOffsetLengths[i].Offset && ol.Length == toc.ChunkOffsetLengths[i].Length);
                }
                catch (Exception ex)
                {
                    errors.Add($"resolve[{i}]: {ex.GetType().Name}: {ex.Message}");
                    w.WriteBoolean("resolve_ok", false);
                }
                w.WriteEndObject();
            }
            w.WriteEndArray();

            w.WriteStartArray("errors");
            foreach (var e in errors) w.WriteStringValue(e);
            w.WriteEndArray();
            w.WriteEndObject();
        }
        Console.Out.Write(Encoding.UTF8.GetString(ms.ToArray()));
        Console.Out.WriteLine();
        return errors.Count == 0 ? 0 : 1;
    }
}
