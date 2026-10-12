# 项目模板与文件接口

只读当前要创建的模板。模板是项目产物的起点；将所有示例路径、空值和未知补为真实事实后才执行命令，不能把空模板当成通过验收。

## 目录与写入归属

在真实论文项目创建 `.codex-run/drl-research/`：

| 位置 | 用途与写入者 |
|---|---|
| `state.md`、`authorization.md` | 主帅维护当前状态与用户直接授权来源 |
| `control.json` | 只由 `project_control.py` 的受检动作更新 |
| `decisions.jsonl` | 主帅追加决策、理由、证据与受影响结果；不写长篇内部推理 |
| `tasks/<task-id>.md` | 主帅写任务书，写明角色报告/代码写集 |
| `literature/`、`design/`、`audit/` | 角色的完整报告及证据索引 |
| `runs/<run-id>/` | 独立配置、运行记录、日志、指标、依赖、检查点和命令 |
| `handoff/<token-or-package-id>/` | 冻结交接清单、必要快照与工具创建回执 |

不覆盖原始运行或把日志全文塞进 state。不要自行改变用户 Git 提交或忽略策略。

## 可复制模板

- [当前项目状态](../assets/state.md)：当前事实、规格版本、G0–G9 闸门、待办、运行、上下文与证据。每个阶段更新快照，历史移入决策账本。
- [任务书](../assets/task.md)：给子代理的最小完整输入，角色只加载自身规范与任务证据。
- [授权记录](../assets/authorization.md)：准确保留用户直接指令的范围与来源，不能用助手自己写的“已授权”代替原指令。
- [运行记录](../assets/run-record.json)：在每次启动前生成具体记录，含配置、代码/环境版本、种子、命令、预算与指标位置。
- [正式实验冻结单](../assets/formal-design.md)：在 G6 锁定比较方法、参数、信息、种子、评估与统计。
- [交接清单](../assets/handoff-manifest.json)：原子接管的输入，详见下文。

决策账本每行一个 JSON 对象：`id`、`timestamp`、`stage`、`spec_version`、`decision`、`reason`、`evidence`（绝对路径/原文位置）、`affected_runs`、`revalidation`。否决方案也登记，避免轮换后重新尝试同一条无依据的路径。

## 决策账本接口

用 `scripts/record_decision.py` 追加与校验，不手工拼接 JSONL：

```powershell
& $researchPython -I -B "$researchSkill\scripts\record_decision.py" --project <论文项目绝对路径> `
  --stage G5 --spec-version <规格版本> --decision '<决定了什么>' --reason '<依据与已否决的选项>' `
  --evidence <产物绝对路径或原文位置> --revalidation '<需重跑或复核的内容>'
```

`--id`/`--timestamp` 省略时自动生成；`--evidence` 至少一条；`--affected-run` 可重复也可为空；`--check-only` 只校验既有账本（含重复 ID、非 JSON 行、缺字段），不写入。工具只追加 `decisions.jsonl`，不改 `state.md`、`control.json` 或既有行。`stage` 不是 G0–G9 时给出警告但仍然记录。

## 运行记录接口

`project_control.py claim-run --record` 至少要求 `run_id`、存在的绝对 `config_path`、非空 `command` 参数列表、现存绝对 `working_directory`、绝对 `log_path`。其余科学复现字段必须由执行任务书补齐，控制工具不替代实验闸门。

命令用参数列表保存，不用一段拼接 shell 字符串。控制工具登记配置与记录的 SHA-256；训练前核验哈希、预算、实际 owner 与记录一致。登记本身不启动训练。

`claimed` 表示已占用该 run ID，不证明进程已启动；`running` 必须有实际运行证据。`completed`/`failed` 是终态，不复活原 ID。断点恢复用新的 run ID 并记录 `resume_of`、检查点与已有预算消耗；这是同一训练链，不增加独立随机样本数。未知进程先调查，不能重启来“试试看”。

训练续跑除模型参数外，按算法实际需求保存优化器、随机数状态、回放缓存/轨迹、训练步数、环境状态与归一化统计。缺失时写“只能重训/部分恢复”，不得声称精确续跑。

## 正式实验冻结

完整记录每个方法的输入信息、动作接口、可行性处理、目标、调参和计算预算。次数由主帅根据预算与已有试验变异性确定并在 G6 锁定；单一种子不能支持稳定优势。若需要增跑，预先规定对所有相关方法一致的规则，不能看测试结果挑种子。

训练、验证和正式测试的场景/数据/种子明确分离；记录外生随机场景的生成方式。对各方法使用可比的场景，不用不同策略导致的随机调用次数变化假装实现了共同随机条件。报告原始运行及区间估计，标明独立样本单位，不能把同一策略的许多评估 episode 当成许多独立训练样本。

## 交接清单接口

将模板保存成项目交接包内的 JSON，填入：

- `schema_version=1`；真实绝对 `project_root`、旧主帅 `outgoing_session_id`、当前 `owner_epoch`。
- `state_file` 必须是该项目规范的 `.codex-run/drl-research/state.md`。
- `evidence_files` 为现存绝对文件路径及小写 SHA-256，至少包括 state 与授权记录，并包括接任必须复核的规格和验收证据。
- `authorization_evidence_file` 指向授权记录，接任者仍须核对其中指向的用户原始指令；哈希不能证明权限。
- `next_steps` 为非空具体动作；`open_tasks` 为每个代理的 ID、状态、报告路径与检查点；`active_runs` 为 control 中所有 `claimed/running/unknown` 的 run ID；`unknowns` 明确列出待核验事实。

冻结清单后不改其中引用的快照；需要修订时保留旧清单，另存新版，由旧 owner 用 `refresh-handoff` 登记原因、重置验包状态并通知已登记接任者，不在验包过程中悄悄换证据。运行中的日志/指标/检查点会变化，记录其位置与运行状态并制作所需证据快照，不把持续追加日志本体作为固定哈希证据。control 也会变化，不把 control.json 本体加入冻结哈希清单。

文件哈希可用 `Get-FileHash -Algorithm SHA256` 或标准库 `hashlib.sha256` 计算。核对实际存在、内容足够复核，再进入交接。引用缺失时列为未知并补齐所需事实，而不是伪造文件来满足格式。
