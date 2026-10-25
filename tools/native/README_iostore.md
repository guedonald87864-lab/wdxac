# iostore.py — UE IoStore（.utoc / .ucas）读取器、补丁写入器与新增包工具

`tools/native/iostore.py` 是纯 Python 的命令行工具，用来读取、解包、比对和修补 UE IoStore 容器，
并能在容器里新增 chunk、路径和包条目，或把一个包复制成独立的新包（第 12 节）。
目前**没有本项目的 eFootball iOS 文件**（IPA、`.utoc/.ucas`）。本工具按公开格式（CUE4Parse、retoc 源码）编写，在合成数据上验证；
唯一接触过的真实 eFootball 数据是 retoc 仓库附带的一个 **Android 版**容器头 chunk（4.7 KB 的包条目元数据，不含任何资源，第 12.2 节）。
拿到真实文件后，必须先按第 7 节和第 13 节的清单逐项核验，才能用它去修补。

## 1. 状态一览（按任务书的四级标注）

| 项目 | 状态 |
|---|---|
| 读取器：FIoStoreTocHeader（144 字节）、TOC 版本 1–10 的各段布局 | **已完成且验证**（合成数据 v2/v3/v4/v5/v8/v10）。v1/v6/v7/v9 已按 CUE4Parse 布局实现，但没有构造测试数据 |
| TOC 原样解析后再序列化，字节完全一致 | **已完成且验证**（合成数据，全部 9 种变体）；**真实文件待验**（第 7 节第 3 步） |
| 解密：AES-256-ECB（UE4/UE5 常规加密） | **已完成且验证**（合成数据，每次测试随机生成密钥） |
| 解密：AES-CTR（TOC v10 / UE6） | **已完成且验证**（合成数据，CUE4Parse 也能回读）；与 eFootball（UE4.26）基本无关 |
| 解压：None / Zlib / LZ4 | **已完成且验证** |
| 解压：Gzip / Zstd | 已实现但未测试；Zstd 需要另装 `zstandard` |
| 解压：Oodle（通过外部解码库） | **已实现，待复验**。较早一次运行中通过过（见第 6 节），但之后本环境禁止编译/运行第三方 ooz 代码，最终测试运行中这两项被 skip，没有可复现的证据，所以按任务书标准**不能算“已完成且验证”**。拿到 Oodle 解码库后必须重跑（第 6、7 节）。不带解码器时的行为（如实报错、靠原始块比对做 verify）每次都会测 |
| patch（追加模式与 `--rebuild`） | **已完成且验证**：合成数据，加上 CUE4Parse 独立回读（含对抗测试：多块/末块不满/空 chunk/多分区/非 ASCII 路径/加密+压缩/共享块布局）。游戏运行时是否接受：**已完成候选、等待实机** |
| 无目录索引的加密容器：写入前验证密钥 | **已完成且验证**（合成数据）：用错误密钥时拒绝写出（独立复审前会静默写出坏容器） |
| `add`：新增 chunk、目录索引路径、容器头包条目 | **已完成且验证（合成数据 + 真实容器头字节）**：我们的读取器回读 + CUE4Parse 独立回读（按 id 解析 chunk、按路径找到包、两种包条目布局的容器头、`IoPackage` 按 id 和按路径加载）。游戏运行时是否接受：**已完成候选、等待实机**。详见第 12 节 |
| `clone-package`：把供体包复制成新包名 / 新 FPackageId 的独立包（含 bulk chunk、包头改名、同批克隆互相改指向、跨容器 `--rewire`） | 同上。只支持 UE4.26/4.27 的 Zen 包头；改动的字段逐项列在第 12.3 节 |
| 容器头包条目布局：eFootball 16 字节 / 原版 32 字节 | **用真实数据验证了判定方法**：retoc 仓库附带的 PesMobile（即 eFootball **Android 版**）真实容器头只符合 16 字节布局，一个 UE4.27 游戏的真实容器头只符合 32 字节布局，两者都能按我们的编码规则逐字节重建（第 12.2 节）。**iOS 11.1.1 的容器头本工程没见过**，拿到文件后用 `container-header` 实测，不得假定 |
| FPackageId = CityHash64（小写 UTF-16 包名） | **已完成且验证（对 UE 5.8 数据）**：Google `city-test.cc` 全部 300 组向量；Unreal Engine 5.8 自己写出的 97 个包 id（CUE4Parse 测试夹具）；与 CUE4Parse 的 C# `FPackageId.FromName` 逐个比对一致。**eFootball 的包名没有见过**：`clone-package` 对每个源包、`add` 对容器里已有的包都会实测“包头里的包名算出的 id == chunk id”，拿到真实文件后这一步通过才算对 eFootball 成立（12.4） |
| 完美哈希重建（TOC v4 及以上，新增 chunk 时） | **已完成且验证**（合成数据 + CUE4Parse）：只保证满足读取端（`TryResolve`）的查找规则，种子不一定与 UE 自己的写入器相同。eFootball 按推测是 v2，没有完美哈希，与它无关 |
| 独立绑定（游戏里“哪个球员用哪个脸部包”的映射） | **缺少原始输入**：映射机制完全未知，容器工具只能把新包放进去，不能让游戏去用它。见第 13 节 |
| 用真实 eFootball 容器验证 | **缺少原始输入**：需要任务书第七节提到的 pc3600_mobile_ios（模型）、pc3611_mobile_ios（纹理）对应的 `.utoc/.ucas`（连同同名 `.pak`、`.sig`，以及 `global.utoc/.ucas`） |

## 2. 格式来源

所有结构的布局都抄自 CUE4Parse 源码（`/opt/tools/ref/CUE4Parse`，commit 77b94fa），具体文件和结构名写在
`iostore.py` 的模块和类文档字符串里。主要包括：`FIoStoreTocHeader.cs`、`FIoStoreTocResource.cs`（各段顺序和版本门槛）、
`FIoChunkId.cs`、`FIoOffsetAndLength.cs`、`FIoStoreTocCompressedBlockEntry.cs`、`FIoStoreTocEntryMeta.cs`、
`FIoDirectoryIndexEntry.cs`、`FIoFileIndexEntry.cs`、`FIoStoreEncryptionIV.cs`、`IoStoreReader.cs` 和 `Aes.cs`。
meta 哈希参考的是 retoc（`/opt/tools/ref/retoc`，`iostore_writer.rs` 与 `lib.rs` 中的 `FIoChunkHash::from_blake3`）。
Epic 官方的 UE 源码仓库在本环境中无法访问，所以凡是“UE 实际怎么做”的说法都只来自上述第三方实现。

已知的 eFootball 相关信息：CUE4Parse 中 `GAME_eFootball = GAME_UE4_26 + 29`。IoStore 相关的唯一特殊处理在
`FFilePackageStoreEntry.cs`（容器头里的包条目没有 `ExportBundlesSize`），TOC 层没有特殊处理；
另外 `FInstancedStaticMeshInstanceData.cs` 里也有一处（资产层，与本工具无关）。
据此推测真实容器的 TOC 版本是 2 或 3、不用完美哈希、chunk 类型按 UE4 命名。**这只是推测，以 `info` 的实际输出为准。**

## 3. 安装

```bash
pip install -r tools/requirements.txt   # pycryptodome, lz4, blake3, pytest；zstandard 可选
```

AES 优先使用 pycryptodome，没有时退回 `cryptography`。BLAKE3 优先使用 `blake3` 包，没有时退回内置的纯 Python 实现
（与 `blake3` 包逐字节对照测试过，但很慢）。

## 4. 用法

AES 密钥只从两处读取：`--aes-key-file 文件`（64 位十六进制，可带 0x；也接受 32 字节二进制），或环境变量 `R9_AES_KEY_HEX`。
工具不会打印、记录密钥，也不会把它写进 manifest（测试会检查 stdout、stderr 和 manifest 中都不出现密钥）。
**我们不会、也不应向用户索取密钥**，容器加密时请用户在自己的机器上提供。

```bash
T=tools/native/iostore.py
python3 $T info      X.utoc [--aes-key-file key.txt]           # header、flags、压缩方法及可否解码、分区、meta 哈希算法探测、往返检查
python3 $T selfcheck X.utoc --deep                             # 拿到真实文件后第一个要跑的命令（见第 7 节）
python3 $T list      X.utoc --glob '*/pc3611/*' [--json] [--type BulkData]
python3 $T extract   X.utoc --glob '*face*' --out-dir out/ex1 --verify   # 也可用 --chunk-id HEX24 / --index N / --all
python3 $T hash-all  X.utoc --out before.json                  # 每个 chunk 解压后的 sha256
python3 $T patch     X.utoc --out-dir out/p1 --replace 'eFootball/Content/.../T_Face.ubulk' new.bin --verify
python3 $T patch     X.utoc --out-dir out/p2 --replace-list repl.json --rebuild --verify
#   加密且没有目录索引的容器：密钥要靠解码未改动的 chunk 来证明；证明不了时需显式 --allow-unverified-key
python3 $T hash-all  out/p1/X.utoc --out after.json
python3 $T diff      before.json after.json                    # 也可以直接比较两个 .utoc；有差异时退出码为 3
python3 $T build-ooz --src /path/to/ooz --out-dir out/ooz --verify       # 编译 Oodle 解码库（第 6 节）

# 新增包 / 独立资源（第 12、13 节）
python3 $T package-id /Game/Face/pc3600/face_r9_2002               # FPackageId 与各类型 chunk id
python3 $T container-header X.utoc [--all]                          # 容器头：实测 16/32 字节布局、包条目
python3 $T container-header --raw ContainerHeader.bin               # 也可直接读取已导出的容器头 chunk
python3 $T find-refs X.utoc /Game/Face/pc3600/face_110718           # 谁在用这个包（只读）
python3 $T find-refs X.utoc 110718                                  # 按文本找：路径、包内名字表、包头里的完整 FName（包名、导出名）
python3 $T clone-package X.utoc --out-dir out/c1 --verify \
    --package /Game/Face/pc3600/face_110718=/Game/Face/pc3600/face_r9_2002 \
    --package /Game/Face/pc3600/MI_face_110718=/Game/Face/pc3600/MI_face_r9_2002 \
    [--rewire OLD=NEW --rewire-with other.utoc]      # --rename-name 一般不要用，见 12.3
python3 $T add X.utoc --out-dir out/a1 --add-list add.json --verify # 任意新 chunk + 路径 + 包条目
```

`add --add-list` 的 JSON 格式（相对路径以 JSON 文件所在目录为准）：
```json
{"chunks": [{"file": "face_r9.uasset", "package": "/Game/Face/r9/face_r9", "type": "ExportBundleData",
             "path": "PesMobile/Content/Face/r9/face_r9.uasset"},
            {"file": "face_r9.ubulk", "package": "/Game/Face/r9/face_r9", "type": "BulkData",
             "path": "PesMobile/Content/Face/r9/face_r9.ubulk"}],
 "store_entries": [{"package": "/Game/Face/r9/face_r9", "export_count": 3, "export_bundle_count": 1,
                    "imported_packages": ["/Game/Face/r9/MI_face_r9", "0x1234ABCD5678EF00"],
                    "export_bundles_size": 1234, "load_order": 7}]}
```
`chunk_id`（24 位 hex）可代替 `package`+`type`；`export_bundles_size`、`load_order` 只在 32 字节布局时需要。

`--replace` 的选择器有以下几种写法：`index:N`、`id:<24位hex>`、直接写 `<24位hex>`、`path:<路径>`、直接写 `<路径>`
（路径带不带挂载点 `../../../` 都可以；非 ASCII 路径，例如中文目录名，也可以直接写）。每个选择器必须恰好命中一个 TOC 条目，否则报错。
`--replace-list` 接受一个 JSON 列表，格式为 `[{"selector": "...", "file": "..."}]`。

退出码：0 成功；1 有读取错误或 selfcheck 失败；2 格式错误、用法错误、文件不存在或被拒绝（例如会覆盖输入、缺少或错误的密钥、容器已签名），
不会打印 Python traceback；3 `diff` 发现差异。

`list` 的输出（合成示例）：
```
     1  3cf7fb33337285b400000003  BulkData        200000   122115     4  None,Zlib  ../../../eFootball/Content/Face/pc3600/face_r9.ubulk
    11  c542e36b0a3ef1160000000a  ContainerHeader    777      420     1  Zlib
```
列依次为：TOC 序号、chunk id（12 字节 hex）、类型、解压后大小、磁盘上的大小、块数、使用的压缩方法、路径。

## 5. patch 的具体做法

**所有写操作都遵守以下规则：**
1. 不覆盖输入。`--out-dir` 必须是新建的或空的目录，且不能是输入文件（包括替换用的源文件）所在的目录（符号链接按真实路径判断）；
   所有输出文件都用独占模式（`x`）新建，包括追加模式下复制过去的分区文件（复审前这里用的是会覆盖目标的 `shutil.copyfile`）。
2. 输出文件名与输入相同（游戏按文件名加载），只放在新目录里。
3. 同时写出 `<名称>.manifest.json`，内容包括：输入文件（utoc、各分区、替换用的源文件）的 sha256 和大小（**在写任何输出之前**计算）、
   输出文件的 sha256 和大小、命令行参数、密钥来源（只记录 `file`、`env` 或 `none`）和密钥验证方式与结果（`aes_key_check`，不含密钥）、
   header 中哪些字段变了（新旧值）、每个被替换 chunk 的新旧大小/offset/块范围/meta 哈希/flags、使用的 meta 哈希算法及其判定依据、
   MemoryMapped 对齐依据、警告。
4. 加 `--verify` 时，会用读取器重新打开输出并逐项检查：输入文件重新计算的 sha256 与写之前一致；TOC 再序列化字节一致；
   版本、块大小、ContainerId、KeyGuid、压缩方法表、完美哈希表未变；chunk id 表、目录索引字节和路径表都未变；
   完美哈希能查到所有 chunk；header 的 PartitionCount 与实际分区文件一致（没有缺失也没有多余的 `_sN.ucas`）；
   所有被引用块的 16 字节对齐磁盘跨度（UE/CUE4Parse 实际读取的范围）不越过分区文件末尾、不跨 PartitionSize、互不重叠
   （与输入相比不能变差）；被替换的 chunk 读出来等于新数据，且 meta 哈希正确；其余所有 chunk 的 length、块内起始偏移和
   meta 未变、磁盘上的原始块字节（AES-CTR 时连同 IV）完全相同；若有对应的解码器，还会比对解压后的 sha256（解码异常记为失败，
   不会让程序崩溃）。任何一项失败，退出码都不为 0（manifest 仍会写出，便于排查）。
   **局限：** `--verify` 用的是同一个读取器，只能发现写入器与读取器之间的不一致、对未改动数据的破坏等；如果读写两边对格式的
   理解犯了同一个错误，它发现不了。对格式理解本身的独立检验靠 CUE4Parse（测试中自动运行，真实文件用 `cue4parse_check`）和实机。

**追加模式（默认）：** 把所有分区原样复制过去，新数据以**不压缩块**（压缩方法索引 0）追加到最后一个分区末尾
（起点取文件末尾与该分区内所有旧块 16 字节对齐跨度的末尾两者中较大者，因此输入的末块即使没有补齐填充，也不会与新数据重叠）。
块按 16 字节对齐并补齐到 16 字节；带 MemoryMapped 标志的 chunk 首块至少按 16 KiB（iOS 页大小，属假设）对齐，若输入中现有的
MemoryMapped chunk 都满足更粗的对齐（最多取 64 KiB），则沿用该对齐，并且整个 chunk 放在同一个分区里、块与块之间连续
（UE 把它作为一段连续文件区间映射）。容器加密时用同一个密钥加密新块，CTR 模式则为每块生成新的 IV。
**密钥必须先被证明是对的：** 有目录索引时用挂载点检查；没有目录索引（无 Indexed 标志）时，解码几个未改动的压缩 chunk 或比对
meta 哈希，解不开就拒绝（`KeyRequired`），什么都证明不了时拒绝，除非显式加 `--allow-unverified-key`。
然后改写以下内容：该 chunk 的 `FIoOffsetAndLength`，指向新块范围的起点，即 `新首块序号 × CompressionBlockSize`；
压缩块表（追加新条目）；`TocCompressedBlockEntryCount`；meta 哈希；meta flags（清掉 Compressed 位，保留 MemoryMapped 位）。
以下内容一律不动：chunk id 及其顺序、完美哈希种子和溢出表、目录索引字节、压缩方法表、ContainerId 和 KeyGuid。
旧数据还留在 `.ucas` 里，只是不再被引用（manifest 中记为 `orphaned_blocks`，只统计确实没有任何 chunk 再引用的块；
若某块还被另一个 chunk 共用，则不算孤立）。
如果追加后超过 `PartitionSize`（仅 v3 及以上），会新建 `_sN.ucas` 并增加 `PartitionCount`，同时给出警告，因为游戏未必认这个新分区。

**`--rebuild`：** 重写所有分区。未改动的 chunk 直接复制原块 16 字节对齐跨度内的磁盘字节（含原有填充；保持压缩和加密状态，CTR 的 IV 也随块带过去），
被替换的 chunk 写成不压缩块，块表按 chunk 在磁盘上的顺序重新编号，不再被引用的块会被丢弃。
使用前提是每个 chunk 都从块边界开始，且没有两个 chunk 共用同一个块；`selfcheck` 的 `layout` 一项会报告这一点，不满足时直接拒绝。

**meta 哈希：** v8（ReplaceIoChunkHashWithIoHash）及以上版本是 20 字节的 FIoHash。FIoHash 是 BLAKE3 截断到 20 字节——这是 UE 的
类型定义（依据 retoc 的实现；CUE4Parse 只把它当 20 字节读出，Epic 源码在本环境无法核对），**不是**从文件格式本身能推出的；
所以 v8+ 也会实测，存储值全为零的容器保持写零，其余写 BLAKE3。
v8 之前是 32 字节的 FIoChunkHash，按 retoc 的写法是 20 字节摘要后补 12 个零。至于摘要用 SHA-1 还是 BLAKE3，取决于引擎版本：
retoc 写的是 BLAKE3，而老的 UE4 据称用 SHA-1，但我们手里没有 UE 源码可以确认。
因此 `--hash-algo auto` 采用**实测**：对若干个未改动、且能解码的 chunk 重新计算两种哈希，与存储值的**全部 32 字节**比对
（复审前只比前 20 字节，尾部 12 字节不为零的未知布局会被误判为 sha1/blake3，写出后与其他条目不一致，selfcheck 也看不出来）。
结果为 `sha1`、`blake3` 或 `zero`（全零）时采用该结果；无法判定（包括尾部不为零）时拒绝写出，并要求用 `--hash-algo` 显式指定。

**签名容器（Signed）：** 默认拒绝修补，因为 TOC 和块的 RSA 签名没有发行方私钥就无法重算。
`--unsign` 会清除 Signed 标志并删掉签名段，同时给出警告：如果运行时强制校验签名，就会拒绝这个容器。

## 6. Oodle

`info` 会列出容器用到的每种压缩方法，以及 `decoder_available` 和 `decoder` 字段；后者在缺少解码器时会说明如何启用。
遇到 Oodle 块而没有解码器时，读取会抛出 `UnsupportedCompression`，不会悄悄输出错误数据。
解码器通过 `--oodle-lib` 或环境变量 `R9_OODLE_LIB` 指定，二选一：

* **官方 Oodle 核心库**（`liboo2core*.so`、`oo2core_*_win64.dll` 或 `.dylib`，需导出 `OodleLZ_Decompress`）。
  由用户自己提供，例如来自其合法持有的 UE 安装或游戏文件。本仓库不附带。
* **开源 ooz 解码器**（https://github.com/powzix/ooz ，支持 Kraken/Mermaid/Selkie/Leviathan/LZNA/BitKnit）：
  `iostore.py build-ooz --src <ooz目录> --out-dir <新目录> --verify`。命令会写入一个 POSIX 兼容垫片（替换 Windows 的
  `stdafx.h`）和导出函数 `ooz_decompress`，用 `g++ -O2 -shared` 编译出 `libooz.so`；`--verify` 再解码 ooz 自带的测试语料。
  ooz 依赖 SSE intrinsics，只能在 x86-64 上编译；Apple Silicon 需要在 x86-64 Linux 上编译，或改用官方库。
* 实在没有 Python 可用的解码器时，可以用 `cue4parse_check --extract-dir` 解出数据，它依赖 CUE4Parse 自带的 C# Oodle 解码器 OodleSharp。

**较早一次运行中做过、但最终测试运行中无法复现的验证**（因此在第 1 节标为“待复验”）：在 Linux 上用 g++ 编译 ooz 成功，并解码了 ooz 测试语料中 xml、sao、x-ray 三个文件的
Kraken、Mermaid、Selkie、Leviathan 四种压缩版本。每个文件的四种版本解出的内容完全相同，大小与文件头一致（xml 为
5,345,280 字节，开头是合法的 tar 头 `ustar`）。之后把这些 Oodle 块装进合成容器（单个 chunk 跨 2 个 Oodle 块），用 iostore.py
读取和修补；CUE4Parse（OodleSharp）解出的 SHA-256 与我们完全一致。包含这两项 Oodle 测试的完整测试集当时为 85 个全部通过。
**此后本环境的权限策略禁止再编译、运行第三方 ooz 代码**，所以这两项测试改为需显式开启
（`R9_OODLE_LIB=<库>` 或 `R9_TEST_BUILD_OOZ=1`），最终一次运行中被 skip。

修补 Oodle 容器时不需要 Oodle 压缩器，因为新块都不压缩。没有解码器时，`--verify` 用“原始块字节完全相同”来证明未改动的
chunk 没变；manifest 中记为 `unchanged_chunks_payload_check_skipped_no_decoder`。

## 7. 拿到真实 eFootball 文件后必须验证的内容（按顺序执行）

1. **备份与留档：** 原文件只读备份，并记录 sha256（`hash-all` 或任意一次写操作的 manifest 都会记录输入哈希）。
2. **`info`：** 记录 TOC 版本、flags（是否 Encrypted、Signed、Indexed）、压缩方法、PartitionCount 和 PartitionSize、
   `reserved_nonzero`、`sections.trailing_bytes`、chunk 类型分布。确认确实是 TOC v2 或 v3（第 2 节的推测）。
3. **`selfcheck`：** `toc_reserialize_identical` 必须为 `true`，这是最关键的自检。若为 false，说明存在未知的扩展字段，
   此时**不得修补**，应先根据报告的首个差异字节偏移分析原因。`trailing_bytes > 0` 同样需要先搞清楚是什么数据。
   `header.reserved_nonzero` 为 true 时（例如 v3 文件里本应保留为 0 的 PerfectHashSeedsCount 位置有值），要记录并弄清含义。
   `disk_spans_aligned16` 有警告时，说明原文件的块没有按 16 字节补齐或互相重叠，需记录（工具不会让情况变差）。
4. **密钥：** 容器加密时由用户在本机提供密钥，确认 `key: accepted`（通过检查目录索引的挂载点）；没有目录索引的容器显示
   `accepted(chunk check)`（靠解码未改动的 chunk 证明），`unverifiable(...)` 表示无法证明，`rejected` 表示密钥错误。
5. **解码器：** 用到 Oodle 时先配置好解码器，再跑 `selfcheck --deep`。要求：`chunks_readable` 等于 chunk 总数；
   `meta_hash_detection.algo` 是 sha1、blake3 或 zero；`meta_hash_mismatches.count` 为 0；
   若容器已签名，再看 `block_signature_sha1_match`，确认块 SHA-1 的计算范围是对齐后的长度还是实际压缩长度。
6. **独立回读：** 用 `cue4parse_check`（`--game GAME_eFootball`）读原文件，逐 chunk 比对 sha256 与 `hash-all`，结果必须全部一致。
7. **恒等补丁：** 先 `extract` 出某个 chunk，再原样 `patch --verify` 回去；然后 `diff` 原容器与输出，结果应为 identical；
   再让 CUE4Parse 读取这份输出。
8. **定位目标：** 用 `list --glob` 在 pc3600（模型）、pc3611（纹理）中找出目标资源及其 `.ubulk`、`.uptnl`。
   注意某个资源可能被多名球员共用（任务书第三节第 3 条），这一层工具无法判断，需要包级别分析。
9. **真机：** 只在隔离的测试客户端上使用修补后的副本；回退方法是还原第 1 步备份的原文件。
   以下问题**只有真机才能回答**：运行时是否接受追加的不压缩块和遗留的孤立数据；是否接受改过的 meta 哈希；
   若用了 `--unsign`，是否接受被清除的 Signed 标志；新建的 `_sN.ucas` 分区能否被挂载；
   游戏自带的资源下载、更新系统是否会校验文件大小或哈希并重新下载；若容器在 `.app` 包内，改动会破坏 iOS 代码签名，需要重签 IPA；
   若在下载目录里，则不受代码签名约束（仍需实测）。

## 8. 失败模式与处理

| 现象 | 可能原因 | 处理 |
|---|---|---|
| `bad utoc magic` | 不是 .utoc，或是游戏改过魔数（例如 CUE4Parse 收录的 ArcheAge `X5=IoStore=Toc=!`） | 不支持；先确认文件来源 |
| `toc_reserialize_identical: false` | 有未知的扩展段或字段 | 不要修补；报告差异偏移 |
| `trailing_bytes > 0` | v9 的 source hashes（CUE4Parse 也不解析），或游戏自定义数据 | 工具会原样保留；修补前先弄清楚 |
| `reserved_nonzero: true` | 保留字段里存了游戏自定义数据 | 往返时原样保留；记录下来 |
| `KeyRequired` 或 `key: rejected` | 缺少密钥或密钥错误（可能用了其他 KeyGuid） | 由用户在本机提供对应 GUID 的密钥 |
| `UnsupportedCompression ... Oodle` | 没有 Oodle 解码器 | 见第 6 节 |
| `cannot determine the TOC meta hash algorithm` | 存储的哈希既不是 SHA-1、BLAKE3，也不是全零（含 32 字节哈希尾部不为零的情况） | 用 `--hash-algo` 显式指定，并在 manifest 中注明；运行时是否校验需实机确认 |
| `AES key check on chunk data: rejected` | 无目录索引的加密容器，密钥错误（未改动的 chunk 解不开） | 换成对应 KeyGuid 的正确密钥 |
| `no chunk could prove the AES key ... --allow-unverified-key` | 无目录索引、没有可解码的压缩 chunk、meta 哈希也认不出 | 先确认密钥来源；确有把握才加 `--allow-unverified-key`，并用独立工具回读 |
| `block disk spans got worse than the input` | 写出的块越过分区末尾、跨 PartitionSize 或互相重叠（不应发生，属工具缺陷） | 不要使用该输出；保留 manifest 报告问题 |
| `container is Signed ... Refusing` | 容器已签名 | 只能 `--unsign`，并接受被运行时拒绝的风险 |
| `--rebuild needs block-aligned...` | chunk 共用块或未对齐 | 改用追加模式 |
| 警告 `package data and changed size` | 包大小变了；标准 UE4.26 的容器头记录了 ExportBundlesSize | eFootball 的布局据称没有这个字段（来自 CUE4Parse），需在真实容器头上核实；包内容的一致性由 Zen 包工具负责 |
| 警告 `created new partition file(s)` | 追加数据超过了 PartitionSize | 游戏可能不认新分区；可以考虑 `--rebuild`，或者替换更小的数据 |
| 磁盘空间、耗时 | 追加模式会复制整个分区；`--verify` 要完整读一遍两个容器 | 预留至少 2 倍容器大小的空间 |
| 游戏特有的 TOC 加密 | CUE4Parse 对少数游戏（TheFinals、NFS Mobile、FragPunk 等）有专门处理 | CUE4Parse 77b94fa 中 eFootball 不在此列；若 `info` 解析失败，需另行分析 |

## 9. 合成数据上的测试（`tools/tests/test_iostore.py`）

测试中的容器由测试文件里一个**独立的构建器**生成：它直接按 CUE4Parse 的布局编写，不调用 iostore.py 的任何序列化代码。
共有 9 种变体：v2 目录索引；v3 多分区（PartitionSize 为 192 KiB，块会跨到 `_s1/_s2.ucas`）；v3 AES；v4 完美哈希；
v5 强制溢出表；v5 AES 加完美哈希；v8 IoHash meta；v10 AES-CTR；v3 Signed。
每种变体都有 12 个 chunk，覆盖以下情况：zlib 块与不压缩块交替、多块 chunk（块大小 64 KiB）、无法压缩的数据以原样存储、
LZ4 块、空 chunk、带 MemoryMapped 标志的 chunk、没有路径的 ContainerHeader chunk、嵌套目录、同一个 id 不同类型的 chunk。

测试覆盖的内容：每个 chunk 读出的数据和路径与构建器一致；TOC、header 和目录索引都能字节级往返；
`selfcheck --deep`（meta 哈希算法探测正确、0 个不匹配、签名块 SHA-1 能对上）；完美哈希查找（含溢出表），以及查找不存在的 id；
chunk id 和压缩块的位布局；纯 Python BLAKE3 与 `blake3` 包一致；缺少密钥或密钥错误时的行为；
每种可修补变体都在追加模式和 `--rebuild` 下各修补一次（`hash-all` 加 `diff` 证明只有目标 chunk 变化，输入文件的哈希不变，
manifest 中的哈希正确，meta 哈希和 flags 正确，其余 meta 不变，修补后的输出能通过 `selfcheck --deep`，rebuild 后没有孤立块）；
一次替换多个 chunk、替换成空 chunk 和从空 chunk 改为有数据；追加时新建分区；签名容器先被拒绝、加 `--unsign` 后成功；
拒绝覆盖输入或写入非空目录；`--hash-algo` 覆盖默认值，以及无法判定时拒绝；
命令行端到端流程（info/list/extract/patch/hash-all/diff/selfcheck），并检查密钥不出现在任何输出中；
Oodle 块在没有解码器时的行为：正确报告、extract 在 manifest 中记录失败项、patch 加 `--verify` 改用原始块比对，
这一项不运行任何第三方代码，每次都会跑；带真实 Oodle 数据的 Oodle 测试（需显式开启）；
CUE4Parse 独立回读（24 个独立测试项，见 `cue4parse_check/README.md`；另外下面的对抗测试在 harness 存在时也内嵌调用 CUE4Parse）。

**独立复审追加的对抗测试（`test_adv_*`，36 项）：**

| 测试 | 针对的缺陷类型 | 复审前 |
|---|---|---|
| `block_math_edges`（4 种配置：明文 v3、AES-ECB v3、AES+完美哈希 v5、AES-CTR v10） | 块计算越界/差一：4 KiB 块上大小 0、1、15/16/17、bs-1/bs/bs+1、25 块±1，两种模式互相替换，全部回读 + selfcheck + CUE4Parse | 通过 |
| `chunk_spanning_hundreds_of_blocks` | 301 块的 chunk 换成 402 块 | 通过 |
| `zero_length_both_directions`（4 变体 × 2 模式） | 空 chunk ↔ 非空，含 ECB/CTR、溢出表、多分区 | 通过 |
| `multi_partition_layouts` | 块跨分区文件、修补中间分区的 chunk、追加新开两个分区、rebuild 减少分区数（无多余 `_sN.ucas`、PartitionCount 一致、无块跨 PartitionSize） | 通过 |
| `non_ascii_paths` | UTF-16 FString（中文、带重音、补充平面字符），命令行按路径选择/glob 解出，CUE4Parse 路径一致 | 通过 |
| `encrypted_and_compressed`（2 变体 × 2 模式） | 加密+压缩：替换 zlib 和 LZ4 chunk，其余压缩加密块逐字节保留，新块占满整 AES 块 | 通过 |
| `wrong_key_without_directory_index_is_refused`（ECB/CTR） | **无目录索引的加密容器用错误密钥**：拒绝写出 | **失败：静默写出用错误密钥加密的块**（`--verify` 时以未捕获的 zlib.error 崩溃） |
| `unverifiable_key_needs_explicit_opt_in` | 无法证明密钥时需 `--allow-unverified-key` | 无此检查 |
| `meta_hash_tail_is_compared` | FIoChunkHash 尾部 12 字节不为零时不得误判算法 | **失败：误判为 sha1** |
| `memory_mapped_chunk_alignment_and_contiguity`（2 模式） | MemoryMapped chunk 首块 16 KiB 对齐、整块位于同一分区且连续、标志保留 | 对齐成立；同一分区/连续性此前无保证 |
| `observed_mmap_alignment_is_honoured` | 输入采用更粗的 mmap 对齐（如 64 KiB）时沿用 | 无此功能（固定 16 KiB） |
| `shared_block_layout_append` | 两个 chunk 共用一个块：rebuild 拒绝；追加模式 verify 通过；孤立块计数正确 | **失败：verify 报 layout problem；孤立块多计 1** |
| `unpadded_input_append_does_not_overlap` | 输入末块未补齐 16 字节：追加数据不得落在其对齐跨度内，selfcheck 给出警告 | 追加起点通过（旧块起点 16 字节对齐时，文件末尾向上取整恰好等于对齐跨度末尾；起点未对齐的输入才会重叠）；selfcheck 警告此前没有 |
| `verify_is_not_vacuous` | 人为改坏输出中未改动 chunk 的一个字节、交换两个 CTR IV，verify 必须报错 | 字节损坏能发现；IV 不在原始块比对之内，只能靠解码后比对间接发现，而解码异常当时未被捕获 |
| `output_safety` | 指向输入目录的符号链接、out-dir 是文件、替换源在 out-dir 中、`copy_file_new` 不截断已有文件、输入哈希写前记录并在 verify 中复核 | 分区复制用会覆盖的 `copyfile`；输入哈希在写之后才算 |
| `cli_errors_are_clean` | `index:zz`、输入文件不存在：退出码 2 且无 traceback；带 BOM 的密钥文件 | **失败：traceback**；BOM 密钥被拒 |
| `reserved_fields_reported` | v3 文件中保留位置（将来版本的 PerfectHash 计数）有值：不解释、原样往返、报告 `reserved_nonzero` | 未报告 |
| `aes_backends_agree` | `cryptography` 回退实现与 pycryptodome 的 ECB/CTR 结果一致 | 回退实现此前未测试 |
| `blake3_pure_tree_shapes` | 纯 Python BLAKE3 的多层树（8/16/64 个 chunk ±1） | 通过 |
| `iohash_zero_convention_kept` | v8+ 容器存全零 FIoHash 时保持写零 | 写入 BLAKE3，与其余条目不一致 |
| `corrupt_header_values_fail_cleanly` | CompressionBlockSize=0、未知 EncryptionMethod：IoStoreError 而非 ZeroDivisionError/ValueError | **失败**：块大小 0 不被拒绝（之后的块计算 ZeroDivisionError）；未知加密方法抛未捕获的 ValueError |

“复审前”一栏中标为失败的，是复审者用当时的代码实际观察到的（错误密钥一项有复现脚本输出），或可从当时代码直接推出的行为；
修复后全部通过。

最终一次运行结果（复审后）：**120 passed, 2 skipped**（共 122 项）。被 skip 的 2 项是需显式开启的 Oodle 测试，
它们在本环境中没有在最终运行里执行过（见第 1、6 节）。复审前的运行结果为 84 passed, 2 skipped。

```bash
python3 -m pytest tools/tests/test_iostore.py -q
R9_OODLE_LIB=/path/libooz.so python3 -m pytest tools/tests/test_iostore.py -q -k oodle   # 开启 Oodle 测试
```

**新增 chunk / 克隆包的测试（`tools/tests/test_iostore_add.py`，66 项，其中 17 项为第二次复审新增，见 12.7）。** 容器由 `test_iostore.py` 的独立构建器生成；容器头由该文件里
另写的构建器按 CUE4Parse/retoc 布局生成，不调用 `ContainerHeaderUE4`；Zen 包由 `zen_package.build_legacy_package` 生成。合成内容模拟任务书第八节的场景：
供体脸 `face_110718` → 材质实例 → 贴图（pc3611），两名球员 `p_000001`、`p_000002` 共用这张脸。

| 类别 | 内容 |
|---|---|
| CityHash64 / FPackageId | Google `city-test.cc` 向量（18 组内嵌 + 全部 300 组）；UE 5.8 夹具中 97 个由引擎写出的包 id（Unversioned、Tagged 两套）；大小写不敏感；与 CUE4Parse `FPackageId.FromName` 逐个对照 |
| 真实容器头 | eFootball（PesMobile）只判为 16 字节、UE4.27 只判为 32 字节；字节级往返；规范编码重建与原文件逐字节相同；独立解码器对照；追加 3 个包后旧条目不变（追加方式下旧记录只有偏移字段变化，排序方式下 id 仍升序）；非规范缓冲区拒绝排序；UE5 容器头拒绝；空容器头要求显式指定布局 |
| 单元 | 目录索引新增文件（新建目录、复用字符串、目录名大小写、文件挂在链表末尾、重复路径拒绝、非法路径拒绝）；完美哈希构建（1/2/3/17/200 个 id，含强制溢出，按 `TryResolve` 规则独立复算）；包改名（字段清单、Δ 位移、导出字节不变、GlobalImportIndex 重算、导入/图数据重定向、UTF-16 名字、拒绝情形） |
| add | 两种布局下新增包 chunk + bulk + 路径 + 包条目，`hash-all`/`diff` 只显示容器头变化与 2 个新增；AES 容器目录索引重新加密；v4/v5/v5 AES/v8/v10 CTR 新增后重建完美哈希（含强制溢出，v4 强制溢出时拒绝）；深层挂载点；拒绝情形与“拒绝时不写输出”；Signed 容器需 `--unsign` |
| clone-package | 两种布局 × TOC v2/v3（多分区）：克隆脸 + 材质 + 贴图，旧 chunk 全部逐字节不变，新包按 id/路径可达，bulk 逐字节复制，同批互相改指向，球员包仍指向供体；32 字节布局 ExportBundlesSize 的实测规则与各选项；跨容器 `--rewire`（并证明不加时材质克隆仍指向共享贴图）；拒绝情形；`find-refs` 克隆前后的引用关系 |
| CUE4Parse | 见 `cue4parse_check/README.md`：容器头按 `GAME_eFootball`/`GAME_UE4_26`/`GAME_UE4_27` 解码、按 id 和路径加载 `IoPackage`、反向对照（错误布局必须读不对） |
| CLI | `package-id`、`container-header`、`clone-package`、`add --add-list`、`find-refs` 端到端；失败时退出码 2 且无 traceback；密钥不出现在任何输出中 |
| 第二次复审 | UE 5.8 真实夹具上的 FName 数字拆分规则；按 UE 方式存名字的供体 `face_110718` 克隆成 `face_r9_2002`（含 CUE4Parse）；`add` 前后 TOC 头逐字节比较（保留区垃圾值、未知 flag 位）与旧表前缀不变；包条目与包头矛盾时拒绝/警告；add-list 畸形输入；v4 chunk 类型编号判定；TOC 尾部未知字节与 v9 source hash；`--rewire-with` 容器内的 id 冲突；完美哈希 verify 非空检查；非规范空数组偏移 |

第二次复审结束时的运行结果：`python3 -m pytest tools/tests/test_iostore.py tools/tests/test_iostore_add.py tools/tests/test_zen_inplace.py -q`
为 **245 passed, 2 skipped**（2 个 skip 是需显式开启的 Oodle 测试；复审前为 228 passed）。全量运行 `python3 -m pytest tools/tests -q`：
**461 passed, 1 failed, 2 skipped**。失败的仍是 `test_body_params.py::test_model_estimate_reproducible`（身体参数子任务，只导入
`tools/model`、`tools/data` 下的模块，不导入本工具的任何模块）；本工具相关的测试全部通过。

## 10. 未实现 / 局限

* 不重新压缩：新块一律不压缩，容器会变大，I/O 也会增加。
* `patch` 只替换已有 chunk 的内容；新增 chunk、路径和容器头包条目由 `add` / `clone-package` 完成（第 12 节）。
  另一条路——做一个独立的 `_P` 补丁容器（自带容器头）——**没有实现**：移动端是否会挂载额外容器未知（第 13 节）。
* 不修改 `.pak` 里的 `AssetRegistry.bin`；如果游戏靠资产注册表找脸部资源，新包还需要登记进去（第 13 节）。
* 不重新生成 RSA 签名；不支持写 OnDemand 容器。
* 不处理包（Zen asset）内部的结构：导出、名称表、容器头的包条目等由其他工具负责。
* 性能：在 15 万个 chunk、60 万个块的合成 TOC（15.5 MB）上，解析约 1.2–1.4 秒，序列化约 0.35 秒，往返结果一致
  （手工测量，复审时重测一次，未纳入自动测试）。
* `--verify` 不是独立验证（见第 5 节第 4 条）；独立验证依赖 CUE4Parse 和实机。
* 写出失败（异常、verify 失败）时，`--out-dir` 中会留下部分或完整的输出及 manifest；输入不受影响，但该目录不要拿去用。
* TOC v9 的 source hashes（作为 trailing 原样保留）不会为被替换的 chunk 更新；与 eFootball（v2/v3）无关。
* chunk 类型命名（UE4 `EIoChunkType` 或 UE5 `EIoChunkType5`）按 TOC 版本推断（v4 起按 UE5），CUE4Parse 则按引擎版本；
  这只影响显示和“包数据大小改变”警告，不影响写入。`list` 输出同时给出两种命名。
* MemoryMapped 的 16 KiB 下限是按 iOS 页大小做的假设；加密容器中的 MemoryMapped chunk 能否被映射，需实机确认。

## 11. 独立复审记录（对抗性审查）

复审方法：逐行对照 CUE4Parse 77b94fa（`FIoStoreTocHeader.cs`、`FIoStoreTocResource.cs`、`IoStoreReader.cs`、`FIoChunkId.cs`、
`FIoOffsetAndLength.cs`、`FIoStoreTocCompressedBlockEntry.cs`、`FIoStoreTocEntryMeta.cs`、`Aes.cs`、`AbstractVfsReader.cs`）和 retoc
（`lib.rs`、`iostore_writer.rs`），检查结构大小、字节序、位打包、版本门槛、块计算、哈希算法、AES 对齐与填充、写入器是否更新了
所有相关字段、写操作是否安全，以及 README 的说法是否有测试支撑。

格式层（header 144 字节各偏移、chunk id 的 NETWORK_ORDER16、5+5 字节大端 offset/length、40/24/24/8 位块条目、33/24 字节 meta、
各段顺序与版本门槛、FNV-1 完美哈希含负 seed 与溢出表、AES-CTR 计数器布局、目录索引遍历顺序）与参考实现一致，未发现错误。

发现并已修复的缺陷（均有对应的 `test_adv_*` 测试，见第 9 节）：

1. **错误密钥被接受（严重）**：加密但没有目录索引的容器无法用挂载点验证密钥，写入器直接用给定密钥加密新块，结果静默写出游戏读不了的
   容器（`--verify` 时还会以未捕获的 zlib.error 崩溃，留下半成品）。现在先用未改动的 chunk 证明密钥，证明不了就拒绝，除非 `--allow-unverified-key`；
   `key_status` 也不再在没有真正检查时报告 `accepted`。
2. **meta 哈希只比前 20 字节**：FIoChunkHash 尾部不为零的未知布局被误判为 sha1/blake3，写出的条目与其余条目形式不同且 selfcheck 看不出。
   现在比较完整的存储值。v8+ 存全零哈希的容器保持写零。
3. **`--verify` 本身的缺陷**：输入中本就存在的共享块使追加模式 verify 必然失败；解码异常未捕获导致崩溃；CTR IV 不在原始块比对内。
   另外新增检查：输入写前/写后哈希一致、固定 header 字段不变、分区文件与 PartitionCount 一致、16 字节对齐磁盘跨度不越界不重叠、块内起始偏移不变。
4. **写入安全**：追加模式复制分区用的是会覆盖目标的 `shutil.copyfile`（与“全部独占创建”的说法不符），改为独占创建；manifest 的输入哈希改为写之前计算。
5. **MemoryMapped chunk**：只保证首块对齐，不保证整块位于同一分区且连续；也不考虑输入实际使用的更粗对齐。已补上（rebuild 时未改动的 MemoryMapped chunk 同样处理）。
6. **追加起点**只看文件末尾，没有考虑旧块的 16 字节对齐读取跨度（只在输入块起点未对齐时有影响）；rebuild 复制明文块时丢掉原有填充，改为原样复制。
7. **孤立块计数**把仍被其他 chunk 引用的块也算作孤立。
8. **报告与健壮性**：`reserved_nonzero` 漏掉了版本门槛之前的字段（v3 中的 PerfectHash 计数等）；`index:zz`、文件不存在、块大小为 0、未知加密方法
   会产生 traceback；带 UTF-8 BOM 的密钥文件被拒；extract 遇到非 IoStoreError 的解码错误或路径冲突会中断而不是记入 manifest。

README 中修正的过度表述：Oodle 由“已验证”改为“已实现，待复验”；“FIoHash 为 BLAKE3 由格式本身确定”改为“UE 类型定义，依据 retoc”；
“所有新文件都用独占模式创建”在修复前不成立；eFootball 在 CUE4Parse 中“唯一的特殊处理”补充了资产层的另一处；新增说明 `--verify` 不是独立验证。

复审未能解决、仍需真实文件或实机的问题：第 7 节第 9 步列出的运行时问题全部仍然开放；Oodle 路径需在有解码库的环境中复验；
MemoryMapped 16 KiB 下限是假设。

## 12. 新增 chunk 与独立包：`add`、`clone-package`

### 12.1 `add` 做什么

输入一个容器，输出到新目录，文件名不变；写操作的安全规则与 `patch` 相同（第 5 节第 1–3 条），
签名、OnDemand、TOC 版本、密钥验证、TOC 字节级往返这几项预检与 `patch` 共用同一段代码。

| 部分 | 改动 |
|---|---|
| chunk 表 | 末尾追加新的 `FIoChunkId`、`FIoOffsetAndLength`、meta（哈希算法与 `patch` 一样实测判定；flags 去掉 Compressed，保留 MemoryMapped） |
| `.ucas` | 原分区逐字节复制，新数据以不压缩块追加到最后一个分区（加密容器用同一密钥，CTR 每块新 IV），做法与 `patch` 追加模式相同 |
| 目录索引 | 新路径逐级查找或新建目录项（目录名不区分大小写），文件项挂在该目录文件链表的**末尾**，已有文件的遍历顺序不变；字符串表能复用就复用。整段重新序列化，加密容器重新加密（ECB 补零到 16 字节；CTR 用新 IV） |
| 完美哈希（v4+） | 新的 chunk 集合重新生成种子表和溢出表（v5+），所有逐 chunk 数组按槽位重排，目录索引的 UserData 跟着重映射。v4 没有溢出表，找不到种子就拒绝。v1–v3（UE4.26 为 v2）直接追加 |
| 容器头 | 见 12.2；容器头 chunk 本身按 `patch` 的方式整体替换 |
| 不动的部分 | 已有 chunk 的 id、长度、meta、磁盘块（AES-CTR 时连同 IV）、解压后的内容；已有路径；已有包条目（追加方式下只有 ImportedPackages 偏移量整体加 K×条目长度）；压缩方法表；ContainerId；KeyGuid |

拒绝的情况：chunk id 已存在（替换请用 `patch`）、同一 id 给了两次、padding 字节非 0、路径已存在（不区分大小写）、
路径在挂载点之外、容器没有目录索引却要加路径、包 id 已在容器头里、容器头布局判定不唯一、32 字节布局缺 `export_bundles_size`/`load_order`；
（第二次复审新增，见 12.7）新包条目与它自己的包头矛盾（包头里的包名算出的 id ≠ 登记的 id，或 ExportCount/ExportBundleCount ≠ 包头里的导出数/bundle 数，
且容器里已有的包都遵守这两条规则时才拒绝，否则只警告）；TOC meta 之后有未解析的字节（可能是逐 chunk 数据，加 chunk 后会对不上；
`--keep-toc-trailing` 可强制原样保留）；TOC v9 声明了 source hash（布局未知）；TOC v4 且无法判定 chunk 类型编号时按名字给类型
（v4 时 retoc 用 UE4 编号、CUE4Parse 按引擎版本，二者不一致；工具用容器头 chunk 自己的类型 10/6 判定，判定不了就要求写数字类型或完整 chunk_id）；
add-list 里的包引用既不是 `/Root/...` 也不是 `0x` 十六进制（以前缺开头斜杠会被静默算成一个不存在的 id）。
被拒绝时不写任何输出（测试会检查输出目录为空）。

`--verify` 用我们自己的读取器重新打开输出，**按 chunk id** 逐个比对：每个旧 chunk 都还能按 id 解析到、长度、块内偏移、meta、
原始磁盘块和解压后内容都不变；每个新 chunk 能按 id 和按路径找到、内容和 meta 正确；旧路径仍指向原 chunk id，新增路径与请求完全一致；
完美哈希能查到所有 chunk；容器头仍只符合原布局，所有旧包条目解码不变，新条目正确；以及与 `patch` 相同的分区文件、磁盘跨度、输入文件哈希检查。
它同样不是独立验证；独立验证见 12.5。

### 12.2 容器头（ContainerHeader chunk）与两种包条目布局

UE4.26/4.27 的容器头没有魔数和版本号（CUE4Parse `FIoContainerHeader.cs` 的 `BeforeVersionWasAdded` 分支；retoc `container_header.rs`）：

```
u64 ContainerId | u32 PackageCount | TArray<u8> Names | TArray<u8> NameHashes |
TArray<FPackageId> PackageIds | TArray<u8> StoreEntries | CulturePackageMap | PackageRedirects | （填充）
StoreEntries = PackageCount 条定长 FPackageStoreEntry，后面紧跟各条目的 ImportedPackages 数组；
CArrayView = (u32 ArrayNum, u32 OffsetToDataFromThis)，偏移相对于这个 CArrayView 字段本身。
```

| 布局名 | 条目 | 长度 | 来源 |
|---|---|---|---|
| `ue4-32`（原版 4.26/4.27） | u64 ExportBundlesSize、i32 ExportCount、i32 ExportBundleCount、u32 LoadOrder、u32 Pad、CArrayView ImportedPackages（偏移 24） | 32 | CUE4Parse `FFilePackageStoreEntry.cs` 默认分支；retoc `Initial => (24, 32)` [T3] |
| `efootball-16` | i32 ExportCount、i32 ExportBundleCount、CArrayView ImportedPackages（偏移 8） | 16 | CUE4Parse `Ar.Game is GAME_eFootball` 分支；retoc `PreInitial => (8, 16)` [T3] |

**布局不靠猜，按结构判定**（`container-header` 输出 `layout_checks`）：条目表放得下、每个 ImportedPackages 数组都落在条目表之后的
8 字节对齐数据区内、计数非负、（32 字节时）Pad 为 0。只有一种布局通过才自动采用；两种都过或都不过就要求用 `--store-entry-layout` 指定。

用本地能拿到的**真实数据**检验过这个判定（retoc 仓库 `retoc/tests/` 附带的测试文件，MIT 许可；不复制进本仓库，测试在文件缺失时跳过）：

| 文件 | 内容 | 判定结果 |
|---|---|---|
| `issues/issue18/header.bin` | retoc issue #18 的 PesMobile 容器头。ContainerId `0x8FA7AE270B85F529`，小端字节即 issue 中的 chunk id `29f5850b27aea78f…0a`（研究笔记 05 §2.5）。182 个包；StoreEntries 3232 字节 = 182×16 + 40 个导入×8 | 只有 `efootball-16` 通过；按 32 字节读，条目表本身就放不下 |
| `UE4.27/ContainerHeader_1.bin` | 某 UE4.27 游戏，52583 个包 | 只有 `ue4-32` 通过；Pad 全为 0 |
| `issues/issue7/header.bin` | SD 高达（UE 4.27.2），40307 个包，带 11 种语言的 CulturePackageMap | 只有 `efootball-16` 通过（与研究笔记 05 §2.15 的说法一致） |

另外两个可以直接看到的事实：
* 两个真实文件的 StoreEntries 用我们的规范编码（全部条目在前，数组按条目顺序紧排）重新生成，与原文件**逐字节相同**。
* **PackageIds 的顺序**：eFootball（PesMobile）文件**没有排序**（181 对相邻 id 中 88 对是降序），UE4.27 和 SD 高达的文件是升序。
  所以 `--store-order auto` 的规则是：原列表已升序就按升序插入（整段规范重编码，且只在原缓冲区本身是规范形式时才允许），否则追加到末尾。
  追加时旧条目的字节只有 ImportedPackages 的偏移量变化（加 K×条目长度），旧数组原样保留。

这说明：至少 issue #18 报告者那份 **Android 版** eFootball（PesMobile，obb 目录）的容器头是 16 字节布局；它的 PackageIds 无序，
而游戏实际在用这份数据，由此**推断** [T4] 运行时不要求 PackageIds 排序。证据只有一个容器的一个容器头；
**iOS 11.1.1 / build 11.1.101 没有见过**，可能相同也可能不同，必须在真实文件上跑 `container-header` 确认。issue18 文件在 4732 字节的有效内容后还有 60804 个零字节（大概是导出时整块保存），
这些尾部字节按原样保留。

新条目需要的字段：ExportCount、ExportBundleCount、ImportedPackages；32 字节布局还要 ExportBundlesSize、LoadOrder（Pad 写 0）。
* **ExportBundlesSize 的含义没有一手资料**（Epic 源码不可得；retoc 写入时填 0）。`clone-package` 在当前容器里**实测**：
  抽查已有包，看它是否等于该包 ExportBundleData chunk 的大小（`chunk_size`），或等于 chunk 大小减去 Zen 包头（`export_data_size`）。
  测得一致的关系就按它算；测不出来时，包大小没变就照抄源条目，变了就拒绝，要求 `--export-bundles-size copy|chunk-size|export-data-size|N`。
  （改名通常会在名字表末尾追加新包名，所以包大小几乎总会变；只有新包名复用已有名字项时才可能不变。）
* **LoadOrder** 照抄源条目（含义未核实，[T4]）。
* eFootball 的 16 字节布局没有这两个字段，不存在这个问题。

### 12.3 `clone-package` 具体改了哪些字段

做法：读出源包的 ExportBundleData chunk，用 `zen_package.rename_legacy_package` 改名；同一个包 id 下的其他 chunk
（BulkData / OptionalBulkData / MemoryMappedBulkData）逐字节复制到新 id 下；源条目复制成新包条目；然后交给 `add`。
新 FPackageId = CityHash64（新包名转小写后的 UTF-16LE）。**UE4.26 的包头里不存包自己的 id**，id 只由包名算出，所以改名就足够让 id 变。

| 位置 | 字段 | 改动 | 依据 |
|---|---|---|---|
| `FPackageSummary`（64 字节） | `Name`、`SourceName`（FMappedName = 名字表下标 + FName 数字） | **改为指向新包名**：新包名按 UE 的 FName 规则拆出末尾数字（`/Game/F/face_r9_2002` → 名字项 `/Game/F/face_r9` + 数字 2003），名字表里有完全相同的项就复用，否则在名字表**末尾追加一项**；`SourceName` 文本与旧包名相同时跟着改，不同（本地化包）则保留并警告 | CUE4Parse `FPackageSummary.cs`；拆数字规则见 12.7 |
| | `NameMapNamesSize`、`NameMapHashesSize` | 按新名字表重算 | |
| | `NameMapHashesOffset`、`ImportMapOffset`、`ExportMapOffset`、`ExportBundlesOffset`、`GraphDataOffset` | 全部加同一个位移 Δ（名字表变长或变短，哈希表按 8 字节对齐） | |
| | `PackageFlags`、`CookedHeaderSize`、`GraphDataSize`、`Pad`、`NameMapNamesOffset` | **不变** | |
| 名字表 | 已有的各项（包括旧包名那一项） | **一项都不改**（下标和文本都不变）：导出数据里的 FName（本工具不解析）可能用到旧包名那一项，可能带供体的数字也可能带别的数字，改它的文本会悄悄改掉这些引用 | |
| | 新包名 | 见上一行：复用或在末尾追加一项 | retoc `FNameMap::store` |
| | `--rename-name OLD=NEW` 指定的项 | 按**名字项文本**改（一般不需要，见下文）。新文本末尾不能是 `_数字`（UE 从不这样存，会被拒绝） | |
| 名字哈希表 | 对应项 | CityHash64（小写名字）；开头的算法 id 原样保留 | retoc `name_map.rs` |
| 导出表 `FExportMapEntry` | `GlobalImportIndex`（只有 public 导出有值） | 按新对象路径重算：PackageImport 类型 + CityHash64（小写 UTF-16 路径，`.` 和 `:` 换成 `/`）的低 62 位。**改之前先用旧包名重算旧值，必须与存储值全部一致，否则拒绝**（说明这个包不是按这个公式生成的） | retoc `zen_asset_conversion.rs` `build_zen_export_map`、`script_objects.rs` [T3] |
| | 其他字段（`CookedSerialOffset/Size`、`ObjectName`（下标和数字）、Outer/Class/Super/Template、ObjectFlags、FilterFlags） | **不变** | |
| 导入表 | 指向同批克隆包（或 `--rewire` 包）public 导出的条目 | 换成克隆后对应导出的 GlobalImportIndex | 同上 |
| 图数据 | 被导入包的 FPackageId | 属于同批克隆或 `--rewire` 的换成新 id；arc 不变 | CUE4Parse `IoPackage.LoadGraphData` |
| 导出数据 | — | **逐字节不变** | |
| bulk chunk | 内容 | 逐字节复制；chunk id =（新 FPackageId，原 ChunkIndex，原类型），meta flags 同源 | CUE4Parse 按包 id 找 bulk [T3]；运行时 [T4] |
| 目录索引 | 路径 | 源路径中包名部分换成新包名（`--path NEWPKG=PATH` 可指定） | |
| 容器头 | 新包条目 | 复制源条目；ImportedPackages 按上面的映射改；32 字节布局的 ExportBundlesSize 按 12.2 的规则，LoadOrder 照抄 | |
| **容器中的其他包** | 包括原来引用供体的球员包 | **完全不变，仍然引用供体**。这正是目的：供体继续给其他人用，克隆只属于 R9 | |

为什么 `CookedHeaderSize`、`CookedSerialOffset` 不用改：它们是旧 `.uasset+.uexp` 坐标系里的数值（`zen_package.py` 的 `legacy_offset`），
导出内部的绝对偏移（例如贴图的 SkipOffset）也在这个坐标系里；Zen 包头变长变短不影响导出之间的相对位置。CUE4Parse 源码
（`IoPackage.PopulateExports`）从 `GraphDataOffset + GraphDataSize` 开始按 bundle 顺序依次定位导出 [T3]。**测试里 CUE4Parse 只读克隆包的包头**
（名字表、导入表、导出表），没有反序列化导出内容（合成数据里的导出不是真实对象）；导出字节不变由我们自己的解析器逐字节比对。
运行时是否同样定位导出属 [T4]。

改名前后的自检：包头能重新解析；新包名读回正确，`Name` 正好是拆分后的（下标, 数字）；名字表只多出新包名那一项，其余各项（除 `--rename-name`）下标和文本不变；
导出数据、导出表其余字段（含 ObjectName 的下标和数字）、bundle、arc、`PackageFlags` 等都与原包一致。
名字表里其他**包含旧包名**的字符串（例如软引用路径 `/Game/.../face_110718.face_110718`，UE 存成名字项 `/Game/.../face_110718.face` + 数字）
**不会自动改**，会在报告的 `names_mentioning_old_package` 里列出并给出警告。

**`--rename-name` 一般不要用。** 导出对象名不必与包名一致（`/Game/F/face_r9_2002.face_110718` 是合法的对象路径），改包名已足以让包和它的
GlobalImportIndex 独立。`--rename-name` 改的是**名字项的文本**，于是所有用这一项的 FName 一起改名——包括导出数据里的属性值、
材质参数名之类本工具看不见的引用；而 FName 的数字存在每个引用里，不在名字项里。真实数据中 `face_110718` 这样的名字存成
名字项 `face` + 数字 110719，`--rename-name face_110718=…` 会被拒绝并提示这一点；改 `face` 这一项则会把所有 `face_N` 一起改掉。

**同批克隆与 `--rewire`**：脸网格 → 材质实例 → 贴图通常是一条引用链。只克隆网格，材质和贴图仍是共享的。一次 `--package` 多个包，
它们之间的引用会全部改到克隆上。brief 第七节说模型在 pc3600、贴图在 pc3611，是两个容器：先在 pc3611 克隆贴图，再在 pc3600 克隆网格和材质，
加 `--rewire 旧贴图包=新贴图包 --rewire-with <pc3611 的输出>.utoc`。工具会读出新旧两个贴图包，按导出顺序一一对应，生成导入映射
（测试 `test_clone_across_containers_with_rewire` 同时证明了：不加 `--rewire` 时，材质克隆仍然指向共享的供体贴图）。

拒绝的情况：新包名已有 chunk 或已在容器头里；源包没有恰好一个 ExportBundleData chunk；源包头里的包名与 chunk id 对不上；
新包名不是 ASCII（UE4 对非 ASCII 字符的小写规则未核实）；新包名末尾数字超出 int32 规则（是否拆分未核实）；`--rename-name` 没有命中任何名字项
或新文本以 `_数字` 结尾；会造成名字表重复；新包 id 已出现在 `--rewire-with` 给出的容器里；TOC v4 且 chunk 类型编号判定不了；
GlobalImportIndex 公式对不上；源包在本容器的容器头里没有条目；不是 UE4.26/4.27 Zen 包头（UE5 包不支持）。

### 12.4 FPackageId 与 CityHash64 的证据

* 算法：CUE4Parse `FPackageId.FromName`（只把 A–Z 转小写，再按 UTF-16LE 做 CityHash64）、retoc `FPackageId::from_name`（`to_ascii_lowercase`）。
* `iostore.cityhash64` 复用 `zen_package.py` 的纯 Python 实现。测试：Google `city-test.cc` 的 300 组 CityHash64 向量（其中 18 组直接写在测试里，不依赖外部文件）；
  CUE4Parse UE 5.8 测试夹具中由 **Unreal Engine 自己打包**的 97 个包，其 chunk id 全部等于 `package_id_from_name("/Game/...")`；
  CUE4Parse 的 C# `FPackageId.FromName`（新增的 `--package-id` 选项）与我们逐个一致（测试 `test_package_ids_match_cue4parse_fromname`：
  大小写混合、超过 64 字节和超过 256 个字符的长名字、非 ASCII 名字）。
* UE 5.8 的结果能证明 5.8 的规则；4.26 也用同一规则的依据是 CUE4Parse/retoc 对 4.26 游戏同样使用它 [T3]。
  **eFootball 的包名本工程没有见过**（唯一的真实 eFootball 数据是一个容器头，里面只有 id 没有名字），所以对 eFootball 这条仍是推断。
  在真实文件上它会被自动实测：`clone-package` 要求每个源包“包头里的包名（含 FName 数字）算出的 id == 它的 chunk id”，否则拒绝；
  `add` 在登记新包前抽查容器里最多 32 个已有包的同一关系（结果在 manifest 的 `entry_vs_package_header`）。
  包名用的是 FName 的完整文本（`名字项_数字-1`），与 CUE4Parse 的 `FName.Text` 相同。

### 12.5 独立回读（CUE4Parse）

`cue4parse_check` 新增三个选项（见 `cue4parse_check/README.md`）：
* `--container-header`：用 CUE4Parse 的 `FIoContainerHeader` 按所选游戏的布局解码（`GAME_eFootball` = 16 字节，其他 UE4 游戏 = 32 字节），
  并对每个包做 `TryResolve(FIoChunkId(id, 0, ExportBundleData))` 和目录索引里的路径查找；
* `--global global.utoc`：把全局容器和被测容器一起挂进 `StreamedFileProvider`，对目录索引里的每个包**按 FPackageId 和按路径**各加载一次
  `IoPackage`（只读包头：名字表、导入表、导出表），并检查 `FPackageId.FromName(包名) == id`。测试里用一个最小的合成 `global.utoc`；
* `--package-id NAME`：输出 CUE4Parse 计算的 FPackageId。

`tools/tests/test_iostore_add.py` 对每个输出都同时用我们的读取器和 CUE4Parse 检查：两种布局下新包能按 id 和路径找到并加载，
包名、导出的 GlobalImportIndex、导入表与我们的一致；除容器头 chunk（它必然改变）外，所有旧 chunk 的 SHA-256、旧包的包头和旧包条目与原容器完全相同。
还有**反向对照**：用错误的布局读（16 字节数据按 `GAME_UE4_26` 读，或反过来），CUE4Parse 要么解析失败、要么读出与真实值不同的条目——
说明测试确实分辨得出两种布局。真实的 eFootball / UE4.27 容器头也被放进合成容器、追加新包后交给 CUE4Parse 读（183 / 52584 个包全部正确）。

**这些只证明 CUE4Parse 接受我们的输出，不证明 eFootball iOS 运行时接受。** 运行时问题见 12.6。

### 12.6 只有真机能回答的问题

* 运行时是否接受容器头里追加的条目、无序的 PackageIds（Android 真实数据本身就是无序的，只能据此推断可行）、变长的容器头 chunk；
* 新包在原 cook 清单（`.pak` 里的 AssetRegistry.bin 等）中没有登记，会不会影响加载；
* iOS 11.1.1 是否还有其他 Konami 改动（容器头、包头、bulk 寻址）；
* 资源下载/更新系统是否校验容器大小或哈希、会不会把改过的容器重新下载回去；
* 以及第 7 节第 9 步列出的所有问题。

### 12.7 第二次对抗性复审（注册部分）记录

复审方法：逐行对照 CUE4Parse 77b94fa（`FIoContainerHeader.cs`、`FFilePackageStoreEntry.cs`、`FPackageId.cs`、`FExportMapEntry.cs`、
`FPackageSummary.cs`、`IoStoreReader.TryResolve`、`FIoStoreTocResource.cs`、`FName.cs`）和 retoc（`name_map.rs`、`lib.rs`、`version.rs`），
重读 `add` / `clone-package` / `container-header` / `find-refs` 与 `zen_package.rename_legacy_package`，并用 UE 5.8 自己 cook 的夹具做实测。
格式层（容器头字段顺序、16/32 字节条目与 CArrayView 相对偏移、FPackageId、完美哈希读写规则、FExportMapEntry 72 字节与 GlobalImportIndex 偏移 56、
FPackageSummary 各偏移）与参考实现一致。

发现并已修复（每项都有新测试，`tools/tests/test_iostore_add.py` 末尾两节）：

1. **FName 数字（严重，直接影响本项目）**：UE 把 `face_110718` 这样以 `_数字` 结尾的名字存成“名字项 `face` + FName 数字 110719”。
   实测依据：UE 5.8 自己 cook 的夹具里 558 个导出名带数字（如 `BodySetup_1` 存成 `("BodySetup", 2)`），4118 个名字项没有一个以可拆分的 `_数字` 结尾；
   retoc 写 Zen 包时 `FNameMap::store` 也这样拆（`name_map.rs` `break_down_name_string`）。原实现：
   (a) 遇到包名带数字的供体（`FPackageSummary.Name` 的数字 ≠ 0）**直接拒绝**——而本项目的供体槽位就叫 110718 这类编号，真实文件上很可能整条克隆流程走不通；
   (b) 新包名带数字（例子和测试里用的就是 `face_r9_2002`）时把整串写进名字项、数字写 0，这是 UE 从不产生的形式：运行时由字符串构造的
   FName（`face_r9` + 2003）与包自己的 FName（`face_r9_2002` + 0）比较不相等 [T4：运行时后果未在真机验证]；
   (c) 改的是旧包名那一项的文本，导出数据里用到这一项的其他 FName（本工具看不见）会被悄悄改名。
   现在：读取时包名按（名字项, 数字）还原完整文本；新包名按 `split_fname_number` 拆分后复用或追加名字项，旧名字项一律不动；
   `--rename-name` 的新文本以 `_数字` 结尾时拒绝，旧文本是“带数字的完整名”时给出提示。合成夹具新增 `split_numbers=True`（按 UE 的方式存名字），
   新测试用这种夹具克隆 `face_110718` → `face_r9_2002`，CUE4Parse 按 id 和路径加载克隆包，读出的包名、`FromName(包名)==id`、导出 GlobalImportIndex 都一致。
   超出 int32 的末尾数字（是否拆分无法核实）一律拒绝。
2. **新包条目与包头不符时照样写出**：`add` 不检查用户给的 ExportCount/ExportBundleCount 是否等于包头里的导出数/bundle 数，也不检查包头里的包名是否对应登记的 id。
   UE4.26 的加载器据说按包条目里的这两个数分配数组 [T4]。现在先在容器已有的包上实测规则，规则成立时违反即拒绝，否则警告；ImportedPackages 与图数据不一致只警告。
3. **TOC v4 的 chunk 类型编号**：代码把 v4 当作 UE5 编号，研究笔记 05 §2.5 和 retoc（`is_new = version > PerfectHash`）认为 v4 仍是 UE4 编号，CUE4Parse 按引擎版本判断——
   参考实现之间本身有分歧，原代码却把其中一种当作事实。按名字写类型（`add` 的 `"type": "ExportBundleData"`）在 v4 容器上可能写出错误的类型号。
   现在 v4 由容器头 chunk 自己的类型（10 = UE4，6 = UE5）判定，判定不了就拒绝按名字写类型；`info` 输出判定依据。与 eFootball（推测 v2/v3）无关。
4. **未知的逐 chunk 数据**：TOC meta 之后的未解析字节和 v9 的 source hash 在加 chunk / 重排完美哈希后会与 chunk 表对不上，原实现原样保留不报。现在默认拒绝（前者可用 `--keep-toc-trailing`）。
5. **add-list 健壮性**：包引用缺开头 `/`（如 `Game/X`）会被静默算成一个不存在的包 id；坏 JSON、缺 `file`、`0xZZ`、超出 16 位的 chunk_index 会出 Python traceback。现在全部以退出码 2 干净报错。`find-refs 0xZZ` 同样。
6. **跨容器 id 冲突**：克隆的新包 id 只在当前容器里查重；现在也查 `--rewire-with` 给出的容器（运行时包存储是全局的，一个 id 只能有一个包）。
7. **目录索引 UserData 越界**：v4+ 重建完美哈希时，孤立文件项的越界 UserData 会在写出一半后抛 IndexError；现在写之前检查并拒绝。
8. **`find-refs 110718` 在真实数据上会漏报**：它只搜名字项文本，而 `face_110718` 在名字表里只剩 `face`（数字存在引用里）。现在同时搜包头里的完整 FName
   （包名、导出对象名，输出字段 `header_fnames`）。导出数据内部的 FName 不解码，其中以 FName 数字形式出现的编号仍然搜不到——
   第 13.1 节第 2、4 项的“名字表里有供体编号”的判断要考虑这一点。

新增的对抗测试还包括：`add` 前后 144 字节 TOC 头逐字节比较（保留区填入垃圾值和一个未知 flag 位，只允许条目数、块数、目录索引大小、分区数变化），
旧的 chunk id 表、偏移表、块表、meta 是输出表的逐字节前缀（容器头 chunk 除外）；故意写错一个完美哈希种子时 `--verify` 必须报错（证明这项检查不是空的）；
空数组带非零偏移（非规范形式）的旧条目在追加后字节不变、不允许重排。

仍然开放（只有真实文件或真机能回答）：12.6 的全部问题；eFootball 包名是否带 FName 数字、FPackageId 规则在 eFootball 上是否成立（由 12.4 的自动检查回答）；
UE4.26 运行时对包条目计数、ImportedPackages 与图数据关系的实际要求。

## 13. “独立绑定”在容器之外还需要什么

容器工具解决的是**资源层**：一个与供体互不影响的新包（新包名、新 FPackageId、自己的 bulk chunk、自己的路径和包条目）已经在容器里，
能按 id 和路径找到。**绑定层**——游戏里让“R9 这名球员”去用这个新包的那张表或那条规则——**目前完全未知**。
没有真实文件之前，本工程不知道它在哪里、长什么样，也不会假设它存在某种形式。下面是拿到文件后要找的具体东西和做法。

### 13.1 要在真实数据里找的东西

| # | 要找什么 | 怎么找（只读） | 能说明什么 |
|---|---|---|---|
| 1 | 脸、头发、脸部贴图包的**命名规则** | `iostore.py list pc3600_mobile_ios.utoc --glob '*110718*'`，pc3611 同样；再对几名已知球员的脸部编号各查一次 | 路径里如果带脸部编号（如 `.../110718/...` 或 `face_110718`），游戏多半是“编号 → 按规则拼出包名” |
| 2 | **谁引用了供体** | `iostore.py find-refs pc3600_mobile_ios.utoc /Game/<供体包名>`；`find-refs ... 110718` | 列出导入供体的包（容器头 ImportedPackages、包导入表、图数据）和名字表里提到它的包。若某个 DataTable/映射资产的名字表里有供体路径，它就是绑定表的候选 |
| 3 | 外观数据中的**脸部编号字段** | 研究笔记 06 的 `PlayerAppearance` 解码：找出几名球员（亨利 → 110718，以及几名有独立脸模的球员）记录中等于路径编号的字段 | 确认“球员 → 脸部编号”的字段位置和编码；独立绑定 = 给 R9 一个新编号，并让该编号对应新包 |
| 4 | 编号是否有**白名单/范围表** | 在 CPK 数据表和容器的 DataTable 里搜索已知编号；看是否存在“合法脸部编号”列表、授权脸标志 | 新编号可能还要在某张表里登记，否则游戏会退回通用脸 |
| 5 | 主程序里的**路径拼接格式串** | 对已解密的主程序做 `strings`（只读，不改）：找 `face`、`%d`、`/Game/`、`pc3600` 等片段 | 例如 `.../face_%06d`：说明新包必须遵守同样的命名规则，新编号也要符合格式 |
| 6 | 是否使用 **AssetRegistry.bin** 查找脸部资源 | 解出同名 `.pak` 里的 `AssetRegistry.bin`，搜索供体包名 | 若是，新包还要登记进资产注册表——**本工具没有实现** |
| 7 | 容器头布局与 TOC 细节 | `iostore.py selfcheck --deep`、`container-header`、`cue4parse_check --container-header --game GAME_eFootball` | 确认 iOS 也是 16 字节条目、TOC v2、无签名等前提 |
| 8 | 是否只挂载清单里的容器 | 应用下载目录里的清单文件、实机日志 | 决定新包放进现有容器（本工具的做法）还是能放进新的 `_P` 容器 |

### 13.2 拿到文件后工具怎么用

```bash
T=tools/native/iostore.py
# 0. 只读备份 + 自检；实测容器头布局（两项 layout_checks 只能有一项 ok）
python3 $T selfcheck pc3600_mobile_ios.utoc --deep
python3 $T container-header pc3600_mobile_ios.utoc
# 1. 找到供体及其共用者（13.1 第 1、2 项）
python3 $T list pc3600_mobile_ios.utoc --glob '*110718*'
python3 $T find-refs pc3600_mobile_ios.utoc /Game/<供体脸包>
# 2. 先在 pc3611 克隆贴图，再在 pc3600 克隆网格和材质，并把引用改到克隆上
python3 $T clone-package pc3611_mobile_ios.utoc --out-dir out/3611 --verify --package /Game/<供体贴图>=/Game/<R9贴图>
python3 $T clone-package pc3600_mobile_ios.utoc --out-dir out/3600 --verify \
    --package /Game/<供体脸>=/Game/<R9脸> --package /Game/<供体材质>=/Game/<R9材质> \
    --rewire /Game/<供体贴图>=/Game/<R9贴图> --rewire-with out/3611/pc3611_mobile_ios.utoc
#    看 manifest 的 clone.packages[*].package_name_fname：旧包名在真实数据里是否带 FName 数字（如 face + 110719），
#    新包名按同样规则存（12.7 第 1 项）；names_mentioning_old_package 列出的软引用不会被改
# 3. 只在克隆上做原位替换：R9 头模（inplace_patch.py mesh-patch-positions）和贴图（texture-replace）
python3 $T extract out/3600/pc3600_mobile_ios.utoc --glob '*<R9脸>*' --out-dir out/ex
#    ……用 inplace_patch.py 生成新的 .uasset/.ubulk……
python3 $T patch out/3600/pc3600_mobile_ios.utoc --out-dir out/3600b --replace <R9脸路径> new.uasset --verify
# 4. 游戏侧绑定：按 13.1 找到的规则，只把 R9 的外观记录改为指向新编号/新包（由数据工具和主控完成）
# 5. 回读：hash-all + diff（只应多出新 chunk、只变容器头和 R9 自己的 chunk）；CUE4Parse 独立回读
python3 $T diff pc3600_mobile_ios.utoc out/3600b/pc3600_mobile_ios.utoc
/opt/tools/cue4parse_check_bin/Cue4ParseCheck out/3600b/pc3600_mobile_ios.utoc --game GAME_eFootball --container-header
# 6. 实机（隔离客户端）：R9 正常显示；其他用供体 110718 的球员外观不变；回退 = 还原第 0 步的原文件
```

新包名要按 13.1 第 1、5 项找到的规则来定；如果游戏用“编号 → 包名”的规则，就选一个没被占用的新编号拼出包名，
`clone-package` 前先用 `package-id` 和 `find-refs` 确认它在所有容器里都不存在。

## 14. 备用方案：隔离测试客户端里的原位替换（**不是生产级独立绑定**）

任务书第八节允许：在找到绑定机制之前，先在**完全隔离的测试客户端**里验证替换效果。做法就是第 7 节的原位 `patch`：
直接替换供体 110718 的网格和贴图 chunk。

* **后果**：所有使用这个供体的球员都会变成 R9 的脸（亨利项目的老问题）。`find-refs` 能列出容器内引用它的包，但列不出数据表里引用该编号的球员。
* **用途只限于**：在真机上验证 R9 头模、贴图、两级 LOD、发型和动作表现；确认运行时接受我们写出的容器（追加块、meta、不压缩块）。
* **禁止**：把这个结果当作 R9 的正式交付或独立绑定，或者装到日常使用的客户端里。交付说明和验收记录里必须写明“共享槽位替换，仅供测试”。
* **隔离**：单独的一份客户端安装（或单独的设备/账号环境），不连接官方在线服务（研究笔记 05 §5.3），保留原文件和 sha256 以便还原。
* 这条路验证过的东西（头模、贴图、容器写法）可以直接搬到第 13 节的独立绑定上：同样的替换数据，改为作用在克隆包上。
