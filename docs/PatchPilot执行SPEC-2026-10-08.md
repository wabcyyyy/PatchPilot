# PatchPilot 验收、恢复、业务链路与简历证据执行 SPEC

日期：2026-10-08。状态：待实现。本文是下一对话的执行规格，不代表功能已完成。

## 1. 目标与执行边界

目标：先把“测试失败诊断与补丁验证平台”做成能演示、能复述、能提供代码和测试证据的简历项目，再补齐真实仓库接入与评测可信度。保留当前 LangGraph、受控工具、块补丁、隔离执行架构。

本轮完整交付包括：修复两个 P0 验收问题；统一预算及最终验收；修复重试轮次；持久化完整任务与源码身份；接通运行中进度；修复演示；版本化指标；准备可解释的单变量实验；形成三条有证据的简历 bullet。

实施价值顺序：S08 的直接演示协议修复可先交付，帮助面试展示；P0 与身份/资源闭环构成主要技术深度；S10 的实验工具是效果证据的准备，不是简历投递必须等待的付费任务。完整 API 与回归收口完成后，即可采用 S11 中有证据的 bullet，不等待 L01。

执行根目录：`C:/Users/25924/.codex/worktrees/a4d3/PatchPilot`。下文任务卡中的文件路径均相对该根目录；在其他 checkout 执行时必须先确认相同代码身份，不得直接修改原项目目录。

约束：

- 先读本仓库 `AGENTS.md`、本文、`TODO.md`、`PROGRESS.md` 与相关 ADR。
- 单次生成不超过 300 行；按任务卡范围修改；每卡至少一个 Conventional Commit。
- 每卡提交前执行 ruff、格式检查和完整离线 pytest；不可并发运行两个 pytest。
- 全部模型用 FakeLLM；`PATCHPILOT_LLM_ENABLED=false`。不调用付费模型、不联网跑题、不拉取容器镜像、不安装新基础设施。
- 保留历史 `runs/`、既有报告、Gold/参考补丁、测试期望；新产物使用新目录。
- 禁改被诊断仓库的测试、禁放宽路径/测试/修改范围/命令门禁。
- 本文明确规定的状态、恢复、预算和指标修正，作为执行时的规格；超出本文的语义改变须另行说明，不能顺手实施。
- 不新增 Redis 队列、微服务、向量库、前端框架或多 Agent；先把当前单进程平台做完整。
- 不把模型自行声明的 finish.success 当成测试验收结论；不自动应用补丁到用户源仓库。

## 2. 开工快照与证据边界

审查代码基线：`442cd8db3efe45deadbaf185ef18e42e3595a028`，本 spec 创建前 git 工作树干净、处于 detached HEAD。

已有能力：9 类受控工具；ripgrep 预筛与 Python 一致输出；AST 符号检索；仓库骨架；16k/keep=6 上下文压缩；LOCALIZE→PLAN→PROPOSE→APPLY→VERIFY；自适应分支；SQLite 检查点与循环快照；异步任务、在途幂等、取消、轨迹和报告。

这些是已有实现，不要在本轮重写成新框架。`TODO.md` 开头的现状表含旧描述，应以勾选任务与当前代码为准。

最新审查验证：ruff check 与 format --check 通过；收集到 710 个 pytest 用例；15 个相关测试文件实跑 217 passed，耗时 666.79s，另有一条 Starlette/AnyIO 弃用警告。**本次审查没有跑完 710 个用例**。旧的 687 passed/3 skipped 仅为历史结果。

可用本机 Python：`D:/wcy/project/PatchPilot/.venv/Scripts/python.exe`，审查时实际版本 3.14.2。目标依然 Python 3.11+；不要把仅在 3.14 跑绿写成覆盖所有支持版本。

历史真实任务仅作量级参考：基础五题 5/5 平台 resolved，来自四个仓库，合计 503690 tokens；pylint-6903 用 200212，当时预算 200000。复杂题 astropy-8707/xarray-3095 成功轨迹分别用 250175/226868；两道 Sphinx 题在 400000 额度下仍耗尽。运行版本不同，不能归因为当前机制，也不是官方 SWE-bench 分数。

审查材料根目录：`C:/Users/25924/.codex/visualizations/2026/10/08/01a11ba7-fb7d-7cc1-92f0-90713e4a13a5`。

| 材料 | SHA256 |
|---|---|
| `PatchPilot-review.md` | `A1C4B9C1F446A7F4636BF4C33F47D22F18ECC27BD96A92AE24AB7D586CA1A863` |
| `audit_probe.py` | `8936440825FD6FFD288DA4053119E4532D5E288B142BA066D49A42598CA6FAC6` |
| `audit-probe-artifacts/findings.json` | `AB5819C1AE6886696ABA11A82749D0F0525402796C99C277156F33EF58FD03C7` |

这些文件是阅读与复现参考。新的回归测试必须进入本仓库，不能只依赖外部探针。

## 3. 主流项目对标与本轮设计决定

| 官方依据 | 已核实做法 | 本项目采用的原则 |
|---|---|---|
| [SWE-agent 模型配置](https://swe-agent.com/latest/reference/model_config/) | 单任务/全局费用、调用次数、输入输出限制分别配置 | 分开累计额度、调用上限、输出限制与上下文软阈值 |
| [SWE-agent 模型实现](https://github.com/SWE-agent/SWE-agent/blob/main/sweagent/agent/models.py) | 更新实际费用后检查费用上限 | 实际消耗必须入账，最后一轮也须检查 |
| [mini-SWE-agent DefaultAgent](https://mini-swe-agent.com/latest/reference/agents/default/) | 下一次模型调用前检查费用，允许单次调用跨阈值；保存用量和退出状态 | 诚实记录超支，不能承诺估算器保证账单绝不超限 |
| [OpenHands 会话设置](https://docs.openhands.dev/api-reference/store-settings) | 独立提供 max_budget_per_task 与 max_iterations | 额度和迭代数是独立限制；不照搬网页示例值为默认值 |
| [SWE-agent 产物](https://swe-agent.com/latest/usage/trajectories/)与[Agent 实现](https://swe-agent.com/latest/reference/agent/) | 轨迹、退出状态、提交产物留档；异常退出可尝试保存已有提交 | 运行终止原因与补丁验证证据分别保存 |
| [SWE-bench Harness](https://www.swebench.com/SWE-bench/api/harness/) | 对实例补丁执行独立评测流程 | 生成、补丁应用与最终验收分层，测试策略由平台掌握 |
| [pytest JUnit 实现](https://docs.pytest.org/en/stable/_modules/_pytest/junitxml.html) | node id 转换为文件、classname、参数化 case name | 匹配完整测试身份；参数 ID 作为数据，不能靠方法名替代类身份 |

核查日期 2026-10-08；latest/main 会变化。只参考接口与设计，不复制外部实现。

### 3.1 本轮保留严格 resolved 契约

当前 README：原失败集全通过 ∧ 回归集全通过 ∧ 门禁全通过 ∧ 未超预算，才 resolved。本轮保留并补齐此规则，不新增“超预算也 resolved”的模式。

增加两个解释维度：

- `validation_status`: `not_run | passed | failed | inconclusive`，说明已有补丁的验证情况。
- `resource_status`: `within_budget | exhausted | exceeded | unknown`，说明任务资源情况。
- 旧 `status/verdict/outcome` 保留；预算超限仍 `BUDGET_EXCEEDED/failed`。即使验证通过，也不能静默写 resolved。
- `inconclusive` 用于双跑不一致、测试身份有歧义或证据不完整；`unknown` 用于有在途模型请求却无法恢复真实用量。不可用 0 代替未知。
- 没有验证过的任务不能因无 gate_violations 而展示“门禁已通过”。新增显式 `gate_status=not_run|passed|rejected|inconclusive`。

预算项调整为 P1：问题是终局与声明不一致；单次 API 回复跨阈值不是普遍的 P0。真正 P0 是错误测试身份被验成通过，以及恢复后补丁消失却仍成功。

### 3.2 预算数值与时间口径

- 默认累计 token 保持 200000；复杂任务可显式配置 400000，作为实验/运行档位，未认定是最优值。
- 上下文压缩软阈值保持 16000/keep=6；输出上限保持 4096；估算因子保持 1.0。不开付费实验时不改这些默认值。
- 0=无限的既有约定保留。任务总额、阶段份额和工具 turn 上限不得互相混用。
- 累计额度包含 LOCALIZE、PLAN、PROPOSE、失败调用中可知的消耗和全部分支候选，不只统计赢家。
- 阶段份额耗尽是阶段收敛信号；任务总额度耗尽是停止新模型调用信号。不能每进入一阶段重新领取全任务额度。
- `task_timeout_seconds` 在新契约中覆盖从执行线程真正开始到最终验收的全任务墙钟；排队不计，恢复停机间隔计入；不重授 900 秒。此项需 ADR 明确记录为口径修正。
- 每次执行前检查剩余时间，执行后复查；仍沿用合作式终止与执行器超时机制，不宣称精确到毫秒的强杀。
- 本轮不新增美元硬门禁。cost_usd 继续是带模型价目表版本的估计值，不能混同实际账单。

### 3.3 成功必须绑定当前候选和当前验证

验证证据至少绑定 `task_spec_hash/source_snapshot_hash/baseline_commit/candidate_hash/test_policy_hash/verification_attempt_id`。

`test_policy_hash` 覆盖两组请求测试、环境、双跑开关与验收规则版本。门禁证据另外带 `gate_policy_hash`。最终接受时核对工作区实际 diff 哈希与候选哈希一致，证据是当前执行尝试产生的。

恢复只恢复输入、候选和进度，不恢复成功结论。旧 checkpoint 中的 verify_* 布尔值不能直接作为新执行的验收证据。

## 4. 已确认问题与目标行为

| 编号 | 等级与证据 | 本轮目标 |
|---|---|---|
| F1 | P1；预算 20000、最后回复 usage=50000，plain/graph 均仍 resolved | 收到回复立即入账；终局共享验收；准确记录超支，不新增放行模式 |
| F2 | P0；真实 SqliteSaver next=finish，恢复 reset 后 diff 为空但 resolved | 冻结候选；恢复后重应用、重门禁、重测试；不可沿用旧旗标 |
| F3 | P0；要求 TestA::test_same，只有 TestB::test_same passed 也 all_passed | 完整类链与参数身份匹配；不确定时不可通过 |
| F4 | P1；failed=[a],regression=[b,c] 与 failed=[a,b],regression=[c] 同 ID；custom 不可重建 | 具名规范 TaskSpec、完整持久化、源码快照身份与执行参数幂等 |
| F5 | P1；max_rounds=2，第一次 gate 拒绝后第二轮被跳过 | 单处递增；成功前允许恰好 N 次候选尝试 |
| F6 | P1；空 touched 集被 strict localization 算成功 | 非空限定、指标版本和明确分母，不回写旧成绩 |
| F7 | P1；当前 demo 仍传 diff_text，实际 VERIFY_FAILED | 使用现行 patch_text/块协议；脚本与 API golden path |
| F8 | P1；不同引擎/PLAN/份额/复核导致对照混杂；按单臂翻转筛题 | 同引擎单变量、预先固定样本、全量报告失败分类 |

F1 是端到端 FakeLLM 复现；F2 是显式图恢复入口复现，未证明服务自动恢复在同窗口可达；F3/F4/F5/F6 为结构化/节点级复现；F7 为实际离线脚本运行。保留这些证据边界。

## 5. 执行顺序和里程碑

| 阶段 | 任务卡 | 完成后的可交付结果 |
|---|---|---|
| A：验收可信 | S00/S01/S02/S05a/S03/S04 | 两个 P0 关闭；预算、轮次、当前 diff 与最终判定一致 |
| B：业务可用 | S05b/S06–S08 | 完整任务可重建、输入不会漂移、进度可查、离线演示可重复 |
| C：证据可写 | S09–S11 | 指标有版本、实验有共同基线、三条 bullet 有明确支撑 |
| D：真实效果 | L01 | 仅在用户另行批准调用数/费用后执行，不属于默认交接范围 |

依赖：S00→S01→S02→S05a→S03→S04→S05b→S06→S07→S08→S09→S10→S11。S05a 的纯 TaskSpec/策略模型须先于 S03，避免为候选工件另造一份任务身份；S05b 完成持久化与服务接线。S08 的旧脚本协议修复可在 S00 后先做，但完整 API 演示须等 S05–S07。

每卡允许顺带更新 `PROGRESS.md`、追加 `TODO.md` 的 S 系列任务状态、更新本 spec 的完成索引；不得重写历史 M 系列证据。新增 ADR 编号先查现有目录，不能覆盖旧 ADR。

### S00 — 建立实施基线与契约 ADR

允许：`docs/` 新规格/ADR、`TODO.md`、`PROGRESS.md`；不改业务源码。

1. 核对当前 HEAD、dirty files、Python、依赖、710 用例基数；本 spec 自身是预期的新增文档，不等于业务已修改。
2. 新建 `codex/patchpilot-reliability-20261008` 分支；若已有同名分支，核对后继续，不覆盖。
3. 全量离线 pytest 跑基线；先记录环境失败与产品失败，不用 skip/改断言掩盖。
4. 建立独立新批次作为当前 HEAD 的 plain 35、graph 35 FakeLLM 基线。graph 批 CLI 目前不支持，逐题调用 `app.evals.run_single --engine graph`。
5. ADR 写定 3.1–3.3 的严格验收、阶段/任务预算、时间、恢复证据和指标版本契约。

验收：基线清单有命令、退出码、通过/跳过数、HEAD、配置、批次 manifest；测试失败先修环境，不宣称基线绿。首个 commit 仅文档。

### S01 — 完整测试身份匹配与合法 node id

允许：`app/adapters/pytest_adapter.py`、`app/evals/bugset.py`、`app/executor/whitelist.py`；新增 `app/adapters/test_identity.py`（需要共享解析器时）；对应 `tests/test_test_identity.py`、`tests/test_executor.py`、`tests/test_custom_task.py`、`tests/test_bugset.py`。

实现：

- 解析文件路径、完整 class chain、函数名和参数化 ID；只在参数部分保留合法数据字符；不把参数字符串内部的分隔符当文件/类边界。
- 明确支持范围：第一版验收要求具体函数/方法 node id。文件/目录/仅类 selector 若未展开为冻结的具体 node ids，预检明确拒绝，不以一个 testcase passed 证明整个 selector 完成；现有支持不得默默变更，若已有此类合法调用，单列兼容扩展及集成测试。
- JUnit 有 file 时也必须核对 class chain；无 file 时保留 rootdir 兼容，但不得以“同后缀方法名”匹配不同类。
- 同文件模块函数、类方法与嵌套类不能互相顶替；缺失/歧义均不通过。
- 保留 rc、超时、失败、错误、skip、实际执行覆盖检查。不要从 expected passed 数量猜测试已执行。
- 支持 `test_tuple[(1,2)]` 等合法 ID；命令始终是 argv 单元素、shell=False。路径绝对化/越界/选项注入仍拒绝；不整段取消白名单。
- 不改 failure_signature 的归一化语义；本卡只修测试身份与 ID 解析。

必测：TestA/TestB 同名、模块/类同名、nested class、参数化精确匹配、file 缺失、rootdir 改变、Win/POSIX 分隔、missing/skipped/deselected、重复/歧义身份；`-p`、`../`、盘符、UNC、NUL/换行攻击拒绝。

验收：单元复现 F3 由假通过变为拒绝；新增一个真实小 pytest 仓库证明同名类不串；API 接收 tuple 参数 ID 并真正执行指定用例。不能只测正则返回值。

### S02 — 资源账本与共享最终验收

建议分 S02a/S02b 两个原子 commit。S02a 做模型用量与终局；S02b 做持久时间与执行边界。每个子卡全量离线测试。

允许：新增 `app/graph/resources.py`、`app/graph/acceptance.py`、`app/graph/verification.py`；`app/graph/plain_loop.py`、`nodes.py`、`runner.py`、`state.py`、`loop_state.py`、`gates.py`；`app/tools/base.py`；`app/evals/driver.py`、`single_shot.py`、`provenance.py`；`app/llm/base.py`、`fake.py`、`openai_client.py`；`app/config.py`、`app/api/report.py`、`schemas.py`；新增资源/验收测试与现有 budget/plan/branch/resume/report 用例。

账本规格：

- 一个任务只持有一份线程安全 ResourceLedger；保存实际 total/prompt/completion、模型调用数、各阶段/分支用量、token limit、deadline、resource_status、停止原因。
- 每次模型调用有持久 call_id：请求前记 pending/reservation，收到回复后先记真实用量，再处理工具或 finish。重复同 call_id 入账无效；不按整份 messages 再收费。
- usage 来源标 `provider|estimated|unknown`；估算不冒充 provider 真值。异常/取消/finish/分支失败均保留可知消耗。
- 请求前保留现有估算检查，并为单次输出预留额度；不自动把校准 1.47 设默认。输出无限或无法可靠估算时记录能力边界，不能宣称绝不超账单。
- 回复导致任务超限：记录实际 overrun，停止本回复后续工具调用与新模型请求，结构化收尾。阶段额度跨阈值则按阶段降级/收敛，不伪装任务总超限。
- 崩溃留下 pending 调用且无实际 usage：标 unknown，保全现场、NEEDS_REVIEW；不把余量重授或已用写 0。本轮不实现 provider 账单对账。
- 账本采用追加 call 记录加原子快照；内部记录能去重并不代表外部 API 恰好调用一次。ledger/checkpoint 存储失败时按证据不完整收敛，不能继续收费再宣称资源受控。无须把每个测试输出也写成账本。
- 已有补丁可保留和取证；本轮不为超限额外赠送验证时间或模型额度。之前已完成的验证可以记录 passed，但严格 verdict 仍失败。
- deadline 在执行启动时建立并持久化；恢复使用原 deadline 的剩余时间，不依赖进程间 monotonic 值。没有可信旧 deadline 的旧快照降级 NEEDS_REVIEW。

验收规格：

- graph 和 plain 调同一套最终 verifier/acceptance helper；两路都遵守配置的 verify_double_run，不允许比较臂少跑复核。
- 最终判定检查当前非空 diff、完整测试身份、门禁、双跑一致性（开启时）、资源和取消状态；不只相信 state 中的两个 True。
- 验证各次 run_pytest 前后都有时间检查，尤其最后一次 rerun 之后；剩余时间不足不启动下一次执行。
- `report_schema_version=2`；新增第 3 节状态和证据字段；JSON 与 Markdown 同源，旧报告字段缺失显示 unknown/not_run。

必测：最后回复含 finish 且超额；纯文本超额；多工具回复超额时工具不执行；金额未知不写 0；各阶段共享额度；分支两候选都入账；异常/取消计数；最后 rerun 越时；恢复不重授 deadline；资源恰等于 limit 的边界；禁改测试/空 diff/取消不能 resolved。

验收：F1 两引擎均不能超预算 resolved；实际已用数字不因异常丢失；相同补丁/测试/策略两引擎的验收一致。既有 gates 只补调用位置，不降低拒绝规则。

### S03 — 候选工件与恢复再验证

允许：新增 `app/graph/candidate.py`；`app/graph/resume.py`、`runner.py`、`nodes.py`、`state.py`、`builder.py`、`loop_state.py`；必要的 `app/gitops/patcher.py` 内部重应用接口；`app/api/service.py` 的恢复判定；`tests/test_resume.py`、`test_resume_crash.py`、新增 `test_candidate_recovery.py`；ADR。

候选：PROPOSE 成功交给 APPLY 前，将实际 workspace diff 冻结为 `candidates/<candidate_id>/diff.patch + manifest.json`；tmp+replace 原子写，state 只保存引用与哈希。manifest 含任务/源码/基线/轮次/候选哈希、父候选与生成 call IDs。未完成冻结的半写文件不能被恢复方信任。

恢复规则：

| checkpoint next | 恢复行为 |
|---|---|
| localize/plan | 校验任务、快照和资源后继续；只读现场不 reset |
| propose | 保留既有循环快照续跑，写阶段先 reset；当前边界不完整时降级，不猜半补丁 |
| apply/verify/finish | 验证冻结候选与身份→reset→重应用候选→清旧 verify/gate 结论→从 APPLY 完整重验 |
| rollback | 先保全失败候选；按当前轮次继续回滚/重试，不能被改成成功路径 |
| END | 仅检查已有终态产物一致性，不冷启动同一任务再次收费 |

- 重应用使用现有合法路径/补丁规则；候选文件来自平台目录也不能跳门禁。
- missing/tampered candidate、baseline 不符、源码身份不符、用量 unknown、无余额：NEEDS_REVIEW 或明确预算终止，不伪成功；拒绝恢复前保全现场。
- `prepare_resume` 返回结构化 ResumeDecision（continue/revalidate/reject/already_terminal），runner 不再把恢复失败默认为冷启动整个任务。
- 服务的可恢复判定纳入合法的 checkpoint+candidate 边界，不能只检查 loop_state 文件存在；旧任务缺所需输入/资源证据时不自动放行。
- 重验占用剩余资源与图执行步数，不增加 max_rounds；不足则停止。不要硬把旧 5N+8 注释当运行证明；用真实 metadata.step 复测必要余量。

必测：本次 next=finish 复现；next=apply/verify；loop snapshot 正常删除但候选可恢复；候选缺失/篡改；reset/reapply 失败；旧 True 与空 diff；double-run 真重跑；END 不重跑；双恢复锁竞争；取消优先；至少增加 verify→finish 边界 `os._exit` 真进程死亡用例。

验收：恢复成功时磁盘补丁、candidate_hash、fresh verification_attempt_id 和最终报告相符；轨迹确有重应用、门禁、两组测试/复核。F2 关闭不能仅靠把 finish 移出 reset 集合。

### S04 — 统一轮次递增与拒绝后重试

允许：`app/graph/nodes.py`、`builder.py`、`state.py`；`tests/test_graph.py`、`test_branching.py`、新增 `test_round_transitions.py`。

- 定义 round_no=当前候选尝试，1..max_rounds；一次 gate 拒绝或一次验证失败各消耗一次候选尝试。
- 普通单线失败统一经 rollback 做保全、reset 和一次递增；apply 不预增再让 route 比较更新值。
- max_rounds=2：第一轮被 gate 拒绝→第二轮 PLAN/PROPOSE→允许验证成功；第二轮也失败才耗尽。
- 不改变阶段内 max_turns 的含义；不能把工具 turn 当补丁 round。
- 分支按既有候选尝试规则单独钉住；候选失败/合流/预算耗尽不得多增或漏增。分支用量仍归 S02 账本。

必测：gate 拒绝、真实 patch apply 失败、failed 集失败、regression 集失败、第一轮成功、最后一轮成功/失败、max_rounds=1/2/3、分支合流、取消。

验收：F5 精确复现由“只有第一轮”变为恰好两轮；已拒绝候选 reset 后不泄漏到下一轮。只补正确重试次数，不降低 gate。

### S05 — 规范 TaskSpec、完整持久化与在途幂等

建议 S05a 数据模型/迁移、S05b API/恢复接线两卡提交。

S05a 在 S02 后实施：完成规范化模型、hash 和策略捕获、SQLite 迁移、runner 生成完整任务契约，并用纯单元/存储测试钉住；暂不宣称 API custom 可恢复。S05b 在 S03/S04 后实施：API 幂等、完整持久化、从 TaskSpec 恢复与失败清理端到端接线。两卡不重复定义 hash 算法。

允许：新增 `app/task_spec.py`；`app/evals/bugset.py`、`provenance.py`；`app/api/service.py`、`schemas.py`、`routes.py`；`app/storage/db.py`、`repository.py`；`app/graph/runner.py`、`nodes.py`、`state.py`、`resume.py`；`tests/test_task_spec.py`、`test_storage.py`、`test_custom_task.py`、`test_service_robustness.py`、`test_resume.py`。

TaskSpec schema_version=1，至少包含：

| 分组 | 字段 |
|---|---|
| 输入 | source_kind、完整 issue_text、failed_tests、regression_tests、allowed_paths |
| 源码 | source locator、source_commit（可空）、source_snapshot_hash、快照相对引用与 manifest |
| 执行 | engine、arm、model provider/name、max_rounds/max_turns、有效预算/超时/上下文/计划/分支配置 |
| 环境 | 受信执行环境描述、镜像/解释器标识、验收/门禁规则版本 |
| 回放 | fake replay 的完整数据或不可变文件引用与哈希；真实模型为 null |
| 溯源 | task_spec_hash、schema_version、平台 git commit、提示词哈希、创建时间（时间不参与内容身份） |

- 用具名 JSON 对象、sort_keys 和固定序列化派生 SHA256；字段边界不得通过裸拼接表达。
- 测试列表保留实际执行顺序；若未来作为集合排序，执行器也必须同样排序，本轮不顺手改变测试顺序。
- issue 按原始 UTF-8 内容参与身份；完整保存，不截到 500。allowed_paths 空列表按现有 None 语义规范化，不趁机改 scope。
- 区分逻辑 bug_id、完整 task_spec_hash 与唯一 task_id。展示 ID 可截短，但幂等锁和校验必须使用完整哈希。
- 执行参数和 fake replay 不同不得 coalesce；同输入同源码同策略的在途重提返回旧任务，终态后重提可生成新 task_id。
- 不把 api_key、Bearer Token、含凭据的 URL、代理密码写入 JSON/哈希日志；模型和非敏感端点身份仍记录。
- TaskSpec 的有效策略在受理时冻结，恢复不能重新读当前 .env 后换模型/预算。代码可用不可变 policy 对象传递，不引入全局可变 Settings 替换。
- 现有列保留，SQLite 增量添加 task_spec_json/hash/schema_version；迁移可重复且事务化，旧行可查询、缺身份的旧 custom 行仍不能自动恢复。
- 同时原子写 `run_dir/task_spec.json`；DB 与文件 hash 不一致时不调模型。创建/写盘/入队中途失败必须释放既有锁并结构化收尾。

源码快照由 S06 完成前：custom 任务先绑定源目录内容指纹，执行前重算，不一致即 INVALID_TASK/source_changed，不静默执行新内容；不可声称已有不可变快照。S06 后使用冻结快照身份。

必测：F4 的两种分组得到不同哈希；同参数稳定；测试顺序/策略/replay/model/source 变化分别改哈希；issue>500 忠实重建；NULL 与规范化 scope；旧 DB 迁移；写盘/提交失败；custom fake 崩溃后脚本仍可恢复；重启后环境变更不重授额度。

验收：API 自定义任务能从持久化 TaskSpec 重建 BugTask，不能再从当前 bugs/ manifest 猜原输入；同键在途幂等和取消原子守卫仍绿。

### S06 — 冻结执行输入与环境预检

允许：新增 `app/gitops/input_snapshot.py`、`app/api/preflight.py`；`app/gitops/testing.py`（共享复制范围，不改已有边界）、`app/task_spec.py`、`app/api/service.py`、`app/evals/bugset.py`、`app/graph/nodes.py`、`app/config.py`；`tests/test_input_snapshot.py`、`test_preflight.py`、`test_custom_task.py`、`test_gitops.py`。

输入冻结：

1. repo root 白名单和包含关系校验通过后，复制到平台自己的临时 intake 目录；哈希**实际复制进去的文件字节**，不只 hash git status 或路径。
2. 复制范围与生产 materialize_repo 保持一致，复用同一规则；记录规则版本。需要排除新目录时单列变更与测试，不能擅自导致源文件缺失。
3. 普通文件内容、相对路径、删除/新增及链接处理决定 fingerprint；不随 mtime 改变；保留现有符号链接/包含关系拒绝规则，不跟随链接读工作区外文件。
4. 将冻结输入移入本 task 的 `source_snapshot/`，TaskSpec 完成持久化后才入队；worker 只从该引用物化 workspace，不再读取用户可变源目录。
5. 来源 git commit 只作辅助身份：dirty 文件、未跟踪且实际纳入执行的文件必须包含在内容指纹中。模板源无 .git 仍可按 source_kind=fixture 处理，不能把 source_commit 编造出来。
6. 重提相同快照可命中在途任务，临时副本按受控目录清理；崩溃残档保留标识供后续清理，不递归清理任意计算路径。
7. 独立冻结副本不写源仓库；保护 S03 所需输入，未完成终态持久化前不可被 recycle 删掉。

预检不执行模型、不安装依赖：验证有效解释器、pytest 可用、本地/已有 Docker 环境是否可用、两个测试集能合法定位且收集；依赖缺失/collect error 与“业务断言失败”分类不同。测试集真实 baseline 必须仍由引擎执行，预检不能替代 baseline。

冻结输入的复制与 collect 可以较慢，不能持数据库写锁或幂等锁执行整段 I/O。第一版按本地单用户服务实现，同步受理时如实记录 intake 耗时；其超时与体量保护走 Settings，不虚构高并发/超大仓库能力。环境 collect 放入受控执行流程，不能在 HTTP handler 中裸 subprocess 绕开执行器。任务执行 timeout 从 worker 起跑计算，intake 单列，不混进累计 LLM token。

- API 请求不能直接塞任意 shell、Docker host/bridge、安装脚本或镜像来扩大现有执行权限。第一版 custom 沿用 server 配置的受信环境。
- server preset 不支持时返回明确原因与修正输入提示；不为保证演示绿自动联网 pip install。
- 本轮不做通用环境构建器或跨语言适配。

必测：受理后修改源仓库仍执行冻结输入；dirty/untracked 变化改变 hash；两次冻结同字节同 hash；删除/二进制/CRLF/链接；源码与工作区互相包含拒绝；复制中失败、DB/文件不同步；源文件与 .git 在任务结束后完全不变。

验收：任务契约、源快照、候选与测试报告能连成一条身份链；preflight 失败不调用模型、不留下永远 RUNNING 的任务。

### S07 — 运行中进度与轨迹可查

允许：`app/tools/tracker.py`；`app/graph/runner.py`、`nodes.py`、`plain_loop.py`；`app/evals/driver.py`；`app/api/service.py`、`schemas.py`；`app/storage/db.py`、`repository.py`；`tests/test_trajectory_summary.py`、`test_api.py`、`test_storage.py`、新增 `test_live_progress.py`。

采取单进程最小方案：Tracker 追加 JSONL 后调用可选事件 sink，service 将事件幂等写入 SQLite。现有 offset/limit 接口即可轮询，本轮不引入 SSE、消息队列或 WebSocket。

- API 的生命周期 status 仍 QUEUED/RUNNING/既有终态；新增独立 stage、round、last_event_at、tokens_used 等进度字段，不能让 LOCALIZE 被误当生命周期终态。
- stage_started/stage_finished/llm_usage/verification/resume/rejected 等关键事件可查；事件 event_id 唯一，`(task_id,event_id)` 去重。
- 历史重复事件迁移先保存原记录再去重；不得为了建唯一索引静默删历史证据。
- `_persist_artifacts` 收尾补写相同事件不能重复；恢复后只增加新 event_id，消费游标稳定。
- JSONL 为原始取证来源；sink 写失败记录可观测错误，运行继续，但收尾必须补录或标 degraded，不谎称实时入库成功。
- 执行中轮询不持 Tracker 锁做 SQLite 操作；避免锁顺序倒置。有限输出、分页语义和日志 task_id/request_id 保持。
- 完成后任务终态不可被迟到进度事件复活；取消与自然完成竞争仍以 Repository 的终态原子守卫为准。

必测：用 Event barrier 暂停 FakeLLM，中途 GET task/trajectory 看得到 LOCALIZE；释放后能看到 VERIFY/终态；重复补录无重复；sink 故障补录；分页跨恢复；取消后迟到事件不改终态。不要用长 sleep 猜执行进度。

验收：用户提交任务后能知道现在在哪一步、有何失败原因；不再等到结束才第一次出现轨迹。

### S08 — 修复演示并交付 API golden path

允许：`demo/run_dirty_ticket.py`、新增 `demo/run_api_ticket.py`、`demo/README.md`、必要的 `demo/dirty-ticket/` 说明；`tests/test_demo_smoke.py`、`tests/test_api_golden_path.py`；不改演示仓库测试期望。

- 修复旧 diff_text 调用，按当前 apply_patch schema 使用 patch_text/块协议；锚点取实际源文件，不能借改 fixture 迎合锚点。
- 脚本支持 `--out` 独立目录；输出任务 ID、baseline 红、changed_files、门禁结果、两组测试绿、资源状态与报告/补丁路径；失败返回非零退出码。
- API demo 依次：归一化工单→POST custom fake→同键重提确认在途幂等→轮询进度与轨迹→读取报告→检查可取补丁；另外独立任务演示取消，不能把取消混到成功任务里。
- 离线测试使用真实 FastAPI TestClient、SQLite、复制工作区、FakeLLM 和真实 pytest；另外文档提供本地 uvicorn+HTTP 客户端命令用于人工演示。两者验证边界分开写。
- 如果生产 recycle 默认开启，演示检查冻结候选与 diff.patch 仍可读取；需要 workspace 检查的测试单独关闭回收，不更改生产默认来造假。
- 每次演示保持用户源目录内容与 git 状态不变；没有 API 的地方不要伪称工单系统集成已实现。

必测：直接启动当前脚本 subprocess；成功退出且 report resolved；旧字段不再出现；源目录 hash 不变；API 完整链路；在途重提同 task_id；取消终态；门禁拒绝演示。

验收：新环境按 README 可重复跑通，不能仅运行一个等价测试函数绕开 demo/main。

### S09 — 指标 v2 与历史口径保留

允许：`app/evals/metrics.py`、`report.py`；新增/必要的 CLI 版本参数；`tests/test_metrics.py`、`test_report.py`；指标 ADR 与证据说明。

- 输出 metrics_version=2；保留显式 v1 重算模式，历史文件不覆盖，新旧结果不能放在无版本的同一列。
- v2 `localized_strict = bool(touched) and touched <= expected`；无 reference 时保留“非空触碰”退化行为并标注参考缺失。
- 命名说明 strict 是“未触碰参考范围外文件”，不是找全根因。附加 expected-file coverage：有 expected 才计算 `|touched∩expected|/|expected|`，无 expected 为 null。
- 空补丁、未执行、gate 拒绝、模型/环境失败按真实字段分类；不能把成本停止等同模型能力不足。
- coverage 仅评测使用 reference；严禁把参考补丁正文或 expected 文件提示偷偷注入模型可见上下文。
- 新 summary 绑定 verifier/gate/metrics 版本与 TaskSpec/样本 hash；报告明确每个指标分子、分母、缺失项。

必测：空 touched、部分命中、范围内非空、范围外、无 reference、混合版本、空分母；v1 能复现旧值，v2 F6 为 False。

验收：新报表不会空补丁 strict 成功；旧证据按旧版本可追溯，不能用指标定义变化制造质量提升。

### S10 — 同引擎对照入口与实验登记 v2

建议 S10a 接线、S10b 预登记与离线统计两卡。

允许：`app/evals/single_shot.py`、`driver.py`、`run_single.py`、`provenance.py`；`app/graph/nodes.py`、`builder.py`、`state.py`、`runner.py`；新增实验策略模块与 `scripts/compare_experiments.py`；`tests/test_single_shot.py`、`test_ablation_invariants.py`、`test_experiment_summary.py`；新增 `docs/experiment-preregistration-v2-2026-10-08.md`。

- 优先在 graph 增加受控 one_shot 策略：复用同一 LOCALIZE/PLAN/上下文/预算/环境/最终 verifier；策略只禁止 PROPOSE 的 run_tests/reset_workspace，并在第一次候选验证失败后停止，不进入反馈重试。
- 这是登记为“执行反馈+跨轮重试”的机制组，不宣称能分别归因到其中一个因素。adaptive_branching 两臂均关闭，或另开单独实验，不能只给 agent 臂额外 best-of-N。
- one_shot 非“只发一次 API 请求”：仍允许检索与补丁协议自纠，区别按工具/状态约束定义。
- plain 旧入口继续可用；历史 graph-agent vs plain-one_shot 只作描述性资料，不当本轮公平对照。
- manifest 保存实际 engine/arm、effective_policy、工具白名单、prompt/template hash、输入/容器/Gold 身份和各版本，比较前检查允许差异字段；未登记差异直接报错。
- 因修 bug 必然改变的旧接受结果单列 known corrections；禁止修改 compare_batches 来忽略所有失败差异。

预登记必须修正旧方案：

1. 在看新运行结果之前固定题目列表与排除原因；稳定 0/3、3/3 的题保留，不按 agent 单臂的 res_i∈{1,2} 选题。
2. 稳定 agent=3/3、control=0/3 也能说明差异；“结果不翻转=方差淹没效果”不成立。
3. n=3 为小样本重复观察，不给任意 Δ≥2 定普遍增益标准；报告逐题两臂所有结果、配对差异与成本，写清限定样本。
4. 预设固定全题 ITT 分母；另报环境有效运行分母及失败分类。容器/网络/SDK/预算/验收失败不能无条件被叫作模型修复错误。
5. 两臂都要执行共同阶段，失败样本不因“猜测对照也会失败”而跳跑；只有整批资金/授权停止才暂停，未完成配对标 incomplete。
6. 修复/反馈机制、累计预算 200k/400k、上下文 16k/32k、估算因子是四类不同实验，一批只改一个登记变量。
7. 预算实验须记录分类：已有补丁未验证、定位无补丁、验证失败、资源耗尽、环境失败；不能只看总 resolved。
8. 上下文效果不能用静态历史回放等同真实行为效果；模型看见压缩内容后会改变动作，需另行授权的在线对照。

必测：两臂共有阶段模型消息一致、共同工具和份额一致、复核一致；对照不可调用 run_tests；验证失败只有 agent 重试；策略 manifest 能发现未登记差异；稳定两极题仍入样本；各种失败分母可重建。

验收：FakeLLM 接线证明实验条件可比；完成登记 v2 与统计脚本，**本卡不开真实模型**。旧预登记文件保留，新增 v2 标注 supersedes。

### S11 — 全量收口、证据索引与简历稿

允许：`README.md`、`docs/design.md`、新 `docs/release-evidence-2026-10-08.md`、`docs/resume-evidence-2026-10-08.md`、相关 ADR/TODO/PROGRESS；必要的 `tests/test_docs_anchors.py`。不再加产品功能。

- 全量离线测试、ruff/format 与 plain/graph 各 35 题回放；用 S00 的当前代码基线比较，另保留历史基线比较作为辅助。
- 文档逐项区分 implemented/offline verified/live verified/planned；禁止用“710 collected”替代“710 passed”。
- 证据索引绑定最终 HEAD、配置、命令、退出码、用例数、35 题逐题结果、10 攻击样例、demo、恢复边界、所有版本/哈希；列出实际未完成项。
- 明确平台自己的 resolved 与 SWE-bench 官方 harness 得分不同；本轮没有新真实修复率数据。
- 双跑只证明配置下的复核一致性；不宣称能防恶意仓库伪造 JUnit。local 子进程隔离不写成操作系统安全沙箱；Docker 验证范围沿用现有隔离实测。
- 不重写旧 ADR 行号锚点为虚假当前证据；被测锚点更新必须仍指向实际实现。

完成后可用的简历结构（实现前不可提前认领）：

介绍：PatchPilot，面向测试失败的代码诊断与补丁验证平台，通过受控工具、隔离执行与可追溯验收管理仓库级修复任务。

- 构建定位—规划—补丁—验证的仓库级 Agent 流程，结合 AST 检索、仓库骨架、上下文压缩与阶段预算管理代码调查和修复重试。
- 设计完整任务契约与候选哈希绑定的验收链，统一测试身份、质量门禁和资源判定；实现崩溃后候选重应用与再验证，防止检查点沿用陈旧成功结论。
- 基于 FastAPI/SQLite 实现异步任务、在途幂等、运行进度与审计产物；建设离线回归、攻击样例和同引擎消融入口，以测试报告和版本化评测证据支撑结果。

如果真实效果实验未执行，不补“修复率提升 XX%”。新增恢复回归次数、通过用例数等数字必须以最终产物现算，并说明不是在线业务成功率。

## 6. Windows 执行和验收命令

在本 spec 所属 checkout 运行；命令分别执行并检查退出码，任何失败不得继续 commit。

```powershell
$ppPython = 'D:/wcy/project/PatchPilot/.venv/Scripts/python.exe'
$env:PATCHPILOT_LLM_ENABLED = 'false'
git status --short
git rev-parse HEAD
& $ppPython --version
& $ppPython -m ruff check .
& $ppPython -m ruff format --check .
& $ppPython -m pytest -q --basetemp=D:/tmp/pt-patchpilot-spec-20261008
```

- basetemp 必须在仓库外；上述路径是专用测试临时根，不与其他 pytest 并发复用。先确认实际 checkout 不位于该临时根父链之内。
- `.venv` 不存在时先找可用项目解释器与已有依赖；不要直接用全局 Python 导致缺 langgraph/pytest，再把它认作产品失败。
- sandbox 拒绝写 D:/tmp 时使用工具正常的权限流程或另一个获准的仓库外临时根；不可改 conftest 防线，不能把 basetemp 移入本仓库。
- pytest 是每卡 commit 前的全量闸；开发时先跑对应文件，失败定位完再全量，不无理由重复整套。
- 所有模型相关测试必须 FakeLLM；不读取或打印密钥值。read .env 只核必要字段是否存在，输出脱敏。
- 本轮针对当前依赖执行；不顺手升级版本或修改锁文件。支持 3.11 的验证在有现成解释器时另跑，不联网安装。

S00 建议批次命令（首次生成目录须确认为新目录，不能覆盖已有产物）：

```powershell
& $ppPython -m app.evals.driver --bugs all --model fake --engine plain --out runs/spec-baseline-plain-20261008
$ppBugIds = Get-ChildItem -LiteralPath bugs -Directory |
    Where-Object Name -Match '^BUG-' |
    Sort-Object Name |
    Select-Object -ExpandProperty Name
foreach ($ppBugId in $ppBugIds) {
    & $ppPython -m app.evals.run_single --bug $ppBugId --model fake --engine graph --out runs/spec-baseline-graph-20261008
    if ($LASTEXITCODE -ne 0) { throw "graph baseline failed: $ppBugId" }
}
```

收口使用新的 `runs/spec-final-plain-20261008`、`runs/spec-final-graph-20261008`，运行对应命令；比较：

```powershell
& $ppPython scripts/compare_batches.py runs/spec-baseline-plain-20261008 runs/spec-final-plain-20261008 --tools
& $ppPython scripts/compare_batches.py runs/spec-baseline-graph-20261008 runs/spec-final-graph-20261008 --tools
```

脚本当前判定字段有 11 项，不能照抄旧文档“9 项”计数。正常 35 题判定应逐题保持；若预先定义的缺陷修复导致差异，原脚本继续报差异，另附逐题原因与新回归证据，不改脚本让它静默绿。

`app.evals.driver` 批 CLI 的退出码 0 只代表执行结束，不证明 35/35 resolved；须检查题集数量与逐题 report。执行基线时若与预期不符，先记录真实结果与原因，不能通过重跑挑选最好一批来制作基线。

S08 开发后约定入口：

```powershell
& $ppPython -m demo.run_dirty_ticket --out runs/spec-demo-direct-20261008
& $ppPython -m pytest -q tests/test_demo_smoke.py tests/test_api_golden_path.py --basetemp=D:/tmp/pt-patchpilot-demo-20261008
```

第一条是**待 S08 实现的 CLI**，当前脚本尚不支持 --out。本地 HTTP 演示的服务启动、端口、环境、认证与脚本参数必须由 S08 的 README 写出可用命令，不能在本 spec 中假称已经验证。

## 7. 总体验收矩阵

| 场景 | 必须成立 |
|---|---|
| 错类同名测试 | 要求 TestA、只跑 TestB 时不通过，报告缺失身份 |
| 最后回复超额度 | 真实用量保留，后续工具不执行，不能 resolved |
| 最后复核越时 | 保留已有测试证据，资源终态明确，不能 resolved |
| next=finish 恢复 | 当前候选重应用、门禁/测试真重跑；无候选则拒绝恢复 |
| 恢复到 END | 不冷启动重复任务、不重新调用模型、不覆盖原终态 |
| 两轮候选上限 | 第一次拒绝后还有第二次机会，第三次绝不执行 |
| 源码/测试契约变化 | task_spec_hash/idem key 改变，不复用原在途任务 |
| 源码受理后变化 | 执行冻结副本，源仓库不被写入 |
| custom 任务重启 | issue/测试/scope/replay/策略忠实重建；未知旧输入不猜 |
| 正在执行 | stage 与事件可轮询；终态不可被迟到事件复活 |
| 空补丁指标 | strict v2 为 False；v1 的历史值仍可重算 |
| 消融公平 | 共同阶段与验收同代码；manifest 只出现登记差异 |
| 面试演示 | 真实脚本与 API 链路可重复、失败非零、报告/补丁可读 |

最终 DoD：

- S00–S11 全部完成，每张卡有 commit、对应缺陷/行为回归测试、全量检查证据。
- 35 plain + 35 graph 离线回放及 10 攻击样例有逐题可核结果；不降既有门禁。
- 至少明确覆盖 PROPOSE 内部与 VERIFY→FINISH 真进程死亡边界；其他边界按实际测试声明，不能写“任意阶段无损恢复”。
- TaskSpec、冻结输入、候选、验证、资源与最终报告身份一致；旧输入/旧版本的降级行为有说明。
- demo 直接脚本、API TestClient、可选本地 HTTP 人工演示分别列验证结果，不能互相替代。
- 没有新付费运行时，交付仍完整：机制修复与离线证据完成，真实效果项明确未执行。
- 代码工作树无未知修改；运行产物不误提交；提交列表、最终 HEAD、已知边界可交接。

## 8. L01：单独授权后才做真实效果实验

S10 先完成 manifest 和离线冒烟。随后只提供具体运行申请，不自行开始调用模型。

建议第一批以当前已有环境可重放的四道复杂题为固定候选：astropy-8707、xarray-3095、sphinx-7590、sphinx-7748。正式批准前核对导入数据、基线、镜像与协议可表达性；若不合格，先固定替补与排除理由，再运行，不能按新成绩换题。

机制组实验：4 题×2 臂×3 次重复=24 次**任务运行**，不是 24 次模型调用。两臂 graph、同模型、同环境、同上下文参数、同资源额度，adaptive branching 同关。题目/臂执行顺序预先登记并交错，单题不并发。

复杂档可登记累计 400000 tokens、任务 1800s；24 次任务的名义额度合计 9.6M tokens。正式运行还需用户批准整批 token/费用停止阈值、模型/价格口径与最长运行范围；在途一次调用存在超支可能，不能把估算当精确账单保证。

本批只回答“执行反馈+跨轮重试”在这四题中的观察差异。预算 200k/400k、上下文 16k/32k 分别是后续独立批，不在同一对照里同时更改。

产物：逐题逐次状态、实际用量、退出原因、模型调用数、补丁/测试/资源证据、paired summary、未完成配对和环境失败清单。不足以支持提升时照实写未证明，不购买到正结果为止。

## 9. 新对话启动提示词

复制以下内容；spec 是唯一执行范围，历史审查材料是辅助证据：

```text
请接手 PatchPilot 的实现，按下面文件执行，不停留在建议或重新规划：
C:/Users/25924/.codex/worktrees/a4d3/PatchPilot/docs/PatchPilot执行SPEC-2026-10-08.md

先读 AGENTS.md、spec、TODO/PROGRESS 和相关 ADR，核对 checkout/HEAD/dirty files。
spec 的审查基线是 442cd8db3efe45deadbaf185ef18e42e3595a028；保留已有修改与历史 runs。
按 S00→S01→S02→S05a→S03→S04→S05b→S06→S07→S08→S09→S10→S11 执行。
我授权按 spec 明确列出的验收、预算、恢复、轮次、任务身份和指标语义修正实施，
但不得降低门禁、改被诊断仓库测试来放行，或扩展未列出的功能。
每卡先用 FakeLLM/实际小 pytest 仓库复现，再修复、跑 ruff/format/全量离线 pytest，
basetemp 必须在仓库外；每卡提交并更新进度。单次生成不超过 300 行，不并发跑 pytest。
优先交付可信验收、完整 API 业务链路和稳定演示，最后产出证据索引与三条简历 bullet。
不调用付费模型、不自动启动 L01、不下载镜像/装依赖；真实效果实验先给具体申请。
遇到当前代码漂移，复核事实后调整文件落点并记录；不要因例行实现选择反复问确认。
最终报告已完成卡、commit/HEAD、测试与回放结果、修复证据、未完成项和 claim 边界。
```

## 10. 执行完成索引（由接手对话填）

| 卡 | 状态 | commit | 验收证据 |
|---|---|---|---|
| S00 | done(2026-10-08;执行根=本 checkout,分支 codex/patchpilot-reliability-20261008,偏离声明见基线清单§0) | 见 git log 首个文档 commit | docs/spec-s00-baseline-2026-10-08.md:全量 pytest 717 passed+3 环境 skip(2 失败为新 ADR-0009 锚点格式所致,当卡修复);plain/graph 基线批均 35/35 resolved;ADR-0009 契约入库 |
| S01 | done(2026-10-08) | fix(adapters) commit | tests/test_test_identity.py 22 例 + executor 6 例真实仓库 + API tuple 全链路;F3 由假通过变拒绝;详见 PROGRESS S01 |
| S02a | done(2026-10-09) | feat(resources) commit | ResourceLedger+共享终局验收;F1 两引擎超额不再 resolved;tests/test_resources 13+test_acceptance 12+test_f1_budget 6;详见 PROGRESS S02a(S02b 待做) |
| S05a | done(2026-10-09) | feat(task-spec) commit | app/task_spec.py 规范契约+canonical SHA256+源指纹;tasks 表三列迁移;graph runner 受理冻结/重算拒绝;test_task_spec 16+storage 2;详见 PROGRESS S05a |
| S03 | done(2026-10-09) | feat(candidate) commit | ResumeDecision 四态分流+候选重应用完整重验;F2 关闭(含 verify→finish 边界 os._exit 真死亡用例);test_candidate_recovery 8 例;详见 PROGRESS S03 |
| S04 | done(2026-10-09) | fix(rounds) commit | apply 不再预增,拒绝/验证失败统一经 rollback 单点递增;max_rounds=2 首拒后第二轮必发生;test_round_transitions 8 例;详见 PROGRESS S04 |
| S05b | done(2026-10-09) | feat(contract-wiring) commit | 幂等键=task_spec_hash、契约双写、执行前一致性闸、custom 从契约重建;test_custom+3/service_robustness+2;详见 PROGRESS S05b |
| S06 | done(2026-10-09) | feat(intake) commit | input_snapshot 冻结+preflight 分类预检;custom 执行/恢复只读冻结副本;test_input_snapshot 8+test_preflight 6+e2e 1;详见 PROGRESS S06 |
| S07 | done(2026-10-09) | feat(live-progress) commit | Tracker sink→SQLite 实时事件+进度列(终态守卫),先存后删迁移+唯一索引,收尾补录降级可观测;test_live_progress 5 例;详见 PROGRESS S07 |
| S08 | pending | — | — |
| S09 | pending | — | — |
| S10a/S10b | pending | — | — |
| S11 | pending | — | — |
| L01 | not authorized | — | — |
