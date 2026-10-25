# 06 球员外观数据格式：14 项身体参数与脸/发绑定字段

> 范围：PES 2015–2021 的 EDIT 存档外观记录和数据库表 `PlayerAppearance.bin`；eFootball（2022+）的 `PlayerAppearance.bin`；身体参数在引擎里怎样改变模型；可公开查到的球员数值；给本项目的布局 schema 提案。
> 编写日期：2026-10-25。研究基准：eFootball iOS 11.1.1（build 11.1.101）。
> 来源分级：[T1] 官方/一手；[T2] 主流媒体/参考；[T3] 维基、社区、改档论坛、社区开源代码；[T4] 本组估计或推断。本文所有格式结论都来自社区逆向（T3），**没有找到 Konami 官方格式文档（T1）**。
> 年代说明：本文研究的是**数据格式**，与 2002 年无关；涉及球员数值时逐条标注游戏版本和球员版本年份。

---

## 0. 结论速览

| # | 问题 | 结论 | 置信度 / 来源 |
|---|---|---|---|
| 1 | PES 2016–2021 存档的外观记录 | 72 字节（0x48）。14 项身体参数是 14 个连续的 4 bit 字段，放在记录偏移 **0x0C–0x12**，低半字节在前。存储值 0–14，界面显示 = 存储值 − 7（−7…+7），中性值 7。PES 2015 为 68 字节（0x44），身体字段位置相同。 | 高。T3：4ccEditor 源码、4cc-studio 实测、4cc/Rigged 维基、GodotFootball 运行时读数，四方一致 [A1][A2][A3] |
| 2 | PES 数据库表 `PlayerAppearance.bin` | PES 2016/2017–2021 每行 60 字节：u32 球员 ID + 56 字节。56 字节等于存档外观块的 0x0C–0x43，所以**库表偏移 = 存档偏移 − 8**，身体字段在 0x04–0x0A。 | 高。T3 [A2][A3][A7] |
| 3 | "64 字节、14 项压缩身体字段"对应哪个版本 | 与 **eFootball PC 版的 `PlayerAppearance.bin`** 吻合：每行 64 字节，开头 8 字节是 u64 外部 PID，后面 56 字节与 PES 2016–2021 的 56 字节逐位对应，所以 **eF 偏移 = 存档偏移 − 4 = PES 库表偏移 + 4**，身体字段在 **0x08–0x0E**【未核实：64 B 行长和 u64 PID 有两个独立来源；但"身体字段在 0x08–0x0E"只来自 EvoTool 的注释和写入代码，以及它与 PES 布局的类比。EvoTool 自己的读取代码实际从 0x04 开始读，也没有任何真实 eF 文件的解码结果可以对照】。PES 系列没有任何一版是 64 字节。 | 中。T3：EvoTool（"eFootball v5.2.0"）[A4] 与 Mod Studio 文档（"eFootball 2027 Steam"）[A6]；两者都没有覆盖 iOS |
| 4 | 身体参数怎样改变模型 | PES 2021：只做**骨骼缩放**，没有 morph 或 blend shape。存储值 n 解码为 v = (n − 7)/7，n = 15 当 0 处理。每项参数作用于固定骨骼的固定轴，例如 ThighSize +1 档约等于大腿前后 +1.43%；身高决定整体缩放 G；体重先按身高归一化，再加粗躯干和四肢。eFootball 的实现**未找到**。 | PES 2021：中高。T3 引擎逆向，含运行时实测 [A3]。eFootball：未知 |
| 5 | R9 的官方体型参数 | **未找到**。pesdb 只在 PES 2017 收录了 "RONALDO"（id 2137，年龄 20，即 1996/97 年的版本，183 cm / 75 kg），没有体型参数；PES 2018–2021 和 eFootball 的 pesdb 中都没有 R9。 | T3 [W4]；[02_body §6.1] |
| 6 | eFootball 还用这些参数吗 | PC 版数据表仍有 `PlayerAppearance.bin`，社区编辑器可以读写其中约 107 个外观参数。编辑器说明里写明这些参数与"授权的 3D 脸模和头部资产"是分开的。官方没有开放外观编辑模式。iOS 版是否带同一张表、格式是否相同：**未找到**。 | T3 [W5][W6][W8][A6] |
| 7 | "FaceNumber" 字段 | 在 PES 2016–2021 的外观块和 eFootball PC 的 64 字节行中，**没有**脸部资产编号字段：56 字节的每一位都已对应到身体、穿着、编辑脸参数、发型或"实测未用"。PES 2021 选用真人脸的方式是"`face/real/<球员ID>/` 下的资源包是否存在"，不靠表内字段。eFootball 里的 FaceNumber 在什么位置：**未找到**。 | T3 [A2][A3]；对 eFootball 的推断为 T4 |

**最重要的警告**：本文的偏移都是**解码后表数据里的位置**（PC 版需先做 WESYS 解密和 zlib 解压；PES 存档要先解密成 data.dat）。它们**不是** IPA、CPK、`.utoc/.ucas`、加密 `.bin` 里的文件偏移，也**不是**内存结构中的位置。PES 2021 的内存体型对象是 16 个 float（另带 1 个 u8），顺序也与文件不同（两项手臂参数对调）。详见 §1、§8。

---

## 1. 层级与位编号约定（必须先读）

### 1.1 从 IPA 到一个半字节

| 层 | 内容 | 本文的偏移是否适用 |
|---|---|---|
| ① IPA / 安装目录 | 资源容器（iOS：UE IoStore `.utoc/.ucas/.pak`，是否另有 CPK **未知**；PC 是 UE 包与 CPK 并存，见 `05_native_formats.md` §0） | 否 |
| ② 容器内的文件 | 例如 PC 版 CPK 里的 `PlayerAppearance.bin` | 否 |
| ③ WESYS 外壳 | 16 字节头 + 数据。v5.0.0 起，PC 版数据先异或流加密，再 zlib 压缩（核查更正：顺序反了。Mod Studio 的实现 `ui/core/wesys.py` L82-88 是**先 `zlib.compress`，再用压缩后长度和原始长度做种子生成密钥流异或加密**；读取时先解密、再解压。文档 §1 那句 "encrypted *before* zlib compression" 容易误读，同一节又写 "a writer must compress first, read the final size, and only then generate the keystream"）；种子取自头里的两个大小字段 [A6 §1]。加密从哪个游戏版本开始也有冲突：Mod Studio 写 "Since game version 5.0.0"，RBsGameLab 写 "v6.0.0 also wrapped the database files in an encrypted WESYS container" [W5]（核查补充，两者都是 T3，没有找到 Konami 的说明） | 否 |
| ④ **解码后的表数据** | N 条定长记录，第 i 条从 i × 64 开始（eF PC） | **是。本文 eFootball 偏移指这一层** |
| ⑤ 记录内字节 + 位 | 例如 NeckLength = 第 0x08 字节的 bit 0–3 | 是 |
| ⑥ 运行时内存对象 | 例如 PES 2021 的体型对象：float[16] + u8，0x44 字节 [A3 physique §0] | **否。顺序、类型、长度都不同** |

- PES 存档：`EDIT00000000` 是加密文件，偏移是相对 pesXdecrypter 导出的 `data.dat` 而言的 [A7 README][A2 维基原文]。
- 改动 ④ 之后要依次：重新压缩 → 用**新的**压缩长度重算密钥种子并加密 → 写回容器。Mod Studio 明确说"不能原地重新加密"，因为压缩后的长度一变，种子就变 [A6 L125-127]。
- iOS 版的数据表放在哪个容器、用什么外壳和密钥：**未找到**（见 §6.4 与 `05_native_formats.md` §5.2）。

### 1.2 位编号

所有社区实现都用同一种约定：**字节内从低位数起，跨字节时按小端拼接**。

```
value(B, b, w) = (int.from_bytes(payload[B:B+4], 'little') >> b) & ((1 << w) - 1)
```

- 4ccEditor 的 `read_data(start_bit, bits, current_byte)` 就是这样逐位读取的（`data_util.cpp` L6-30 [A1]）。
- EvoTool 先按小端读出 u32 字，再用 `ExtractBits(word, start, count)` 取位，效果完全相同 [A4 L484-488]。
- 所以"第 0x0F 字节 bit 4"与"0x0C 处 u32 字的 bit 28"是同一个位置。§7 的 schema 两种写法都给出。

---

## 2. PES 2015–2021：EDIT 存档中的外观记录

### 2.1 各版本记录大小与位置（[T3]）

| 版本 | 外观记录 | 大小 | 在 data.dat 中的位置 | 来源 |
|---|---|---|---|---|
| PES 2015 | 独立区块 | 0x44 = 68 B（含 4 B 球员 ID） | 外观区从 0x2AB9CC 开始；球员区从 0x4C 开始，每条 0x70 | 4ccEditor `pes15.cpp` L3-14（"Advance by length of 1 Appearance Entry"）、`main.cpp` L1697；4cc-studio `pes15.rs` L377-378（`APPEARANCE size: 68`）[A1][A2] |
| PES 2016 | 独立区块 | 0x48 = 72 B | 外观区从 0x2AB9CC 开始；球员区从 0x4C 开始 | `main.cpp` L1771-1781；`pes16.rs` L324-325（`size: 72`）[A1][A2] |
| PES 2017–2019 | 嵌在球员记录**内部** | 球员记录 188 B（0xBC），外观块在 +0x74（bit 928），长 72 B | 球员区从 0x78（17）或 0x7C（18、19）开始 | `pes17.rs`/`pes18.rs`/`pes19.rs` L16-17 与 L290（`AppearanceId bit_offset: 928`）；`main.cpp` L1830、L1886、L1948 [A1][A2] |
| PES 2020–2021 | 嵌在球员记录内部 | 球员记录 312 B（0x138），外观块在 **+0xF0**，长 72 B | 球员区从 0x7C 开始 | `pes20.rs` L16-17（`size: 312`）、L315（`AppearanceId bit_offset: 1920` = 0xF0 × 8）；4ccEditor `pes20.cpp` L733（`current_byte += 0xF0+0x48`）[A1][A2]；GodotFootball："records from data.dat+0x7C, stride 0x138" [A3 physique L65] |

- PES 2020 与 2021 共用同一套球员记录解析（4ccEditor `fill_player_entry20` 同时服务两版；4cc-studio 注释 "The PES 20 and 21 field tables"）[A1][A2]。
- 维基原文："A player appearance entry always follows the player data entry"，"Entry Length: 72 (0x48) bytes"（PES 2017 节 L3239-3243；PES 2021 节 L10904-10905）[A2 `resources/Editfile.wikitext`]。

### 2.2 14 项身体参数（PES 2015–2021 存档，偏移相对外观块起点）

| # | 字段（维基 / 4ccEditor 名） | 字节:位 | 宽 | 存储 | 界面 | 中性 | 来源行 |
|---|---|---|---|---|---|---|---|
| 1 | Neck Length / `neck_len` | 0x0C:0 | 4 | 0–14 | −7…+7 | 7 | `pes20.cpp` L157；维基 L10904 起的表；`pes20.rs` L355-358（bit 2016 = 0xF0×8 + 0x0C×8） |
| 2 | Neck Size / `neck_size` | 0x0C:4 | 4 | 0–14 | −7…+7 | 7 | `pes20.cpp` L158 |
| 3 | Shoulder Height / `shldr_hi` | 0x0D:0 | 4 | 0–14 | −7…+7 | 7 | L159 |
| 4 | Shoulder Width / `shldr_wid` | 0x0D:4 | 4 | 0–14 | −7…+7 | 7 | L160 |
| 5 | Chest Measurement / `chest` | 0x0E:0 | 4 | 0–14 | −7…+7 | 7 | L161 |
| 6 | Waist Size / `waist` | 0x0E:4 | 4 | 0–14 | −7…+7 | 7 | L162 |
| 7 | Arm Size / `arm_size` | 0x0F:0 | 4 | 0–14 | −7…+7 | 7 | L163 |
| 8 | Arm Length / `arm_len` | 0x0F:4 | 4 | 0–14 | −7…+7 | 7 | L164 |
| 9 | Thigh Size / `thigh` | 0x10:0 | 4 | 0–14 | −7…+7 | 7 | L165 |
| 10 | Calf Size / `calf` | 0x10:4 | 4 | 0–14 | −7…+7 | 7 | L166 |
| 11 | Leg Length / `leg_len` | 0x11:0 | 4 | 0–14 | −7…+7 | 7 | L167 |
| 12 | Head Length / `head_len` | 0x11:4 | 4 | 0–14 | −7…+7 | 7 | L168 |
| 13 | Head Width / `head_wid` | 0x12:0 | 4 | 0–14 | −7…+7 | 7 | L169 |
| 14 | Head Depth / `head_dep` | 0x12:4 | 4 | 0–14 | −7…+7 | 7 | `pes20.cpp` L170；`pes20.rs` L420-423（bit 2068） |

存储与显示的换算有三处独立证据：
- 维基每一行都写着 "In-game value plus seven"，"PES: must be in range [0, 14]"，默认值 7 [A2 Editfile.wikitext L10904-11887]。
- 4ccEditor 显示时减 7（`main.cpp` L2626-2654，如 `_itow_s(gplayers[p_ind].neck_len - 7, …)`），保存时加 7（L3285-3321，`output.neck_len = _wtoi(buffer) + 7`）[A1]。
- 4cc-studio："Physique keys convert between the game number and the stored value (+7)"（`model.md` L170）[A2]。

两个容易忽略的细节：
- **PES 2021 的引擎解码器**：`v = 2n/14 − 1`，**n = 15 时取 0**（GodotFootball `FUN_141ad98b0`，physique L83-85）。所以 15 不会让游戏崩溃，但也不是合法值。写入时应限制在 0–14。
- **两项手臂参数在内存里对调**：存储顺序是 ArmSize 在低半字节、ArmLength 在高半字节；PES 2021 内存对象里 `+0x20` 是臂长、`+0x24` 是臂围，解码器读 `+0x20 = byte 0x0F >> 4`。GodotFootball 用运行时数据验证过，并否定了"4ccEditor 标签与引擎相反"的说法 [A3 physique L105, L293]。**这正是"内存位置 ≠ 文件位置"的实例。**

另外，"Edited Physique" 标志（0x04 bit 2）在 PES 2021 解码器中**从不检查**，半字节总是生效。未编辑的球员靠"全部为 7"才等同于不变（GodotFootball：4830 名球员中 4600 多名 14 项全为 7）[A3 physique L108-111]。

### 2.3 外观块完整字段（PES 2016–2021，72 字节）

以 PES 2021 维基表为骨架（[A2] `Editfile.wikitext` L10904-11887，标题 "Player appearance entry"），宽度以 4cc-studio 2026-10-13 的游戏内实测为准（`docs/spec/pes_savefile/model.md` L274-400，`schema/ingame_face.rs`）[A2]。

| 区段 | 字节 | 字段 | 说明 / 冲突 |
|---|---|---|---|
| 头 | 0x00 | 球员 ID（u32） | |
| | 0x04:0–3 | Edited Face / Hairstyle / Physique / Strip Style 标志 | 每项 1 bit |
| | 0x04:4 | Boots ID，14 bit | |
| | 0x06:2 | 门将手套 ID | **冲突**：维基写 10 bit，后面 0x07:4 有 4 bit "Unknown A"；4ccEditor 读 14 bit，注释 "Extend to be 14 bits, using Unk A space"（`pes20.cpp` L155）（复核补充：4ccEditor 写入也是 14 bit（L885），4cc-studio `pes20.rs` `GlovesId bit_offset 1970, bit_width 14`；§7 schema 已按"宽度取 4cc-studio 实测值"的规则改为 14 bit，见核查记录 K28） |
| | 0x08 | Base Copy Player（u32） | 维基："references itself if the player is defaulted" |
| 身体 | 0x0C–0x12 | 14 项身体参数 | 见 §2.2 |
| 穿着 | 0x13 | 护腕颜色左（3 bit）、右（3 bit）、护腕位置（2 bit） | |
| | 0x14 | 眼镜框颜色（3 bit）、眼镜样式（3 bit）、袖长（2 bit） | 维基："Sleeves 00 Season specific, 01 Short, 10 Long" |
| | 0x15 | 长袖内衣、袜长、内裤各 2 bit；衬衫下摆、脚踝胶带各 1 bit | |
| 编辑脸区段（"ingame-face run"，从第 22 字节到末尾，50 字节） | 0x16 | 球员手套（1）、手套颜色（3）、Jawline（4） | |
| | 0x17–0x2C | 约 40 个 4 bit 面部滑杆（下颌、颧骨、眉、眼、眼睑、鼻、口、下巴） | 存储 = 显示 + 7。其中 7 个只有 PES 2015 渲染；PES 16–21 仍会写入（维基里称 "duplicate"）。另有 3 个 4 bit 实测未用（写 7） |
| | **0x2D:0** | **Skin colour，3 bit** | **冲突**：维基写 0 = 白/定制模型透明，1–6 = 肤色档，7 = 无/透明；GodotFootball 写 "skin colour 1–6 (0/7 = special)"；4ccEditor 对 PES 18+ 把 7 改成 0（"No custom skin in 18+"，`pes20.cpp` L189-193），下拉框 0 号标为 "White/Def" |
| | 0x2D:3 | 颊型（5 bit） | |
| | 0x2E | 额头类型（3）、胡须类型（5） | |
| | 0x2F–0x30 | 笑纹（3）、上眼睑类型、下眼睑类型、耳长 | **冲突**：上眼睑类型维基 3 bit、4cc-studio 5 bit；下眼睑类型 3 bit 与 4 bit |
| | 0x31 | 耳宽、耳角 | |
| | 0x32 | 眉型、领口类型 | **冲突**：眉型 3 bit 与 5 bit；领口 2 bit 与 3 bit |
| | 0x33 | 脸大小、脸高 | |
| | 0x34 | 鼻型 | **冲突**：维基 3 bit，另有 5 bit "Unknown N"；4cc-studio 实测 6 bit，0x34:6 的 2 bit 实测未用（写 1） |
| | 0x35:0 / 0x35:3 | 上唇类型、下唇类型（各 3 bit） | |
| | 0x35:6 / 0x36:4 / 0x37:2 | 头发颜色 R/G/B（各 6 bit，0–63） | |
| | **0x38:0** | **发型**（维基 3 bit：1 普通、2 干发、3 莫西干、4 爆炸头、5 脏辫、6 编发、7 特殊；4cc-studio 4 bit） | |
| | **0x38:4** | **侧后修剪样式**（维基 3 bit，范围 1–6；4cc-studio 4 bit） | 4cc-studio：PES 21 在该字段高位置 1 时会崩溃 |
| | 0x39–0x3B | 眉毛颜色 RGB（各 6 bit）+ 眉毛样式、浓度、粗细（各 2 bit） | |
| | **0x3C:0** | **发长**（3 bit：1 剃光、2 很短、3 短、4 中、5 长） | 维基原文 "1=Shaven, 2=Very Short, 3=Short, 4=Medium, 5=Long" |
| | 0x3C:3 | 卷曲度（存储 = 显示 + 1） | |
| | 0x3D | 发饰（1）、发饰颜色（3）、胡须颜色 R 起始 | |
| | 0x3E–0x3F | 胡须颜色 G/B、胡须浓度 | |
| | **0x40:0** | **虹膜颜色（4 bit）** | 维基范围 0–10 |
| | **0x40:4** | **发型变体（Hair Variation）** | 维基 5 bit，范围 1–29；4cc-studio 8 bit |
| | 0x41:4 | 头发颜色预设（4 bit） | 4cc-studio：0 = 自定义 RGB；1–8 映射到内部 id |
| | **0x42** | **前额发型**（3 bit；维基 "01=Up, 10=Down, 11=Combed Back"）、分缝（3）、侧后发型（3；维基 "001=Normal, 010=Reduce Body, 011=Shorter Sides, 100=Cropped"） | **冲突**（核查更正：维基给前额发型的宽度是 **2 bit**，0x42:2 另有 1 bit "Unknown S"；3 bit 是 4cc-studio `ingame_face.rs` 的实测宽度（`HairFrontStyle bit_offset 352, bit_width 3`）；EvoTool 读 `fontStyle` 时也只取 2 bit） |
| | **0x43:1 / 0x43:4** | **发际线类型**、**前额宽度** | 维基各 2 bit；4cc-studio 为 3 bit 和 4 bit |
| | 0x44–0x47 | PES 16–21 实测未用（写 0） | **不在**库表行和 eFootball 行中 |

**与本项目的关系（T4）**：2002 年 R9 的"额前半圆形短发 + 其余剃得很短"（见 `01_appearance_2002.md`）在 PES 体系里只能用"发长 + 侧后修剪 + 前额发型 + 发际线 + 发型变体"的组合**近似**，没有对应的现成参数。真人脸和发型走独立资产（§2.4、§6.3）。任务书要求的标志性发型应由 3D 资产实现，不能指望这些字段。

### 2.4 PES 2021 怎样把一名球员绑定到"真人脸"或"编辑脸"（[T3] 引擎逆向）

GodotFootball 对 PES2021.exe 的静态分析加运行时只读观察 [A3 face]：
- **真人脸是否存在 = 资源包是否存在**：`FUN_141ec53d0(id, isReferee)` 拼出 `<real dir>/%d/face.fpk`（id 先与 `0x800bffff` 按位与），再调用文件存在检查（face L63）。
- **表里没有"真人脸"位**："Killed hypothesis: a dedicated 'real face' bit in PlayerAppearance.bin. No field separates the 1,985 real-face ids from the 15,797 others"（face L68）。
- **真人脸**：加载包内的 `face_high.fmdl`、`hair_high.fmdl`、`face_diff.bin`（960 字节，33 根面部骨骼偏移 + 眼球缩放 + 眼睑角度 + 下颌偏移），以及可选的 `oral.fmdl` 和 `arm_bsm.ftex`（face L70-82、L99-126）。
- **编辑脸**：用通用 `facial.fmdl`（1600 顶点）+ montage 形状补丁 + 30 骨晶格变形，加编辑发型 `face/edithair/model/<style>/#Win/face.fpk`（共 896 种），参数取自外观记录（face L146-186、L310-393）。
- **肤色**：外观记录的肤色 c ∈ 1–6 选择 `FaceSKIN<c>..`、`arm_<c>_bsm`、`thigh_<c>_bsm`、`head_<c>_srm`；真人脸自带脸部肤色贴图，手臂仍用 `arm_<c>_bsm`，除非包内自带 `arm_bsm`（face L384-391）。

对本项目的启示（T4）：
- PES 2021 中**按球员 ID 找脸**。任务书第八节"不得全局覆盖共享脸部资源槽"的风险，在 PES 体系里对应"多名球员指向同一个脸包"。
- eFootball 是否保留"按 ID 找资产路径"的做法：**未找到证据**（与 `05_native_formats.md` §5.2 一致）。

### 2.5 PES 数据库表 `PlayerAppearance.bin`（60 字节行）

- GodotFootball："`PlayerAppearance.bin` (`common/character0/model/character/appearance/`) has 60-byte records: `u32 playerId` + the 56-byte body. The body is identical to bytes 0x0C–0x43 of the EDIT save's 72-byte appearance entry, so **PA offset = EDIT offset − 8**"（face L312）。原版 `dt00_x64.cpk` 中有 17,782 条记录 [A3]。
- 4cc-studio：`appearance_row(player_id, version) -> [u8; 60]`，"the player id, then the 56 appearance bytes … (record bytes 128 to 184 on PES 18/19, 252 to 308 on PES 20/21)"；"The run's last 4 bytes (the block's last 4) are in no row"（`model.md` L228-238）[A2]。
- pes-db-generator 的模板 `PlayerAppearance_Base_16.bin` 正好 60 字节：ID 70101（`d5 11 01 00`），后面 7 字节全是 `0x77`，即 14 项身体参数全为中性 7。生成器对 PES 16–21 都用这个模板（`player_appearance.py` L10-25）[A7]。（核查补充：同一仓库的 PES 2015 模板 `PlayerAppearance_Base_15.bin` 也是 60 字节，开头同样是 `d5 11 01 00` 加 7 个 `0x77`，但后面的字节与 16 版不同。15 版的 56 字节体与 16–21 是否逐位对应，没有核对。）
- 文件位置：GodotFootball 写在 `common/character0/model/character/appearance/` [A3]；Devil Cold52 的 eFootball 编辑器说明把它和 `Player.bin`、`Team.bin` 放在同一个 pesdb 目录（见 `05_native_formats.md` §5.1）[W6]。两种说法针对的游戏版本不同，**以实际文件为准**。

---

## 3. "64 字节、14 项压缩身体字段"对应哪个布局

### 3.1 候选布局对照

| 布局 | 游戏 / 版本 | 记录长度 | ID 字段 | 身体字段位置 | 是否 64 B + 14 个压缩字段 |
|---|---|---|---|---|---|
| EDIT 外观块 | PES 2015 | 68 B | u32 @0 | 0x0C–0x12 | 否（68 B） |
| EDIT 外观块 | PES 2016–2021 | 72 B | u32 @0 | 0x0C–0x12 | 否（72 B） |
| 数据库表行 | PES 2016/2017–2021 | 60 B | u32 @0 | 0x04–0x0A | 否（60 B） |
| PES 2021 内存体型对象 | PES 2021 运行时 | 0x44 = 68 B | 无 | float，从 +0x08 起，不压缩 | 否 |
| **eFootball `PlayerAppearance.bin` 行** | **eFootball PC，EvoTool 标为 v5.2.0（2025-12）；Mod Studio 标为 eFootball 2027 Steam dt200** | **64 B** | **u64 @0** | **0x08–0x0E** | **是** |
| Football-Championship 的读取器 | 同一作者早期的 Unity 项目 | 代码写死 60 B | u32 | 0x04 | 否；仓库里的样本文件是空表（16 字节 WESYS 头），无法验证 |

结论（T3 + T4）：任务书说的"亨利研究识别的 64 字节记录、14 项压缩身体字段"，与 **eFootball PC 版 `PlayerAppearance.bin` 的 64 字节行**最吻合。它沿用了 PES 2016–2021 数据库表的 56 字节，只把 ID 从 u32 加宽成 u64。eFootball 的 `Player.bin`、`PlayerAssignment.bin` 同样把外部 PID 存成 u64 [A6 §2-3]。**是否与 iOS 11.1.1 一致：未验证。**

### 3.2 eFootball 64 字节行的证据

**(a) Mod Studio 文档**（`docs/PESDB_DATABASE_FORMAT.md`，测试版本 "eFootball 2027, Steam build, dt200"）[A6]：
- "Fixed **64 bytes per record**, keyed by external PID in bytes 0–7, sorted strictly ascending by that PID. Roughly 107 decoded fields covering build, face, hair and kit-fit parameters."（L286-290）
- 识别表格的规则："`PlayerAppearance` | 64 B | PID (bytes 0–7) strictly ascending"。作者说对 pesdb 目录下全部 53 个 `.bin` 测试过，只有预期的三张表匹配（L306-326）。这是在真实文件上做的**结构性检验**，比较可靠。
- "External PIDs are 64-bit"；卡片 ID 带高位前缀（L330-341）。本次计算的例子：Rivaldo Brazil 2002 Epic 卡 `88044145348046 = 0x501360_020DCE`，Hulk Trending 卡 `53878485695166 = 0x310090_003ABE`，都超过 2³²，u32 存不下。
- 文档内部有一处不一致：一面写 "Not every player has a record — the table is sparse relative to Player.bin"，一面又说文件长 2,410,752 字节（= 37,668 条 × 64 B），多于 `Player.bin` 的 23,552 条。原因**未找到**。

**(b) EvoTool v5.2.0**（`Controllers/v520/PlayerAppearanceControllerV520.cs`）[A4]：
- `FILE_NAME = "/PlayerAppearance.bin"`，`BLOCK = 64`（L15-16）。
- 代码注释给出的布局是：0x00 playerId、0x04 cardType（已注释掉）、"// 0x08 - Cuerpo (Bloque de 4 bits cada uno)"，然后 0x0C、0x10 … 0x3C 共 14 个 u32 字，合计正好 64 字节。
- 写入函数先写 `pa.playerId`（C# `ulong`，即 8 字节），再写 `(uint)0`，然后定位到 +0x08 写身体字（L553-577）。
- **内部不一致（T3，需警惕）**：
  - 读取函数是 `ulong playerId = ReadPlayerAppearance.ReadUInt32();`，后面紧跟 `//uint cardType = ...`（L98-99），所以实际从 0x04 开始读身体字，与注释的 0x08 差 4 字节。
  - 同一作者的 `PlayerControllerV520.cs` L112-113 是 `ulong playerId = ReadPlayer.ReadUInt64(); // 0x08`，后面也跟着 `//uint cardType`。由此推断（T4）：作者把"u32 ID + u32 cardType"改成 u64 ID 时，在外观表里漏改了 `ReadUInt32` → `ReadUInt64`。
  - 写入函数用 `Seek(BLOCK + 0x08)`，而不是 `index*BLOCK + 0x08`（L567），也就是说永远写到第 1 条记录；`AddNewEntry` 追加 60 字节（L789）。**写入功能显然没测过，不能当作格式证据，只能参考它的位定义。**
- Football-Championship 是同一作者更早的项目，`BLOCK_SIZE = 60`，并注释掉了 `unknown04`（L16、L51）。60 正是 PES 16–21 库表的行长，推断（T4）作者最初照搬了 PES 的知识。

**(c) 逐字段对齐**：把 EvoTool 每个 u32 字的位定义平移到 PES 的位置（eF 偏移 + 4 = 存档偏移），结果与 4cc-studio 实测的 PES 16–21 布局**全部落在同一位置**：

| eF 字（偏移） | EvoTool 位定义（节选） | 对应 PES 存档字节 | 4cc-studio / 维基字段 |
|---|---|---|---|
| 0x08 | neckLength 0-3 … armLength 28-31 | 0x0C–0x0F | 身体 1–8 ✓ |
| 0x0C | thighSize 0-3 … headDepth 20-23；WristTapeColour 24-26；Unknown2 27-29；wristTaping 30-31 | 0x10–0x13 | 身体 9–14 + 护腕颜色左/右 + 护腕位置 ✓ |
| 0x10 | Unknown3 0-2、Unknown4 3-5、sleeves 6-7 … playerGloves 16、color 17-19、jawline 20-23、jawHeight 24-27、Unknown5 28-31 | 0x14–0x17 | 眼镜颜色/样式、袖长 … 手套、Jawline、Jaw Height、Pes15JawDepth（维基 "Jawline (duplicate)"）✓ |
| 0x14 | cheekbones 0-3、unknown15-18、browWidth 20-23、innerEyebrowHeight 24-27、Unknown6 28-31 | 0x18–0x1B | Cheekbones、CheekboneHeight、Pes15CheekboneDepth、两个实测未用、BrowWidth、InnerEyebrowHeight、EyebrowDepth ✓ |
| 0x28 | chinHeight 0-3、chinDepth 4-7、**skinColor 8-10**、cheekType 11-15、forehead 16-18、facialHairType 19-23 … | 0x2C–0x2F | ChinHeight、ChinDepth、**SkinColor @0x2D:0**、CheekType @0x2D:3 … ✓ |
| 0x34 | **overallStyle 0-2**、**sideBackCropped 4-6**、eyebrowColorRed 8-13 … | 0x38–0x3B | **HairStyle @0x38:0**、**HairCroppedStyle @0x38:4**、眉毛颜色 R … ✓ |
| 0x38 | **overallLength 0-2**、overallWaveLevel 3-5、hairAccessories 8 … | 0x3C–0x3F | **HairLength**、WaveLevel、HairAccessory ✓ |
| 0x3C | **irisColor 0-3**、**overallHairVariation 4-8**、unknown19 12-15、**fontStyle 16-17**、**fontParted 19-21**、**sideBackStyle 22-24**、**fontHairLine 25-26**、**fontForeheadWidth 28-29** | 0x40–0x43 | IrisColor、HairVariation、HairColorPreset、前额发型、分缝、侧后发型、发际线、前额宽度 ✓ |

- 宽度上的差异只在类型字段：EvoTool 多用 3 bit，4cc-studio 实测 5、6 或 8 bit。EvoTool 标为 Unknown 的位，正好是这些字段的高位。
- 这种逐位吻合有两种解释：eFootball 确实沿用了 PES 布局；或者 EvoTool 作者照 PES 布局写了代码。**仅凭代码无法区分。** 所以 eF 字段位置定为"中"置信度，用 §8 的测试向量在真实文件上确认。

### 3.3 结论与置信度

| 项 | 结论 | 置信度 |
|---|---|---|
| eF PC 行长 64 B、u64 PID 在 0x00 | Mod Studio 在真实文件上做过结构检验，EvoTool 的 BLOCK 与注释一致 | 中高（T3） |
| 14 项身体参数在 eF 行 0x08–0x0E，低半字节在前，顺序同 PES | EvoTool + PES 体系一致性 | 中（T3 + T4） |
| 存储 0–14、中性 7 | eFootball 网站 pesdb 显示的 11 项体型值都在 0–14，常见值为 7（见 `02_body_2002.md` §6.1）；Devil Cold52 编辑器说明 "values from 7 to 14 are positive and 0 to 7 is negative"（据 WebFetch 对页面的摘要，未逐字核对）[W6]（核查更正：已逐字核对。原文是页面 "Notes" 第 5 条："Appearance Editor: For excel and CSV Value between 7 and 14 is positive, the Value between 0 and 7 is negative And 7 is neutral."） | 中高（T3） |
| iOS 11.1.1 的表与以上相同 | 无公开证据 | **未验证** |

---

## 4. 身体参数在引擎中的视觉效果

### 4.1 PES 2021 的机制（[T3] GodotFootball 引擎逆向，含运行时实测）

来源：`docs/re/pes-re-physique.md`（2026-10-13 修订）[A3]。方法：对内存镜像做静态反编译，并只读地读取运行中的 `PES2021.exe`。文中标 OBS 的是实测，标 INF 的是推断。

1. **只有骨骼缩放**："Mechanism = per-bone scale, nothing else. There are no blend shapes and no morph targets for body physique."（L35）
2. **一个 0x44 字节的体型对象驱动全部效果**：`+0x00` 身高（cm），`+0x04` 归一化体重，`+0x08..+0x3C` 14 个滑杆（−1..1），`+0x40` 禁用标志（L38-42）。这是**内存布局**，不是文件布局。
3. **身高 → 整体缩放 G**：`G = (1 + k_L·L)·(1 + k_h·g(h))`。L 是腿长滑杆，k_L = 0.018（L ≥ 0）或 0.019；k_h = 0.223（g ≥ 0）或 0.277；h ≥ 180 时 g = (h − 180)/40。运行时 24 名角色与公式吻合到 1e-7（L43-47）。
4. **体重**先按身高查表归一化到 −1..1，用于加粗或变细躯干和四肢，**不改变 G 和动画骨长**（L57、L114-129）。
5. **每项滑杆按三张写死在 exe 里的表**（A：绝对，含 G；B：相对；C：辅助）缩放指定骨骼的指定轴（L170-198，附录 A L306-360）。
6. **动画**：骨长按父骨缩放；根运动乘以 `clamp(G, 0.9166, 1.0833)`；播放速度与体型无关（L202-218）。
7. **约束骨**：`dsk_*` 辅助骨（三角肌、肘、胸肌等）每帧由约束重新计算，表值只是初值（L242-250）。

### 4.2 每档效果（PES 2021；"每档" = 存储值 ±1 = 显示值 ±1）

计算方法：缩放 R = 1 + |v|·(X − 1)，v = (n − 7)/7，X 取表中负端或正端向量（a、b 系数为 1 的条目）。所以每档变化 = (X − 1)/7。下表的表值来自 [A3] 附录 A（T3），**每档百分比是本组按其公式算出的（T4）**，只适用于 PES 2021。

| 参数 | 主要骨骼 · 轴（x = 沿骨长度，y = 前后，z = 左右） | −1 档 | +1 档 | n = 0 时 | n = 14 时 | 附带效果 |
|---|---|---|---|---|---|---|
| NeckLength | `sk_neck` x | −2.86% | +1.43% | 0.80 | 1.10 | — |
| NeckSize | `sk_neck` y / z | −1.43% / −1.43% | +0.36% / +1.43% | 0.90 | 1.025 / 1.10 | 锁骨区 `dsk_collar` y 最大 1.02 |
| ShoulderHeight | `sk_shoulder` z | −2.86% | **+7.86%** | 0.80 | 1.55 | 锁骨绕 Y 轴转 −8·v°（左）/ +8·v°（右）；三角肌 z 0.4…1.2；斜方肌反向 1.2…0.9 |
| ShoulderWidth | `sk_shoulder` x | −1.43% | +2.86% | 0.90 | 1.20 | 胸 z 0.975…1.025；锁骨 z 0.95…1.10 |
| ChestMeasurement | `sk_chest` y | −1.14% | +1.43% | 0.92 | 1.10 | 颈 y 最大 1.10；锁骨区 y 最大 1.15 |
| WaistSize（只在 B 表生效） | `sk_belly` y / z | −1.43% / −1.86% | +1.00% / +1.57% | 0.90 / 0.87 | 1.07 / 1.11 | `dsk_hip` z 0.96…1.05；短裤 y/z |
| ArmSize | 上臂、前臂 y,z | −1.14% | +1.14% | 0.92 | 1.08 | 袖口 0.95…1.10 |
| ArmLength | 上臂、前臂 x | −0.71% | +0.71% | 0.95 | 1.05 | — |
| ThighSize | `sk_thigh` y / z | −1.43% / −0.71% | +1.43% / +1.07% | 0.90 / 0.95 | 1.10 / 1.075 | `dsk_hip` y 1.10、z 1.095（只在正向）；裤脚、短裤 |
| CalfSize | `sk_leg` y,z | −1.43% | **+2.86%** | 0.90 | 1.20 | 膝部辅助骨反向 1.1…0.8 |
| LegLength | 见下 | — | — | — | — | 见下 |
| HeadLength | `sk_head` x | −1.43% | +1.43% | 0.90 | 1.10 | — |
| HeadWidth | `sk_head` z | −1.43% | +1.43% | 0.90 | 1.10 | — |
| HeadDepth | `sk_head` y | −1.43% | +1.43% | 0.90 | 1.10 | — |

LegLength 的效果单独说明。依据是 A 表第 12 项（a = 0.981，b = 1.018）和 G 公式；分解方式是 T4：
- **+7（n = 14）**：整体 G × 1.018，髋部高度随之 +1.8%。A 表中头、颈、胸、手臂约 × 1.000，即**上身基本不变**；大腿和小腿长度约 × 1.020；`sk_belly` 长度约 × 0.882，**腰腹段变短**。
- **−7（n = 0）**：G × 0.981，腿约 × 0.979，腰腹段约 × 1.124。
- 换算到 183 cm：髋高约 96 cm，+2% 约 1.9 cm，即每档约 0.27 cm（T4）。
- **对照**：pesdb 给 eFootball 用的派生指标是 "Leg-length Equivalent Height = Height + (LegLength − 7) cm"，即每档 1 cm（见 `02_body_2002.md` §6.1）。那是 pesdb 自己的模型，**与 PES 2021 引擎的数字不同，两者都不能代表 eFootball 的实际效果**。

几点非线性要注意：
- 正负两端不对称。例如 ShoulderHeight 正端 +55%、负端 −20%；CalfSize 正端 +20%、负端 −10%。
- WaistSize 在 A 表里没有任何效果（`sk_thigh (1,1,1) → (1,1,1)`），只通过 B 表作用于渲染骨骼。
- ShoulderHeight 正端的变化量（+7.86%/档）是其他参数的 3–5 倍，**很容易调过头**。

### 4.3 身高、体重与参数的相互作用（PES 2021；表值 T3，183 cm 的数值是本组按公式计算，T4）

**身高 → G**：175 cm 时 0.9711，180 cm 时 1.0000，**183 cm 时 1.0167**，185 cm 时 1.0279，188 cm 时 1.0446。前四个值与 GodotFootball 的运行时样本一致（0.9711、1.0279 等，physique L47）；183 cm 和 188 cm 是按同一公式算的。（核查更正："前四个值"有误。GodotFootball 原文给出的运行时样本只有 175 → 0.9711、185 → 1.0279、194 → 1.0781、199 → 1.1059（§0 第 3 条和 §5.1 表）。180 cm = 1 是公式本身的定义，183 和 188 cm 是本组按公式算的。复算结果：183 cm 时 g = 3/40，G = 1 + 0.223 × 0.075 = 1.016725；188 cm 时 G = 1.0446。）

**体重归一化**：12 行表 {h, Wmin, Wstd, Wmax}，按身高线性插值（physique L117-128）。
- 183 cm 时插值得 Wmin 49.9、**Wstd 73.25**、Wmax 103.4。
- 归一化体重 = (w − 73.25)/(103.4 − 73.25)：

| 体重（kg，183 cm） | 75 | 78 | 80 | 82 | 84 | 86 |
|---|---|---|---|---|---|---|
| 归一化值 | 0.058 | 0.158 | **0.224** | **0.290** | 0.357 | 0.423 |

- 体重的正端效果（A 表第 1 项）：腹部 y/z 最大 1.10；`dsk_hip` 1.05；大腿、小腿 y/z 1.05；肩、上臂、前臂 y/z 1.10。B 表第 17 项另外加粗颈、胸、锁骨、斜方肌、胸肌、肩胛。
- 例：183 cm / 80 kg 时，腹部、肩、臂约 × 1.022，大腿、小腿约 × 1.011。
- **对 R9 的含义（T4）**：在 PES 2021 中，80–82 kg 已经让整体"偏厚"约 2–3%。再叠加 ThighSize、WaistSize 等滑杆时，两者是**相乘**，不是相互替代。

**动画**：
- 骨长只受 A 表影响，涉及身高、腿长、肩宽、颈长、臂长、头部各项。体重和围度不改变骨长 [A3 L215]。
- 根运动缩放限制在 [0.9166, 1.0833]。G 超过约 1.083（约 194 cm 以上）时，姿势缩放超过位移缩放，可能出现轻微滑步（INF）[A3 L209]。（核查更正：GodotFootball 原文写 "Above roughly 193 cm"（physique L211）；按其公式（腿长 = 中性）G = 1.0833 对应 h ≈ 194.9 cm。两种说法都是近似值，阈值另外随腿长参数变化。）183 cm 的 G = 1.0167，**不受此限制影响**。

### 4.4 eFootball 的情况

- 引擎从 Fox 换成了 UE4（CUE4Parse 中 `GAME_eFootball = GAME_UE4_26 + 29`，见 `tools/native/README_iostore.md` §2）。PES 2021 的骨骼缩放表和公式**不能直接套用**，eFootball 的实现**未找到**公开逆向。
- pesdb 和 eFootBase/eFHUB 都给出了"派生体型指标"：腿部覆盖、手臂覆盖、峰值跳高、躯干碰撞宽度、腿长等效身高 [A9 L158-184][W1-W3]。这些站点显然认为身高和体型参数会影响碰撞与触球范围，但公式是站点自己的，**不能当作引擎事实**（T3）。
- pesdb 只显示 11 项（不含头部 3 项）[W1]；EvoTool 的 eF 解析读取全部 14 项 [A4]。推断（T4）：eFootball 数据里仍有头部 3 项，只是网站不显示。

### 4.5 社区经验值（[T3]，不针对 R9）

- PES 2021 "realistic body model settings"（Moiduran2 制作，pesmodding 2023-09-14）原文："Weight between 55 and 65"，"Shoulder width -4"，"Waist thickness between -1 and -3"，"Thigh thickness between +1 and -1"。理由是 "the Konami model is very rough"，并提示效果因球员类型而异 [W9]。
- 这是给"所有球员"调瘦的通用经验，方向与 R9 2002 年需要的"粗大腿、腰不细"相反，**只说明社区普遍认为 PES 默认体型偏壮**，不能直接拿来用。

---

## 5. Ronaldo 与对照球员的公开数值

### 5.1 R9 本人（结论：**未找到**官方体型参数）

| 游戏 | 检索结果 | 年份 / 版本说明 |
|---|---|---|
| PES 2017 | pesdb `/pes2017/players/ronaldo-2137`："RONALDO"，CF，Brazil，FREE，OVR 92，**Height 183 cm，Weight 75 kg，Age 20**，Goal Poacher，右脚。**页面没有体型和外观参数。** | 年龄 20 说明这是 **1996/97 年**的版本，不是 2002 年。T3 [W4] |
| PES 2016、2018、2019、2020、2021 | 同一 id 2137 在 pesdb 上返回 404；PES 2018、2019 按名字搜索 "Ronaldo" 也没有 R9 | T3 [W4] |
| eFootball（2022+） | pesdb 中检索 Ronaldo、Nazario、Fenomeno 都没有 R9 | 见 `02_body_2002.md` §6.1 |
| 社区 option file 或 edit 数值 | 本次检索没有找到任何公开的 R9 体型或脸部编辑数值（PES 2019–2021） | 网络搜索额度已用完，未能继续 |

**怎样补上**：
- 若用户有 PES 2017 PC 版，可以从数据 CPK 中取出 `common/character0/model/character/appearance/PlayerAppearance.bin`，查 PID 2137 的 60 字节行，按 §2.2 解出 14 项。
- 4cc-studio 说 PES 17–21 的原版表格式相同 [A2 model.md L231-236]。
- 得到的是 **Konami 对 1996/97 年 R9** 的建模，不是 2002 年，只能当参考，必须标注年份。

### 5.2 对照球员（eFootball 当前库，pesdb 存储值 0–14；本次 2026-10-25 重新抓取）

完整的 12 人对照表见 `02_body_2002.md` §6.2。下面三人用来做**解码测试向量**（§8），因为他们的值足够有区分度：

| 球员（PID / 卡） | 身高 / 体重 | NL | NS | SH | SW | CM | WS | AS | AL | TS | CS | LL | 用途 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Hulk（基础 PID 15038；Trending 卡 53878485695166 数值相同） | 180 / 85 | 4 | 13 | 5 | 10 | 9 | 9 | 8 | **1** | 9 | 9 | 10 | AL = 1、AS = 8，可以检出两项手臂参数是否对调 [W2] |
| Karim Benzema（基础 PID 16786160） | 185 / 81 | 4 | 10 | 10 | 10 | 7 | 7 | 7 | 7 | **11** | 9 | 7 | 身高体重与 R9 接近 [W1] |
| Rivaldo（Brazil 2002 Epic 卡 88044145348046，基础 PID 134606【未核实：134606 只是该卡 ID 的低 24 位。pesdb 上 8 张 Rivaldo 卡的低 24 位都是 134606，但 `pesdb.net/efootball/players/rivaldo-134606` 返回 404。Mod Studio §7 明确警告，用屏蔽高位的办法推回基础卡，只有 30.7% 正确】） | 186 / 75 | 7 | 7 | 6 | 8 | 5 | **2** | 3 | 7 | 3 | 2 | **14** | 2002 年队友，长腿细腰的反例 [W3] |

- 派生指标举例（pesdb，T3）：Hulk 的 Leg-length Equivalent Height 是 183.00 cm（= 180 + 10 − 7）；Rivaldo 是 193.00 cm（= 186 + 14 − 7），与 `02_body` 发现的公式一致。
- 头部 3 项：pesdb 不显示，**未找到**。

### 5.3 把参考区间换算成 PES 2021 的缩放（T4，仅作直觉参考）

以 `02_body_2002.md` §6.4 的建议区间为例，按 §4.2 的公式算：
- NeckLength 4 → 颈长 × 0.914；NeckLength 6 → × 0.971。
- ThighSize 10 → 大腿前后 × 1.043、左右 × 1.032；ThighSize 12 → × 1.071、× 1.054。
- 叠加 183 cm / 80–82 kg 的体重效果：大腿再 × 1.011–1.015。
- ShoulderHeight 9（区间上限）→ 肩 z × 1.157，锁骨转 2.3°，三角肌 z × 1.057。正端每档 +7.86%，是各项中最陡的，所以 `02_body` 把它标为"低置信"是对的。若 eFootball 也是这种刻度，实机标定应从 7 和 8 开始试。

这些数字**只描述 PES 2021 的骨骼缩放**。eFootball 的效果必须实机标定（任务书第五节）。

---

## 6. eFootball（2022+）的变化

### 6.1 数据表仍在，但格式随版本漂移

| 观察 | 来源 |
|---|---|
| PC 版数据表：`Player.bin`、`PlayerAssignment.bin`、`Team.bin`、`PlayerAppearance.bin` 等；v5.0.0 起套了加密 WESYS 外壳 | [A6 §1]；RBsGameLab 页面摘要：v6.0.0 "wrapped the database files in an encrypted WESYS container"，编辑器可自动解密和重新加密 [W5] |
| `Player.bin` 行长：EvoTool v5.2.0 为 392 B（L16）；Mod Studio 写 dt200 为 400 B、dt870（2026-08-17）为 392 B；RBsGameLab 写 2026-10-09 更新后 "records are now a fixed 400 bytes each"（核查更正：原文是 "eFootball 2027 changed the record layout completely — records are now a fixed 400 bytes each (previously variable-length)"，说的是 eFootball 2027 整体改了布局，并不是 2026-10-09 那次更新。文中写到 2026-10-09 的只有 "the newly relocated ability, skill, playing-style tables, and national-team card slots"，没有说那次更新后的行长。另外，"previously variable-length" 与 EvoTool v5.2.0 的定长 392 B 相互矛盾） | [A4][A6 L26-31, L176-181][W5] |
| `PlayerAppearance.bin` 行长：64 B（EvoTool v5.2.0、Mod Studio eFootball 2027） | [A4][A6] |
| 文件位置有两种说法：GPSL 写在 CPK 内 `common\character0\model\character\appearance\`（与 PES 2021 相同）；Mod Studio 把它列在 `common/etc/pesdb/` 下 | [A8 L113][A6 L3-5, L325] |

**结论（T4）**：Player.bin 在 2025-12 到 2026-10 之间至少改过一次行长，所以 `PlayerAppearance.bin` 在 v6.1.x（App 11.1.x）也**可能**变化。必须用 §8 的结构检验确认，不能照搬 v5.2.0。

### 6.2 编辑模式与社区编辑器的历史

| 时间 | 事件 | 来源 |
|---|---|---|
| 2021-10 | Devil Cold52 的 eFootball 2022 Player Data Editor beta，可改技能、年龄、能力（Player.bin 由 vadim_95 解码） | [W10] |
| 2022–2023 | 该编辑器 V2.5.x 修复过外观字段（"Jawline: Writing problem Fixed it"、"Eyebrown Colour: Writing problem Fixed it"、上眼睑类型、眼高、眼角高）；V2.5.1.4 增加 "Export PlayerAppearance Block"；V5.2 说明 "values from 7 to 14 are positive and 0 to 7 is negative" | [W6]（据页面摘要）（核查更正：已逐字核对页面。前面几条修复说明属实，分别见 V2.5.1.2 和 V2.5.1.3。"7–14 为正" 一句不在 V5.2 的说明里，而在 V5.3 Fix Notes 之后的 "Notes" 第 5 条，原文见 §3.3。核查补充：V2.5.1.4 Fix Notes 还写了 "player ids are now read as uint64"，这是 eFootball 外部 PID 为 u64 的又一个独立 T3 证据，针对的是 Player.bin） |
| 2023-06 | "eFootball 2.6.0 Edit Menu Unlocker"：Konami 在 eFootball 2022 首版里带了一个**不完整、隐藏**的编辑模式，"the functions are not yet implemented"，解锁后 "nearly useless" | [W8] |
| 2024 | eFootball 2024 版编辑器："player appearance feature is still disabled (data will be decoded again)" | [W6] |
| 2025 | eFootball 2025 版 V4.4.0.0 仍然是 "The player appearance feature is still disabled" | [W7] |
| 2026-07 至 10 | EvoTool v5.2.0 加入 PlayerAppearance 解析（2026-07-18）；Mod Studio 文档（2026-08）；RBsGameLab eFootball 2027 Player.bin Editor 宣称 "107 customizable parameters"，支持按 PID 整块克隆外观 | [A4][A6][W5] |

推断（T4）：2024–2025 年编辑器的外观功能被禁用，说明 eFootball 2024（v3.x）前后外观数据**变过一次**（格式或加密）。2026 年的 64 字节行是重新解码后的结果。

### 6.3 外观由"脸/发资产 + 参数"驱动吗

- RBsGameLab 原文："Appearance parameter data is distinct from licensed 3D face models and head assets."（T3）[W5]
- 在 PES 2021 中，真人脸靠资源包是否存在来决定；编辑脸用外观参数加通用模型（§2.4）。eFootball 里多数球员是授权真人脸。推断（T4）：其外观参数里的编辑脸滑杆对授权脸**可能不起作用**，只有身体 14 项、肤色、穿着等仍然生效。**未验证。**
- **FaceNumber**：
  - 64 字节行的 56 字节已经全部对应到 PES 字段（§3.2），**没有空位留给脸部资产编号**，除非 eFootball 重用了"实测未用"的位：存档的 0x19:4、0x1A:0、0x29:4，以及 0x34:6 的 2 bit。
  - 所以 FaceNumber（如果存在）更可能在 `Player.bin`、另一张表，或 UE 资产注册表中（T4）。
  - 与 `05_native_formats.md` 一致：**未找到**。
- eFootBase 的接口数据中有一个 `prefix` 字段（例如 `"0501_291"`），eflab 文档称之为 "tiền tố asset Konami"（Konami 资产前缀）。它是否与脸部资产有关：**未找到**（T3 [A9 L184]）。

### 6.4 iOS 移动端

- 没有找到任何公开资料说明 iOS 或 Android 的 eFootball 带有 `PlayerAppearance.bin`，也没有找到移动端的数据表格式、外壳或密钥。
- 要从 IPA 里确认的事项见 §8 和 `05_native_formats.md` §8：表在哪个容器、是否套 WESYS、密钥是否与 PC 相同、行长、PID 宽度。

---

## 7. 建议的参数布局 schema（JSON）

- 说明：
  - 由本组脚本根据 [A1][A2][A4][A6] 整理生成。edit72 和 pesdb60 两列是 PES 实测布局；ef64 列是推断出的 eFootball PC 布局。
  - **整个文件只是提案，必须先用用户的 11.1.1 实际数据验证（§8），才能用于写入。**
  - 偏移格式为 `[字节, 位]`，按 §1.2 的低位优先约定；`ef64_u32` 给出 `[u32 字偏移, 字内起始位]`。
  - `conf` 按布局分别标注置信度。宽度取 4cc-studio 的实测值；与维基不同的地方见 §2.3。
- **再次强调：这些是解码后表数据里的偏移，不是 IPA 或容器的文件偏移，也不是内存位置。**

```json
{
 "schema_id": "r9.player_appearance.layouts.v0",
 "generated": "2026-10-25",
 "status": "PROPOSAL - must be verified against the user's actual eFootball iOS 11.1.1 data before any write",
 "warning": [
  "Offsets are positions inside the DECODED table payload (after WESYS decrypt + zlib inflate) or inside the decrypted EDIT data.dat. They are NOT offsets in the IPA, the CPK, the .utoc/.ucas, the encrypted .bin, or process memory.",
  "In-memory structures differ (PES 2021 physique object = float[16] + u8, arm pair swapped). Never write a memory bit position into a file.",
  "Record N of a table starts at N*record_size in the decoded payload; tables may be sorted by PID and binary-searched (Player.bin is; PlayerAppearance claimed sorted ascending)."
 ],
 "bit_numbering": "LSB-first within each byte: field at (byte B, bit b, width w) = (int.from_bytes(payload[B:B+4],'little') >> b) & ((1<<w)-1). ef64_u32 gives the same field as (u32 word at offset, start bit within that LE word) as used by EvoTool.",
 "layouts": {
  "edit72": {
   "games": "PES 2016-2021 EDIT00000000 (decrypted data.dat) appearance block",
   "record_size": 72,
   "where": {
    "PES2015": "separate block @0x2AB9CC, stride 0x44 (68 B, different face run)",
    "PES2016": "separate block @0x2AB9CC, stride 0x48",
    "PES2017-2019": "inside 188-B (0xBC) player record at +0x74",
    "PES2020-2021": "inside 312-B (0x138) player record at +0xF0; players start @0x7C"
   },
   "confidence": "high (4ccEditor code, 4cc-studio measured 2026-10-13, Rigged/4cc wiki, GodotFootball live reads)"
  },
  "pesdb60": {
   "games": "PES 2016/2017-2021 PlayerAppearance.bin row (Fox games' own table)",
   "record_size": 60,
   "header": {
    "pid": "u32 LE @0x00"
   },
   "rule": "pesdb60 offset = edit72 offset - 8, covers edit bytes 0x0C..0x43",
   "confidence": "high (GodotFootball face report §4, 4cc-studio model.md, pes-db-generator template)"
  },
  "ef64": {
   "games": "eFootball PC PlayerAppearance.bin (EvoTool 'eFootball v5.2.0'; Mod Studio 'eFootball 2027 Steam dt200')",
   "record_size": 64,
   "header": {
    "pid": "u64 LE @0x00 (external PID / card id)"
   },
   "rule": "ef64 offset = edit72 offset - 4 = pesdb60 offset + 4, covers edit bytes 0x0C..0x43",
   "confidence": "MEDIUM: size/PID from Mod Studio doc (structural test on real files); field bits from EvoTool v520 (comments+writer use 8-byte header, its Load() reads a 4-byte id - internal inconsistency); every EvoTool field lands exactly on the PES16-21 field when shifted by 4"
  },
  "ios_11_1_1": {
   "games": "eFootball iOS 11.1.1 (build 11.1.101)",
   "inherits": "ef64",
   "confidence": "UNVERIFIED hypothesis - no public mobile decode found"
  }
 },
 "verification_tests": [
  "decoded_len % 64 == 0 and u64 at +0 strictly ascending over all records (Mod Studio §6)",
  "for >=99% of records every body nibble (ef64 0x08..0x0E) <= 14, modal value 7",
  "PID 15038 (Hulk, pesdb 2026-10-25): expect ef64[0x08..0x0C] = D4 A5 99 18 99 and low nibble of [0x0D] = 0xA (NL4 NS13 SH5 SW10 CM9 WS9 AS8 AL1 TS9 CS9 LL10); AL=1 detects an arm-pair swap",
  "PID 16786160 (Benzema base): expect A4 AA 77 77 9B, low nibble [0x0D]=7",
  "card 88044145348046 (Rivaldo, Brazil 2002 Epic): NL7 NS7 SH6 SW8 CM5 WS2 AS3 AL7 TS3 CS2 LL14 -> 77 86 25 73 23, low nibble [0x0D]=0xE (only if this card id has its own row; else use base 134606 【未核实】: 134606 is only the low 24 bits of the card id; pesdb /rivaldo-134606 returns 404; Mod Studio says masking recovers the base card in only 30.7% of variants)",
  "decode->encode round trip of the whole table is byte-identical before any edit",
  "change ONE nibble on a test player, confirm only that nibble differs after re-pack, then confirm the visual direction in-game"
 ],
 "fields":[
  {"name": "player_id", "width": 32, "enc": "u32 LE", "edit72": [0, 0], "conf": "high(edit)", "note": "edit-only header; DB row has its own PID"},
  {"name": "edited_face_flag", "width": 1, "enc": "bool", "edit72": [4, 0], "conf": "high(edit)", "note": "edit-only; PES2021 decoder never tests it (GodotFootball)"},
  {"name": "edited_hair_flag", "width": 1, "enc": "bool", "edit72": [4, 1], "conf": "high(edit)", "note": "edit-only"},
  {"name": "edited_physique_flag", "width": 1, "enc": "bool", "edit72": [4, 2], "conf": "high(edit)", "note": "edit-only"},
  {"name": "edited_strip_flag", "width": 1, "enc": "bool", "edit72": [4, 3], "conf": "high(edit)", "note": "edit-only"},
  {"name": "boots_id", "width": 14, "enc": "id", "edit72": [4, 4], "conf": "high(edit)", "note": "edit-only (DB: BootsList.bin)"},
  {"name": "gk_gloves_id", "width": 14, "enc": "id", "edit72": [6, 2], "conf": "high(edit)", "note": "edit-only; 14 bits as read AND written by 4ccEditor (pes20.cpp L155/L591/L885 'using Unk A space') and 4cc-studio (pes20.rs GlovesId bit_offset 1970, bit_width 14); wiki: 10 bits + 4-bit Unknown A (review K28)"},
  {"name": "base_copy_player_id", "width": 32, "enc": "u32 LE", "edit72": [8, 0], "conf": "high(edit)", "note": "edit-only"},
  {"name": "NeckLength", "width": 4, "enc": "slider: stored 0..14, display=stored-7, neutral 7", "edit72": [12, 0], "pesdb60": [4, 0], "ef64": [8, 0], "ef64_u32": ["0x08", 0], "conf": "high(edit/pesdb60); medium(ef64); UNVERIFIED(iOS 11.1.1)"},
  {"name": "NeckSize", "width": 4, "enc": "slider: stored 0..14, display=stored-7, neutral 7", "edit72": [12, 4], "pesdb60": [4, 4], "ef64": [8, 4], "ef64_u32": ["0x08", 4], "conf": "high(edit/pesdb60); medium(ef64); UNVERIFIED(iOS 11.1.1)"},
  {"name": "ShoulderHeight", "width": 4, "enc": "slider: stored 0..14, display=stored-7, neutral 7", "edit72": [13, 0], "pesdb60": [5, 0], "ef64": [9, 0], "ef64_u32": ["0x08", 8], "conf": "high(edit/pesdb60); medium(ef64); UNVERIFIED(iOS 11.1.1)"},
  {"name": "ShoulderWidth", "width": 4, "enc": "slider: stored 0..14, display=stored-7, neutral 7", "edit72": [13, 4], "pesdb60": [5, 4], "ef64": [9, 4], "ef64_u32": ["0x08", 12], "conf": "high(edit/pesdb60); medium(ef64); UNVERIFIED(iOS 11.1.1)"},
  {"name": "ChestMeasurement", "width": 4, "enc": "slider: stored 0..14, display=stored-7, neutral 7", "edit72": [14, 0], "pesdb60": [6, 0], "ef64": [10, 0], "ef64_u32": ["0x08", 16], "conf": "high(edit/pesdb60); medium(ef64); UNVERIFIED(iOS 11.1.1)"},
  {"name": "WaistSize", "width": 4, "enc": "slider: stored 0..14, display=stored-7, neutral 7", "edit72": [14, 4], "pesdb60": [6, 4], "ef64": [10, 4], "ef64_u32": ["0x08", 20], "conf": "high(edit/pesdb60); medium(ef64); UNVERIFIED(iOS 11.1.1)"},
  {"name": "ArmSize", "width": 4, "enc": "slider: stored 0..14, display=stored-7, neutral 7", "edit72": [15, 0], "pesdb60": [7, 0], "ef64": [11, 0], "ef64_u32": ["0x08", 24], "conf": "high(edit/pesdb60); medium(ef64); UNVERIFIED(iOS 11.1.1)"},
  {"name": "ArmLength", "width": 4, "enc": "slider: stored 0..14, display=stored-7, neutral 7", "edit72": [15, 4], "pesdb60": [7, 4], "ef64": [11, 4], "ef64_u32": ["0x08", 28], "conf": "high(edit/pesdb60); medium(ef64); UNVERIFIED(iOS 11.1.1)"},
  {"name": "ThighSize", "width": 4, "enc": "slider: stored 0..14, display=stored-7, neutral 7", "edit72": [16, 0], "pesdb60": [8, 0], "ef64": [12, 0], "ef64_u32": ["0x0C", 0], "conf": "high(edit/pesdb60); medium(ef64); UNVERIFIED(iOS 11.1.1)"},
  {"name": "CalfSize", "width": 4, "enc": "slider: stored 0..14, display=stored-7, neutral 7", "edit72": [16, 4], "pesdb60": [8, 4], "ef64": [12, 4], "ef64_u32": ["0x0C", 4], "conf": "high(edit/pesdb60); medium(ef64); UNVERIFIED(iOS 11.1.1)"},
  {"name": "LegLength", "width": 4, "enc": "slider: stored 0..14, display=stored-7, neutral 7", "edit72": [17, 0], "pesdb60": [9, 0], "ef64": [13, 0], "ef64_u32": ["0x0C", 8], "conf": "high(edit/pesdb60); medium(ef64); UNVERIFIED(iOS 11.1.1)"},
  {"name": "HeadLength", "width": 4, "enc": "slider: stored 0..14, display=stored-7, neutral 7", "edit72": [17, 4], "pesdb60": [9, 4], "ef64": [13, 4], "ef64_u32": ["0x0C", 12], "conf": "high(edit/pesdb60); medium(ef64); UNVERIFIED(iOS 11.1.1)"},
  {"name": "HeadWidth", "width": 4, "enc": "slider: stored 0..14, display=stored-7, neutral 7", "edit72": [18, 0], "pesdb60": [10, 0], "ef64": [14, 0], "ef64_u32": ["0x0C", 16], "conf": "high(edit/pesdb60); medium(ef64); UNVERIFIED(iOS 11.1.1)"},
  {"name": "HeadDepth", "width": 4, "enc": "slider: stored 0..14, display=stored-7, neutral 7", "edit72": [18, 4], "pesdb60": [10, 4], "ef64": [14, 4], "ef64_u32": ["0x0C", 20], "conf": "high(edit/pesdb60); medium(ef64); UNVERIFIED(iOS 11.1.1)"},
  {"name": "WristTapeColorLeft", "width": 3, "enc": "enum", "edit72": [19, 0], "pesdb60": [11, 0], "ef64": [15, 0], "ef64_u32": ["0x0C", 24], "conf": "high(edit); medium(ef64)"},
  {"name": "WristTapeColorRight", "width": 3, "enc": "enum", "edit72": [19, 3], "pesdb60": [11, 3], "ef64": [15, 3], "ef64_u32": ["0x0C", 27], "conf": "high(edit); medium(ef64)"},
  {"name": "WristTaping", "width": 2, "enc": "enum", "edit72": [19, 6], "pesdb60": [11, 6], "ef64": [15, 6], "ef64_u32": ["0x0C", 30], "conf": "high(edit); medium(ef64)"},
  {"name": "SpectaclesColor", "width": 3, "enc": "enum", "edit72": [20, 0], "pesdb60": [12, 0], "ef64": [16, 0], "ef64_u32": ["0x10", 0], "conf": "high(edit); medium(ef64)"},
  {"name": "Spectacles", "width": 3, "enc": "enum", "edit72": [20, 3], "pesdb60": [12, 3], "ef64": [16, 3], "ef64_u32": ["0x10", 3], "conf": "high(edit); medium(ef64)"},
  {"name": "Sleeves", "width": 2, "enc": "enum", "edit72": [20, 6], "pesdb60": [12, 6], "ef64": [16, 6], "ef64_u32": ["0x10", 6], "conf": "high(edit); medium(ef64)"},
  {"name": "LongSleevedInners", "width": 2, "enc": "enum", "edit72": [21, 0], "pesdb60": [13, 0], "ef64": [17, 0], "ef64_u32": ["0x10", 8], "conf": "high(edit); medium(ef64)"},
  {"name": "SockLength", "width": 2, "enc": "enum", "edit72": [21, 2], "pesdb60": [13, 2], "ef64": [17, 2], "ef64_u32": ["0x10", 10], "conf": "high(edit); medium(ef64)"},
  {"name": "Undershorts", "width": 2, "enc": "enum", "edit72": [21, 4], "pesdb60": [13, 4], "ef64": [17, 4], "ef64_u32": ["0x10", 12], "conf": "high(edit); medium(ef64)"},
  {"name": "Shirttail", "width": 1, "enc": "enum", "edit72": [21, 6], "pesdb60": [13, 6], "ef64": [17, 6], "ef64_u32": ["0x10", 14], "conf": "high(edit); medium(ef64)"},
  {"name": "AnkleTaping", "width": 1, "enc": "enum", "edit72": [21, 7], "pesdb60": [13, 7], "ef64": [17, 7], "ef64_u32": ["0x10", 15], "conf": "high(edit); medium(ef64)"},
  {"name": "PlayerGloves", "width": 1, "enc": "enum/type", "edit72": [22, 0], "pesdb60": [14, 0], "ef64": [18, 0], "ef64_u32": ["0x10", 16], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "PlayerGlovesColor", "width": 3, "enc": "enum/type", "edit72": [22, 1], "pesdb60": [14, 1], "ef64": [18, 1], "ef64_u32": ["0x10", 17], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "Jawline", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [22, 4], "pesdb60": [14, 4], "ef64": [18, 4], "ef64_u32": ["0x10", 20], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "JawHeight", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [23, 0], "pesdb60": [15, 0], "ef64": [19, 0], "ef64_u32": ["0x10", 24], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "Pes15JawDepth", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [23, 4], "pesdb60": [15, 4], "ef64": [19, 4], "ef64_u32": ["0x10", 28], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": "stored on all versions, drawn only by PES 2015 (4cc-studio)"},
  {"name": "Cheekbones", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [24, 0], "pesdb60": [16, 0], "ef64": [20, 0], "ef64_u32": ["0x14", 0], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "CheekboneHeight", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [24, 4], "pesdb60": [16, 4], "ef64": [20, 4], "ef64_u32": ["0x14", 4], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": "no slider in PES16-21 UI but drawn (4cc-studio)"},
  {"name": "Pes15CheekboneDepth", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [25, 0], "pesdb60": [17, 0], "ef64": [21, 0], "ef64_u32": ["0x14", 8], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": "stored on all versions, drawn only by PES 2015 (4cc-studio)"},
  {"name": "BrowWidth", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [26, 4], "pesdb60": [18, 4], "ef64": [22, 4], "ef64_u32": ["0x14", 20], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "InnerEyebrowHeight", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [27, 0], "pesdb60": [19, 0], "ef64": [23, 0], "ef64_u32": ["0x14", 24], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "EyebrowDepth", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [27, 4], "pesdb60": [19, 4], "ef64": [23, 4], "ef64_u32": ["0x14", 28], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "TempleWidth", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [28, 0], "pesdb60": [20, 0], "ef64": [24, 0], "ef64_u32": ["0x18", 0], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "OuterEyebrowHeight", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [28, 4], "pesdb60": [20, 4], "ef64": [24, 4], "ef64_u32": ["0x18", 4], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "Pes15EyebrowDepth", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [29, 0], "pesdb60": [21, 0], "ef64": [25, 0], "ef64_u32": ["0x18", 8], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": "stored on all versions, drawn only by PES 2015 (4cc-studio)"},
  {"name": "PupilSize", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [29, 4], "pesdb60": [21, 4], "ef64": [25, 4], "ef64_u32": ["0x18", 12], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "EyeDepth", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [30, 0], "pesdb60": [22, 0], "ef64": [26, 0], "ef64_u32": ["0x18", 16], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "InnerEyePosition", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [30, 4], "pesdb60": [22, 4], "ef64": [26, 4], "ef64_u32": ["0x18", 20], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "InnerEyeHeight", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [31, 0], "pesdb60": [23, 0], "ef64": [27, 0], "ef64_u32": ["0x18", 24], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "OuterEyePosition", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [31, 4], "pesdb60": [23, 4], "ef64": [27, 4], "ef64_u32": ["0x18", 28], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "EyeCornerHeight", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [32, 0], "pesdb60": [24, 0], "ef64": [28, 0], "ef64_u32": ["0x1C", 0], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "UpperEyelidWidthInner", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [32, 4], "pesdb60": [24, 4], "ef64": [28, 4], "ef64_u32": ["0x1C", 4], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "UpperEyelidHeightInner", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [33, 0], "pesdb60": [25, 0], "ef64": [29, 0], "ef64_u32": ["0x1C", 8], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "UpperEyelidWidthOuter", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [33, 4], "pesdb60": [25, 4], "ef64": [29, 4], "ef64_u32": ["0x1C", 12], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "UpperEyelidHeightOuter", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [34, 0], "pesdb60": [26, 0], "ef64": [30, 0], "ef64_u32": ["0x1C", 16], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "Pes15BottomEyelidPosition", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [34, 4], "pesdb60": [26, 4], "ef64": [30, 4], "ef64_u32": ["0x1C", 20], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": "stored on all versions, drawn only by PES 2015 (4cc-studio)"},
  {"name": "BottomEyelidHeight", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [35, 0], "pesdb60": [27, 0], "ef64": [31, 0], "ef64_u32": ["0x1C", 24], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "HorizontalEyePosition", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [35, 4], "pesdb60": [27, 4], "ef64": [31, 4], "ef64_u32": ["0x1C", 28], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "EyeHeight", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [36, 0], "pesdb60": [28, 0], "ef64": [32, 0], "ef64_u32": ["0x20", 0], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "NoseHeight", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [36, 4], "pesdb60": [28, 4], "ef64": [32, 4], "ef64_u32": ["0x20", 4], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "NoseTipDepth", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [37, 0], "pesdb60": [29, 0], "ef64": [33, 0], "ef64_u32": ["0x20", 8], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "NostrilWidth", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [37, 4], "pesdb60": [29, 4], "ef64": [33, 4], "ef64_u32": ["0x20", 12], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "Pes15NasalBridgePosition", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [38, 0], "pesdb60": [30, 0], "ef64": [34, 0], "ef64_u32": ["0x20", 16], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": "stored on all versions, drawn only by PES 2015 (4cc-studio)"},
  {"name": "NoseDepth", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [38, 4], "pesdb60": [30, 4], "ef64": [34, 4], "ef64_u32": ["0x20", 20], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "NoseWidth", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [39, 0], "pesdb60": [31, 0], "ef64": [35, 0], "ef64_u32": ["0x20", 24], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "Pes15NasalRootHeight", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [39, 4], "pesdb60": [31, 4], "ef64": [35, 4], "ef64_u32": ["0x20", 28], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": "stored on all versions, drawn only by PES 2015 (4cc-studio)"},
  {"name": "MouthHeight", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [40, 0], "pesdb60": [32, 0], "ef64": [36, 0], "ef64_u32": ["0x24", 0], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "MouthDepth", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [40, 4], "pesdb60": [32, 4], "ef64": [36, 4], "ef64_u32": ["0x24", 4], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "LipSize", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [41, 0], "pesdb60": [33, 0], "ef64": [37, 0], "ef64_u32": ["0x24", 8], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "MouthCornerHeight", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [42, 0], "pesdb60": [34, 0], "ef64": [38, 0], "ef64_u32": ["0x24", 16], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "LipWidth", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [42, 4], "pesdb60": [34, 4], "ef64": [38, 4], "ef64_u32": ["0x24", 20], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "Pes15MouthEdgePosition", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [43, 0], "pesdb60": [35, 0], "ef64": [39, 0], "ef64_u32": ["0x24", 24], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": "stored on all versions, drawn only by PES 2015 (4cc-studio)"},
  {"name": "ChinWidth", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [43, 4], "pesdb60": [35, 4], "ef64": [39, 4], "ef64_u32": ["0x24", 28], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "ChinHeight", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [44, 0], "pesdb60": [36, 0], "ef64": [40, 0], "ef64_u32": ["0x28", 0], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "ChinDepth", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [44, 4], "pesdb60": [36, 4], "ef64": [40, 4], "ef64_u32": ["0x28", 4], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "SkinColor", "width": 3, "enc": "enum/type", "edit72": [45, 0], "pesdb60": [37, 0], "ef64": [41, 0], "ef64_u32": ["0x28", 8], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": "wiki: 0 white/transparent, 1-6 tones, 7 none; GodotFootball: 1-6 (0/7 special); 4ccEditor maps 7->0 for PES18+"},
  {"name": "CheekType", "width": 5, "enc": "enum/type", "edit72": [45, 3], "pesdb60": [37, 3], "ef64": [41, 3], "ef64_u32": ["0x28", 11], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "ForeheadType", "width": 3, "enc": "enum/type", "edit72": [46, 0], "pesdb60": [38, 0], "ef64": [42, 0], "ef64_u32": ["0x28", 16], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "FacialHairType", "width": 5, "enc": "enum/type", "edit72": [46, 3], "pesdb60": [38, 3], "ef64": [42, 3], "ef64_u32": ["0x28", 19], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "LaughterLinesType", "width": 3, "enc": "enum/type", "edit72": [47, 0], "pesdb60": [39, 0], "ef64": [43, 0], "ef64_u32": ["0x28", 24], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "UpperEyelidType", "width": 5, "enc": "enum/type", "edit72": [47, 3], "pesdb60": [39, 3], "ef64": [43, 3], "ef64_u32": ["0x28", 27], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "LowerEyelidType", "width": 4, "enc": "enum/type", "edit72": [48, 0], "pesdb60": [40, 0], "ef64": [44, 0], "ef64_u32": ["0x2C", 0], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "EarLength", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [48, 4], "pesdb60": [40, 4], "ef64": [44, 4], "ef64_u32": ["0x2C", 4], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "EarWidth", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [49, 0], "pesdb60": [41, 0], "ef64": [45, 0], "ef64_u32": ["0x2C", 8], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "EarAngle", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [49, 4], "pesdb60": [41, 4], "ef64": [45, 4], "ef64_u32": ["0x2C", 12], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "EyebrowType", "width": 5, "enc": "enum/type", "edit72": [50, 0], "pesdb60": [42, 0], "ef64": [46, 0], "ef64_u32": ["0x2C", 16], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "NeckLineType", "width": 3, "enc": "enum/type", "edit72": [50, 5], "pesdb60": [42, 5], "ef64": [46, 5], "ef64_u32": ["0x2C", 21], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "FaceSize", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [51, 0], "pesdb60": [43, 0], "ef64": [47, 0], "ef64_u32": ["0x2C", 24], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "FaceHeight", "width": 4, "enc": "slider: stored 0..14 (display-7)", "edit72": [51, 4], "pesdb60": [43, 4], "ef64": [47, 4], "ef64_u32": ["0x2C", 28], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "NoseType", "width": 6, "enc": "enum/type", "edit72": [52, 0], "pesdb60": [44, 0], "ef64": [48, 0], "ef64_u32": ["0x30", 0], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "UpperLipType", "width": 3, "enc": "enum/type", "edit72": [53, 0], "pesdb60": [45, 0], "ef64": [49, 0], "ef64_u32": ["0x30", 8], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "LowerLipType", "width": 3, "enc": "enum/type", "edit72": [53, 3], "pesdb60": [45, 3], "ef64": [49, 3], "ef64_u32": ["0x30", 11], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "HairColorRed", "width": 6, "enc": "colour channel 0..63", "edit72": [53, 6], "pesdb60": [45, 6], "ef64": [49, 6], "ef64_u32": ["0x30", 14], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "HairColorGreen", "width": 6, "enc": "colour channel 0..63", "edit72": [54, 4], "pesdb60": [46, 4], "ef64": [50, 4], "ef64_u32": ["0x30", 20], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "HairColorBlue", "width": 6, "enc": "colour channel 0..63", "edit72": [55, 2], "pesdb60": [47, 2], "ef64": [51, 2], "ef64_u32": ["0x30", 26], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "HairStyle", "width": 4, "enc": "enum/type", "edit72": [56, 0], "pesdb60": [48, 0], "ef64": [52, 0], "ef64_u32": ["0x34", 0], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "HairCroppedStyle", "width": 4, "enc": "enum/type", "edit72": [56, 4], "pesdb60": [48, 4], "ef64": [52, 4], "ef64_u32": ["0x34", 4], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "EyebrowColorRed", "width": 6, "enc": "colour channel 0..63", "edit72": [57, 0], "pesdb60": [49, 0], "ef64": [53, 0], "ef64_u32": ["0x34", 8], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "EyebrowStyle", "width": 2, "enc": "enum/type", "edit72": [57, 6], "pesdb60": [49, 6], "ef64": [53, 6], "ef64_u32": ["0x34", 14], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "EyebrowColorGreen", "width": 6, "enc": "colour channel 0..63", "edit72": [58, 0], "pesdb60": [50, 0], "ef64": [54, 0], "ef64_u32": ["0x34", 16], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "EyebrowDensity", "width": 2, "enc": "enum/type", "edit72": [58, 6], "pesdb60": [50, 6], "ef64": [54, 6], "ef64_u32": ["0x34", 22], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "EyebrowColorBlue", "width": 6, "enc": "colour channel 0..63", "edit72": [59, 0], "pesdb60": [51, 0], "ef64": [55, 0], "ef64_u32": ["0x34", 24], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "EyebrowThickness", "width": 2, "enc": "enum/type", "edit72": [59, 6], "pesdb60": [51, 6], "ef64": [55, 6], "ef64_u32": ["0x34", 30], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "HairLength", "width": 3, "enc": "enum/type", "edit72": [60, 0], "pesdb60": [52, 0], "ef64": [56, 0], "ef64_u32": ["0x38", 0], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "HairWaveLevel", "width": 3, "enc": "enum/type", "edit72": [60, 3], "pesdb60": [52, 3], "ef64": [56, 3], "ef64_u32": ["0x38", 3], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "Pes15EyeWhite", "width": 2, "enc": "enum/type", "edit72": [60, 6], "pesdb60": [52, 6], "ef64": [56, 6], "ef64_u32": ["0x38", 6], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": "stored on all versions, drawn only by PES 2015 (4cc-studio)"},
  {"name": "HairAccessory", "width": 1, "enc": "enum/type", "edit72": [61, 0], "pesdb60": [53, 0], "ef64": [57, 0], "ef64_u32": ["0x38", 8], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "HairAccessoryColor", "width": 3, "enc": "enum/type", "edit72": [61, 1], "pesdb60": [53, 1], "ef64": [57, 1], "ef64_u32": ["0x38", 9], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "FacialHairColorRed", "width": 6, "enc": "colour channel 0..63", "edit72": [61, 4], "pesdb60": [53, 4], "ef64": [57, 4], "ef64_u32": ["0x38", 12], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "FacialHairColorGreen", "width": 6, "enc": "colour channel 0..63", "edit72": [62, 2], "pesdb60": [54, 2], "ef64": [58, 2], "ef64_u32": ["0x38", 18], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "FacialHairColorBlue", "width": 6, "enc": "colour channel 0..63", "edit72": [63, 0], "pesdb60": [55, 0], "ef64": [59, 0], "ef64_u32": ["0x38", 24], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "FacialHairThickness", "width": 2, "enc": "enum/type", "edit72": [63, 6], "pesdb60": [55, 6], "ef64": [59, 6], "ef64_u32": ["0x38", 30], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "IrisColor", "width": 4, "enc": "enum/type", "edit72": [64, 0], "pesdb60": [56, 0], "ef64": [60, 0], "ef64_u32": ["0x3C", 0], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": "wiki range 0..10"},
  {"name": "HairVariation", "width": 8, "enc": "enum/type", "edit72": [64, 4], "pesdb60": [56, 4], "ef64": [60, 4], "ef64_u32": ["0x3C", 4], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "HairColorPreset", "width": 4, "enc": "enum/type", "edit72": [65, 4], "pesdb60": [57, 4], "ef64": [61, 4], "ef64_u32": ["0x3C", 12], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "HairFrontStyle", "width": 3, "enc": "enum/type", "edit72": [66, 0], "pesdb60": [58, 0], "ef64": [62, 0], "ef64_u32": ["0x3C", 16], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "HairParted", "width": 3, "enc": "enum/type", "edit72": [66, 3], "pesdb60": [58, 3], "ef64": [62, 3], "ef64_u32": ["0x3C", 19], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "HairSideBackStyle", "width": 3, "enc": "enum/type", "edit72": [66, 6], "pesdb60": [58, 6], "ef64": [62, 6], "ef64_u32": ["0x3C", 22], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "Hairline", "width": 3, "enc": "enum/type", "edit72": [67, 1], "pesdb60": [59, 1], "ef64": [63, 1], "ef64_u32": ["0x3C", 25], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "HairForeheadWidth", "width": 4, "enc": "enum/type", "edit72": [67, 4], "pesdb60": [59, 4], "ef64": [63, 4], "ef64_u32": ["0x3C", 28], "conf": "high(edit, measured by 4cc-studio); medium(ef64)", "note": ""},
  {"name": "unused_run3_4", "width": 4, "enc": "unused (measured)", "edit72": [25, 4], "pesdb60": [17, 4], "ef64": [21, 4], "ef64_u32": ["0x14", 12], "conf": "high(edit)", "note": "written 7"},
  {"name": "unused_run4_0", "width": 4, "enc": "unused (measured)", "edit72": [26, 0], "pesdb60": [18, 0], "ef64": [22, 0], "ef64_u32": ["0x14", 16], "conf": "high(edit)", "note": "written 7"},
  {"name": "unused_run19_4", "width": 4, "enc": "unused (measured)", "edit72": [41, 4], "pesdb60": [33, 4], "ef64": [37, 4], "ef64_u32": ["0x24", 12], "conf": "high(edit)", "note": "written 7"},
  {"name": "unused_run30_6", "width": 2, "enc": "unused (measured)", "edit72": [52, 6], "pesdb60": [44, 6], "ef64": [48, 6], "ef64_u32": ["0x30", 6], "conf": "high(edit)", "note": "written 1"},
  {"name": "unused_edit_0x44_0x47", "width": 32, "enc": "unused (measured)", "edit72": [68, 0], "conf": "high(edit)", "note": "PES16-21 edit only; written 0; absent from pesdb60 and ef64"}
 ]
}
```

最小编解码规则（T4，供任务 #8 的 codec 使用）：

```python
BODY = ["NeckLength","NeckSize","ShoulderHeight","ShoulderWidth","ChestMeasurement","WaistSize",
        "ArmSize","ArmLength","ThighSize","CalfSize","LegLength","HeadLength","HeadWidth","HeadDepth"]
def decode_body(row: bytes, base: int):          # base = 0x08 (ef64 假设) / 0x04 (pesdb60) / 0x0C (edit72)
    return {k: (row[base + i//2] >> (4*(i%2))) & 0xF for i, k in enumerate(BODY)}
def encode_body(row: bytearray, base: int, vals: dict):
    for i, k in enumerate(BODY):
        v = vals[k]; assert 0 <= v <= 14          # 15 不合法（PES 2021 会把它当 0）
        b = base + i//2; sh = 4*(i%2)
        row[b] = (row[b] & ~(0xF << sh) & 0xFF) | (v << sh)   # 只改这一个半字节，同一字节的另一半保留原值
```

---

## 8. 验证方案（拿到 iOS 11.1.1 数据后）

1. **定位**：在 IPA 或下载目录的容器中按名称找 `PlayerAppearance` 或 `pesdb`。若没有目录索引，按 Mod Studio §6 的内容特征识别：行长 64 B，开头的 u64 严格递增。注意它的总长也是 24 的倍数，要先排除 `PlayerAssignment`（L318-323）。
2. **解壳**：确认是否有 WESYS 头（头里 0x03 处是 `WESYS`，0x08 是压缩长度，0x0C 是原始长度）。解密方法按 Mod Studio §1；移动端密钥可能不同。解压后检查：解压长度 = 原始长度，且是 64 的整数倍。
3. **整表往返**：解码 → 不做任何修改重新编码 → 与原始解压数据**逐字节相同**；再把压缩和加密也走一遍往返。
4. **结构检验**：
   - u64 PID 严格递增。
   - 身体 7 字节的所有半字节 ≤ 14 的记录占 ≥ 99%，众数为 7。
   - 区分 PID 是 8 字节还是 4 字节：看每行的 0x04–0x07。按 ef64 假设，这 4 字节是 u64 PID 的高 32 位，基础球员（PID < 2³²）应为 `00 00 00 00`，变体卡为卡种前缀。如果这里普遍是 `77 77 77 77` 一类的半字节值，说明身体字段从 0x04 开始（4 字节 PID 的变体），全部偏移要减 4 后重新判断。
5. **测试向量**（pesdb 存储值，2026-10-25 抓取；按 ef64 假设计算）：

| PID | 期望 0x08–0x0C | 期望 0x0D 低半字节 |
|---|---|---|
| 15038（Hulk） | `D4 A5 99 18 99` | `A` |
| 16786160（Benzema） | `A4 AA 77 77 9B` | `7` |
| 88044145348046（Rivaldo B2002 Epic；若卡没有独立行则查 134606【未核实：134606 是推出来的基础 PID，见 §5.2】） | `77 86 25 73 23` | `E` |

   - 0x0B 是 `18`（不是 `81`）说明 ArmSize 在低半字节，与 PES 相同。
   - 三条都对上，ef64 的身体字段即为"已验证"；对不上则停止，回到第 4 步。
6. **单点修改**：只改一名测试球员的一个半字节（例如 ThighSize 7 → 10），重新打包，与原表比较：只有 1 个半字节不同。然后在**隔离的测试客户端**中看方向（大腿变粗），并保留截图作为标定记录。
7. **方向与刻度标定**：对 14 项各做 0、7、14 三档，测量建模比例的变化，作为 eFootball 的每档效果表（替代 §4.2 中 PES 2021 的数据）。
8. **保留原值**：任务书要求保留未修改字段的原值。写入时只改目标半字节；肤色、发型等字段一律保持 R9 供体或新建记录的原值，除非有意修改。

---

## 9. 未找到 / 待解决

| 项 | 状态 | 怎样解决 |
|---|---|---|
| Konami 官方格式文档（T1） | 未找到 | 不存在公开文档；只能用社区逆向加实机验证 |
| iOS 11.1.1 是否有 `PlayerAppearance.bin`，以及它的外壳、密钥、行长 | 未找到 | 读 IPA 容器目录（`tools/native/iostore.py list`），按 §8 检验 |
| eFootball 的 PID 宽度：Mod Studio 写 8 B，EvoTool 读取代码用 4 B | 冲突 | §8 第 4 步的结构检验加测试向量（核查补充：支持 8 B 的还有 EvoTool 自己的写入代码（`pa.playerId` 是 `ulong`）和 Devil Cold52 V2.5.1.4 的 "player ids are now read as uint64"，后者针对 Player.bin，属 T3。PID ≥ 2³² 的卡确实存在，例如 88044145348046） |
| 宽度冲突：维基与 4cc-studio（鼻型、眉型、上下眼睑类型、发型、侧后修剪、发型变体、发际线、前额宽度、手套 ID；核查补充：前额发型，维基 2 bit，4cc-studio 3 bit） | 冲突已列出（§2.3） | 以 4cc-studio 实测为准；eFootball 中以实际数据的取值范围为准 |
| 肤色取值含义（0 与 7） | 冲突（§2.3） | 统计实际表中 0–7 的分布，对照游戏内肤色 |
| R9 的 Konami 体型和脸部参数（任何版本） | 未找到 | PES 2017 原版表 PID 2137（属于 1996/97 年版本）；或 Konami 已发行的 R9 数据（若用户的本地数据库或 IPA 中有） |
| eFootball 体型参数的视觉效果和每档刻度 | 未找到 | §8 第 7 步实机标定 |
| eFootball 里体型是否影响碰撞和触球范围 | 只有 pesdb、eFootBase 的派生指标（T3） | 需要客户端逆向或实机对比 |
| FaceNumber 的位置与编码 | 未找到 | 在 `Player.bin` 的未知位、其他表和 UE 资产注册表中，对照已知球员（如亨利供体槽位 110718）查找 |
| Mod Studio 引用的参考实现 `Giggitycountless/efootball-player-tool` | 无法访问（clone 要求认证，可能是私有或已删除） | 以后重试 |
| EvoWeb 线程 88692、RBsGameLab Patreon 原文 | 403，只看到搜索摘要 | 用浏览器人工查看 |
| 网络搜索 | 本轮搜索额度已用完，个别方向（eFHUB 体型指标说明、PES 6 时代 R9 数据）没有继续 | 后续可再检索 |

---

## 10. 来源

### 10.1 开源代码与文档（GitHub，均为 [T3]；本次克隆到草稿目录，提交号如下）

| 编号 | 仓库 / 文件（行号） | 提交 / 日期 | 内容 |
|---|---|---|---|
| A1 | https://github.com/the4chancup/4ccEditor — `editor.h` L155-195；`data_util.cpp` L6-30；`pes15.cpp` L3-14, L220-299；`pes16.cpp` L199-275；`pes19.cpp` L166-277；`pes20.cpp` L146-201, L733；`main.cpp` L1697, L1771-1781, L1830, L1886, L1948, L2626-2663, L3285-3330；`window.cpp` L1484-1520 | `8125b98`，2026-10-22（"Autumn 26 Edition (Version C)"） | PES 2015–2021 存档编辑器（zlib 许可） |
| A2 | https://github.com/the4chancup/4cc-studio — `crates/libs/pes_savefile/src/schema/pes15.rs` L377-378；`pes16.rs` L324-325；`pes17.rs`/`pes18.rs`/`pes19.rs` L16-17, L290；`pes20.rs` L16-17, L315-423, L769-772；`schema/ingame_face.rs`；`docs/spec/pes_savefile/model.md` L150-400；`resources/Editfile.wikitext` L3239-3243, L10904-11887（4cc/Rigged 维基 "Edit file" 页面的副本） | `b8ed279`，2026-10-23 | PES 存档格式规范；编辑脸各位为游戏内实测（2026-10-13） |
| A3 | https://github.com/zocram4cc/GodotFootball — `docs/re/pes-re-physique.md`（L33-57, L78-136, L170-218, L259-360）；`docs/re/pes-re-face.md`（L13-40, L62-126, L146-186, L310-393） | `0dd11fb`，2026-10-25（核查时 HEAD 已到 `f81633f`，2026-10-25 18:04；所引内容都还在，个别行号相差约 2 行） | PES 2021 引擎逆向：体型骨骼缩放、脸部装配；含运行时只读实测 |
| A4 | https://github.com/Vasquito89/eFootball2022-Project — `EvoTool/Controllers/v520/PlayerAppearanceControllerV520.cs` L15-16, L87-259, L553-760, L789；`Models/v520/PlayerAppearanceV520.cs`；`Controllers/v520/PlayerControllerV520.cs` L16, L108-129；`Home.cs` L128-141 | `fd8aef2`，2026-07-18 | eFootball v5.2.0 PC 数据表编辑器（CC0）；外观解析为最新一次提交加入 |
| A5 | https://github.com/Vasquito89/Football-Championship — `Assets/Scripts/DB/Controladores/PlayerAppearanceController.cs` L15-16, L45-60；`Assets/StreamingAssets/db/PlayerAppearance.bin`（16 B，空表） | `da12f05`，2026-06-25 | 同一作者的早期读取器（60 B） |
| A6 | https://github.com/Master-Antonio/Efootball-Mod-Studio — `docs/PESDB_DATABASE_FORMAT.md` L13-46, L54-136, L176-221, L286-292, L306-341 | `c077835`，2026-08-16（核查：这是 author date，commit date 为 2026-09-26） | eFootball 2027 Steam 数据表格式（WESYS、Player、Assignment、Appearance） |
| A7 | https://github.com/the4chancup/pes-db-generator — `generators/player_appearance.py` L1-31；`generators/bin/PlayerAppearance_Base_16.bin`；`README.md` | `9571cd2`，2025-06-27 | PES 15–21 数据库表生成器；60 B 外观行模板 |
| A8 | https://github.com/gpsuperleague/GPSL — `docs/efootball-managers-extract.md` L113 | `ec5f7e9`，2026-10-24 | eFootball PC 数据表清单与路径 |
| A9 | https://github.com/duogxaolin/eflab — `docs/efootbase-efhub-features.md` L158-184；`packages/db/src/efootbase-map.ts` L139-160 | `79b48e5`，2026-10-25 | eFootBase/eFHUB 的 11 项体型字段与派生指标 |

### 10.2 网页（均为 [T3]，访问日期 2026-10-25）

| 编号 | URL | 内容 |
|---|---|---|
| W1 | https://pesdb.net/efootball/players/karim-benzema-16786160 | Benzema 体型 11 项、派生指标 |
| W2 | https://pesdb.net/efootball/players/hulk-15038 ；https://pesdb.net/efootball/players/hulk-53878485695166 | Hulk 基础卡与 Trending 卡，数值相同 |
| W3 | https://pesdb.net/efootball/players/rivaldo-88044145348046 | Rivaldo Brazil 2002 Epic |
| W4 | https://pesdb.net/pes2017/players/ronaldo-2137 ；https://pesdb.net/pes2018/?name=Ronaldo ；https://pesdb.net/pes2019/?name=Ronaldo ；同一 id 在 pes2016、2018、2019、2020、2021 均返回 404（curl 检查） | R9 在 PES 系列中的收录情况 |
| W5 | https://www.pesmodding.com/2026/08/efootball-2027-playerbin-editor-by.html | RBsGameLab eFootball 2027 Player.bin Editor（文章日期 2026-10-14） |
| W6 | https://www.pesmodding.com/2023/02/efootball-20222023-player-data-editor_28.html | Devil Cold52 编辑器各版本说明（外观功能状态） |
| W7 | https://www.pesmodding.com/2025/03/efootball-2025-player-data-editor-v4310.html | eFootball 2025 版 V4.4.0.0："player appearance feature is still disabled" |
| W8 | https://www.pesmodding.com/2023/06/efootball-260-edit-menu-unlocker.html | 隐藏编辑模式 |
| W9 | https://www.pesmodding.com/2023/09/pes-2021-realistic-body-model-settings.html | PES 2021 社区体型经验值 |
| W10 | https://www.pesmodding.com/2021/10/efootball-2022-player-data-editor-beta.html | eFootball 2022 首个 Player.bin 编辑器 |
| W11 | https://www.patreon.com/RBsGameLab/posts/efootball-player-160799196 | 403，只看到搜索摘要（提到 PlayerAppearance.bin 可编辑 body、face、hair） |
| W12 | https://evoweb.uk/threads/88692/ | 403，未能打开 |

### 10.3 本项目内部文档

- `docs/TASK_BRIEF.md` 第五、七、八节（14 项参数、64 字节布局、FaceNumber）。
- `research/notes/02_body_2002.md` §6（pesdb 体型 11 项与对照球员）。
- `research/notes/04_efootball_systems.md` §1（App 与游戏版本对应）、§8.4（card_id 结构）。
- `research/notes/05_native_formats.md` §5（社区工具、脸部存储、pc 编号）。
- 本文计算脚本（不入库）：`/tmp/claude-0/-home-user-wdxac/53d2b8c2-6abb-5fab-91db-f6077147a038/scratchpad/calc/phys.py`、`/tmp/claude-0/-home-user-wdxac/53d2b8c2-6abb-5fab-91db-f6077147a038/scratchpad/calc/gen_schema.py`。

---

## 核查记录

> 核查人：对抗性事实核查（独立复核），日期 2026-10-25。
> 方法：
> - 把 §10 列出的 9 个 GitHub 仓库重新浅克隆到独立目录 `/tmp/claude-0/-home-user-wdxac/53d2b8c2-6abb-5fab-91db-f6077147a038/scratchpad/fc06/repos/`，逐行读源码和文档，没有使用原作者留在草稿目录里的克隆。
> - 用 curl 重新抓取 pesdb、pesmodding、Konami 页面，存到 `/tmp/claude-0/-home-user-wdxac/53d2b8c2-6abb-5fab-91db-f6077147a038/scratchpad/fc06/web/`。
> - 所有算术（测试向量字节、卡 ID 十六进制、G、体重归一化、每档百分比、LegLength 分解）都重新计算了一遍。
> - 用脚本把 §7 JSON 中 89 个面部、发型字段的 `edit72` 位置和宽度，逐一对比 4cc-studio `ingame_face.rs` 的 `INGAME_FACE_FIELDS`：0 处不符。只缺 3 个 PES 2015 专用字段，属于预期。
>
> 限制：本轮 WebSearch 额度已用完，没有再做搜索引擎检索，只抓取了已知 URL。4cc/Rigged 维基在线页面（implyingrigged.info）对 curl 和 WebFetch 都返回 403，所以维基内容只核对了 4cc-studio 仓库里的副本 `resources/Editfile.wikitext`。

| # | 被核查的说法（位置） | 结果 | 依据（第一来源 + 交叉来源） |
|---|---|---|---|
| K1 | PES 2016–2021 EDIT 外观记录 72 B；PES 2015 为 68 B；14 项身体参数是 0x0C–0x12 的 14 个半字节，低半字节在前（§0-1、§2.1、§2.2） | **确认** | 4ccEditor `pes20.cpp` L157（`neck_len = read_data(0,4,…)`）至 L170（`head_dep = read_data(4,4,…)`）；`pes15.cpp` L13（`startByte + 0x44`）。4cc-studio `pes15.rs` `size: 68`、`pes16.rs` `size: 72`，`pes20.rs` NeckLength `bit_offset: 2016`（= 0xF0×8 + 0x0C×8）、HeadDepth `bit_offset: 2068`（= 0x12:4）。维基副本 L10908 "Entry Length: 72 (0x48) bytes"，并有 "0x0C:0 … Neck Length"。GodotFootball physique §1.2 表 |
| K2 | 存储 0–14，界面 = 存储 − 7，中性 7（§2.2、§3.3） | **确认** | 维基副本：226 处 "In-game value plus seven" 和 "PES: must be in range [0, 14]"，默认值 7。4ccEditor `main.cpp` L2633（`neck_len - 7`）、L3291（`+ 7`）。EvoTool v520 读时 −7、写时 +7。Devil Cold52 Notes 第 5 条 "7 is neutral"（原文已逐字核对，见 §3.3 更正） |
| K3 | PES 2020/21 球员记录 312 B，外观块在 +0xF0；PES 17–19 记录 188 B，外观块在 +0x74；球员区起点 0x78 / 0x7C；PES 15/16 外观区在 0x2AB9CC（§2.1） | **确认** | 4cc-studio `pes20.rs` `size: 312`、`AppearanceId bit_offset: 1920`；`pes17.rs` `size: 188`、`pes18/19.rs` `bit_offset: 928`。4ccEditor `pes20.cpp` L733 `current_byte += 0xF0+0x48`；`main.cpp` L1697、L1773（0x2AB9CC）、L1830（0x78）、L1886、L1948（0x7C） |
| K4 | PES 数据库表 `PlayerAppearance.bin` 每行 60 B（u32 PID + 56 B），库表偏移 = EDIT 偏移 − 8；原版 `dt00_x64.cpk` 中有 17,782 条（§0-2、§2.5） | **确认**（并补充 PES 15 模板） | GodotFootball `pes-re-face.md` L312-314 原文一致。4cc-studio `model.md` L228-236（"record bytes 128 to 184 on PES 18/19, 252 to 308 on PES 20/21"）。pes-db-generator 两个模板（_15、_16）都是 60 B |
| K5 | 模板 `PlayerAppearance_Base_16.bin` = ID 70101（`d5 11 01 00`）+ 7 个 `0x77`（§2.5） | **确认** | 本地 `od` 输出 `d5 11 01 00 77 77 77 77 77 77 77 00 …`，文件长 60 B；`player_appearance.py` 对 16–21 版用 `file_ver = 16` |
| K6 | eFootball PC 版 `PlayerAppearance.bin` 每行 64 B，u64 PID 在 0–7 且严格递增；文件长 2,410,752 B（= 37,668 × 64）；作者对 53 个 `.bin` 做过结构识别测试；文档自相矛盾，一面说表是"sparse"（§3.2a） | **确认** | Mod Studio `PESDB_DATABASE_FORMAT.md` L288-292、L306-326（原文一致）。EvoTool v520 `BLOCK = 64`（L16）。Player.bin 23,552 条可由 9,420,800 / 400 复算。u64 PID 的旁证：Devil Cold52 V2.5.1.4 "player ids are now read as uint64" |
| K7 | eF 行中 14 项身体参数在 0x08–0x0E（§0-3、§3.3、§7 `ef64`） | **未核实**（已在 §0-3 加【未核实】） | 只有 EvoTool v520 的注释（`// 0x08 - Cuerpo`）和写入代码支持；其读取代码 L98-99 实际是 `ReadUInt32()` 加上被注释掉的 `cardType`，等于从 0x04 开始读。没有任何真实 eF 文件的解码结果。原文定为"中"置信度，这一点合理 |
| K8 | EvoTool 的内部矛盾：读取用 `ReadUInt32`；写入先写 `ulong` 再写 `(uint)0`，然后 `Seek(BLOCK + 0x08)`；`AddNewEntry` 追加 60 B；外观解析是 2026-07-18 那次提交加入的（§3.2b） | **确认** | `PlayerAppearanceControllerV520.cs` L98-99、L561-567（`MemoryPlayerAppearance.Seek(BLOCK + 0x08, …)`）、L789-791（`+ 60`）；`Models/v520/PlayerAppearanceV520.cs` L16 `public ulong playerId`；`git show --stat fd8aef2` 新增了这两个文件（+843、+166 行）。Football-Championship `BLOCK_SIZE = 60`，样本文件 16 B（`ff 10 81 'WESYS' 00…`） |
| K9 | EvoTool 各 u32 字的位定义平移 4 字节后，与 PES 16–21 字段逐位重合（§3.2c 表） | **确认**（代码层面） | 逐行核对 EvoTool L102-259：`skinColor` aux28 bit 8-10、`overallStyle` aux34 bit 0-2、`overallLength` aux38 bit 0-2、`irisColor` aux3C bit 0-3、`fontStyle` aux3C bit 16-17 等，与 4cc-studio 和维基位置一致。这只能证明代码一致，不能证明 eF 数据也一致（见 K7） |
| K10 | WESYS：16 字节头，0x03 处是魔数 `WESYS`，0x08 是压缩长度，0x0C 是原始长度；密钥种子取自两个长度字段，所以不能原地重新加密（§1.1、§8-2） | **确认** | Mod Studio 文档 §1.1-1.2。Football-Championship 空表实物 `ff 10 81 57 45 53 59 53 …`，魔数确实在 0x03 |
| K11 | "先异或流加密，再 zlib 压缩"（§1.1 表 ③） | **更正**：先 zlib 压缩，再异或加密 | Mod Studio `ui/core/wesys.py` L82-88：`compressed = zlib.compress(...)` 之后 `crypt_current_payload(compressed, …, len(compressed), len(data))`；解包 L113-127：先解密，再 `zlib.decompress`。同时补充了"加密起始版本"的冲突：5.0.0（Mod Studio）对 6.0.0（RBsGameLab） |
| K12 | PES 2021 解码器 `v = 2n/14 − 1`，n = 15 时取 0；两项手臂参数在内存中对调，4cc 的标签与引擎一致；"Edited Physique" 标志从不检查；4830 人中 4600 多人全为 7（§2.2） | **确认** | GodotFootball `pes-re-physique.md` §1.2（"n = 15 → 0"，"+0x20 = byte 0x0F >> 4"，live 74008 例）。§7 Killed hypotheses 原文："4ccEditor arm_size/arm_len labels are swapped relative to the engine. Killed"。4ccEditor `pes20.cpp` L163-164 读取顺序 `arm_size` (bit 0) → `arm_len` (bit 4) 与此一致 |
| K13 | G 公式常数（k_h 0.223/0.277，k_L 0.018/0.019），183 cm → G = 1.0167；"前四个值与运行时样本一致"（§4.1、§4.3） | 常数与数值**确认**；措辞**更正** | physique §0 第 3 条、§3（.rdata 0x142a83530 = {0.018, 0.019, 0.223, 0.277}）。复算：175 → 0.97115，183 → 1.016725，185 → 1.027875，188 → 1.0446。源文的运行时样本只有 175、185、194、199 cm |
| K14 | 体重归一化：183 cm 时 Wmin 49.9、Wstd 73.25、Wmax 103.4；80 kg → 0.224，82 kg → 0.290；80 kg 时腹、肩、臂 × 1.022，大腿、小腿 × 1.011（§4.3） | **确认** | physique §1.3 的 12 行表：180 (48/71/100) 与 188 (53/77/109) 之间取 t = 3/8 线性插值，得 49.875 / 73.25 / 103.375。(80 − 73.25)/30.125 = 0.2241。A 表第 1 项：腹部、肩、臂正端 1.1，大腿、小腿正端 1.05 |
| K15 | 每档效果表（ShoulderHeight +7.86% / −2.86%，CalfSize +2.86%，WaistSize 只在 B 表生效，ThighSize y/z 等）与 LegLength 分解（G × 1.018，腹 × 0.882，腿 × 1.020；负端 × 0.981 / 1.124 / 0.979）（§4.2） | **确认** | 按 physique 附录 A 的表 A、B 逐项复算：sk_shoulder z 0.8 → 1.55；sk_leg yz 0.9 → 1.2；B 表第 18 项 sk_belly (0.9, 0.87) → (1.07, 1.11)；A 表第 7 项 waist 为 (1,1,1) → (1,1,1)；A 表第 12 项 a = 0.981、b = 1.018，sk_belly x 1.146 / 0.8665，腿 x 0.9978 / 1.002。锁骨转角符号与源文 2026-10-13 的更正一致（左 −8·s，右 +8·s） |
| K16 | 根运动 clamp [0.9166, 1.0833]，"约 194 cm 以上"开始滑步（§4.3） | clamp **确认**；阈值**更正**（近似值） | physique §4：clamp 值一致；原文 "Above roughly 193 cm"；按公式 G = 1.0833 时 h ≈ 194.9 cm |
| K17 | pesdb PES 2017 "RONALDO" id 2137：CF、OVR 92、183 cm / 75 kg、Age 20、Goal Poacher、右脚、BRAZIL • FREE；PES 2016、2018–2021 同一 id 均为 404；PES 2018–2021 和 eFootball 中按名字也找不到 R9（§0-5、§5.1） | **确认** | 2026-10-25 重新抓取 `pesdb.net/pes2017/players/ronaldo-2137`（数值逐项一致）。pes2016、2018、2019、2020、2021 的 `/players/ronaldo-2137` 均为 HTTP 404。按名字 "Ronaldo" 检索 pes2018–2021 和 efootball：普通 "Ronaldo" 条目（各版本中的 102827、104488、109566、110562、123096）逐一打开，分别是 DMF、GK（Vitória，194 cm）、RB（América Mineiro）等其他巴西球员，没有 R9；eFootball 中 "Nazario" 检索无结果。"Age 20 = 1996/97" 是本组推断（T4，出生日 1976-09-18 对得上） |
| K18 | 对照球员体型值：Hulk 15038（4/13/5/10/9/9/8/1/9/9/10，180/85，Trending 卡 53878485695166 数值相同）、Benzema 16786160（4/10/10/10/7/7/7/7/11/9/7，185/81）、Rivaldo Epic 88044145348046（7/7/6/8/5/2/3/7/3/2/14，186/75）；腿长等效身高 183.00 / 185.00 / 193.00（§5.2） | **确认** | 2026-10-25 重新抓取 4 个 pesdb 页面，11 项逐一一致；页面标题 "Rivaldo - Epic (Brazil 2002)"，"Featured Pack Brazil 2002"；页面确实只显示 11 项，不含头部 3 项 |
| K19 | 测试向量字节（§7、§8-5）：Hulk `D4 A5 99 18 99` + `A`；Benzema `A4 AA 77 77 9B` + `7`；Rivaldo `77 86 25 73 23` + `E` | **确认**（算术） | 按"低半字节在前"由 K18 的数值重新编码，三组完全一致。但这些字节在 eF 文件中的位置依赖 K7，K7 未核实 |
| K20 | 卡 ID：88044145348046 = 0x501360_020DCE，53878485695166 = 0x310090_003ABE，都大于 2³²（§3.2a） | **确认**（算术） | Python 复算。低 24 位分别为 134606 和 15038 |
| K21 | Rivaldo "基础 PID 134606"（§5.2、§7、§8） | **未核实**（已在 3 处加【未核实】） | `pesdb.net/efootball/players/rivaldo-134606` 返回 HTTP 404。pesdb 上 8 张 Rivaldo 卡的低 24 位都是 134606，所以这个数是屏蔽高位得来的。Mod Studio §7 原文："Do not try to recover a base card from a variant by masking off the high bits … only 1,135 of 3,701 variants (30.7%) map back correctly"。Hulk 15038 和 Benzema 16786160 的基础页存在，没有问题 |
| K22 | RBsGameLab（W5，2026-10-14）："107 customizable parameters"，"Appearance parameter data is distinct from licensed 3D face models and head assets"，可按 PID 整块克隆外观，v6.0.0 加了 WESYS 外壳，"fixed 400 bytes each"（§0-6、§6.1、§6.3） | 引文**确认**；"400 B 是 2026-10-09 更新后" 的归属**更正** | 页面逐字核对（发布日期 `datePublished 2026-10-14`）。400 B 一句针对 "eFootball 2027 changed the record layout"；2026-10-09 一句只说能力、技能等表换了位置。另有 Mod Studio：dt200 为 400 B，dt870（2026-08-17）为 392 B |
| K23 | Devil Cold52 编辑器各版本（W6、W7、W10）、隐藏编辑模式（W8）、PES 2021 社区体型经验值（W9） | **确认**；W6 版本归属**更正** | pesmodding 6 个页面重新抓取，逐字核对：V2.5.1.2 "Jawline: Writing problem Fixed it"、V2.5.1.3 "Eyebrown Colour…"、V2.5.1.4 "added Export PlayerAppearance Block"、2024 版 "player appearance feature is still disabled (data will be decoded again)"、2025 版 V4.4.0.0 同句、W8 "functions are not yet implemented … nearly useless"、W9 "Weight between 55 and 65 / Shoulder width -4 / Waist thickness between -1 and -3 / Thigh thickness between +1 and -1"（2023-09-14）、W10 "vadim_95 (Player.bin Decoding)"。"7–14 为正" 一句在 Notes 中，不属于 V5.2 |
| K24 | PES 2021 真人脸由"`face/real/<id>/face.fpk` 资源包是否存在"决定（id 先 `& 0x800bffff`），表中没有真人脸位（1,985 对 15,797）；`face_diff.bin` 960 B，含 33 个面部骨骼偏移；编辑脸用 `facial.fmdl`（1,600 顶点）加 30 骨晶格；编辑发型 896 种（§2.4） | **确认** | GodotFootball `pes-re-face.md` §0 第 1-3 条、L46-54、L63、L68、L99、L312-314、L372、L390 原文一致；1,985 + 15,797 = 17,782 与 K4 的记录数吻合 |
| K25 | CUE4Parse 中 `GAME_eFootball = GAME_UE4_26 + 29`（§4.4） | **确认** | 2026-10-25 抓取 `raw.githubusercontent.com/FabianFG/CUE4Parse/master/CUE4Parse/UE4/Versions/EGame.cs`，L130 一致 |
| K26 | 前额发型宽度"维基 3 bit"（§2.3 0x42 行） | **更正** | 维基副本 L10904 起的 PES 2021 表：0x42:0 为 2 bit "Hair Style Front"，0x42:2 为 1 bit "Unknown S"。4cc-studio 实测 3 bit（`bit_offset 352`）。已补进 §9 的宽度冲突列表 |
| K27 | EvoTool 的 "v5.2.0" 对应 2025-12（§3.1） | **部分确认** | Konami 官网 `versioninfo_v5-20` 页面把 v5.2.0 归在 2025 年，并写 "As the European league season approaches the midway point"，但页面没有写具体日期。`04_efootball_systems.md` 的 App Store 记录为 10.2.0 = 2025-12-04（同组 T3，已经核查过） |
| K28 | §7 schema 中门将手套 ID（`gk_gloves_id`，0x06:2）宽 10 bit，0x07:4 留 4 bit "Unknown A" | **更正**：改为 14 bit | 4ccEditor `pes20.cpp` L155 读、L885 写都是 `(2, 14)`，注释 "Extend to be 14 bits, using Unk A space"；4cc-studio `pes20.rs` `GlovesId bit_offset 1970, bit_width 14`（1970 − 0xF0×8 = 50 = 0x06:2）。§7 说明里的规则是"宽度取 4cc-studio 的实测值"，原 schema 只有这一项沿用了维基宽度。10 bit 时，ID ≥ 1024 会被解码截断。这是只在存档里出现的头部字段，编解码器默认禁止改它，所以对写入身体参数没有影响。复核时另用逐位移植的 4ccEditor `read_data` 按 `pes20.cpp` L146-201 的读取顺序模拟游标，推出 36 个头部/身体/穿着字段的位置，与 edit72 全部一致（`tools/tests/test_player_appearance_adversarial.py`；复核日期 2026-10-25） |

**结论**：
- 本文的核心格式结论都经得起复核：PES 72 B / 60 B 布局、身体半字节位置、+7 编码、PES 2021 骨骼缩放机制与数值、eF PC 版 64 B 行长和 u64 PID。原作者引用的源码行号和原文基本准确，没有发现编造的来源、ID 或数值。
- 共更正 7 处：加密与压缩的顺序、前额发型宽度、"前四个值"、滑步阈值、RBsGameLab 400 B 的归属、Devil Cold52 引文的位置、Mod Studio 提交日期。都属于细节或措辞，不影响写入方案，但"先压缩、后加密"会影响 codec 的实现顺序，必须按更正后的写。
- 标为【未核实】2 项：eF 身体字段在 0x08–0x0E（真实数据层面），以及 Rivaldo 基础 PID 134606。前者正是 §8 的实机检验要回答的问题。
- 复核补充（2026-10-25，外观编解码器对抗性复核）：K28，门将手套 ID 宽度由 10 bit 改为 14 bit，edit72 的 72 字节因此全部有归属；身体 14 项、穿着、编辑脸 89 个字段的位置与宽度，再次逐一对照 4cc-studio `pes20.rs`/`ingame_face.rs` 和 4ccEditor 的读取顺序，没有其他差异。这只说明 PES 布局与社区源码一致，**不改变 ef64 对 iOS 11.1.1 未验证的结论**。
