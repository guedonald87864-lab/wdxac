# 主控 Claude 接入包：2002 世界杯罗纳尔多（R9）

> 读者：负责 MyTEAM 本地单机框架的主控 Claude。**不需要读聊天记录**，本文件 + 文中链接的文件就是全部信息。
> 范围：只有这一名球员的数据与资产。**不要**据此改动卡池、经济、奖励、T0 获取资格（任务书 §14）。
> 状态标签：✅ 已完成且验证 ｜ 🟡 已完成候选、等待实机 ｜ ⛔ 技术受阻 ｜ 📥 缺少原始输入。

## 1. 你要接入的东西

| 内容 | 文件 | 状态 | 说明 |
|---|---|---|---|
| 球员卡数据（26 项能力、等级上限、成长、技能、增能、位置、风格、逆足、状态、伤病） | `data/game/r9_2002_player.json`（机器可读）、`r9_2002_player.csv`（平表）、`DESIGN.md`（依据） | 🟡 | 字段名/取值写法来自社区资料（pesdb 等），**不是客户端内部编码**。导入时按你的数据库字段映射，见 §3 |
| 培养模拟与校验 | `tools/data/progression_sim.py`、`tools/data/efootball_rules.py`、`data/game/simulation_report.txt` | ✅（社区规则下的计算正确性） / 🟡（规则本身待客户端核实） | 用来复核你导入后的数值：`python3 tools/data/progression_sim.py data/game/r9_2002_player.json` |
| 身体 14 项参数候选 + 外观表编解码器 | `data/body/r9_body_params.json`、`data/body/BODY_PARAMS.md`、`tools/data/player_appearance.py` | 🟡 / 📥 | 必须先拿到解码后的 `PlayerAppearance` 表核对布局（见 `docs/NATIVE_FILE_REQUEST.md` B3） |
| 3D 头部/身体（可编辑源） | `model/source/r9_2002.blend`、`model/export/r9_2002_hd.glb`、`model/textures/` | ✅（离线模型） | 这是高精度制作版；游戏内用的是"包裹到原生供体拓扑"后的版本（📥 需要原始资源） |
| 原生资源（头部网格两级 LOD、ASTC 贴图、容器补丁） | 尚未生成 | 📥 | 需要 `docs/NATIVE_FILE_REQUEST.md` A/B 组文件 |
| 卡面 | `card/`（分层 PNG + 合成图 + `card_spec.json`） | 🟡 | 原生卡图接口未知，先交源文件与尺寸规范 |

## 2. 不可违反的约束

1. `card_id 88044146203441` 是历史候选：**导入前先查你的数据库**。存在就更新该记录（先备份），不存在才新增；不得出现两张 2002 大罗。
2. 不改其他球员记录；不改共享资源（亨利项目覆盖共享脸部槽 110718 的做法会让别的球员也变脸，禁止在正式包使用）。
3. 所有导入脚本可重复执行、可撤销（回退方案：`rollback/ROLLBACK.md`）。

## 3. 数据字段映射（待补：拿到你的数据库结构后逐项对应）

本节在拿到你的球员/卡片表结构（`docs/NATIVE_FILE_REQUEST.md` A4）后补全为逐字段映射表。在此之前请按 `data/game/r9_2002_player.json` 中的英文字段名理解含义。

## 4. 验收（实机）

见 `docs/ACCEPTANCE_PLAN.md`。
