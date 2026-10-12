# drl-research-commander

统筹 DRL 资源分配论文实验全流程的 Codex skill：**主帅 + 临时子代理**协作、**G0–G9 阶段闸门**验收、**可复核记忆**与**上下文轮换**。让每一步实验与论文结论都有可复核的依据。

## 核心规则

所有角色共同遵守：

1. **学习方法禁令**：所提方法及训练路径禁止模仿学习、专家示范、教师策略监督/蒸馏、专家或启发式策略暖启动、物理引导网络或损失、人工调度优先级和额外奖励塑形；不得借"加速收敛""辅助监督""预训练"等名义绕过。
2. **允许的边界**：真实物理仿真、原始优化目标、传统比较基线、标准 DRL 算法、只表达硬约束的可行性处理。动作掩码只能排除非法动作；投影只能按明确、与业务收益无关的规则修复可行性。
3. **奖励与特征审查**：问题规格中原本定义的权重与指标不算偏置；为让某方法获胜而加的奖励项、未来信息、优化器标签、人工优先级特征属于违规。
4. **贡献与证据匹配**：接受组合、适配和弱创新；不把"未检索到"等同于"从未有人做过"，不刻意造有利环境。
5. **事实分层**：区分原文报告、本地复现、推断与未知；每个关键结论附原文位置、产物路径或验证输出；保留失败与负结果。
6. **权限不扩张**：任务书写清可读/可改范围与预算；创建聊天、跨聊天发消息、新增付费资源等动作必须有可核验的用户直接授权。

## 阶段闸门

```
G0 需求 → G1 文献 → G2 主线与建模冻结 → G3 环境验证 → G4 基线复现
→ G5 最小方案与试验 → G6 正式设计冻结 → G7 主实验/消融/泛化
→ G8 独立审查 → G9 论文结论
```

主帅逐阶段核验出口证据；未解释的环境/基线失败必须先修复；跨阶段并行只允许不依赖未通过闸门的工作。**通过闸门不要求新方法获胜。**

## 角色与按需加载

| 角色 | 规范 | 工作边界 |
|---|---|---|
| 主帅 / 接任主帅 | `references/commander.md` | 维护当前状态、验收、预算和调度所有权 |
| 文献调查者 | `references/literature.md` | 只读研究材料，写指定文献报告 |
| 思路分析者 | `references/analyst.md` | 形成可检验的主线，写指定设计报告 |
| 实验实施者 | `references/executor.md` | 只改任务书指定代码和实验产物 |
| 独立审查者 | `references/reviewer.md` | 只读成品和冻结规格，写审查报告 |

一次只加载当前角色所需规范；阶段/模板/交接文档按需读取 `experiment-protocol.md`、`templates.md`、`context-handoff.md`。

## 仓库结构

```
drl-research-commander/
├── SKILL.md                        # 入口：角色表、共同规则、阶段、派发与验收
├── LICENSE                         # MIT
├── README.md
├── agents/
│   └── openai.yaml                 # 界面定义（显示名、默认提示、隐式调用策略）
├── references/                     # 各角色规范与协议
│   ├── commander.md                #   主帅协议
│   ├── literature.md               #   文献调查规范
│   ├── analyst.md                  #   思路分析规范
│   ├── executor.md                 #   实验实施规范
│   ├── reviewer.md                 #   独立审查规范
│   ├── experiment-protocol.md      #   实验闸门
│   ├── templates.md                #   项目模板与文件接口
│   └── context-handoff.md          #   上下文与交接
├── scripts/
│   ├── context_policy.py           # 会话 ID 绑定、80/90/2 上下文策略
│   ├── project_control.py          # 唯一调度所有权、交接验包、运行登记
│   └── record_decision.py          # decisions.jsonl 追加与校验
├── tests/
│   ├── test_context_policy.py      # 上下文策略 + 阈值配置 + reader 兼容性
│   ├── test_project_control.py     # 项目控制测试
│   ├── test_record_decision.py     # 决策账本测试
│   └── validation.md               # 交付验收记录
└── assets/                         # 论文项目运行时文件模板
    ├── state.md                    #   当前状态
    ├── task.md                     #   角色任务书
    ├── authorization.md            #   项目授权与来源
    ├── formal-design.md            #   正式设计冻结件
    ├── handoff-manifest.json       #   交接清单
    └── run-record.json             #   运行登记
```

## 依赖

- **Python 3.11+**，脚本只用标准库，无需安装第三方包。
- **`context-gauge` skill（必需）**：`scripts/context_policy.py` 复用它的会话解析来读取真实上下文用量，默认在其**兄弟目录**查找：

  ```
  <skills>/
  ├── context-gauge/scripts/context_gauge.py
  └── drl-research-commander/scripts/context_policy.py
  ```

  找不到兄弟目录时会回退到 `CODEX_HOME`（默认 `~/.codex`）下的同一相对路径；非标准位置用 `--gauge-path` 显式指定。该 reader 的接口会被校验，缺失或接口变化时脚本明确报错并把轮换动作标为未知，不会静默算出错误的占用率。

## 安装

将本仓库放入 Codex 的 skill 目录（用户级为 `~/.codex/skills/`），与 `context-gauge` 并列：

```
~/.codex/skills/context-gauge/
~/.codex/skills/drl-research-commander/SKILL.md
```

重启或刷新宿主后，可通过 `skills/list` 确认发现（`scope=user`、`enabled=true`）。

## 使用

默认提示词：

> 使用 $drl-research-commander 统筹我的 DRL 资源分配论文实验，在本项目内派发临时子代理，并在需要轮换时自动创建全新接任主帅聊天、发送交接信息。

也可直接提供论文概要（默认作为主帅）、角色任务书（按其中角色执行）或交接启动语（作为接任主帅）。普通单文件编程不必启动整套流程。

项目材料统一放在论文项目的 `.codex-run/drl-research/` 下，不放到 skill 安装目录；决策历史、文献、报告与原始运行分开保存。

## 测试

在 skill 目录内，或从任意位置用 `-t` 指定顶层目录：

```powershell
# 在 skill 目录内
& $researchPython -I -B -m unittest discover -s tests -q

# 从任意位置（必须用 -t 指定顶层目录，否则会报 "Start directory is not importable"）
& $researchPython -I -B -m unittest discover -s "<skill路径>\tests" -t "<skill路径>\tests" -q
```

共 112 项：项目控制、决策账本、阈值配置与 reader 兼容性等测试在有/无 `context-gauge` 时都会运行；36 项上下文策略测试**需要真实 reader**，在新克隆（无 `context-gauge`）时以明确原因 **skip 而不是 fail**，因此 clone 后直接跑仍会得到有效结果（`OK (skipped=36)`），不安装依赖时不会出现假失败。全部使用临时项目、合成 JSONL 和惰性运行记录，不启动训练或创建聊天。验收与独立前向验证记录见 `tests/validation.md`。

## 许可

MIT，见 `LICENSE`。

## 说明

- 自动轮换依赖目标宿主的实际工具与具体项目的直接用户授权；本 skill 只在已授权范围内自动推进。
- 上下文策略为 **80% 准备、90% 交接；第二次压缩交接**；只用当前实际型号和宿主上报窗口。
