# 04 eFootball 球员数据系统研究（截至 2026-10-25，对应 iOS 版 11.1.1）

> **范围**
> - 研究对象：Konami《eFootball™》手游/主机版当前赛季（游戏版本 v6.1.x，App 版本 11.1.x）中**球员卡的数据系统**。
> - 覆盖内容：26 项能力、其他字段、比赛风格、技能、培养、增能（Booster）、主教练影响、总评公式，以及官方现有的罗纳尔多（R9）卡。
> - 本文件只研究"游戏规则与数据结构"，**不**设计 R9 的具体数值。数值设计见任务 #6；本文第 9 节只列出对设计的约束。
>
> **证据分级**（与 01、02 号笔记一致）
> - **[T1]** 官方或一手资料：Konami 官网版本说明与公告、Apple App Store 官方数据、网易官方页面。
> - **[T2]** 有信誉的媒体。
> - **[T3]** 社区数据库与逆向分析：pesdb.net、eFHUB、efootballexplained.com、ef-labo、GamingonPhone 等。
> - **[T4]** 本组的计算、推断或交叉验证。**T4 不是事实。**
>
> **必须先知道的三点**
> 1. Konami 不公开客户端内部字段的编码、偏移或公式。本文的"机制数值"大多来自社区逆向（T3），即使与官方说明一致也只标 T3。
> 2. 本组**没有**拿到 IPA，也没有做任何二进制分析。文中凡涉及"客户端字段编码"的内容都写明"未找到"或标 T4。
> 3. eFootball 每个大版本都会改系统。2026-08-13 的 v6.0.0 把比赛风格拆成"进攻型 + 防守型"两类，这是**当前版本与 2024–2025 资料最大的差别**。引用旧资料时一律注明年份。

---

## 0. 结论速览

| 项目 | 结论 | 分级 |
|---|---|---|
| iOS 11.1.1 是什么 | Konami 国际服 App `eFootball™`（bundle `jp.konami.pesactionmobile`），**2026-10-15** 上架，更新说明只有 "A number of issues were fixed"。<br>它是游戏版本 **v6.1.0（App 11.1.0，2026-10-08）的修复版**，属于 "eFootball 2027" 赛季（v6.0.0 起，2026-08-13）。<br>App 主版本号 = 游戏主版本号 + 5。 | T1 [S01][S02][S04] + T4 推断 |
| build 11.1.101 | 公开渠道**未找到**这个构建号。推测是 iOS 的 CFBundleVersion（T4）。需要 IPA 的 `Info.plist` 才能确认。 | 未找到 |
| 能力项数 | **26 项**：21 项场上能力 + 5 项门将能力。<br>卡面数值范围 **40–99**。增能（Booster）加在 99 封顶之后，社区逆向称上限 **120**。<br>官方确认：增能可让能力"超越 99"（v3.0.0）；总评自 v2.5.0 起可超过 100。 | T1 [S06][S15] + T3 [S25] |
| 能力英文名有两套 | 英式英语用 **Attacking Awareness**，美式英语用 **Offensive Awareness**。<br>其他差异：Set Piece Taking / Set Pieces，GK Parrying / GK Deflecting。<br>简中（Konami 国际服）："进攻意识"。 | T1 [S13] + T3 [S20] |
| 逆足 | 频率 4 档：Almost Never / Rarely / Occasionally / Regularly。<br>精度 4 档：Low / Medium / High / Very High。 | T3 [S20] |
| 状态波动（Form） | 3 档：Inconsistent / Standard / Unwavering。官方说它影响"状态（Condition）"的抽取概率。<br>社区逆向称比赛中不直接读取 Form。旧作 PES 2021 是 1–8 档。 | T1 [S06] + T3 [S25][S38] |
| 抗伤 | 3 档：Low / Medium / High。 | T3 [S20][S25] |
| 位置适性 | 13 个位置，**3 级**：高（深绿）/ 中（浅绿）/ 低（无色）。不是旧 PES 的 A/B/C。<br>位置训练：v3.4.0 起可随机提升最多 2 个位置；v6.1.0 新增"选择位置代币"（100 个换 1 次自选）。 | T1 [S08][S04] |
| 比赛风格 | v6.0.0 起分为**进攻型比赛风格 + 防守型比赛风格**，每名球员有其一或两者。<br>另有"AI 比赛风格"7 种：Trickster、Mazing Run、Speeding Bullet、Incisive Run、Long Ball Expert、Early Cross、Long Ranger。 | T1 [S03] + T3 [S20][S25] |
| 技能总数 | 当前数据库共 **67 种**球员技能。其中约 44 种可以通过技巧训练获得，其余为"特殊技能/Show Time 技能"。<br>客户端代码里另有 7 个未发布的占位技能（名为 "Dummy"）。 | T3 [S20][S25][S34] |
| 技能上限 | 卡面技能实测最多 **10** 个。另外可通过技巧训练加 **最多 5 个附加技能**（v2.5.0 官方）。合计 15（T2 媒体）。 | T1 [S15] + T2 [S35] + T3 实测 |
| 培养点数 | 能力点 = **2 × (最高等级 − 1)**。<br>每个培养类别第 n 次加点花费 **⌈n/4⌉** 点（1–4 次各 1 点，5–8 次各 2 点……）。每加 1 次，该类别内每项能力 +1，单项封顶 99。 | T3 [S22][S26]，两来源一致；eFHUB "0/66" 印证 |
| 培养类别 | 10 类：Shooting、Passing、Dribbling、Dexterity、Lower Body Strength、Aerial Strength、Defending、GK 1、GK 2、GK 3。具体对应关系见 §5.3。 | T3 [S22][S26] |
| 增能（Booster） | 常规增能共 **28 种**（官方 v5.2.0）或 **29 种**（pesdb 列表），存在冲突，见 §6.2。<br>增能槽可以用"随机/选择增能代币"制作，每个 +1；选择代币需要 100 个。 | T1 [S10][S14] + T3 [S21][S28] |
| 主教练影响 | 主教练对当前球队风格的熟练度（Team Playstyle Proficiency）按**百分比**缩放 26 项卡面值（不含逆足精度），结果仍封顶 99。<br>主教练自带的"增能"（如 Physical Contact +1）在 99 之后叠加。 | T3 [S25][S26][S22] |
| 总评公式 | 社区逆向公式：按位置加权 26 项能力 + 身高 + 逆足精度，分母 1000，向下取整。完整权重表见 §7。<br>本组用它复算 pesdb 的 97 张卡（基础总评 97/97 一致）、51 张卡的满级总评（51/51），以及 eFHUB 上一张卡的 13 个位置评分（13/13）。**全部一致。** | T3 [S22] + T4 验证 |
| 官方 R9 卡 | **未找到**。2026-10-25 检索 pesdb 全库（含"不可用球员"），没有 Ronaldo Nazário。<br>网上的 "Epic R9 103/112" 都是粉丝概念图或标题党。 | T3 [S20] + 见 §8 |
| 最近似的官方卡 | **Epic "Brazil 2002"** 卡包：Rivaldo、Kaká、Cafu、Roberto Carlos、Ronaldinho。<br>**Big Time Brazil 21 Jun '02** 卡：Ronaldinho。<br>同批次另有 Epic "Brazil 1994" Romário（CF）。 | T3 [S21] |
| 历史候选 card_id | `88044146203441` = `0x5013600F1B31`。<br>高位 `0x501360` 与当前 Epic Brazil 2002 / 1994 批次相同；低 24 位是 **990001**，属于**自定义基础球员 ID**。pesdb 查无此卡。 | T4 推断 + T3 查询 |

---

## 1. App 版本对照（任务 §8）

### 1.1 iOS 11.1.1 的官方元数据 [T1]

数据来源：Apple iTunes Lookup API，2026-10-25 查询 [S01]，以及 App Store 版本历史页面 [S02]。

| 字段 | 值 |
|---|---|
| trackName | eFootball™ |
| bundleId | `jp.konami.pesactionmobile` |
| App Store ID | 1117270703 |
| version | **11.1.1** |
| currentVersionReleaseDate | **2026-10-15T04:57:46Z** |
| releaseNotes | "A number of issues were fixed. Check out the News section in-game for further details." |
| minimumOsVersion | 15.0 |
| fileSizeBytes | 3,484,689,408 |
| sellerName | Konami Digital Entertainment Co., Ltd. |
| 语言 | languageCodesISO2A 共 14 项，其中 "ZH" 出现两次（推测为简体和繁体，T4），另有 EN、JA 等 |

- us、jp、gb、hk、br 五个区的商店返回同一版本和同一日期。
- 中国区商店**查不到**该 App。
- 网易代理的国服"实况足球"是另一个 App：ID 1347776121，bundle `com.netease.pes`，2026-10-25 查询时版本为 11.0.0（2026-09-23）[S42]。
- **研究基准 "eFootball iOS 11.1.1" 指的是 Konami 国际服 App，不是网易国服。**

### 1.2 App 版本与游戏版本对照

App Store 各版本的更新说明，与 Konami 官网版本说明的条目**逐字一致**。据此可以对应如下 [S02][S03][S04][S10][S12][S13][S14]。

| App 版本 | 上架日 | 游戏版本 | 赛季 | 与球员数据相关的变更 [T1] |
|---|---|---|---|---|
| 9.2.0 | 2024-12-05 | v4.2.0 | 2025 | 可用 GP 兑换道具；比赛中角色的适配值计入增能、位置适性和主教练熟练度。 |
| 9.4.0 | 2025-04-17 | v4.4.0 | 2025 | 新增 "Advanced Skill Training"；可以单独删除已学的附加技能。 |
| 10.0.0 | 2025-08-14 | v5.0.0 | 2026 | 主教练新增 Link-up Play；"Attacking Awareness" 开始影响进攻跑动的加速。 |
| 10.2.0 | 2025-12-04 | v5.2.0 | 2026 | 可制作的增能扩充到 28 种；随机增能按注册位置抽取。 |
| **11.0.0** | **2026-08-13** | **v6.0.0** | **2027** | 比赛风格拆为进攻型和防守型；新增多种（以防守为主的）风格；位置代币不能再学注册位置；培养界面显示比赛计划下的能力；部分语言的技能名和风格名有改动。 |
| **11.1.0** | **2026-10-08** | **v6.1.0** | 2027 | 新增 "Select Skill Token" 和 "Select Position Token"；移除 Advanced Skill Token，每 1 个换 20 个 Select Skill Token [S05]。 |
| **11.1.1** | **2026-10-15** | v6.1.x 修复版（T4） | 2027 | 只写了"修复若干问题"。 |

- **规律（T4）**：App 主版本号 = 游戏主版本号 + 5，即 9↔v4、10↔v5、11↔v6。次版本号对应游戏的 x.y（11.1 ↔ v6.1）。
- **构建号**：APKMirror 搜索摘要中，安卓 10.5.1 的 versionCode 写作 `310050101`（T3，没有打开原页面核实）。iOS 的 "11.1.101" 可能遵循类似的"补丁×100+构建"写法，**未证实**。
- **社区分析也基于这个版本**：efootballexplained.com 全站标注 "Checked on game version 11.1.1 (2026-10)" [S25]。eFHUB 在 2026-10-17 发表的机制分析文章，基于对一个游戏包（SHA-256 前缀 `2ea6b0b5…`）的静态分析和 ARM64 指令级实验 [S24]。这两份是目前与研究基准版本最接近的社区逆向资料，但都自称可能有误（T3）。

---

## 2. 26 项能力字段

### 2.1 名称对照表

字段说明：
- `key`：pesdb、eFHUB 页面数据中使用的键名（T3）。**不是**客户端字段名；客户端字段名未找到。
- "简中"：pesdb 简体中文界面的用语（T3）。其中"进攻意识、防守意识、防守参与度、截球（技能）、偷猎者（风格）、锋线施压（风格）、传统十号位置（风格）"已与 Konami 官网简中版本说明对上 [S17]，所以判断 pesdb 的简中多半取自游戏文本，但并未逐项核对。
- "国服"：网易官方页面上实际见到的写法（T1，仅限能找到的）。

| # | key | 英式英语（UK） | 美式英语（US）差异 | 简中（国际服） | 繁中 | 日文 | 所属培养类别 |
|---|---|---|---|---|---|---|---|
| 1 | offensive_awareness | **Attacking Awareness** | **Offensive Awareness** | 进攻意识 | 進攻意識 | オフェンスセンス | Dexterity |
| 2 | ball_control | Ball Control | = | 控球 | 控球 | ボールコントロール | Dribbling |
| 3 | dribbling | Dribbling | = | 盘球 | 盤球 | ドリブル | Dribbling |
| 4 | tight_possession | Tight Possession | = | 紧密控球 | 緊密控球 | ボールキープ | Dribbling |
| 5 | low_pass | Low Pass | = | 地面传球 | 地面傳球 | グラウンダーパス | Passing |
| 6 | lofted_pass | Lofted Pass | = | 空中传球 | 高吊傳球 | フライパス | Passing |
| 7 | finishing | Finishing | = | 射门 | 射門 | 決定力 | Shooting |
| 8 | heading | Heading | = | 头球 | 頭球 | ヘディング | Aerial Strength |
| 9 | set_piece_taking | Set Piece Taking | Set Pieces | 罚定位球 | 定位球 | プレースキック | Shooting |
| 10 | curl | Curl | = | 弧线球 | 弧線球 | カーブ | Shooting |
| 11 | defensive_awareness | Defensive Awareness | = | 防守意识 | 防守意識 | ディフェンスセンス | Defending |
| 12 | tackling | Tackling | = | 抢球 | 搶截 | ボール奪取 | Defending |
| 13 | aggression | Aggression | = | 积极性 | 積極性 | アグレッシブネス | Defending |
| 14 | defensive_engagement | Defensive Engagement | = | 防守参与度 | 防守參與 | 守備意識 | Defending |
| 15 | gk_awareness | GK Awareness | = | 守门员意识 | 守門員意識 | GKセンス | GK 1 |
| 16 | gk_catching | GK Catching | = | 守门员接球能力 | 守門員接球能力 | キャッチング | GK 3 |
| 17 | gk_parrying | GK Parrying | **GK Deflecting** | 守门员扑救 | 守門員撲救能力 | クリアリング | GK 2 |
| 18 | gk_reflexes | GK Reflexes | = | 守门员扑救反应 | 守門員反應 | コラプシング | GK 3 |
| 19 | gk_reach | GK Reach | = | 守门员臂展 | 守門員臂展 | ディフレクティング | GK 2 |
| 20 | speed | Speed | = | 速度 | 速度 | スピード | Lower Body Strength |
| 21 | acceleration | Acceleration | = | 加速（官网简中写"加速度"） | 加速 | 瞬発力 | Dexterity |
| 22 | kicking_power | Kicking Power | = | 脚下力量 | 腳下力量 | キック力 | Lower Body Strength |
| 23 | jumping | Jumping | = | 跳起 | 跳躍 | ジャンプ | Aerial Strength + GK 1 |
| 24 | physical_contact | Physical Contact | = | 身体接触 | 身體對抗 | フィジカルコンタクト | Aerial Strength |
| 25 | balance | Balance | = | 平衡 | 平衡 | ボディコントロール | Dexterity |
| 26 | stamina | Stamina | = | 体力 | 體力 | スタミナ | Lower Body Strength |

来源：pesdb 的 en、en-US、zh-Hans、zh-Hant、ja 五个界面 [S20][S21]；Konami 官网的英式、美式、简中版本说明 [S13][S17]。

- 英式与美式的差异是 Konami 官网实证：同一条 v5.0.0 说明，英式页面写 "Attacking Awareness" Player Stat，美式页面写 "Offensive Awareness" Player Stat [S13]。
- 日文门将能力的命名沿用 PES 旧称，与英文并非一一直译（T3）。

### 2.2 数值范围与上限

| 项目 | 规则 | 分级 / 来源 |
|---|---|---|
| 卡面能力值 | **40–99**。外场球员的 5 项 GK 能力在所有抽查卡上都是 40。 | T3 [S21] 实测；[S25] 写明"卡面值加状态后夹在 40–99" |
| 增能后 | 可以超过 99。官方原文："Boosters enhance certain Player Stats and allow players to perform beyond the normal ceiling of 99." | T1 [S06] |
| 增能上限 | 增能点在 99 封顶**之后**相加，最高 **120**（社区逆向） | T3 [S25] |
| 总评上限 | v2.5.0 起，显示总评可以超过 100 | T1 [S15] |
| 能力的边际收益 | 社区逆向称：Speed、Acceleration、Dribbling、Tight Possession，以及四项精度能力（Finishing、Low Pass、Lofted Pass、Heading）共用一条曲线。60 以下收益很小，60–89 收益最大，90 以上每点约只值 40%。部分能力是"阶梯式"生效。 | T3 [S25] |

### 2.3 部分能力在 11.1.1 中的作用

以下为社区逆向结论（T3 [S25][S24]），仅供数值设计参考。

| 能力 | 作用摘要 |
|---|---|
| Attacking Awareness | 决定电脑队友何时启动跑位、抢回球后多快转入进攻、定位球时多早移动。<br>官方补充：v5.0.0 起它也影响进攻跑动的加速（T1 [S13]）。 |
| Speed / Acceleration | Speed 决定冲刺极速，Acceleration 只影响达到极速的快慢，不改变极速。 |
| Dribbling | 与 Speed 共同决定带球极速，并让假动作更快。 |
| Tight Possession | 让触球更早，转身时节奏更快。 |
| Finishing | 让大偏差更少出现。但干净击球时，最大偏差由射门力量决定，而不是 Finishing。 |
| Kicking Power | 每点约让远射快 0.3 km/h，对传球几乎没有影响。 |
| Curl | 最多让旋转增加约 40%。 |
| Set Piece Taking | 只在定位球中起作用。 |
| Jumping | 每点约让跳顶高度增加 0.74 cm。 |
| Physical Contact | 主要是双方数值的相对比较。 |
| Balance | 被撞到踉跄（未倒地）时，低 Balance 会让球员僵直更久。 |
| Stamina | 只决定体能槽的大小，不影响消耗速度。 |
| 身高 | **不影响**速度。身高 ≤165 cm 时触球、急停和变向略快；高个子空中够得更高。 |
| 体重 | 比赛逻辑中**没有用到**体重，只传给 3D 体型。这一点与 02 号身体笔记相关。 |

---

## 3. 其他球员字段

### 3.1 名称与档位一览

| 字段 | 英式 UI 档位 | 美式 UI 名称 | 简中（国际服） | 页面数据编码 | 来源 |
|---|---|---|---|---|---|
| Weak Foot Usage 逆足使用频率 | Almost Never / Rarely / Occasionally / Regularly | Non-Dominant Leg Usage | 非惯用脚使用频率：几乎从不 / 很少 / 偶尔 / 经常 | 文本 | T3 [S20] |
| Weak Foot Accuracy 逆足精度 | Low / Medium / High / Very High | Non-Dominant Leg Precision | 非惯用脚精准度：低 / 中 / 高 / 非常高 | pesdb 页面数据 `weakFootAccuracy` 取 **0–3**（Medium=1，已核） | T3 [S22] |
| Form 状态波动 | Inconsistent / Standard / Unwavering | Form | 状态波动：不稳定 / 标准（官网简中作"普通"）/ 稳定 | 文本 | T1 [S06][S17] + T3 |
| Injury Resistance 抗伤 | Low / Medium / High | = | 抗受伤程度：低 / 中 / 高 | 文本 | T3 |
| Stronger Foot 惯用脚 | Right Foot / Left Foot | Dominant Leg | 惯用脚：右脚 / 左脚 | 文本 | T3 |
| Height / Weight / Age | cm / kg / 岁 | = | 高度 / 体重 / 年龄 | 整数 | T3 |

- **客户端内部编码未找到。** 旧作 PES 2021 在 pesdb 上以数字显示：逆足频率 1–4、逆足精度 1–4、Form 1–8、抗伤 1–3。例如 C 罗为 3/4/7/2 [S38]。eFootball 是否仍用这种编码存储，只有拿到 IPA 数据才能确认。
- "年龄"显示的是**卡面所代表时期的年龄**，不是现在的年龄。例如 Adriano Epic 05-06 显示 23 岁 [S21]。2002 年决赛版 R9 应为 25 岁（见 01、03 号笔记）。

### 3.2 逆足在比赛中的效果（T3，11.1.1 逆向 [S25]）

**逆足频率（level 1–4）**

| 项目 | level 1 | level 2 | level 3 | level 4 |
|---|---|---|---|---|
| 传中或高球朝逆足侧偏 >15° 时，允许用逆足的概率 | 0% | 33% | 67% | 100% |

- level 4 时，弧线射门等技能射门可以两只脚都作为候选。
- 等级高**不会**强迫球员在不需要时用逆足。

**逆足精度（level 1–4）**：逆足踢球时从踢球能力上扣分。

| 扣分项目 | level 1 | level 2 | level 3 | level 4 |
|---|---|---|---|---|
| 一般传射 | −15 | −11 | −7 | −3 |
| 脚背大力 | −25 | −17 | −10 | −3 |
| Curl | −24 | −17 | −10 | −3 |
| 逆足第一脚停球（Ball Control） | −20 | −13 | −6 | 0 |
| 逆足射门速度上限（km/h） | 100 | 107 | 113 | 120 |

- 头球不受逆足影响。
- 低于 level 4 时，部分传中或射门动作（如 Edged Crossing）会被强制换成强势脚。
- 官方旁证：Blitz Curler 只用强势脚触发，"unless the player has a 'Very High' Weak Foot Accuracy"（T1 [S07]）。
- **逆足精度也参与总评计算**，见 §7。

### 3.3 Form（状态波动）与 Condition（状态）

- **官方说法 [T1 S06]**：v3.0.0 降低了 Standard / Inconsistent 球员被抽到 "Poor" 或更差状态的概率；Unwavering 的概率不变。并且 Inconsistent 球员的 Live Update Rating 在 "B" 或以上时，状态至少为 "Normal"。
- **社区逆向 [T3 S25]**：比赛逻辑中**不读取** Form，只在卡数据校验（checksum）中出现。真正影响比赛的是**状态箭头**。
  - 最好的箭头，对 ≤60 能力的球员加 +15 / +11 / +6（意识类 / 技术类 / 身体类）；对 ≥95 的球员只加 +3 / +2 / +2。
  - 最差的箭头最多扣 12 点。
  - 箭头还会让体能槽变化约 ±10%。
  - Aggression 与逆足不受影响；GK 能力只对门将变化。
- 两种说法**并不矛盾**：Form 影响的是在线更新时状态的分配，而不是比赛引擎。对单机项目，可以把 Form 当作"状态抽取规则的输入"。
- **Live Update Rating（实时更新评价）**：A–E 五档（pesdb 筛选项，T3）。官方提到过 "B or higher"、"A"、"C or lower"（T1 [S07]）。
  - 第三方指南称：Epic、Big Time、Legendary 卡固定为 "B"；Show Time 卡随现实表现变化（T3 [S29]）。

### 3.4 抗伤（T3 [S25]）

| 档位 | 非接触伤病概率倍率 | 伤病负荷累积倍率 | 接触伤病中严重度减半的概率 | 极重接触被升级为更重伤的概率 |
|---|---|---|---|---|
| 1 | ×1.5 | ×1 | 30% | 5% |
| 2 | ×1.0 | ×0.8 | 50% | 2% |
| 3 | ×0.5 | ×0.5 | 70% | 0 |

- 非接触伤病只会在体力 <20% 时发生。

### 3.5 体型参数（转交身体组，T3）

- pesdb 卡页的 "Physique Parameters" 有 **11 项整数**：Neck Length、Neck Size、Shoulder Height、Shoulder Width、Chest Measurement、Waist Size、Arm Size、Arm Length、Thigh Size、Calf Size、Leg Length。
- 抽查到的取值在 **1–14** 之间。例如 Adriano 颈粗 14，Rivaldo 腿长 14、腰 2，Romário 臂长 1。这与任务书"0–14"的说法吻合。
- pesdb **没有**显示 HeadLength、HeadWidth、HeadDepth 三项。
- pesdb 还显示 5 项派生量（单位 cm）：Leg Coverage、Arm Coverage、Peak Jump Height、Torso Collision Width、Leg-length Equivalent Height。例如 Adriano 189 cm 对应 "Leg-length Equivalent Height 192"。
- 这些派生量说明**体型参数会进入碰撞或判定计算**。改体型可能改变比赛表现，不只是外观。数据见 §8.3 表格末行。

### 3.6 位置与位置适性

**位置**：GK、CB、LB、RB、DMF、CMF、LMF、RMF、AMF、LWF、RWF、SS、CF，共 13 个。

**适性分三级 [T1 S08]**：
- 深绿 = 高（high level of Proficiency）。注册位置永远是高。
- 浅绿 = 中（intermediate）。
- 无色 = 低。
- pesdb 的 UI 写作 "Full Familiarity / Partial Familiarity"（T3）。

**不在高适性位置时的惩罚（T3 [S25]，11.1.1 逆向）**：

| 能力组 | 浅绿 | 无色 |
|---|---|---|
| Dribbling、Ball Control、Finishing、Low/Lofted Pass、Heading、Tackling、Set Piece Taking、Curl、Speed、Jump | ×0.92 | ×0.80 |
| Attacking Awareness、Defensive Awareness、Kicking Power、Acceleration、5 项 GK | ×0.94 | ×0.82 |
| Physical Contact、Balance、Stamina | ×0.96 | ×0.84 |
| Defensive Engagement、Tight Possession、Aggression、逆足精度 | 不受影响 | 不受影响 |

- 惩罚只按卡面值计算；增能点不打折。
- 比赛风格在不兼容的位置上**大部分失效**（约 450 处检查会判断位置）。左右同组，比如 LWF 和 RWF 视为同一位置。

**位置训练 [T1]**：

| 版本 | 规则 |
|---|---|
| v3.4.0 | 用 "Position Training Programs" 随机提升**最多 2 个**位置的适性。<br>可学的位置是：深绿或浅绿的位置，加上与卡面位置相邻的位置。CF 的相邻位置是 SS / RWF / LWF。<br>Trending 卡不能进行位置训练 [S08]。 |
| v6.0.0 | 位置代币不能再学"卡面注册位置" [S03]。 |
| v6.1.0 | 新增 "Select Position Token"：集齐 100 个可任选一个可学位置 [S04][S05]。 |

### 3.7 比赛风格

**结构变化 [T1 S03]**：
- v6.0.0 起，比赛风格分为 "Attacking Playing Style" 和 "Defensive Playing Style"，每名球员有其一或两者。
- 官方举例：进攻时用 "Goal Poacher"，防守时用 "Front Line Pressure"。
- v6.0.0 还移除了 "Classic No. 10" 降低防守参与的效果。
- pesdb 卡页显示为两个独立字段：`attackingPlayingStyle` 和 `defensivePlayingStyle`。没有特色的一侧为 "Basic"（T3 [S21]）。

**进攻型比赛风格（22 项，含 Basic）**
- 列表顺序取自 pesdb 筛选器 [S20]。
- 兼容位置取自 efootballexplained [S25]，标为 11.1.1 代码中的 "Compatible Positions"（T3）。
- Classic No. 10 的位置另有官方依据：v3.4.0 起为 SS、AMF，CMF 不再适用（T1 [S08]）。
- 美式英语名称差异来自 pesdb en-US（T3）。

| 英式名 | 美式名 | 简中（国际服） | 兼容位置 | 作用摘要（T3 [S25]，括号内另注 T1） |
|---|---|---|---|---|
| Basic | Basic | 基础战术 | — | 无特色 |
| Goal Poacher | **Adv. Striker** | 偷猎者 | CF | 主要改变电脑的跑位选择：可在更远处获得"主前插"任务。官方原文："makes runs in behind the defence"（T1 [S03]） |
| Dummy Runner | = | 虚假跑位 | CF, SS, AMF | 禁区附近有对手贴身时，电脑会关闭其 6 种无球动作 |
| Fox in the Box | **Poacher** | 禁区之狐 | CF | 控球时最多只跟随全队横移的 30%；为其保留禁区中路位置 |
| Prolific Winger | = | 高产翼锋 | LWF/RWF | 持球时不受两条横向限制，可贴边线 |
| Classic No. 10 | = | 传统十号位置 | SS, AMF（T1） | 其持球时，电脑按高一级进攻等级行动 |
| Hole Player | = | 影子前锋 | SS, AMF, CMF, LMF/RMF | 优先获得插向防线的单人跑位，可更深位置起跑 |
| Box-to-Box | = | 全能中场 | DMF, CMF, LMF/RMF | 活动范围更长更宽 |
| Anchor Man | = | 靠山 | DMF | 不参与进攻跑动，几乎总是两名留守者之一 |
| Extra Frontman | = | 额外前锋 | CB | 唯一可以压上进攻的中卫 |
| Attacking Full-back | **Offensive Fullback** | 进攻型后卫 | LB/RB | 更早、更远地套边，进攻站位最多高 10 m |
| Defensive Full-back | **Defensive Fullback** | 防守型后卫 | LB/RB | 留守，进攻站位最多深 5 m |
| Deep-lying Forward | Deep-Lying Forward | 组织型前锋 | CF, SS | 在两种支援情形下站得更深 |
| Creative Playmaker | = | 有创意的中场指挥官 | SS, LWF/RWF, AMF, LMF/RMF | 改变跑动类型权重，更愿意转移长传 |
| Build Up | = | 组织大师 | CB | 持球时更易长传转移 |
| Roaming Flank | = | 自由侧翼 | LWF/RWF, LMF/RMF | 持球时向中路收的倾向随推进而增强 |
| Cross Specialist | = | 传中专家 | LWF/RWF, LMF/RMF | 电脑选择传中的分数 +30 |
| Orchestrator | = | 指挥官 | DMF, CMF | 更愿意转移长传；不会被派出单人前插 |
| Full-back Finisher | **Fullback Finisher** | 迭瓦式终结者 | LB/RB | 内收前插，可插到球内侧 8 m |
| Target Man | = | 站桩式中锋 | CF | 唯一的"做支点"目标，也是首选空中目标 |
| Build-up GK | = | 组织型门将 | GK | 队友持球时站到后卫线后 5–15 m |
| High Line GK | = | 高位防线门将 | GK | 控球时站位正对球门中线 |

**防守型比赛风格（17 项，含 Basic）**

| 英式名 | 美式名 | 简中（国际服） | 兼容位置 | 作用摘要（T3 [S25]） |
|---|---|---|---|---|
| Basic | Basic | 基础战术 | — | — |
| Box-to-Box | = | 全能中场 | DMF, CMF, LMF/RMF | 同上（两类列表都有） |
| Anchor Man | = | 靠山 | DMF | 同上 |
| The Destroyer | = | 破坏型中场 | CB, LB/RB, DMF, CMF（代码还接受 LMF/RMF） | 更早、从更多角度参与逼抢，视为更具侵略性 |
| Attacking GK | **Offensive GK** | 进攻型守门员 | GK | 出击判断的余量更小（0.45 s，常规 0.65 s） |
| Defensive GK | = | 防守型守门员 | GK | 谨慎出击，最多离门 4 m |
| Press Back | = | 后撤压迫 | CF, SS, LWF/RWF | 前锋在对方半场就可被派去逼抢，回追更积极 |
| **Front Line Pressure** | = | 锋线施压 | CF, SS, LWF/RWF | 唯一在对方半场执行前线逼抢任务的角色 |
| Front Line Poacher | = | 前场埋伏者 | CF, SS, LWF/RWF | 前锋版 Pass Disruptor，优先切断传球线路 |
| **Attack Outlet** | = | 突前球员 | CF, SS, LWF/RWF, AMF | 无球时不进入 4–8 人的横排防线，前线站位最多高 5 m。eFHUB 文本："Avoids defending to save energy … remains up front waiting to attack" |
| All-action Defender | = | 全能后卫 | DMF, CMF, LMF/RMF, AMF | 在通常条件不满足时仍会作为第二逼抢人 |
| Pass Disruptor | = | 传球拦截者 | DMF, CMF, LMF/RMF, AMF | 电脑首选的传球线路切断者 |
| Covering Role | = | 补位型球员 | CB, LB/RB, DMF, CMF | 优先补防持球防守者身边的空当 |
| High Line Master | = | 高位防线大师 | CB, LB/RB | 盯人点最多落后防线 5 m |
| Tough Marker | = | 紧密盯防者 | CB, LB/RB, DMF | 紧贴盯人，不被抽去协防 |
| Deep Defender | = | 纵深防守者 | CB, LB/RB | 盯人时可退到防线后 7 m |
| Sweeper GK | = | 清道夫门将 | GK | 在 Attacking GK 基础上，最远可站到离门 42 m |

- 拥有新防守风格的卡还很少。2026-10-16 卡表中：Front Line Pressure 40 张、Front Line Poacher 32 张、Attack Outlet 未统计、Press Back 4 张（T3 [S25]）。

**AI 比赛风格（7 项）**

| 英式名 | 美式名 | 简中 | 作用（T3 [S25]） |
|---|---|---|---|
| Trickster | = | 魔术师 | 电脑多一种花式动作，更常尝试花式 |
| Mazing Run | **Agility King** | 惊人突破 | 电脑更多盘带，对贴身盘带动作评分更高 |
| Speeding Bullet | = | 疾速子弹 | 电脑更多选择直线带球推进 |
| Incisive Run | = | 内切突破 | 电脑可选弧线射门选项，起始概率 35%，相当于 Long-range Curler |
| Long Ball Expert | = | 长球专家 | 电脑更多长传、更少盘带 |
| Early Cross | = | 提前传中 | 电脑传中分数 +30，五项决策中按"有 Pinpoint Crossing"处理。efx 写作 "Early Crosser" |
| Long Ranger | = | 重炮手 | 电脑远射分数 +60，是单项最大加成 |

- AI 风格**没有位置检查**，只影响电脑做出的选择。
- 一张卡的 AI 风格实测最多 3 个，例如 Epic Roberto Carlos 有 Mazing Run、Speeding Bullet、Long Ranger（T3 [S21]）。

---

## 4. 技能系统

### 4.1 数量、上限与获取途径

**数量**
- pesdb 当前筛选器（eFootball 2027 数据）列出 **67 种**球员技能（T3 [S20]）。
- efootballexplained 称 11.1.1 代码中另有 7 个**未发布**的占位技能，文件中名为 "Dummy"：Spring Cut、Thames、Nile、Amazon、Mekong、Congo、Indus。其效果可能在发布前改变（T3 [S25]）。

**上限**
- 卡面技能：实测 Epic、Big Time、Show Time 卡都是 10 个，没有见过超过 10 个的卡（T3，抽查约 60 张）。**客户端是否硬性限制为 10，未找到官方说明。**
- 附加技能：最多 5 个。v2.5.0 官方原文："Added a new feature 'Skill Training' for users to add a maximum of 5 random Additional Skills onto players"（T1 [S15]）。
- 合计 15 个（T2 [S35]）。

**获取途径的版本演变 [T1]**

| 版本 | 变更 |
|---|---|
| v2.5.0 | Skill Training Program，随机习得；Legacy Transfer 可以把经验和附加技能转移给另一名球员 [S15] |
| v4.2.0 | 可以用 GP 兑换 Skill Training Programs [S11] |
| v4.4.0 | 新增 Advanced Skill Training，可选技能的"类别"；可以单独删除已学的附加技能 [S12] |
| v6.1.0 | Advanced Skill Token 废止，改为 Select Skill Token：集齐 100 个可在 Skill Training 中任选一项。旧代币每个换 20 个 [S04][S05] |

- 不能学已有的技能（T3 指南）。Trending（POTW 等）卡不能做技巧训练（T3 [S29]）。

**特殊技能与普通技能的区分**
- Konami 没有公布哪些技能"不能训练"。
- Operation Sports（2026-08-13）把 19 个技能列为 "Showtime skills … not available through training"，并按 6 类列出了可训练技能（T2/T3 [S34]）。
  - 该文自称"40 个"可训练技能，但实际列出 44 个不重复的名称，**存在矛盾**。
- 网易国服官方活动页把旋风盘带、瞬间加速、无定型射门、落叶式横传、后防领袖、到位传球、伸脚抢截、炮弹式头球、掠地瞬击等称为 **"特殊技能"**（T1 [S37]）。
- 2026 年新出现的 Tap Trick、Snap Strike、Power Tackle、Shadow Hunt 四项，没有出现在任何可训练列表中。新闻片段称 Snap Strike 和 Power Tackle 是 "Showtime skill"（T3 [S41]），本组推断四项都属特殊技能（T4）。
- **客户端用什么字段编码"特殊"标记，未找到。**

### 4.2 全部 67 种技能

列说明：
- "类别"：可训练技能按 [S34] 的分组标注；特殊技能标"特殊"。
- "效果"：主要取自 efootballexplained（T3，11.1.1 逆向）的数值，以及 eFHUB 收录的游戏内说明文本（T3 [S23]）。
- 所有技能加成都**在 99 封顶之前**相加：已到 99 的能力不再受益（T3 [S25]）。

**盘带**

| # | 英式名 | 美式差异 | 简中（国际服）/ 国服 | 类别 | 效果 |
|---|---|---|---|---|---|
| 1 | Scissors Feint | | 剪刀脚假动作 | 训练·盘带 | 使用专属的快速剪刀脚，同等 Dribbling 下快约 6–7% |
| 2 | Double Touch | | 两次触球 | 训练·盘带 | 专属快速两次触球，Dribbling 60 以上时快约 9–10% |
| 3 | Flip Flap | | 牛摆尾 / 国服"牛尾巴" | 训练·盘带 | 摇杆方向稳定触发牛尾巴，动作快约 10% |
| 4 | Marseille Turn | | 马赛回旋 | 训练·盘带 | 专属马赛回旋，快约 20% |
| 5 | Sombrero | | 挑球过顶 | 训练·盘带 | 挑球过人和彩虹过人成功率提高（两次判定 Ball Control +10）；接地面传球时也可挑球 |
| 6 | Chop Turn | | 脚后跟磕球变向 | 训练·盘带 | 快约 16–18%；横向输入时也会做 Chop Turn |
| 7 | Cut Behind & Turn | | 向后切球并转身 | 训练·盘带 | 解锁 Cut Behind 和专属停球转身，动作计时按 Dribbling +20 |
| 8 | Scotch Move | **Inside Bounce** | 磕球过人 | 训练·盘带 | 解锁 Scotch Move，计时按 Dribbling +20 |
| 9 | Tap Trick | | 后脚磕球变向 | 2026 新，推断特殊 | 解锁 Tap Trick，摇杆侧向约 56–124° 时触发 |
| 10 | Sole Control | | 足底控球 | 训练·盘带 | 基础切球的计时改为 Dribbling +20（无技能时为 −20）；对手 4 m 内可做脚底两次触球 |
| 11 | **Momentum Dribbling** | | 旋风盘带 | 特殊 | 持球进入对方半场约 18 m 以上区域时，Dribbling、Tight Possession、Speed、Acceleration 各 **+6**（两份独立逆向一致 [S24][S25]） |
| 12 | **Acceleration Burst** | | 瞬间加速 | 特殊 | 静止或慢速带球时可做快速 Sharp Touch，并有专属动作；**不直接加** Speed 或 Acceleration [S24] |
| 13 | Magnetic Feet | | 磁力脚 | 特殊 | 持球时 5 m 内每有 1 名对手，Dribbling、Tight Possession、Physical Contact、Balance 各 +2，最多 4 人（+8） |

**射门与头球**

| # | 英式名 | 美式差异 | 简中（国际服）/ 国服 | 类别 | 效果 |
|---|---|---|---|---|---|
| 14 | Heading | | 头球 | 训练·通用 | 头球射门按 Heading +5 瞄准，下压头球频率加倍；**不**增加跳跃或争顶能力 |
| 15 | Bullet Header | | 炮弹式头球 | 特殊 | 头球射门速度上限 +4 km/h；转身或别扭姿势时精度损失更少；被干扰时误触减半 |
| 16 | Long-range Curler | Long-Range Curler | 远距离弧线球 | 训练·射门 | Controlled Shot 速度最多 +3 km/h；弧线射门按 Finishing +5 |
| 17 | Blitz Curler | | 弧线落叶射门 | 特殊 | 力量 ≥50% 的 Controlled Shot 变成强上旋弧线；只用强势脚，逆足精度 Very High 例外（T1 [S07]） |
| 18 | Chip Shot Control | | 吊射控制 | 训练·射门 | 吊射按 Finishing +5；奔跑中仰角损失大幅减少 |
| 19 | Knuckle Shot | | 落叶球射门 | 训练·射门 | Stunning Shot 力量 50–65% 且球几乎静止时为电梯球，Finishing +5 |
| 20 | Dipping Shot | | 急坠射门 | 训练·射门 | 力量 20–50% 的强势脚 Stunning Shot 变为下坠球，Finishing +5 |
| 21 | Rising Shot | | 急升射门 | 训练·射门 | 力量 65–95% 的强势脚 Stunning Shot 变为上升球，Finishing +5 |
| 22 | Long-range Shooting | Long-Range Shooting | 远射 | 训练·射门 | 禁区外脚背射门按 Finishing +5 瞄准 |
| 23 | **Low Screamer** | | 掠地瞬击 | 特殊 | 力量 <50% 的强势脚 Stunning Shot 按"力量条多 20%"击出，约快 3–15 km/h；不再出现 Dipping |
| 24 | Snap Strike | | 瞬发射门 | 2026 新，特殊（T3） | Stunning Shot 出脚提前 0.07–0.11 s |
| 25 | Acrobatic Finishing | | 杂技般进球 | 训练·射门 | 杂技射门 Finishing +5、弹道更低、误触更少，并有两种新动作 |
| 26 | Heel Trick | | 脚跟绝技 | 训练·传球 | 解锁脚跟传球和近距离脚跟射门；脚跟踢球精度 +5 |
| 27 | First-time Shot | **One-time Shot** | 一脚射门 | 训练·射门 | 一脚射门时，来球速度在惩罚计算中按 90% 计 |
| 28 | **Phenomenal Finishing** | | 无定型射门 | 特殊 | 别扭姿势射门按 **Finishing +7** 瞄准，速度损失和误触大幅减少，头球也适用（两份逆向一致） |
| 29 | Willpower | | 百折不撓（pesdb 原样）/ 国服"百折不挠" | 特殊 | 每射门一次，本场 Finishing 和 Kicking Power 各 +1，最多 +8，受 99 封顶 |

**传球与传中**

| # | 英式名 | 美式差异 | 简中（国际服）/ 国服 | 类别 | 效果 |
|---|---|---|---|---|---|
| 30 | One-touch Pass | | 一脚传球 | 训练·传球 | 一脚传球、传中、解围时，来球按慢 10% 计 |
| 31 | Through Passing | | 直传球 | 训练·传球 | 直塞按 Low Pass 或 Lofted Pass +5 瞄准，可超过 99 |
| 32 | Weighted Pass | | 精准长传 | 训练·传球 | 45 m 以上的前向长传使用强后旋专属动作，无数值加成 |
| 33 | Pinpoint Crossing | | 精确横传球 | 训练·传球 | 传中 Lofted Pass +5、Curl +5 |
| 34 | Edged Crossing | | 急坠传中 / 国服"落叶式横传" | 特殊 | 12–32 m、慢速或静止球可踢前旋下坠传中，高度和速度误差减少 30% |
| 35 | Outside Curler | | 外脚背弧线球 | 训练·传球 | 10–40 m 外脚背弧线传球，以及外脚背射门 |
| 36 | Rabona | | 插花脚 | 训练·传球 | 解锁插花脚传球、传中和轻射，无精度加成 |
| 37 | No Look Pass | | 不看球员传球 | 训练·传球 | 解锁 13 m 内不看人短传，以及近门不看人射门 |
| 38 | Game-changing Pass | | 翻盘传球 | 特殊 | 下半场开球后己方平局或落后时，Low Pass、Lofted Pass +6 |
| 39 | Visionary Pass | | 到位传球 | 特殊 | 其传球后，队友停球 Ball Control +6；下一名触球的队友传、射、顶也按相应能力 +6 |
| 40 | Phenomenal Pass | | 无定型传球 | 特殊（v3.6.0 引入，T1 [S09]） | 别扭姿势传球时传球能力 +6，速度损失和误触更少 |
| 41 | Low Lofted Pass | | 低空传球 | 训练·传球 | 30 m 以上高球的弹道最多降低 0.8 m |

**门将与定位球**

| # | 英式名 | 美式差异 | 简中（国际服）/ 国服 | 类别 | 效果 |
|---|---|---|---|---|---|
| 42 | GK Low Punt | | 守门员低弹道凌空球 | 训练·门将 | 低平大脚 |
| 43 | GK High Punt | | 守门员高弹道凌空球 | 训练·门将 | 高大脚落点约 60 m（无技能约 50 m） |
| 44 | Long Throw | Long Throws | 长距离投掷 | 训练·通用 | 力量 >60% 的界外球可掷 32 m（无技能 25 m） |
| 45 | GK Long Throw | GK Long Throws | 守门员长距离投掷 | 训练·门将 | 手抛球最远 50 m（无技能 30 m） |
| 46 | Penalty Specialist | | 点球专家 | 训练·射门 | 说明文本："higher accuracy penalty kicks"。但逆向称手游中不改变玩家点球落点，只让电脑罚球的误差上限小 0.1 m。**存在冲突** |
| 47 | GK Penalty Saver | | 守门员扑点球 | 训练·门将 | 点球扑救评分 +5 |
| 48 | GK Directing Defence | GK Directing Defense | 守门员指挥防守 | 特殊 | 本方 CB 和边后卫在距本方底线约 34.5 m 以内时，Defensive Awareness、Aggression +2 |
| 49 | GK Spirit Roar | | 门神战吼 | 特殊 | 下半场或加时领先时，本方 CB 和边后卫 Physical Contact、Jump、Acceleration +2 |

**防守与对抗**

| # | 英式名 | 美式差异 | 简中（国际服）/ 国服 | 类别 | 效果 |
|---|---|---|---|---|---|
| 50 | Gamesmanship | | 假摔 | 训练·通用 | 更容易造犯规（绊倒判定距离 11 cm，常规 7 cm） |
| 51 | Man Marking | | 盯人 | 训练·防守 | 电脑盯人时，站位更新频率按 Defensive Awareness +5 计 |
| 52 | Track Back | | 压迫 | 训练·防守 | 丢球后 5 s 内，在 15 m 范围内持续逼抢（无技能 5 m） |
| 53 | Interception | | 截球 | 训练·防守 | 盯防对象接球时，反应延迟降到 60%；三项电脑防守决策按 Defensive Engagement +5 计 |
| 54 | Blocker | | 封堵 | 训练·防守 | 封堵按 Defensive Awareness +5 计，反弹更软 |
| 55 | Aerial Superiority | | 空中优势 | 训练·防守/通用 | 争顶时身体对抗比较值 +5 |
| 56 | Sliding Tackle | | 飞身铲球 | 训练·防守 | 铲球按 Tackling +5 计算 |
| 57 | Long-reach Tackle | | 伸脚抢截 / 国服"伸腿抢截" | 特殊 | 解锁额外站立抢断动作，远伸脚的角度宽 5.6° |
| 58 | Power Tackle | | 强力抢断 | 2026 新，特殊（T3） | 解锁两种身体对抗式抢断，从侧后方撞击可撞倒持球人 |
| 59 | Fortress | | 后防领袖 | 特殊 | 下半场起己方领先时，Defensive Awareness、Tackling、Aggression、Defensive Engagement 各 +6 |
| 60 | Acrobatic Clearance | | 杂技般解围 | 训练·防守 | 解锁杂技解围动作 |
| 61 | Aerial Fort | | 空中堡垒 | 特殊 | 本方禁区内的高球情形下，Physical Contact、Jump +4 |
| 62 | Shadow Hunt | | 紧急回防 | 2026 新，推断特殊 | 担任 CB、边后卫或 DMF 回追身后球时，Speed、Acceleration +3 |

**团队与比赛状态**

| # | 英式名 | 美式差异 | 简中（国际服）/ 国服 | 类别 | 效果 |
|---|---|---|---|---|---|
| 63 | Captaincy | | 队长 | 训练·通用 | 说明文本："reducing the effects of fatigue for the entire team"。逆向称只对队长生效：让疲劳的电脑队友更久保持工作强度，没有数值加成。**存在冲突** |
| 64 | Attack Trigger | | 进攻号手 | 特殊 | 其持球时，其他队友 Attacking Awareness +3 |
| 65 | Super-sub | Super-Sub | 超级候补 | 训练·通用 | 半场或之后替补登场时，25 项能力 +1（两份逆向一致） |
| 66 | Fighting Spirit | | 战斗精神 | 训练·通用 | 受压迫时精度损失减少 20%；电脑控制时，疲劳状态下更努力 |
| 67 | Attacking Surge | | 进攻提速 | 特殊 | 在对方半场且队友持球时 Speed、Acceleration +3，自己拿到球即失效 |

- **可训练技能合计 44 个**：盘带 9、射门 9、传球 9、防守 6、门将 4、通用 7（其中 Aerial Superiority 在 [S34] 中归入防守组）。
- **特殊技能合计 23 个**：19 个 Show Time 技能 + 4 个 2026 年新技能。
- 网易国服的写法与国际服简中不同的例子（T1 [S37]）：落叶式横传（= Edged Crossing）、伸腿/伸脚抢截、牛尾巴、旋风盘球/旋风盘带。

### 4.3 技能与 R9 的直接关系（只列事实，不做设计）

**官方"特殊技能"的时间线（T1）**：
- Blitz Curler：v3.2.0 随 Show Time 卡推出 [S07]。
- Phenomenal Pass：v3.6.0 推出 [S09]。
- Snap Strike：2026-09 首次出现（T3）。
- 其余特殊技能的首发版本**未逐一核实**。

**同类卡的技能组合（T3 [S21]）**：
- Romário（Epic Brazil 1994）：Double Touch、Sole Control、Magnetic Feet、Phenomenal Finishing、Fighting Spirit 等。
- Adriano（Epic 05-06）：Low Screamer、Snap Strike、Phenomenal Finishing 等。

---

## 5. 培养系统（Progression）

### 5.1 等级与经验

- 球员通过比赛和训练道具获得经验并升级。官方 App 描述："Develop players through matches and special items … or use the 'Auto Select' setting"（T1 [S02]）。
- 训练道具 "Level Training Program" 有 100、1,000、4,000、10,000 EXP 四种（T3 [S30]）。
- **Legacy Transfer**（v2.5.0）：可以把一名球员的经验值和附加技能转移给另一名球员（T1 [S15]）。

### 5.2 能力点与等级上限

**能力点公式**：可用能力点 = **2 × (最高等级 − 1)**。

依据：
- pesdb 的计算脚本直接写作 `availablePoints = (maxLevel - 1) * 2`（T3 [S22]）。
- ef-labo 写 "Each level gives you 2 points"（T3 [S26]）。
- 独立核对：eFHUB 显示 Adriano Epic 05-06 的能力点为 "0 / 66"，与 pesdb 的最高等级 34 吻合，因为 2 × 33 = 66（T3 [S23]）。

**各卡种的最高等级**（T3 [S21]；2026-10-25 按卡种各抽 5–10 张 CF 卡，不是全量统计）：

| 卡种（英 / 简中） | 观察到的最高等级 | 对应能力点 | 备注 |
|---|---|---|---|
| Epic / 史诗 | 31–34（一张旧 Massaro 卡为 1） | 60–66 | Brazil 2002 批次多为 33（64 点） |
| Big Time | 31–34 | 60–66 | 有一批名为 "dummy" 的数据卡等级为 1，可忽略 |
| Show Time | 31–34 | 60–66 | |
| Highlight / 高光 | 29–36，部分为 1 | 56–70 | |
| Trending / 状态火热（POTW、POTM 等） | 1 | 0 | 不能培养（T3 [S29]） |
| Legendary / 传奇 | 1–10 | 0–18 | 多为 2022 年的旧卡 |
| Standard / 标准 | pesdb 显示为 1 | — | **可能是 pesdb 未收录**，未核实 |
| Strike Arena | 1 | 0 | 特殊模式用卡 |

### 5.3 培养类别与对应能力

两份第三方资料一致（T3 [S22][S26]）。

| 类别（英，pesdb 用语） | 简中（pesdb） | 每加 1 次 +1 的能力 |
|---|---|---|
| Shooting | 射门 | Finishing、Set Piece Taking、Curl |
| Passing | 传球 | Low Pass、Lofted Pass |
| Dribbling | 盘球 | Ball Control、Dribbling、Tight Possession |
| Dexterity | 灵巧 | Attacking Awareness、Acceleration、Balance |
| Lower Body Strength | 下肢力量 | Speed、Kicking Power、Stamina |
| Aerial Strength | 空中力量 | Heading、Jumping、Physical Contact |
| Defending | 防守 | Defensive Awareness、Tackling、Aggression、Defensive Engagement |
| GK 1 | GK 1 | GK Awareness、Jumping |
| GK 2 | GK 2 | GK Parrying、GK Reach |
| GK 3 | GK 3 | GK Catching、GK Reflexes |

- Jumping 同时属于 Aerial Strength 和 GK 1。pesdb 只在门将位置时启用 GK 1–3。
- GamingonPhone 把 "Lower Body Strength" 写作 "Lower Body"，并指出 2023→2024 赛季删除了"球队风格"培养类别（T3 [S30]）。官方对应条目见 §6.4。

### 5.4 每步花费

第 n 次加点的花费为 ⌈n/4⌉。前 n 次的累计花费为 (g+1)×(2g+r)，其中 g = ⌊n/4⌋，r = n mod 4。这是 pesdb 脚本原式（T3 [S22]）；ef-labo 的表格与之一致（1–4: 1，5–8: 2，9–12: 3，13–16: 4，T3 [S26]）。

| 同一类别第几次加点 | 1–4 | 5–8 | 9–12 | 13–16 | 17–20 |
|---|---|---|---|---|---|
| 每次花费 | 1 | 2 | 3 | 4 | 5 |
| 累计花费（到该段末） | 4 | 12 | 24 | 40 | 60 |

- **单一类别有没有加点次数上限：未找到。** pesdb 不设上限，只把每项能力夹在 99。
- 13 次及以后的单价（4、5）来自公式外推。efootballlab 只明确写到第 12 次（T3 [S27]）。

### 5.5 自动分配与重置 [T1]

| 版本 | 变更 |
|---|---|
| v2.6.0 | 可以用 GP 重置能力点分配 [S16] |
| v4.0.0 | 重置**不再需要 GP**。新增自动分配（官方英文 "Auto Development"，简中"自动提升"）："automatically re-allocate all your player's Progression Points in a way that will maximise his Overall Rating every time he levels up" [S10][S17] |
| v6.0.0 | 培养界面会显示当前比赛计划下的能力值，即包含主教练等影响 [S03] |

- pesdb 的"最大化"算法是贪心法：每一步选"总评增量 ÷ 花费"最高的类别（T3 [S22]）。**游戏内的自动分配是否用同一算法，未找到。**
- 本组用贪心法复算的满级总评，与 pesdb 列出的值 51/51 一致（T4）。但 pesdb 的满级值本身可能就是用同一算法算出来的，所以这个一致**不能**证明游戏内算法相同。

### 5.6 算例：Epic Adriano（Internazionale 05-06），CF，66 点

- 贪心分配结果：Shooting 11、Passing 1、Dribbling 4、Dexterity 12、Aerial 4、Lower Body 8，正好用完 66 点。
- 总评：1 级 86 → 满级 99（无增能）→ 102（加上固定增能 Striker's Instinct +4）。与 pesdb 的 102 一致（T4 复算）。

---

## 6. 增能（Booster，国际服简中"加成"，国服"增能"）

### 6.1 版本演变 [T1]

| 版本 | 内容 |
|---|---|
| v3.0.0（2023-09） | 推出增能："enhance certain Player Stats and allow players to perform beyond the normal ceiling of 99"。<br>示例：Free-kick Taking = Finishing / Set Piece Taking / Curl / Kicking Power；Technique = Ball Control / Dribbling / Tight Possession / Low Pass；Agility = Speed / Acceleration / Balance / Stamina。<br>官方预告将推出"球员专属增能" [S06] |
| v3.2.0 | 两种条件型增能 [S07]：<br>① "The Power of Many"：比赛计划中满足条件的球员越多，加成越大（例如条件为 "J.League"）；<br>② 与现实表现挂钩：Live Update Rating 为 B 及以上才生效，A 时效果最大，C 及以下不生效 |
| v3.4.0 | 调整增能效果和条件的说明画面 [S08] |
| v4.0.0（2024-09） | 引入 **Booster Slot**：<br>· "Random Booster Token" 随机制作 17 种常规增能之一；<br>· "Select Booster Tokens" 集 100 个可自选；释出球员可以得到选择代币 [S10] |
| v4.2.0 | 可以用 GP 兑换 Random Booster Token [S11] |
| v5.2.0（2025-12） | 新开放 12 种可制作增能，例如 "Accuracy" 和 "Strength"。<br>选择代币可从"全部 **28** 种"中选；随机代币按注册位置从 14–15 种中抽，门将从 8 种中抽 [S14] |

### 6.2 常规增能完整列表

列说明：
- 能力组合取自 pesdb 卡页的可选项（T3 [S21]），与 GamingonPhone 的表格逐项一致（T3 [S28]）。其中 3 项另有官方依据（T1 [S06]）。
- "制作增能"每项为 +1（pesdb 显示为 "+1"，[S28] 也写 "+1"）。卡面固定增能的数值另见 §6.4。
- 简中名称：前 17 项取自 Konami 官网简中 v4.0.0 表格（按顺序对应，T1 [S17]）。Accuracy = 精准、Strength = 强壮 取自官网简中 v5.2.0。其余 10 项的官方简中名**未找到**。

| pesdb id | 英文名 | 4 项能力（各 +1） | 官网简中 | 国服已见写法 |
|---|---|---|---|---|
| 1 | Shooting | Ball Control, Finishing, Kicking Power, Physical Contact | 攻门 | 攻门 |
| 2 | Free-kick Taking | Finishing, Set Piece Taking, Curl, Kicking Power | 罚任意球 | |
| 3 | Aerial | Finishing, Heading, Jumping, Physical Contact | 空中对抗 | |
| 4 | Passing | Low Pass, Lofted Pass, Curl, Kicking Power | 传球 | 传球 |
| 5 | Ball-carrying | Dribbling, Tight Possession, Speed, Balance | 带球 | 带球 |
| 6 | Technique | Ball Control, Dribbling, Tight Possession, Low Pass | 技术 | |
| 7 | Defending | Acceleration, Jumping, Defensive Awareness, Tackling | 防守 | |
| 8 | Duelling | Speed, Stamina, Defensive Awareness, Tackling | 1对1 | |
| 9 | Agility | Speed, Acceleration, Balance, Stamina | 敏捷 | |
| 10 | Physicality | Jumping, Physical Contact, Balance, Stamina | 身体素质 | |
| 11 | Goalkeeping | GK Awareness, GK Catching, GK Parrying, GK Reflexes | 守门 | |
| 64 | Striker's Instinct | Attacking Awareness, Ball Control, Finishing, Acceleration | 射手本能 | |
| 65 | Shutdown | Speed, Defensive Awareness, Tackling, Defensive Engagement | 一夫当关 | |
| 66 | Hard Worker | Acceleration, Physical Contact, Stamina, Aggression | 防守铁人 | |
| 67 | Saving | GK Awareness, GK Parrying, GK Reflexes, GK Reach | 固若金汤 | |
| 68 | Crossing | Lofted Pass, Curl, Speed, Stamina | 传球大师 | |
| 69 | Fantasista | Ball Control, Dribbling, Finishing, Balance | 球场幻想家 | 幻想家（片段） |
| 172 | Regista | Tight Possession, Low Pass, Defensive Awareness, Tackling | 未找到 | |
| 173 | Rebuilding | Low Pass, Defensive Awareness, Aggression, Defensive Engagement | 未找到 | |
| 174 | Accuracy | Low Pass, Lofted Pass, Finishing, Kicking Power | 精准 | |
| 175 | Offence Creator | Attacking Awareness, Ball Control, Low Pass, Kicking Power | 未找到 | |
| 176 | Ball Protection | Ball Control, Tight Possession, Physical Contact, Balance | 未找到 | |
| 177 | Balancer | Attacking Awareness, Acceleration, Stamina, Defensive Awareness | 未找到 | |
| 178 | Counter | Low Pass, Physical Contact, Tackling, Defensive Engagement | 未找到 | 反击 |
| 179 | Aerial Block | Heading, Jumping, Physical Contact, Defensive Awareness | 未找到 | |
| 180 | Breakthrough | Dribbling, Speed, Kicking Power, Physical Contact | 未找到 | |
| 181 | Strength | Speed, Kicking Power, Jumping, Physical Contact | 强壮 | 强壮 |
| 182 | Off the Ball | Attacking Awareness, Speed, Acceleration, Stamina | 未找到 | |
| 183 | Stealing | Acceleration, Physical Contact, Tackling, Aggression | 未找到 | |

- **数量冲突**：官方 v4.0.0 说常规增能 17 种，v5.2.0 说新增 12 种、"全部 28 种"。但 17 + 12 = 29，pesdb 的可选列表也是 29 项。差异原因**未找到**，可能官方计数口径不同（T4）。
- "pesdb id" 是 pesdb 页面数据中的增能 ID。同一种增能在不同档位有不同 ID，例如 Striker's Instinct +4 的 ID 为 82，+3 为 76、115、469 等。**这些 ID 是否就是客户端 ID，未找到。**

### 6.3 专属与单项增能（T3）

- eFHUB 的增能名称表列出：Bearer of Fate、King of Football、Le Petit Prince、Magical、Natural-born、Son of God、Striking、The Undisputed、Total Package [S23]。
  - 可见数值的例子：Total Package +3 = 26 项**全部** +3（Show Time 卡，pesdb id 101 / 170，T3 [S21]）。
  - 其余各项的能力组合**未逐一查证**。
- **单项增能**例如 Kicking Power +6、Heading +6、Physical Contact +6、Curl +8，以及 Aggression、Balance、Ball Control、GK Catching、Jumping、Speed 等名目 [S21][S23]。

### 6.4 卡种的固定增能（2026-10-25 抽样观察，T3 [S21]）

| 卡种 | 增能 1（pesdb 默认激活） | 增能 2 | 例子 |
|---|---|---|---|
| Epic | 4 项型 +3 或 +4 | ① "Empty Booster Slot +1"（可制作），或 ② 单项 +6（pesdb 默认**未激活**） | Adriano 05-06：Striker's Instinct +4 / Kicking Power +6<br>Drogba：Striker's Instinct +4 / Heading +6<br>Rivaldo Brazil 2002：Ball Protection +3 / 空槽 |
| Big Time | 4 项型 +4 至 +6 | 4 项型 +3 或单项 +8（默认未激活），或空槽 | Del Piero Italy 00：Accuracy +6 / Curl +8<br>C 罗 Portugal 26：Striker's Instinct +4 / Physicality +3<br>Ronaldinho Brazil 02：Technique +4 / 空槽 |
| Show Time | 4 项型 +3 或 +4，或 Total Package +3 | 4 项型 +3（默认未激活）或空槽 | Haaland：Shooting +3 / Physicality +3 |
| Trending（POTW） | 4 项型 +3 | 无 | Haaland POTW：Breakthrough +3 |
| Highlight | 无，或只有空槽 | | |
| Legendary、Standard、Strike Arena | 无 | | |

- **"增能 2 默认未激活"的含义未找到。** eFHUB 页面把两个增能槽都显示为 "off"。它们可能是有激活条件的增能（参考 §6.1 的条件型），也可能需要玩家选择。需要游戏内说明或数据表来确认。
- 网易国服称同时拥有两个增能的卡为"双增能史诗"（T1 [S37]）。

### 6.5 主教练的三种影响（彼此独立）

**① 主教练对当前球队风格的熟练度（Team Playstyle Proficiency）**
- v3.0.0 起，**球员本身不再有**球队风格熟练度："From v3.0.0 onwards … players will not have 'Team Playstyle Proficiency'"（T1 [S06]）。现在只剩**主教练的**熟练度。
- 社区逆向的公式（T3 [S25]，11.1.1）：

  当 p < 90：倍率 = (104 − 38 × (p − 99)² ÷ 99²) ÷ 100
  当 p ≥ 90：倍率 = (99.253 + 0.0485 × p) ÷ 100

  - 例：p = 40 → ×0.905；p = 67 → ×1.0003；p = 89 → ×1.036；p = 99 → ×1.0405。
  - 能力增量 =（倍率 − 1）× 卡面值，向零取整。所以 p = 64–71 时不改变任何能力。
  - 作用于全部 26 项能力。逆足精度不属于这 26 项，也不受影响。结果仍封顶 99。
- pesdb 用"门槛表"实现同一效果：能力值达到门槛就 +1、+2、+3（T3 [S22]）。例：p = 89 时门槛为 40、56、84，即能力 40–55 加 1，56–83 加 2，84 及以上加 3。ef-labo 给出相同的分段（T3 [S26]）。
- **本组核对（T4）**：两种描述是同一回事。卡面值 × 3.6% 向下取整，在 56、84 处刚好跨过 2、3，与门槛表一致。
- 球队风格共 6 种：Possession Game、Quick Counter、Long Ball Counter、Long Ball、Out Wide、Overload（Overload 为 v6.0.0 新增，T1 [S03]）。

**② 主教练增能**
- 例：José Mourinho 带 Physical Contact +1、Stamina +1，对全队所有球员生效（T3 [S26]）。
- 逆向称它与球员自身的增能一样，在 99 之后相加，不被倍率缩放（T3 [S25]）。

**③ Link-up Play**（v5.0.0 新增，T1 [S13]）
- 两名特定位置和风格的球员（Centrepiece 与 Key Man）同时上场时，在特定时机提升能力。v6.0.0 起有主教练带两个 Link-up Play。
- 逆向称加成为 **+3**，在 99 封顶之前相加（T3 [S25]）。**触发条件的细节未核实。**

### 6.6 比赛中能力值的叠加顺序与上限

以下为社区逆向结论（T3 [S25]，11.1.1）。官方只确认了"增能可以超过 99"（T1）。

1. 卡面值 + 状态箭头 → 夹在 [40, 99]。
2. 加上生效中的技能加成（例如 Momentum Dribbling +6）和 Link-up 的 +3。
3. 加上比例变化：（位置适性 × 疲劳 × 主教练熟练度 − 1）× 卡面值，向零取整。
4. 结果再夹在 [40, 99]。
5. 最后加上增能点（球员自身两个槽 + 主教练增能），**不缩放**，最高 **120**。
6. 两个增能槽作用在同一项能力上时直接相加（pesdb 计算即如此，T3 [S22]）。
7. 培养加点在卡面阶段完成，每项最高 99（pesdb，T3 [S22]）。

**本项目必须避免的重复叠加（T4 推论）**：
- 不要把增能写进基础能力值。
- 不要把主教练加成写进卡面值。
- 不要让技能加成越过 99 再算一次。

### 6.7 增能对总评的影响

- pesdb 的总评计算**包含已激活的增能**：先算培养后的能力（≤99），再加增能，然后代入总评公式（T3 [S22]）。
- 官方旁证：v5.1.0 的修复列表中有一条 "The Overall Rating shown on Player Cards may glow yellow, even if they do not have an active Booster"。这说明增能激活时，总评会以黄色高亮显示（T1 [S13] 所列 v5.1.0 页面）。

---

## 7. 总评计算（按位置）

### 7.1 公式

来源：pesdb `player.js` 中的 `rawOverall` 函数（T3 [S22]）。Konami 从未公布总评公式。

```
raw(pos) = [ Σ_s (stat_s − 25) × w_s,pos
           + (height_cm − 136) × w_h,pos
           + (WFR − 25) × 4
           + 500 ] / 1000

WFR = floor(59 × WFA / 3 + 40)，其中 WFA = 逆足精度档位 0–3
    → Low = 40，Medium = 59，High = 79，Very High = 99

总评 = floor(raw)
```

- 公式里的 stat_s 是培养后、加上增能后的值（见 §6.7）。
- 总评**不包含**技能、比赛风格和状态。
- 主教练熟练度：卡面基础总评不含它。pesdb 在用户选了主教练时，会先把熟练度影响加到能力上，再代入公式（T3 [S22]）。游戏内比赛计划画面显示的总评是否包含主教练影响，**未核实**。参考：v6.0.0 起培养界面会显示比赛计划下的能力（T1 [S03]）。
- 总评**包含**身高和逆足精度。这与部分媒体所说"总评不计逆足"相反 [S43]，以逆向公式和验证结果为准。

### 7.2 各位置权重（原样摘自 pesdb，T3）

| 字段 \ 位置 | GK | CB | LB | RB | DMF | CMF | LMF | RMF | AMF | LWF | RWF | SS | CF |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 身高 Height（cm，减 136） | 186 | 136 | 49 | 49 | 61 | 37 | 12 | 12 | 37 | 49 | 49 | 62 | 99 |
| Attacking/Offensive Awareness 进攻意识 | 0 | 14 | 61 | 61 | 61 | 98 | 98 | 98 | 171 | 159 | 159 | 173 | 210 |
| Ball Control 控球 | 13 | 27 | 86 | 86 | 122 | 171 | 171 | 171 | 196 | 159 | 159 | 210 | 123 |
| Dribbling 盘球 | 0 | 14 | 61 | 61 | 37 | 98 | 122 | 110 | 122 | 159 | 159 | 123 | 62 |
| Tight Possession 紧密控球 | 0 | 0 | 37 | 37 | 24 | 49 | 61 | 73 | 73 | 86 | 86 | 86 | 37 |
| Low Pass 地面传球 | 27 | 41 | 61 | 61 | 122 | 208 | 135 | 135 | 196 | 73 | 73 | 99 | 37 |
| Lofted Pass 空中传球 | 40 | 68 | 147 | 147 | 122 | 159 | 196 | 196 | 159 | 98 | 98 | 74 | 12 |
| Finishing 射门 | 0 | 27 | 24 | 24 | 37 | 73 | 86 | 86 | 184 | 159 | 159 | 284 | 358 |
| Heading 头球 | 0 | 55 | 24 | 24 | 61 | 24 | 12 | 12 | 24 | 24 | 24 | 25 | 62 |
| Set Piece Taking 罚定位球 | 0 | 14 | 24 | 24 | 12 | 12 | 24 | 24 | 12 | 12 | 12 | 12 | 12 |
| Curl 弧线球 | 0 | 14 | 24 | 24 | 12 | 12 | 24 | 24 | 12 | 12 | 12 | 12 | 12 |
| Speed 速度 | 13 | 136 | 220 | 220 | 61 | 61 | 196 | 196 | 98 | 220 | 220 | 86 | 99 |
| Acceleration 加速 | 40 | 150 | 184 | 184 | 61 | 86 | 159 | 159 | 86 | 159 | 159 | 99 | 123 |
| Kicking Power 脚下力量 | 53 | 27 | 24 | 24 | 49 | 73 | 24 | 24 | 73 | 61 | 61 | 99 | 123 |
| Jumping 跳起 | 133 | 109 | 37 | 37 | 37 | 12 | 12 | 12 | 12 | 24 | 24 | 37 | 62 |
| Physical Contact 身体接触 | 80 | 204 | 98 | 98 | 122 | 49 | 24 | 24 | 24 | 37 | 37 | 37 | 86 |
| Balance 平衡 | 0 | 0 | 24 | 24 | 12 | 24 | 61 | 61 | 24 | 73 | 73 | 74 | 86 |
| Stamina 体力 | 0 | 68 | 196 | 196 | 196 | 196 | 147 | 147 | 86 | 49 | 49 | 49 | 37 |
| Defensive Awareness 防守意识 | 13 | 286 | 147 | 147 | 220 | 86 | 49 | 49 | 24 | 12 | 12 | 0 | 0 |
| Tackling 抢球 | 0 | 191 | 86 | 86 | 122 | 86 | 24 | 24 | 24 | 12 | 12 | 12 | 12 |
| Aggression 积极性 | 0 | 82 | 37 | 37 | 98 | 37 | 12 | 12 | 12 | 12 | 12 | 12 | 12 |
| Defensive Engagement 防守参与度 | 0 | 14 | 24 | 24 | 24 | 24 | 24 | 24 | 24 | 24 | 24 | 12 | 12 |
| GK Awareness 守门员意识 | 279 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| GK Catching 守门员接球能力 | 226 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| GK Parrying 守门员扑救 | 173 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| GK Reflexes 守门员扑救反应 | 173 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| GK Reach 守门员臂展 | 226 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| 能力权重合计（不含身高） | 1489 | 1541 | 1626 | 1626 | 1612 | 1638 | 1661 | 1661 | 1636 | 1624 | 1624 | 1615 | 1577 |

- 权重中有一处不对称：LMF 与 RMF 的 Dribbling（122 / 110）和 Tight Possession（61 / 73）不同。表中按原数据保留，未作修正。

### 7.3 验证（T4）

本组用上述公式和权重，在 Python 中复算了以下数据：

| 验证内容 | 结果 |
|---|---|
| pesdb 抓取的 97 张卡的 1 级总评 | **97/97 一致**（涵盖 8 种卡型、多个位置、各档逆足精度） |
| 其中 51 张可培养卡的满级总评（贪心自动分配 + 固定增能 1） | **51/51 一致** |
| Adriano Epic 05-06 在 13 个位置的评分，对照 eFHUB 独立显示的位置评分（LWF 83、CF 86、SS 83、RWF 83、LMF 78、AMF 79、CMF 74、DMF 68、RMF 78、LB 75、CB 69、GK 48、RB 75） | **13/13 一致** |

- **局限**：pesdb 和 eFHUB 都是社区站点，可能用的是同一份逆向结果。复算一致只能说明公式与这两个数据库自洽，**不能等同于**官方客户端的公式。最终仍需在实机或从 IPA 中验证。
- 复算脚本在 `/tmp/claude-0/-home-user-wdxac/53d2b8c2-6abb-5fab-91db-f6077147a038/scratchpad/calc/ovr.py`（临时目录，未入库）。需要时由任务 #6 移入 `tools/data/`。

---

## 8. 官方 Ronaldo Nazário（R9）卡

### 8.1 检索结论：未找到官方卡

**检索过程**（2026-10-25）：
- pesdb（eFootball 2027 数据库）：分别按 "Ronaldo"、"Nazario"、"Naz" 搜索，并选"不可用球员"。另外筛选巴西籍不可用球员，共 667 人。**均无 R9**（T3 [S20]）。
  - 名为 "Ronaldo" 的只有三名现役巴西球员（ID 102827、109566、110562，标准卡，身高 178 / 194 / 179 cm），与 R9 无关。
- pesdb 按 ID 查询：`88044146203441`（历史候选 card_id）返回 404，`990001` 也返回 404。作为对照，已知 ID 4522 和 88045755960841 都能查到。说明这个候选 ID **不是**官方现有的卡（T3）。
- 用 `site:konami.com efootball "Ronaldo Nazário" OR "Ronaldo Nazario"` 检索，没有返回任何 konami.com 页面。这是检索结果为空，不等于官方否认。

**网上流传的内容都不是官方卡**：

| 内容 | 性质 |
|---|---|
| X 账号 @EFOOT_INFORMER（2025-07-14、2025-08-26，日期由帖子 ID 换算）："Epic: World Classics" R9，总评 103（满 109），技能 Phenomenal Finishing、Bullet Header；"Epic: National Legend" R9，102（满 108）。帖子自己标明 "CONCEPT ART" / "Fan Demand" | 粉丝概念，T4 级，不可用作事实 [S39] |
| X 账号 "Efootball__PES"（2025-03-09，日期由帖子 ID 换算）："Ronaldo Nazário 🇧🇷 played for four eFootball partner clubs, yet no Epic card? #BringR9" | **非官方账号**。官方账号为 @play_eFootball。只看到了搜索结果标题 |
| YouTube 视频 "Epic R9 Ronaldo Nazário"、"RONALDO NAZARIO 112 CARD? … LEAKED"（2026） | 标题党或传闻 [S40] |
| GamingonPhone（2026-09-28）：传闻中有一张 112 总评的 Big Time 卡，但明确写 "There is no confirmation that this player is Ronaldo"；另提到 Steam 版国际米兰球场过场中出现 R9 图像 | 传闻，T3 [S32] |

- 公开说法认为 R9 没有授权给 eFootball，与他和 EA 的独家协议有关。这只是推文回复里的说法，**未找到可靠来源**。

### 8.2 最接近的官方卡：Brazil 2002 主题（T3 [S21]）

**pesdb 精选卡包 "Brazil 2002"（featured id 50）包含**：

| 卡种 | 球员与 card_id |
|---|---|
| Epic | Cafu ×3（88030723575245、88035555413453、88044145348045）<br>Roberto Carlos ×3（87963346275788、88035555413452、88044145348044）<br>Rivaldo 88044145348046<br>Kaká 88044145217198<br>Ronaldinho 88033407929808 |
| Legendary | Cafu 17592186179021、Roberto Carlos 17592186179020 |

**其他相关卡**：
- "Brazil 1994"（id 51）：Romário、Bebeto 等。
- "Big Time Brazil 21 Jun '02"：Ronaldinho 89138288266704。这场是 2002 年世界杯 1/4 决赛对英格兰。

### 8.3 参考卡：1 级与满级能力（T3 数据 + T4 复算）

- "1 级"一栏为 pesdb 卡页数据（T3）。
- "满级"为本组按"贪心最大化总评 + 增能槽 1"复算的结果（T4），满级总评与 pesdb 一致。
- 能力格中的写法为"1 级 → 满级"。

| 能力 | Romário Epic Brazil 1994 | Adriano Epic Internazionale Milano 05-06 | Ronaldinho Gaúcho Big Time Big Time Brazil 21 Jun '02 | Rivaldo Epic Brazil 2002 | Kaká Epic Brazil 2002 | Adriano Big Time Big Time 12 Jun '05 |
|---|---|---|---|---|---|---|
| Attacking Awareness 进攻意识 | 82 → 94 | 75 → 91 | 75 → 83 | 77 → 85 | 76 → 87 | 79 → 91 |
| Ball Control 控球 | 82 → 86 | 72 → 80 | 83 → 99 | 83 → 98 | 82 → 96 | 77 → 81 |
| Dribbling 盘球 | 84 → 88 | 79 → 83 | 82 → 98 | 83 → 95 | 88 → 99 | 74 → 78 |
| Tight Possession 紧密控球 | 84 → 88 | 75 → 79 | 83 → 99 | 82 → 97 | 83 → 94 | 74 → 78 |
| Low Pass 地面传球 | 66 → 66 | 65 → 66 | 80 → 92 | 78 → 86 | 77 → 89 | 62 → 63 |
| Lofted Pass 空中传球 | 62 → 62 | 63 → 64 | 79 → 87 | 74 → 82 | 74 → 83 | 62 → 63 |
| Finishing 射门 | 82 → 92 | 77 → 92 | 78 → 82 | 81 → 85 | 78 → 82 | 73 → 82 |
| Heading 头球 | 70 → 74 | 76 → 80 | 56 → 56 | 62 → 62 | 61 → 61 | 73 → 77 |
| Set Piece Taking 罚定位球 | 67 → 77 | 64 → 75 | 82 → 86 | 80 → 84 | 65 → 69 | 73 → 82 |
| Curl 弧线球 | 74 → 84 | 76 → 87 | 82 → 86 | 76 → 80 | 72 → 76 | 75 → 84 |
| Defensive Awareness 防守意识 | 44 → 44 | 41 → 41 | 40 → 40 | 46 → 46 | 56 → 56 | 45 → 45 |
| Tackling 抢球 | 41 → 41 | 44 → 44 | 43 → 43 | 51 → 51 | 54 → 54 | 42 → 42 |
| Aggression 积极性 | 45 → 45 | 45 → 45 | 45 → 45 | 56 → 56 | 58 → 58 | 67 → 67 |
| Defensive Engagement 防守参与度 | 44 → 44 | 43 → 43 | 40 → 40 | 47 → 47 | 53 → 53 | 52 → 52 |
| GK Awareness | 40 → 40 | 40 → 40 | 40 → 40 | 40 → 40 | 40 → 40 | 40 → 40 |
| GK Catching | 40 → 40 | 40 → 40 | 40 → 40 | 40 → 40 | 40 → 40 | 40 → 40 |
| GK Parrying | 40 → 40 | 40 → 40 | 40 → 40 | 40 → 40 | 40 → 40 | 40 → 40 |
| GK Reflexes | 40 → 40 | 40 → 40 | 40 → 40 | 40 → 40 | 40 → 40 | 40 → 40 |
| GK Reach | 40 → 40 | 40 → 40 | 40 → 40 | 40 → 40 | 40 → 40 | 40 → 40 |
| Speed 速度 | 78 → 89 | 83 → 91 | 78 → 86 | 76 → 84 | 83 → 91 | 76 → 84 |
| Acceleration 加速 | 80 → 95 | 77 → 93 | 84 → 92 | 76 → 84 | 80 → 88 | 70 → 82 |
| Kicking Power 脚下力量 | 81 → 89 | 84 → 92 | 77 → 85 | 83 → 91 | 75 → 86 | 90 → 98 |
| Jumping 跳起 | 66 → 70 | 83 → 87 | 60 → 60 | 70 → 70 | 56 → 56 | 74 → 78 |
| Physical Contact 身体接触 | 73 → 77 | 83 → 87 | 77 → 77 | 81 → 84 | 77 → 77 | 85 → 89 |
| Balance 平衡 | 79 → 94 | 66 → 78 | 81 → 89 | 68 → 79 | 82 → 90 | 70 → 82 |
| Stamina 体力 | 72 → 83 | 75 → 83 | 73 → 81 | 74 → 82 | 73 → 81 | 70 → 78 |
| 注册位置 | CF | CF | AMF | AMF | AMF | CF |
| 身高 cm | 169 | 189 | 182 | 186 | 185 | 189 |
| 1 级总评 / 满级（+固定增能 1） | 87 / 101（无增能 100） | 86 / 102（无增能 99） | 87 / 102（无增能 100） | 87 / 101（无增能 100） | 87 / 102（无增能 100） | 84 / 97（无增能 97） |
| 最高等级 / 能力点 | 32 / 62 | 34 / 66 | 33 / 64 | 33 / 64 | 33 / 64 | 31 / 60 |
| 自动加点（本组复算） | shooting:10, dribbling:4, dexterity:12, aerial_strength:4, lower_body_strength:8 | shooting:11, passing:1, dribbling:4, dexterity:12, aerial_strength:4, lower_body_strength:8 | shooting:4, passing:8, dribbling:12, dexterity:8, lower_body_strength:8 | shooting:4, passing:8, dribbling:12, dexterity:8, lower_body_strength:8 | shooting:4, passing:9, dribbling:11, dexterity:8, lower_body_strength:8 | shooting:9, passing:1, dribbling:4, dexterity:12, aerial_strength:4, lower_body_strength:8 |
| 增能 | Agility +3; Empty Booster Slot +1（默认未激活） | Striker's Instinct +4; Kicking Power +6（默认未激活） | Technique +4; Empty Booster Slot +1（默认未激活） | Ball Protection +3; Empty Booster Slot +1（默认未激活） | Offence creator +3; Empty Booster Slot +1（默认未激活） | 无 |
| 风格（进攻/防守） | Goal Poacher / Basic | Goal Poacher / Attack Outlet | Creative Playmaker / Basic | Hole Player / Basic | Dummy Runner / Basic | Goal Poacher / Basic |
| AI 风格 | Mazing Run | Mazing Run, Long Ranger | Trickster | Trickster, Long Ranger | Long Ranger | Mazing Run, Long Ranger |
| 逆足频率/精度、状态波动、抗伤 | Rarely/High, Unwavering, Medium | Rarely/Medium, Unwavering, High | Rarely/High, Unwavering, Medium | Rarely/High, Unwavering, Medium | Occasionally/High, Unwavering, High | Almost Never/Low, Standard, Low |
| 技能（10） | Double Touch, Sole Control, Magnetic Feet, Long-range Curler, Long-range Shooting, Acrobatic Finishing, First-time Shot, Phenomenal Finishing, Outside Curler, Fighting Spirit | Marseille Turn, Heading, Long-range Curler, Rising Shot, Long-range Shooting, Low Screamer, Snap Strike, First-time Shot, Phenomenal Finishing, Outside Curler | Scissors Feint, Double Touch, Flip Flap, Sole Control, Momentum Dribbling, Long-range Curler, Blitz Curler, Through Passing, Outside Curler, No Look Pass | Scissors Feint, Flip Flap, Marseille Turn, Sole Control, Magnetic Feet, Long-range Curler, Long-range Shooting, Low Screamer, First-time Shot, Outside Curler | Double Touch, Marseille Turn, Momentum Dribbling, Acceleration Burst, Long-range Curler, Long-range Shooting, First-time Shot, One-touch Pass, Through Passing, Super-sub | Marseille Turn, Sombrero, Heading, Chip Shot Control, Knuckle Shot, Rising Shot, Long-range Shooting, Acrobatic Finishing, First-time Shot, Outside Curler |
| 体型 11 参数（颈长,颈粗,肩高,肩宽,胸围,腰,臂粗,臂长,大腿,小腿,腿长） | 4,7,2,8,6,6,8,1,10,10,9 | 6,14,4,11,7,7,11,8,11,10,10 | 9,8,7,7,8,7,5,7,10,7,11 | 7,7,6,8,5,2,3,7,3,2,14 | 7,6,6,8,6,7,6,5,8,6,7 | 6,14,4,11,7,7,11,8,11,10,10 |

**观察（T4）**：
- Epic、Big Time 的锋线卡：1 级总评 84–87，满级 97–103；最高等级 31–34。
- 抽样中 Epic 卡的状态波动多为 "Unwavering"，但不是全部：Diego Costa Epic 是 Standard。Big Time 卡也有 Standard 的，例如 Del Piero 和 C 罗。
- 体型 11 参数在同一球员的不同卡之间保持不变（Adriano 三张卡相同），只有派生量 Peak Jump Height 随 Jumping 变化。

### 8.4 card_id 结构（T4 推断，基于 25 张卡的规律）

把 pesdb 上的 card_id 转成十六进制后，可以看出两部分：

- **低 24 位 = 基础球员 ID**。同一球员的不同卡，低 24 位相同。
  - Adriano 各卡都是 134665，与 eFootBase 的 URL `/players/134665/88045755960841` 吻合（T3）。
  - C 罗是 4522，与 pesdb 的 `?id=4522` 跳转吻合（T3）。
- 巴西传奇的基础 ID 是连号：Roberto Carlos 134604、Cafu 134605、Rivaldo 134606、Romário 134607、Ronaldinho 134608、Bebeto 134646、Adriano 134665。**没有 R9。**
- **高位 = 卡种或批次**：

| 高位 | 卡种或批次 |
|---|---|
| `0x100000`–`0x100060` | Legendary |
| `0x5000xx`–`0x5013xx` | Epic 各批次 |
| `0x5101xx`–`0x5112xx` | Big Time |

  当前 Brazil 2002 / 1994 的 Epic 批次（Kaká、RC、Cafu、Rivaldo、Romário）都是 `0x501360`。
- **历史候选 `88044146203441` = `0x501360_0F1B31`**：高位与这一 Epic 批次相同，低 24 位为 **990001**。这是一个**自造的基础球员 ID**，官方库中不存在。
- **风险提示（T4）**：
  - 如果客户端用低 24 位关联球员外观、姓名、FaceNumber 等基础数据，990001 必须在本地数据库中有完整的基础球员记录。
  - 高位沿用 `0x501360` 意味着它在 UI 或卡框上可能被当作该 Epic 批次。
  - 部分标准球员的基础 ID 大于 2²⁴，例如 16898477。所以"低 24 位 = 基础 ID"的规律**并非普遍成立**，必须用客户端实际数据确认。

---

## 9. 对本项目数据设计的约束（T4，供任务 #6 使用）

1. **字段结构以 v6 为准**：进攻型风格、防守型风格、最多若干 AI 风格（实测 ≤3）。防守风格可选 Basic、Front Line Pressure、Attack Outlet、Press Back、Front Line Poacher，CF 都兼容。Goal Poacher 只兼容 CF；Fox in the Box、Target Man 也只兼容 CF。
2. **能力值**：卡面 26 项在 40–99；增能单独存放；不写入主教练影响；不把技能加成当成能力值。
3. **技能**：卡面最多 10 个（实测）+ 附加最多 5 个。特殊技能只能写在卡面，因为它们不可训练。新技能（Tap Trick、Snap Strike、Power Tackle、Shadow Hunt）在 11.1.1 中已存在。
4. **培养**：能力点 = 2 ×（最高等级 − 1）；Epic 常见最高等级 31–34（60–66 点）；第 n 次加点花费 ⌈n/4⌉；每项能力 ≤99。
5. **总评自检**：用 §7 的公式算 1 级和满级总评，与同类 Epic CF（1 级 86–87，满级 101–102）对照，作为"别全给 95–99"的量化护栏。
6. **逆足精度与身高进入总评**：CF 位置的身高权重为 99。例如 183 cm 比 169 cm 的 Romário 多 (183 − 169) × 99 / 1000 ≈ 1.4 点总评。
7. **体型 11 参数是共享的球员级数据**，并且会影响派生的碰撞和覆盖范围。修改时要与身体组的 14 项方案对齐。
8. **客户端编码一律未知**：枚举值（档位用 0 起还是 1 起）、风格和技能的位序、增能 ID、card_id 位结构，都必须从 IPA 内的真实数据表核对。不得套用本文的 pesdb 键名或 ID。

---

## 10. 未确认与待解决清单

| # | 问题 | 当前状态 | 解决途径 |
|---|---|---|---|
| U1 | build 11.1.101 的含义 | 未找到 | 读取 IPA 的 `Info.plist`（CFBundleVersion） |
| U2 | 26 项能力、逆足、Form、抗伤在客户端的字段名、编码和取值区间（0 起还是 1 起） | 未找到；pesdb 的 WFA 用 0–3，PES 2021 用 1–4 / 1–8 / 1–3 | 从 IPA 或缓存中解析球员数据表 |
| U3 | 卡面技能是否硬性限制 10 个 | 实测 ≤10，没有官方说明 | 数据表或实机 |
| U4 | 技能的位序或编码，"特殊技能"标志位 | 未找到 | 数据表 |
| U5 | 2026 年四个新技能是否可训练 | 推断不可（T4） | 游戏内 Skill Training 列表 |
| U6 | 增能种数 28 还是 29 | 冲突 | 游戏内 Select Booster 列表 |
| U7 | Epic、Show Time 卡"增能 2（单项 +6、+8 或 +3）"的激活条件 | 未找到 | 游戏内增能说明画面 |
| U8 | 9 种专属增能（Son of God 等）的能力组合和数值 | 只有 Total Package 已知 | pesdb、eFHUB 单卡页或数据表 |
| U9 | 单一培养类别的加点次数上限 | 未找到（pesdb 不设上限） | 实机 |
| U10 | 游戏内自动分配是否等同贪心法 | 未找到 | 实机对照 |
| U11 | 总评公式是否就是客户端公式 | 社区公式 + 本组复算自洽 | 实机抽查若干卡 |
| U12 | Link-up +3 的触发细节，主教练熟练度在 Strike Arena 等模式下的例外 | T3，部分"未知" | 实机 |
| U13 | Standard 卡的最高等级（pesdb 显示 1） | 存疑 | 游戏内 |
| U14 | 10 种增能和部分界面用语的官方简中名，以及手游国服的完整用语 | 部分未找到 | 游戏内中文界面截图 |
| U15 | Captaincy、Penalty Specialist 的实际效果（说明文本与逆向冲突） | 冲突 | 实机 |
| U16 | card_id 位结构，自定义基础 ID 990001 的完整基础记录 | T4 推断 | 本地数据库与客户端数据 |

---

## 11. 来源列表

访问日期均为 2026-10-25。

### T1：官方或一手

| 编号 | 来源 |
|---|---|
| S01 | Apple iTunes Lookup API, eFootball™ (id1117270703), country = us/jp/gb/hk/br：https://itunes.apple.com/lookup?id=1117270703&country=us |
| S02 | App Store 页面及版本历史：https://apps.apple.com/us/app/efootball/id1117270703 |
| S03 | Konami, eFootball™ v6.0.0 Information：https://www.konami.com/efootball/en/page/v6/versioninfo_v6-00<br>美式英语版：https://www.konami.com/efootball/en-us/page/v6/versioninfo_v6-00<br>Steam 版公告：https://www.konami.com/efootball/en/topic/news/5633（2026-08-12） |
| S04 | Konami, eFootball™ v6.1.0 Information：https://www.konami.com/efootball/en/page/v6/versioninfo_v6-10 |
| S05 | Konami 公告 "Regarding New Items in the v6.1.0 Update"（2026-09-28）：https://www.konami.com/efootball/en/topic/news/5888 |
| S06 | Konami v3.0.0：https://www.konami.com/efootball/en/page/2024/versioninfo_v3-00 |
| S07 | Konami v3.2.0：https://www.konami.com/efootball/en/page/2024/versioninfo_v3-20 |
| S08 | Konami v3.4.0：https://www.konami.com/efootball/en/page/2024/versioninfo_v3-40 |
| S09 | Konami v3.6.0：https://www.konami.com/efootball/en/page/2024/versioninfo_v3-60 |
| S10 | Konami v4.0.0：https://www.konami.com/efootball/en/page/v4/versioninfo_v4-00 |
| S11 | Konami v4.2.0：https://www.konami.com/efootball/en/page/v4/versioninfo_v4-20 |
| S12 | Konami v4.4.0：https://www.konami.com/efootball/en/page/v4/versioninfo_v4-40 |
| S13 | Konami v5.0.0（英式）：https://www.konami.com/efootball/en/page/v5/versioninfo_v5-00<br>Konami v5.0.0（美式）：https://www.konami.com/efootball/en-us/page/v5/versioninfo_v5-00<br>Konami v5.1.0：https://www.konami.com/efootball/en/page/v5/versioninfo_v5-10 |
| S14 | Konami v5.2.0：https://www.konami.com/efootball/en/page/v5/versioninfo_v5-20 |
| S15 | Konami, eFootball™ 2023 v2.5.0 Patch Notes：https://www.konami.com/efootball/en-us/page/2023/season4_patch-notes_v2-50 |
| S16 | Konami v2.6.0 Patch Notes：https://www.konami.com/efootball/en-us/page/2023/season5_patch-notes_v2-60 |
| S17 | Konami 官网简体中文版本说明：<br>v6.0.0：https://www.konami.com/efootball/zh-cn/page/v6/versioninfo_v6-00<br>v6.1.0：https://www.konami.com/efootball/zh-cn/page/v6/versioninfo_v6-10<br>v3.0.0：https://www.konami.com/efootball/zh-cn/page/2024/versioninfo_v3-00<br>v3.4.0：https://www.konami.com/efootball/zh-cn/page/2024/versioninfo_v3-40<br>v4.0.0：https://www.konami.com/efootball/zh-cn/page/v4/versioninfo_v4-00<br>v4.4.0：https://www.konami.com/efootball/zh-cn/page/v4/versioninfo_v4-40<br>v5.0.0：https://www.konami.com/efootball/zh-cn/page/v5/versioninfo_v5-00<br>v5.2.0：https://www.konami.com/efootball/zh-cn/page/v5/versioninfo_v5-20 |
| S37 | 网易《实况足球Online》官网活动页（国服用语"增能""特殊技能"等）：<br>https://skol.163.com/news/official/20251023/41562_1266629.html<br>…/20260108/41562_1280807.html<br>…/20260129/41562_1284562.html<br>…/20260226/41562_1288472.html<br>…/20260410/41562_1295521.html<br>…/20260625/41562_1305662.html |
| S42 | Apple iTunes Search API（cn，"实况足球"）：https://itunes.apple.com/search?term=实况足球&country=cn&entity=software |

### T2：媒体

| 编号 | 来源 |
|---|---|
| S18 | Wikipedia "eFootball"（赛季与版本表）：https://en.wikipedia.org/wiki/EFootball（T3，作版本日期旁证） |
| S34 | Operation Sports, "All eFootball 2027 Player Skills, Explained"（2026-08-13）：https://www.operationsports.com/all-efootball-2027-player-skills-explained/ |
| S35 | RealSport101, eFootball 2023 v2.5.0（2023-03-09）：https://realsport101.com/article/efootball-2023-update-v250-release-date-new-features-my-team |

### T3：社区数据库与逆向

| 编号 | 来源 |
|---|---|
| S20 | pesdb.net 球员搜索与筛选器（en / en-US / zh-Hans / zh-Hant / ja 界面）：https://pesdb.net/efootball/players/ |
| S21 | pesdb.net 单卡页（举例）：<br>https://pesdb.net/efootball/players/adriano-88045755960841<br>…/adriano-88040387251721<br>…/adriano-89066884435465<br>…/adriano-17593796791817<br>…/romario-88044145348047<br>…/rivaldo-88044145348046<br>…/kaka-88044145217198<br>…/cafu-88044145348045<br>…/roberto-carlos-88044145348044<br>…/ronaldinho-gaucho-89138288266704<br>…/rivaldo-89072253144526<br>精选卡包：https://pesdb.net/efootball/players/?featured=50 、?featured=51 、?featured=1754<br>各卡种抽样：?type=epic / big_time / show_time / highlight / trending / legendary / standard / strike_arena |
| S22 | pesdb.net 培养与总评计算脚本：https://pesdb.net/assets/js/player.js |
| S23 | eFHUB 单卡页（Adriano 88045755960841，含技能说明文本、风格说明、增能名称表、位置评分）：https://efhub.com/players/88045755960841 |
| S24 | eFHUB, "[17 October 2026] eFootball's Skill Mechanics Cracked: The Complete Guide"：https://efhub.com/articles/261017_3-17-october-2026-efootball-s-skill-mechanics-cracked-the-complete-guide |
| S25 | efootballexplained.com（标注 "Checked on game version 11.1.1 (2026-10)"）：<br>/stats/、/skills/、/playing-styles/、/myths/、/about/、/changelog/<br>/stats/wfa/、/stats/wfu/、/stats/inj/<br>/playing-styles/manager-team-playstyle-proficiency/<br>/playing-styles/playing-out-of-position/<br>以及各风格页 /playing-styles/<name>/ |
| S26 | ef-labo：<br>培养计算器：https://ef-labo.com/efootball/player-progression/<br>主教练库：https://ef-labo.com/efootball/managers/<br>José Mourinho 页：https://ef-labo.com/efootball/manager-jose-mourinho-aug-2025/ |
| S27 | efootballlab 培养计算器：https://efootballlab.com/efootball-player-progression-calculator |
| S28 | GamingonPhone, Booster Crafting and Tokens guide（2025-12-11）：https://gamingonphone.com/guides/efootball-the-complete-booster-crafting-and-tokens-guide/ |
| S29 | GamingonPhone, types of player cards（2025-09-20）：https://gamingonphone.com/guides/efootball-2026-a-guide-to-types-of-player-cards-in-the-game/ |
| S30 | GamingonPhone, Progression Points（2025-09-20）：https://gamingonphone.com/guides/efootball-2026-guide-how-to-obtain-and-use-progression-points/ |
| S31 | GamingonPhone, Epic Snap Strike pack（2026-09-05）：https://gamingonphone.com/guides/efootball-2027-epic-snap-strike-adriano-totti-shevchenko-pack-review-best-progression-builds-and-skills |
| S32 | GamingonPhone, rumoured eFootball 2027 players（2026-09-28）：https://gamingonphone.com/news/these-could-be-the-next-players-reportedly-coming-to-efootball-2027-list-discussion-and-potential-ratings |
| S33 | GamingonPhone, upcoming Show Time skills leak（2026-08-19）：https://gamingonphone.com/news/efootball-2027-complete-list-of-upcoming-show-time-skills/ |
| S38 | pesdb.net PES 2021 C 罗（旧作数值档位参照）：https://pesdb.net/pes2021/?id=4522 |
| S39 | X 帖（只读了搜索结果标题，未打开原帖）：<br>https://x.com/EFOOT_INFORMER/status/1944708765334569393<br>https://x.com/EFOOT_INFORMER/status/1960349884940497322<br>https://x.com/Efootball__PES/status/1898627455784501296（打开返回 402） |
| S40 | YouTube oEmbed 标题：<br>https://www.youtube.com/watch?v=UkDV3fyDgEs<br>https://www.youtube.com/watch?v=nu_ythrLMWw |
| S41 | Sportsdunia（只用了搜索摘要）：<br>https://www.sportsdunia.com/esports/efootball-snap-strike-skill<br>…/efootball-power-tackle-skill<br>…/efootball-tap-trick-skill |
| S43 | GameMarket 等媒体关于"总评不计逆足"的说法（仅出现在搜索摘要，未采信）：https://gamemarket.gg/news/efootball/efootball-player-stats-explained-ratings-playing-styles-skills |

> **本组的中间数据（未入库，位于会话临时目录）**
> - 抓取的页面：`/tmp/claude-0/-home-user-wdxac/53d2b8c2-6abb-5fab-91db-f6077147a038/scratchpad/web/`
> - 复算脚本：`/tmp/claude-0/-home-user-wdxac/53d2b8c2-6abb-5fab-91db-f6077147a038/scratchpad/calc/ovr.py`、`calc/ref_cards.py`
> - 参考卡数据：`/tmp/claude-0/-home-user-wdxac/53d2b8c2-6abb-5fab-91db-f6077147a038/scratchpad/ref_cards.json`
