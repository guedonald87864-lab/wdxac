# iostore.py — UE IoStore（.utoc / .ucas）读取器与补丁写入器

`tools/native/iostore.py` 是纯 Python 的命令行工具，用来读取、解包、比对和修补 UE IoStore 容器。
目前**还没有任何真实 eFootball 文件**，所以本工具只按公开格式（CUE4Parse 源码）编写，并只在合成数据上做过验证。
拿到真实文件后，必须先按第 7 节的清单逐项核验，才能用它去修补。

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
```

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

## 10. 未实现 / 局限

* 不重新压缩：新块一律不压缩，容器会变大，I/O 也会增加。
* 不能新增 chunk 或路径（目录索引、chunk 表、完美哈希都不动），只能替换已有 chunk 的内容。
  任务书第八节要求的“独立球员资源”需要新增 chunk 及容器头条目，或者做一个独立的 `_P` 补丁容器；这两条路都没有实现，也都没有验证。
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
