# cue4parse_check：用 CUE4Parse 独立回读 IoStore 容器

目的：证明 `tools/native/iostore.py` **写出的** `.utoc/.ucas` 能被第三方读取器（FabianFG/CUE4Parse 的
`IoStoreReader`，FModel 用的就是它）接受，并且读出的每个 chunk 与我们自己的读取器字节一致。
这是对 `iostore.py` 的独立交叉验证，不依赖我们自己的解析代码。

## 构建（本云端环境中已实际完成）

```bash
# 1) .NET SDK：CUE4Parse 当前（commit 77b94fa）的 Directory.Build.props 要求 net10.0，所以装 10.0（不是 8.0）
curl -sSL -o dotnet-install.sh https://dot.net/v1/dotnet-install.sh
bash dotnet-install.sh --channel 10.0 --install-dir /opt/tools/dotnet      # 实装版本 SDK 10.0.402
export DOTNET_ROOT=/opt/tools/dotnet PATH=/opt/tools/dotnet:$PATH

# 2) CUE4Parse 源码
git clone https://github.com/FabianFG/CUE4Parse /opt/tools/ref/CUE4Parse

# 3) 构建本工具（CUE4PARSE_SKIP_NATIVE=1：跳过 CUE4Parse-Natives 的 CMake 原生库，None/Zlib/LZ4/Oodle 读取都不需要它）
cd tools/native/cue4parse_check
CUE4PARSE_SKIP_NATIVE=1 dotnet build -c Release -o /opt/tools/cue4parse_check_bin
#   其他 CUE4Parse 路径： dotnet build ... -p:Cue4ParseDir=/path/to/CUE4Parse
```

构建结果：`Build succeeded. 0 Error(s)`（CUE4Parse 自身有约 600 条编译警告，与本工具无关）。
NuGet 依赖从 api.nuget.org 拉取，需联网。

## 运行

```bash
export DOTNET_ROOT=/opt/tools/dotnet
/opt/tools/cue4parse_check_bin/Cue4ParseCheck path/to/pakchunk.utoc [--game GAME_eFootball] \
    [--aes-key-file key.txt] [--extract-dir outdir]
```

* AES 密钥：`--aes-key-file`（64 位十六进制，可带 0x）或环境变量 `R9_AES_KEY_HEX`；程序从不打印密钥。
* `--game` 默认 `GAME_eFootball`（CUE4Parse 的 EGame 中 eFootball = UE4.26 变体）；TOC v10 / AES-CTR 容器需 `--game GAME_UE6_0`
  （CUE4Parse 只在 UE6 下读取 header 中的 EncryptionIVCount）。
* 输出（stdout JSON）：header 摘要、`mount_point`、每个 TOC 条目的 `chunk_id`（12 字节 hex）、`offset/length`、
  `path`、`meta_hash20`、`meta_flags`、解出数据的 `size`/`sha256`、`resolve_ok`（`TryResolve(FIoChunkId)`，含完美哈希查找）、`errors`。
* 退出码：0 全部读出；1 有读取错误（见 `errors`）；2 参数错误。

新增选项（用于验证 `iostore.py add` / `clone-package` 的输出，见 `README_iostore.md` 第 12.5 节）：

* `--container-header`：用 CUE4Parse 的 `FIoContainerHeader` 解码容器头 chunk。包条目长度由 `--game` 决定：`GAME_eFootball` 为 16 字节，
  其他 UE4 游戏（如 `GAME_UE4_26`、`GAME_UE4_27`）为 32 字节。输出 `container_header`：`parsed`、`version`、`container_id`、`package_count`，
  以及每个包的 `export_count`、`export_bundle_count`、`imported_packages`、`chunk_resolve_ok`（`TryResolve(FIoChunkId(id, 0, ExportBundleData))`）、
  `path`（目录索引 `PackageIdIndex`）。CUE4Parse 对 UE4 容器头解析失败时返回 null，此时输出 `parsed: false` 并记入 `errors`。
* `--global global.utoc`：把该全局容器（文件名必须是 `global.utoc`）和被测容器一起挂进 `StreamedFileProvider`，对目录索引中的每个包
  分别按 FPackageId（`TryLoadPackage(id)`）和按路径加载 `IoPackage`，输出 `provider.packages`：`name`、`name_hash_matches_id`
  （`FPackageId.FromName(name) == id`）、`names`、`imports`、`exports`（名字、`GlobalImportIndex`、flags）、`load_by_id_ok`、`load_by_path_ok`。
  只读包头，不反序列化导出内容。
* `--package-id NAME`（可重复；可以不带容器单独使用）：输出 CUE4Parse 计算的 `FPackageId.FromName(NAME)`。

## 覆盖了 CUE4Parse 的哪些代码路径

`FIoStoreTocResource`（header、chunk id、offset、完美哈希种子、溢出表、压缩块、方法名、签名段跳过、目录索引）、
`IoStoreReader.Mount`（目录索引解密 + 遍历）、`IoStoreReader.Extract(FIoStoreEntry)`（每个 TOC 条目的块解密 + 解压）、
`IoStoreReader.TryResolve`（完美哈希查找）。TOC meta 用第二次 `ReadTocMeta` 解析读取（见下）。

## 发现的 CUE4Parse 问题（如实记录）

`FIoStoreTocResource` 在同时指定 `ReadDirectoryIndex | ReadTocMeta`（`ReadAll`）时，只记录
`DirectoryIndexBufferOffset`，**没有跳过 `DirectoryIndexSize` 字节**，于是 `ChunkMetas` 从目录索引的字节处读出
（在合成容器上实测：meta_hash 出现 `0a0000002e2e2f2e2e2f2e2e2f...`，即目录索引里的 `"../../../"`）。
本工具因此用两次解析：读取器用 `ReadDirectoryIndex`，meta 用只带 `ReadTocMeta` 的独立解析。

## 在本环境中的验证结果

由 `tools/tests/test_iostore.py` 中的 `test_cue4parse_*` 调用（缺少 .NET 或二进制时自动 skip）：

| 测试 | 内容 | 结果 |
|---|---|---|
| `test_cue4parse_reads_original_synthetic` ×7 | 独立构建器生成的 v2/v3 分区/v3 AES/v4 完美哈希/v5 溢出/v5 AES+完美哈希/v8 IoHash | 通过 |
| `test_cue4parse_reads_our_patched_output` ×14 | 上述 7 种容器经 `iostore.py patch`（追加模式与 `--rebuild` 各一次）后的输出 | 通过 |
| `test_cue4parse_reads_unsigned_output` | Signed 容器经 `--unsign` 修补后的输出 | 通过 |
| `test_cue4parse_reads_aes_ctr_v10` | v10 AES-CTR 容器（原始与修补后，`GAME_UE6_0`） | 通过 |
| `test_cue4parse_reads_oodle_container` | Kraken/Mermaid/Leviathan/Selkie 压缩块的容器；CUE4Parse 用其托管 C# Oodle 解码器（OodleSharp）解出的 SHA-256 与我们经 ooz 解出的一致 | 本会话中较早的一次完整运行通过；之后本环境的权限策略禁止编译/运行第三方 ooz 代码，此测试改为需显式开启（见 README_iostore.md） |

每个测试都逐 chunk 比较：chunk id、SHA-256（CUE4Parse 解出 vs. iostore.py 解出）、路径、`resolve_ok`、meta 哈希前 20 字节。

`tools/tests/test_iostore_add.py`（新增 chunk / 克隆包）中调用 CUE4Parse 的测试：

| 测试 | 内容 |
|---|---|
| `test_add_package_chunks_paths_and_store_entry` ×2 | 两种包条目布局（`GAME_eFootball` / `GAME_UE4_26`）：新包按 id 和路径找到并能作为 `IoPackage` 加载；旧条目不变；用错误布局读时 CUE4Parse 解析失败或读错（反向对照） |
| `test_original_game_container_reads_in_cue4parse` ×2 | 对照：合成输入本身被 CUE4Parse 正确读取 |
| `test_add_encrypted_container_reencrypts_directory_index` | AES 容器新增路径后，目录索引重新加密，CUE4Parse 用密钥读出新路径 |
| `test_add_rebuilds_perfect_hash` ×10 | v4/v5/v5 AES/v8/v10 CTR，各跑正常种子搜索与强制溢出表两种：新增 chunk 后重建完美哈希，CUE4Parse 的 `TryResolve` 能查到每个 chunk（v4 没有溢出表，强制溢出时应被拒绝，这一项不调用 CUE4Parse） |
| `test_clone_face_set_independent` ×4 | 克隆脸网格 + 材质 + 贴图：CUE4Parse 按 id / 路径加载克隆包，包名、`GlobalImportIndex`、导入表与 iostore.py 一致；供体和球员包在 CUE4Parse 看来与原容器完全相同 |
| `test_clone_numbered_donor_stored_like_unreal` ×2 | 供体包名按 UE 的方式存（名字项 `face` + FName 数字 110719）：克隆成 `face_r9_2002` 后 CUE4Parse 按 id / 路径加载，包名文本、`FromName(包名)==id`、导出 GlobalImportIndex 与 iostore.py 一致；供体和球员包不变 |
| `test_real_header_in_container_add_and_cue4parse` ×2 | 真实 eFootball（PesMobile）与 UE4.27 容器头放进合成容器、追加新包：CUE4Parse 读出 183 / 52584 个包且全部正确；错误布局对照 |

## 局限

* 这只证明 **CUE4Parse** 接受我们的输出；**不等于** eFootball 的 iOS 运行时（UE4.26 改版的 FIoDispatcher/FFileIoStore）会接受，
  后者只能在真机上验证。
* CUE4Parse 不校验 TOC meta 哈希和块 SHA-1 签名，所以这两项靠 `iostore.py selfcheck --deep` 自查。
