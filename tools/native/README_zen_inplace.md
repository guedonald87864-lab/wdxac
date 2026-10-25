# Zen 包头解析 + 尺寸保持的原地补丁（资产层）

本目录中与本文档对应的文件：

| 文件 | 作用 |
|---|---|
| `zen_package.py` | 解析 IoStore（Zen）包头：UE4.26/4.27 `FPackageSummary`、UE5 `FZenPackageSummary`（含版本信息、name batch）。输出每个 export 的对象名、类、在 chunk 中的绝对字节范围，并启发式判断 UE 版本 |
| `inplace_patch.py` | 尺寸保持的原地补丁：贴图 mip 定位/导出/替换（ASTC），网格顶点流（位置、切线、UV）按内容定位，位置/切线/包围盒原地改写 |
| `../tests/test_zen_inplace.py` | 59 个 pytest 测试（其中 27 个 `test_adv_*` 为独立审查新增，见第 11 节）：合成数据 + retoc 附带的真实 UE4.27/UE5.x 样本包 |

它们位于 IoStore 容器层（`iostore.py`，见 `README_iostore.md`）之上：先用 `iostore.py extract` 从 `.utoc/.ucas` 中取出
chunk，再用本文档的工具在 chunk 内部定位和改写字节，最后用 `iostore.py patch` 把改好的 chunk 放回新容器。

## 1. 状态（按任务书四级标注）

| 项目 | 状态 |
|---|---|
| UE4.26/4.27 Zen 包头解析（名称表、哈希、导入/导出表、export bundle、graph data、export 字节范围） | **UE4.27 已完成且验证**：合成包 + retoc 附带的全部 7 个真实 UE4.27 包（export 数、bundle 数、chunk 大小与容器 store entry 一致；`TestModUI` 220 个 export 与同名 legacy `.uasset+.uexp` 逐字节一致）。**UE4.26 没有真实样本**：只依据 CUE4Parse/retoc 两者都对 4.26/4.27 使用同一布局 |
| UE5.4–5.6 Zen 包头解析与版本判断 | **已完成且验证**：真实 UE5.4/5.5/5.6 包（4 个带 legacy 对照文件的包 export 范围逐字节一致；全部 11 个真实 UE5 样本布局判断正确、无警告、export 首尾相接）。真实样本全部是无版本信息（unversioned）的包 |
| UE5.0–5.3 Zen 包头解析；带版本信息（versioned）的 UE5 包 | **已完成候选，未在真实数据上验证**：只有本工具自带构建器生成的合成包（同一套对格式的理解写、同一套读，只能证明自洽，不能证明与真实 UE 一致）。5.0–5.2 的 `legacy_offset` 约定未经核实（retoc 视 CookedSerialOffset 为相对值，CUE4Parse 对 5.0–5.2 视为绝对值） |
| 类名解析（script object） | **已完成且验证**：用真实 UE4.27 `LoaderInitialLoadMeta`（23393 项）与 UE5.3 `ScriptObjects`（30977 项）测试，哈希重算 0 个不一致；没有 global 数据时内置约 160 个常用引擎类的哈希表，其余打印原始 `FPackageObjectIndex` |
| 贴图 `FTexturePlatformData` 定位、mip 报告（内联 / BulkData / OptionalBulkData / MemoryMapped） | **已完成且验证**：合成 ASTC（astcenc 真实编码 9 级 mip，内联、分离、UE5.2+ bulk data map 三种）+ 真实 UE4.27 DXT5、UE5.4 BGRA8、UE5.5/5.6 DXT1（含 bulk data map 与 `.ubulk`）贴图；SkipOffset 一致性校验在这 4 个真实贴图上全部通过。真实数据中没有 ASTC 贴图（ASTC 只有合成数据） |
| ASTC mip 同尺寸替换 + `--verify` 回读 + astcenc 解码检查 | **已完成，合成数据验证**（包括经过 AES 加密 + 压缩 + 多分区的合成 IoStore 容器的完整往返，第 6 节第 7 步）；eFootball 真实贴图 **缺少原始输入** |
| 网格顶点流按内容定位（位置/切线/UV/包围盒） | **已完成，合成数据验证**（FModel 风格 GLB 重排序、PSK、OBJ、任意旋转、两级 LOD、对称网格）；真实 UE5.5 `SM_Cube` 上定位到真实偏移；eFootball 头模 **缺少原始输入** |
| 位置/切线/包围盒原地改写 + `--verify` | **已完成，合成数据验证**；能否在 iPhone 上正常加载 **等待实机** |

**没有任何一项在 eFootball 的真实文件上跑过。** 拿到文件后必须按第 6 节重新核验，不能假定 eFootball 的布局与样本一致。

## 2. 为什么用“尺寸保持”策略

亨利项目的教训是：改模型和贴图本身不难，难的是“改完以后游戏还能加载、其他东西不被破坏”。最稳妥的做法是：
**保留供体资产的拓扑、顶点数、顶点顺序、UV、骨架和蒙皮权重、贴图尺寸、像素格式、mip 数量，只覆盖等长的字节。**

* 不增删任何字节，包内所有偏移、大小、`SkipOffset`、bulk data 偏移、export map 的 `CookedSerialSize`、UE5 bulk data map、
  容器头里的 `ExportBundlesSize` 全都不用改。需要重建的只有 IoStore 容器本身（chunk 长度不变，只换内容和块哈希）。
* 每次写入都记录“偏移 + 长度 + 新旧 sha256”，可以逐字节审查、比较，并完整回退（原文件永不被覆盖）。
* 代价：新头模必须做成与供体**完全相同的顶点数和顶点顺序**（由 donor-wrap 工具把我们的头型包裹到供体拓扑上），
  新贴图必须与供体**相同尺寸、相同 ASTC block**。顶点数不同的模型需要重写整个 LOD 渲染数据，那是另一条高风险路线，本工具拒绝这种输入。
* 骨骼、蒙皮权重、morph target、布料等数据都不动，所以不会因为改了几何就破坏动画绑定；
  但大幅移动顶点后，蒙皮权重仍然是供体的权重，脸部大改要检查动画（第 7 节）。

## 3. zen_package.py

```bash
Z=tools/native/zen_package.py
python3 $Z info  pkg.uasset                                   # 版本判断、证据、export 表（名称、类、abs_start/abs_end、大小）
python3 $Z info  pkg.uasset --json report.json               # 完整 JSON（names/imports/exports/bundles/graph/bulk map）
python3 $Z info  pkg.uasset --global-names LoaderGlobalNames.bin --global-name-hashes LoaderGlobalNameHashes.bin \
                 --initial-load-meta LoaderInitialLoadMeta.bin  # UE4：用 global.utoc 中的 chunk 解析类名
python3 $Z info  pkg.uasset --script-objects ScriptObjects.bin   # UE5
python3 $Z extract-export pkg.uasset --export SK_Head --out-dir out/e1 --verify   # 取出单个 export 的字节（只读操作，也写 manifest）
python3 $Z hash script /Script/Engine.Texture2D              # 计算 FPackageObjectIndex / FName 哈希 / FPackageId
```

* **版本判断方法：** 用每一种已知布局（UE4.26-4.27、UE5.0-5.1、5.2、5.3、5.4-5.5、5.6+）去解析，只有所有结构检查都通过
  （偏移单调、名称哈希算法 ID 为 `0xC1640000`、export map 是 72 字节的整数倍、每个 export 恰好一个 Create 和一个
  Serialize 条目、graph data 恰好读完、各段首尾相接、export 范围不越界且互不重叠等）的布局才被接受，证据会一并输出。
  名称的 CityHash64 不一致、export 之间有空隙、chunk 末尾有多余字节只给警告（`evidence` 中只有在 export 真正首尾相接时
  才会出现 “contiguous and non-overlapping”）。带版本信息的 UE5 包只接受与版本号和 summary 大小一致的那一种布局
  （1009 + 44 字节 = 5.2，1009 + 52 字节 = 5.3，≥1012 = 5.4-5.5，≥1015 = 5.6+）。所有布局都失败时，会放宽名称哈希算法
  ID 再试一次（retoc 也这样做，有的游戏改了这个值），结果会带警告。
  4.26 与 4.27 的包布局完全相同，要用 `.utoc` 的版本号区分（2 = 4.26，3 = 4.27，见 `iostore.py info`）。
* **export 字节范围：** 4.26–5.2 中，export 数据从 graph data（或 UE5 的 HeaderSize）之后开始，按 export bundle 的顺序连续存放；
  5.3 及以后为 `HeaderSize + CookedSerialOffset`。每个 export 还给出 `legacy_offset`（在旧 `.uasset+.uexp` 坐标系中的位置）
  和 `legacy_to_chunk_delta`，用于校验贴图 `SkipOffset` 这类绝对偏移。
* 类名优先用 global script object 表解析；没有时用内置的引擎常用类表（这些索引就是类路径的哈希，与游戏无关）；
  游戏自定义类、以及 4.26/4.27 的包导入（同样是路径哈希）在没有对应数据时，打印原始 `FPackageObjectIndex`。

## 4. inplace_patch.py：贴图

```bash
P=tools/native/inplace_patch.py
python3 $P texture-scan    T_Face.uasset [--zen-export T_Face] [--json scan.json]
python3 $P texture-extract T_Face.uasset --bulk T_Face.ubulk --out-dir out/tex0 --decode --verify   # 导出供体 mip（raw + PNG）
python3 tools/native/astc_tool.py encode r9_face.png --block 6x6 --size 256 --mips --outdir out/enc   # 与供体同尺寸、同 block
python3 $P texture-replace T_Face.uasset --bulk T_Face.ubulk --astc-manifest out/enc/r9_face.astc6x6.json \
                           --out-dir out/tex1 --verify --decode-check
```

* **定位方法：** 在 export 中查找像素格式 FString（`PF_ASTC_4x4`、`PF_ASTC_6x6`、`PF_ASTC_8x8`、`PF_ASTC_4x4_HDR`…，前面带 int32 长度），
  它前面 12 字节必须是合理的 SizeX/SizeY/PackedData，后面必须能解析出 mip 数组：第 i 个序列化的 mip 尺寸为
  SizeX >> (FirstMipToSerialize + i)（SizeX/SizeY 是全尺寸；移动端 LOD bias 会让 FirstMipToSerialize > 0，有自动测试），
  payload 大小严格等于该格式的块计算结果（SizeOnDisk = 0 的空 mip 允许存在，但至少要有一级带数据）。可识别三种 mip 布局：UE4（每级前有 `bool bCooked`）、UE5（没有）、
  UE5.2+（bulk 头只是 bulk data map 的下标，需要输入 Zen 包）。还会核对贴图前面的 `FName + int64 SkipOffset`：
  UE4.20–4.27 中它是 legacy 坐标下的绝对位置，与 export 的 delta 一致才算通过；UE5 中它是相对位置。
  输入是裸 export（没有 Zen 包头）时，UE4 的绝对 SkipOffset 无法核对，`consistent` 为 `null`（不是 false）。
* **mip 位置：** 每级 mip 都报告 bulk 标志、ElementCount、SizeOnDisk、OffsetInFile，以及数据实际所在位置：
  `inline`（紧跟 bulk 头之后，**以头的位置为准**。真实 UE4.27 样本中 OffsetInFile 与实际位置相差 2 字节，所以不能用它定位内联数据）、
  `BulkData`（.ubulk chunk，偏移 = OffsetInFile，要求带 NoOffsetFixUp）、`OptionalBulkData`（.uptnl）、`MemoryMappedBulkData`（.m.ubulk）。
* **块填充规则：** 真实 UE4.27 样本把小于一个块的 mip（2×2、1×1）记录成块大小（4×4）；真实 UE5.5/5.6 样本则记录精确尺寸
  （2×1、1×1）。两种都能识别。
  替换时把“精确相同”和“块填充后相同”都视为同一 mip，并在 manifest 中写明采用了哪条规则；加 `--strict-dims` 则只接受精确相同。
* **拒绝条件（每一条都有自动测试）：** 像素格式与 manifest 的 block 不同；顶层尺寸不同（贴图尺寸不允许改）；任何一级 mip 的尺寸或字节数不同；
  manifest 的 .bin 哈希不符、.bin 不存在、mip 条目不是按 0..n-1 排列的 mip 链；mip 在 .ubulk 中但没有提供 `--bulk`；
  .ubulk 比 mip 范围短；payload 是压缩的；payload 位于包内 PayloadAtEndOfFile 区域（尚未在真实数据上确认）；
  需要 offset fix-up；PackedData 带 HasCpuCopy（UE5.4+ FSharedImage，`texture-scan` 在 `unsupported` 中列出）；
  一个 export 里有多个 platform data 而没有给 `--index`（或越界）；输出目录非空或与输入相同。拒绝时不创建输出目录。
* `--verify`：重新扫描输出文件，检查结构（每级 mip 的偏移、尺寸、bulk 头）不变、每级 payload 等于新数据、补丁范围以外的字节完全不变；
  `--decode-check` 还会用 astcenc 逐级解码新 payload。
* sRGB/线性、法线贴图等属性存在 UTexture 的 tagged properties 中，本工具不修改。编码时要和供体保持一致（例如法线贴图要用 `astc_tool.py --normal --linear`）。

## 5. inplace_patch.py：网格

```bash
python3 $P mesh-locate SK_Head.uasset --zen-export SK_Head --ref head_lod0.glb [--json loc.json]
python3 $P mesh-patch-positions SK_Head.uasset --zen-export SK_Head \
        --pair head_lod0.glb r9_lod0.glb --pair head_lod1.glb r9_lod1.glb \
        --tangents --bounds union --out-dir out/mesh1 --verify
```

**参考网格：** 必须是用 FModel/umodel 从**同一个资产、同一级 LOD**导出的 GLB/glTF、PSK 或 OBJ（位置为 UE 单位或米）。
工具不做猜测：它在字节里寻找与参考网格一致的数据。

* **位置流：** 对文件中每一个 float32 三元组（4 种字节对齐）取“排序后的绝对值”做哈希，与参考点集比对。这个不变量不受轴交换和正负号影响，
  因此可以同时尝试 48 种轴排列/翻转，缩放依次尝试 100（米→厘米，如 FModel glTF）、1、0.01。在固定步长上出现的连续命中即为候选流，
  然后投票确定具体的轴排列，再把每个流顶点映射到同位置的参考顶点（FModel glTF 按三角形首次使用重新排序了顶点，这一步可以还原映射）。
  候选流前面的 `FPositionVertexBuffer` 头（Stride, NumVertices, 12, NumVertices）用于确认；后面紧跟的 `FStaticMeshVertexBuffer` 元数据
  （UV 套数、是否高精度切线、是否 float UV）也会读出来。若轴排列不是 48 种之一（例如参考网格被任意旋转过），会退回“按顺序”的相似变换搜索。
* **已知约定：** FModel/CUE4Parse glTF 为 `(X, Z, Y) × 0.01`（米）；PSK/ActorX 为 Y 取反；OBJ 的 V 一般被翻转（工具会两种都试）。
  报告中会写出识别到的约定和变换矩阵。
* **切线流：** `FPackedNormal` int8×4（UE4.20+，TangentX + TangentZ，步长 8）、`FPackedRGBA16N` int16×4（高精度，步长 16），以及 4.20 以前的 uint8 偏置编码。
  做法是把参考法线变换到 UE 空间，再与 TangentZ 比对。**UV 流：** half（`FVector2DHalf`）或 float，每顶点 1–4 套 UV 交错存放。
  接缝、硬边、极点处多个参考顶点共用同一位置，只要与其中任意一个一致即算匹配；之后再用 UV 和法线消除映射歧义。
  先在位置流后面紧邻的区域搜索（cooked LOD 的序列化顺序），找不到再搜索整个范围。
* **包围盒：** `FBoxSphereBounds`（UE4：7 个 float32。UE5 开启 LWC 后 ImportedBounds 是 7 个 double（CUE4Parse `ReadFReal`），
  本工具**不支持**，找不到候选时 `--bounds` 会拒绝）。要么用 `--old-bounds` 给出 FModel 显示的 ImportedBounds 精确值，要么按条件搜索：
  盒子包含顶点 AABB、每个轴最多大 25%、`max(extent) ≤ radius ≤ |extent|`。`--bounds union` 只会放大包围盒（不会缩小，避免被错误剔除）；
  `recompute` 则保留原有边距，按新顶点重新计算。
* **对称网格的风险：** 脸部模型常常严格左右对称，此时镜像的轴排列与正确的排列同样能匹配所有位置，左右顶点可能被对调，
  不对称的修改会被镜像写入。工具会依次用 UV/切线一致性、顶点顺序相关性来区分；仍然无法区分时标记 `symmetry_tie`，`mesh-patch-positions` 会拒绝执行。
  解决办法是改用 PSK（保持缓冲区顺序），或在检查 `alternative_transforms` 之后显式加 `--allow-symmetric-tie`。
* **多个匹配：** 同一组坐标可能在别处还有副本（真实 UE5.5 `SM_Cube` 的 BodySetup 中就有一份 24 个顶点的碰撞数据）。
  只有“完整”的流（所有流顶点都有对应参考顶点，且数量 ≥ 参考顶点数）才算 primary；恰好一个 primary 带有 `FPositionVertexBuffer` 头时会被自动选中，
  其余匹配记录在 manifest 的 `other_matches_not_patched` 中。否则需要用 `--stream-offset` 明确指定。
* **`mesh-patch-positions` 规则：** 新网格必须与参考网格顶点数相同、面列表完全相同（证明顶点顺序没变），否则拒绝；
  所有流顶点都要有对应的参考顶点（`--allow-partial` 可放宽）；歧义顶点如果在新网格中移动不一致也会拒绝（`--allow-ambiguous` 可放宽）。
  新网格中位置没变的顶点保留**原始字节**（不经过浮点往返，diff 很干净）。
  `--tangents` 使用“增量法”：`n' = normalize(n_原存储 + R(n_新几何) − R(n_旧几何))`，其中几何法线按位置焊接（接缝不出现硬边）；
  TangentX 对 n' 重新正交化，W 分量（副法线符号）保留。没有被移动、也不与移动顶点相邻的顶点，切线字节完全不变。
* **LOD0 和 LOD1 是两组独立的流**（各自的位置、切线、UV、蒙皮权重缓冲区）。**两级都必须修改**，否则远景会显示供体原来的脸。
  每级 LOD 用一个 `--pair 参考 新网格`，可以在同一次运行中同时修改；没有列出的 LOD 不会被改动（manifest 中有说明）。
  若 LOD 数据是流式存放的（在 .ubulk 中），就把 BulkData chunk 作为输入，包围盒则用 `--bounds-file 包.uasset` 指定
  （`--bounds-file` 与输入是同一个文件时只产生一个输出文件；审查前会拆成两个各缺一半修改的输出，见第 11 节）。
* `--verify`：用**新网格**作为参考，在输出文件中重新定位，必须在同一偏移找到；存储的 float32 必须等于计划值；
  切线法线必须为单位长度；包围盒必须包含所有新顶点；输入文件和 `--bounds-file` 的输出在补丁范围以外的字节必须完全不变。

## 6. 拿到真实文件后要做什么（按顺序）

需要用户提供的最小文件集合（只需文件，**不需要账号、密码或私钥**；如果容器加密，用户在自己机器上用 `--aes-key-file` 或环境变量提供密钥，密钥不进入任何输出）：

1. IPA 中包含目标头模（`pc3600_mobile_ios`）和贴图（`pc3611_mobile_ios`）的 `.utoc/.ucas/.pak` 文件；
2. `global.utoc/.ucas`（用于读取 `LoaderGlobalNames`/`LoaderGlobalNameHashes`/`LoaderInitialLoadMeta`，UE5 则为 `ScriptObjects`）；
3. 用 FModel（或 umodel）对**同一个供体头模**导出的 LOD0、LOD1 GLB（或 PSK），以及 FModel 显示的 `ImportedBounds` 数值（可选）。
   如果用户没有 PC 来运行 FModel，我们需要先根据第 1 项写出原生网格解码器来生成参考网格（参考网格只是用来做定位，换哪个工具生成都可以）。

核验步骤：

1. `iostore.py selfcheck/info`：确认容器版本（4.26/4.27/UE5）、压缩方式、是否签名；
2. `iostore.py extract` 取出头模和贴图所在包的 ExportBundleData chunk（以及对应的 BulkData chunk）；
3. `zen_package.py info`：确认没有警告、evidence 中有 “export ranges are contiguous and non-overlapping” 和
   “export ranges end exactly at the chunk end”、类名为 `/Script/Engine.SkeletalMesh` / `Texture2D`；
   有 global chunk 时加上 `--global-names ... --initial-load-meta ...`；
4. `inplace_patch.py texture-scan`：确认像素格式、尺寸、mip 数、每级 mip 的位置，`skip_offset.consistent` 必须为 true；
   `texture-extract --decode` 导出的供体 PNG 必须看起来正确（这一步证明格式理解无误）；
5. **恒等测试：** ASTC 贴图的 `texture-extract` 会额外写出与 astc_tool 格式相同的 `donor.astcNxM.json/.bin`；
   用它做一次 `texture-replace --strict-dims --verify`，输出必须与输入逐字节相同（sha256 相同）。
   网格同理：`mesh-patch-positions --pair 参考 参考 --tangents --bounds union --verify` 的输出必须与输入完全相同
   （manifest 中每个改动都是 `identical: true`，moved 为 0）。两项都有对应的自动测试；
6. `mesh-locate`：必须恰好有一个 PRIMARY 流且带头部确认，切线、UV 都要找到，不能出现 `symmetry_tie`；
7. 写入真实修改 → `--verify` → `iostore.py patch --verify` 重建容器 → 用 `iostore.py extract` 从新容器取回 chunk，再次运行 `--verify`/`texture-extract --decode` 做独立回读；
   这一整条流程对**贴图**有自动测试（`test_adv_end_to_end_encrypted_compressed_container`：AES 加密、压缩、多分区、4 KiB 块、
   跨 20 多个块且最后一块不满的 chunk、零长度 chunk、非 ASCII 路径，append 与 rebuild 两种模式，并检查密钥没有写进任何输出）。
   网格只测试到 chunk 层，没有经过容器往返；
8. 只在隔离测试客户端中加载验证，见第 7 节。

## 7. 限制与风险（如实说明）

* **共享资源槽：** 原地补丁改的是供体资源本身，所有引用该资源的球员都会变（亨利项目的问题 3）。本工具解决的是“字节怎么改才安全”，
  不解决独立球员绑定（任务书第八节）。在完成独立资源注册之前，只能在隔离测试客户端中验证，不能当作生产级独立绑定。
* 尚未覆盖：贴图 `HasCpuCopy`（UE5.4+）、包内 PayloadAtEndOfFile 的 mip、压缩的 bulk payload（都会被拒绝，不会猜测）；
  UE5 的 double 精度包围盒；UE5.6+ Verse cell 只读取两个偏移、不解析表内容，真实 UE5.6 样本的 cell 表全为空，
  非空 cell 表没有测试过（cell export 的字节会表现为 export 之间的空隙，只给警告）；网格的 morph target、布料、顶点色、蒙皮权重、法线贴图都不修改；
  非 float32 位置格式（部分手游用 16 位压缩位置）需要拿到真实数据后再扩展。
* 移动顶点后，供体的蒙皮权重会原样作用于新形状。脸部的大幅修改需要用比赛动作检查有没有拉伸或穿模（任务书第九节）。
* 定位依赖参考网格与游戏数据一致。如果 FModel 导出时做了额外处理（例如合并了顶点），PRIMARY 判断会失败，工具会拒绝，而不是写错位置。

## 8. 安全规则（所有写命令）

* 只写到新建的或空的 `--out-dir`；输出路径与任何输入相同时拒绝；文件（包括 PNG 预览）用独占模式创建；
  先完成全部检查再创建输出目录，拒绝时不留下半成品目录；
* 生成 `manifest.json`：输入/输出文件的路径、大小、sha256；参数；每个改动区间（文件、偏移、长度、内容、新旧 sha256）；verify 结果；
* `--verify` 用同一套读取代码重新读取输出；任何一项检查失败都返回非零退出码。注意：它证明的是“写入结果与本工具对格式的
  理解一致、补丁范围外字节不变”，**不证明** UE 会以同样方式读取——资产层没有第三方读取器做交叉检查
  （容器层由 `iostore.py` 的测试用 CUE4Parse 交叉检查）；
* 本层不接触密钥（解密由 `iostore.py` 负责）。

## 9. 格式来源

CUE4Parse（`/opt/tools/ref/CUE4Parse`）：`UE4/IO/Objects/FPackageSummary.cs`、`FZenPackageSummary.cs`、`FExportMapEntry.cs`、`FExportBundleHeader.cs`、
`FExportBundleEntry.cs`、`FMappedName.cs`、`FPackageObjectIndex.cs`、`FScriptObjectEntry.cs`、`FBulkDataMapEntry.cs`；
`UE4/Assets/IoPackage.cs`；`UE4/Objects/UObject/FNameEntrySerialized.cs`；`UE4/IO/IoGlobalData.cs`；
`UE4/Assets/Exports/Texture/UTexture.cs`、`FTexturePlatformData.cs`、`FTexture2DMipMap.cs`；`UE4/Assets/Objects/FByteBulkDataHeader.cs`、
`EBulkDataFlags.cs`、`TBulkData.cs`；`UE4/Objects/Meshes/FPositionVertexBuffer.cs`；`UE4/Assets/Exports/StaticMesh/FStaticMeshVertexBuffer.cs`、
`FStaticMeshUVItem.cs`；`UE4/Objects/RenderCore/FPackedNormal.cs`、`FPackedRGBA16N.cs`；`UE4/Assets/Exports/SkeletalMesh/FStaticLODModel.cs`、
`USkeletalMesh.cs`；`UE4/Objects/Core/Math/FBoxSphereBounds.cs`；`CUE4Parse-Conversion/Writers/Gltf/Gltf.cs`、`Writers/ActorX/ActorXMesh.cs`。
retoc（`/opt/tools/ref/retoc`）：`retoc/src/zen.rs`、`name_map.rs`、`script_objects.rs`、`version.rs`（引擎版本 ↔ 对象版本号）、
`zen_asset_conversion.rs`（UE5 CookedSerialOffset 不含 cooked header）、`version_heuristics.rs`，以及它附带的真实样本包和
`*.metadata.json` 中的 store entry（用于测试）。
Google CityHash v1.1（`/opt/tools/ref/cityhash`）：`CityHash64`，测试中的黄金值由其 C++ 源码编译生成。

## 10. 测试

```bash
cd tools && python3 -m pytest tests/test_zen_inplace.py -q      # 59 项；没有 /opt/tools/ref/retoc 时，真实样本相关的测试会跳过
```

合成测试覆盖：CityHash64 黄金值；4.26/4.27 合成包（2 个 bundle、UTF-16 名称、graph data）及其损坏后被拒绝；UE5 四种布局的判断；
CLI 的 manifest 与拒绝覆盖；9 级 ASTC（astcenc 真实编码）的内联与 .ubulk 两种存放方式的扫描、替换、verify、解码；
各种拒绝路径（block 不同、尺寸不同、缺少 bulk、manifest 被篡改、输出目录非空、`--strict-dims`）；
FModel 风格 GLB（重排序 + 轴交换 + 米）、PSK（Y 镜像）、OBJ（V 翻转）、任意旋转的定位；两级 LOD 一次修改，并检查切线和包围盒；
贴图与网格的恒等往返（输出与输入 sha256 相同）；
拒绝路径（顶点数不同、面不同、LOD 不匹配、重复顶点歧义、参考网格不在文件中、输出目录非空）；对称网格被标记并拒绝。
审查新增（`test_adv_*`）：缺少 Serialize 条目 / export 范围重叠或有空隙 / 带版本信息的 UE5 各布局 / 非标准名称哈希 ID；
奇数偏移处的 UTF-16 名称与 BMP 以外字符；零长度 export、5 个 bundle、非 ASCII export 名与路径；全部真实样本与 store entry 一致；
FirstMipToSerialize > 0；压缩 / 包尾 / 需要 fix-up / .ubulk 截断的拒绝；SizeOnDisk = 0 的 mip；HasCpuCopy；manifest 乱序与缺失 .bin；
一个 export 多个 platform data 与 `--index`；UE5.2+ bulk data map 贴图的替换与恒等往返；`--bounds-file` 等于输入；同一 LOD 列两次；
加密 + 压缩 + 多分区容器的完整往返（见第 6 节第 7 步）。

性能（**手工测量，不在自动测试中**）：12.8 MB 文件中的 5440 顶点头模（无关随机字节在前后各 6 MB），`locate_all` 完整定位
（位置、切线、UV、包围盒）约 9.6 秒，进程内存峰值约 240 MB（4 核容器，审查时复测）。

## 11. 独立审查记录（对照 CUE4Parse / retoc 逐项核对后的修改）

下列问题都由 `tests/test_zen_inplace.py` 中的 `test_adv_*` 覆盖；标 *已复现* 的在修改前用探针脚本在原代码上确认过。

| 问题 | 影响 | 修改 |
|---|---|---|
| 某个 export 没有 Serialize 条目（另一个被序列化两次）时仍被接受，范围为 (-1, -1)（*已复现*） | `extract-export` 写出空文件且 `--verify` 通过 | 每个 export 必须恰好一个 Create + 一个 Serialize（与 retoc 一致），否则该布局不成立 |
| 5.3+ 的 export 范围来自 CookedSerialOffset，重叠也被接受；README 却说会检查“铺满 chunk”（*已复现*） | 可能把错误的字节范围当成某个 export | 范围越界或重叠即拒绝；有空隙给警告，只有真正首尾相接时才给出该证据 |
| 带版本信息的 UE5 包：同样 summary 大小的几种布局都能解析，取了列表里第一个（*已复现*） | 5.2/5.4/5.5/5.6 的包被标成 `ue5.0-5.1`/`ue5.3`，并带虚假的 “other layouts also parsed” 警告；1009 + 52 字节被称为 UE5.2 | 有版本号时只接受 `ue5_layout_for_version` 推出的布局；版本名称表按 retoc `version.rs` 修正（1009 = 5.2 或 5.3） |
| 名称哈希算法 ID 不是 0xC1640000 时所有布局都失败（*已复现*） | 改过此值的游戏完全无法解析（retoc 885a8da 已放宽） | 严格解析失败后放宽此项再试，结果带两条警告 |
| HasOptData 标志靠近扫描范围末尾时 `struct.error` 直接抛出（*已复现*） | `texture-scan` 整个命令崩溃 | 越界的候选直接跳过 |
| 裸 UE4 export（没有 Zen 包头）的 SkipOffset 被报告为 “ue5-relative, consistent: false”（*已复现*） | 用户按第 6 节要求检查 `consistent` 时被误导 | 优先给出一致的解读；否则按 mip 布局选 UE4/UE5 解读，无法核对时为 `null` 并附说明 |
| `--bounds-file` 与输入是同一文件时产生两个各缺一半修改的输出，`--verify` 仍通过（*已复现*） | 拿到的输出要么没改包围盒、要么没改顶点 | 同一文件只用一个 Patch；`--verify` 也检查 bounds 文件补丁范围外字节不变 |
| SizeOnDisk = 0 的 mip 使整个 platform data 被丢弃 | 贴图“找不到” | 允许空 mip（至少一级有数据） |
| HasCpuCopy 的候选被静默丢弃 | 只报 “no FTexturePlatformData found” | `texture-scan` 列入 `unsupported`，替换时给出明确拒绝原因 |
| `--index` 越界抛 IndexError；manifest mip 乱序会把 level 错配到别的 mip | 崩溃 / 潜在错写 | 越界与乱序都明确拒绝；manifest 必须是 0..n-1 的 mip 链（允许块填充尺寸） |
| `texture-extract` 先建目录后检查 fix-up；PNG 用普通模式写；输入文件名为 `manifest.json` 时与清单冲突 | 拒绝后留下无清单的半成品目录；不符合“独占创建”的规则 | 先解析全部 payload 再建目录；PNG 独占创建；`manifest.json` 作为保留名 |
| 状态表把 5.0–5.3 和 versioned 包标为“已完成且验证”，但只有本工具自带构建器的合成包（同一理解写、同一理解读） | 过度声明 | 拆成“5.4–5.6 已验证（真实样本）”与“5.0–5.3 / versioned 仅合成候选”；补充 4.26 无真实样本、UE5 double 包围盒不支持、cell 表只读偏移、`--verify` 只证明自洽等说明 |

测试夹具同时扩展：`build_ue5_package` 可写版本信息（`versioning=`）、空 Verse cell 表（`ue5.6+`）和 bulk data map（`bulk_data_map=`），
并按 retoc 的写法在 name batch / bulk map 与 imported public export hashes 之间不再插入对齐填充。
