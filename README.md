# 2002 世界杯罗纳尔多（R9）完整球员制作 · 云端工程

> 目标：为用户的 eFootball iOS 本地单机 MyTEAM 构建，制作 2002-06-30 世界杯决赛版 Ronaldo Nazário（25 岁、9 号、CF）的完整球员：3D 头部与身体、Cascão 发型、能力/技能/成长/增能数据、身体 14 项参数、原生资源工具链、卡面、主控接入包与回退方案。
> 任务书全文：[`docs/TASK_BRIEF.md`](docs/TASK_BRIEF.md)。分支：`r9-2002`。
> **重要：本仓库是公开仓库。不要上传任何游戏原始文件或密钥**，见 [`docs/NATIVE_FILE_REQUEST.md`](docs/NATIVE_FILE_REQUEST.md) §0。

## 1. 当前状态（2026-10-25）

图例：✅ 已完成且验证 ｜ 🟡 已完成候选、等待实机 ｜ ⛔ 技术受阻（附证据） ｜ 📥 缺少原始输入（需要你提供）

**一句话：第一部分（不依赖游戏文件的全部工作）已完成；第二部分（原生资源接入、独立绑定、实机）需要你提供 `docs/NATIVE_FILE_REQUEST.md` 中的 A 组文件后才能进行，现在不能声称已生成可安装的原生补丁。**

| # | 交付物（任务书 §13） | 状态 | 说明与证据 |
|---|---|---|---|
| 1 | README | ✅ | 本文件 |
| 2 | 人物研究 | ✅ | `research/notes/01–06`，6 份资料均经独立对抗核查（每份末尾有"核查记录"）；参考图索引 `research/refs/REFERENCE_INDEX.md`；入库图片仅限 CC 授权并有署名 |
| 3 | 3D 源文件 | ✅（离线模型） | `model/source/r9_2002.blend`（骨架、头、身、眼、牙、Cascão 发壳）、`model/export/r9_2002_hd.glb`、`model/textures/`；全部由 `tools/build_all.sh` 从参数文件一键复现 |
| 4 | 游戏能力数据 | 🟡 | `data/game/`：26 项能力（1 级/满级/增能/主教练分层）、等级上限 32、成长点 62、加点方案、位置、风格、技能、增能、逆足、状态、伤病、各位置总评；规则与总评公式来自社区逆向，在 99 张真实卡上复算一致，但**客户端编码待核实** |
| 5 | 身形数据 | 🟡 / 📥 | `data/body/`：实测模型数据（183.0 cm、83.2 kg 体积估算）、14 项参数候选（0–14）及依据、编解码器（用 3 名真实球员字节向量验证）；**写入前需要你的解码后 PlayerAppearance 表** |
| 6 | 原生资源产物 | 📥 | 工具已就绪并在合成数据 + 真实 eFootball 安卓容器头上验证；**没有 iOS 原始资源，尚未生成任何原生补丁** |
| 7 | 视觉对照 | ✅（高清）/ 🟡（游戏精度预览） | `renders/hd/`（头部 8 视角 + 全身正侧背 3/4）；`renders/game_precision/`（按亨利供体预算 5,376/1,338 顶点、256 px ASTC 的预览，非真实游戏渲染） |
| 8 | 验证报告 | ✅（已做部分） | `docs/VERIFICATION.md`：所有实际运行过的测试与数值；未做的明确列出 |
| 9 | 主控接入包 | 🟡 | `handoff/HANDOFF.md` |
| 10 | 回退方案 | ✅（方案） | `rollback/ROLLBACK.md`；工具层"不覆盖原件、写清单"有测试覆盖 |

### 按任务书要求逐项

| 要求 | 状态 | 依据 |
|---|---|---|
| 真实脸模与五官 | 🟡 | 照片引导拟合（9 张 2002 年 6 月照片，检测器偏差闭环校正）+ 颅骨轮廓拟合 + 3 名独立评审修订；面部比例与照片在同一检测器下平均偏差多数在 ±6% 内（`data/face/ratio_check_final.json`）。最终相似度只能由你在实机/详情页判断 |
| 2002 决赛 Cascão 发型 | 🟡 | 真实几何发壳（非贴图画块），尺寸按照片叠图迭代；发区边界、发际线、剃短头皮发茬色均有依据；远景 LOD 表现见游戏精度预览，实机待验 |
| 身体比例 | ✅（模型） / 🟡（游戏） | 身高与胸/腰/臀/大腿/小腿/上臂围、肩宽、髋膝高度命中厘米目标；颈围按照片有意加粗到 41.8（目标 40）；游戏内由 14 项参数近似，需实机校准 |
| 能力、技能、成长、增能 | 🟡 | 见第 3 节；规则待客户端核实 |
| 原生模型/骨架/材质/动画兼容 | 📥 | 供体包裹工具保持顶点数/顺序/UV/骨骼权重逐位不变（合成供体验证）；动画只在本地骨架上做了形变测试 |
| 独立球员身份（不覆盖共享资源） | 📥 / ⛔ 部分 | 工具能新增/克隆独立资源包（CUE4Parse 交叉验证）；但"球员 → 脸部资源"的映射规则只能在真实文件里找到 |
| 卡面 | 🟡 | `card/`：分层源文件 + 合成图 + 尺寸规范；原生卡图接口未知 |
| 实机验收 | 📥 | `docs/ACCEPTANCE_PLAN.md` |

## 2. 快速使用

```bash
tools/setup_env.sh                      # 安装 Blender 4.2.23、astcenc 4.8.0、MakeHuman CC0 数据、MediaPipe
tools/build_all.sh                      # 从 model/source/*.json 复现全部 3D 交付物与渲染（--quick 为低精度快速版）
python3 -m pytest tools/tests -q        # 全部单元/合成测试
python3 tools/data/progression_sim.py data/game/r9_2002_player.json      # 培养与总评模拟
python3 tools/native/iostore.py --help                                    # 原生容器工具（拿到文件后使用）
```

## 3. 关键数值

**卡数据（CF，社区逆向公式，待客户端核实）**：1 级总评 87 → 满级 100 → 满级 + 固定增能 102 → 再加典型主教练熟练度 106–107。等级上限 32，成长点 62，推荐加点 Shooting 10 / Dribbling 4 / Dexterity 12 / Aerial 4 / Lower Body 8（与游戏"自动提升"的两种已知算法结果一致）。

| 能力 | 1 级 | 满级 | +固定增能 |
|---|---|---|---|
| Attacking Awareness | 81 | 93 | 96 |
| Ball Control | 84 | 88 | 91 |
| Dribbling / Tight Possession | 84 / 84 | 88 / 88 | 88 / 88 |
| Finishing | 81 | 91 | 94 |
| Speed / Acceleration | 79 / 77 | 87 / 89 | 87 / 92 |
| Physical Contact / Balance | 81 / 74 | 85 / 86 | 85 / 86 |
| Low / Lofted Pass | 72 / 69 | 72 / 69 | 72 / 69 |
| Heading / Jumping | 64 / 64 | 68 / 68 | 68 / 68 |
| Kicking Power / Curl / Set Piece | 77 / 67 / 58 | 85 / 77 / 68 | 85 / 77 / 68 |
| Stamina | 58 | 66 | 66 |
| 防守四项 | 42–50 | 42–50 | 42–50 |

- 位置：CF 高，SS / LWF 中。风格：进攻 Goal Poacher，防守 Attack Outlet，AI 风格 Trickster。
- 技能（6）：Scissors Feint、Cut Behind & Turn、Acceleration Burst、First-time Shot、Phenomenal Finishing、Gamesmanship。若客户端强制 10 个卡面技能，由主控按 `DESIGN.md` §6.1 决策，不要静默填充。
- 增能：slot 1 固定 Striker's Instinct +3；slot 2 出厂为空的可制作槽（推荐玩家自制 Ball Protection +1）。
- 右脚；逆足频率 Occasionally、精度 High；状态 Unwavering；伤病耐受 Low。
- 设计依据（含对旧稿"速度 91 / 加速 91 / 射门 95"的取舍）：`data/game/DESIGN.md`。

**身体（模型实测）**：183.0 cm，体积估算 83.2 kg（目标 82 ± 2），颈围 41.8，胸围 102，腰围 82.5，臀围 99.6，大腿围 60，小腿围 38.8，上臂围 31，肩峰宽 42，髋关节高 96.6，膝高 51.6；头高 22.4、头宽 16.0、头长 20.8（cm）。

**身体 14 项参数候选（存储值 0–14，中性 7）**：颈长 5、颈围 11、肩高 8、肩宽 10、胸围 9、腰围 8、臂围 8、臂长 7、大腿围 11、小腿围 7、腿长 6、头长/头宽/头深 7（实机校准）。见 `data/body/BODY_PARAMS.md`。

## 4. 需要你做的事

1. 先把仓库改为私有，或给我一个私有仓库用于上传原始文件。
2. 按 [`docs/NATIVE_FILE_REQUEST.md`](docs/NATIVE_FILE_REQUEST.md) 提供 A 组文件（`Info.plist`、两个 `.utoc`、亨利项目的 Codex 输出、主控数据库结构说明）。
3. 审美确认：看 `renders/hd/` 与卡面，告诉我哪里最不像（这一步只有你能判断）。

## 5. 目录

```
docs/        任务书、文件清单、验收计划、验证报告
research/    6 份研究资料（含核查记录）、参考图索引、CC 授权图片
model/       参数源文件、.blend、GLB、贴图
data/        game（卡数据与模拟）、body（身形与参数）、face（拟合与核验数据）
tools/       model（建模与渲染）、face（照片拟合）、wrap（供体包裹、贴图转移、游戏精度）、
             native（IoStore/Zen/ASTC/原位替换/新增资源）、data（规则与模拟、外观编解码）、card、tests
renders/     高清与游戏精度视觉对照
card/        卡面分层源文件与合成图
handoff/     主控接入包
rollback/    回退方案
```

## 6. 许可与素材

- 人体基础网格、形变目标、骨架与权重：MakeHuman 1.x 数据，CC0。
- 字体：Oswald、Barlow Condensed（SIL OFL，许可文本在 `card/fonts/`）。
- 研究图片：仅 Wikimedia Commons 自由授权图片入库，署名见 `research/refs/commons/ATTRIBUTION.jsonl`；新闻照片只列链接，只在会话临时目录中参考，未入库。
- 外部工具：Blender（GPL）、ARM astcenc（Apache-2.0）、MediaPipe（Apache-2.0）、CUE4Parse（Apache-2.0，仅用作独立交叉校验）。
- 修改后的游戏只用于你的本地单机构建，不要接入官方在线服务（研究资料 05 §5.3）。
