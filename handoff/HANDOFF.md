# 主控 Claude 接入包：2002 世界杯罗纳尔多（R9）

> 读者：负责 MyTEAM 本地单机框架的主控 Claude。**不需要读聊天记录**，本文件和文中链接的文件就是全部信息。
> 范围：只有这一名球员的数据与资产。**不要**据此改动卡池、经济、奖励、T0 获取资格（任务书 §14）。
> 状态标签：✅ 已完成且验证 ｜ 🟡 已完成候选、等待实机 ｜ ⛔ 技术受阻 ｜ 📥 缺少原始输入。
> 本组没有接触任何 eFootball iOS 原始文件，所以**没有可安装的原生补丁**，下面的"接入"分为现在能做的（数据）和拿到文件后才能做的（资源）。

## 1. 交付内容

| 内容 | 文件 | 状态 | 说明 |
|---|---|---|---|
| 球员卡数据（26 项能力、等级上限、成长、技能、增能、位置、风格、逆足、状态、伤病） | `data/game/r9_2002_player.json`（机器可读）、`r9_2002_player.csv`（平表）、`DESIGN.md`（依据与取舍） | 🟡 | 取值写法来自 pesdb/社区，**不是客户端内部编码**；只写 §3 列出的字段 |
| 培养/总评模拟器 | `tools/data/progression_sim.py`、`tools/data/efootball_rules.py`、`data/game/simulation_report.txt` | ✅ 计算正确性 / 🟡 规则本身 | 在 99 张真实卡上复算一致；导入后用它复核：`python3 tools/data/progression_sim.py data/game/r9_2002_player.json` |
| 身体 14 项参数候选与外观表编解码器 | `data/body/r9_body_params.json`、`data/body/BODY_PARAMS.md`、`tools/data/player_appearance.py`、`data/body/layouts/` | 🟡 / 📥 | 必须先用解码后的 `PlayerAppearance` 表核对布局（`docs/NATIVE_FILE_REQUEST.md` B3） |
| 3D 头部与身体（可编辑高清源） | `model/source/r9_2002.blend`、`model/export/r9_2002_hd.glb`、`model/textures/`；重建：`tools/build_all.sh` | ✅ 离线模型 | 游戏里用的是"包裹到原生供体拓扑"后的版本（📥） |
| 供体包裹与贴图转移工具 | `tools/wrap/`（说明见 `tools/wrap/README.md`） | ✅ 合成供体 / 📥 真实供体 | 保持供体的顶点数、顺序、索引、UV、骨骼与权重逐位不变 |
| 原生容器工具（IoStore 读写、Zen 包、ASTC、等长原位替换、新增/克隆资源包） | `tools/native/`（`README_iostore.md`、`README_zen_inplace.md`） | ✅ 合成数据与真实安卓容器头 / 📥 iOS 文件 | 全部"写新文件 + 清单"，不覆盖原件 |
| 原生资源（头部两级 LOD、256 px ASTC、容器补丁） | 尚未生成 | 📥 | 需要 `docs/NATIVE_FILE_REQUEST.md` A/B 组文件 |
| 卡面 | `card/out/`（`card_full/squad/detail/pack/back.png`、分层 `10–50_*.png`、`card_spec.json`），`card/layers/` 源层，生成脚本 `tools/card/` | 🟡 | 原生卡图接口未知；字体 OFL，许可在 `card/fonts/` |

## 2. 不可违反的约束

1. **`card_id 88044146203441`（0x5013600F1B31）是历史候选，导入前先查你的数据库。** 存在就更新该记录（先备份），不存在才新增；不得出现两张 2002 大罗。
2. **批次前缀风险**：高 24 位 `0x501360` 与官方 Epic Brazil 2002/1994 批次相同（`research/notes/04` §8.4，推断）。如果你的项目按批次前缀或 `card_type` 组织卡包或精选列表，这个 ID 可能把 R9 放进现有 Epic 卡包，属于卡池改动，任务书禁止。本组没有改它（它是任务书给的历史值），**请你决定是否换一个你项目内部未使用的前缀**，换了以后同步改 `card_id` 字段和你自己的引用。
3. 不改其他球员记录；不改共享资源。亨利项目覆盖共享脸部槽 110718 的做法会让其他用这个槽的球员也变脸，**禁止在正式包使用**。
4. 所有导入脚本必须可重复执行、可撤销（`rollback/ROLLBACK.md`）。
5. 修改后的客户端只用于本地单机，不接官方在线服务。

## 3. 写哪些字段

`r9_2002_player.json` 的顶层同时也是模拟器的输入格式，**不能整份写入客户端**。

- **只写** `notes.client_apply_fields.fields` 列出的 21 个字段：`name, card_id, card_type, registered_position, position_proficiency, height_cm, weight_kg, age, stronger_foot, weak_foot_usage, weak_foot_accuracy, form, injury_resistance, live_update_rating, max_level, stats, playing_style, ai_playing_styles, skills, additional_skills, boosters`。
- **永远不写** `simulation`、`notes`、`schema`、`_comment`。
  - `simulation.allocation` 是满级时的推荐加点（复现游戏的"自动提升"）。写进 1 级卡会让卡带着 62 点已分配的培养、可用点数为 0，1 级总评直接变成 100。
  - `simulation.manager` 是典型主教练假设。写进去会让主教练加成叠加两次。
- `stats` 是 **1 级、未培养、无增能**的 26 项基础值；满级上限由 `max_level` 和客户端的成长规则算出，不要写满级值。
- `boosters`：slot 1 固定 `Striker's Instinct +3`；slot 2 出厂为空的可制作槽。如果你的格式不支持空槽，就只写 slot 1，**不要**替玩家填 slot 2。
- 技能、增能、风格目前是英文名称。你的数据库若用数字 ID，请按你的 ID 表映射，并在导入日志里记下每一项映射；找不到的项要报错，不要静默丢弃。

逐字段对应到你的表结构（列名、类型、枚举 ID）要等拿到 `docs/NATIVE_FILE_REQUEST.md` A4 后才能补全。在那之前，以上就是完整规则。

## 4. 需要你做的决策

| 问题 | 本组建议 | 依据 |
|---|---|---|
| 卡面技能 6 个，抽样所见官方卡多为 10 个 | 保持 6 个。如果客户端强制 10 个：有比赛证据的候补只有 One-touch Pass；其余 3 个由你另作设计决定并记录，**不要用无证据的技能悄悄填满**（`DESIGN.md` §6.1） | 2002 年证据只支持这 6 个；模拟器对此只给 INFO，不是错误 |
| `card_id` 批次前缀 | 换成你项目内部前缀 | §2.2 |
| 总评公式 | 先用实机详情页核对 1 级 87 / 满级 100 / 满级加固定增能 102 三个值 | 公式来自社区逆向（`research/notes/04`） |

## 5. 拿到原始文件后的资源接入顺序

前提：用户提供 `docs/NATIVE_FILE_REQUEST.md` A 组文件（`Info.plist`、`pc3600_mobile_ios` / `pc3611_mobile_ios` 的 `.utoc`、亨利项目 Codex 输出、主控数据库结构）。

1. 用 `tools/native/iostore.py` 读 `.utoc`，确认版本、加密标志、压缩方式（Oodle 时需要用户自己提供库）。
2. 找"球员 → 脸部资源包"的映射规则：这是独立绑定的关键，目前**未知**（⛔，只能在真实文件里找）。没有找到之前，不做任何会影响共享槽的写入。
3. 选一个供体头（建议亨利项目用过的同代资产，LOD0 约 5,376 / LOD1 约 1,338 顶点），解出网格与骨架，用 `tools/wrap/wrap.py` 包裹到 R9 高清头，`tools/wrap/transfer_texture.py` 生成 256 px ASTC 贴图。
4. 用 `tools/native/iostore.py` 的克隆/新增资源包功能生成 **R9 自己的新资源包 ID**，不改供体包。
5. 用新包写一份独立的补丁容器和清单；原件不动。按 `rollback/ROLLBACK.md` 备份。
6. 实机验收：`docs/ACCEPTANCE_PLAN.md`。

## 6. 已知风险与未验证项

见 `docs/VERIFICATION.md` §9。最重要的三条：

- 客户端字段编码、技能/增能 ID、总评公式都没有在客户端上验证。
- 球员到脸部资源的映射未知，独立绑定的最后一步还没做。
- 相似度：3 名评审给 4–5/10，共识修改已应用，但修改后没有再做评审打分，最终要由用户在实机上判断。
