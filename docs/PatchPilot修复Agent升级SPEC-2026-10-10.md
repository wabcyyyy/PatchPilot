# PatchPilot 修复 Agent 升级 SPEC

初版日期：2026-10-10。执行与验证约定修订：2026-10-11。状态：分批实施中；卡状态、源码身份与测试收据以 [执行进度](work-notes/agent-upgrade-progress-2026-10-10.md) 为准。原审查基线：`d1bf0aae1a40c04308c8bb6d027a864fcf286838`，仅用于解释历史发现。

2026-10-11 文档复核时，进度已记录 B0–B2（G00–G08）的实施及离线收据，下一批为 B3（G09–G10）。B3 已于同日收口（G09–G10 完成，批次全量 1157 passed / 9 skipped 见进度文件）；下一批为 B4（G11–G12）。这不是所有契约均已收口的声明；跨卡差距见 [SPEC 复核记录](work-notes/agent-upgrade-spec-review-2026-10-11.md)，最终验收仍由 G16 收口。

## 1. 产品目标、范围与依据

目标：PatchPilot 专注仓库级代码修复，同时具备精简而完整的 coding agent 工作方式：理解仓库与规则、连续调查、维护证据与计划、修改代码、执行复现与检查、吸收用户纠偏、委派有限子任务、独立验收和恢复。

用户输入本地 Git 仓库与缺陷描述；已知失败/回归测试时直接进入修复；未知时先诊断并形成验收清单。输出为补丁、验证证据、费用/资源记录与明确结论。首版仍以 Python/pytest、本地单用户、CLI/API 为主。

本规格对齐优秀 coding agent 的能力类别，不承诺同等模型能力、修复率或产品规模。以下属于设计选择，而非已经验证的最佳参数。

| 能力 | 精简版目标 | 首版约束 |
|---|---|---|
| 连续 Agent 循环 | 调查、行动、验证可交错，阶段服务审计 | 保留现有 graph/plain 引擎，不推倒重写 |
| 上下文与证据 | 一个任务的连续会话、结构化工作记忆、可取回原始证据 | 不先做向量数据库或长期自动记忆 |
| 仓库规则 | 自动装配 AGENTS.md 等项目说明及路径作用域 | 文本规则不能改变宿主权限和验收规则 |
| 命令执行 | 复现、收集测试、静态检查、必要构建 | 容器内执行具名命令模板，不开放宿主任意 shell |
| 用户参与 | 主动澄清、等待输入、途中纠偏、取消 | CLI/API 足够，不先建设 IDE 或复杂前端 |
| subagent | 调查、补丁评审、候选修复 | 最多两名子执行者并发，不递归组队 |
| 可恢复性 | 会话、证据、工具调用、预算和候选一致恢复 | 不承诺外部副作用的 exactly-once |
| 可扩展性 | 内部工具/角色/命令 profile 接口，按需装配 | MVP 不要求完整 MCP/插件市场 |
| 可信评测 | 输入与控制变量冻结，保留所有重复和负结果 | 平台 resolved 不等于官方 SWE-bench 分数 |

明确不纳入 MVP：自动提交/推送/PR、任意依赖联网安装、自动部署、多租户、高并发集群、通用浏览器操作、多语言 LSP、自动修改既有验收测试、无限子 Agent、跨任务自动学习。

参考能力与对应设计：

- Claude Code 的循环允许调查、行动、验证交错；本文借鉴连续循环和途中纠偏。[官方工作原理](https://code.claude.com/docs/en/how-claude-code-works)
- 项目说明、会话上下文和跨会话记忆职责不同；本文先实现项目说明与任务内记忆。[官方项目记忆](https://code.claude.com/docs/en/memory)
- 子 Agent 拥有独立上下文、专用提示和工具权限；本文实现有限角色委派。[官方子 Agent](https://code.claude.com/docs/en/sub-agents)
- 动作权限需要由执行层裁决；本文采用冻结的命令 profile 和容器边界。[官方权限说明](https://code.claude.com/docs/en/permissions)

这些是接口与设计参考，不复制外部项目实现，也不宣称 PatchPilot 具有上述产品的全部能力。

## 2. 当前基线与历史兼容

实际执行目录：`D:/wcy/project/PatchPilot`。初始文档同步时 HEAD：`876ff6fedd0a2e04b04bb489e05e2073ab5ea485`，分支：`master`；这是历史身份。原审查基线来自另一工作树的 `d1bf0aae`，G00 已登记实际项目核验。新对话先复查 git status、HEAD 和分支，并保留已有改动；从当前进度继续，不重新执行 G00。

原审查基线具备（实际项目由 G00 复核）：受控工具、Python AST 检索、块补丁、失败/回归测试、范围门禁、资源账本、输入冻结、完整测试身份、候选 hash、恢复再验证、loop 快照、轨迹/报告、SQLite 服务、Docker 测试后端。

原审查基线的有限分支（实际接线由 G00 复核）：`app/graph/nodes.py` 的 Best-of-N，默认两个候选、每任务最多一次；CLI graph 入口接了 `branch_model_factory`，`app/api/service.py` 未接，故 API 任务不会触发它。任务线程池并发不等于子 Agent 协作。

原审查发现见 [2026-10-10 评审](work-notes/agent-readiness-review-2026-10-10.md)。以下是历史问题清单；是否已修及剩余限制按对应卡进度判断，不默认全部仍存在：

1. PLAN 提示要求不用工具，但通用循环要 finish 才退出；FakeLLM 特判掩盖真实协议问题。
2. 可见工具 schema 与阶段权限不一致；阶段间只传薄摘要，代码与失败证据丢失。
3. usage 来源、缓存费用、schema 开销、异常计量与在途并发预算没有完整闭环。
4. 输出在 JSON 序列化后按字符硬裁剪，信息与结构可能损坏。
5. 比较器把 verdict 等结果当控制变量，遗漏配置，且丢失重复运行。
6. 缺少受控复现命令、issue-only 诊断、项目规则、用户交互和通用角色委派。

旧 S00–S11 文档是历史实现来源，不能按其“待实现”字样重新做一遍；G00 核验收据与当前进度共同说明现状。旧评测、Q0 原始数据、失败现场和旧预登记保留。

产品策略和实验策略分开版本化：

- `legacy_v1`：保留旧配置与既有证据解释，旧任务恢复沿用自己的 TaskSpec/策略。
- `repair_v2`：本 SPEC 的新产品策略，显式记录运行策略版本。
- 新产品上线可以在离线验收完成后切换默认策略；这不构成真实修复率提升证据。
- 本 SPEC 不替换或重跑已经登记的在线批次。未来实验另建 manifest，不继续把旧 Q0 停手逻辑当产品开发禁令。
- 不混合 v1/v2 的报告口径；新报告有 schema_version，旧报告只读兼容。

## 3. 不可降低的验收与信任边界

### 3.1 严格成功条件

`resolved` 必须同时满足：冻结的失败测试在基线上确实失败、冻结的回归测试在基线上通过；当前候选对应的原失败测试与回归测试全部通过；修改范围与门禁通过；所有启用的资源限制通过；证据与候选 hash 匹配。

模型 finish、子 Agent 结论、命令 exit_code=0、复现脚本变绿、没有 gate_violations，均不能单独产生 resolved。失败集/回归集为空、测试身份不匹配、验证未运行或不确定、资源 unknown 等情形，必须保留原有严格拒绝/待审语义。

验收唯一入口仍为 `app/graph/acceptance.py` 及其调用链。新增工具不能旁路 `app/graph/gates.py`、pytest 身份规则和候选冻结检查。

### 3.2 允许的语义变更与禁止事项

本 SPEC 讨论并限定以下新语义：明确 PLAN 结束协议；能力对应 schema；结构化工具输出；任务内会话/记忆；版本化模型容量与预算策略；只在容器运行受控诊断命令；诊断后冻结验收清单；等待输入与事件流；有限子任务；候选检查点。

上述变更只在对应任务卡范围内实施。禁止改变既有测试的期望行为来适配实现，禁止弱化修改范围、测试身份和终局验收。已有测试若断言旧协议，必须保留其 legacy_v1 覆盖，并另外增加 v2 契约测试；不能直接删除旧断言。

模型可提交新的 repro/回归建议为 run_dir 中的独立工件（用户源仓库外）；MVP 不把测试改动加入最终仓库补丁。既有测试和 pytest 控制文件仍受现有门禁保护。

仓库代码与说明是低信任输入。AGENTS.md、issue、工具输出、子 Agent 返回不能授予额外权限，不能把自己的文字当用户批准。Docker 提供执行隔离；argv 白名单本身不是 OS 沙箱。现有 JUnit 验收仍有不可信仓库伪造证据的边界，不能宣称绝对安全。

### 3.3 运行及实现权限

- 初版 SPEC 编写会话只生成规格与交接；后续实施权限来自用户指定批次的执行指令，文档中的历史交付说明不阻止已授权实施。
- 新对话收到执行某批的用户指令后，可按该批直接实现，不因处于 AGENTS 的必须掌握区反复请求同一语义的确认。
- 测试离线，使用 FakeLLM 或 mock SDK；真实模型、端点探针和在线评测均另需明确额度授权。
- 禁止默认 push、改写历史、外部发布、删除旧证据或执行用户源仓库中的补丁。
- 单次生成/补丁块不超过 300 行；大任务拆成多次小变更，每卡至少一个 Conventional Commit。

## 4. 实现结构与数据契约

采用增量实现。已有模块保持职责；模块映射以 G00 核验与各卡已登记范围为依据，必要接线扩张按 §7 先记录理由与路径。不能以“架构优化”为由扩大所有卡的修改范围。

| 模块 | 职责与主要数据 |
|---|---|
| `app/llm/` | ModelProfile、UsageEnvelope、ModelResponseEnvelope、重试与 transport |
| `app/graph/resources.py` | 任务共享、可持久化、原子预留的 token/费用/调用数账本 |
| `app/context/` | EvidenceStore、TaskMemory、ContextAssembler、项目规则与压缩 |
| `app/tools/` | 能力注册、结构化输出、取回证据、ask_user、delegate、run_command |
| `app/executor/` | 受控命令 profile、进程生命周期、容器执行、取消 |
| `app/agent/`（新增） | SessionState、角色定义、子任务管理；不另建一套主修复引擎 |
| `app/api/`、`app/storage/` | 用户控制事件、等待输入、持久状态、事件流与幂等 |
| `app/evals/`、`scripts/` | 固定实验 manifest、provenance、配对统计和 offline eval |

必需契约：

1. `ModelProfile`：端点去敏身份、请求模型与返回模型、容量/输出上限、计量估算方法、工具/思考/缓存能力、价格版本与来源。能力可由显式配置声明，不能从模型名字推断。
2. `UsageEnvelope`：原始 usage（去敏）、prompt/completion/total、cache_read/cache_write/uncached、可选 reasoning 明细、provider/estimated/unknown、价格版本、费用来源。缓存字段可能是 prompt 子集，不得重复相加。
3. `ModelResponseEnvelope`：即便 choices/工具参数解析失败，也能先交付 usage 与 finish_reason；transport 异常保留已知/未知消耗。
4. `EvidenceRef`：任务内 ID、来源/工具、路径/行范围、workspace_revision、内容 hash、字节/行数、截断标记、受控取回方式。ID 不能映射成任意宿主路径。
5. `TaskMemory`：目标/约束、验收清单引用、证据引用、假设及其支持/反例、已排除方向、计划版本、当前候选、验证状态和用户输入版本。事实与模型假设分开。
6. `SessionState`：会话 ID、协议版本、消息序列、工具调用状态、记忆版本、当前能力与阶段。graph/plain 使用同一会话基础设施。
7. `CommandProfile`：具名 profile、固定可执行及 argv 构造器、参数约束、cwd、输入工件、env 允许集、timeout、输出上限、网络和写入域、profile hash。
8. `ChildTask`：parent/child ID、角色、子任务、证据包、能力、基线/candidate hash、预算预留、状态、返回证据/建议、费用和终止原因。
9. `ControlEvent`：event_id、task_id、用户身份来源、类型、内容 hash、expected_revision、问题 ID、幂等键；模型不能伪造 trusted_user 类型。

持久化：任务仍以冻结 TaskSpec 为入口。证据和原始输出落 run_dir；结构化索引、会话/控制状态使用 SQLite 或现有原子文件快照，不引入新存储服务。新字段有版本、旧行可读、迁移可重复。所有外部 API/报告默认不暴露密钥、provider 思考原文、任意绝对路径或宿主环境。

CLI/API 共用 `app/agent/runtime.py` 的任务装配；交互 CLI 放在 `app/agent/cli.py`。它们调用既有 graph/plain 引擎，不创建第三种修复实现。单任务 RuntimePolicy 从冻结契约生成；运行中的环境变量或全局 Settings 变化不能悄悄改变已受理任务的窗口、权限、价格和预算。

## 5. 预算、窗口和阶段的明确设计

资源分四类，不相互替代：

- 单请求容量：完整 messages、有效 schema、协议开销、需要回传的 reasoning 字段，加本次输出预留与容量余量。
- 累计计算量：raw input/output/total tokens，缓存命中仍占上下文与累计 token。
- 资金：按端点支持的缓存/未缓存/输出类别和冻结价目计算；区分估算费用与结算账单。
- 运行限制：模型调用数、工具数、修复轮数、任务墙钟、命令 timeout、子任务数。

`budget_mode=tokens|cost|hybrid` 必须显式冻结。legacy_v1 保持现有 token 闸；repair_v2 不能静默取消旧闸，也不能盲目继承 200k 后把软窗口改大。运行 profile 必须明确所有有效上限；cost/hybrid 需要已知且有来源的价格及金额上限，缺失时受理拒绝，不自动联网查价或无限运行。

费用用 Decimal/最小货币单位，禁止浮点累计误差。请求前以未缓存输入价与完整输出预留作保守预留，收到 usage 后结算；未知在途消耗保留预留，不写零。现金预算不能保证 provider 绝不发生一次在途超支；真实 overrun 必须记录并停止后续动作。

共享账本原子检查“已用 + 全部在途预留 + 本次预留”，包括重试、摘要、主 Agent、所有子任务和落选候选。预算不能在换阶段/子任务/恢复时重授。子额度属于父账本，不独立扩张总额。

上下文策略：

- legacy_v1 的 16k/keep=6 不改写历史。
- repair_v2 配置目标软窗口 64k、最近回合目标 12；实际窗口不得超过明确配置的模型容量减输出预留与余量。这是工程起点，尚无效果最优性证据。
- 未知端点必须提供显式容量声明及来源，或使用明确标为 unverified 的保守配置；不能声称已探测支持 128k/1M。越过声明容量时在请求前拒绝。
- 32k/64k/128k 为后续可比较档位。`context_window_tokens=0` 仅关闭软压缩，仍必须满足 provider 硬容量。
- 先去重复/替换可取回的大输出，再整理 TaskMemory，再压缩旧回合；保护任务约束、规则、未完成 tool-call 配对、当前失败与候选、近期必要证据。
- 保护内容本身超容量时返回 context_capacity_exceeded；不得裁掉系统约束、生成坏 JSON 或反复压缩直至耗尽预算。

阶段策略：先修旧 PLAN 协议，再提供 repair_v2 连续会话；LOCALIZE/PLAN/PROPOSE/VERIFY 保留为事件与权限边界，不强制每轮重新开会话。v2 阶段份额是可记录的软警告/行动建议，任务总限额仍是硬闸；旧 0.6/0.15 硬份额属于 legacy_v1。不强制“进入补丁阶段后禁止再调查”。

## 6. 任务依赖、批次与退出条件

卡状态唯一来源为 `docs/work-notes/agent-upgrade-progress-2026-10-10.md`，本节定义批次和目标，不另维护一份 TODO 清单。每完成一张卡更新进度，记录 commit、改变的语义、测试、环境限制、未运行项与下一卡，不以勾选代替证据。已记录的离线通过不代表跨卡接线、Docker 或真实端点已经验证。

| 批次 | 卡 | 交付物与退出条件 |
|---|---|---|
| B0 首批 | G00–G01 | 当前基线收据；PLAN 与工具权限契约修复；全量离线检查通过 |
| B1 测量 | G02–G04 | 使用来源正确、共享原子预算、有效实验比较器 |
| B2 上下文 | G05–G08 | 输出可取回、证据跨阶段、容量 profile、仓库规则 |
| B3 修复接入 | G09–G10 | 容器命令能力；issue-only 到冻结验收清单 |
| B4 交互 | G11–G12 | 等待输入/纠偏；事件流、取消与恢复 |
| B5 子任务 | G13–G14 | 三角色委派；候选检查点与可审计局部进展 |
| B6 收口 | G16 | CLI/API 完整离线演示、迁移与兼容验收、证据索引 |
| 可选扩展 | G15 | 本地能力扩展和只读联网文档工具，单独启用 |
| 在线证据 | G17 | 经明确授权的端点探针和小批对照，不能默认启动 |

依赖：G01→G02→G03；G02→G04；G03+G04→G05→G06→G07→G08；G03+G05+G08→G09→G10；G06+G10→G11→G12；G03+G06+G08+G09+G12→G13→G14；G00–G14→G16；G16→G17。G15 不阻塞 MVP。

默认新对话只执行用户指定的一批；用户仅说“继续 SPEC/当前批次”时，从进度定位未完成卡或下一批，不默认回到 B0。附带 B0 提示仅供初始启动参考。批次中途可交接，登记定向结果与全量待运行状态；完成声明须满足批次退出条件。

开发验证频率按 `AGENTS.md` 的“开发验证节奏(2026-10-11 起生效)”执行：卡内定向验证后可提交，
批次退出时完成一次全量；失败修复后按影响补验并完成必要的最终全量。新开对话继续原批次，
不自动重跑基线、已完成卡或全量。各批次均须有最终适用输入上的全量通过收据才能声明完成。

## 7. 详细任务卡

每卡均须完成：实现、行为测试、直接受影响的回归测试、文档/进度更新、快速检查后提交；全量由批次退出统一执行，卡内登记“定向通过，批次全量待运行”。以下路径是允许修改范围；新增模块必须在对应目录下，越界前先更新当前卡的范围说明。

### G00 / P0：核验基线，建立执行收据

- 问题：旧 SPEC、当前实现、历史报告和环境不能直接互相替代。
- 范围：`docs/work-notes/agent-upgrade-progress-2026-10-10.md`、`docs/evidence/agent-upgrade/`、本文的模块映射；不改业务代码、旧证据、AGENTS.md。
- 核查：cwd/HEAD/branch/dirty 文件；当前 TaskSpec、验收、测试身份、恢复、分支 API 接线；记录审查报告对应行为是否仍存在。
- 当前 detached HEAD 时，新建 `codex/repair-agent-b0` 分支继续；已有合适工作分支则复用。不 reset、checkout 覆盖或重新创建工作树来丢掉未提交文档。
- 显式识别本轮三个既有未跟踪工件：评审 md、离线探针 py、探针 JSON；以及新 SPEC/交接文件。保留原内容，单独记录，不把它们当本批实现成果。
- 运行离线基线检查，记录 Python/依赖、命令、总数、失败分类与耗时。用可写的仓库外 basetemp。
- 产出：`baseline.json` 与进度文件；环境失败单列，不能通过 skip、放宽断言、删除测试消掉。
- 验收：收据可复核当前代码身份；列出已实现/待修/未验证，历史 54 个测试通过记录不能充作本批全量结果。

### G01 / P1：修复 PLAN 完成协议与可见工具权限

- 范围：`app/prompts.py`、`app/graph/nodes.py`、`app/graph/plain_loop.py`、`app/tools/registry.py`、`app/llm/fake.py`、`app/evals/experiment_policy.py`；`app/llm/openai_client.py` 仅允许空 tools 时省略请求字段的兼容接线，计量/重试留给 G02；`tests/test_plan_stage.py`、`test_plan_invariants.py`、`test_plain_loop.py`、`test_graph.py`、`test_llm.py`、新增 `test_tool_capabilities.py`；对应 ADR/进度。
- PLAN 首选单次文本完成契约：非空、非截断、无真实工具副作用的计划文本正常结束；普通合法输出只发一次请求。不能依赖 finish 伪工具才能完成。
- 明确处理空回复、length 截断、无效工具、finish 兼容：最多一次协议修正请求；修正失败产生结构化 plan_invalid/degraded，不循环烧空阶段份额。降级必须进入轨迹与报告。
- 仍兼容 legacy 回放中的文本+finish，但计划阶段不能执行读写/测试工具。计划完成不等于修复成功。
- `tool_schemas(allowed_tools=...)` 仅装配当前可用工具；真实执行再次核对同一能力集合，不能只隐藏 schema。one_shot 禁执行反馈、禁分支的约束保持。
- FakeLLM 保留旧 PLAN 兼容回放，增加显式严格脚本模式；新契约测试必须覆盖遵守 PLAN 提示的普通纯文本，不能由 marker 自动补 finish。
- 测试：正常计划=1 次调用；纯文本/兼容 finish 等效；空文本/截断/非法工具有界结束；LOCALIZE 无写工具；PROPOSE/one_shot schema 与执行权限一致；伪造隐藏工具不能执行。
- 退出：无“计划合规但 max_turns/phase_share 才退出”路径；旧回放及独立验收保持。

### G02 / P1：模型响应与计量协议，异常先计量

- 范围：`app/llm/`、`app/errors.py`、`app/graph/plain_loop.py`、`app/graph/resources.py`（接线，不在此改预算算法）、`app/tools/tracker.py`；`tests/test_llm.py`、`test_resources.py`、新增 `test_usage_envelope.py`。
- 引入第 4 节的 usage/响应 envelope，解析原始 usage 先于 choices、工具参数、finish_reason 的业务解释。
- 对兼容端点建立可测试字段适配：标准 cached_tokens、DeepSeek 风格命中/未命中，以及不支持缓存字段的响应。只解释已知字段，保留未知字段去敏副本。
- 缺 usage 为 estimated 或 unknown；总额与明细不一致记录异常，禁止默认 provider 或用零充当缺失。支持只返回 total 的合法响应。
- 请求估算计入有效 schema、消息协议、tool arguments 与需回传的 reasoning；保存估算方法/版本，未有 tokenizer 时承认估算，不能称硬账单保证。
- repair_v2 禁 SDK 隐式重试，由平台显式 retry，每个物理 attempt 独立 ID、用量和 unknown 状态；legacy 路径的 SDK 重试不可观测性如实记录，不能补造历史用量。
- 修正请求、thinking 协议和 finish_reason=length 均显式归类；截断补丁不能执行。提供 timeout/取消接入点，取消行为在 G12 完整实现。
- 测试：usage 缺失、仅 total、cache 子集、响应有 usage 但 choices 缺失、工具 JSON 损坏、输出截断、thinking schema、transport 断开/重试。解析失败仍能查询已知消耗。
- 退出：所有调用都有明确计量来源；异常没有吞掉已知费用；不输出密钥或思考原文到公开报告。

### G03 / P1：共享原子预留、容量与费用账本

- 范围：`app/graph/resources.py`、`app/graph/plain_loop.py`、`app/graph/nodes.py`、`loop_state.py`、`resume.py`、`runner.py`、`app/evals/driver.py`、`pricing.py`、`report.py`、`app/api/report.py`、`app/config.py`、`app/task_spec.py`；`tests/test_resources.py`、`test_f1_budget.py`、`test_time_budget.py`、新增 `test_budget_reservations.py`。
- 增量扩展现有 ResourceLedger，不另建互不一致的费用计数器。实现 reserve→settle/unknown 的幂等持久状态。
- 原子预留在单个临界区完成额度检查与登记；并行两次各能单独装下但合计超限时，只允许一次发出。
- 按第 5 节实现 budget_mode、调用数、费用与容量检查；所有 Settings 新键纳入 provenance 分类，不能留下零读者开关。
- 费用有 price_source/version/currency、estimate/provider_usage/settled_bill 的来源标签；provider usage 乘价表仍是估计账单，不冒充已结算金额。
- 请求失败或崩溃后未知消耗保留预留；恢复不能释放未知余量。收到实际回复即使已经取消也结算一次，不继续执行工具。
- 汇总主/子/落选候选/重试/模型摘要的消耗，记录 overrun。没有已知 price 的 cost/hybrid 在线任务受理拒绝。
- 不取消现有终局资源检查；within_budget/exhausted/exceeded/unknown 与验收保持一致。
- 测试：并发竞态、重复结算、缓存命中/未命中价、未知费用、恢复预留、最后 finish 超支、阶段切换、候选落选、输出预留不足、关闭软压缩仍受硬容量约束。
- 退出：主账本可由调用明细重算；任何并发/恢复路径不能重复领取预算。

### G04 / P1：修复 provenance、比较器与预登记入口

- 范围：`app/evals/provenance.py`、`app/task_spec.py`、`app/evals/metrics.py`（只扩展身份/统计，不改 resolved）、`scripts/compare_experiments.py`、`scripts/run_paid_batch.py`、实验辅助脚本；`tests/test_provenance_anchor.py`、`test_experiment_summary.py`、`test_driver.py`、新增 `test_experiment_manifest.py`；新实验模板。
- 把控制变量与结果字段分开：verdict/status/tokens/rounds/duration/费用是观测结果，允许不同；不能拿结果相同证明配置相同。
- 记录完整的模型可见限制、请求/工具协议、prompt/schema/策略 hash、价格/能力版本、去敏端点身份、容器 digest、任务/源码/测试清单身份。模型别名不能证明服务端版本未漂移。
- 去敏端点仅保留规范化 host/路径或 hash；删去 URL 用户信息、query token、Authorization 和密钥。Settings 键必须分类，文本裁剪/输出上限不是复现豁免。
- 以 `(task_identity, arm, repetition)` 配对，保留每次运行；重复 ID、缺对、输入漂移和未登记控制变量差异给出明确诊断。不得 latest_per_bug 丢掉重复。
- 比较实验 manifest 明确允许变量及其派生策略 hash，所有其他控制身份相同；协议升级前后比较必须把升级本身登记为变量，不能绕过源码差异。
- paid driver 显式读取指定 manifest，不继续隐式选择旧 Q0 判据。增加 dry-run 展示矩阵/额度/哈希，不发请求；旧文档和批次只读保留。
- 测试：仅 verdict 不同合法；仅窗口/工具上限/thinking 漂移拒绝；三次重复都保留；缺配对/重复 ID 拒绝；去敏无凭据；dry-run 零网络/零模型请求。
- 退出：可离线生成可信配对摘要；不写“修复率提升”，不改写原始 Q0 结果。

### G05 / P1：结构化输出、证据存储与分页取回

- 范围：`app/tools/output_filter.py`、`app/tools/base.py`、`app/tools/files.py`、`app/tools/search.py`、`app/tools/symbols.py`、`app/tools/execution.py`、`app/tools/registry.py`、`app/tools/tracker.py`、`app/context/evidence.py`（新增）；输出/检索测试及新增 `test_evidence_store.py`。
- 先针对源码行/匹配项/测试失败列表裁剪，再序列化；最终送入模型的消息保持合法结构，禁止任意截断 JSON。
- 原始输出保存为 EvidenceRef；模型收到有界 excerpt 和 total/returned/omitted、行范围、hash、next_cursor 等元数据。原始证据不因模型可见上限而丢失。
- 增加 `read_evidence(evidence_id, cursor, limit)`；限定任务内引用、单次字节上限和分页范围，不能读取别的任务或宿主任意文件。
- 引用区分原始输入快照和当前候选 workspace_revision；文件修改后旧引用标 stale，可取回旧证据但不能当当前内容使用。
- command/repro 的 stdout/stderr 同样走此协议；终端日志和测试 XML 仍独立保存。G05 建立证据协议，G09 接入执行层原始 stdout/stderr 落盘；只保存 tail 的路径不能声明全文可恢复，端到端完整性在 G09/G16 验收。
- 复用现有 ToolResult，不把每个工具各做一套截断器；嵌套大字段也要裁剪。
- 测试：超过 8k 的源码、Unicode/转义换行、长错误、嵌套列表、非法游标、跨任务引用、修改后 stale、原始与 excerpt hash、磁盘上限。
- 退出：窗口变大前，单条回执的信息损失已经可见、可解释、可恢复。

### G06 / P1：连续会话与结构化任务记忆

- 范围：`app/agent/session.py`（新增）、`app/context/task_memory.py`、`context/assembler.py`（新增）、`app/graph/plain_loop.py`、`nodes.py`、`state.py`、`loop_state.py`、`resume.py`、`runner.py`、`app/evals/driver.py`、`app/tools/base.py`、`app/prompts.py`；上下文/恢复测试及新增 `test_task_memory.py`。
- TaskMemory 采用确定性更新优先：工具证据自动建引用，模型以受校验的 update_memory 提交假设/计划；不得把推测写成已验证事实。
- LOCALIZE/PLAN/PROPOSE 共享 SessionState 与 TaskMemory；阶段切换不丢读行范围、反例、排除假设和失败证据。重复 system 提示按版本装配，不能每次叠加。
- 允许补丁阶段按需补查；改动后标失效的证据，验证失败更新假设，不机械要求禁止重读。
- run_plain_loop 与 graph 共用消息/工具结果配对规则；阶段结束、快照和恢复不能留下孤立 tool_call 或重复工具副作用。
- 快照绑定会话/记忆/证据/候选/账本版本与 hash；恢复 mismatch 待审，不从薄摘要默默重新启动。
- 模型摘要是可选、有预算的能力；确定性摘要为默认，所有压缩前原始记录保留。
- 测试：LOCALIZE 读到关键反例后，PLAN/PROPOSE 请求仍能看到引用与关键片段；修改后引用 stale；跨阶段及崩溃恢复证据一致；多工具消息配对；纯文本和工具回复顺序有效。
- 退出：用 FakeLLM 检查实际装配请求，证明传递了证据；不能只断言内部 memory 字段有值。

### G07 / P1：模型能力 profile 与 repair_v2 上下文策略

- 范围：`app/llm/profiles.py`、`app/agent/runtime.py`（新增）、`app/config.py`、`app/context/token_window.py`、`context/assembler.py`、`app/task_spec.py`、`app/graph/resources.py`、`runner.py`、`nodes.py`、`plain_loop.py`、`app/evals/driver.py`、`run_single.py`、`provenance.py`、`app/api/service.py`；`tests/test_token_window.py`、`test_context_wiring.py`、新增 `test_model_profiles.py`；运行 profile 示例。
- 实现第 5 节策略，分离模型硬容量、软压缩目标、累计预算和 completion 上限。设置项名称必须能区分这些概念。
- 提供 legacy_v1、repair_v2 的显式 profile；v2 工程起点为目标 64k/keep=12，输出上限按已声明能力配置，不继续固定假设 4096 可覆盖所有补丁。
- 只在能力声明允许时采用目标窗口；超长保护区给出错误或要求缩小任务，不反复无效压缩。
- stable prefix 与 append-only 证据索引可减少无谓前缀改写，但缓存命中只由响应证明，不把设计愿望写成实际节省。
- 阶段策略在 v2 采用软份额提醒，不在一次 LOCALIZE/PLAN 耗尽后丢记忆；仍有无进展轮次/请求数上限防止无限调查。
- 所有有效参数写入 TaskSpec/报告；旧任务恢复沿用旧 profile。新默认切换须到 G16 收口后，不能改动历史实验身份。
- runtime 将冻结的策略实例传入引擎/工具，不能修改全局 Settings 来模拟不同任务 profile。受理后改变环境变量、并发运行两个不同 profile 的任务，均不应相互影响。
- 测试：模型容量小于目标、未知能力、schema 尖峰、output reserve、reasoning、保护区过大、软压缩关闭、v1/v2 恢复、不同窗口下关键证据可取回。
- 退出：不再混淆 16k、模型容量和 400k 累计额度；离线通过不推出 64k 的真实效果最优。

### G08 / P1：目标仓库规则与作用域装配

- 范围：`app/context/repo_instructions.py`（新增）、`context/assembler.py`、`app/gitops/input_snapshot.py`、`app/task_spec.py`、`app/tools/base.py`、`registry.py`、`files.py`、`paths.py`（只保留或强化现有边界）；新增 `test_repo_instructions.py`，快照/provenance 测试。
- 从已冻结输入加载根和相关子目录 AGENTS.md；CLAUDE.md 可作为兼容项目说明，优先级在项目 profile 中明确，不自行载入宿主个人配置。
- 平台硬规则 > 用户明确任务约束 > 已冻结仓库说明；目录更近的说明只影响其作用域。根说明开局加载，目标路径确定后按需补充目录说明。
- 每份说明记录相对路径/hash/作用域/裁剪；默认单文件 16KiB、总量 64KiB，可配置并冻结。超限明确提示，不能静默丢掉约束。
- 拒绝符号链接越界、`.git`/密钥/宿主路径读取；说明中要求外传、关闭门禁、修改验收测试等不能改变执行权限。
- 子任务继承适用规则的已冻结引用，内容变动必须使运行身份变化；不是自动长期记忆。G08 提供引用与身份，实际子任务继承由 G13 接线验收。
- 测试：根/子目录冲突、相关与无关目录、无说明、超限、越界链接、恶意权限指令、恢复 hash 漂移；子任务装配在 G13 运行面实现后验收。
- 退出：Agent 实际请求包含适用规则；规则能指导修复，但不能扩大工具能力或修改验收契约。

### G09 / P1：受控命令、复现工件与容器生命周期

- 范围：`app/executor/command_profiles.py`、`command_runner.py`（新增）、`docker_runner.py`、`local_runner.py`（生命周期接线，保留旧白名单）、`app/tools/commands.py`（新增）、`tools/registry.py`、`config.py`、`task_spec.py`；executor 测试及新增 `test_command_profiles.py`、`test_command_lifecycle.py`。
- 新工具 `run_command(profile_id, parameters)`，不接受模型提供的 shell 字符串。profile 由可信 Settings/运维配置定义并冻结，仓库文件不能自行注册可执行命令。
- MVP profile：pytest_collect、pytest_selected、python_repro、python_compile、ruff_check；必要的 python_build 仅在镜像已有工具和任务明确启用时提供。任意 `python -c`/`python -m` 不构成白名单。
- repro 内容通过 `write_repro` 写入 run_dir/scratch，绑定 source hash；在候选镜像/临时诊断副本内运行，不写用户源仓库，不把它加入 patch diff。
- 新通用诊断命令仅允许 Docker；网络关闭、无宿主密钥/env 透传、无 docker socket、非 root、CPU/内存/PID/时间/输出额度，写入只在任务副本和 scratch。Docker 不可用则能力不可用，不能自动退回宿主执行。
- repair_v2 的诊断、基线和最终验证采用同一冻结执行环境；接入未信任仓库要求 docker。legacy 的 local 后端只保留为用户明确接受的宿主执行方式，不能把容器诊断结果与宿主验收混称同环境证据。
- 使用镜像预装依赖；安装/下载/拉取新镜像不是模型动作，缺依赖返回 environment_not_ready 和需要的镜像说明。禁止悄悄 pip install 或联网拉取。
- 参数采用 argv 构造与语义校验；拒绝越界路径、危险选项、插件/配置注入、可执行名替换；命令模板不仅检查前缀。冻结的源快照不得以可写 mount 暴露。
- 命令输出进入 EvidenceStore；执行层在截取模型可见 tail 前保存受磁盘额度约束的原始 stdout/stderr，并明确磁盘上限造成的缺失；验证超过旧 tail 上限的输出可分页取回。记录 container/process ID、profile/argv hash、exit/signal/timeout/cancel、资源与副作用。辅助命令不能替代平台最终 verify。
- timeout/取消能终止该任务的容器/进程树；禁全局 kill/docker prune。诊断复制区中的改动不进入候选，修改候选的命令 profile MVP 不开放。
- 测试：profile 参数、路径/配置注入、env 清理、网络标志、输出分页、timeout/cancel、缺 Docker、缺依赖、命令 exit=0 不触发 resolved、修改诊断副本不污染候选。
- 退出：离线 mock 覆盖策略；另有 Docker 集成脚本验证真实隔离/子进程终止。环境不可用时明确未验证，不能把 mock 测试叫容器验证。

### G10 / P1：issue-only 诊断、验收清单提议与冻结

- 范围：`app/task_spec.py`、`app/diagnosis/`（新增）、`app/agent/cli.py`（新增）、`app/agent/runtime.py`、`app/agent/control.py`（最小批准/等待契约，与 G11 共用）、`app/api/schemas.py`、`routes.py`、`service.py`、`app/storage/db.py`、`repository.py`、`app/graph/runner.py`、`nodes.py`、`app/graph/state.py`、`app/graph/resume.py`（验收等待与恢复接线）；新增 `test_diagnosis_intake.py`、`test_acceptance_manifest.py`，API/TaskSpec/基线测试。
- TaskSpec v2 增加 mode=`repair|diagnose_then_repair`。repair 保留已有非空失败/回归集要求；diagnose_then_repair 可仅 repo+issue，但只能先诊断。
- intake source/issue/权限/profile hash 冻结不变；之后追加 AcceptanceManifest，不覆盖原始 TaskSpec。运行身份绑定 intake hash、manifest hash 和 session policy hash。
- Agent 通过现有检索与 G09 命令提出失败测试、回归范围、复现依据、环境问题；平台在未修改的快照上重新校验精确测试身份与红/绿基线。
- 自动提议不等于可信验收清单。未知清单需要 trusted_user 确认；已有 trusted_fixture 的清单可直接使用。用户批准内容限于已有测试身份/范围与诊断信息，不能批准旁路既有门禁。
- `POST /api/tasks/{id}/acceptance` 提交 proposal_id/hash 与 expected_revision，批准后冻结 manifest。必须在任何生产补丁前完成；重复相同批准幂等，不同 hash 冲突。
- G10 先提供最小 awaiting_acceptance/WAITING_INPUT 状态与 CLI 确认入口；G11 复用同一等待/控制机制扩展任意澄清，不再建立第二套暂停系统。
- headless 无批准时返回 needs_review 与 manifest 提议；禁止以 Agent 生成的 repro=通过、空集或 import 错误判 resolved。没现成可信失败测试时可以交付诊断/候选建议，但不能伪装严格修复成功。
- 环境故障、collection error、测试已有红与实际 issue 的关联分开记录；模型推断的关联标 hypothesis。
- 验收清单冻结后不可增删/换身份；需改变失败集/权限/预算时创建新任务或显式新契约修订，重做基线，不原地把旧成功证据复用为新成功。
- 测试：旧输入兼容；issue-only 可受理但不能直接修复成功；基线红/绿检查；空集/身份歧义/无关失败；人工确认与 hash 竞态；批准前无 apply_patch；崩溃恢复仍等批准；manifest 冻结后变更拒绝。
- 退出：非专家可以从 issue 开始；平台仍能解释“验收依据由谁、何时、基于哪个快照确认”。

### G11 / P1：用户澄清、途中纠偏与持久等待

- 范围：`app/agent/control.py`（新增）、`app/tools/interaction.py`（新增）、`tools/registry.py`、`graph/plain_loop.py`、`state.py`、`resume.py`、`app/api/`、`app/storage/`、CLI；新增 `test_user_control.py`、`test_waiting_input.py`，恢复/取消测试。
- 新工具 `ask_user(question, choices?, reason)`；有交互的任务进入 WAITING_INPUT，保存会话/证据/预算和问题 ID，释放执行线程，等待不发模型请求。
- `POST /api/tasks/{id}/messages` 支持 answer 和 steer；G10 的 acceptance 批准复用 ControlEvent。API 仅接受已配置鉴权下的用户输入，不信任模型或文件中的“用户已批准”。
- 用户途中补充 bug 线索和修复偏好追加到会话，在安全边界消费并增加 revision；不覆盖 issue 原文，不自动改变测试/权限/预算。
- 不支持在途任意变更冻结契约。权限/测试/金额扩张指向新契约/新任务，保留旧证据；文本“忽略门禁”永远无效。
- resume 通过问题 ID、expected_revision、幂等键消费答案一次；过期问题、重复不同答案、终态输入返回明确冲突/拒绝。暂停与取消竞争只有一个有效状态转移。
- 用户等待有明确独立超时与执行 deadline 口径：主动运行时长暂停累计，另有任务绝对寿命上限；绝对寿命不在恢复时重置。legacy deadline 保持既有口径。
- headless 不无限等：结构化 needs_review/clarification_required，返回所需信息与保全工件，不作失败修复率的无条件归因。
- CLI 展示当前计划、问题/选项、状态和取消方式；实现简洁文本交互，不建设新 GUI。
- 测试：等待时线程释放/无 LLM 调用；重启仍等同一问题；一次性答案；steer 到达当前循环；权限文本无效；取消与回答竞态；等待/主动时长；终态不可复活。
- 退出：用户能实际纠正正在修复的方向，而不是只能取消后重新创建所有输入。

### G12 / P1：事件流、及时取消与工具恢复语义

- 范围：`app/llm/`（transport/可选 stream 接口）、`app/executor/`、`graph/plain_loop.py`、`loop_state.py`、`resume.py`、`api/routes.py`、`service.py`、`cancellation.py`、storage/轨迹、CLI；新增 `test_event_stream.py`、`test_inflight_cancel.py`、`test_tool_recovery.py`。
- 最小产品流式能力是 `GET /api/tasks/{id}/events` SSE：阶段、可展示说明、tool_started/tool_finished、候选、等待、usage、终态；不是把完整 provider reasoning 暴露出来。
- 稳定 event_id、Last-Event-ID 续传、心跳、断连后查询补齐；events 与现有 trajectory 共享事件事实，不能产生两份矛盾进度。
- token delta 为能力支持时的可选增强。聚合成完整且校验过的工具参数后才执行；半截 JSON/半截补丁禁止执行。最终 usage 与非 streaming 同一账本。
- 取消传播主模型请求、辅助命令、子任务和队列；确认取消后不开始新动作。晚到的回复只计量/保存，不能继续写补丁。
- 验收目标：空闲/等待状态取消 1 秒内收敛；受控命令在 cancel 请求后 5 秒内终止，超出则报告 cancellation_pending；LLM transport 有明确中断能力或 timeout 上界，不能虚报已中止 provider 计算。
- 工具调用持久 `tool_call_id` 与 planned/running/completed/unknown；read/search 可安全重放，apply_patch 依据候选 hash 判断是否已应用，命令 unknown 不盲目重跑。
- 恢复重验候选，不复用过去绿结果；不同副作用工具的恢复策略在 registry 元数据中声明。
- 测试：SSE 顺序/续传/重复事件、流断开、半工具参数、取消期间 late usage、取消与自然完成竞争、命令进程树、apply 后落盘前崩溃、unknown 工具待审。
- 退出：CLI/API 看到同一事实；取消/恢复成本与结果有可解释边界。

### G13 / P2：轻量角色委派与现有候选分支统一

- 范围：`app/agent/roles.py`、`subtasks.py`（新增）、`app/tools/delegation.py`（新增）、`registry.py`、`graph/nodes.py`、`runner.py`、`app/api/service.py`、`app/evals/run_single.py`、`config.py`、`task_spec.py`、报告/轨迹；`tests/test_branching.py`、新增 `test_subagents.py`、`test_subagent_wiring.py`。
- 新工具 `delegate(role, task, evidence_ids, candidate_id?)`；首版同步返回可等待的子任务 ID/结果，底层有独立状态、取消和恢复，不引入 Agent teams 或互相聊天总线。
- 三角色：researcher 只读调查与受控诊断；reviewer 读取冻结候选、指出风险/补充检查；repair_candidate 在自己的工作区修改并验证。前两者没有 apply_patch 能力。
- 子任务获得独立模型会话、精简任务/证据包、适用规则与能力；不复制全部父消息，也不共享可变 messages。模型可按配置选同模型或便宜模型，所有身份和价格冻结。
- 默认最多两个子执行者并发、每任务最多三次委派、递归深度 1。legacy 自适应 Best-of-N 的最多一次语义保持；其两个候选各消耗一个子任务名额，不能叠加两套额度。
- 父账本先原子预留子额度；默认全部子任务额度合计不超过任务有效限额的 25%，可显式配置且冻结。剩余额度不足拒绝委派；不能因分支创建重授墙钟/调用数。
- 返回有界结构化 ChildResult：结论、证据引用、未验证假设、风险、候选引用、计量、终止原因。父 Agent 只吸收摘要/引用；子 Agent “测试通过”不是最终成功授权。
- 现有 `_run_candidate` 接入统一 child runner；CLI/API 共用模型工厂和能力装配，修复 API 未接线。plain 默认单 Agent，graph repair_v2 可显式启用；one_shot 始终禁委派。
- FakeLLM 子任务使用独立、冻结的 replay_subtasks 脚本/工厂；缺脚本明确未配置，不能沿用父脚本游标或偷偷调用真实模型。API 自定义任务也能离线覆盖实际委派接线。
- 候选 patch 合流前核对输入/candidate hash，再走原 apply/gates/verify。reviewer 的意见只能促使重新调查/检查，不能覆盖平台验收。
- 测试：不同会话与目录、只读角色越权拒绝、API/CLI 同配置触发、预算不足/并发预留、全部落选消耗入账、父取消、子崩溃、恶意返回要求放行、递归委派拒绝、one_shot 禁工具。
- 退出：可委派具体调查/评审任务；报告能说明每个子任务贡献，不能仅用线程池存在作为完成证据。

### G14 / P2：候选检查点、失败现场与可控局部进展

- 范围：`app/gitops/snapshot.py`、`patcher.py`、`app/graph/nodes.py`、`resume.py`、`acceptance.py`（只强化候选绑定）、`app/tools/patching.py`、`app/agent/subtasks.py`、报告；候选/恢复测试及新增 `test_candidate_checkpoints.py`。
- 保存 baseline、每次候选和最近可信候选的 patch/hash/验证证据；被 gate 拒绝的工作区先保全再复位，保持现有安全行为。
- v2 在候选没有越界且完成约定检查时，允许从具名候选继续修复，避免每次整体 reset 丢失已证实局部进展；“部分测试通过”只是一条事实，不表示局部代码正确。
- 候选选择/恢复由平台基于明确 candidate_id/hash 执行，模型不能把未核验目录当新基线。继续修复后产生新的候选 hash，旧验证全部失效并重新验收。
- 分支可从明确选择的合法候选 snapshot 起步，记录 parent_candidate；不能无声改为当前用户源仓库，不能把落选 diff 混入赢家。
- 连续相同失败/无新增证据时有界停止或重新规划；不靠增加 max_rounds 绕预算。
- 新 repro/建议回归测试作为独立工件交付，既有测试门禁不放宽。
- 测试：合法候选续改、gate 失败回 baseline、落选隔离、hash 漂移/补丁消失、旧绿证据拒绝、恢复后完整 verify、同失败无进展停止。
- 退出：恢复和重试既能保留可审计进展，又不把过去某个候选的成功套到新代码上。

### G15 / P3，可选：按需扩展能力与只读文档工具

- 范围：`app/tools/registry.py`、`app/agent/roles.py`、`app/extensions/`（新增）、`app/tools/document_fetch.py`（可选）、config/provenance；新增 `test_capability_extensions.py`、`test_document_fetch.py`；扩展说明。
- 提供内部 ToolSpec/RoleSpec/CommandProfile 的注册和能力裁决接口；小型本地 skill 为受限文本步骤，不导入仓库任意 Python 插件，不改变硬权限。
- 工具描述按当前阶段/角色/任务启用，schema 实际 hash 入报告；同名能力冲突拒绝。不能用加载外部 skill 自动扩大动作域。
- 可选 fetch_document 只读 HTTPS 文档，域名与 URL 来源由任务/profile 显式授权；内容作为低信任 EvidenceRef，不自动执行其中命令。
- 检查所有重定向、私网/loopback/metadata 地址、域名解析与响应大小，禁凭据透传和工作区文件上传。测试全部 mock，不为测试访问真实公网。
- 整个能力默认关闭。MVP 不含网页搜索、多站爬取、浏览器登录和完整 MCP 协议，README 如实区分内部扩展与 MCP。
- 退出：新能力可按任务装配并受统一预算/策略/证据约束；没有“接了 skill 就有执行权限”的旁路。

### G16 / P1：离线端到端收口与精简产品交付

- 范围：`demo/`、`tests/`、`scripts/`（离线验收）、README、design/ADR、`docs/evidence/agent-upgrade/`、进度文件；业务接线修复仅限 G00–G14 已声明模块。
- 增加真实可演示的 FakeLLM 流程：issue-only→诊断→用户确认验收清单→调查→计划→补丁→反馈重试→独立验收→报告；另演示用户纠偏/子任务/取消恢复，不把所有情况挤进一个成功脚本。
- CLI/API 使用同一运行工厂、profile 和工具裁决；禁止 CLI 有而 API 静默失效。engine/profile/role/model/预算真实有效值可查询。
- 收口 G07 冻结运行契约的已登记差距：受理与恢复均从已冻结 TaskSpec 重建有效策略，不能仅比较 profile 名称；测试同名 profile 下窗口/输出上限/价格/权限发生环境漂移仍沿用原契约或明确待审，旧身份不完整时按兼容约定处理。
- 收口 G06/G08 的引擎差异：repair_v2 的 graph/plain 按 §4、G06/G08 接入共用会话与规则装配；未接线组合在受理时明确拒绝并登记未完成，不能静默忽略后声明交付。one_shot 原有禁反馈/禁委派约束保留，能力支持表与实际请求一致。
- 收口上下文各部分的共同容量约束：骨架、记忆、规则、输出与回合历史在已声明容量下整体可装配；用离线边界样本验证。最优字符配比与真实效果属于 G17，不以缺付费标定为由推迟容量正确性检查。
- 新产品入口显式使用 repair_v2；旧 API 契约/回放和旧持久任务继续 legacy_v1。通过请求契约版本或显式 profile 区分，不能按有无某环境变量猜测旧/新客户端。
- MVP CLI 的新任务默认 repair_v2；旧 demo 和旧回放可显式 legacy_v1。在线 cost/hybrid 仍必须配置价格和金额，无配置时应明确拒绝，不能静默变成无限预算。
- 离线集包含单文件、跨文件、长输出、错误假设、无补丁、身份错配、门禁拒绝、环境失败、预算停止、输入等待、取消、恢复、落选分支等场景。
- 迁移从旧 SQLite/loop/report fixture 出发；旧任务恢复不可自动使用新提示/新工具/新价格。缺完整旧身份时待审，不伪造迁移成功。
- `report.json` v2 记录验收清单、候选、验证、资源来源、模型能力、命令、子任务、输入事件和运行身份；Markdown 报告显示“已验证/未运行/不确定”各项。
- 产出 release-receipt.json、逐场景结果、测试日志、演示录屏/文字步骤（任选）、证据索引与已知限制；缺 Docker 集成证据则标 release_incomplete_for_container_execution。
- 全量 ruff/format/pytest 必须通过；不购买模型结果来弥补离线失败，不用 FakeLLM 成功数宣称真实修复率。
- 退出：在声明环境内完整的修复 Agent MVP 可复现；窗口、交互、subagent 是实际接线能力而非文档名词。

### G17 / 单独授权：能力探针和真实任务探索性验证

- 范围：新 probe/实验 manifest、离线 dry-run、端点适配的必要补充、`docs/evidence/agent-upgrade/online/`；不覆盖旧 Q0 或历史 runs。
- 未获具体用户授权前，只能准备脚本、mock、题单、价目来源和额度方案；本 SPEC 不授权任何真实调用。
- 授权应写清 endpoint/model、最大调用次数/美元、上下文探针档位、最大输入输出、任务题单、重复数与整批停止条件。探测大容量不是“极小成本”承诺。
- 先小探针：真实响应模型、usage/cache 字段、工具/finish/thinking/streaming 能力；确认声明与实测差异。只测某个输入档位成功，不推出硬上限。
- 主产品探索性批：固定题单/顺序/预算，记录全部运行，失败按环境/资源/协议/候选/验收分类；早停和缺对照不能静默剔除。
- 要归因窗口、subagent 或重试机制时另建单变量配对批；不能将 B0–B6 整体前后差异归因于其中一个机制。保留稳定两极题，不用单臂噪声地板筛掉它们。
- 主指标：严格 resolved、资源/环境失败比例、正常 PLAN 完成率、首次候选时间、请求/重复读取、上下文截断/取回、主/子费用、验证时间和取消延迟。
- 等 tokens、等美元、等时长是不同问题；manifest 选择一种主比较口径，并另列其他消耗。缓存未知时不能虚构等美元。
- 小样本报告限定题单/模型/profile/日期；平台指标与官方 harness 分开。没有优势照实写未证明，不持续付费直到获得正结果。
- 退出：新增真实证据有输入、协议、费用和分母来源；在线实验失败不倒改离线验收和登记参数。

## 8. 必须覆盖的验收矩阵

测试主要检验行为和边界，避免只镜像实现细节。FakeLLM 严格脚本/mock 必须看到最终实际请求和动作，不能仅检查中间字段。

本矩阵定义最终覆盖责任，不要求每卡重新执行全部场景。卡内根据修改点选择受影响用例并复用适用收据；
跨卡能力在实际接线的负责卡验收，G16 汇总已覆盖与缺项，批次全量按 AGENTS 执行。
T23 每卡检视是否影响原失败/回归身份与门禁，仅运行受影响的保护用例；批次全量保留完整保护集。

| ID | 场景 | 必须观察到的行为 | 负责卡 |
|---|---|---|---|
| T01 | 遵守提示返回普通计划文本 | 一次请求正常计划完成，非 phase_share 降级 | G01 |
| T02 | 计划截断/隐藏工具调用 | 有界修正/降级，零非法副作用 | G01 |
| T03 | 模型返回 usage 后响应结构损坏 | 已知用量先入账，报告解析失败 | G02 |
| T04 | 无 usage、只 total、缓存子集 | 来源和缺失如实表示，不重复累计 | G02–G03 |
| T05 | 两请求各自足够、合计不足 | 原子预留只允许一个发出 | G03 |
| T06 | finish 超支、unknown 后恢复 | 不得 resolved，不重授额度 | G03 |
| T07 | 结果不同、配置相同 | 比较器接受结果差异，保留各重复 | G04 |
| T08 | 结果相同、配置不同 | 未登记漂移拒绝 | G04 |
| T09 | 长源码/长 stdout 超可见上限 | 合法结构、明确截断、原文可分页取回 | G05（协议）/G09（执行输出） |
| T10 | 定位找到反例后进入计划/行动 | 实际请求仍有证据，假设状态可追踪 | G06 |
| T11 | 修改文件与恢复会话 | 旧引用标 stale，候选/预算/消息一致；同名 profile 参数漂移不能替换冻结契约 | G06–G07/G16（差距收口） |
| T12 | 模型容量小于配置窗口 | 请求前处理/拒绝，不宣称大窗口已支持 | G07 |
| T13 | 项目说明要求关闭门禁或读宿主文件 | 约束不变，越权拒绝 | G08 |
| T14 | 恶意 profile 参数、缺 Docker | 拒绝或能力不可用，无宿主 fallback | G09 |
| T15 | 只输入 issue、没可信测试 | 诊断/待批准，不能变绿即 resolved | G10 |
| T16 | 确认验收后修改测试集 | 拒绝原地变更，需新契约并重做基线 | G10 |
| T17 | ask_user 后进程重启/重复回答 | 保存问题、无模型消耗、仅消费一次 | G11 |
| T18 | 在途取消与 late response | 停新动作、晚到 usage 入账，SSE 状态一致 | G12 |
| T19 | reviewer 写文件/child 递归/超预算 | 能力、层级和父额度拒绝 | G13 |
| T20 | API/CLI 相同委派配置 | 都触发同类型子任务，成本包含落选者 | G13 |
| T21 | 合法候选续改/旧绿证据 | 保留可审计进展，新候选重新独立验证 | G14 |
| T22 | 老数据库/TaskSpec/回放 | 可读或明确待审，不默默换 v2 身份 | G16 |
| T23 | 原失败错配/回归缺失/测试文件补丁 | 既有拦截不下降 | 所有卡 |
| T24 | 公开报告与事件流 | 无 key、授权 header、思考原文/任意文件泄漏 | G02–G16 |

安全边界的 mock 验证和真实 Docker 集成结果分别记录。T23 使用已有攻击/验收样本作为保护，不降低既有门禁测试数量和断言。

## 9. 具体离线执行与交付规范

2026-10-11 文档复核时，实际项目 venv 为 Python 3.14.2：`D:/wcy/project/PatchPilot/.venv/Scripts/python.exe`，其中没有 pip 模块。cwd、源码、报告和测试数据都使用实际项目目录。新对话先复查路径/依赖；此本地读数不替代 CI/Docker 的 Python 3.11 兼容证据。

推荐 PowerShell 检查，命令逐条执行并检查退出码，不把失败后的后续输出当通过：

```powershell
Set-Location 'D:/wcy/project/PatchPilot'
git status --short
git rev-parse HEAD
git branch --show-current
$taskPython = 'D:/wcy/project/PatchPilot/.venv/Scripts/python.exe'
& $taskPython --version
& $taskPython -c "import importlib.metadata as m; print({name: m.version(name) for name in ('pytest', 'ruff', 'pydantic', 'langgraph')})"
```

本会话可写且位于仓库外的 basetemp 示例；新会话若权限不同，换成它实际可写的外部路径，记录落点。`D:/tmp/pt` 是 AGENTS 的常规落点，只有实际可写时才使用。

先完成每卡快速检查。改过被锚定的源码/文档时，先维护锚点并运行对应测试，避免在全量中才发现行号漂移。
以下命令逐条运行；失败即先修复该检查，不继续启动更大的验证范围。

```powershell
$taskBaseTemp = 'D:/tmp/pt-upgrade-b0'
& $taskPython -m ruff check .
& $taskPython -m ruff format --check .
& $taskPython -m pytest -q tests/test_docs_anchors.py --basetemp=$taskBaseTemp
```

B0 卡内定向测试示例；其他卡根据修改点与调用链选择相关集合，避免默认复制整个历史回归列表：

```powershell
& $taskPython -m pytest -q tests/test_plan_stage.py tests/test_plan_invariants.py tests/test_plain_loop.py tests/test_graph.py --basetemp=$taskBaseTemp
```

批次全部实现、文档与定向检查通过后，执行一次最终全量并保存日志及退出码：

```powershell
& $taskPython -m pytest -q --durations=30 --basetemp=$taskBaseTemp
```

全量期间保持被测文件不变，不另起 pytest。重复或扩大检查前说明代码变化、失败或未解决风险；
已通过且输入仍适用的检查复用收据。纯文档任务按 AGENTS 的文档例外执行。
本节只规定开发验证安排，不改变 §3、G10/G14 中产品内部的独立验收与候选重新验证要求。

每张卡的进度记录至少包括：

- 卡号、commit、开始/结束源码身份、修改文件、允许范围是否扩张。
- 最小行为证据：输入、实际请求/动作、预期、结果、工件路径/hash。
- ruff/format/锚点/目标测试的命令和结果；全量待运行时明确登记，批尾补入全量收据；产品失败、环境失败和未运行分开。
- 测试收据注明被测 HEAD/dirty 文件、解释器/依赖身份、basetemp、退出码、耗时、日志路径及通过/失败/skip；新对话先核对适用性，不重复启动同一验证。
- profile/协议/迁移变更，旧任务/回放兼容方式和剩余限制。
- 本卡模型费用/外部动作（离线应为零）、未授权在线工作、下一张卡。

每批结束交接只选择下一批，带当前 HEAD/dirty 文件/测试收据，不假定另一工作树已经有成果。新卡若在当前源码已实现，先核验契约与回归，避免重做。

## 10. 项目完成度的声明

- G00–G14 与 G16 通过：可以称“面向代码修复的精简 coding agent，具备连续上下文、受控执行、用户交互和有限子任务委派”，同时列出语言/执行/验收限制。
- 只有单元测试/mock：可以称协议/边界离线验证，不能称真实模型修复率验证或真实容器安全验证。
- G17 有合格证据：按固定模型/端点/任务集/额度描述结果；不泛化成达到 Claude Code 水平。
- G15 未做：如实说明联网文档/内部扩展未启用；不阻塞专注修复的 MVP。

初版编写会话仅交付本 SPEC 与 [新对话交接提示](work-notes/PatchPilot升级交接Prompt-2026-10-10.md)，当时没有实施 G 卡或运行新全量测试。后续实施及收据见执行进度；2026-10-11 文档复核未实施业务代码、未运行全量或付费调用，不能将文档修订当作实现差距已解决。
