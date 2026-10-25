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

## 局限

* 这只证明 **CUE4Parse** 接受我们的输出；**不等于** eFootball 的 iOS 运行时（UE4.26 改版的 FIoDispatcher/FFileIoStore）会接受，
  后者只能在真机上验证。
* CUE4Parse 不校验 TOC meta 哈希和块 SHA-1 签名，所以这两项靠 `iostore.py selfcheck --deep` 自查。
