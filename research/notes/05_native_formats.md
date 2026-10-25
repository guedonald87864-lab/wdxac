# 05 原生资源格式与工具链：eFootball iOS（Unreal Engine）球员头部资源

> **用途**
> - 为 2002 世界杯决赛版罗纳尔多（R9）的头模、发型、贴图进入 eFootball iOS 原生资源提供格式依据。
> - 内容包括：引擎版本证据、IoStore 容器逐字节结构、与面部相关的 cooked 资产结构、开源工具能力与许可、eFootball 社区已知信息、iOS 签名与存储约束、ASTC 编码规范。
> - 最后给出「解析 → 解码 → 修改 → 重编码 → 重打包 → 回读验证」的工具链计划和风险清单。
>
> **本文没有使用任何真实的 eFootball 文件**
> - 本工程目前**没有** IPA、`.utoc/.ucas`、可执行文件或任何 cooked 资产。
> - 下文所有结构都来自公开源码和公开资料。凡是写着「eFootball 实际是……」的地方，都标明了证据等级。
> - 拿到真实文件后，必须按 §8 的步骤逐项核验。
>
> **证据分级**（与任务书一致）
> - **[T1]** 官方或一手资料：Konami、Apple、Epic、Arm 官方文档。
> - **[T2]** 有信誉的媒体或参考资料。
> - **[T3]** 维基、社区、模组论坛，以及第三方开源解析器源码。CUE4Parse、retoc、UEViewer 都属于这一级。它们是可以编译运行、可以复现的代码证据，但**不是 Epic 官方引擎源码**。
> - **[T4]** 本组的推断或估计。**T4 不是事实。**
>
> **关于 Epic 官方源码**
> - Epic 的官方 UE 源码仓库需要关联 Epic 账号才能访问，本环境读不到。
> - 因此凡是「UE 运行时实际怎么做」的说法，最多只到 T3（第三方实现）。
> - 运行时行为（是否校验签名、是否接受不压缩的块等）一律标为 T4 或「未证实」。
>
> **敏感信息**
> - 公开的 GitHub issue [S11] 中贴出过 eFootball 移动版的 AES 密钥字符串。本文**不转录**任何密钥。
> - 工具链设计为：用户在自己的机器上提供密钥（`--aes-key-file` 或环境变量）。密钥不进仓库，不进日志，也不进 manifest。
>
> **源码版本基准**（本地检出，可复现）
>
> | 仓库 | commit | 日期 | 本地路径 |
> |---|---|---|---|
> | FabianFG/CUE4Parse | `77b94fa` | 2026-10-25 | `/opt/tools/ref/CUE4Parse` |
> | trumank/retoc | `885a8da` | 2026-08-13 | `/opt/tools/ref/retoc` |
> | gildor2/UEViewer | `a0bfb46` | 2024-02-23 | 临时克隆 |
> | rm-NoobInCoding/UnrealReZen | `bf9e8de` | 2026-04-19 | 临时克隆 |
> | ARM astcenc | v4.8.0 | — | 本机 `/usr/local/bin/astcenc` |
>
> 文中写「源码 X.cs」时，指上表 commit 下的文件。
>
> **研究基准**：任务书指定 eFootball iOS 11.1.1 / build 11.1.101。App Store 当前版本见 §6.1。

---

## 0. 结论速览

| # | 问题 | 结论 | 等级 |
|---|---|---|---|
| 1 | 引擎 | Konami 官方只说「用 Unreal Engine 重建」，**没有公布版本号**。<br>社区与代码证据一致指向**定制的 UE 4.26**，2022–2026 各代都是如此。 | 官方说法 T1；版本号 T3 |
| 2 | Konami 定制的直接证据 | ① CUE4Parse 于 2026-07-03 新增 `GAME_eFootball = GAME_UE4_26 + 29`。<br>② 唯一的 IoStore 特例：容器头里每个包条目只有 **16 字节**（没有 `ExportBundlesSize`、`LoadOrder`、`Pad`）。原版 4.26 是 32 字节。<br>③ retoc issue #18（2025-03，PesMobile，UE 4.26.2）需要用 `--override-container-header-version PreInitial` 才能解析，说明的是同一件事。 | T3 |
| 3 | 移动版 chunk 类型编号 | 采用 **UE4 旧枚举**：Android 版（PesMobile，2025-03）容器头 chunk id 末字节为 `0x0a`，即 UE4 的 `ContainerHeader=10`。UE5 中该值为 6。<br>iOS 版**未直接证实**；[T4] 同一工程，推断相同。 | T3（issue 原文）+ T4（解读） |
| 4 | 资源包命名 | PC 版为 `pcNNNN_console_win`（Steam）和 `pcNNNN_console_wgd`（微软商店），每组是 `.pak/.ucas/.utoc` 三件套，放在游戏根目录的 `pak/` 下。<br>国服移动版为 `pc*` 和 `nt*`，两者用不同密钥，`pc0000` 是 zip。<br>移动版 `pc3600_mobile_ios`、`pc3611_mobile_ios` 的含义**未在公开资料中找到**，只来自任务书和亨利项目的内部分析。 | T3 / 内部 |
| 5 | 混合资源 | UE4 原生包与沿袭 PES 的 **CPK** 包并存。<br>球员数据库 `Player.bin`、`PlayerAppearance.bin` 等在 CPK（`dt870_*`、`dt200_*`）里，新版本还套了一层加密的 WESYS 容器。 | T3 |
| 6 | IoStore 关键结构 | 头 144 字节；chunk id 12 字节；偏移/长度条目 10 字节（大端 5+5）；压缩块条目 12 字节（小端 5/3/3/1）。<br>块级 AES-256-ECB 按 16 字节对齐，目录索引也加密，TOC 表本身不加密。详见 §2。 | T3（多实现交叉一致） |
| 7 | 现成写入工具与 eFootball 的兼容性 | **没有一个现成工具能直接写出与 eFootball 容器头布局一致、且保留压缩和加密的容器。**<br>· UnrealReZen 固定写 TOC v3 和 32 字节包条目，与 4.26（v2）和 eFootball（16 字节）都不符。<br>· retoc 能写 PreInitial（16 字节）包条目，但不压缩、不加密、不签名。<br>· 本工程 `tools/native/iostore.py` 走「原容器原地追加」路线：保留原版本号、chunk id 和目录索引，替换的块以不压缩形式写入，按需加密。 | 工具代码 T3；兼容性判断 T4 |
| 8 | Oodle | 专有库。Linux 上可以从 OodleUE 镜像取得 `liboo2corelinux64.so.9`（2.9.10），受 UE EULA 约束。<br>开源纯解码实现有 ooz（GPL-3）和 oozextract（crates.io 标为 MIT）。<br>**eFootball 实际用哪种压缩，未找到公开资料。** | T1/T3；eFootball 部分未找到 |
| 9 | iOS 签名 | 改动 `.app` 包内**任何**资源都会破坏代码签名的资源封印，必须整体重签。<br>App 容器（Documents、Library）里的文件不在代码签名覆盖范围内，但应用自己可能做完整性校验。<br>eFootball iOS 下载数据的存放位置**未找到**。 | 签名 T1；位置未找到 |
| 10 | ASTC | UE 把每级 mip 存成**裸 ASTC 块**：每块 16 字节，没有 `.astc` 文件头。<br>cooked 数据里像素格式以字符串形式存储，如 `"PF_ASTC_6x6"`。<br>sRGB 不在块里，由纹理的 `SRGB` 属性决定使用 sRGB 还是线性视图。<br>UE4 ASTC 法线贴图的通道布局（RG 还是 L+A）**未证实**，必须用 donor 解码判定（§7.4）。 | T1/T3；法线布局待核验 |

---

## 1. eFootball 使用的 Unreal Engine 版本

### 1.1 官方声明（Konami）[T1]

**2021-07-21 Konami 新闻稿**（美区 [S01]、欧区 [S02]，内容相同）
- 原文："Dramatically rebuilt in Unreal® Engine, providing many advances on and off the pitch"。
- 系列制作人木村誠太郎（Seitaro Kimura）的话："we've made a number of modifications to virtually create a new football game engine"。
- 新闻稿还说 "Motion Matching" 会在包括移动端在内的全平台使用，移动版 "will be detailed at a later date"。
- **新闻稿没有给出 UE 版本号。**

**移动端的引擎沿革**
- 2018-12，PES 2019 Mobile 更新为 Unreal Engine 4。
  - mac4ever [S05] 原文："utilise désormais le moteur d'Epic Unreal Engine 4"，该更新要求 iOS 9.0。[T2]
  - 9to5mac [S06] 报道同一次更新，称从 UE3 升级到 UE4。[T2，**只看到搜索摘要，未直接读取页面**]（核查更正：核查时已直接读取页面。标题 "Pro Evolution Soccer 2019 for iOS updated with Unreal Engine 4, 12 new leagues"，2018-12-17 9:33 am PT，原文 "upgrading from Unreal Engine 3 to Unreal Engine 4"。不再只是搜索摘要，结论确认）
- 这说明移动端在 2018 年已经用 UE4，比主机版 2021 年转用 UE 更早。当时的 UE4 小版本号**未找到**。

### 1.2 社区与代码证据（版本号）[T3]

| 证据 | 内容 | 日期 | 来源 |
|---|---|---|---|
| Gildor 论坛主题「eFootball 2022-2026」 | 版主 spiritovod 写："UE4 version: 4.26"；"It's the first game from the series using UE4 engine and it's also using mixed resources"（UE4 原生包与 cpk 混用）。<br>Steam 版的全局容器名为 `global_console_win.utoc/ucas`，用 FModel 时需改名为 `global.utoc/ucas`。<br>"For mobile version you don't need to rename global container"。 | 2021-09-30 建帖，2026-02-15 最后编辑 | [S07] |
| Gildor 论坛主题「[UE4] eFootball PES Mobile」（国服《实况足球》，pes.163.com） | UE 4.26。<br>"pc0000 is actually zip file and should be extracted first"。<br>"pc* paks are using key for global/pc version while nt* paks are using separate key"。 | 2023-07-12 | [S08] |
| CUE4Parse commit `7fdb7b9a`「eFootball support」（作者 LongerWarrior） | 在 `EGame.cs` 中新增 `GAME_eFootball = GAME_UE4_26 + 29`。<br>改动 `FFilePackageStoreEntry.cs`：eFootball 的包条目只读 `ExportCount`、`ExportBundleCount`、`ImportedPackages`。<br>改动 `FInstancedStaticMeshInstanceData.cs`：多跳 16 字节。 | 2026-07-03 | [S09] |
| retoc issue #18 | 报告者使用 UE 4.26.2，路径为 `...\obb\PesMobile\Content\Paks`。<br>报错 "Failed to parse ContainerHeader (FIoChunkId { chunk_id: "29f5850b27aea78f0000000a" ... })"。<br>维护者的解决办法是 `--override-container-header-version PreInitial`，报告者确认 "it worked"。 | 2025-03-06 至 03-15 | [S11] |
| pesmodding 教程「eFootball detailed texture replacement」 | 要求 "Unreal Engine 4.26.2"，工程名必须是 "PesConsole"。<br>把打出的 `pakchunk1-WindowsNoEditor` 三件套改名为 `pc3607_console_win` 后替换原文件（球衣贴图）。（核查补注：页面标题为 "eFootball 2023 Detailed Texture Replacement Tutorial"，由 G. Leroy 发布，教程署名 Cadi Lillian。其余细节逐条确认） | 2023-07-04 | [S15] |

> **核查补注（S07 原文）**
> - 首帖原句："It expects global_console_win.utoc/ucas as default naming for global container, which is valid for steam version"。这里说的是 **umodel 专用构建**的默认命名。MS Store 等其他版本要把全局容器改成这个名字（第 9 楼：`global_console_wgd` → `global_console_win`）。
> - 「改名为 `global.utoc/ucas`」出自**第 2 楼（2021-10-05）**，针对的是 FModel，不在首帖。
> - 移动版那句的完整原文是 "For mobile version you don't need to rename global container **or use game specific override**"，并要求改选正确的引擎版本。也就是说，umodel 读移动版时**不需要** eFootball 专用覆盖选项。
>   - 这与 §1.3 的「16 字节包条目」不一定矛盾：umodel 可能根本不读包条目。此推断【未核实】。
> - 同帖说 "It's the first game from the series using UE4 engine"。这与 §1.1「PES 2019 Mobile 在 2018-12 已改用 UE4」[S05][S06] 字面上冲突。合理的解释是版主只指主机/PC 系列 [T4]。两种说法都保留。

### 1.3 解读与冲突

**[T4] 结论**：eFootball 2022 至今（PC、主机、移动）基于 UE **4.26 的 Konami 定制分支**。证据：
- Gildor 2026-02 仍标注 4.26；
- CUE4Parse 2026-07 把它作为 4.26 变体加入；
- retoc #18 的报告者用 4.26.2。

**定制痕迹**（多个独立来源说的是同一件事）：
- 容器头包条目从原版 4.26 的 32 字节缩成 16 字节。
- retoc 维护者在 issue #7（另一款游戏）中解释：这种 16 字节布局来自 Epic 在 "4.27.2 与 5.0.0 之间" 的一个提交（"removes ExportBundlesSize/Load Order/Pad from the container header"）[S12]。
- [T4] 由此推断 Konami 把该改动回移到了自己的 4.26 分支。

**冲突（未解决）**
- 2023 年的 PC 教程 [S15] 用**原版** UE 4.26.2 编辑器打包的容器，替换 `pc3607_console_win` 后能在游戏里生效。
- 原版 4.26.2 的容器头应是 32 字节条目，这与 2025–2026 年移动版和 CUE4Parse 所示的 16 字节布局矛盾。
- 可能的解释（都是 T4）：
  - (a) Konami 在 2023 年之后才改了布局；
  - (b) PC 版与移动版的布局不同；
  - (c) 运行时实际不读被替换容器里的这些字段。
- **只能用真实文件核验**（§8 步骤 1）。

**仍未找到的信息**
- Konami 是否已迁移到 UE5：**未找到任何证据**。
- 11.1.1 / 11.1.101 是否仍是 4.26：**未证实**。

**拿到文件后的核验方法 [T4 建议]**
- 读 `.utoc` 第 16 字节的 TOC 版本：4.26 预期为 2。
- 看 chunk 类型字节：旧枚举的 ContainerHeader 是 10，新枚举是 6。
- 看容器头包条目长度：16 或 32 字节。
- 在可执行文件中搜索 `++UE4+Release-4.` 之类的版本字符串。这是 UE 的惯例，但**未证实** Konami 保留了它。

### 1.4 工程名与目录 [T3]

| 平台 | UE 工程名或路径 | 来源 |
|---|---|---|
| PC（Steam） | `eFootball\PesConsole\Content\Paks\`；EvoMod 把自己的包放进其中的 `~mods` 子目录（该目录是否为游戏自带未知）。<br>官方 `pcNNNN_console_win.*` 在根目录的 **`eFootball/pak/`**。<br>另有 `eFootball\cpk\`（如 `dt200_console_all.cpk`）。 | [S15][S16][S17] |
| PC（微软商店） | 文件名后缀为 `_wgd`，需要把 `_win` 改名为 `_wgd`。 | [S16] |
| Android | `obb\PesMobile\Content\Paks`（报告者解包 OBB 后的路径） | [S11] |
| iOS | **未找到**。<br>[T4] 按 UE4 惯例，可能在 `Payload/<App>.app/cookeddata/pesmobile/content/paks/`，需用 IPA 核对。 | — |

**PC 模组的覆盖方式 [T3]**
- 把官方 `pc3610_console_win.*` 从 `eFootball/pak` 复制到 `PesConsole/Content/Paks`，再改名为 `pc3610_console_win_P.*` [S17]。
- [T4] 这说明 UE 标准目录下带 `_P` 后缀的补丁包，挂载优先级高于 Konami 自定义 `pak/` 目录中的原包。移动端是否有同样的机制，**未知**。

---

## 2. IoStore 容器格式（UE4.25–4.27 与 UE5.x）

### 2.0 文件组成

**容器由以下文件组成**
- `X.utoc`：目录表（table of contents）。
- `X.ucas`：数据。v3 起可以分区为 `X_s1.ucas`、`X_s2.ucas`……
- `X.pak`：同名配套 pak。
  - Epic Games Japan 的 UE4.25 IoStore 讲解 [S14][T1]：IoStore 在 4.25 为 Experimental、4.26 为 Beta；`.ucas` 是"コンテナファイル"，`.utoc` 是"目次ファイル"；`.pak` 保留访问较少的文件。【未核实】核查时 SlideShare 页面无法加载（WebFetch 和 curl 都只拿到错误页），以上内容未能复读。此条不影响工具链结论。
  - 社区所有打包流程都是三件套同时出现 [S15][S16][S49][S48]。
  - [T4] 运行时很可能通过挂载 `.pak` 来带出同名的 IoStore 容器，所以替换时三件套的名字要一致。
- 全局容器：UE4 为 `global.utoc/.ucas`，eFootball PC 为 `global_console_win.*` [S07]。
  - UE4 的全局容器存放 `LoaderGlobalNames`、`LoaderGlobalNameHashes`、`LoaderInitialLoadMeta`（脚本对象）、`LoaderGlobalMeta`。
  - UE5 改为一个 `ScriptObjects` chunk（CUE4Parse `IoGlobalData.cs`）[T3]。
  - 解析 4.26 包的导入（脚本对象名）需要它。

### 2.1 TOC 版本 `EIoStoreTocVersion`（`.utoc` 偏移 16 处的 uint8）[T3]

| 值 | 名称 | 引入的变化 | 首个对应 UE 版本 |
|---|---|---|---|
| 0 | Invalid | — | — |
| 1 | Initial | — | 4.25？（见下方冲突） |
| 2 | DirectoryIndex | 目录索引 | retoc：4.25、4.26 |
| 3 | PartitionSize | 多分区 `.ucas`，头里有 PartitionCount 和 PartitionSize | UEViewer 注释："UE4.27"；retoc：4.27 |
| 4 | PerfectHash | 完美哈希种子表 | UE5 早期？（retoc 无直接映射） |
| 5 | PerfectHashWithOverflow | 加溢出表；**chunk 类型改用新枚举**（retoc 判定条件为 `version > PerfectHash`） | retoc：5.0–5.3 |
| 6 | OnDemandMetaData | OnDemand 附加 meta | retoc：5.4 |
| 7 | RemovedOnDemandMetaData | — | — |
| 8 | ReplaceIoChunkHashWithIoHash | meta 哈希从 32 字节缩为 20 字节加 3 字节填充 | retoc：5.5–5.7 |
| 9 | AddedSourceHashes | — | CUE4Parse 枚举中有 |
| 10 | ContainerEncryptionMethod | 头里有加密方法字段，支持 AES-CTR 和每块 IV | CUE4Parse 只在 `GAME_UE6_0` 读取 IV 计数 |

- 来源：CUE4Parse `FIoStoreTocHeader.cs`（枚举）、retoc `version.rs`（`toc_version()` 映射）、UEViewer `IOStoreFileSystem.cpp`（只到 PartitionSize，注释 "UE4.27, allows to use multi-volume (partitioned) ucas files"）[S09][S10][S13]。
- **冲突**：4.25 用的是 v1 还是 v2，retoc 与 UEViewer 的说法不完全一致。这与 eFootball 无关，不再深究。
- **eFootball**：[T4] 按 4.26 推断为 v2。Android 版的 chunk 类型使用旧枚举（§2.5），与 v≤4 一致。

### 2.2 `FIoStoreTocHeader`（固定 144 字节，小端）[T3]

来源：CUE4Parse `FIoStoreTocHeader.cs`（`SIZE = 144`）、retoc `lib.rs: struct FIoStoreTocHeader`、UEViewer。三者字段一致。

| 偏移 | 大小 | 字段 | 说明 |
|---|---|---|---|
| 0 | 16 | `TocMagic` | ASCII `-==--==--==--==-` |
| 16 | 1 | `Version` | 见 §2.1 |
| 17 | 1 | `Reserved0` | |
| 18 | 2 | `Reserved1` | |
| 20 | 4 | `TocHeaderSize` | = 144 |
| 24 | 4 | `TocEntryCount` | chunk 数 N |
| 28 | 4 | `TocCompressedBlockEntryCount` | 压缩块数 B |
| 32 | 4 | `TocCompressedBlockEntrySize` | = 12（用于自检） |
| 36 | 4 | `CompressionMethodNameCount` | M |
| 40 | 4 | `CompressionMethodNameLength` | = 32（retoc 写死 32） |
| 44 | 4 | `CompressionBlockSize` | 每块解压后的最大字节数。retoc 和 UnrealReZen 写 0x10000（64 KiB）；Epic Oodle 文档建议 256 KB [S35]。**以实际文件为准** |
| 48 | 4 | `DirectoryIndexSize` | 目录索引的字节数 |
| 52 | 4 | `PartitionCount` | v≥3 有效，否则视为 1 |
| 56 | 8 | `ContainerId` | uint64；也用作容器头的 chunk id |
| 64 | 16 | `EncryptionKeyGuid` | FGuid；全零表示主密钥（CUE4Parse/ZenTools 的 keys.json 用 GUID 作键 [S46]） |
| 80 | 1 | `ContainerFlags` | 见 §2.3 |
| 81 | 1 | `Reserved3`，v10 起为 `EncryptionMethod` | v10 前由 Encrypted 标志推出为 AES（ECB） |
| 82 | 2 | `Reserved4` | |
| 84 | 4 | `TocChunkPerfectHashSeedsCount` | v≥4 |
| 88 | 8 | `PartitionSize` | v≥3；否则视为无穷大 |
| 96 | 4 | `TocChunksWithoutPerfectHashCount` | v≥5 |
| 100 | 4 | `Reserved7` | UE6/v9 起，CUE4Parse 读作 `TocSourceHashCount` |
| 104 | 40 | `Reserved8[5]` | uint64×5；UE6 起 CUE4Parse 读 `EncryptionIVCount` 等 |

### 2.3 标志位

**`EIoContainerFlags`**（uint8）[T3]

| 位 | 名称 |
|---|---|
| 0x01 | `Compressed` |
| 0x02 | `Encrypted` |
| 0x04 | `Signed` |
| 0x08 | `Indexed`（有目录索引） |
| 0x10 | `OnDemand`（UE5） |

**`FIoStoreTocEntryMetaFlags`**（uint8）

| 位 | 名称 |
|---|---|
| 0x01 | `Compressed` |
| 0x02 | `MemoryMapped` |

### 2.4 TOC 主体各段顺序

紧跟在头后面，各段之间没有填充。来源：CUE4Parse `FIoStoreTocResource.cs`、retoc `Toc::de`。[T3]

| # | 段 | 每项大小 | 条件 |
|---|---|---|---|
| 1 | `ChunkIds[N]` | 12 | 总是 |
| 2 | `ChunkOffsetLengths[N]` | 10 | 总是 |
| 3 | `ChunkPerfectHashSeeds[S]`（int32） | 4 | v≥4 且 S>0 |
| 4 | `ChunkIndicesWithoutPerfectHash[O]`（int32） | 4 | v≥5 且 O>0 |
| 5 | `CompressionBlocks[B]` | 12 | 总是 |
| 6 | `EncryptionIVs[...]` | — | 仅 v10 的 AES-CTR |
| 7 | `CompressionMethodNames[M]` | 32（以 NUL 结尾，不足补零） | 总是 |
| 8 | 签名段 | 见 §2.10 | `Signed` |
| 9 | 目录索引 | DirectoryIndexSize | v≥2 且 `Indexed`；**`Encrypted` 时整段加密** |
| 10 | `ChunkMetas[N]` | v<8：33（32 字节 FIoChunkHash + 1 字节 flags）<br>v≥8：24（20 字节 FIoHash + 1 字节 flags + 3 字节填充） | 总是写入 |
| 11 | OnDemand meta | 20×N + 20×B | 仅 v6 且 `OnDemand` |

注意 CUE4Parse 的一个解析缺陷：同时请求「目录索引 + meta」时，它不跳过目录索引，导致把目录索引当成 meta 读（本工程 `cue4parse_check/README.md` 已记录）。

### 2.5 `FIoChunkId`（12 字节）与 `EIoChunkType`

**字节布局** [T3]：

| 字节 | 内容 |
|---|---|
| 0–7 | `ChunkId`，uint64 小端。对包数据来说就是 FPackageId |
| 8–9 | `ChunkIndex`，uint16 |
| 10 | 填充 |
| 11 | `ChunkType` |

- `ChunkIndex` 的字节序有冲突：CUE4Parse 构造函数注释为 `NETWORK_ORDER16`（大端）；retoc `FIoChunkId::create` 按小端写入。普通包数据的索引都是 0，不受影响。

**FPackageId**
- = CityHash64(小写化后的 UTF-16LE 包名，如 `/Game/...`)。
- 来源：CUE4Parse `FPackageId.FromName`；本工程 `zen_package.py hash` 已实现。[T3]

**容器头 chunk**
- id 为 `(ContainerId, 0, ContainerHeader)`（CUE4Parse `IoStoreReader.cs` 第 605 行）。
- 实例：retoc #18 的 `29f5850b27aea78f 0000 00 0a`。前 8 字节按小端读出即 ContainerId，末字节 `0x0a` = 10。[T3 原文；拆解为 T4]

**两套 `EIoChunkType` 枚举** [T3]

| 值 | UE4（4.25–4.27，TOC ≤ v4） | UE5（TOC ≥ v5） | 扩展名惯例（CUE4Parse） |
|---|---|---|---|
| 0 | Invalid | Invalid | |
| 1 | InstallManifest | ExportBundleData | uasset |
| 2 | **ExportBundleData** | BulkData | uasset（UE4） / ubulk（UE5） |
| 3 | **BulkData** | OptionalBulkData | ubulk / uptnl |
| 4 | OptionalBulkData | MemoryMappedBulkData | uptnl / m.ubulk |
| 5 | MemoryMappedBulkData | ScriptObjects | m.ubulk |
| 6 | LoaderGlobalMeta | ContainerHeader | |
| 7 | LoaderInitialLoadMeta | ExternalFile | |
| 8 | LoaderGlobalNames | ShaderCodeLibrary | |
| 9 | LoaderGlobalNameHashes | ShaderCode | |
| 10 | **ContainerHeader** | PackageStoreEntry | |
| 11 | ShaderCodeLibrary（**仅 retoc**；CUE4Parse 的 UE4 枚举到 10 为止） | DerivedData | |
| 12 | ShaderCode（仅 retoc） | EditorDerivedData | |
| 13 | — | PackageResource | |

- 来源：CUE4Parse `FIoChunkId.cs`（`EIoChunkType`、`EIoChunkType5`）、retoc `lib.rs: EIoChunkType::new(value, is_new)`。

**4.26 包的 chunk 映射** [T3]
- 一个包的主体是 `(PackageId, 0, ExportBundleData)`。
- 外置 bulk（`.ubulk`）是 `(PackageId, 0, BulkData)`。
- 可选 bulk 和内存映射 bulk 分别用对应类型。
- UE5.4+ 起 bulk 有 `CookedIndex`（`FBulkDataCookedIndex`），4.26 没有。

### 2.6 `FIoOffsetAndLength`（10 字节，**大端** 5+5）[T3]

```
Offset = b[0]<<32 | b[1]<<24 | b[2]<<16 | b[3]<<8 | b[4]     // 40 位
Length = b[5]<<32 | b[6]<<24 | b[7]<<16 | b[8]<<8 | b[9]     // 40 位
```

- 来源：CUE4Parse `FIoOffsetAndLength.cs`。
- `Offset` 是该 chunk 在容器的「**解压后连续地址空间**」中的位置，不是 `.ucas` 里的物理偏移。
- 读取时：首块序号 = `Offset / CompressionBlockSize`，块内起点 = `Offset % CompressionBlockSize`。
  - UEViewer 注释："CompressionBlocks are not in sync with any chunk data, it is sequential compression of the whole .ucas file"。
- 写入时：retoc、UnrealReZen 和本工程 `iostore.py` 都让每个 chunk 从块边界开始。UE 原生写入器是否一定如此**未证实**；`iostore.py selfcheck` 会报告实际布局。

### 2.7 `FIoStoreTocCompressedBlockEntry`（12 字节，小端 5/3/3/1）[T3]

| 字节 | 位宽 | 字段 |
|---|---|---|
| 0–4 | 40 | `Offset`：**物理**偏移。跨分区时，分区号 = Offset / PartitionSize，分区内偏移 = Offset % PartitionSize |
| 5–7 | 24 | `CompressedSize`：磁盘上的字节数，**不含** AES 补齐 |
| 8–10 | 24 | `UncompressedSize`：≤ CompressionBlockSize，上限 16 MiB−1 |
| 11 | 8 | `CompressionMethodIndex`：0 = 不压缩；i>0 指向方法名表第 i−1 项 |

- 来源：CUE4Parse `FIoStoreTocCompressedBlockEntry.cs`（`SIZE = 5+3+3+1`，读作 uint64+uint32 后用掩码拆开）、retoc 同名结构。
- 读取时，每块从磁盘读 `Align(CompressedSize, 16)` 字节，解密后取前 `CompressedSize` 字节再解压（CUE4Parse `IoStoreReader.cs` 第 270 行）。
- **同一容器内各块可以用不同方法**：方法索引按块记录。retoc 写入时用索引 0，本工程 `iostore.py patch` 的替换块也写索引 0。[T3]
- **[T4]** eFootball 运行时是否接受「同一容器里压缩块与不压缩块混用」：未证实。按格式定义应当接受。

### 2.8 压缩方法名

- 方法名为 32 字节的 ASCII 槽位，如 `Zlib`、`Oodle`、`LZ4`、`Gzip`，以及游戏自定义的 `Zstd`。
- CUE4Parse 用 `Enum.TryParse` 映射名称，未知名称记为 `Unknown`；retoc 只认 `Zlib`、`Zstd`、`LZ4`、`Oodle` [T3]。
- Oodle 有多种算法：Kraken、Mermaid、Selkie、Leviathan [S35][T1]。它们**共用方法名 "Oodle"**，解码器从数据流本身识别具体算法。Epic 文档原文："The decoder at runtime never needs to know what method was used" [S35]。
- **eFootball 用哪种方法：未找到。**
  - retoc #18 报告者说 to-zen 输出"不使用任何压缩方法"，暗示原容器是压缩的 [T4]。
  - 是否为 Oodle，用 `iostore.py info` 读方法名表即可确认。

### 2.9 加密 [T3]

- **算法**：AES-256，**ECB** 模式，32 字节密钥（v<10，或 v10 的 `EncryptionMethod=AES`）。
- **v10 / UE6**：可选 AES-CTR，每块一个 IV，存放在 `EncryptionIVs` 段（CUE4Parse `FIoStoreEncryptionIV.cs`）。与 4.26 无关。

**加密范围（`Encrypted` 标志置位时）**

| 加密 | 不加密 |
|---|---|
| ① 每个压缩块在 `.ucas` 中的磁盘字节，按 16 字节补齐后加密 | TOC 头 |
| ② `.utoc` 中的目录索引整段（CUE4Parse `DecryptIfEncrypted(indexBuffer)`；retoc `read_directory_index`） | chunk id 表、偏移/长度表、压缩块表、方法名表、meta |

- 所以**没有密钥也能看到 chunk 数量、大小、类型和压缩方法**，但看不到路径和内容。
- 密钥选择：按头部 `EncryptionKeyGuid` 选取。CUE4Parse 和 ZenTools 用 GUID→密钥映射，全零 GUID 是主密钥 [S46]。
- 密钥正确性：有目录索引时，可用解密后的挂载点字符串（通常为 `../../../`）验证；没有目录索引时，要靠解码 chunk 或比对 meta 哈希来验证（本工程 `iostore.py` 的做法）。
- eFootball：PC 与移动容器**是加密的**——Gildor 两帖都说需要 AES 密钥，并给了外部链接 [S07][S08][T3]。国服 `nt*` 包用另一把密钥 [S08]。

### 2.10 签名

**段布局**（`Signed` 标志置位时）[T3]

```
int32  HashSize
uint8  TocSignature[HashSize]
uint8  BlockSignature[HashSize]
FSHAHash ChunkBlockSignatures[B]     // 每块 20 字节 SHA-1
```

- 来源：CUE4Parse 读完后直接跳过（注释 "You could verify hashes here but nah"）；retoc `read_chunk_block_signatures`。
- 签名用 RSA，参数来自工程的 CryptoKeys 设置。Epic `UCryptoKeysSettings` 文档 [S34][T1] 列出了 `bEnablePakSigning`（签名以检测篡改）、`SigningModulus`、`SigningPublicExponent`、`SigningPrivateExponent`。
- 每块 SHA-1 具体对哪些字节计算（压缩后、加密后、还是含补齐），**未证实**（无 UE 源码）。
- **对修改的影响**：没有发行方私钥就无法重算 TocSignature 和 BlockSignature。
  - UnrealReZen 的 README 原文："For games that have archive signature (.sig file for each utoc) this tool doesn't work until you bypass the sig loader" [S48]。
  - 本工程 `iostore.py` 默认拒绝修补已签名容器；`--unsign` 会去掉 Signed 标志和签名段，但运行时如果强制校验就会拒绝该容器。
- **eFootball**：社区 PC 模组只有 `.pak/.ucas/.utoc` 三件套，没有出现 `.sig` [S16][S17]。2023 年教程用未签名的原版打包产物替换后能生效 [S15]。[T4] 推测 PC 版至少在 2023 年没有强制签名。**移动版未知。**

### 2.11 目录索引 `FIoDirectoryIndexResource` [T3]

```
FString                      MountPoint        // 通常为 "../../../"
TArray<FIoDirectoryIndexEntry> DirectoryEntries  // 每项 16 字节：Name, FirstChildEntry, NextSiblingEntry, FirstFileEntry（uint32，无效值 0xFFFFFFFF）
TArray<FIoFileIndexEntry>      FileEntries       // 每项 12 字节：Name, NextFileEntry, UserData（= TOC 条目序号）
TArray<FString>                StringTable       // 名字字符串池，Name 是池内下标
```

- 来源：CUE4Parse `FIoDirectoryIndexEntry.cs`、`FIoFileIndexEntry.cs`；retoc `FIoDirectoryIndexResource`；UEViewer。
- 路径形如 `PesMobile/Content/.../X.uasset`。
- **UE4 中，目录索引是「路径 → chunk」的唯一来源**：包本身只按 PackageId 寻址，与路径无关。所以加入新包时，必须同时在目录索引里加文件项（见 §4.5）。

### 2.12 Chunk meta

- **v<8**：`FIoChunkHash` 32 字节加 flags 1 字节。
  - retoc 写入 BLAKE3 截断到 20 字节，后面补 12 个零（`FIoChunkHash::from_blake3`）。
  - **UE4.26 原生写入器用 SHA-1 还是 BLAKE3，未找到一手源码**。本工程 `iostore.py --hash-algo auto` 会对未改动的 chunk 实测判定。
- **v≥8**：`FIoHash` 20 字节加 flags 加 3 字节填充。
- 运行时（4.26，未签名容器）是否校验 meta 哈希：**未证实**。CUE4Parse 不校验 [T3]。

### 2.13 分区（v≥3）

- `.ucas` 拆成 `X.ucas`、`X_s1.ucas`……，每个分区不超过 `PartitionSize`；块的物理偏移是跨分区的全局值（§2.7）。
- 4.26（v2）只有单个 `.ucas`。

### 2.14 完美哈希（v4/v5，UE5）[T3]

来源：CUE4Parse `IoStoreReader.TryResolve`、`FIoChunkId.HashWithSeed`。

- 哈希函数为 FNV 变体（先乘后异或），对 12 字节 chunk id 逐字节计算：`h = seed ? seed : 0xcbf29ce484222325; h = h*0x100000001B3 ^ byte`。
- 查找步骤：
  1. `seedIndex = H(id, 0) % S`，`seed = Seeds[seedIndex]`。
  2. `seed == 0`：不存在。
  3. `seed < 0`：直接下标 `-seed-1`；若该值 ≥ N，则改为线性查溢出表。
  4. `seed > 0`：`slot = H(id, seed) % N`。
  5. 最后比对 `ChunkIds[slot]` 是否就是目标 id。
- **对写入的影响**：chunk id 表的顺序被哈希固定，**不能随意增删或重排 chunk**。本工程原地补丁不改 chunk id 表，所以不受影响。
- 4.26（v2）没有完美哈希，按线性或字典查找，增加 chunk 不受此限制。

### 2.15 容器头（ContainerHeader chunk）与包存储（package store）

**UE4.26/4.27（无版本号，"PreInitial/BeforeVersionWasAdded"）** [T3]

来源：CUE4Parse `FIoContainerHeader.cs`、retoc `container_header.rs`。

```
uint64   ContainerId
uint32   PackageCount
TArray<uint8> Names        // 容器级 name batch（UE4 中通常为空）
TArray<uint8> NameHashes
TArray<FPackageId> PackageIds
TArray<uint8>  StoreEntries // N 个 FPackageStoreEntry 的数据块，CArrayView 用「相对本字段的偏移」指向块内数据
TMap   CulturePackageMap
TArray PackageRedirects
```

**每个包的 `FPackageStoreEntry`**

| 布局 | 字段 | 总长 |
|---|---|---|
| 原版 4.26/4.27（retoc 称 `Initial`） | `ExportBundlesSize` u64、`ExportCount` i32、`ExportBundleCount` i32、`LoadOrder` u32、`Pad` u32、`ImportedPackages` CArrayView（i32 数量 + i32 相对偏移） | **32 字节** |
| **eFootball**（CUE4Parse 特例）、retoc `PreInitial`、SD 高达 4.27.2、PesMobile | `ExportCount` i32、`ExportBundleCount` i32、`ImportedPackages` CArrayView | **16 字节** |

- retoc 代码 `(member_offset, entry_size)`：`PreInitial => (8, 16)`，`Initial => (24, 32)`。
- **[T4]** CUE4Parse 的 eFootball 特例与 retoc 的 PreInitial 布局逐字段一致。retoc 加 `--override-container-header-version PreInitial` 应能读写 eFootball 容器头。#18 已证明能读；写入未证实。

**UE5**
- 以 `0x496F436E` 魔数开头，后跟版本号：
  - 0 Initial；1 LocalizedPackages；2 OptionalSegmentPackages；3 NoExportInfo（包条目只剩 ImportedPackages 和 ShaderMapHashes）；4 SoftPackageReferences；5 SoftPackageReferencesOffset。
- 另有可选段包、本地化包、重定向。
- retoc 映射：5.0→1，5.1/5.2→2，5.3/5.4→3，5.5→4，5.6/5.7→5。

### 2.16 Zen 包（ExportBundleData chunk 的内容）

**UE4.26/4.27** [T3]

来源：CUE4Parse `FPackageSummary.cs`、`FExportMapEntry.cs`、`FExportBundleHeader.cs`、`FExportBundleEntry.cs`；本工程 `zen_package.py` 的文档字符串。

| 结构 | 大小 | 字段 |
|---|---|---|
| `FPackageSummary` | 64 | `Name` FMappedName(8)、`SourceName`(8)、`PackageFlags` u32、`CookedHeaderSize` u32、`NameMapNamesOffset`、`NameMapNamesSize`、`NameMapHashesOffset`、`NameMapHashesSize`、`ImportMapOffset`、`ExportMapOffset`、`ExportBundlesOffset`、`GraphDataOffset`、`GraphDataSize`（各 i32）、`Pad` |
| 名字表 | 变长 | 每个名字带 `FSerializedNameHeader`，可以是 ANSI 或 UTF-16；哈希为小写名字的 CityHash64 |
| `ImportMap` | 8×n | `FPackageObjectIndex`：最高 2 位是类型（Export、ScriptImport、PackageImport、Null），低 62 位是值。脚本导入的值为「小写对象路径（`.` 和 `:` 换成 `/`）的 CityHash64」（`FPackageObjectIndex.cs`；retoc `script_objects.rs`） |
| `FExportMapEntry` | 72 | `CookedSerialOffset` u64、`CookedSerialSize` u64、`ObjectName`(8)、`OuterIndex`、`ClassIndex`、`SuperIndex`、`TemplateIndex`、`GlobalImportIndex`（各 8 字节）、`ObjectFlags` u32、`FilterFlags` u8，补齐到 72 |
| `FExportBundleHeader` | 8 | `FirstEntryIndex` u32、`EntryCount` u32（UE5 前面多一个 `SerialOffset` u64，共 16 字节） |
| `FExportBundleEntry` | 8 | `LocalExportIndex` u32、`CommandType` u32（0 = Create，1 = Serialize） |

- 4.26 到 5.2：export 数据按 bundle 顺序紧接在 `GraphDataOffset + GraphDataSize` 之后（`zen_package.py` 的说明）。
- `PackageFlags & 0x2000` = `PKG_UnversionedProperties`：属性区为**无版本序列化**，要解析属性名需要 `.usmap` 映射（CUE4Parse `AbstractUePackage.cs` 第 32 行）。
  - 社区映射档案 Unreal-Mappings-Archive 中**没有 eFootball 条目**（2026-10-25 检索，只有 "Maximum Football"）[S53]。
  - eFootball 是否使用无版本属性：**未找到**。
  - 本工程原地补丁只改属性区**之后**的原生序列化数据（渲染数据、平台数据），不需要解析属性。

**UE5**
- `FZenPackageSummary`：`bHasVersioningInfo`、`HeaderSize`、`Name`、`PackageFlags`、`CookedHeaderSize`、`ImportedPublicExportHashesOffset`、`ImportMapOffset`、`ExportMapOffset`、`ExportBundleEntriesOffset`、`GraphData` 或 `DependencyBundle*`、`ImportedPackageNamesOffset`，可选 `FZenPackageVersioningInfo`。
- bulk 的位置改由 `FBulkDataMapEntry` 表描述（32 字节：`SerialOffset`、`DuplicateSerialOffset`、`SerialSize` u64、`Flags` u32、`CookedIndex` u8 加填充）。

---

## 3. 与面部相关的 cooked 资产内部结构

> 以 CUE4Parse 对 `GAME_eFootball`（4.26 变体）的读取路径为准 [T3]。版本开关见 `VersionContainer.cs`：
> - `SkeletalMesh.UseNewCookedFormat = Game >= 4.24`：真；
> - `SkeletalMesh.HasRayTracingData = Game >= 4.27 or 4.25_Plus/4.26_Plus or ...`：对 eFootball 为**假**；
> - `StaticMesh.UseNewCookedFormat = Game >= 4.23`：真。
>
> Konami 是否改动了这些结构：除 §1.2 列出的两处外，**未找到证据**。CUE4Parse 能读出 eFootball 网格，可以间接说明没有其他大改，但这是 T4。

### 3.1 `USkeletalMesh`（cooked）序列化顺序

来源：CUE4Parse `USkeletalMesh.cs`、`FStaticLODModel.cs`。

1. 标记属性（tagged 或 unversioned）：`LODInfo`、`bHasVertexColors`、`NumVertexColorChannels`、`MorphTargets`、`Skeleton`、`PhysicsAsset`……
2. `FStripDataFlags`。
3. **`ImportedBounds`**：`FBoxSphereBounds`，见 §3.4。
4. `SkeletalMaterials[]`：`FSkeletalMaterial`，含 MaterialInterface、槽位名、UV 通道信息。
5. **`FReferenceSkeleton`**：骨骼信息（名字、父骨骼下标）和参考姿势（FTransform，4.26 为 float 四元数、平移、缩放，共 40 字节），以及名字到下标的映射。
6. 编辑器数据（cooked 中被剥离）。
7. `bCooked`（bool32）。
8. `LODRenderData` 数组：每级 LOD 调用 `SerializeRenderItem`：
   - `FStripDataFlags`、`bIsLODCookedOut`、`bInlined`、`RequiredBones[]`（int16）
   - `Sections[]`（`FSkelMeshSection.SerializeRenderItem`）：`MaterialIndex` i16、`BaseIndex`、`NumTriangles`、`bRecomputeTangent`、`bCastShadow`、`BaseVertexIndex`、`ClothMappingData`、`BoneMap[]`（u16，本段使用的骨骼子集）、`NumVertices`、`MaxBoneInfluences`、cloth 相关字段……
   - `ActiveBoneIndices[]`
   - `uint32 BuffersSize`
   - `bInlined` 为真：紧接 **StreamedData**；为假：`FByteBulkData` 存在 bulk 中，可能在 `.ubulk`（BulkData chunk）里。

**StreamedData 的顺序**（`SerializeStreamedData`）

| # | 缓冲区 | 序列化 |
|---|---|---|
| 1 | `FStripDataFlags` | 2 字节 |
| 2 | `FMultisizeIndexContainer` | `uint8 DataTypeSize`（2 或 4），然后是 bulk 数组 |
| 3 | `FPositionVertexBuffer` | `int32 Stride`（=12）、`int32 NumVertices`，然后是 bulk 数组 `FVector`（float32×3） |
| 4 | `FStaticMeshVertexBuffer` | `FStripDataFlags`、`NumTexCoords`、`NumVertices`、`bUseFullPrecisionUVs`、`bUseHighPrecisionTangentBasis`（各 bool32），然后是**切线**和**UV**两个 bulk 数组，见 §3.2 |
| 5 | `FSkinWeightVertexBuffer` | 见 §3.2 |
| 6 | `FColorVertexBuffer` | 仅当 `bHasVertexColors` |
| 7 | `AdjacencyIndexBuffer` | 仅当未剥离 `CDSF_AdjacencyData` |
| 8 | `FSkeletalMeshVertexClothBuffer` | 仅有 cloth 时 |
| 9 | `FSkinWeightProfilesData` | |
| 10 | 光追数据 | **eFootball 不存在** |
| 11 | MorphTarget 渲染数据 | 4.26 不在这里 |

**bulk 数组格式** [T3]：`int32 ElementSize`、`int32 ElementCount`，然后是 ElementSize×ElementCount 字节（CUE4Parse `FArchive.ReadBulkArray`）。

**对改形的意义**
- 每级 LOD 有独立的位置、切线、UV、权重流，**LOD0 和 LOD1 都要改**，否则远景会显示 donor 原来的脸（`README_zen_inplace.md` 第 5 节）。
- 改位置不改顶点数时，以上各缓冲区的长度都不变，可以做**等长原地补丁**。
- 亨利项目的 donor 数据（166 骨骼，LOD0 有 5,376 个顶点，LOD1 有 1,338 个）来自任务书（内部资料），未经本组核验。

### 3.2 顶点缓冲区字节格式 [T3]

| 缓冲区 | 每顶点格式 | 说明 |
|---|---|---|
| 位置 | `float32 x, y, z`（12 字节） | UE 单位为厘米。部分手游用 16 位压缩位置（CUE4Parse 中有多款游戏特例），**eFootball 没有特例**，按 float32 处理（T4，需用 `mesh-locate` 确认 stride 为 12） |
| 切线（普通精度） | `FPackedNormal TangentX` + `FPackedNormal TangentZ`，各 4 字节 | 4.20 之后存为 int8×4（CUE4Parse 读入后 `Data ^= 0x80808080` 转成无符号）。TangentZ 的 W 分量是副法线的符号 |
| 切线（高精度） | `FPackedRGBA16N` ×2，各 8 字节 | 由 `bUseHighPrecisionTangentBasis` 决定 |
| UV | `FVector2DHalf`（fp16×2）或 `FVector2D`（float×2），共 `NumTexCoords` 套 | 由 `bUseFullPrecisionUVs` 决定 |
| 蒙皮权重（4.26 新格式） | 元数据：`bVariableBonesPerVertex`、`MaxBoneInfluences`、`NumBones`、`NumVertices`、`bUse16BitBoneIndex`。数据：bulk 字节数组，每顶点 `BoneIndex[k]`（u8 或 u16）加 `BoneWeight[k]`（u8；UE5 可为 u16）。变长时另有 `LookupData[]`（u32：高 24 位为偏移，低 8 位为影响数） | 骨骼下标指向 section 的 `BoneMap`，**不是**全局骨骼下标。来源 `FSkinWeightVertexBuffer.cs`、`FSkinWeightInfo.cs` |
| 索引 | u16 或 u32 | 由 `DataTypeSize` 决定 |
| 顶点色 | `FColor` BGRA8 | |

**`FPackedNormal` 的编码**
- 来源：CUE4Parse `FPackedNormal.cs`。
- `Pack(v) = clamp(round((v+1)×127.5), 0, 255)`；读取时 `x = b/127.5 − 1`。
- 改顶点后要**重算 TangentZ（法线）和 TangentX**，并**保留 W（副法线符号）**。本工程 `mesh-patch-positions --tangents` 用增量法实现了这一点。

### 3.3 `UStaticMesh`（cooked；头发若为静态网格时适用）

来源：CUE4Parse `UStaticMesh.cs`、`FStaticMeshRenderData.cs`、`FStaticMeshLODResources.cs`。[T3]

- `FStaticMeshRenderData`：`LODResources[]`。
- 每个 `FStaticMeshLODResources`：
  - `FStripDataFlags`、`Sections[]`、`MaxDeviation`、`bIsLODCookedOut`、`bInlined`；
  - 内联时 `SerializeBuffers`：`FPositionVertexBuffer`、`FStaticMeshVertexBuffer`、`FColorVertexBuffer`、`IndexBuffer`，以及按剥离标志决定的 Reversed、DepthOnly、Wireframe、Adjacency 索引缓冲；
  - 非内联时为 bulk，再加若干计数。
- 之后是 **`Bounds`**（`FBoxSphereBounds`）和每级 LOD 的 `ScreenSize`（4.26 为 `FPerPlatformFloat`）。
- 发型资产是骨骼网格还是静态网格、是否与头部同一个资产：**未知，需用真实文件确认**。

### 3.4 包围盒 `FBoxSphereBounds`

- 布局：`Origin` FVector、`BoxExtent` FVector、`SphereRadius`。
- UE4 为 float×7 = **28 字节**；UE5 起 FVector 为 double，共 56 字节（CUE4Parse `ReadFReal`）。[T3]
- 骨骼网格的 `ImportedBounds` 在 §3.1 第 3 步。
- 改形后只能**放大**包围盒（union），不能缩小，否则可能被视锥剔除错误地裁掉。

### 3.5 `UTexture2D`（cooked）与 ASTC 平台数据

**序列化顺序**（来源：CUE4Parse `UTexture.cs`、`UTexture2D.cs`、`FTexturePlatformData.cs`、`FTexture2DMipMap.cs`、`FByteBulkDataHeader.cs`、`EBulkDataFlags.cs`）[T3]

1. 标记属性：**`SRGB`**（bool，默认 true）、**`CompressionSettings`**（`TC_Default`、`TC_Normalmap`、`TC_Masks`……）、`LODGroup`、`Filter`、`AddressX`、`AddressY`……
2. `FStripDataFlags`（UTexture），`FStripDataFlags`（UTexture2D），`bCooked`（bool32）。
3. `SerializeCookedPlatformData` 循环：
   - `FName PixelFormatName`，例如 `PF_ASTC_6x6`。为 `None` 时循环结束。
   - `int64 SkipOffset`：4.20–4.27 为**绝对**位置，UE5 改为相对位置。
   - `FTexturePlatformData`：
     - `int32 SizeX`、`int32 SizeY`。
     - `uint32 PackedData`：低 30 位为 NumSlices；bit31 = 立方体贴图；bit30 = 有 OptData；bit29 = 有 CPU 副本（UE5.4+）。
     - **`FString PixelFormat`**：字符串，如 `"PF_ASTC_6x6"`。**cooked 数据里存的是名字，不是枚举数值。**
     - （OptData：`ExtData` u32、`NumMipsInTail` u32）
     - `int32 FirstMipToSerialize`。
     - `int32 MipCount`，后接 `FTexture2DMipMap[MipCount]`：
       - `bCooked`（bool32，4.x）
       - `FByteBulkData`：头部 `uint32 BulkDataFlags`；`ElementCount` 和 `SizeOnDisk`（int32，有 `BULKDATA_Size64Bit` 时为 int64）；`int64 OffsetInFile`；内联时后接数据。
       - `int32 SizeX`、`SizeY`、`SizeZ`。
     - `bIsVirtual`（bool32）。
4. 回到循环开头读下一个 `PixelFormatName`。

**mip 的存放位置**（由 `EBulkDataFlags` 决定）

| 标志 | 含义 | IoStore 中的位置 |
|---|---|---|
| `BULKDATA_ForceInlinePayload` 0x40 | 内联 | 本 export 的序列化数据中（ExportBundleData chunk） |
| `BULKDATA_PayloadInSeperateFile` 0x100（常与 `PayloadAtEndOfFile` 0x1 同时出现） | 外置 `.ubulk` | **BulkData chunk** `(PackageId, 0, BulkData)` |
| `BULKDATA_OptionalPayload` 0x800 | `.uptnl` | OptionalBulkData chunk |
| `BULKDATA_MemoryMappedPayload` 0x1000 | `.m.ubulk` | MemoryMappedBulkData chunk |
| `BULKDATA_NoOffsetFixUp` 0x10000 | 4.26：偏移不再加 `BulkDataStartOffset` | — |

- 常见布局 [T4]：高分辨率 mip 外置（可流式加载），尾部小 mip 内联。实际以 `inplace_patch.py texture-scan` 的输出为准。
- 亨利项目的面部贴图是 256 px 原生 ASTC（任务书内部资料）。256 px 的贴图可能整体内联，也可能部分外置：**未知**。

**ASTC 像素格式**（UE 枚举值，CUE4Parse `PixelFormat.cs` 带原注释）

| 名称 | 值 | bpp | 备注 |
|---|---|---|---|
| `PF_ASTC_4x4` | 50 | 8.00 | |
| `PF_ASTC_6x6` | 51 | 3.56 | |
| `PF_ASTC_8x8` | 52 | 2.00 | |
| `PF_ASTC_10x10` | 53 | 1.28 | |
| `PF_ASTC_12x12` | 54 | 0.89 | |
| `PF_ASTC_{4x4..12x12}_HDR` | 73–77 | — | HDR profile |
| `PF_ASTC_{4x4..12x12}_NORM_RG` | 87–91 | — | **UE5**：原注释 "RG format stored in LA endpoints for better precision (requires RHI support for texture swizzle)" |

- `8x5`、`8x6`、`10x8` 被 CUE4Parse 标为 253–255，是该库的扩展值，并非 UE 官方编号 [T3]。

**块与 mip 尺寸**
- 每个 ASTC 块固定 128 位，即 16 字节 [S31][T1]。
- 单级 mip 的字节数 = `ceil(W/bx) × ceil(H/by) × 16`，块按行主序排列。
- 示例（`astc_tool.py info` 计算，算术结果）：

| 256×256 的块大小 | 块数 | 字节数 |
|---|---|---|
| 4×4 | 64×64 | 65,536 |
| 6×6 | 43×43 | 29,584 |
| 8×8 | 32×32 | 16,384 |

- 完整 mip 链从最大级开始，每级尺寸减半，最小到 1×1（每级至少一个块）。实际级数以 cooked 的 `MipCount` 为准，cook 时可能被 `LODBias` 或最大尺寸裁掉。

### 3.6 其他可能与脸相关的资产（只列出，未核验）

材质实例（`MaterialInstanceConstant`：参数贴图槽、肤色参数）、`Skeleton`、`PhysicsAsset`、`MorphTarget`、动画资产。
- 面部表情是用骨骼（亨利 donor 有 166 根骨骼，暗示表情靠骨骼）还是 MorphTarget：**未知**。
- 如果有 MorphTarget，只改基础顶点而不改 morph 的增量数据，表情时会出现形变错误（见风险 R12）。

---

## 4. 开源工具：能力、版本、许可、能否写入

### 4.1 总表

| 工具 | 语言 | 许可证 | 读 IoStore | **写 IoStore** | 支持的 UE 版本 | 与 eFootball 的关系 | 来源 |
|---|---|---|---|---|---|---|---|
| **CUE4Parse** | C# | Apache-2.0 | 是（含 AES、Oodle、完美哈希、分区、v10 CTR） | **否**（只是解析库） | UE4.0 至 UE5.x/6；**有 `GAME_eFootball`** | 最直接的参考实现；本工程 `cue4parse_check` 用它做独立回读 | [S09][S51] |
| **FModel** | C#（基于 CUE4Parse） | GPL-3.0 | 是；预览，导出 PSK/PSKX、glTF 2.0、UEFormat、USD，贴图导出为 PNG | **否** | 同 CUE4Parse | PC 用户可用来导出参考网格（GLB/PSK）。Steam 版需要把全局容器改名为 `global.*` [S07] | [S43]；导出格式见 `CUE4Parse-Conversion/Options/EMeshFormat.cs` |
| **UEViewer（umodel）** | C++ | MIT | 是（TOC 只到 v3 PartitionSize，即 4.25–4.27） | **否** | UE1 到 4.27（UE5 IoStore 不支持） | Gildor 提供带 eFootball 游戏覆盖选项的专用构建 [S07] | [S13][S45] |
| **retoc** | Rust | MIT | 是（manifest、info、list、verify、unpack、unpack-raw、get） | **是**：`pack-raw`、`to-zen`；**不压缩**（每块方法 0，块大小 0x10000），**不加密**（源码注释 "TODO encrypt directory index"），**不签名**，不写完美哈希 | UE4.25 至 5.7；作者称 5.3 及以上支持良好，更早版本「可能有少量资产加载失败」 | 需要 `--override-container-header-version PreInitial`，否则 eFootball 容器头解析失败 [S11]；写出产物能否被 eFootball 运行时接受：**未证实** | [S10] |
| **ZenTools**（Archengius） | C++（UE 程序） | MIT | 是 | 否（只提取） | 5.1、5.2 | **已被 retoc 取代**（README 原文 "superseded by retoc"） | [S46] |
| **ZenTools-UE4**（WistfulHopes 分支） | C++ | MIT | 是 | 否 | "all versions of UE4 that have IO Store" | 提取后的 legacy 包要用 UnrealPak 或编辑器重打 | [S47] |
| **UnrealReZen** | C#（基于 CUE4Parse） | GPL-3.0 | 借用 CUE4Parse | **是**：生成补丁用 `X_P.utoc/.ucas/.pak`；压缩可选 None/Zlib/Oodle/LZ4；`--encrypt-output` 可用游戏 AES 加密 `.ucas` | 参数接受 CUE4Parse 的 `EGame` | **固定写 TOC v3（`PackUtocVersion = 3`）**；UE4 容器头按 32 字节包条目写（`DepLinks_UE4.SizeOf() = 8×2+4×4`）。[T4] 与 4.26（v2）和 eFootball（16 字节条目）都不一致，**不能直接用**。README 也说对签名容器无效 | [S48]，源码 `Constants.cs`、`FIoDependency.cs` |
| **UAssetAPI / UAssetGUI** | C# | MIT | 否（只处理 legacy `.uasset/.uexp`） | 写 legacy 资产 | 约 4.13 至 5.7 | Zen 包需先用 retoc `to-legacy` 转换。网格渲染数据只当原始字节处理，不解析顶点 | [S44] |
| **repak** | Rust | MIT/Apache | `.pak`，不含 IoStore | 写 `.pak` | — | 其 `oodle_loader` 子库会**下载** Oodle 专有库（§4.3） | [S40] |
| **本工程 `tools/native/*`** | Python | 本工程 | 是（v1–v10、AES-ECB/CTR、None/Zlib/LZ4、外部 Oodle） | **是：原地追加或重建**。保留原版本号、chunk id、目录索引和完美哈希；替换块不压缩；按需加密；拒绝签名容器（除非 `--unsign`）；写 manifest 并支持 `--verify` | UE4.25 至 UE6 的 TOC | 只在合成数据上验证过（含 CUE4Parse 独立回读）。**真实 eFootball 文件：缺少原始输入** | `tools/native/README_iostore.md` 等 |

### 4.2 结论 [T4]

- **读**：CUE4Parse/FModel（`GAME_eFootball`）是首选，retoc（PreInitial）和 UEViewer（Gildor 的专用构建）做交叉验证。
- **写**：在 eFootball 上没有被公开验证过的现成写入器。最稳妥的是「**只换 chunk 内容，其余字节保持不变**」的原地补丁。这样容器头（16 字节条目）、chunk id 表、目录索引都原样保留，不必依赖第三方写入器对 Konami 定制布局的理解。

### 4.3 Oodle 在 Linux 上的可用性与许可

| 方案 | 获取方式 | 许可 | 能力 | 来源 |
|---|---|---|---|---|
| 官方 oo2core 动态库 | Epic UE 源码的 SDK，或 OodleUE 镜像（`WorkingRobot/OodleUE`，按日更新，README 写明最新为 2.9.16） | **专有**；镜像 README 原文："By using these assets, you agree to the Unreal Engine EULA" | 压缩与解压 | [S39] |
| repak `oodle_loader` | 运行时下载 `liboo2corelinux64.so.9`（Oodle 2.9.10），从 OodleUE 仓库的 `Engine/Source/Programs/Shared/EpicGames.Oodle/Sdk/` 路径获取并校验 sha256 | 同上 | 压缩与解压 | [S40] |
| CUE4Parse `OodleHelper` | Linux 上下载 `liboodle-data-shared.so`（OodleUE release，2.9.16） | 同上 | 解压 | `OodleHelper.cs` |
| Epic 官方说法 | 「Oodle now free to use in Unreal Engine via GitHub」；UE 4.27 发布时 "Oodle Compression Suite ... now built into Unreal Engine ... for free" | 免费的前提是在 UE 中使用（UE EULA） | — | [S36][S37]（**只看到搜索摘要**，页面返回 403）【未核实】核查时两个 URL（含 `/en-US/` 变体）仍返回 403，原文无法复读 |
| RAD Game Tools（现属 Epic） | 商业授权，"We generally license per game and per platform" | 商业 | — | [S38] |
| **ooz**（powzix） | 源码 | **GPL-3.0**（`kraken.cpp` 文件头） | **解码** Kraken、Mermaid、Selkie、Leviathan、LZNA、Bitknit；压缩需要 Windows DLL | [S41]；本地 `/opt/tools/ref/ooz` |
| **oozextract** | crates.io 0.5.6（2026-10-23） | crates.io 标为 **MIT** | 纯 Rust 解码 | [S42]。[T4] 它自称移植 ooz 的算法，而 ooz 是 GPL；许可沿袭关系**未核实**，商用前要查 |

**对本工程的影响**
- 解码可以用开源实现（GPL 或 MIT），或用户自己按 UE EULA 取得的 Oodle 库。
- **写入时不需要 Oodle**：替换块用方法 0（不压缩）写入，见 §2.7。代价是容器变大，以及「运行时是否接受混用」未证实（R7）。
- 本工程环境中，编译或运行第三方 ooz 代码一度被禁止（`README_iostore.md` 第 1 节），所以 Oodle 路径目前是「已实现、待复验」。

---

## 5. eFootball 社区已知信息

### 5.1 公开的模组和工具（eFootball 2022+）[T3]

| 类别 | 已知内容 | 来源 |
|---|---|---|
| UE 资源替换（PC） | **球衣贴图**：用 UE 4.26.2 新建空工程 "PesConsole"，开启 "Allow ChunkID Assignments"、"Use Pak File"、"Use IoStore"、"Generate Chunks"；把资产放在 `Content/Assets/character/Uniform/License/...` 并分配 chunk 1；用 Shipping 打包后改名为 `pc3607_console_win`。<br>球衣参数（长短袖、领型）在 `dt200_console_all.cpk` 内的 `.bin` 中用十六进制改。 | [S15]（2023-07-04，Cadi Lillian 教程） |
| 综合补丁（PC） | EvoMod 5.4/5.5：改球衣、字体、号码、裁判、广告板、联赛 logo；涉及 `pc7000_console_win(_P)`、`evomod_FNT_P.pak`、`PesConsole/Content/Paks/~mods`。**没有提到脸**（核查更正：该 URL 目前显示的是 "eFootball 2026 Patch: EvoMod 5.5.0"，2026-05-28 由 G. Leroy 发布，不是 5.4.0。内容逐条确认，包括「没有提到脸」） | [S16]（2026） |
| UI 模组（PC） | 记分牌：把 `pc3610_console_win` 从 `eFootball/pak` 复制为 `PesConsole/Content/Paks/pc3610_console_win_P` | [S17]（2025-09）（核查更正：页面标题是 "eFootball 2026 Transparent Scoreboard for Blimp Camera Angle [ver. 2.0]"，日期 2025-09-01。安装步骤原文确认） |
| 数据编辑器 | `Player.bin`、`Team.bin`、`Country.bin`、`PlayerAppearance.bin`，路径为 `dt870_console_*.cpk` 内的 `common\etc\pesdb\`。<br>2024 版编辑器的外观功能"still is disabled"。<br>2027 版：固定 400 字节记录，WESYS 加密容器，`PlayerAppearance.bin` 有 "107 customizable parameters"，并说明这些参数与 "licensed 3D face models and head assets" 是分开的<br>（核查更正：① S19 原文是 "player appearance feature is still disabled (data will be decoded again)"，不是 "still is disabled"。② `dt870` 路径不在 S19 正文，而在站长 2025-01-26 的评论里："open dt870_console_all file ..."，再到 "common\etc\pesdb\player.bin"。③ **S18（2027 版编辑器）说的是直接打开并写回 `dt200_console_all.cpk`**，不是 dt870。所以不同版本、不同工具给出的 CPK 不一致，以真实文件为准。400 字节、WESYS、107 项参数、与 3D 脸模分开这几句原文确认） | [S19]（2024-06）；[S18]（2026-10） |
| 脸部模组 | 公开的**只有**「从 eFootball 导出的脸转换到 PES 2021（Fox 引擎）」的脸包，用 Sider 或 CPK 安装，提取过程没有公开。<br>**未找到任何把自制脸放进 eFootball（PC 或移动）的公开模组或教程。** | [S20] 等 |

### 5.2 球员脸部在 eFootball 中如何存储

**能确认的**
- 资源是「UE 原生包与 CPK 混合」[S07]。
- 球员数据库（含外观参数）在 CPK 数据表中 [S18][S19]。
- 外观参数与「授权的 3D 脸模和头部资产」分开存放 [S18]。

**未找到的**（都需要真实文件才能确认）
- 面部或头发 UE 资产的**命名规则和路径**。
- 外观数据中引用头部资产的**字段名和编码**。
- 「FaceNumber」这个字段名。
- `pc3600`、`pc3611` 的含义。

**PES 时代（Fox 引擎）的做法，作对照**
- PES 2021 的脸在 `Asset/model/character/face/real/<球员ID>/`，文件夹里的 `.fpk` 内嵌球员 ID；改 ID 要同时改文件夹名和 `.fpk` 内的 ID [S21][S20][T3]。
  - 核查结果：S21 原文只给出示例 `.../face/real/44840`，说明工具会 "automatically edits the ID embedded inside the .fpk files and renames the folder"。这两点确认。
  - **前缀 `Asset/model/character/` 在 S20、S21 中都没有出现，【未核实】。** S20 只说把带编号的 ID 文件夹复制到游戏的 face 目录。
- [T4] eFootball 的数据表沿用了 PES 的 `Player.bin` 体系（编辑器沿用 PES 时代的命名），但头部模型改用 UE 资产。「按球员或脸 ID 映射到资产路径」的思路**可能**延续下来，但**没有证据**。

**pc 编号已知含义**

| 编号 | 已知含义 | 来源 |
|---|---|---|
| pc3607 | 球衣贴图 | [S15] |
| pc3610 | 记分牌相关 | [S17] |
| pc7000 | EvoMod 修改的对象 | [S16] |
| pc0000 | 国服，是 zip | [S08] |
| pc3600、pc3611 | **未找到公开资料**；只来自任务书 | — |

**拿到文件后的确认方法 [T4]**
1. 用 `iostore.py list` 读目录索引，按路径关键词（face、hair、head、球员 ID）定位包。
2. 解码 `PlayerAppearance.bin`，取一个已知球员（例如亨利 donor 所在的资源槽 110718，任务书内部资料）的记录，与资产路径中的数字对照。
3. 用 `zen_package.py info` 看材质实例的导入表，找贴图和网格之间的引用关系。

### 5.3 在线检测

Konami FAQ 把 "Game data modification: Notable changes were made to player ability levels or appearance (height, etc.)" 列为违规行为 [S22][T1]。所以修改**只能用于用户的本地单机构建**，不能接入官方在线服务。

---

## 6. iOS 特有约束

### 6.1 App 基本信息 [T1]

- iTunes Lookup API（2026-10-25 查询）[S23]：
  - `trackName` "eFootball™"
  - **`bundleId` `jp.konami.pesactionmobile`**
  - `version` **11.1.1**（`currentVersionReleaseDate` 2026-10-15）
  - `fileSizeBytes` 3,484,689,408（约 3.48 GB）
  - `minimumOsVersion` 15.0
  - 首次上架 2017-05-24
- App Store 页面："Approximately 3.5 GB of free storage space is required"。
- Android 包名为 `jp.konami.pesam` [S07]。（核查确认：S07 首帖含 Google Play 链接 `details?id=jp.konami.pesam`；Google Play 该页标题为 "eFootball™ - Apps on Google Play" [T1]）
- 版本号 11.1.1 与任务书一致；build 号 11.1.101 公开资料中**没有**，需看 IPA 的 `Info.plist`（`CFBundleVersion`）。

### 6.2 代码签名与改动 [T1/T3]

**包内资源受签名保护 [T1]**
- Apple TN3126 [S24]："If your code exist in a bundle then the code signature protects not just your code but the resources in your bundle. Central to this is the CodeResources file. Slot -3 in the code directory holds the hash of that file … So, if that file changes, the code directory hash changes and you break the seal on the code signature."
- Apple 文件系统指南 [S25] 对 `AppName.app` 的说明："the bundle directory is signed at installation time. Writing to this directory changes the signature and prevents your app from launching."
- 结论：**修改 IPA 包内任何 `.utoc/.ucas/.pak` 后，都必须对整个 `.app` 重签**：更新 CodeResources，重签主程序和所有嵌入的框架和扩展。

**App Store 二进制是加密的 [T3]**
- App Store 下发的二进制带 FairPlay 加密（`LC_ENCRYPTION_INFO(_64)` 的 `cryptid 1`），嵌入的框架同样如此 [S28]，是开发者论坛用户的观察，非 Apple 官方说明。
  - （核查更正：S28 是一个关于「App Store 增量更新后启动失败（mremap_encrypted）」的帖子。楼主（普通用户 yyy07）的原话是 "every binary with `LC_ENCRYPTION_INFO_64` has `cryptid 1` ... the Firebase/Google frameworks are `cryptid 0`"。所以**并非所有嵌入框架都加密**，有的框架 `cryptid 0`。Apple DTS（Quinn）在帖中只建议提交 bug，没有解释加密机制。eFootball 主程序和框架实际的 cryptid 必须对真实 IPA 用 `otool -l` 检查）
- [T4] 重签侧载需要已解密的二进制。资源文件（`.ucas` 等）是否也被 FairPlay 加密：**未找到**说明。从 FairPlay 只作用于 Mach-O 的描述看，大概率没有。

**免费签名的期限 [T2]**
- Apple DTS 工程师在论坛中说，免费 Personal Team 的描述文件 7 天过期，付费开发者为一年 [S27]（**只看到搜索摘要**）。
  - （核查更正：已直接读取 S27。帖中**没有** Apple DTS 或 Apple 员工的回复。只有楼主（用户，2016-12）说付费账号的描述文件显示 7 天后过期、本以为是一年；另一用户 KMT 反问是否先用过免费描述文件。所以「DTS 工程师说」这一归属是错的。）
  - 「免费 7 天 / 付费 1 年」【未核实】：S27 不能作为来源，需要 Apple 官方文档佐证。核查时 WebSearch 配额已用完，未能另找。

**重签的副作用 [T4]**
- 换签名团队后，原有 entitlements（Game Center、推送、iCloud、钥匙串访问组）可能失效。
- 这会影响「Link Data」等功能。对纯本地单机构建影响有限，但需要实机确认。

**本工程的位置**
- 用户的本地单机版（由另一个 Claude 维护）**已经是改过的客户端**，签名和侧载流程由主控负责。
- 本文只要求：资源补丁交付时说明「替换哪些包内文件」，并提醒主控必须重签。

### 6.3 资源可能放在哪里 [T1 + 未找到]

| 位置 | 特性 | 修改的影响 | eFootball 是否使用 |
|---|---|---|---|
| IPA 包内（`.app/`） | 只读，受签名封印 [S24][S25] | 改了就必须重签 | 3.48 GB 的 IPA 显然包含大量资源 [T4]；具体有哪些容器**未知** |
| Apple 按需资源（ODR） | "hosted on the App Store and are separate from the related app bundle"，由系统管理下载和清除 [S26] | 侧载构建拿不到 App Store 托管的 ODR [T4] | **未找到**使用证据 |
| 应用自行下载到容器（`Documents/`、`Library/Application Support`、`Library/Caches/`） | 不在代码签名范围内。`Caches` 不备份，低空间时可能被系统清掉 [S25] | 不需要重签，但应用可能校验哈希或清单，或通过「Live Update」重新下载覆盖 [T4] | Konami FAQ 提到标题菜单有 "Clear Cache"、"Delete Live Update" 功能，以及 [Extras] > [Game Settings] > [Download Data] > [Commentary Language] [S22]。**确认存在应用自管理的下载数据**；具体目录**未找到** |

**确认下载目录的办法 [T4]**
- 对开发签名的构建，用 Xcode「Devices and Simulators → 下载容器（.xcappdata）」。OWASP MASTG [S29] 说明该方法只对 debug（开发签名）构建有效。
- 或者查看 `Info.plist` 中的 `UIFileSharingEnabled`。
- 或者在主控的单机构建中记录挂载日志。

---

## 7. ASTC：编码、块大小、sRGB、法线、UE 的存放方式

### 7.1 工具与格式 [T1]

**ARM astcenc**（本机 v4.8.0，Apache-2.0）
- 压缩：`astcenc -cl|-cs|-ch|-cH in out BxB quality`。
- 解压：`-dl|-ds`。
- 测试（压缩后立即解压回读）：`-tl|-ts`。
- 质量预设：`-fast`（10）、`-medium`（60）、`-thorough`（98）、`-verythorough`（99）、`-exhaustive`（100）。官方建议 "For compression of production content we recommend using a quality level equivalent to -medium or higher"（`astcenc -help`）[S33]。

**`.astc` 文件头**（16 字节）[S32]：
- 魔数 `13 AB A1 5C`；`block_x`、`block_y`、`block_z`（各 1 字节）；`dim_x`、`dim_y`、`dim_z`（各 3 字节小端）。
- **UE 的 cooked mip 里没有这个头**，只有裸块。本工程 `astc_tool.py` 负责加头和去头。

### 7.2 块大小 [T1]

- 每块 128 位。2D 块大小从 4×4（8.00 bpp）到 12×12（0.89 bpp）[S31][S33]。
- **替换时必须使用与 donor 相同的块大小和尺寸**：cooked 数据中的字符串 `PF_ASTC_NxN`、mip 数和每级字节数都是固定的。本工程的原地补丁只做等长替换（`texture-replace --strict-dims`）。
- UE 项目设置中「ASTC 质量与块大小」的映射：**未找到 4.26 官方文档**。不影响本工程，因为块大小直接从 cooked 字符串读出。

### 7.3 sRGB 与线性 [T1/T3]

- ASTC 块本身不带 sRGB 标志。sRGB 由纹理视图决定：Metal 同时提供 `MTLPixelFormat.astc_4x4_srgb`（"conversion between sRGB and linear space"）和 `astc_4x4_ldr`，iOS 8.0 起支持 [S30][T1]。
- [T4] UE 根据 `UTexture.SRGB` 属性（CUE4Parse 默认 true）选择视图。
- 编码规则（Arm 文档 [S31]）：颜色数据 "nearly always a perceptual quality win to use sRGB input source textures that are then compressed using the ASTC sRGB compression mode (`-cs`)"；"The uncompressed input texture … must be stored in the sRGB color space for `-cs` to function correctly"；alpha 始终是线性的。
- **本工程规则 [T4]**：
  - donor 的 `SRGB` 为 true（如 BaseColor/漫反射）：用 `-cs`，即 `astc_tool.py encode` 的默认行为。
  - donor 的 `SRGB` 为 false（法线、遮罩、粗糙度）：用 `-cl`（`--linear`）。

### 7.4 法线贴图的 ASTC 通道布局（**有冲突，必须实测**）

| 来源 | 说法 | 等级 |
|---|---|---|
| Arm astcenc 文档 [S31] | 推荐把 X、Y 存在 **L+A 端点**：编码时用 `rrrg` 换位（`-normal`），采样用 `.ga`，Z 由 `sqrt(1−x²−y²)` 重建；如要匹配 BC5 的顺序，用 `-normal -esw gggr`，解码用 `-dsw arz1`（核查更正：原文是 "to match **BC5n** component ordering use `-normal -esw gggr` for compression and `-normal -dsw arz1` for decompression"。是 BC5n，不是 BC5；解码同样要带 `-normal`） | T1 |
| CUE4Parse `TextureDecoder.cs`（UE4 的 ASTC 路径） | 解码后若是法线贴图，就用 **R、G** 重建 B，注释为 "UE4 drops blue channel for normal maps before encoding, restore it"，暗示 UE4 的 ASTC 法线是 **RG** 布局 | T3 |
| CUE4Parse `PixelFormat.cs`（UE5） | `PF_ASTC_*_NORM_RG`："RG format stored in LA endpoints … (requires RHI support for texture swizzle)"，说明 UE5 才专门引入 LA 布局的 RG 法线 | T3 |

**影响**
- 本工程 `astc_tool.py --normal` 直接调用 astcenc 的 `-normal`，即 `rrrg`（X→RGB，Y→A）。
- **如果 eFootball 的 UE4 法线是 RG 布局，这样编码会让着色器读错通道（R12/R10）。**

**处置 [T4]**
- 用 astcenc 解码 donor 法线 mip（`-dl`，不加 `-normal`），统计各通道：
  - R≈G≈B 且 A 有变化：L+A 布局；
  - R、G 变化而 B 为常数或已丢弃：RG 布局。
- 然后按实测结果选择 `-esw`（例如 RG 布局用 `-esw rg01` 之类，**需实测确定**）。
- 用回读的 PSNR 和视觉对比验证。在此之前，**不要**用 `--normal` 直接替换法线贴图。

### 7.5 UE 中 ASTC mip 的存放方式（汇总）[T3]

- `PixelFormat` 字符串 → 块大小。
- 每级 mip 的字节数 = 块数 × 16，块按行主序排列，不带 `.astc` 头。
- `Mips[0]` 是最大级。
- 每级的 `FByteBulkData` 标志决定数据在 export 内联，还是在 `.ubulk`（BulkData chunk）或 `.uptnl` 中（§3.5）。
- 内联 mip 的偏移在 export 数据中；外置 mip 的 `OffsetInFile` 指向 `.ubulk` chunk 内部。
- 4.26 有 `NoOffsetFixUp` 语义；本工程 `texture-scan` 还会检查 `SkipOffset` 是否一致。

---

## 8. 工具链计划：解析 → 解码 → 修改 → 重编码 → 重打包 → 验证

> 原则：
> 1. **原地、等长、只改内容**：不增删 chunk，不改 chunk id、目录索引和容器头。
> 2. 每一步都有**独立回读**：自己的读取器、CUE4Parse、astcenc 各读一遍。
> 3. 所有写操作只写到新目录，并生成 manifest（输入输出的 sha256、改动区间）。
> 4. 「独立球员绑定」（新增 chunk 或路径）单独作为第二阶段（§8.7），不与共享槽替换混在一起。

### 8.0 最小必需输入（只要文件，不要账号、密码或私钥文本）

1. IPA 中（或应用下载目录中）**含目标头模和贴图的容器三件套**：`pc3600_mobile_ios.{utoc,ucas,pak}`、`pc3611_mobile_ios.{utoc,ucas,pak}`，命名以真实文件为准，若有 `_sN.ucas` 一并提供。
2. 全局容器 `global.{utoc,ucas}`，或该版本中对应的文件。
3. `Info.plist` 和主程序（**只读**，用来取版本字符串，不改）。
4. 含 `PlayerAppearance.bin` 的 CPK：交给外观格式研究使用，本文不展开。
5. （可选）用户在 PC 上用 FModel（`GAME_eFootball`）导出的同一 donor 头模 LOD0、LOD1 的 GLB/PSK，以及贴图 PNG。
6. AES 密钥：**用户在自己的机器上**通过 `--aes-key-file` 或环境变量 `R9_AES_KEY_HEX` 提供，不进入仓库和日志。

### 8.1 解析（识别格式与定制点）

| 步骤 | 命令或方法 | 通过标准 |
|---|---|---|
| P1.1 | `python3 tools/native/iostore.py selfcheck X.utoc --deep`，再运行 `info` | 记录 TOC 版本（预期 2）、标志位（Encrypted、Signed、Indexed、Compressed）、方法名表、块大小、分区、meta 哈希算法；往返序列化逐字节一致 |
| P1.2 | `iostore.py extract` 取出 ContainerHeader chunk，检查包条目长度 | 判定 16 字节还是 32 字节（§1.3 的冲突） |
| P1.3 | 独立读取：`/opt/tools/cue4parse_check_bin/Cue4ParseCheck X.utoc --game GAME_eFootball`；`retoc --override-container-header-version PreInitial info/list`（只读） | 每个 chunk 的 sha256 与 `iostore.py` 一致 |
| P1.4 | `iostore.py list --glob ...`，按目录索引定位头部、头发和贴图包 | 找到 donor 的 ExportBundleData 和 BulkData chunk |
| P1.5 | `strings` 搜索主程序中的 UE 版本字符串（T4 建议） | 记录结果；没找到不算失败 |

### 8.2 解码

| 步骤 | 命令或方法 | 通过标准 |
|---|---|---|
| P2.1 | `zen_package.py info <ExportBundleData>`，有全局容器时加 `--global-names`、`--initial-load-meta` | 类名为 `/Script/Engine.SkeletalMesh` 或 `Texture2D`；export 区间铺满 chunk；记录 `PackageFlags`（是否 unversioned） |
| P2.2 | `inplace_patch.py texture-scan` 和 `texture-extract --decode` | 像素格式、尺寸、mip 数、每级位置都读出；`skip_offset.consistent` 为 true；PNG 看起来正确 |
| P2.3 | `inplace_patch.py mesh-locate --pair ref_lod0.glb ...`（参考网格来自 FModel） | 每级 LOD 恰好一个 PRIMARY 流；切线和 UV 都找到；没有 `symmetry_tie` |
| P2.4 | 法线贴图通道统计（§7.4） | 判定 RG 或 L+A 布局，写进交付说明 |
| P2.5 | 若用户没有 PC 可跑 FModel | 先基于 §3.1 和 §3.2 写网格解码器生成参考网格（缺口，见 R11） |

### 8.3 修改

- 头模：在 Blender 中，基于 donor 的拓扑、UV 和权重做形体修改（任务 #3 和 #7 的产物）。
  - 顶点数、面列表、顶点顺序必须与 donor 完全一致。
  - **LOD0 和 LOD1 都要改。**
  - 不改骨骼和权重；评估是否存在 MorphTarget。
- 贴图：按 donor 的原生分辨率绘制（例如 256 px），保持相同的通道语义（BaseColor 用 sRGB，法线和遮罩用线性）。
- 发型（Cascão 前额半圆）要靠**几何体加贴图**同时表现（任务书 4.2）。
  - 如果 donor 发型网格的顶点数不够，原地补丁无法增加顶点，只能另选 donor 发型资产。

### 8.4 重编码

| 步骤 | 命令或方法 | 通过标准 |
|---|---|---|
| P4.1 | `astc_tool.py encode --block <donor> --size <donor> --mips [--linear]`；法线按 P2.4 的结果另行指定换位 | 每级字节数与 donor 一致；`roundtrip` 的 PSNR 达到门限 |
| P4.2 | 网格：`mesh-patch-positions --pair ref new --tangents --bounds union --verify`（LOD0、LOD1 各一对） | 不变的顶点保留原字节；切线为单位长度；包围盒包住所有新顶点；补丁区间外的字节不变 |
| P4.3 | 贴图：`texture-replace --strict-dims --verify` | 只改 mip 数据区 |

### 8.5 重打包

| 步骤 | 命令或方法 | 通过标准 |
|---|---|---|
| P5.1 | `iostore.py patch X.utoc --replace-list repl.json --out-dir out/ --verify`（默认追加模式） | 原 chunk id、目录索引、完美哈希、方法表不变；替换块以方法 0 写入；加密容器用同一把密钥加密且先验证密钥正确；签名容器默认拒绝 |
| P5.2 | 不推荐（§4.2）：retoc `to-zen`（不压缩、不加密）或 UnrealReZen（v3、32 字节条目） | 只在 P1 证明兼容后才考虑 |

### 8.6 验证

| 步骤 | 命令或方法 | 通过标准 |
|---|---|---|
| P6.1 | 恒等测试：用 donor 自己替换 donor（贴图和网格各做一次） | 输出与输入的 sha256 完全相同 |
| P6.2 | `iostore.py hash-all` 修改前后各跑一次，然后 `diff` | 只有计划内的 chunk 变化 |
| P6.3 | 从新容器中 `extract`，再运行 `texture-extract --decode` 和以新网格为参考的 `mesh-locate` | 在同一偏移找到新数据；解码图与源图的 PSNR 达标 |
| P6.4 | CUE4Parse 独立回读（`cue4parse_check`）；条件允许时 FModel 目视检查 | 全部 chunk 都能读；网格和贴图可以导出 |
| P6.5 | 交付主控：说明替换的文件、需要重签、回退方式（保留原文件和 sha256） | — |
| P6.6 | 实机（**由用户在隔离测试客户端中进行**）：加载、远近 LOD 切换、各机位和光照下的发型、比赛动作中有无拉伸或穿模、**其他引用同一 donor 槽位的球员**是否被连带改变 | 用户验收，不得伪造结果 |

### 8.7 第二阶段：独立资源（新增 chunk 或路径，对应任务 #9）[T4 设计，未验证]

要让 R9 不再覆盖共享槽位，需要以下全部步骤：

1. **新包名**：例如 donor 包名加后缀。计算 `FPackageId = CityHash64(lower UTF-16 包名)`。
2. **新 chunk**：`(新 PackageId, 0, ExportBundleData)`，有外置 bulk 时再加 `(新 PackageId, 0, BulkData)`。
3. 复制 donor 的 Zen 包，改写其中的 `Name` 和 `SourceName`（FMappedName 指向本包名字表）。如需扩展名字表，要重排各段偏移，**不再是等长补丁**。
4. **目录索引**：加入新路径的文件项，`UserData` 指向新 TOC 条目序号。
5. **容器头**：追加 PackageId 和包条目。eFootball 为 16 字节布局：`ExportCount`、`ExportBundleCount`、`ImportedPackages`。
6. 新增 chunk 的方式有两种：
   - 放进**新的补丁容器**（自带容器头），依赖 4.26 的包存储会合并所有已挂载容器的容器头 [T4]；
   - 或在原容器末尾追加。v2 没有完美哈希，追加 chunk id 不会破坏查找。
7. **引用**：必须有东西指向新包，例如外观数据表中的脸 ID 到路径的映射，或材质实例的导入。这一层的机制**完全未知**，依赖 §5.2 的确认方法。
8. 移动端能否像 PC 一样，靠 UE 标准 `Content/Paks` 下的 `_P` 包覆盖或新增内容：**未知**，必须在隔离客户端实测。

---

## 9. 风险清单

| ID | 风险 | 影响 | 缓解或检测 | 等级 |
|---|---|---|---|---|
| R1 | Konami 定制的 4.26：容器头为 16 字节条目，可能还有未公开的改动 | 第三方写入器写出的容器不被接受，或解析错位 | 只做原地补丁；P1.2 实测条目长度；所有结论以 CUE4Parse `GAME_eFootball` 回读为准 | T3/T4 |
| R2 | 容器 AES 加密 | 没有密钥就看不到路径和内容 | 用户在本机提供密钥；工具不记录密钥；用挂载点或解码先证明密钥正确 | T3 |
| R3 | 签名（`Signed` 标志或 `.sig`）或运行时强制校验 | 修改后的容器被拒绝加载 | P1.1 读取标志；签名容器默认拒绝修补。是否需要改主程序**超出本组范围**，交由主控决定 | T3/T4 |
| R4 | 应用层完整性校验或 Live Update 覆盖 | 修改被检测，或被重新下载覆盖 | 只在本地单机构建中使用，关闭在线更新；记录原文件哈希以便回退 | T4（FAQ 证实 Live Update 存在） |
| R5 | iOS 代码签名 | 改包内文件后不重签就无法启动 | 交付说明中写明必须整体重签；可能需要已解密的 IPA；免费签名 7 天过期【未核实】（见 §6.2 核查更正） | T1/T3 |
| R6 | 共享 donor 槽位 | 其他球员的脸被一起改掉（亨利项目问题 3） | 第二阶段做独立绑定（§8.7）；在此之前只在隔离测试客户端验证，不能冒充生产级绑定 | 任务书 |
| R7 | Oodle 与混用压缩方法 | 解码依赖专有库或 GPL 实现；替换块不压缩会使体积变大；运行时是否接受混用未证实 | 读取方法名表；实机验证；如有必要，用户按 UE EULA 取得 Oodle 后再压缩 | T3/T4 |
| R8 | meta 哈希算法（SHA-1 还是 BLAKE3）以及运行时是否校验 | 哈希写错时，若运行时校验则加载失败 | `--hash-algo auto` 实测判定，判定不了就拒绝 | T4 |
| R9 | 无版本属性 | 没有 `.usmap` 就读不出 `SRGB`、`CompressionSettings` 等属性 | 原地补丁不需要属性；属性值可在 FModel 中（如有映射）或通过实验推断 | T3 |
| R10 | ASTC 参数不一致：块大小、sRGB、mip 数 | 颜色发灰或过亮，mip 闪烁，越界 | `--strict-dims`；sRGB 跟随 donor；每级 mip 都重编码 | T1/T3 |
| R11 | 顶点格式或 donor 参考网格缺失 | `mesh-locate` 失败（例如压缩位置格式、没有 FModel 导出） | 按 §3 写原生解码器；stride 不是 12 时扩展支持 | T4 |
| R12 | 法线通道布局（RG 还是 L+A）、MorphTarget、表情骨骼 | 光照错误，表情时脸部变形 | P2.4 实测；检查 MorphTargets 属性；在比赛动作中检查 | T3/T4 |
| R13 | LOD 数大于 2、mobile 最低 LOD、LOD 数据在 bulk 中 | 远景仍显示 donor 的脸 | 列出全部 LOD，逐级处理；bulk 中的 LOD 用 BulkData chunk 作为输入 | T3 |
| R14 | 游戏版本更新（11.1.x → 后续） | 容器、chunk 布局、密钥都可能变 | 补丁绑定具体版本和输入文件的 sha256；每个版本重新生成 | T4 |
| R15 | 在线检测与条款 | Konami 把修改外观列为违规 [S22] | 仅限离线或本地单机 | T1 |
| R16 | 分区上限和对齐（PartitionSize、16 KiB 内存映射对齐等） | 追加后越界，内存映射失败 | `--verify` 检查对齐和越界；4.26 只有一个分区 | T4 |
| R17 | 发型几何受 donor 顶点数限制 | Cascão 造型无法只靠移动顶点做出来 | 选择合适的 donor 发型；或走第二阶段新增资产 | T4 |
| R18 | 许可与版权 | Konami 资产受版权保护；Oodle 受 UE EULA 约束；GPL 工具的传染性 | 资产只在本地私用，不入公共仓库、不分发；工具只按许可调用 | T1/T3 |
| R19 | 第三方源码是否代表 UE 真实行为 | 读取器与写入器犯同样的理解错误时，自检发现不了 | 用多实现交叉（CUE4Parse、retoc、UEViewer、本工程）；最终只能靠实机验证 | — |

---

## 10. 未找到的信息与解决途径

| 项目 | 状态 | 解决途径 |
|---|---|---|
| Konami 官方的 UE 版本号 | 未找到（只说 "Unreal Engine"） | 读可执行文件中的字符串，以及 `.utoc` 版本字节 |
| 11.1.1 / build 11.1.101 是否仍是 4.26 | 未证实 | 同上 |
| `pc3600_mobile_ios`、`pc3611_mobile_ios` 的含义 | 未找到 | 读目录索引 |
| 面部和头发资产的命名、路径；头部引用字段（"FaceNumber"） | 未找到 | 读目录索引，并与 `PlayerAppearance` 解码结果对照（§5.2） |
| eFootball 的压缩方法 | 未找到 | `iostore.py info` |
| 移动容器是否签名 | 未找到 | `iostore.py info`，看标志位 |
| iOS 下载数据的目录 | 未找到 | Xcode 下载容器，或挂载日志（§6.3） |
| UE4 ASTC 法线的通道布局 | 有冲突 | donor 解码统计（§7.4） |
| 4.26 的 meta 哈希算法 | 未找到一手源码 | 实测（§2.12） |
| 公开的 eFootball 脸部模组 | 未找到 | — |
| PES 2019 Mobile 使用的 UE4 小版本 | 未找到 | 与本项目无关 |

---

## 11. 来源

> 访问日期均为 2026-10-25。「搜索摘要」表示只看到搜索引擎摘要，页面本身因 403 等原因未能读取。

**官方或一手 [T1]**

- [S01] Konami（美区），"KONAMI PRESENTS eFootball™ …"，2021-07-21。https://www.konami.com/games/us/en/topics/1724/
- [S02] Konami（欧区）新闻稿，2021-07-21。https://www.konami.com/games/eu/en/topics/15974/
- [S14] Epic Games Japan（鍬農健二郎），「UE4.25 IoStore」幻灯片。https://www.slideshare.net/slideshow/ue425iostore/239108822
- [S22] Konami eFootball FAQ（英文）。https://www.konami.com/efootball/faq/0/en
- [S23] Apple iTunes Lookup API。https://itunes.apple.com/lookup?id=1117270703&country=us ；App Store 页面：https://apps.apple.com/us/app/efootball/id1117270703
- [S24] Apple TN3126「Inside Code Signing: Hashes」（2022-05-03 重发，2024-02-06 修订）。https://developer.apple.com/documentation/technotes/tn3126-inside-code-signing-hashes
- [S25] Apple「File System Programming Guide – File System Basics」。https://developer.apple.com/library/archive/documentation/FileManagement/Conceptual/FileSystemProgrammingGuide/FileSystemOverview/FileSystemOverview.html
- [S26] Apple「On-Demand Resources Guide」（2017-01-12 更新）。https://developer.apple.com/library/archive/documentation/FileManagement/Conceptual/On_Demand_Resources_Guide/index.html
- [S30] Apple Metal `MTLPixelFormat.astc_4x4_srgb`、`astc_4x4_ldr`、`astc_6x6_srgb`。https://developer.apple.com/documentation/metal/mtlpixelformat/astc_4x4_srgb
- [S31] Arm astc-encoder `Docs/Encoding.md`。https://github.com/ARM-software/astc-encoder/blob/main/Docs/Encoding.md
- [S32] Arm astc-encoder `Docs/FileFormat.md`。https://github.com/ARM-software/astc-encoder/blob/main/Docs/FileFormat.md
- [S33] `astcenc -help`（v4.8.0，本机输出）；许可证 Apache-2.0：https://github.com/ARM-software/astc-encoder/blob/main/LICENSE.txt
- [S34] Epic API 文档 `UCryptoKeysSettings`。https://dev.epicgames.com/documentation/unreal-engine/API/Plugins/CryptoKeys/UCryptoKeysSettings
- [S35] Epic「Oodle Data」（UE 5.8 文档页）。https://dev.epicgames.com/documentation/en-us/unreal-engine/oodle-data
- [S36] Epic 博客「Oodle now free to use in Unreal Engine via GitHub」（搜索摘要）。https://www.unrealengine.com/blog/oodle-now-free-to-use-in-unreal-engine-via-github
- [S37] Epic 博客「Unreal Engine 4.27 released」（搜索摘要）。https://www.unrealengine.com/blog/unreal-engine-4-27-released
- [S38] RAD Game Tools，Oodle 页面与授权页面。https://www.radgametools.com/oodle.htm ；https://www.radgametools.com/sales.htm

**媒体与参考 [T2]**

- [S05] mac4ever，"PES Mobile 2019 passe sous Unreal Engine 4"，2018-12-17。https://www.mac4ever.com/mobilite/139630-pes-mobile-2019-passe-sous-unreal-engine-4-video
- [S06] 9to5mac，"PES 2019 graphics update"，2018-12-17（搜索摘要）。（核查：已直接读取，实际标题 "Pro Evolution Soccer 2019 for iOS updated with Unreal Engine 4, 12 new leagues"）https://9to5mac.com/2018/12/17/pes-2019-graphics-update/
- [S27] Apple Developer Forums「provisioning profile expiring in 7 days」中 Apple DTS 的回复（搜索摘要）。（核查更正：帖中没有 DTS 回复，只有两名用户发言。此帖不能支持「免费 7 天 / 付费 1 年」，见 §6.2）https://developer.apple.com/forums/thread/69248
- [S29] OWASP MASTG-TECH-0059（Xcode 下载应用容器，仅限 debug 构建；搜索摘要）。https://mas.owasp.org/MASTG/techniques/ios/MASTG-TECH-0059/

**社区、论坛与第三方代码 [T3]**

- [S07] Gildor's Forums「eFootball 2022-2026」（spiritovod）。https://www.gildor.org/smf/index.php/topic,7938.msg41448.html
- [S08] Gildor's Forums「[UE4] eFootball PES Mobile」（国服），2023-07-12。https://www.gildor.org/smf/index.php/topic,8641.0.html
- [S09] CUE4Parse：commit `7fdb7b9a`「eFootball support」https://github.com/FabianFG/CUE4Parse/commit/7fdb7b9a8729299ebbf088e3d5b523a2a70a2c8a ；源码基准 `77b94fa`（`CUE4Parse/UE4/IO/Objects/*.cs`、`UE4/IO/IoStoreReader.cs`、`UE4/Assets/Exports/{SkeletalMesh,StaticMesh,Texture}/*.cs`、`UE4/Objects/{Meshes,RenderCore}/*.cs`、`UE4/Assets/Objects/{FByteBulkDataHeader,EBulkDataFlags}.cs`、`UE4/Versions/{EGame,VersionContainer}.cs`、`Compression/OodleHelper.cs`、`CUE4Parse-Conversion/Textures/TextureDecoder.cs`）https://github.com/FabianFG/CUE4Parse/tree/77b94fa26cde08d34e80be2ced29c8f8885ff0a7
- [S10] retoc 源码 `885a8da`（`retoc/src/{lib,version,container_header,iostore_writer,compression}.rs`，README）。https://github.com/trumank/retoc/tree/885a8dae740cb1ce1e41ff2e74f67f9f0c118237
- [S11] retoc issue #18「Failed to parse ContainerHeader」（PesMobile，UE 4.26.2），2025-03。https://github.com/trumank/retoc/issues/18
- [S12] retoc issue #7（SD Gundam Battle Alliance，UE 4.27.2，PreInitial 的由来）。https://github.com/trumank/retoc/issues/7
- [S13] UEViewer `Unreal/FileSystem/IOStoreFileSystem.cpp` @ `a0bfb46`。https://github.com/gildor2/UEViewer/blob/a0bfb468d42be831b126632fd8a0ae6b3614f981/Unreal/FileSystem/IOStoreFileSystem.cpp
- [S15] pesmodding，「eFootball detailed texture replacement」，2023-07-04（教程作者 Cadi Lillian）。https://www.pesmodding.com/2023/07/efootball-detailed-texture-replacement.html
- [S16] pesmodding，「eFootball 2026 Patch EvoMod 5.4.0」，2026。（核查更正：该 URL 当前显示 "eFootball 2026 Patch: EvoMod 5.5.0"，2026-05-28）https://www.pesmodding.com/2026/04/efootball-2026-patch-evomod-540.html
- [S17] pesmodding，「eFootball 2025 transparent scoreboard」，2025-09-01。（核查更正：实际标题 "eFootball 2026 Transparent Scoreboard for Blimp Camera Angle [ver. 2.0]"；URL 中的 2025 只是路径）https://www.pesmodding.com/2025/07/efootball-2025-transparent-scoreboard.html
- [S18] pesmodding，「eFootball 2027 Player.bin Editor」（RBsGameLab），2026-10-14。https://www.pesmodding.com/2026/08/efootball-2027-playerbin-editor-by.html
- [S19] pesmodding，「eFootball 2024 Player Data Editor V3.6.0.0」，2024-06-22。https://www.pesmodding.com/2023/02/efootball-20222023-player-data-editor_28.html
- [S20] pesmodding，「PES 2021 faces converted from eFootball」，2022-12-15。https://www.pesmodding.com/2022/12/pes-2021-faces-converted-from-efootball.html
- [S21] pesmodding，「Face ID Relinker PES 2021」，2026-06-12。https://www.pesmodding.com/2026/06/face-id-relinker-pes-2021.html
- [S28] Apple Developer Forums thread 845557（用户观察：`cryptid 1` 与 FairPlay 加密的嵌入框架），2026-09。（核查补注：帖子标题为 App Store 更新后 dyld "could not register fairplay decryption" 启动失败；楼主同时指出部分框架为 `cryptid 0`）https://developer.apple.com/forums/thread/845557
- [S39] WorkingRobot/OodleUE README（UE EULA 声明）。https://github.com/WorkingRobot/OodleUE
- [S40] trumank/repak `oodle_loader/src/lib.rs`（下载 `liboo2corelinux64.so.9`，2.9.10）。https://github.com/trumank/repak/blob/master/oodle_loader/src/lib.rs
- [S41] powzix/ooz（GPL-3.0）。https://github.com/powzix/ooz
- [S42] oozextract（crates.io 0.5.6，MIT）。https://crates.io/crates/oozextract
- [S43] 4sval/FModel（GPL-3.0）。https://github.com/4sval/FModel
- [S44] atenfyr/UAssetAPI 与 UAssetGUI（MIT）。https://github.com/atenfyr/UAssetAPI
- [S45] gildor2/UEViewer README（MIT）。https://github.com/gildor2/UEViewer
- [S46] Archengius/ZenTools（MIT，已被 retoc 取代）。https://github.com/Archengius/ZenTools
- [S47] WistfulHopes/ZenTools-UE4（MIT）。https://github.com/WistfulHopes/ZenTools-UE4
- [S48] rm-NoobInCoding/UnrealReZen（GPL-3.0）@ `bf9e8de`（`UnrealReZen/Core/{Constants,FIoDependency,FIoPack}.cs`、`Program.cs`）。https://github.com/rm-NoobInCoding/UnrealReZen
- [S49] narknon/PhoenixUProj wiki「Packaging assets as IO Store (utoc ucas)」（UE 4.27.2，`_P` 三件套）。https://github.com/narknon/PhoenixUProj/wiki/Packaging-assets-as-IO-Store-(utoc-ucas)
- [S50] Unreal Engine 论坛「Project Crypto」（eFootball 2025 补丁加密后闪退；搜索摘要，页面现为 404）。https://forums.unrealengine.com/t/project-crypto/2658816
- [S51] CUE4Parse LICENSE（Apache-2.0）。https://github.com/FabianFG/CUE4Parse/blob/master/LICENSE
- [S53] TheNaeem/Unreal-Mappings-Archive（没有 eFootball 条目）。https://github.com/TheNaeem/Unreal-Mappings-Archive

**本工程内部 [T4 / 合成数据验证]**

- `tools/native/README_iostore.md`、`tools/native/README_zen_inplace.md`、`tools/native/cue4parse_check/README.md`、`tools/native/{iostore,zen_package,inplace_patch,astc_tool}.py`。
- 任务书 `docs/TASK_BRIEF.md`：亨利项目的 donor 数据、`pc3600_mobile_ios` 与 `pc3611_mobile_ios` 的命名、资源槽位 110718。

---

## 核查记录

> **核查方式**
> - 核查人：对抗性事实核查（独立复核），2026-10-25。
> - 原则：每条结论都回到**被引用的原始来源**重读，再找**至少一个独立来源**交叉对照。
>   - 代码类结论：本地检出的 CUE4Parse `77b94fa`、retoc `885a8da` 源码，加上 GitHub 页面（commit、issue、文件历史），以及 UEViewer、UnrealReZen、repak、Arm 文档的原始文件（raw.githubusercontent.com）。
>   - 事实类结论：直接读取 Konami、Apple、pesmodding、Gildor 原页面，加上 iTunes Lookup API 与 Google Play 的实时查询。
> - **限制**：本轮 WebSearch 的共享配额在核查开始时已经用完，只能直接抓取已知 URL，没有做新的搜索引擎检索。因此凡是「原文来源打不开，又找不到替代来源」的条目，都标为【未核实】。
> - **结论**：抽查 41 项关键结论（A–E 五组）。
>   - 完全确认：34 项。
>   - 含更正：4 项（C5、E5、E7、E8）。都是引文措辞、标题、日期、归属或过度概括，**没有改变任何格式、偏移或工具链结论**。
>   - 含未核实：4 项（D5、E6 的路径前缀、E8 的「7 天/1 年」、E9）。
>   - E8 同时有更正和未核实，所以 34 + 4 + 4 − 1 = 41。
>   - 正文中已就地标注「（核查更正：…）」或【未核实】，原有内容一律保留。

### A. 引擎版本与 Konami 定制（§0、§1）

| # | 被核查的结论 | 结果 | 核查依据 |
|---|---|---|---|
| A1 | CUE4Parse commit `7fdb7b9`「eFootball support」：作者 LongerWarrior，**2026-07-03**；新增 `GAME_eFootball = GAME_UE4_26 + 29`；改动 3 个文件（`EGame.cs`、`FFilePackageStoreEntry.cs`、`FInstancedStaticMeshInstanceData.cs`） | **确认** | ① GitHub commit 页面：消息、作者、3 个文件、新增行原文；② GitHub 上 `FFilePackageStoreEntry.cs` 的文件历史：日期 Jul 3, 2026；③ 本地 `77b94fa` 源码 `EGame.cs:130`。CUE4Parse 中只有这 3 处提到 eFootball（grep） |
| A2 | eFootball 包条目为 **16 字节**（`ExportCount`、`ExportBundleCount`、`ImportedPackages`），原版 4.26 为 **32 字节**；retoc `PreInitial => (8, 16)`，`Initial => (24, 32)`；retoc 把 4.25/4.26/4.27 都映射到 `Initial` | **确认** | ① CUE4Parse `FFilePackageStoreEntry.cs`：eFootball 分支与默认分支（跳 8、读 4+4、跳 8、CArrayView 8 = 32）；② retoc `container_header.rs:436-437`、`version.rs:44-46`。两个实现彼此独立，结论一致 |
| A3 | retoc issue #18：PesMobile、UE 4.26.2、`obb\PesMobile\Content\Paks`、chunk id `29f5850b27aea78f0000000a`、`--override-container-header-version PreInitial`、报告者回复 "it worked"，时间 2025-03-06 至 03-15 | **确认** | ① GitHub issue 原页（报告者 AbirHayat，维护者 trumank 2025-03-14 给出 PreInitial，同日报告者确认，03-15 关闭）；② 维护者同帖 "IoStore container compression is not implemented yet"，与 retoc 源码 `iostore_writer.rs:68`（方法索引 0）一致 |
| A4 | retoc issue #7：SD Gundam Battle Alliance（UE 4.27.2）；维护者说 16 字节布局来自 "4.27.2 与 5.0.0 之间" 移除 `ExportBundlesSize/Load Order/Pad` 的提交 | **确认** | ① GitHub issue 原页（2025-02-08 原话）；② #18 中维护者说 "Tested as working in #7 as well"，两帖相互印证 |
| A5 | Gildor「eFootball 2022-2026」：spiritovod，2021-09-30 建帖，2026-02-15 最后编辑；"UE4 version: 4.26"；mixed resources（UE4 + cpk）；移动版不用改全局容器名 | **确认（补注）** | ① Gildor 原帖；② 国服帖 [S08] 同样写 4.26；③ CUE4Parse 把 eFootball 定为 4.26 变体。补注：「改名为 `global.*`」出自第 2 楼（FModel），不在首帖；移动版那句还包括 "or use game specific override"；"first game from the series using UE4" 与 PES 2019 Mobile 用 UE4 字面冲突（§1.2 后已补注） |
| A6 | Gildor 国服帖：2023-07-12，UE 4.26，"pc0000 is actually zip file …"，"pc* paks are using key for global/pc version while nt* paks are using separate key" | **确认** | ① Gildor 原帖逐句；② 帖子注明官网 pes.163.com |
| A7 | Konami 2021-07-21 新闻稿："Dramatically rebuilt in Unreal® Engine…"；木村誠太郎 "we've made a number of modifications to virtually create a new football game engine"；没有给出 UE 版本号；移动版 "will be detailed at a later date" | **确认** | 美区 [S01]、欧区 [S02] 两个原页面分别读取，引文一致；两页都没有版本号 |
| A8 | PES 2019 Mobile 于 2018-12 改用 UE4（mac4ever："utilise désormais le moteur d'Epic Unreal Engine 4"，要求 iOS 9.0；9to5mac：UE3→UE4） | **确认（S06 已升级）** | 两篇原文都已直接读取（2018-12-17）。S06 原标为「只看到搜索摘要」，现已核实，正文已标注 |
| A9 | pesmodding 教程 [S15]：UE 4.26.2、工程名必须是 "PesConsole"、4 个设置项、chunk 1、Shipping、`pakchunk1-WindowsNoEditor` 改名为 `pc3607_console_win` | **确认（补注）** | ① 原页逐条；② 与 [S49]（PhoenixUProj，4.27.2，三件套同名 `_P`）的打包流程一致。补注：发布者 G. Leroy，教程署名 Cadi Lillian，页面标题 "eFootball 2023 Detailed Texture Replacement Tutorial" |

### B. IoStore 格式（§2）

| # | 被核查的结论 | 结果 | 核查依据 |
|---|---|---|---|
| B1 | `FIoStoreTocHeader` 固定 **144 字节**，§2.2 表中各偏移（16 Version、20 TocHeaderSize … 56 ContainerId、64 Guid、80 Flags、81 EncryptionMethod、84 PerfectHashSeedsCount、88 PartitionSize、96、100、104 Reserved8[5]） | **确认** | CUE4Parse `FIoStoreTocHeader.cs`（`SIZE = 144`），逐字段累加偏移，结果完全吻合；retoc `lib.rs`（`compression_method_name_length: 32`，第 542 行） |
| B2 | `FIoOffsetAndLength` 为 10 字节**大端** 5+5；压缩块条目为 12 字节小端 5/3/3/1 | **确认** | CUE4Parse `FIoOffsetAndLength.cs`（字节 [0] 移 32 位）、`FIoStoreTocCompressedBlockEntry.cs`（`SIZE = 5+3+3+1`，40/24 位掩码）；UEViewer 注释 "CompressionBlocks are not in sync with any chunk data…" 原文确认 |
| B3 | `EIoChunkType` 两套枚举：UE4 中 ContainerHeader = 10，UE5 中 = 6；11、12 只在 retoc 中出现；retoc 用 `version > PerfectHash` 判断新枚举 | **确认** | CUE4Parse `FIoChunkId.cs`（UE4 枚举到 10 为止，`EIoChunkType5` 中为 6）；retoc `lib.rs:1263-1300`、`lib.rs:862` |
| B4 | TOC 版本映射：retoc 4.25/4.26 → DirectoryIndex(2)，4.27 → PartitionSize(3)，5.0–5.3 → 5，5.4 → 6，5.5–5.7 → 8；UEViewer 注释 "UE4.27, allows to use multi-volume (partitioned) ucas files"；容器头版本映射 5.0→1 … 5.6/5.7→5 | **确认** | retoc `version.rs:25-55`；UEViewer `IOStoreFileSystem.cpp@a0bfb46` 第 19 行 |
| B5 | `FIoChunkId` 布局，以及 ChunkIndex 字节序冲突（CUE4Parse 注释 `NETWORK_ORDER16`，retoc 按小端写） | **确认** | CUE4Parse `FIoChunkId.cs:68`；retoc `lib.rs:877-878`（`u16::to_le_bytes`） |
| B6 | 完美哈希：FNV 变体（`0xcbf29ce484222325`、`0x100000001B3`，先乘后异或），以及查找步骤 | **确认** | CUE4Parse `FIoChunkId.HashWithSeed`、`IoStoreReader.TryResolve`（第 167–200 行） |
| B7 | chunk meta：v<8 为 33 字节（32+1），v≥8 为 24 字节（20+1+3）；retoc 写 BLAKE3 截断到 20 字节再补零 | **确认** | CUE4Parse `FIoStoreTocEntryMeta.cs`；retoc `lib.rs:1035`（`from_blake3`）、`iostore_writer.rs:62` |
| B8 | CUE4Parse 缺陷：同时请求「目录索引 + meta」时，没有跳过目录索引 | **确认** | 重读 `FIoStoreTocResource.cs:178-200`：`ReadDirectoryIndex` 分支只记录 `DirectoryIndexBufferOffset`，不前移位置，meta 从同一位置开始读。与本工程 `cue4parse_check/README.md` 的实测记录（读出 `../../../`）一致 |
| B9 | 签名段布局，CUE4Parse 注释 "You could verify hashes here but nah"；Epic `UCryptoKeysSettings` 中有 `bEnablePakSigning`、`SigningModulus`、`SigningPublicExponent`、`SigningPrivateExponent` | **确认** | CUE4Parse `FIoStoreTocResource.cs:168-175`；Epic API 文档原页（共 10 个属性，含上述 4 个，描述原文 "Enable signing of pak files, to prevent tampering of the data."） |
| B10 | UE5 容器头魔数 `0x496F436E`，版本 0–5 | **确认** | CUE4Parse `FIoContainerHeader.cs:46-63` |

### C. cooked 资产与 ASTC（§3、§7）

| # | 被核查的结论 | 结果 | 核查依据 |
|---|---|---|---|
| C1 | `PF_ASTC_4x4..12x12` = 50–54（bpp 8.00/3.56/2.00/1.28/0.89）；HDR 为 73–77；NORM_RG 为 87–91（原注释 "RG format stored in LA endpoints…"）；8x5/8x6/10x8 为 253–255，是库内扩展值 | **确认** | CUE4Parse `PixelFormat.cs`（枚举与行内注释原文）。UE 官方头文件不在本环境，所以「这些是 UE 官方编号」仍只到 T3 |
| C2 | 纹理序列化：`SkipOffset` 在 4.20–4.27 为绝对位置、UE5 为相对位置；`PackedData` 中 bit31 = 立方体、bit30 = OptData、bit29 = CPU 副本、低 30 位 = NumSlices；`PixelFormat` 是 FString；bulk 标志值 0x1/0x40/0x100/0x800/0x1000/0x2000/0x10000 | **确认** | CUE4Parse `UTexture.cs:118-125`、`FTexturePlatformData.cs:40-43、97`、`EBulkDataFlags.cs` |
| C3 | Zen 包：`FPackageSummary` 64 字节、`FExportMapEntry` 72 字节、`FExportBundleHeader` 8 字节（UE5 为 16）；`PKG_UnversionedProperties = 0x2000` | **确认** | CUE4Parse `FPackageSummary.cs`（8+8+4+4+9×4+4 = 64）、`FExportMapEntry.cs`（`Size = 72`）、`FExportBundleHeader.cs`、`EPackageFlags.cs:23` |
| C4 | eFootball 骨骼网格不读光追数据；`FPackedNormal` 自 4.20（`IncreaseNormalPrecision`）起用 `^0x80808080`，`Pack = clamp(round((v+1)×127.5))` | **确认** | CUE4Parse `VersionContainer.cs:80-85`（eFootball 不在 HasRayTracingData 列表中）、`FPackedNormal.cs:13-50`、`FRenderingObjectVersion`（`< GAME_UE4_21 => IncreaseNormalPrecision`） |
| C5 | ASTC 法线：CUE4Parse 注释 "UE4 drops blue channel for normal maps before encoding, restore it"（暗示 RG 布局）；Arm 推荐 `rrrg`/`.ga`（L+A） | **确认（部分更正）** | CUE4Parse `TextureDecoder.cs:364`；Arm `Docs/Encoding.md` 原文。更正：Arm 原文是 "to match **BC5n** component ordering"，不是 BC5；解码参数为 `-normal -dsw arz1` |
| C6 | ASTC 每块 16 字节；`.astc` 文件头魔数 `13 AB A1 5C`；astcenc 质量预设 fast = 10、medium = 60、thorough = 98、verythorough = 99、exhaustive = 100，以及 "-medium or higher" 的建议；sRGB 编码建议原文 | **确认** | 本机 `astcenc -help`（v4.8.0）；Arm `Docs/FileFormat.md`、`Docs/Encoding.md`；256² 各块大小的字节数已重新算过（65,536 / 29,584 / 16,384） |
| C7 | Metal `astc_4x4_srgb` 自 iOS 8.0 起可用，说明为 "conversion between sRGB and linear space" | **确认** | Apple 文档 JSON（`introducedAt: 8.0`，摘要原文） |

### D. 工具与许可（§4）

| # | 被核查的结论 | 结果 | 核查依据 |
|---|---|---|---|
| D1 | UnrealReZen：`PackUtocVersion = 3`；UE4 依赖条目 `DepLinks_UE4.SizeOf() = 8×2+4×4`（32 字节），并实际用于写容器头 chunk；README "…doesn't work until you bypass the sig loader"；GPL-3.0 | **确认** | `bf9e8de` 的 `Constants.cs`、`FIoDependency.cs:179-192`、`FIoPack.cs:150、241、258`、`README.md:50`、`LICENSE` |
| D2 | retoc：写入不压缩（方法 0、块大小 0x10000）、"TODO encrypt directory index"、MIT 许可；README 称 5.3 及以上支持良好，更早版本 "failing to load a handful of assets" | **确认** | 本地 retoc `iostore_writer.rs:32、68`、`lib.rs:530`、`LICENSE`、`README.md:77-80`；issue #18 维护者原话 |
| D3 | 许可：CUE4Parse Apache-2.0、FModel GPL-3.0、UEViewer MIT、UAssetAPI MIT、ZenTools MIT（README "superseded by retoc"，支持 5.1/5.2）、ZenTools-UE4 MIT、ooz GPL-3（`kraken.cpp` 文件头）、oozextract 0.5.6（2026-10-23，MIT） | **确认** | 各仓库 LICENSE 原文件；本地 `/opt/tools/ref/ooz/kraken.cpp`；crates.io API。oozextract 与 ooz 之间的许可沿袭关系仍为 T4，未核实（原文已注明） |
| D4 | Oodle：repak 下载 `liboo2corelinux64.so.9`（2.9.10），从 OodleUE 的 `…/EpicGames.Oodle/Sdk/` 取得并校验 sha256；OodleUE 最新为 2.9.16，原文 "By using these assets, you agree to the Unreal Engine EULA"；CUE4Parse 用 `liboodle-data-shared.so`（2.9.16）；Epic 文档 "The decoder at runtime never needs to know what method was used" 以及 256KB 块大小建议；RAD "We generally license per game and per platform" | **确认** | repak `oodle_loader/src/lib.rs:100-191`；OodleUE `README.md:9、21、38`；CUE4Parse `OodleHelper.cs:25-27`；Epic「Oodle Data」（UE 5.8 页）；RAD sales 页 |
| D5 | Epic 博客「Oodle now free…」「UE 4.27 released」的引文 [S36][S37] | **未核实** | 两个 URL 仍返回 403，与原作者的情况相同；没有替代来源（搜索配额已用完）。D4 的 OodleUE EULA 声明和 Epic Oodle 文档可以间接说明「在 UE 中使用受 UE EULA 约束」 |
| D6 | Unreal-Mappings-Archive 中没有 eFootball，只有 "Maximum Football" | **确认** | 仓库根目录列表 |

### E. 社区信息与 iOS（§5、§6）

| # | 被核查的结论 | 结果 | 核查依据 |
|---|---|---|---|
| E1 | iTunes Lookup：`bundleId` `jp.konami.pesactionmobile`、`version` 11.1.1、`currentVersionReleaseDate` 2026-10-15、`fileSizeBytes` 3,484,689,408、`minimumOsVersion` 15.0、首次上架 2017-05-24；App Store 页面 "Approximately 3.5 GB of free storage space is required…" | **确认** | ① 实时调用 iTunes Lookup API，各字段逐一吻合（seller：Konami Digital Entertainment Co., Ltd.）；② App Store 网页：版本 11.1.1、Oct 15、iOS 15.0、引文原文 |
| E2 | Android 包名 `jp.konami.pesam` | **确认** | ① S07 首帖中的 Google Play 链接；② Google Play 页面标题 "eFootball™ - Apps on Google Play" |
| E3 | Apple TN3126 引文（CodeResources、Slot -3、"break the seal"）；2022-05-03 重发、2024-02-06 修订；文件系统指南中 `.app` "signed at installation time…prevents your app from launching"；ODR "hosted on the App Store and are separate from the related app bundle"（2017-01-12）；OWASP MASTG 中 Xcode 下载容器 "only works for debug builds" | **确认** | Apple 文档 JSON 与原页面；修订记录原文（另有 2022-05-24 的小修订，原文未列，不影响结论）；OWASP 原页 |
| E4 | Konami FAQ："Game data modification" / "Notable changes were made to player ability levels or appearance (height, etc.)"；Clear Cache、Delete Live Update；[Extras] > [Game Settings] > [Download Data] > [Commentary Language] | **确认** | Konami FAQ 原页逐句 |
| E5 | pesmodding [S16][S17][S18][S19] 的标题、日期、措辞 | **更正** | S16 当前为 EvoMod **5.5.0**（2026-05-28）；S17 实际标题为 "eFootball **2026** Transparent Scoreboard … [ver. 2.0]"；S19 原文是 "still disabled"，dt870 路径出自站长评论；S18 写回的是 **`dt200_console_all.cpk`**。400 字节、WESYS、107 项参数、与 3D 脸模分开这几点确认 |
| E6 | PES 2021 脸路径 `Asset/model/character/face/real/<ID>/`，`.fpk` 内嵌 ID [S20][S21] | **部分确认 / 前缀未核实** | S21 原文确认 `.../face/real/44840`，以及「改 .fpk 内 ID + 改文件夹名」；`Asset/model/character/` 前缀在两个来源中都没有出现 |
| E7 | App Store 二进制和嵌入框架都带 FairPlay（`cryptid 1`）[S28] | **更正** | S28 楼主（普通用户）原话指出 Firebase/Google 框架为 `cryptid 0`，所以「嵌入框架同样如此」不能一概而论；Apple DTS 在帖中没有解释加密机制 |
| E8 | 「Apple DTS 工程师说免费描述文件 7 天、付费 1 年」[S27] | **更正 + 未核实** | 直接读取 S27：帖中没有 DTS 或 Apple 员工回复，只有两名用户（2016-12）。归属错误已更正；7 天/1 年的说法本身需要 Apple 官方来源 |
| E9 | Epic Games Japan 幻灯片 [S14]：IoStore 在 4.25 为 Experimental、4.26 为 Beta，以及 `.ucas`/`.utoc` 的日文术语 | **未核实** | SlideShare 对 WebFetch 和 curl 都只返回错误页 |

### F. 核查后仍然成立的关键判断（承重结论）

1. eFootball 是 **UE 4.26 的 Konami 定制版**，目前唯一有代码证据的 IoStore 定制是**容器头包条目 16 字节**（CUE4Parse + retoc 两套独立实现 + issue #18 实测）。
2. IoStore 的 TOC 头、偏移/长度、压缩块、chunk id、meta、完美哈希等**全部字节布局**在 CUE4Parse、retoc、UEViewer 之间一致，§2 可以作为 `tools/native/iostore.py` 的规格依据（T3）。
3. **没有现成写入器能直接产出 eFootball 兼容容器**：UnrealReZen 固定写 v3 和 32 字节条目；retoc 写入不压缩、不加密。所以「原地、等长」补丁路线仍然成立（T4 判断，依据已核实）。
4. iOS 版本 11.1.1、bundleId、包体大小已实时确认；build 号 11.1.101 仍需从 IPA 的 `Info.plist` 读取。
5. `pc3600_mobile_ios`、`pc3611_mobile_ios`、FaceNumber 的含义在公开资料中**仍然未找到**（核查没有发现任何能补上的公开来源），需要真实文件。
