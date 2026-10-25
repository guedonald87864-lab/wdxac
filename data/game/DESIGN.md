# R9 2002 世界杯决赛版：能力、技能与培养数据设计说明

> **对象**：Ronaldo Nazário（Ronaldo Luís Nazário de Lima）。2002-06-30 世界杯决赛，德国 0–2 巴西，横滨。25 岁，巴西，9 号，CF。
>
> **版本**：第 2 版（2026-10-25）。第 1 版经足球证据审查和规则审查两轮意见后修订，逐条处理结果见文末"审查与修订记录"。
>
> **交付文件**
> - `data/game/r9_2002_player.json`：完整卡数据。顶层是模拟器的输入格式；`simulation` 是只用于模拟的推荐加点和主教练假设；`notes` 是设计记录和派生结果。**写入客户端的只有 `notes.client_apply_fields` 列出的字段。**
> - `data/game/r9_2002_player.csv`：同一份数据的平表。
> - `data/game/simulation_report.txt`：模拟器在命令行的原样输出。
> - `tools/data/build_r9_2002.py`：生成派生数据。
> - `tools/tests/test_r9_card.py`：回归测试。
>
> **状态**（按任务书 §13 的口径）：**已完成候选，等待实机**。
> - 已做到：数据在本组规则引擎（`tools/data/`）里自洽，模拟器校验 0 ERROR、0 WARN、1 INFO。这条 INFO 是"卡面技能 6 个，少于抽样所见的 10 个"，属有意的设计，见 §6.1。
> - 未做到：客户端字段编码、技能和增能 ID、总评公式都**没有**在客户端上验证。
>
> **证据来源**
> - 03 号档案：2002 年 6 月的比赛表现，含 2026-10-25 的核查记录。另外，足球审查者直接抓取了 ESPN 7 场比赛的文字直播和 CNN、Guardian、BBC 原文；本组在修订时又用 ESPN 原始数据逐条复核过。
> - 04 号档案：客户端规则，大部分来自社区逆向。本稿对照的是含"核查记录" C01–C31 的版本（文件时间 17:23:37 UTC），对账见 §13。
>
> **标记约定**
> - **「社区逆向」**：来自 04 号档案 T3 来源（pesdb、eFHUB、efootballexplained 等）的规则。
> - **「待客户端核实」**：必须用 IPA 数据表或实机确认的值。
> - **「T4」**：本组的设计选择或推断，不是事实。
>
> **范围**：只做这一名球员。不涉及卡池、经济、奖励或 T0 获取资格（任务书 §14）。

---

## 0. 结论速览

| 项目 | 结果 |
|---|---|
| 位置适性 | CF 高（亮绿，注册位置）；SS 中、LWF 中（淡绿）；其余低（无色） |
| 最高等级 / 能力点 | 32 级 / 62 点。公式 2×(32−1)，社区逆向，待客户端核实 |
| 推荐加点 | Shooting 10、Dribbling 4、Dexterity 12、Aerial Strength 4、Lower Body Strength 8，正好用完 62 点。<br>这是"自动提升"两种复现算法（pesdb 贪心、精确最优）的共同结果。<br>但精确搜索的第二名（Shooting 12 / Lower Body 5）只少 13/1000 点总评，客户端可能给出两者之一，两者显示总评相同（§3） |
| CF 总评（社区逆向公式，待客户端核实） | 1 级 **87** → 满级中性 **100** → 加固定增能 **102** → 再加推荐的制作增能 **102** → 再加典型主教练（熟练度 89）**107** |
| 满级中性时 ≥95 的能力 | **0 项**。最高三项：Attacking Awareness 93、Finishing 91、Acceleration 89 |
| 比赛风格 | 进攻型 Goal Poacher（在 CF 生效）<br>防守型 Attack Outlet（在 CF / SS / LWF 生效）<br>AI 风格 Trickster |
| 卡面技能（6 个，上限 10） | Scissors Feint、Cut Behind & Turn、**Acceleration Burst★**、First-time Shot、**Phenomenal Finishing★**、Gamesmanship<br>★ = 特殊技能，不能训练。附加技能出厂为空；可选推荐 One-touch Pass |
| 增能 | slot 1：固定 **Striker's Instinct +3**<br>slot 2：出厂为空槽，推荐玩家自己制作 **Ball Protection +1** |
| 其他字段 | 右脚；逆足频率 Occasionally、精度 High；Form Unwavering；抗伤 Low；Live Update Rating B；183 cm / 82 kg |
| 在 2002 年官方卡中的位置 | 与 Ronaldinho Big Time 21 Jun '02 并列最高一档（满级加固定增能 102），比 Rivaldo Epic 2002 高 1（§8） |

---

## 1. 设计原则

### 1.1 只做 2002 年 6 月

03 §3.3 的结论是本卡的骨架：

- **保留的**：门前嗅觉、一脚出球、短距离启动、急停急转、踩单车、护球。
- **减弱的**：
  - 爆发力："The knee injuries … took away the explosiveness"（Smyth，T2），"less explosive"（Leydon，T3）；
  - 长距离持球冲刺和持续高速（"lost a step"）；
  - 体能：只能稳定踢 70 分钟左右；
  - 抗伤。
- **2002 年特有的**：更多靠跑位和一两脚处理；伤病导致的"保护性"射门方式（脚尖捅射）。

所以本卡**不复制** 1996/97 的形象：

- 不复制从中场带球长途奔袭（03 §3.2 "长距离持球冲刺"）。
- 不复制全场高速（03 §3.2 "体能上限"）。
- 不复制过门将、挑射（03 §3.2 "挑射/过门将"）。

本卡也**不因历史地位拔高**防守、头球、传中（03 §4 数值设计红线）。传中能力只按 2002 年的比赛记录给（§2.1 Lofted Pass）。

### 1.2 七个层次分开存放，避免重复叠加

| 层 | 存放位置 | 本卡内容 | 叠加规则（社区逆向，待客户端核实） |
|---|---|---|---|
| ① 1 级卡面 | JSON `stats` | 26 项 | 取值 40–99（04 §2.2） |
| ② 培养 | JSON `simulation.allocation`（推荐值，**不写入客户端**） | 62 点 | 在卡面阶段相加，每项 ≤99（04 §6.6 第 7 条） |
| ③ 固定增能 | `boosters` slot 1 | Striker's Instinct +3 | 在 99 封顶**之后**相加，上限 120，不被主教练倍率缩放（04 §6.6） |
| ④ 可制作增能 | `boosters` slot 2 出厂为空；推荐写在 `notes.boosters` | Ball Protection +1（玩家制作） | 同 ③ |
| ⑤ 主教练 | **不在卡上**；`simulation.manager` 只是模拟用的假设（T4） | 熟练度 89 | 倍率乘在培养后的卡面值上，再封顶 99；之后才加增能（04 §6.5、§6.6） |
| ⑥ 直接加能力值的技能 | 无 | 本卡**没有**（Momentum Dribbling、Magnetic Feet、Attacking Surge、Willpower 都不选） | 在 99 封顶之前相加。04 核查记录 C21：这条只适用于这一类技能 |
| ⑦ "按能力 +N 瞄准/计算"的技能 | `skills` 中的 Phenomenal Finishing | 别扭姿势射门（含头球）按 Finishing / Heading +7 瞄准 | **不受 99 封顶**，叠在增能之后仍生效（C21：Through Passing 有原文 T3，其余同类为推断 T4）。有效值单独列在 §9.2 |

由此得出的硬性约束：

- 增能不写进 `stats`。
- 主教练的影响不写进 `stats`，`simulation` 整块不写入客户端。如果把 `simulation.allocation` 写进 1 级卡的培养状态，1 级卡就带着 62 点培养、可用点数为 0，1 级 CF 总评会变成 100，等于把培养层重复叠加一次。
- 不选直接加能力值的技能（⑥）。
- 瞄准型技能（⑦）的效果不当作能力值，但它的**有效值**会超过表格里的能力值，单独公布（§9.2），不隐藏。
- 同一个比赛事实不在两处重复计证：例如对土耳其的飞身垫射不同时支撑两个射门技能（§6.3）；伤病和体能起伏已体现在抗伤和 Stamina 上，不再拿来定 Form（§7）。

旧稿的 "Finishing 95" 是一个反例。如果 95 是满级中性值，再加 Striker's Instinct +3 就是 98，属于重复叠加（§2.2）。

### 1.3 量程校准：03 的数值带和游戏的实际量程

03 §4 的"参考数值带"标明是 [T4]，按一个通用量程假设：顶级 ≥90，很好 84–89，良好 76–83。

游戏里官方卡的实际量程要从 04 §8.3 读。例如官方 Epic CF 满级后：

- Ball Control 80–86；
- Low Pass 63–66；
- Lofted Pass 62–64。

本卡的处理办法：

- **满级（含固定增能）**时，能力的档次按 03 的分级排列。**1 级不严格保持**：1 级 = 满级 − 自动加点，而自动加点给 Shooting 10 次、Dribbling 只给 4 次，所以 1 级 Finishing 81 低于 Ball Control 84（§8.1）。
- **绝对值**向 04 §8.3 的同组官方卡靠拢。
- 用 CF 总评落在同类官方锋线卡的区间作为总护栏：04 §8.3 观察到 Epic / Big Time 锋线卡 1 级 84–87、满级加固定增能 97–103；04 §9.5 给出的同类 Epic CF 是 1 级 86–87、满级 101–102。

结果有三项满级中性值低于 03 的数值带：Ball Control、Finishing、Low Pass。前两项加固定增能后回到带内；Low Pass 按游戏量程有意压低（§2.3）。

---

## 2. 26 项能力

### 2.1 总表

各列含义：
- 满级中性 = 1 级 + 推荐加点。
- "+固定" = 再加 slot 1 的 Striker's Instinct +3。
- "+两槽" = 再加推荐的 Ball Protection +1。
- 03 数值带见 03 §4。
- 证据中 "ESPN" 指 ESPN 文字直播（T3），由审查者抓取、本组复核。

| # | 能力（UK / 简中） | 1 级 | 培养 | 满级中性 | +固定 | +两槽 | 03 §4 数值带 | 主要依据 |
|---|---|---:|---:|---:|---:|---:|---|---|
| 1 | Attacking Awareness 进攻意识 | 81 | +12 | **93** | 96 | 96 | 93–95 | 03 §1.2 近点、后点、跟进补射；§1.1 越位 5 次；ESPN 多处 "lurking" |
| 2 | Ball Control 控球 | 84 | +4 | **88** | 91 | 92 | 90–92 | 03 §1.2 #8 卸球后推射；§3.2 小范围转身；半决赛 45' "rare lapse in control"、决赛 30' 停球失误 |
| 3 | Dribbling 盘球 | 84 | +4 | **88** | 88 | 88 | 87–90 | 2002 年仍有：BBC "bumping off one defender, bamboozling another with a trademark step over"；ESPN 对土耳其 8' "turns past the Turkish defence"、54' "shimmying his way past defenders"。反证：对哥斯达黎加 4' 被三人围住，对比利时 21' 冲 Simons 被顶住，所以不再往上加。与 Romário E94 相同 |
| 4 | Tight Possession 紧密控球 | 84 | +4 | **88** | 88 | 89 | 86–89 | 03 §3.2 转身摆脱（对哥斯达黎加 13'、半决赛 49'、决赛 45'）；§1.1 被犯规 14 次 |
| 5 | Low Pass 地面传球 | 72 | 0 | **72** | 72 | 72 | 76–80 | 03 §1.4 只有分边和撞墙，0 助攻；ESPN 对中国 11' "sloppy pass" |
| 6 | Lofted Pass 空中传球 | 69 | 0 | **69** | 69 | 69 | 68–74 | 2002 年有 4 次传中制造了机会：对土耳其 40' "crosses superbly"、对比利时 22'、对哥斯达黎加 41' 和 44'；没有长传记录 |
| 7 | Finishing 射门 | 81 | +10 | **91** | 94 | 94 | 93–96 | 03 §1.1 8 球、每 90 分钟 1.30 球；§1.2 一脚出球 5 个。约 33 次射门中 8 次射偏、打高或中柱（§2.4） |
| 8 | Heading 头球 | 64 | +4 | **68** | 68 | 68 | 68–74 | 03 §3.2 0 个头球进球；对中国 41'、对哥斯达黎加 41' 两次头球都偏；对比利时 52' 差一点没够到 |
| 9 | Set Piece Taking 罚定位球 | 58 | +10 | **68** | 68 | 68 | 65–72 | 03 §1.3 不是点球手；决赛 58'（ESPN 记为 60'）唯一一次任意球打在人墙上 |
| 10 | Curl 弧线球 | 67 | +10 | **77** | 77 | 77 | 76–82 | 03 §4 没有弧线球进球；对比利时 19' 右脚弧线球打高（CNN），对土耳其 54' 弧线球被扑 |
| 11 | Defensive Awareness 防守意识 | 42 | 0 | **42** | 42 | 42 | 35–45（卡面下限 40） | 03 §3.2 7 场只犯规 3 次；不愿参加战术训练 |
| 12 | Tackling 抢球 | 45 | 0 | **45** | 45 | 45 | 40–50 | 03 §1.2 #7 决赛从 Hamann 脚下抢球，属于偶发 |
| 13 | Aggression 积极性 | 50 | 0 | **50** | 50 | 50 | 50–60 | 03 §1.1 犯规 3 次，0 黄牌；ESPN 对土耳其 35' 拉人犯规 |
| 14 | Defensive Engagement 防守参与度 | 42 | 0 | **42** | 42 | 42 | 40–50 | 03 §3.2 防守投入低 |
| 15–19 | GK 五项 | 40 | 0 | **40** | 40 | 40 | — | 外场球员一律 40（04 §2.2） |
| 20 | Speed 速度 | 79 | +8 | **87** | 87 | 87 | 85–89 | 03 §3.1 "lost a step"、"pace diminished"；ESPN 多次被后卫追上：对英格兰 18' Campbell、对哥斯达黎加 48' Martínez、决赛 56' Metzelder |
| 21 | Acceleration 加速 | 77 | +12 | **89** | 92 | 92 | 89–92 | 短距离启动仍有：对比利时 37' "burst through"、对哥斯达黎加 48' "sprints through the middle"、半决赛 49' "races into the area"。但伤后失去了爆发力（Smyth T2 "took away the explosiveness"，Leydon T3 "less explosive"），取带内下沿 |
| 22 | Kicking Power 脚下力量 | 77 | +8 | **85** | 85 | 85 | 84–87 | 03 §4 射门被形容为 crisp，但 2002 年三次禁区外尝试全部失败（对土耳其 3'、55'，对英格兰 23'） |
| 23 | Jumping 跳起 | 64 | +4 | **68** | 68 | 68 | 68–74 | 03 §4 没有制空威胁的记录 |
| 24 | Physical Contact 身体接触 | 81 | +4 | **85** | 85 | 86 | 84–88 | 03 §1.2 #7 撞开 Hamann；BBC "bumping off one defender"。反证：对中国 6' 被两名中国后卫挤掉；FFT "sheer brute force … diminished" |
| 25 | Balance 平衡 | 74 | +12 | **86** | 86 | 87 | 85–88 | 03 §3.2 重心低，"twists on a sixpence"，飞身垫射后仍能完成触球 |
| 26 | Stamina 体力 | 58 | +8 | **66** | 66 | 66 | 65–72 | 03 §1.1 7 场 5 场被换下，4 场在 68–73 分钟（其中对中国 4–0 时换下不算疲劳）；§3.1 "比别人都累"；§2.1 俱乐部赛季只踢了约 833 分钟 |

**1 级值是倒推出来的**：1 级 = 满级目标 − 推荐加点带来的增量。推荐加点是自动提升的复现结果（§3）。只要客户端的自动提升给出同样的方案，满级中性值就是上表；如果给出第二名方案，差别见 §3.3。

### 2.2 回应任务书旧稿：速度 91、加速 91、射门 95、传球参考 74

| 项 | 旧稿（满级、无外部加成） | 本稿满级中性 | 加固定增能后 | 结论 | 理由 |
|---|---:|---:|---:|---|---|
| Speed | 91 | **87** | 87 | **改** | 03 §3.1："lost a step"（Ringer）、"pace … diminished"（FourFourTwo）；ESPN 记录了三次被后卫追上。<br>速度 91 等于 Kaká Epic 2002、Adriano Epic 05-06 这两张体能巅峰卡的满级值（04 §8.3），用在 2002 年的 R9 身上偏高。<br>87 在 03 数值带 85–89 的中间。<br>Petrone 的百米 10.2–10.3 秒属于伤前，而且测试年份未核实（03 核查记录 #23），不能用作 2002 年的依据。 |
| Acceleration | 91 | **89** | 92 | **改** | "explosive"说的正是启动加速：Smyth（T2）原文 "The knee injuries suffered at Internazionale took away the explosiveness"，Leydon（T3）"less explosive"。<br>03 §3.3 的"保留得最好的身体能力"是 T4 推断，不能压过 T2 原文。<br>短冲刺仍有记录，所以仍然高于 Speed（89 > 87，满足 03 红线），并保留 Acceleration Burst 技能。<br>社区逆向称 Acceleration 只影响达到极速的快慢，不改变极速（04 §2.3），待客户端核实。 |
| Finishing | 95 | **91** | 94 | **改**：95 拆成"中性 91 + 增能 3" | 旧稿的 95 如果是中性值，再加 Striker's Instinct +3 就是 98，这是重复叠加（§1.2）。<br>本稿中性 91；加固定增能 94，仍比 Romário E94 和 Adriano E05-06（都是 92）高 2，是同组第一，符合金靴和每 90 分钟 1.30 球的产出。<br>为什么不是 92 或更高：2002 年约 1/4 的射门偏出目标（§2.4）。不降到 90：那样满级中性 CF 会掉到 99。 |
| 传球参考 | 74 | **Low Pass 72 / Lofted Pass 69** | — | **改** | 拆成两项。官方 Epic CF 的地面传球在 63–66（Romário 66、Adriano 66）。<br>74 已接近 Kaká（AMF）的 1 级地面传球 77。03 §1.4 的结论是"终结者，不是组织核心，0 助攻"。<br>Lofted Pass 69 来自 2002 年的 4 次传中（§2.1），不是因为地位拔高；它在 03 数值带的下部，CF 总评权重只有 12。 |

### 2.3 低于 03 数值带的项目

| 能力 | 03 带 | 本卡满级中性 | 原因 |
|---|---|---:|---|
| Ball Control | 90–92 | 88 | 游戏量程里 CF 的控球偏低（Romário 86）。加固定增能后 91，再加制作增能 92，回到数值带。2002 年也有两次控球失误（半决赛 45'、决赛 30'） |
| Finishing | 93–96 | 91 | 加固定增能后 94，在带内。见 §2.2、§2.4 |
| Low Pass | 76–80 | 72 | 见 §2.2 |

其余各项都在 03 数值带内。Attacking Awareness、Acceleration、Heading、Jumping、Set Piece Taking、Stamina 刻意取在数值带下沿：伤后爆发力下降，0 个头球进球，不是定位球手，体能"一般（下沿）"。

### 2.4 射门与单刀：Finishing 和技能怎样诚实地校准

**2002 年的射门记录（T4 计数）**

按 ESPN 文字直播，加上 CNN、Guardian 逐条计数，7 场约 33 次射门尝试（足球审查者计约 32 次，本组复核 ESPN 部分后差 1 次，属计数口径差异）：

| 结果 | 次数 | 实例 |
|---|---:|---|
| 进球 | 8 | 03 §1.2 |
| 射偏、打高或中柱 | 8 | 对土耳其 3'（禁区外打高）；对中国 41'（头球顶高）；对哥斯达黎加 41'（头球顶偏）、49'（"the highest shot of the World Cup so far"）、83'（中柱）；对比利时 19'（CNN："curling a right-foot shot just over"）、41'（"makeshift volley flies over"）；决赛 18' |
| 被门将扑出 | 约 9 | 如对土耳其 54'、对中国 71'、对比利时 37'、对英格兰 19' 和 27'、半决赛 23' 和 29'、决赛 30' 和 45' |
| 被后卫封堵 | 约 8 | 如对土耳其 10'、49'、55'，对英格兰 23'，对哥斯达黎加 90+4'，半决赛 39'，决赛 53'（Ramelow）、58'（任意球打人墙） |

由此得出两点：

1. **ESPN 的汇总"射门 27 / 射正 21"不是射正率的下限。** 它与 ESPN 自己的文字直播对不上。例如对土耳其首战汇总为 4 射 4 正，文字直播里却是 3' 打高、10'/49'/55' 三次被封堵、54' 被扑，再加进球。射门次数被低估会让射正率**偏高**。第 1 版用"射正率高"论证 Finishing 应顶格，这个前提不成立。
2. **射偏与被扑一样多。** 社区逆向称 eFootball 的 Finishing 主要减少"大偏差"（04 §2.3，待客户端核实），它正好管着约 1/3 的失机。所以 Finishing 不顶格：中性 91，加固定增能 94。

**单刀问题用以下办法表达，而不是把 Finishing 压到不合理的低值**

1. **不给** Chip Shot Control 和任何过门将类技能：2002 年 0 个挑射或过门将进球（03 §1.2、§1.5）。
2. **不给** Willpower：每射一次门 Finishing +1，最多 +8，会把失机"刷"成进球。
3. **不给** Fighting Spirit：受压迫时精度损失减少 20%，同样会掩盖失机（§6.3）。
4. **删去** Acrobatic Finishing：证据只有一个进球，而那个进球已用于 Phenomenal Finishing（§6.3）。
5. 逆足精度 High，不是 Very High；逆足频率 Occasionally，不是 Regularly（§7）。

**Phenomenal Finishing 的影响要如实公开**

- 保留它是本组的设计选择（T4），证据中等。2002 年他多次在别扭姿势下射门：对土耳其飞身伸腿垫射（进球）、对哥斯达黎加 13' 转身射门（进球，但 "scuffed"）、决赛 45' 原地转身抽射（被卡恩挡出）。
- 它按 Finishing +7 瞄准，**不受 99 封顶**，在增能之后仍生效（04 C21，社区逆向 + 本组推断，待客户端核实）。所以在别扭姿势射门时，有效瞄准值高于表格值：

  | 场景 | Finishing | 别扭姿势时的有效瞄准值 |
  |---|---:|---:|
  | 满级中性 | 91 | 98 |
  | 满级 + 固定增能（无主教练） | 94 | 101 |
  | 比赛中 CF（完整培养 + 主教练 89） | 97 | **104** |

- 它对头球也适用：别扭姿势的头球按 Heading +7 瞄准，比赛中 CF 70 → 77。2002 年 0 个头球进球，这是一个副作用；Heading 本身仍是 68，没有拔高。
- **退路**：如果客户端证实 PF 的加成确实不受 99 限，而且实机试玩中 R9 把别扭姿势的机会转化得明显高于 2002 年的水平，就删去 PF。那时本卡只剩 Acceleration Burst 一个特殊技能。

First-time Shot 不是瞄准加成：它只把一脚射门时的来球速度按 90% 计算，不改能力值。

### 2.5 不是"全部 95–99"

| 阶段 | ≥95 的能力 |
|---|---|
| 满级中性 | 无 |
| 加固定增能 | Attacking Awareness 96 |
| 加两槽增能 | Attacking Awareness 96 |
| 再加主教练 89（比赛中，CF） | Attacking Awareness 99、Finishing 97、Acceleration 95、Ball Control 95 |
| 情境：别扭姿势射门（PF 瞄准值，CF 比赛中） | Finishing 104 |

主教练这一层对全队所有球员都一样，所以不算本卡的拔高。

---

## 3. 等级上限、成长点数与加点

### 3.1 为什么是 32 级 / 62 点

能力点公式为 2 × (最高等级 − 1)。每个类别第 n 次加点花费 ⌈n/4⌉：第 13–16 次每次 4 点有 ef-labo 原文，第 17–20 次每次 5 点由 pesdb 代码直接给出（04 C13）。两条都是社区逆向，待客户端核实（04 §5.2、§5.4）。

选 32 级的理由是**卡种和位置**（本组设计选择，T4）：

1. Epic 卡的抽样范围是 31–34 级（04 §5.2，T3）。
2. 04 §8.4 列出的同批次前缀 0x501360 的官方卡（Kaká、Roberto Carlos、Cafu、Rivaldo、Romário）里，唯一的 CF 是 Romário Epic Brazil 1994，32 级 / 62 点。Brazil 2002 批次多为 33 级（04 §5.2；§8.3 列出的 Rivaldo、Kaká 都是 33），该批次没有 CF，都是 AMF 和边后卫。本卡是 CF，取 32 级。

第 1 版还有一条理由："62 点时自动提升的结果是唯一的，与客户端算法无关。"**这条理由过强，已撤回。**
- 精确搜索确认推荐方案是第一名，但第二名只少 **13/1000** 点总评（第 1 版误写为 136/1000）。
- 客户端权重、取整或并列规则的任何微小差别，都可能让结果换成第二名（§3.3）。
- 33 级时两种复现算法本来就不同：贪心 Shooting 10 / Dribbling 5 / Lower Body 8，精确最优 Shooting 12 / Dribbling 4 / Lower Body 6。不过两者满级都取整为 100（raw 100.281 / 100.305），所以 33 级本身也可以接受，最终按卡种取 32。

### 3.2 推荐加点 = 自动提升的复现

| 类别（英 / 简中） | 次数 | 花费 | 影响的能力（每次 +1） |
|---|---:|---:|---|
| Shooting 射门 | 10 | 18 | Finishing、Set Piece Taking、Curl |
| Passing 传球 | 0 | 0 | — |
| Dribbling 盘球 | 4 | 4 | Ball Control、Dribbling、Tight Possession |
| Dexterity 灵巧 | 12 | 24 | Attacking Awareness、Acceleration、Balance |
| Defending 防守 | 0 | 0 | — |
| Aerial Strength 空中力量 | 4 | 4 | Heading、Jumping、Physical Contact |
| Lower Body Strength 下肢力量 | 8 | 12 | Speed、Kicking Power、Stamina |
| **合计** | 38 次 | **62 / 62** | |

"推荐 = 自动提升"在两个前提下成立：

1. 游戏内的最大化算法与 pesdb 贪心或精确最优一致（04 U10 未知）。
2. 它最大化的是**不含主教练**的卡面总评。v6.0.0 起培养界面显示比赛计划下的能力（T1），pesdb 在选了主教练时也会把主教练算进去（T3）。如果客户端按"含主教练"的总评最大化：
   - 熟练度 89 时方案不变；
   - 按 efx 公式，熟练度 72、86 时方案会变，熟练度低于 62 的多数值也会变（低于 64 时倍率 <1）；
   - 按 pesdb 门槛表，熟练度 72、82、84、86 时会变。例如 84 时为 Shooting 8 / Dribbling 5 / Dexterity 13 / Aerial 4 / Lower Body 8。
   - 这些熟练度（≥62）下得到的方案，不含主教练的满级中性 CF 总评都是 **99**（不是 100），加固定增能后仍是 102。原因是满级中性 raw 只比 100 多 0.059，任何偏离第一名的方案都会掉到 99。
   - 完整列表见 `notes.derived.auto_plan_under_manager`（含每个方案的卡面总评）。待客户端核实（§12.2）。

精确搜索的前 5 名（总评分子，÷1000 取整为显示总评；`notes.derived.allocation.top_plans`）：

| 名次 | 分子 | 显示总评 | 加点 |
|---:|---:|---:|---|
| 1 | 100059 | 100 | Shooting 10 / Dribbling 4 / Dexterity 12 / Aerial 4 / Lower Body 8（推荐） |
| 2 | 100046 | 100 | Shooting 12 / Dribbling 4 / Dexterity 12 / Aerial 4 / Lower Body 5 |
| 3 | 100022 | 100 | Shooting 11 / Dribbling 4 / Dexterity 11 / Aerial 4 / Lower Body 8 |
| 3 | 100022 | 100 | Shooting 10 / Dribbling 5 / Dexterity 12 / Aerial 4 / Lower Body 7 |
| 5 | 100010 | 100 | Shooting 10 / Dribbling 4 / Dexterity 12 / Aerial 5 / Lower Body 7 |

分级重新分配的结果（pesdb 贪心复现。官方说法是每次升级都重新分配全部点数，T1）：

| 等级 | 点数 | 分配 | CF 总评 |
|---:|---:|---|---:|
| 1 | 0 | — | 87 |
| 5 | 8 | Shooting 4、Dexterity 4 | 90 |
| 10 | 18 | Shooting 4、Dribbling 4、Dexterity 4、Aerial 2、Lower Body 4 | 92 |
| 15 | 28 | Shooting 4、Dribbling 4、Dexterity 8、Aerial 4、Lower Body 4 | 95 |
| 20 | 38 | Shooting 8、Dribbling 4、Dexterity 8、Aerial 4、Lower Body 5 | 96 |
| 25 | 48 | Shooting 8、Dribbling 4、Dexterity 12、Aerial 4、Lower Body 4 | 98 |
| 30 | 58 | Shooting 8、Dribbling 5、Dexterity 12、Aerial 4、Lower Body 8 | 99 |
| 32 | 62 | Shooting 10、Dribbling 4、Dexterity 12、Aerial 4、Lower Body 8 | 100 |

**升级时个别能力会下降 1 点，这是正常现象。** 因为每级都整体重新分配，按贪心复现：
- 21、24 级：Speed、Kicking Power、Stamina 各 −1；
- 22、25、32 级：Low Pass、Lofted Pass 各 −1（前一级临时给了 Passing 1 次）；
- 31 级：Ball Control、Dribbling、Tight Possession 各 −1。

总评不会下降。验收时不应把这些当作数据错误（`notes.derived.level_up_stat_drops`）。

手动加点的点击顺序（每步取"总评增量 ÷ 花费"最大的类别）有 38 步，见 `simulation_report.txt` 第 1 节，或 JSON 的 `notes.derived.training_steps`。

这个加点与 2002 年的形象一致：
- Dexterity 12 次：无球跑位、启动和平衡，是本卡的核心。
- Shooting 10 次：终结。
- Defending 和 Passing 都是 0 次。

### 3.3 第二名方案：客户端可能给出的另一个自动结果

| | Finishing / Set Piece Taking / Curl | Speed / Kicking Power / Stamina | CF 分子 | 显示总评（中性 / +固定） |
|---|---|---|---:|---:|
| 推荐（第一名） | 91 / 68 / 77 | 87 / 85 / 66 | 100059 | 100 / 102 |
| 第二名 | 93 / 70 / 79 | 84 / 82 / 63 | 100046 | 100 / 102 |

第二名不是推荐：Stamina 63 掉出 03 数值带（65–72），Finishing 93 又把终结推高。但它是"客户端算法与社区复现稍有不同"时最可能出现的结果。实机验收时出现它，不算数据错误（§11）。

第 1 版的"手动方案"（Lower Body 8→6、Dribbling 4→6）已删除：它的用途是补偿当时偏低的 Dribbling，审查后 Dribbling 已上调到 88，这个用途不存在了。

---

## 4. 增能

### 4.1 slot 1 固定增能：Striker's Instinct +3

- **四项能力**：Attacking Awareness、Ball Control、Finishing、Acceleration。来自 04 §6.2 常规增能列表（pesdb id 64，官网简中"射手本能"），能力组合为社区数据，待客户端核实。
- **为什么是它**：这四项对应 03 §3.3 判定 2002 年仍然保留的四个特点：
  - 门前嗅觉、跑位 → Attacking Awareness；
  - 卸球调整 → Ball Control；
  - 一脚终结 → Finishing；
  - 短距离启动 → Acceleration。Acceleration 中性值已因"失去爆发力"下调到 89，加增能后 92，在 7 张对照卡里与 Ronaldinho 并列第 3（§8.3），不再是第 2。

候选对比（CF 总评按 §7 公式和权重计算，社区逆向）：

| 候选（+3） | 四项 | CF 满级+固定 | 与 2002 档案的关系 |
|---|---|---:|---|
| **Striker's Instinct** | AA、BC、Fin、Accel | 102（raw 102.501） | 四项都是 2002 年仍保留的特点 |
| Shooting | BC、Fin、KP、PC | 102（102.129） | 加 Kicking Power，但 2002 年禁区外 0 进球 |
| Fantasista | BC、Drib、Fin、Bal | 101（101.946） | Dribbling 偏向 1990 年代的盘带形象 |
| Off the Ball | AA、Speed、Accel、Stam | 101（101.466） | 加 Stamina，与体能有限的设定相反 |
| Agility | Speed、Accel、Bal、Stam | 101（101.094） | 加 Speed 和 Stamina，正是 2002 年减弱的两项 |
| Ball Protection | BC、TP、PC、Bal | 101（101.055） | 贴合护球特点，但不覆盖终结和跑位；更适合放在 slot 2 |

**为什么取 +3，不取 +4**：
- 04 §8.3 列出的该批次三张 Epic 卡，固定增能都是 +3：Rivaldo Ball Protection +3、Kaká Offence Creator +3、Romário Agility +3。批次里其他卡（Cafu、Roberto Carlos 等）的增能 04 没有列出。
- +4 见于 Adriano Epic 05-06、Drogba 等卡（04 §6.4）。
- +3 让满级加固定增能落在 102，与 2002 年锚点卡的最高值（Ronaldinho BT 102）并列，不碰抽样上沿 103。
- 固定增能的抽样范围是 +3 到 +6（04 §6.4，社区逆向）。

### 4.2 slot 2：出厂空槽，推荐制作 Ball Protection +1

- **出厂状态**：空的可制作槽，与官方 Epic Brazil 2002 批次相同（04 §6.4、§8.3）。写入客户端卡数据时 slot 2 **必须保持为空**。否则玩家再制作一次，就会与卡面重复。
- **推荐制作**：Ball Protection +1（Ball Control、Tight Possession、Physical Contact、Balance）。
  - 它在 04 §6.2 列表中（pesdb id 176，v5.2.0 新增的可制作增能）。
  - 它补的是 slot 1 没有覆盖的 2002 年特点：背身护球、小范围转身、对抗后保持平衡（03 §3.2）。
  - 它**不再**叠加 Finishing。
  - CF 满级加两槽：102（raw 102.833）。

| 制作候选（+1） | CF 满级+两槽 | 说明 |
|---|---:|---|
| **Ball Protection** | 102（102.833） | 推荐 |
| Shooting | 103（103.191） | 总评高 1，但会再加 Finishing，与 slot 1 重复强化终结 |
| Agility | 102（102.846） | 不推荐：加 Stamina 和 Speed |

### 4.3 叠加规则（社区逆向，待客户端核实）

- 增能在 99 封顶之后相加，上限 120，不被主教练倍率缩放（04 §6.6）。
- 两个槽作用在同一项能力上时直接相加（本卡的两槽只在 Ball Control 上重叠：88 + 3 + 1 = 92）。
- 增能计入总评（04 §6.7）。
- 制作增能每项 +1（04 §6.1、§6.2）。

---

## 5. 位置与比赛风格

### 5.1 位置适性

| 位置 | 适性 | 依据 |
|---|---|---|
| CF | 高（亮绿，注册位置） | 03 §4：CF 主位；8 球全部在禁区内或禁区线附近（03 §1.2） |
| SS | 中（淡绿） | 03 §4：SS 次位 |
| LWF | 中（淡绿） | 见下 |
| AMF、RWF 及其他 | 低（无色） | 03 §4：不建议给 AMF 或 RWF 高适性 |

**颜色名称**（04 核查记录 C18）：官方原文只有 "Bright green"（亮绿 = 高）和 "Faded green"（淡绿 = 中），T1。"无色 = 低"和"注册位置永远是高"出自 efootballexplained（T3）。位置序号和 3 档适性在客户端里怎样编码，待客户端核实。

**为什么只给 LWF、不给 RWF**：
- 左路记录 5 次，多数形成机会：对土耳其 40' 左路突破后传中；对比利时 18' 在禁区左侧接球、22' 左路下底传中；对哥斯达黎加 68' 从左路突入禁区；半决赛进球从左侧启动。03 §3.2 也总结为"常拉到左路接球"。
- 右路记录 3 次，都没有形成机会：对土耳其 35' 拉人犯规、半决赛 65' 右路接球、决赛 52' 右路争到角球（ESPN，T3）。
- RWF 是 CF 的相邻位置，玩家可以自己做位置训练（04 §3.6）。

非高适性位置的能力折扣（04 §3.6，社区逆向，待客户端核实）：
- 淡绿位置，Finishing、Ball Control、Speed 等 ×0.92，Attacking Awareness、Acceleration 等 ×0.94。
- 只折算卡面值，增能不打折。

所以本卡在 SS / LWF 比赛中的 Finishing 是 90，在 CF 是 97（都按完整培养加主教练 89 计，§9）。

### 5.2 比赛风格

| 类型 | 选择 | 兼容位置（04 §3.7） | 本卡生效位置 | 依据 |
|---|---|---|---|---|
| 进攻型 | **Goal Poacher**（美式 Adv. Striker，简中"偷猎者"） | CF | CF | 03 §5.1 首选：贴着最后一道防线、往身后跑（越位 5 次，近点和后点抢点，跟进补射）。<br>官方说明 "makes runs in behind the defence"（04 §3.7，T1） |
| 防守型 | **Attack Outlet**（"突前球员"） | CF、SS、LWF/RWF、AMF | CF、SS、LWF | 社区逆向称：无球时不参加横排防线，留在前面等球（04 §3.7；eFHUB 说明 "Avoids defending to save energy"）。<br>对应 03 §3.2 的防守投入低、Scolari 说他不愿参加战术训练、体能有限。<br>先例：Adriano Epic 05-06 就是 Goal Poacher + Attack Outlet；2026-10-16 卡表中 Attack Outlet 共 15 张，其中 CF 6 张（04 C28，T3） |
| AI 风格 | **Trickster**（"魔术师"） | 无位置检查 | — | 2002 年的实例：对土耳其首战 BBC "trademark step over"，ESPN 8' "turns past the Turkish defence"、54' "shimmying"；对比利时 22' 连续踩单车（Trivela "pedalou"）。<br>Smyth 的 "His stepover was a form of hypnosis" 说的是 1990 年代，不作为 2002 年证据。社区逆向称它让电脑更常尝试花式动作（04 §3.7） |

不选的风格：

- **Fox in the Box**：社区逆向称它只跟随全队横移的 30%，并占住禁区中路（04 §3.7）。R9 常往左路拉开，所以只作为备选（03 §5.1）。
- **Front Line Pressure / Press Back**：决赛抢 Hamann 只是偶发的逼抢（03 §3.2）。
- **Mazing Run / Speeding Bullet**（AI）：长距离盘带和直线带球推进是 1996/97 的特点（03 §5.2）。
- **Incisive Run**（AI）：03 §5.2 原本列为可选。但 04 §3.7 的社区逆向显示，它的实际效果是"电脑可选弧线射门，起始概率 35%"。2002 年没有弧线球进球，所以不选。
- **Long Ranger**（AI）：2002 年三次禁区外尝试全部失败。

---

## 6. 技能

### 6.1 卡面技能（6 个；上限 10 为实测值，社区逆向，待客户端核实）

| 技能（英 / 简中） | 特殊技能 | 能否作为附加技能训练 | 2002 年证据 | 效果与所属层（04 §4.2，社区逆向） |
|---|---|---|---|---|
| Scissors Feint 剪刀脚假动作 | 否 | 能 | 对土耳其首战 BBC "trademark step over"；对比利时 22' Trivela "pedalou" | 专属快速剪刀脚，同等 Dribbling 下快约 6–7%。动作，不改能力值 |
| Cut Behind & Turn 向后切球并转身 | 否 | 能 | 对哥斯达黎加 13' 转身摆脱、半决赛第一脚趟球摆脱、决赛 45' 原地转身。03 §5.3 要求与 Chop Turn 二选一，本卡只给这一个 | 解锁专属停球转身，动作计时按 Dribbling +20。动作，不改能力值 |
| **Acceleration Burst 瞬间加速** | **是** | 不能 | 对比利时 37' "burst through"、对哥斯达黎加 48' "sprints through the middle"、半决赛 49' "races into the area" | 慢速或静止带球时做快速 Sharp Touch；**不直接加** Speed 或 Acceleration（04 C20 确认）。动作，与 Acceleration 能力值、增能不在同一层 |
| First-time Shot 一脚射门 | 否 | 能 | 03 §1.2：8 球中 5 球一脚出球 | 一脚射门时来球速度按 90% 计。不改能力值，也不是瞄准加成 |
| **Phenomenal Finishing 无定型射门** | **是** | 不能 | 本组设计选择（T4），证据中等：#1 飞身伸腿垫射（进球）、对哥斯达黎加 13' 转身射门（进球，"scuffed"）、决赛 45' 原地转身（被扑）。不再引用 #4"打得不实"（PF 要减少误触，那是反证）和 #6 脚尖捅射（03 §5.3 要求不为此硬套技能） | 别扭姿势射门按 Finishing +7 瞄准，头球也适用；速度损失和误触更少。**瞄准型，不受 99 封顶**（第 ⑦ 层，§2.4、§9.2） |
| Gamesmanship 假摔 | 否 | 能 | 7 场被犯规 14 次，造了对中国的点球；ESPN：对土耳其 43' 突破时被放倒，对哥斯达黎加 68' 倒地未判，半决赛 54'、67' 被犯规 | 更容易造犯规（绊倒判定距离 11 cm，常规 7 cm） |

**两个特殊技能在客户端里存在吗？"特殊"身份的依据有多强？**
- 两者都在 04 §4.2 的 67 种技能列表中，03 核查记录 #25 再次确认 pesdb 有这些名称。
- 两者都出现在官方卡上（04 §8.3，T3）：
  - Phenomenal Finishing：Romário Epic Brazil 1994、Adriano Epic 05-06；
  - Acceleration Burst：Kaká Epic Brazil 2002。
- "特殊（不可训练）"身份只有两条依据：Operation Sports 的 Showtime 列表（T2/T3），以及它们出现在官方 Epic 卡上（T3）。
  - Operation Sports 正文写 "over 20 Showtime picks"，却只列了 19 个，自相矛盾（04 C22）。
  - 第 1 版曾引网易页面作旁证。那是网易《实况足球Online》PC 端游官网，不是手游国服（04 C27），已不再引用。
- 它们在 build 11.1.101 客户端数据中的 ID、"特殊"标志位和效果，**待客户端核实**（04 §10 U4）。

**为什么只有 6 个（少于 10 个）**：
- 只放 2002 年有比赛证据的技能：03 §5.3 "证据充分"一组，加上设计选择 Phenomenal Finishing。审查后删去了 Acrobatic Finishing（§6.3）。
- **这不是常见形态**：04 抽样的约 60 张 Epic、Big Time、Show Time 卡都正好 10 个卡面技能。第 1 版"标准卡常少于 10 个"一句没有来源，已删除。
- 客户端怎样编码少于 10 个技能的卡（定长 10 槽还是逐技能标志位，空槽怎么表示）、是否接受，未找到，待客户端核实（04 §4.1、C09、U3）。模拟器对此给出一条 INFO。
- 如果客户端硬性要求 10 个：有比赛证据的候补只有 One-touch Pass（证据中等，§6.2）；其余 3 个需要主控另作设计决定并记录，**不要**用无证据的技能悄悄填满。

### 6.2 附加技能（出厂为空；可选，由玩家训练，最多 5 个，T1）

| 推荐（可选） | 证据强度 |
|---|---|
| One-touch Pass 一脚传球 | 中：对中国 44' "collects a one-two" 后造点；对土耳其 29' 与 Cafu、Rivaldo 连线；决赛 22' 与 Lúcio 撞墙未成（ESPN，T3）；03 §1.4 分边和撞墙配合 |

它是可训练技能（04 §4.2）。附加技能不能是特殊技能，也不能与已有技能重复（04 §4.1）。

第 1 版推荐的 Fighting Spirit 和 Chop Turn 已删除，理由见 §6.3。

### 6.3 排除的技能

| 技能 | 排除原因 |
|---|---|
| Acrobatic Finishing | **审查后删除。** 证据只有进球 #1（对土耳其飞身垫射），而这个进球已是 Phenomenal Finishing 的证据，同一个进球不计两次。2002 年另一次杂技式尝试（对比利时 41' "makeshift volley flies over"）打飞了。它也是不受 99 限的瞄准加成，与 PF 在同一脚射门上是否叠加（+5 与 +7）未知，待客户端核实 |
| Fighting Spirit | **审查后从推荐中删除。** 与被排除的 Willpower 用的是同一条"复出叙事"证据（03 §5.3 把两者并列为主观设计）；效果（受压迫时精度损失减少 20%）同样会掩盖失机。留一个、排一个，标准不一致 |
| Chop Turn | **审查后从推荐中删除。** 03 §5.3 要求与 Cut Behind & Turn 二选一，卡面已给 Cut Behind & Turn |
| Momentum Dribbling（特殊） | 持球进入对方半场后四项 +6。这是 1996/97 的长途奔袭特点（03 §3.2）；2002 年 8 球没有一个是长途奔袭 |
| Magnetic Feet（特殊） | 2002 年被围抢时并不占优：对中国 6' "muscled out by a pair of Chinese defenders"、对哥斯达黎加 4' 被三人围住、对比利时 21' 冲 Simons 被顶住（ESPN，T3）。效果是 5 m 内每名对手四项 +2、最多 +8 |
| Attacking Surge（特殊） | 2002 年无球冲刺比 1990 年代慢："lost a step"（Ringer）、"took away the explosiveness"（Smyth）、"less explosive"（Leydon）。另：v5.0.0 起 Attacking Awareness 已影响进攻跑动的加速（04 §2.3，T1）——这是机制重叠，不是任务书意义上的分层叠加 |
| Willpower（特殊） | 每射门一次 Finishing / Kicking Power +1，最多 +8。会掩盖单刀失手（§2.4），只是复出叙事（03 §5.3 主观） |
| Snap Strike（特殊） | 作用于 Stunning Shot（大力射门）的出脚时机。半决赛的脚尖捅射是受伤所迫（03 §1.2 #6），03 §5.3 要求不要为此硬套技能；它是否对应低后摆射门未核实（03 核查记录 #25） |
| Chip Shot Control | 0 个挑射或过门将进球（03 §1.5） |
| Flip Flap | 2002 年比赛报道中没有使用记录（03 §3.2） |
| Long-range Shooting、Long-range Curler、Knuckle Shot、Low Screamer、Blitz Curler | 0 个远射或弧线球进球；三次禁区外尝试全部失败（对土耳其 3'、55'，对英格兰 23'） |
| Heading、Bullet Header、Aerial Superiority | 0 个头球进球（03 §3.2） |
| Penalty Specialist | 2002 年不是点球手（03 §1.3） |
| Captaincy | 2002 年队长是 Cafu（03 §5.3；核查记录 #26 确认） |
| Track Back、Man Marking、Interception | 防守投入低（03 §3.2） |
| Pinpoint Crossing、Through Passing、Visionary Pass | 有传中但不是特长（03 §1.4） |
| Spring Cut 等 7 个占位技能 | 04 §4.1：11.1.1 代码中未发布的 "Dummy" 技能，一律不用 |

---

## 7. 惯用脚、逆足、状态波动、抗伤、Live Update Rating

| 字段 | 取值 | 依据 | 游戏效果（社区逆向，待客户端核实） |
|---|---|---|---|
| 惯用脚 | Right | 03 §4：已知用脚的 5 球中 4 球是右脚 | — |
| 逆足频率 | **Occasionally**（4 档中第 3 档） | 03 §4 的首选是 Occasionally。<br>2002 年确认的左脚射门只有对比利时 87' 的进球（03 §1.2 #5，核查记录 #7 确认）；决赛 18' 的"左脚"只有 T3 单一来源；CNN 写明对比利时 19' 是右脚射门。约 33 次射门中左脚约占 3%（T4 计数）。<br>同组官方卡没有一张是 Regularly：Kaká 是 Occasionally，其余是 Rarely 或 Almost Never。<br>第 1 版用的"生涯能用双脚"（维基 T3，跨年份）违反"只做 2002"，不再使用 | 只决定"球偏向逆足侧 >15° 时是否允许用逆足"：level 3 为 67%，level 4 为 100%。对比利时那类来球多数仍可用左脚。**不会**强迫球员使用逆足（04 §3.2）。不进总评 |
| 逆足精度 | **High**（不给 Very High） | 对比利时左脚一脚推射质量高；但确认的左脚射门只有这 1 次，另 1 次（决赛 18'）只有维基转引 BBC 的 T3 来源（03 核查记录 #27） | 逆足踢球扣分：一般 −7，脚背大力 −10，弧线 −10，停球 −6。<br>同时计入总评：WFR = 79（04 §3.2、§7.1） |
| 状态波动 Form | **Unwavering** | 本组设计选择（T4）。产出稳定性：7 场 6 场进球，含半决赛和决赛（03 §1.1）。<br>第 1 版取 Standard 的理由（Zagallo 认为他"不对劲"、身体起伏大）是伤病证据，已经用在抗伤 Low 和 Stamina 66 上，再用于 Form 就是同一事实计三次。03 §4 原建议"二选一取 Standard"，其前提正是身体起伏。<br>同组官方卡多为 Unwavering（Romário、Rivaldo、Kaká、Ronaldinho、Adriano E05-06） | 社区逆向称比赛逻辑不读取 Form，只影响状态抽取（04 §3.3、C19）。官方说 v3.0.0 降低了 Standard / Inconsistent 抽到 "Poor" 的概率，Unwavering 不变（T1）。只有本地版本会抽取状态时才有作用 |
| 抗伤 | **Low** | 03 §2.2：1999、2000 年两次髌腱断裂；2001/02 三次以上大腿肌肉伤；世界杯期间右大腿伤。<br>核查记录 #16 新增了左/右大腿的冲突，但"多次肌肉伤"本身没有疑问 | 非接触伤病概率 ×1.5；伤病负荷累积 ×1（04 §3.4）。非接触伤病只在体力低于 20% 时发生 |
| Live Update Rating | **B** | 第三方指南称 Epic、Big Time、Legendary 卡固定为 B（T3 [S29]，04 §3.3、C30）。官方 Form 规则和条件型增能都以 "B or higher" 为界（T1） | 只在本地版本使用状态抽取或条件型增能时才有作用。字段与编码待客户端核实 |
| 身高 / 体重 | 183 cm / 82 kg | 02 号档案：183 是工作基线；体重 82 为中值，可接受 80–84，**不是**体测值 | 身高进入总评（CF 权重 99）；社区逆向称比赛逻辑不读取体重（04 §2.3） |

---

## 8. 总评，以及在官方同组卡中的位置

### 8.1 各位置总评（社区逆向公式，待客户端核实）

下表是 §7 公式的直接结果，**不含位置适性惩罚**（与 pesdb 口径相同）：

| 阶段 | GK | CB | LB | RB | DMF | CMF | LMF | RMF | AMF | LWF | RWF | SS | CF |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 级 | 44 | 65 | 73 | 73 | 67 | 76 | 81 | 81 | 84 | 86 | 86 | 88 | **87** |
| 1 级 + 固定增能 | 44 | 66 | 74 | 74 | 68 | 77 | 83 | 83 | 86 | 88 | 88 | 90 | 89 |
| 满级中性 | 46 | 71 | 82 | 82 | 73 | 84 | 91 | 91 | 93 | 98 | 98 | 99 | **100** |
| 满级 + 固定增能 | 46 | 72 | 83 | 83 | 74 | 85 | 92 | 92 | 95 | 99 | 99 | 101 | **102** |
| 满级 + 两槽增能 | 46 | 72 | 83 | 83 | 75 | 85 | 93 | 93 | 95 | 100 | 100 | 102 | **102** |
| 满级 + 固定增能 + 主教练 89 | 48 | 75 | 87 | 87 | 78 | 89 | 96 | 96 | 99 | 104 | 104 | 106 | **106** |
| 满级 + 两槽增能 + 主教练 89 | 49 | 75 | 87 | 87 | 78 | 89 | 97 | 97 | 100 | 104 | 104 | 106 | **107** |

CF 的精确值（raw）依次为 87.411、89.853、100.059、102.501、102.833、106.926、107.258。

**满级中性的 100 离 99 只差 0.059**：客户端公式只要有很小的差别，就可能显示 99。这不影响设计判断（护栏看的是"加固定增能后 101–102"），但验收时要知道。

**计入位置适性惩罚的估算**（本组推断 T4）：v4.2.0 官方说比赛中角色的适配值计入增能、位置适性和主教练（T1），但没有公布算法。下表按 04 §3.6 的倍率先折算卡面值，再加增能，再代入公式（`notes.derived.overall_position_penalised`，待客户端核实）：

| 阶段 | AMF | LWF | RWF | SS | CF |
|---|---:|---:|---:|---:|---:|
| 满级 + 固定增能 | 72 | 91 | 76 | 93 | 102 |
| 满级 + 两槽增能 + 主教练 89 | 76 | 96 | 81 | 97 | 107 |

注意事项：
- **1 级时 SS 88 高于 CF 87**。这在 CF 注册卡里不常见：三张 CF 参考卡的 1 级都是 SS ≤ CF（Romário 87/87、Adriano Epic 05-06 83/86、Adriano BT 82/84）。第 1 版用 AMF 卡作比较，不可比，已更正。
  - 原因：1 级 = 满级 − 自动加点，自动加点给 Shooting 10 次、Dribbling 只给 4 次，所以 1 级的 Finishing（81）相对偏低、Ball Control 和 Dribbling（84）相对偏高；SS 的权重又更看重 Ball Control。
  - 不重新平衡：要让 1 级 CF ≥ SS，只能抬高满级 Finishing（与 §2.4 冲突）或压低满级 Ball Control / Dribbling（进一步低于 03 数值带）。满级后 CF 反超（100 对 99）。
- "主教练 89"是本组假设的典型值（T4）。
- 游戏内比赛计划画面显示的总评是否计入主教练影响，未核实（04 §7.1）。

### 8.2 与 2002 年官方卡对照（04 §8.3；满级为自动加点 + 增能槽 1）

| 卡 | 位置 | 身高 | 1 级 | 满级（无增能） | 满级 + 增能 1 | 作为参照的性质 |
|---|---|---:|---:|---:|---:|---|
| **R9 2002（本卡）** | CF | 183 | **87** | **100** | **102** | — |
| Ronaldinho Big Time 21 Jun '02 | AMF | 182 | 87 | 100 | 102 | **2002 年锚点**：对英格兰 1/4 决赛的单场卡 |
| Rivaldo Epic Brazil 2002 | AMF | 186 | 87 | 100 | 101 | **2002 年锚点** |
| Romário Epic Brazil 1994 | CF | 169 | 87 | 100 | 101 | 纯禁区型前锋的参照（另一届世界杯） |
| Adriano Epic Inter 05-06 | CF | 189 | 86 | 99 | 102 | 同类 Epic CF 参照 |
| Adriano Big Time 12 Jun '05 | CF | 189 | 84 | 97 | 97 | 同类 CF 参照 |
| Kaká Epic Brazil 2002 | AMF | 185 | 87 | 100 | 102 | **只作对照，不作同档依据**：2002 年 Kaká 只替补出场一次（对哥斯达黎加 72' 换下 Rivaldo，ESPN）；这张卡的 Speed 91、Dribbling 99 是生涯巅峰数值 |

**排名**：与 Ronaldinho BT 21 Jun '02 并列 2002 年锚点卡的最高一档（满级加增能 102），比 Rivaldo Epic 2002 高 1。同时落在 04 §8.3 的抽样范围（1 级 84–87，满级加增能 97–103）和 04 §9.5 的同类 Epic CF 范围（1 级 86–87，满级 101–102）之内，没有碰上沿。

这一档的理由：
- 他是这届世界杯的金靴（8 球，03 §1.1），所以排在最高一档。
- 但金球奖排第二（03 §3.1）；Scolari 认为战术上最重要的是 Rivaldo（03 §1.4）。所以只与 Ronaldinho 的单场卡并列，不单独高出一档。

### 8.3 逐项对照（同口径：满级 + 增能槽 1）

| 能力 | R9 2002 | Romário E94 | Adriano E05-06 | Ronaldinho BT02 | Rivaldo E02 | Kaká E02 | Adriano BT05 | R9 排名（7 张中） |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Attacking Awareness | **96** | 94 | 91 | 83 | 85 | 87 | 91 | 1 |
| Ball Control | 91 | 86 | 80 | 99 | 98 | 96 | 81 | 4 |
| Dribbling | 88 | 88 | 83 | 98 | 95 | 99 | 78 | 4（与 Romário 并列） |
| Tight Possession | 88 | 88 | 79 | 99 | 97 | 94 | 78 | 4（与 Romário 并列） |
| Low Pass | 72 | 66 | 66 | 92 | 86 | 89 | 63 | 4 |
| Lofted Pass | 69 | 62 | 64 | 87 | 82 | 83 | 63 | 4 |
| Finishing | **94** | 92 | 92 | 82 | 85 | 82 | 82 | 1 |
| Heading | 68 | 74 | 80 | 56 | 62 | 61 | 77 | 4 |
| Set Piece Taking | 68 | 77 | 75 | 86 | 84 | 69 | 82 | 7 |
| Curl | 77 | 84 | 87 | 86 | 80 | 76 | 84 | 6 |
| Speed | 87 | 89 | 91 | 86 | 84 | 91 | 84 | 4 |
| Acceleration | 92 | 95 | 93 | 92 | 84 | 88 | 82 | 3（与 Ronaldinho 并列） |
| Kicking Power | 85 | 89 | 92 | 85 | 91 | 86 | 98 | 6（与 Ronaldinho 并列） |
| Jumping | 68 | 70 | 87 | 60 | 70 | 56 | 78 | 5 |
| Physical Contact | 85 | 77 | 87 | 77 | 84 | 77 | 89 | 3 |
| Balance | 86 | 94 | 78 | 89 | 79 | 90 | 82 | 4 |
| Stamina | **66** | 83 | 83 | 81 | 82 | 81 | 78 | 7 |
| 防守四项 | 42 / 45 / 50 / 42 | 44 / 41 / 45 / 44 | 41 / 44 / 45 / 43 | 40 / 43 / 45 / 40 | 46 / 51 / 56 / 47 | 56 / 54 / 58 / 53 | 45 / 42 / 67 / 52 | 中下 |

**读法**
- **第一**：跑位和终结（Attacking Awareness、Finishing）。
- **前列**：对抗（Physical Contact 第 3）、短距离启动（Acceleration 与 Ronaldinho 并列第 3，低于 Romário 和 23 岁的 Adriano）。
- **中游**：速度、控球、盘带、平衡。
- **明显低于** AMF 组：盘带、传球、定位球。
- **最低**：体能。

这就是 03 描述的 2002 年 R9："great goalscorer"，而不是 1990 年代的 "great everything"（03 §3.1 Smyth）。

---

## 9. 比赛中的实际能力

### 9.1 能力值

条件：完整培养、两槽增能、主教练 89；满体力、状态正常。计算顺序见 04 §6.6，社区逆向，待客户端核实。本卡没有直接加能力值的技能，所以下表就是能力值本身的上限。

| 能力 | CF（高） | SS（中） | LWF（中） |
|---|---:|---:|---:|
| Attacking Awareness | 99 | 94 | 94 |
| Ball Control | 95 | 88 | 88 |
| Dribbling | 91 | 84 | 84 |
| Tight Possession | 92 | 92 | 92 |
| Low Pass / Lofted Pass | 74 / 71 | 69 / 66 | 69 / 66 |
| Finishing | 97 | 90 | 90 |
| Heading / Jumping | 70 / 70 | 65 / 65 | 65 / 65 |
| Speed / Acceleration | 90 / 95 | 83 / 90 | 83 / 90 |
| Kicking Power | 88 | 83 | 83 |
| Physical Contact / Balance | 89 / 90 | 86 / 87 | 86 / 87 |
| Stamina | 68 | 66 | 66 |
| 防守四项 | 43 / 46 / 51 / 43 | 41 / 43 / 51 / 43 | 41 / 43 / 51 / 43 |

### 9.2 瞄准型技能的有效值（第 ⑦ 层）

能力值不是全部。Phenomenal Finishing 按"能力 +7"瞄准，不受 99 封顶，在增能之后仍生效（04 C21，社区逆向 + 本组推断，待客户端核实）：

| 场景 | Finishing → 别扭姿势射门有效值 | Heading → 别扭姿势头球有效值 |
|---|---|---|
| 满级中性 | 91 → 98 | 68 → 75 |
| 满级 + 固定增能（无主教练） | 94 → 101 | 68 → 75 |
| 比赛中 CF（完整培养 + 主教练 89） | 97 → **104** | 70 → 77 |
| 比赛中 SS / LWF | 90 → 97 | 65 → 72 |

**要点**
- 在 CF 时，Goal Poacher 和 Attack Outlet 两种风格都生效。
- 在 SS / LWF 时，只有 Attack Outlet 生效，而且 Finishing、Speed 等按淡绿位置打折。这符合"中路终结者、偶尔拉边"的 2002 年角色。
- 第 1 版说"上表就是上限"，不对：在 PF 触发的别扭姿势射门中，有效值高于 9.1 的表格值。§2.4 的校准已把这一点算进去，并给出了退路。

---

## 10. 防重复叠加检查表

| 检查 | 结果 |
|---|---|
| `stats` 里没有任何增能 | ✔ 1 级值加推荐加点就是满级中性；增能只在 `boosters` 中 |
| `stats` 里没有主教练影响 | ✔ 主教练只在 `simulation.manager`，不属于卡数据 |
| 模拟专用字段不会被写进客户端 | ✔ `allocation`、`manager` 已移到 `simulation`；`notes.client_apply_fields` 列出可写字段（测试 `test_simulation_fields_not_in_card_layer`） |
| slot 2 出厂为空 | ✔ 推荐的 Ball Protection 只写在 `notes.boosters.slot2_recommendation` |
| 增能在 99 之后相加，不被主教练缩放 | ✔ 模拟器按 04 §6.6 实现（社区逆向） |
| 没有直接加能力值的技能 | ✔ 6 个卡面技能的 `stat_bonus` 都为空（测试 `test_skills_exist_and_specials`） |
| 瞄准型技能的有效值已公开 | ✔ 只有 Phenomenal Finishing；有效值见 §9.2 和 `notes.derived.aim_effective` |
| 同一个进球不支撑两个技能 | ✔ 对土耳其的飞身垫射只用于 Phenomenal Finishing；Acrobatic Finishing 已删除 |
| 伤病证据不重复使用 | ✔ 伤病 → 抗伤 Low、Stamina 66；Form 按产出稳定性定为 Unwavering |
| "爆发力减弱"不被三重叠加抵消 | ✔ Acceleration 中性 89；Acceleration Burst 只加动作，不加能力值 |
| 特殊技能不在附加技能里 | ✔ 附加技能出厂为空；推荐的 One-touch Pass 是可训练技能 |
| 旧稿 Finishing 95 没有再叠加增能 | ✔ 中性 91 + 固定 3 = 94 |
| 自动提升不会改写设计 | △ 推荐加点 = pesdb 贪心 = 精确最优；但第二名只差 13/1000，客户端可能给出第二名（§3.3）。两者显示总评相同 |

---

## 11. 验证方法与结果

```bash
# 1) 用模拟器校验卡数据（存在 ERROR 时退出码为 1）
python3 tools/data/progression_sim.py data/game/r9_2002_player.json
# 2) 查看自动提升的逐级分配，以及精确最优加点
python3 tools/data/progression_sim.py data/game/r9_2002_player.json --allocation auto --no-steps
python3 tools/data/progression_sim.py data/game/r9_2002_player.json --allocation optimal --no-steps --no-levels
# 3) 假设自动提升按含主教练的总评最大化（例：熟练度 84，pesdb 门槛表）
python3 tools/data/progression_sim.py data/game/r9_2002_player.json --allocation auto --auto-with-manager --manager 84 --manager-model pesdb --no-steps --no-levels
# 4) 重新生成派生数据（JSON 的 notes.derived、CSV、模拟报告），或只检查它们是否最新
python3 tools/data/build_r9_2002.py
python3 tools/data/build_r9_2002.py --check
# 5) 回归测试
python3 -m pytest -q tools/tests/test_r9_card.py tools/tests/test_progression.py
```

**本次结果（2026-10-25，第 2 版）**
- 模拟器：报告中 4 次运行退出码都为 0；校验结果 0 ERROR、0 WARN、1 INFO（SKILL_CARD_COUNT，见 §6.1）。
- 派生产物：`--check` 显示"全部最新"。
- 测试：`test_r9_card.py` 10 项、`test_progression.py` 66 项，共 76 项全部通过。

**这些验证能说明什么**：数据满足本组从 04 号档案整理出的全部规则，并且三个产物彼此一致。

**不能说明什么**：
- 客户端是否接受这些字段和取值。
- 总评公式和叠加顺序是否与客户端相同。
- 技能和增能 ID 是否正确。

**实机验收建议**
1. 在隔离的测试存档中导入本卡，只写 `notes.client_apply_fields` 列出的字段。比对 1 级的 26 项和总评（应为 87）。
2. 用自动提升升到 32 级，核对 62 点和分配。
   - 应为 §3.2 的推荐方案，或 §3.3 的第二名（Shooting 12 / Lower Body 5）。两者都表示规则基本一致，只是算法细节不同。
   - 如果是别的方案，先看是否与主教练有关：在不设主教练或主教练熟练度 89 的条件下再试一次。仍不同，说明客户端的算法或花费规则与社区逆向不一致。
   - 升级过程中个别能力下降 1 点是正常的（§3.2）。
3. 核对固定增能激活后总评为 102，制作 Ball Protection +1 后仍为 102。满级中性如果显示 99 而不是 100，有两种可能：公式有小差别（raw 只比 100 多 0.059），或者自动提升按含主教练的总评给出了别的方案（§3.2）。先核对第 2 步的加点再下结论。
4. 核对技能页：6 个卡面技能正常显示，没有空白或 "Dummy" 占位；如果客户端要求 10 个，见 §6.1 的处理办法。
5. 进入比赛：核对两种风格在 CF 生效、Acceleration Burst 有专属动作、逆足在左侧来球时可用。观察别扭姿势射门的转化率是否明显偏高（§2.4 的退路）。

---

## 12. 规则出处与"待客户端核实"清单

### 12.1 本卡用到的规则（摘自 `tools/data/efootball_rules.py` 的规则登记表）

| 规则 | 用在 | 出处 | 分级 / 标记 |
|---|---|---|---|
| `STAT_RANGE` | 26 项卡面值 40–99；GK 能力 40 | 04 §2.2 | T3，社区逆向，待客户端核实 |
| `POINTS` | 32 级 → 62 点 | 04 §5.2 | T3，社区逆向，待客户端核实 |
| `STEP_COST` | 加点花费 ⌈n/4⌉ | 04 §5.4、C13 | T3，社区逆向，待客户端核实 |
| `CATEGORY_MAP` | 类别与能力对应 | 04 §5.3 | T3，社区逆向，待客户端核实 |
| `TRAIN_CAP_99` | 培养后 ≤99 | 04 §6.6 | T3，社区逆向，待客户端核实 |
| `AUTO_DEV_GOAL` | 自动提升以总评最大为目标 | 04 §5.5 | T1 |
| `AUTO_DEV_GREEDY` | 自动提升的算法（pesdb 贪心） | 04 §5.5、U10 | T3，社区逆向，待客户端核实 |
| `AUTO_DEV_MANAGER` | 自动提升是否按含主教练的总评最大化 | 04 §5.5、§7.1、U10 | T1+T3，社区逆向，待客户端核实 |
| `BOOST_AFTER_CAP` / `BOOST_CAP_120` | 增能在 99 之后相加，≤120 | 04 §6.6、§2.2 | T3，社区逆向，待客户端核实 |
| `BOOSTER_LIST` | Striker's Instinct / Ball Protection 的四项 | 04 §6.2、U6 | T1+T3，社区逆向，待客户端核实 |
| `BOOSTER_SLOTS` / `BOOSTER_CRAFTED` / `BOOSTER_FIXED_RANGE` | 2 个槽；制作 +1；固定 +3 | 04 §6.1–6.4 | T3（部分 T1），社区逆向，待客户端核实 |
| `RATING_FORMULA` / `RATING_WEIGHTS` | 全部总评 | 04 §7.1–7.3；权重 C15 逐格核对；97/97、51/51 全量复算【未核实】（C16 只重算了 5 张卡） | T3+T4，社区逆向，本组推断，待客户端核实 |
| `MANAGER_FORMULA` / `MANAGER_THRESHOLDS` / `MANAGER_IN_RATING` | 主教练 89 | 04 §6.5、§7.1 | T3，社区逆向，待客户端核实 |
| `MATCH_ORDER` / `OUT_OF_POSITION` | 比赛中实际能力；SS/LWF 折扣；按位置折算的总评估算 | 04 §6.6、§3.6 | T3，社区逆向，待客户端核实 |
| `POSITION_LEVELS` | 3 级适性；亮绿 / 淡绿 / 无色 | 04 §3.6、C18 | T1+T3，社区逆向，待客户端核实 |
| `STYLE_SPLIT` | 进攻型 + 防守型两类风格 | 04 §3.7 | T1+T3，社区逆向 |
| `STYLE_COMPAT` / `AI_STYLE_MAX` | 风格生效位置；AI 风格 ≤3 | 04 §3.7 | T3，社区逆向，待客户端核实 |
| `SKILL_LIST` / `SKILL_CARD_MAX` | 6 个技能存在；≤10 | 04 §4.1–4.2、U3 | T3，社区逆向，待客户端核实 |
| `SKILL_CARD_COUNT_TYPICAL` | 抽样卡都是 10 个；少于 10 个的编码未知 | 04 §4.1、C09、U3/U4 | T3，社区逆向，待客户端核实 |
| `SKILL_SPECIAL` | 特殊技能不可训练 | 04 §4.1、C22、C27、U4/U5 | T2+T3+T4，社区逆向，待客户端核实 |
| `SKILL_BONUS_BEFORE_CAP` | 直接加能力值的技能在 99 前相加（只限这一类） | 04 §4.2、C21 | T3，社区逆向，待客户端核实 |
| `SKILL_AIM_UNCAPPED` | 瞄准型技能不受 99 限 | 04 §4.2 核查更正、C21 | T3+T4，社区逆向，本组推断，待客户端核实 |
| `SKILL_ADDITIONAL_MAX` | 附加技能 ≤5 | 04 §4.1 | T1 |
| `WEAK_FOOT` / `FORM` / `INJURY` / `LIVE_UPDATE_RATING` | 档位与效果 | 04 §3.1–3.4、C19、C30 | T3（FORM、LIVE_UPDATE_RATING 含 T1），社区逆向，待客户端核实 |
| `LEVEL_CAP` | 最高等级 32 在 Epic 抽样范围内 | 04 §5.2 | T3，社区逆向，待客户端核实 |

另外，各技能、比赛风格、AI 风格的**效果描述**（§5、§6）全部来自 04 §3.7、§4.2 的社区逆向（T3），待客户端核实。

### 12.2 写入客户端前必须核对

1. `card_id` 候选 `88044146203441`（`0x5013600F1B31`，低 24 位 990001 是自造基础 ID）：**接入前必须与主项目核对**，不得重复建实例（04 §8.4、U16）。
2. **批次前缀**：高位 `0x501360` 就是当前 Epic Brazil 2002/1994 批次（pesdb 精选卡包 50/51，04 §8.4，T4）。如果主项目按批次前缀或 card_type 组织卡包、精选列表，这个 card_id 可能把 R9 放进现有 Epic 卡包——那是卡池改动，任务书 §14 禁止。本组不改 card_id，建议由主控分配前缀。
3. **只写入 `notes.client_apply_fields` 列出的字段**；`simulation` 和 `notes` 不得写入客户端。
4. 26 项能力、逆足频率和精度、Form、抗伤、Live Update Rating 的客户端字段名、编码、取值区间（0 起还是 1 起）（04 U2）。JSON 用的是 pesdb 和 UI 的名称。
5. 位置序号与 3 档位置适性的编码（04 §3.6、C18）。CSV 的 `registered_position` 一行已标注。
6. Goal Poacher、Attack Outlet、Trickster，以及 6 个技能的客户端 ID 和位序；Acceleration Burst、Phenomenal Finishing 的"特殊"标志位（04 U4）。
7. 卡面技能少于 10 个时的编码方式与客户端是否接受（04 U3；§6.1）。
8. Phenomenal Finishing 的 +7 是否真的不受 99 封顶、是否作用于头球（04 §4.2、C21）。
9. Striker's Instinct +3 的客户端增能 ID（pesdb 的 id 64 或 +3 档位 id 都不是客户端 ID），空增能槽在数据中的表示方法（04 §6.2、U6、U7）。
10. 最高等级 32 与能力点、花费、类别规则，以及自动提升的算法（04 §5、U9、U10）。第一名与第二名只差 13/1000。
11. 自动提升最大化的是卡面总评还是比赛计划（含主教练）下的总评（§3.2）。
12. 总评公式（04 U11）：87 / 100 / 102 / 102 都是用社区公式算出的；满级中性 raw 100.059 离 99 只差 0.06。
13. 主教练熟练度倍率，以及显示总评是否计入它（04 §6.5、§7.1）。
14. 比赛中按位置的角色评价是否计入位置适性惩罚（§8.1 的估算为 T4）。
15. 卡面技能上限 10（04 U3）。
16. 身体 14 项参数与本卡无关，由身体组负责。04 §3.5 提示体型参数会进入碰撞计算，需与身体组对齐。

---

## 13. 与 03 / 04 核查记录的对账

**03 号档案的核查记录（2026-10-25）**，与本卡相关的条目：

| 核查条目 | 结果 | 对本卡的影响 |
|---|---|---|
| #27 决赛 18' "左脚"单刀 | 部分未核实（只有 T3 来源） | 逆足精度 High、频率 Occasionally 的论证以对比利时左脚进球（#7 确认）和样本量为主，18' 只作旁证（§7） |
| #6 决赛射门：53' Ramelow 封堵 | Guardian 文字直播没有此条；ESPN 文字直播有 "53' Rivaldo finds Ronaldo with a superb pass: Ramelow blocks his attempted shot"（T3） | 改为"仅有 T3 单一来源支持"。§2.4 的射门计数把它算作一次被封堵 |
| #6 新增 30' 一脚没打出力量被卡恩接住 | 确认 | 计入 §2.4 的被扑 |
| #7、#8、#10 对比利时左脚一脚出球；半决赛右脚脚尖捅射；决赛抢 Hamann 后补射 | 确认 | 支撑 First-time Shot、逆足频率、Tackling 45、Physical Contact。#8 脚尖捅射不再用作 Phenomenal Finishing 的证据（§6.1） |
| #9 引语 "only non-painful thing I could do" | 未核实 | 本卡没有引用 |
| #23 百米 10.2–10.3 秒的测试年份 | 未核实 | 本卡不用百米数据定 Speed（§2.2） |
| #25 eFootball 名称只经 pesdb 确认 | 客户端未核实 | 全部名称保持"待客户端核实"（§12.2） |
| #26 2002 年队长是 Cafu | 确认 | 不给 Captaincy |
| #16 2001/02 大腿伤是左腿还是右腿 | 新增冲突 | 抗伤 Low 不受影响 |
| #11、#12、#18 等（TSG 机构名、意杯出场、国家队出场数） | 更正 | 与能力设计无关 |

**04 号档案**：第 1 版写道"17:20 时 04 没有核查记录，内容与设计时使用的版本逐字相同"，这已过时。04 在 17:22 之后加入了"核查记录"一节（C01–C31）和多处行内"核查更正"，最终版本的文件时间为 17:23:37 UTC。本稿开工时和完成前各读取一次，内容相同（校验和 `11008a90…`）。与本卡相关的条目：

| 04 条目 | 核查结论 | 对本卡的影响与处理 |
|---|---|---|
| C09 | 67 种技能、7 个 Dummy 确认；pesdb 有 67 个 `s_*` 技能字段 | 客户端可能按逐技能标志位存储，少于 10 个技能的编码未知（§6.1、规则 `SKILL_CARD_COUNT_TYPICAL`） |
| C13 | ⌈n/4⌉ 确认；"13 次以后是外推"更正：ef-labo 明确写了 13–16 次每次 4 点，pesdb 代码直接给出 17–20 次每次 5 点 | `STEP_COST` 规则说明已改。本卡单类最多 12 次，数值不受影响 |
| C15 | 总评公式与 13 × 27 权重表逐格确认 | 无变化 |
| C16 | 参考卡复算确认；97/97、51/51 全量复算【未核实】（只重算了 5 张卡） | `RATING_FORMULA` 规则说明已注明；总评 87 / 100 / 102 仍待客户端核实 |
| C18 | 颜色名更正为 Bright green / Faded green；"无色 = 低"属 T3 | `POSITION_LEVELS` 改为 T1+T3、待客户端核实；标签改为亮绿 / 淡绿；新增位置编码核对项（§5.1、§12.2） |
| C19 | 逆足、抗伤、Form 的逆向数值确认（T3） | Form 不被比赛逻辑读取，是改为 Unwavering 的背景之一（§7） |
| C20 | Acceleration Burst 不加能力值、Phenomenal Finishing +7 确认（T3） | 结论不变 |
| C21 | "所有技能加成都在 99 之前相加"更正：只限直接加能力值的技能；"按能力 +N 瞄准"的技能不受此限 | 新增第 ⑦ 层；新增规则 `SKILL_AIM_UNCAPPED`；公开 PF 的有效值（§1.2、§2.4、§9.2）；删除 Acrobatic Finishing |
| C22 | Operation Sports 计数自相矛盾（"over 20" 但列 19 个）；分组为 5 个标题，防守 7、通用 6 | 特殊技能身份的依据降级说明（§6.1）；Aerial Superiority 按 [S34] 归入防守组 |
| C27 | 网易 [S37] 是 PC 端游《实况足球Online》，不是手游国服 | 不再用作特殊技能旁证；规则表中"国服"字样改为"网易 PC 端游写作" |
| C28 | Attack Outlet 有统计：15 张，其中 CF 6 张 | 补充为风格先例（§5.2） |
| C30 | Epic、Big Time 固定为 "B" 确认（T3） | 新增 `live_update_rating: "B"`（§7） |

如果 04 之后再修改：
1. 先更新 `tools/data/efootball_rules.py`（规则只在这一处）。
2. 再运行 `python3 tools/data/build_r9_2002.py --check` 和测试。
3. 任何受影响的数值都会在校验或测试中暴露。

---

## 14. 文件与字段对照

| 文件 | 内容 |
|---|---|
| `data/game/r9_2002_player.json` | **顶层**：progression_sim.py 的输入，即出厂卡（1 级 26 项、位置、风格、技能、增能、最高等级、Live Update Rating）。<br>**`simulation`**：只用于模拟的推荐加点（`allocation`）和典型主教练假设（`manager`），**不写入客户端**。<br>**`notes.client_apply_fields`**：可以写入客户端的字段白名单。<br>**`notes.identity`**：国籍、出生日期、号码、卡面时刻、card_id 与批次前缀的核对状态。<br>**`notes.progression`**：62 点、推荐加点、成立条件、第二名方案、逐级重分配说明。<br>**`notes.boosters`**：slot 1 的理由、slot 2 的推荐和备选。<br>**`notes.positions` / `playing_styles` / `skills` / `other_fields`**：逐项依据，技能的特殊身份来源和瞄准加成。<br>**`notes.client_checks`**：待客户端核实清单。<br>**`notes.derived`**：自动生成的各阶段能力、各位置总评（公式值和计入位置惩罚的估算）、精确搜索前 5 名、含主教练时的自动加点、逐级能力下降、瞄准型技能有效值、逐级自动分配、点击顺序、比赛中能力、校验结果 |
| `data/game/r9_2002_player.csv` | 平表，每行一项。<br>section 取值：identity / profile / stat / overall / allocation / playing_style / ai_playing_style / card_skill / additional_skill_recommended / booster / manager_assumption。<br>能力行带 1 级、培养增量、满级中性、各层增能、主教练、比赛中（CF/SS/LWF）各列；总评行另有两列计入位置惩罚的估算（T4）。<br>UTF-8 带 BOM，方便 Excel 打开 |
| `data/game/simulation_report.txt` | 模拟器 4 次运行的原样输出：出厂卡、自动提升、精确最优、加推荐制作增能的变体 |
| `tools/data/build_r9_2002.py` | 由 JSON 输入生成上面三个产物；`--check` 只检查是否最新 |
| `tools/tests/test_r9_card.py` | 回归测试：校验结果、产物最新、上限、技能与特殊技能、增能、加点与自动提升、总评与设计红线、模拟字段不在卡数据层、加点差距如实记录、逆足/Form/Live Update Rating |

---

## 审查与修订记录

第 1 版（2026-10-25 17:20）完成后，两名独立审查者分别从足球证据和客户端规则两方面审查了设计。本节逐条列出意见和处理结果。足球审查者直接抓取的 ESPN 7 场比赛 JSON，本组在修订时用自己的脚本逐条复核过与本卡相关的条目（射门、换人、Kaká 出场、左右路记录等）。

### 足球证据审查

| # | 意见 | 处理 | 说明 |
|---|---|---|---|
| A1 | Acceleration 满级中性 91 → 89（1 级 79 → 77）：Smyth "took away the explosiveness"（T2）；DESIGN 用 "less explosive" 压 Speed 却保留加速，自相矛盾；数值、增能、Acceleration Burst 三重叠加 | **接受** | 1 级 77、中性 89、加固定增能 92、CF 比赛中 95。仍高于 Speed 87。"less explosive"改为只用于 Acceleration；Acceleration Burst 保留（§2.1、§2.2、§6.1） |
| A2 | Finishing 92 → 91（1 级 82 → 81）；重写 §2.4：ESPN 汇总不是下限，约 32 次射门中射偏与被扑一样多 | **接受** | 本组复核 ESPN 部分后计约 33 次（8 进、8 偏、约 9 扑、约 8 封堵），结论相同。§2.4 已重写 |
| A3 | 删除 Acrobatic Finishing；重写 Phenomenal Finishing 的证据（#4 是反证，#6 违反 03 §5.3） | **接受** | 卡面技能 7 → 6。PF 证据改为 #1、对哥斯达黎加 13'、决赛 45'，标为 T4 设计选择、证据中等；并公开头球副作用和退路（§2.4、§6.1、§6.3） |
| A4 | 按 04 C21 修正叠加说明：瞄准型技能不受 99 限 | **接受** | 新增第 ⑦ 层、规则 `SKILL_AIM_UNCAPPED`、技能的 `aim_bonus` 字段；模拟器和派生数据输出有效值（PF：98 / 101 / 104）。§1.2、§2.4、§6.1、§9、§10 和 JSON `notes.skills` 均已改 |
| A5 | §8.2 不以 Kaká Epic 为同档参照（2002 年只替补一次） | **接受** | 已用 ESPN 数据确认 Kaká 只在对哥斯达黎加 72' 替补出场。锚点改为 Ronaldinho BT 21 Jun '02、Rivaldo E02；Romário E94 作纯禁区前锋参照；Kaká 只列作对照（§8.2，JSON `design_targets`） |
| B6 | Dribbling、Tight Possession 86 → 88（1 级 82 → 84）：被压了两次 | **接受** | 长途奔袭已由 Speed 87、Stamina 66、不给 Momentum Dribbling / Mazing Run / Speeding Bullet 压住。88 在 03 数值带内，与 Romário 相同；反证（被三人围住、被 Simons 顶住）不支持更高 |
| B7 | 逆足频率 Regularly → Occasionally；精度保持 High | **接受** | "生涯能用双脚"跨年份，违反只做 2002；同组卡没有 Regularly（§7） |
| B8 | Form Standard → Unwavering（T4），或保留 Standard 但重写理由 | **接受前者** | Standard 的理由是伤病证据，已用于抗伤和 Stamina，再用就是计三次；7 场 6 场进球支持 Unwavering。标为 T4（§7） |
| B9 | 附加技能推荐删去 Fighting Spirit 和 Chop Turn；One-touch Pass 证据升为中 | **接受** | §6.2、§6.3 |
| C10 | Lofted Pass 67 → 69（1 级 69） | **接受** | 4 次传中制造机会是比赛证据；69 在 03 数值带下部，CF 权重 12 |
| C11 | Trickster 保留，证据补成 2002 年实例，注明 Smyth 的话指 1990 年代 | **接受** | §5.2、JSON `playing_styles.ai` |
| C12 | LWF 保留，说明为什么只给左路（右路也有记录） | **接受** | 左路 5 次多数形成机会，右路 3 次都没有；RWF 可由玩家位置训练（§5.1） |
| C13 | 排除理由改为证据理由：Attacking Surge、Magnetic Feet | **接受** | 两条都改为 2002 年的比赛证据；Attacking Surge 的机制重叠降为附注（§6.3） |
| C14 | 03 #6 的 53' Ramelow 改为"仅有 T3 单一来源支持" | **接受** | 已用 ESPN 原始数据确认该条（§13） |
| D | 与 04 核查记录对账：§13 过时；C21；C16；C18；C27/C22；C28；C20 | **接受** | 见 §13 的逐条表 |
| E | 可保留项（Attacking Awareness 93、Speed 87、Stamina 66 等） | **同意** | 未改动 |
| F | 修改后的数值影响：1 级 87、满级 100、加固定 102、两槽 102、主教练 107；满级 raw 约 100.06 | **已复现** | 本组模拟结果一致：raw 100.059 / 102.501 / 102.833 / 107.258。"离 99 只差 0.06"已写入 §8.1 和验收步骤 |

### 规则审查

| # | 意见 | 处理 | 说明 |
|---|---|---|---|
| H1 | §13 过时，改为按 C 条目的对账表 | **接受** | §13 |
| H2 | 瞄准型技能不受 99 限；PF 对头球也适用；PF 与 AF 是否叠加未知 | **接受** | 同 A3、A4。`SKILL_BONUS_BEFORE_CAP` 限定为直接加能力值的技能；新增 `SKILL_AIM_UNCAPPED`（Through Passing 有原文 T3，其余 T4）；§2.4 按有效值 98–104 重新校准 |
| H3 | "唯一最优"的差距写错（实为 13/1000，不是 136/1000）；"与客户端算法无关"过强；等级上限应按卡种定 | **接受** | 新增精确前 k 名搜索 `top_allocations()`，派生数据输出前 5 名和差距；32 级的理由改为"同批次唯一的 CF（Romário 1994）是 32 级"；验收步骤列出两种可能的自动结果（§3.1、§3.3、§11） |
| H4 | "推荐 = 自动提升"只在自动提升不计主教练时成立 | **接受** | 写明两个前提；派生数据列出改变方案的熟练度（efx：72、86 及多数 <62 的值；pesdb：72、82、84、86）；熟练度 89 不变；新增规则 `AUTO_DEV_MANAGER` 和核对项；模拟器新增 `--auto-with-manager`，用于验收时复现这种情形（§3.2、§11、§12.2） |
| M1 | 7 个卡面技能缺少来源支持（抽样卡都是 10 个） | **接受（选"保留少于 10 个 + 核对项"）** | 删去无来源的"标准卡常少于 10 个"；新增规则 `SKILL_CARD_COUNT_TYPICAL`；模拟器对 Epic/Big Time/Show Time 卡少于 10 个技能给出 INFO；给出"客户端硬性要求 10 个"时的处理办法。不用无证据的技能填满，因为两名审查者都要求删去弱证据技能 |
| M2 | 位置适性标签、分级与编码（C18） | **接受** | `POSITION_LEVELS` 改为 T1+T3、待客户端核实；`PROFICIENCY_ZH` 改为亮绿 / 淡绿 / 无色（T3）；模拟器接受亮绿 / 淡绿（仍兼容深绿 / 浅绿）；JSON、CSV、§12.2 加入编码核对项 |
| M3 | 各位置总评没有计入位置惩罚，也没说明 | **接受** | §8.1 标明"公式值"，并新增计入位置惩罚的估算行（T4）：满级加固定增能 SS 93、LWF 91；主教练 89 时 SS 97、LWF 96；CF 不变。新增函数 `overall_in_position()` |
| M4 | 顶层 `allocation`、`manager` 有被误写进客户端的风险 | **接受（两项都做）** | 移到 `simulation` 下（模拟器同时兼容旧的顶层写法，两处同时出现时报 ERROR）；新增 `notes.client_apply_fields` 白名单；新增测试 |
| M5 | card_id 批次前缀可能让本卡进入现有卡包（任务书 §14） | **接受** | 新增核对项，建议主控分配前缀；card_id 本身不改（它是任务书给出的历史候选） |
| M6 | 缺少 Live Update Rating 字段 | **接受** | 新增 `live_update_rating: "B"`（T3 [S29]，待客户端核实）；模拟器新增枚举、校验和报告行 |
| M7 | 特殊 / 可训练分类的来源比表面上弱 | **接受** | `SKILL_SPECIAL` 去掉 [S37]，注明 [S34] 的矛盾；JSON 每个技能新增 `special_source` 和 `client_check` |
| L1 | `STEP_COST` 说"13 次以后是外推"（C13 已更正） | **接受** | 规则说明已改 |
| L2 | Aerial Superiority 按 [S34] 应在防守组 | **接受** | 已移入防守组；`test_progression.py` 的分组断言相应改为防守 7、通用 6 |
| L3 | 规则表中的"国服"写法来自 PC 端游（C27） | **接受** | 改为"网易 PC 端游写作"；别名表注明只用于名称解析 |
| L4 | `RATING_FORMULA` 引用的全量复算已标【未核实】（C16） | **接受** | 规则说明已改 |
| L5 | 给 04 §6.5 的反馈：efx 公式与 pesdb 门槛表在 p = 73、81、82、84、85、86、90 处不一致，不是"同一回事" | **记录** | 本卡用 p = 89，两者一致，无影响。04 不由本组修改，反馈记录在此和 `MANAGER_THRESHOLDS` 规则说明中 |
| L6 | 1 级 SS 88 > CF 87 的解释用了不可比的 AMF 卡；1 级 Finishing < Ball Control 与"严格按 03 排序"矛盾 | **接受（改文字，不改数值）** | 三张 CF 参考卡 1 级都是 SS ≤ CF，本卡属非典型；原因和不重新平衡的理由写在 §8.1；§1.3 改为"满级按 03 的档次，1 级不严格保持" |
| L7 | 逆足频率 Regularly 是取值问题 | **接受** | 同 B7 |
| L8 | 每级整体重分配会让个别能力在升级时下降 | **接受（写明是正常现象）** | 派生数据列出下降的等级和能力（21、22、24、25、31、32 级）；§3.2 和验收步骤说明 |
| L9–L10 | — | **未收到** | 规则审查意见的文本在 L8 中途被截断，L9、L10 没有送达，无法处理 |

### 修订后的核心数值变化

| 项目 | 第 1 版 | 第 2 版 |
|---|---|---|
| 1 级 Acceleration / Finishing / Dribbling / Tight Possession / Lofted Pass | 79 / 82 / 82 / 82 / 67 | 77 / 81 / 84 / 84 / 69 |
| 满级中性 Acceleration / Finishing / Dribbling / Tight Possession / Lofted Pass | 91 / 92 / 86 / 86 / 67 | 89 / 91 / 88 / 88 / 69 |
| 逆足频率 / Form | Regularly / Standard | Occasionally / Unwavering |
| 卡面技能 | 7 个（含 Acrobatic Finishing） | 6 个 |
| 附加技能推荐 | One-touch Pass、Fighting Spirit、Chop Turn | One-touch Pass |
| 新字段 | — | `live_update_rating`；`simulation`；`notes.client_apply_fields` |
| 推荐加点 | Shooting 10 / Dribbling 4 / Dexterity 12 / Aerial 4 / Lower Body 8 | 不变 |
| CF 总评：1 级 / 满级 / +固定 / +两槽 / +主教练 | 87 / 100 / 102 / 103 / 107 | 87 / 100 / 102 / 102 / 107 |
