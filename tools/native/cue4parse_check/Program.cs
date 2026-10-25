// Cue4ParseCheck: open an IoStore container with CUE4Parse (third-party reference reader) and
// dump header facts plus SHA-256 of every chunk as JSON on stdout.
//
// Usage:
//   Cue4ParseCheck <file.utoc> [--game GAME_eFootball] [--aes-key-file PATH] [--extract-dir DIR]
//                  [--container-header] [--global global.utoc] [--package-id NAME ...]
//   Cue4ParseCheck --package-id NAME [--package-id NAME ...]        (FPackageId.FromName only)
//
//   --container-header  decode the ContainerHeader chunk with CUE4Parse's FIoContainerHeader (the
//                       FPackageStoreEntry layout follows --game: GAME_eFootball = 16-byte entries,
//                       other UE4 games = 32-byte entries) and resolve each package by id
//                       (TryResolve(FIoChunkId(id, 0, ExportBundleData))) and by path (PackageIdIndex)
//   --global PATH       mount PATH (must be named global.utoc) + the container in a
//                       StreamedFileProvider and load every package of the directory index by id
//                       AND by path as an IoPackage (header only: name map, import map, export map)
//   --package-id NAME   print CUE4Parse's FPackageId.FromName(NAME)
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
using CUE4Parse.FileProvider;
using CUE4Parse.UE4.Objects.Core.Misc;
using CUE4Parse.UE4.IO;
using CUE4Parse.UE4.IO.Objects;
using CUE4Parse.UE4.Readers;
using CUE4Parse.UE4.Versions;

internal static class Program
{
    private static int Main(string[] args)
    {
        string? utoc = null, keyFile = null, extractDir = null, globalPath = null;
        var dumpHeader = false;
        var packageNames = new List<string>();
        var gameName = "GAME_eFootball";
        for (var i = 0; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--game": gameName = args[++i]; break;
                case "--aes-key-file": keyFile = args[++i]; break;
                case "--extract-dir": extractDir = args[++i]; break;
                case "--container-header": dumpHeader = true; break;
                case "--global": globalPath = args[++i]; break;
                case "--package-id": packageNames.Add(args[++i]); break;
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
        if (utoc == null && packageNames.Count > 0)
        {
            var ms0 = new MemoryStream();
            using (var w0 = new Utf8JsonWriter(ms0, new JsonWriterOptions { Indented = true }))
            {
                w0.WriteStartObject();
                WritePackageIds(w0, packageNames);
                w0.WriteEndObject();
            }
            Console.Out.WriteLine(Encoding.UTF8.GetString(ms0.ToArray()));
            return 0;
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

            FAesKey? aesKey = null;
            if (reader.IsEncrypted)
            {
                var hex = keyFile != null ? File.ReadAllText(keyFile).Trim() : Environment.GetEnvironmentVariable("R9_AES_KEY_HEX")?.Trim();
                if (string.IsNullOrEmpty(hex))
                {
                    Console.Error.WriteLine("container is encrypted: provide --aes-key-file or R9_AES_KEY_HEX");
                    return 2;
                }
                aesKey = new FAesKey(hex.StartsWith("0x") ? hex : "0x" + hex);
                reader.AesKey = aesKey;
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

            if (dumpHeader) WriteContainerHeader(w, reader, game, errors);
            if (globalPath != null) WritePackages(w, utoc, globalPath, versions, aesKey, h.EncryptionKeyGuid, errors);
            if (packageNames.Count > 0) WritePackageIds(w, packageNames);

            w.WriteStartArray("errors");
            foreach (var e in errors) w.WriteStringValue(e);
            w.WriteEndArray();
            w.WriteEndObject();
        }
        Console.Out.Write(Encoding.UTF8.GetString(ms.ToArray()));
        Console.Out.WriteLine();
        return errors.Count == 0 ? 0 : 1;
    }

    private static string Hex(ulong v) => "0x" + v.ToString("X16");

    private static void WritePackageIds(Utf8JsonWriter w, List<string> names)
    {
        w.WriteStartArray("package_ids_from_names");
        foreach (var n in names)
        {
            w.WriteStartObject();
            w.WriteString("name", n);
            w.WriteString("package_id", Hex(FPackageId.FromName(n).id));
            w.WriteEndObject();
        }
        w.WriteEndArray();
    }

    // FIoContainerHeader as CUE4Parse reads it for the selected game, plus lookups by id and path.
    private static void WriteContainerHeader(Utf8JsonWriter w, IoStoreReader reader, EGame game, List<string> errors)
    {
        w.WriteStartObject("container_header");
        try
        {
            var ch = reader.ContainerHeader; // CUE4Parse returns null for UE4 when parsing throws
            if (ch == null)
            {
                w.WriteBoolean("parsed", false);
                errors.Add("container_header: CUE4Parse could not parse the ContainerHeader chunk");
            }
            else
            {
                w.WriteBoolean("parsed", true);
                w.WriteString("version", ch.Version.ToString());
                w.WriteString("container_id", Hex(ch.ContainerId.Id));
                w.WriteNumber("package_count", ch.PackageIds.Length);
                var ebd = game >= EGame.GAME_UE5_0 ? (byte) EIoChunkType5.ExportBundleData : (byte) EIoChunkType.ExportBundleData;
                w.WriteStartArray("packages");
                for (var i = 0; i < ch.PackageIds.Length; i++)
                {
                    var pid = ch.PackageIds[i];
                    var e = ch.StoreEntries[i];
                    w.WriteStartObject();
                    w.WriteString("package_id", Hex(pid.id));
                    w.WriteNumber("export_count", e.ExportCount);
                    w.WriteNumber("export_bundle_count", e.ExportBundleCount);
                    w.WriteStartArray("imported_packages");
                    foreach (var ip in e.ImportedPackages ?? []) w.WriteStringValue(Hex(ip.id));
                    w.WriteEndArray();
                    w.WriteBoolean("chunk_resolve_ok", reader.TryResolve(new FIoChunkId(pid.id, 0, ebd), out _));
                    if (reader.PackageIdIndex.TryGetValue(pid, out var gf)) w.WriteString("path", gf.Path);
                    w.WriteEndObject();
                }
                w.WriteEndArray();
            }
        }
        catch (Exception ex)
        {
            errors.Add($"container_header: {ex.GetType().Name}: {ex.Message}");
            w.WriteString("error", ex.Message);
        }
        w.WriteEndObject();
    }

    // Mount global.utoc + the container in a provider and load every package by id and by path.
    private static void WritePackages(Utf8JsonWriter w, string utoc, string globalPath, VersionContainer versions,
        FAesKey? aesKey, FGuid keyGuid, List<string> errors)
    {
        w.WriteStartObject("provider");
        try
        {
            var provider = new StreamedFileProvider("cue4parse_check", versions, StringComparer.OrdinalIgnoreCase);
            provider.RegisterVfs(globalPath);
            provider.RegisterVfs(utoc);
            provider.Mount();
            if (aesKey != null) provider.SubmitKey(keyGuid, aesKey);
            w.WriteBoolean("global_data_loaded", provider.GlobalData != null);
            w.WriteNumber("mounted", provider.MountedVfs.Count);
            w.WriteStartArray("packages");
            foreach (var (pid, file) in provider.FilesById.OrderBy(kv => kv.Key.id))
            {
                w.WriteStartObject();
                w.WriteString("package_id", Hex(pid.id));
                w.WriteString("path", file.Path);
                try
                {
                    var byId = provider.TryLoadPackage(pid, out var pkg);
                    w.WriteBoolean("load_by_id_ok", byId);
                    if (byId && pkg != null)
                    {
                        w.WriteString("name", pkg.Name);
                        w.WriteBoolean("name_hash_matches_id", FPackageId.FromName(pkg.Name).id == pid.id);
                        w.WriteStartArray("names");
                        foreach (var n in pkg.NameMap) w.WriteStringValue(n.Name);
                        w.WriteEndArray();
                        w.WriteStartArray("imports");
                        foreach (var imp in pkg.ImportMap) w.WriteStringValue(Hex(imp.TypeAndId));
                        w.WriteEndArray();
                        w.WriteStartArray("exports");
                        foreach (var e in pkg.ExportMap)
                        {
                            w.WriteStartObject();
                            w.WriteString("name", pkg.CreateFNameFromMappedName(e.ObjectName).Text);
                            w.WriteString("global_import_index", Hex(e.GlobalImportIndex.TypeAndId));
                            w.WriteNumber("object_flags", (uint) e.ObjectFlags);
                            w.WriteNumber("cooked_serial_size", e.CookedSerialSize);
                            w.WriteEndObject();
                        }
                        w.WriteEndArray();
                        var byPath = provider.TryLoadPackage(file.Path, out var p2);
                        w.WriteBoolean("load_by_path_ok", byPath && p2 != null && p2.Name == pkg.Name);
                    }
                    else errors.Add($"package {Hex(pid.id)}: TryLoadPackage(id) failed");
                }
                catch (Exception ex)
                {
                    errors.Add($"package {Hex(pid.id)}: {ex.GetType().Name}: {ex.Message}");
                    w.WriteString("error", ex.Message);
                }
                w.WriteEndObject();
            }
            w.WriteEndArray();
        }
        catch (Exception ex)
        {
            errors.Add($"provider: {ex.GetType().Name}: {ex.Message}");
            w.WriteString("error", ex.Message);
        }
        w.WriteEndObject();
    }
}
