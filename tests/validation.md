# 交付验收记录

日期：2026-10-12（Asia/Shanghai）。skill 版本：1.0.0。

> 版本 1.1.0 的核查补充在文末“修订 1.1.0”一节；上文 1.0.0 记录按其原始日期保留，不覆盖。

## 格式与发现

- skill-creator 的 `quick_validate.py` 校验通过；界面 YAML、模板 JSON、Python 语法及本地引用已核验。
- Codex app-server `skills/list` 返回 `drl-research-commander`，路径为本 skill 的 `SKILL.md`，`scope=user`、`enabled=true`，显示名与默认提示正确。此项证明新 app-server 能发现该 skill，不能证明所有已打开聊天已刷新其目录。
- 原 `context-gauge` 的入口、读取脚本和协作协议与实施前 SHA-256 一致；未改它的全局阈值或 hooks。

## 自动化测试

共 **91 项通过**：上下文策略 36 项、项目控制 55 项，无失败或跳过。全部使用临时项目、合成 JSONL 和惰性运行记录，不启动训练或创建聊天。

```powershell
& 'C:\Users\Administrator\.workbuddy-ai\binaries\python\versions\3.13.12\python.exe' -I -B -m unittest discover -s 'C:\Users\Administrator\.codex\skills\drl-research-commander\tests' -q
```

覆盖 80/90 临界与超限、显式绑定、结构化事件、新 turn/压缩/模型切换/缺失型号、窗口来源、读数不写入；以及所有权冻结与激活、文件漂移、缺失授权证据、验包修订、延迟确认竞态、确认哈希、运行去重、终态保护、并发领取和原子更新失败。

## 独立前向验证

全新上下文代理读取三个模拟项目的原始材料，未提供预期答案，未读取测试断言；只写验证报告。结果由主会话对照实际产物核验：

| 请求 | 实际行为 |
|---|---|
| 主帅评估正式实验就绪 | 阻止资源守恒未通过的实验；识别专家暖启动、额外奖励、测试选参、挑种子及不公平预算；保留纯容量可行性处理，给出具体修复任务 |
| 分析组合适配路线 | 接受有环境动机的 PPO 与循环编码器组合；区分摘录事实、推断与未知，提出可证伪假设，未声称首创或已有优势 |
| 仅凭文件接任 | 机械哈希检查通过后仍发现基线/运行证据缺口；保持派发冻结，未冒用模拟身份确认、未激活、未重复训练 |

原始验证报告：`C:/Users/Administrator/AppData/Local/Temp/drl-skill-forward-bz6e_o4o/report.md`。15 个模拟项目输入文件保持原字节；该临时位置不是运行时依赖。

## 独立交付审查

独立代理检查当时全部 20 个交付文件及共享读取器，复现两项问题：旧清单的延迟确认能确认新版、无效最新型号能继承历史归属。修复后由同一代理以三项隔离复现核验并关闭，相关情形已加入上述 91 项回归测试。验包确认现在必须携带实际审查的清单 SHA-256，激活再次匹配；无效最新型号使比例裁决保持未知。

## 验证范围

已验证文档行为、读取适配器、文件交接状态机、运行登记及宿主技能发现。未执行真实聊天创建/消息/跨聊天激活、真实子代理会话绑定或训练进程恢复；自动轮换依赖目标宿主的实际工具与具体项目的直接用户授权。没有开展真实论文实验，也不把模拟验收当作论文结果。

## 修订 1.1.0（同日）

改动范围：

- `scripts/context_policy.py`：阈值参数化（`--prepare-percent`/`--required-percent`/`--compaction-limit`，默认仍为 80/90/2 且输出逐字不变）；reader 接口显式校验（缺失或不再接受所用关键字即明确报错，不再静默漂移）。
- 新增 `scripts/record_decision.py`：`decisions.jsonl` 的追加与校验（`--check-only` 只校验不写入）。
- `tests/test_context_policy.py`：缺兄弟 `context-gauge` 时整类 skip 并给出原因，不再 fail；新增不依赖 reader 的阈值配置与接口兼容性测试。
- 新增 `tests/test_record_decision.py`。
- 文档：`references/context-handoff.md` 与 `templates.md` 去掉机器专属绝对路径并补新接口；README 补充依赖与正确的 discover 命令；新增 LICENSE（MIT）；description 补英文关键词。

实测（`python -I -B -m unittest discover -s tests -q`）：

| 环境 | 结果 |
|---|---|
| 仓库副本（无兄弟 `context-gauge`） | Ran 112 tests，OK (skipped=36) |
| 安装位置（有兄弟 reader） | Ran 112 tests，OK（0 skipped，0 failed） |

CLI 冒烟：非法阈值（prepare ≥ required）返回退出码 2 且不写文件；自定义阈值原样出现在 `policy` 字段；决策账本端到端追加一条合法记录，`--check-only` 校验通过。

验证范围仍同 1.0.0：未执行真实聊天创建/跨聊天激活、真实子代理会话绑定或训练恢复；没有开展真实论文实验，不把本记录当作论文结果。

## 修订 1.2.0（同日）

改动范围：

- 新增 `references/pitfalls.md`：常见陷阱与不公平比较清单（信息泄漏、可行性处理越界、不公平比较、环境迎合，以及 Lyapunov/虚拟队列、储能与碳强度、评价统计、可复现），并在 `SKILL.md` 与 `experiment-protocol.md` 中接入（G2/G6 冻结前、G8 派发前使用）。
- 新增 `assets/paper-claims.md`：G9 论断—证据对应表、图表追溯、限制与审查处理记录，并在 `templates.md` 的可复制模板中登记。
- `scripts/context_policy.py`：读取器接缝重构——模块级 `open` 的替换改为 `try/finally` 显式恢复；新增"注入未被使用"检测，读取器不再经由模块级 `open` 取数时判为 `reader_mismatch` 并标未知，不再可能采信原始文件的数字。
- 新增 `tests/test_skill_assets.py`：技能文件完整性（相对链接可解析、JSON 模板可解析、frontmatter 有 name 与语义化版本、文档引用的脚本存在、外部依赖脚本未被内置）。
- 新增 `tests/run_suite.py`：带空跑守卫的测试入口（收集数与实际执行数下限），Discovery 失败时给出明确失败而非堆栈。
- 新增 `.github/workflows/tests.yml`：`windows-latest` 上 Python 3.11 与 3.13 各跑一次 `tests/run_suite.py`。

实测：

| 环境 | 结果 |
|---|---|
| 仓库副本，Python 3.13 | `run_suite.py`：collected=125 executed=89 skipped=36 failures=0 errors=0，退出码 0 |
| 仓库副本，Python 3.11.9 | 同上（125/89/36，0 失败） |
| 空目录（守卫自检） | collected=0 → 明确报"低于下限"，退出码 1（证明守卫非空转） |
| 安装位置（有兄弟 reader） | `run_suite.py`：collected=125 executed=125 skipped=0 failures=0 errors=0，退出码 0 |

读取器接缝由 4 项替身 reader 测试固定：结构化遥测驱动决策、调用后模块级 `open` 已恢复、忽略注入的 reader 不被采信（`reader_ignored_structured_telemetry`）、接口缺关键字即拒绝。

未执行项同 1.1.0。

CI 补记：`.github/workflows/tests.yml` 起初因令牌缺少 `workflow` 权限被 GitHub 拒绝入仓，改用具备 `repo, workflow` 权限的令牌后推送成功（commit `dba2b33`），并已在 GitHub 上真实跑完一次：

| 项 | 结果 |
|---|---|
| 触发方式 / run | `push`，run 38220542904，`conclusion: success` |
| `suite (3.11)` | Ran 125 tests，OK (skipped=36)，collected=125 executed=89 skipped=36 failures=0 errors=0 |
| `suite (3.13)` | 同上，逐字一致 |

即本机预测（89 executed / 36 skipped）与 GitHub 实跑结果完全吻合，空跑守卫在真实 CI 中未触发"绿但空跑"。
