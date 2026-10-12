# PatchPilot Agent 落地评审（2026-10-10）

结论：隔离工作区、严格验收、候选冻结与恢复再验证值得保留；Agent 控制循环、证据传递、资源计量和实验执行仍有实质缺陷。只把 16k 改成 128k，无法解决这批运行暴露的问题。优先级应是修协议与测量，再改善工作记忆，最后做新的窗口与预算实验。

## 1. 范围与证据身份

- 当前工作树：`C:/Users/25924/.codex/worktrees/0cbd/PatchPilot`；审查基线 HEAD：`d1bf0aae1a40c04308c8bb6d027a864fcf286838`。
- 原始 Q0：只读检查 `D:/wcy/project/PatchPilot/runs/q0-noise-2026-10-08` 的 21 份 report 与 trajectory；它们绑定历史 commit `92d751882062a88b2692c3293d704dfbf97d259c`。
- `git diff 92d7518 HEAD -- app/graph/plain_loop.py app/graph/nodes.py app/prompts.py app/llm app/context app/config.py` 为空：本文对应的核心行为在当前代码中仍存在。
- 本轮仅新增评审文档、离线探针和证据 JSON；业务代码、门禁、历史报告均未修改，没有真实模型请求。
- 探针：[audit_agent_review_20261010.py](audit_agent_review_20261010.py)；输出：[2026-10-10-agent-review-probes.json](2026-10-10-agent-review-probes.json)。SDK 请求由 Mock 完全替代。
- 当前验证：Python 3.14.2；`test_token_window/test_llm/test_resources/test_plan_invariants` 共 50 passed（59.46s），`test_experiment_summary` 4 passed（0.19s）；ruff check / format check 通过。未重跑全量 pytest、Docker 或在线对照。
- 历史可靠性缺陷不能重复列作现存问题：完整测试身份、统一终局验收、TaskSpec 持久化、候选绑定恢复等已在 S00–S11 落地，本轮重新检查其源码和证据索引。

## 2. 对前述解释的事实订正

| 项目 | 原始产物复核 | 能推出什么 |
|---|---|---|
| 成功与失败 | 8/21 resolved，13/21 failed；13 次均为预算停止 | 这是该模型、该配置、该自选题单的观测；不是官方 SWE-bench 分数 |
| 成功运行的模型请求 | 每次 28–33 个 `llm` 事件 | “成功题只打 6–8 个电话”把 PROPOSE 子阶段当成整题 |
| 全批输入输出 | 输入 6,976,531，输出 138,270，合计 7,114,801 tokens；输入占 98.06% | 累计重发占资源大头，不能直接推成实际账单比例 |
| PLAN | 132 次模型请求，624,599 tokens，占全批 8.78% | 20/21 次靠阶段份额耗尽降级；仅 1 次出现正常 finish |
| 历史压缩 | 8/21 次运行触发过压缩 | 另外 7 次失败运行完全没有触发压缩，不能把失败统一归因于丢历史 |
| 单次大小 | 轨迹最大单次 total usage 为 20,809 | 原始轨迹不记逐请求 prompt/cache 明细，不能把 total 当作精确上下文上限 |

16,000 是 `messages_tokens` 字符估算下的软压缩阈值，保护区可能使压缩后仍超阈值；不是 provider 的硬上下文上限，也不是每题固定可调用次数。400k 是累计 token 闸，不是美元预算。

“16k 是为教学题定标的”只得到部分支持：配置注释确有最近六轮与单条 read 回执的推导，但同一注释明确把引入动机写成真实多文件题调查耗尽预算。不能只凭项目起源推出这个参数的完整成因。

文件 `docs/evidence/2026-10-09-q0-noise-floor-gen2.txt` 的逐题表支持 8 成功/13 失败，但末段写成“阶段闸×9；resolved×9”，与原始报告冲突。当前原始报告应优先于这段二次汇总。

## 3. 具体发现

### R1 / P1：PLAN 的提示与循环退出协议冲突，真实批已反复烧空份额

证据：`app/prompts.py:83` 的 PLAN_PROMPT 要求“不要调用任何工具”；`app/graph/nodes.py:497` 用 run_plain_loop 执行 PLAN；`app/graph/plain_loop.py:399` 对纯文本继续循环，只有 `:420` 的 finish 调用才能正常退出。

离线探针：连续返回三条符合要求的纯文本计划，得到三次请求与 `agent loop exceeded max_turns=3`。真实 Q0 的 PLAN 每题 3–9 次调用，20 次轨迹均出现 `plan_degraded(reason=phase_share)`。

`app/llm/fake.py` 对 PLAN_MARKER 特判，自动返回文本加 finish，并且不消耗脚本步。该兼容处理让离线回放看起来一轮收敛，遮住了真实模型遵守“不要调用工具”后的失效路径。

建议：明确单次文本/结构化计划的完成契约，或明确要求 finish；增加遵守提示的纯文本响应测试。PLAN 的 schema 只暴露本阶段工具；不要把扩大阶段预算当成修复。

### R2 / P1：阶段切换丢失实质证据，机械拆阶段制造重复调查

证据：`app/graph/plain_loop.py:219` 每次构造新的 system/user 会话；`app/graph/nodes.py:382` 正常 LOCALIZE 只传 finish.summary；`:402` 的降级交接主要是最后文本与查询/路径次数；`:576` 的 PROPOSE 只收到 issue/findings/feedback/plan。

定位阶段读到的代码片段、反例、排除的假设、精确读行范围，没有作为统一证据对象进入下一阶段。PLAN 又不能核实内容；随后 PROPOSE 被提示“不要重新调查”。这是先丢证据，再要求模型直接行动的冲突。

建议：共享任务工作记忆，或交接带路径、行区间、内容哈希、关键代码/失败证据与假设状态的证据包。保留可审计阶段边界，但允许调查、行动、验证交错。本批仅能证明交接机制与重复调查的存在，不能量化其修复率损失。

### R3 / P1：资源口径没有分离；缓存缺失只是其中一项

证据：`app/llm/openai_client.py:78` 只取 total/prompt/completion；`app/evals/pricing.py` 只有输入/输出两档约价；`app/graph/resources.py` 按累计原始 tokens 判闸。

进一步的问题：

- `app/llm/base.py` 的 messages_tokens 不计工具 schema、reasoning_content 和消息协议开销；`plain_loop.py` 实际每轮发送完整十项 schema。窗口和请求前预算估算都不完整。
- usage 缺失时客户端做字符估算，但 AssistantTurn 没有传递 usage_source；plain_loop 入账默认标 provider。探针已复现“估算 289 tokens，被写成 provider”。
- 客户端在解析 `choices`、截断工具参数等错误时可能先抛异常，即便响应已经携带 usage，也没有先把它交给账本。SDK 内部重试也不是逐物理请求计量。
- llm_max_tokens 默认 4096；大补丁被截断时会直接终止。它与长上下文、计划份额是不同限制，需单独按任务和模型能力配置。

建议：分别记录模型容量（单次输入加输出预留）、累计计算资源、资金成本、墙钟/调用数。保留原始 usage 与 provider/estimated/unknown 来源，先入账再解释模型响应；缓存 read/write/miss 字段按端点适配。保守估算可做闸，不能写成实际账单。

如果使用 DeepSeek 官方当前 `deepseek-flash`，官方文档列 1M 上下文；峰时缓存命中输入 $0.006/M，未命中 $0.30/M，即 1/50，不是笼统的 1/10。兼容网关是否同价同能力、历史实际命中率多少，仍需对应端点响应和账单确认。[官方价目](https://api-docs.deepseek.com/quick_start/pricing/)

缓存命中只降低对应费用，不减少上下文占用，不保证命中，也不能恢复已丢掉的证据。修改旧前缀可能损害复用；不能由“输入占 98%”直接计算省钱倍数。[官方缓存机制](https://api-docs.deepseek.com/guides/kv_cache/)

### R4 / P1：旧 Q0 筛选逻辑错误，且已有 v2 与实际批驱动冲突

“稳定 0/3 或 3/3 的题不可判别”不成立：agent 稳定 3/3、control 稳定 0/3，恰可产生明确差异。仅观测一臂的 k=1，无法推出“模型方差淹没机制效应”，更不能据此判断 Q2 上下文或 Q3 计量实验没有价值。

13 次预算停止还让结果受到资源闸影响；不能把这些样本直接当成模型能力失败。稳定接近预算顶也不等于测到了低噪声的修复能力。

`docs/experiment-preregistration-v2-2026-10-08.md` 已明文 supersede 旧版、保留稳定两极题并禁止跳跑公共阶段失败的对照；它在 commit c9a07a8（2026-10-09 10:07）入库。但之后 Q0 及 `scripts/run_paid_batch.py` 仍引用旧版判据。

建议：旧批与旧判据保留为历史事实；撤回错误的统计解释。另行登记新的能力/窗口实验与授权，不改写历史分母，不以“遵守了旧登记”证明旧设计合理。产品参数更新与实验参数冻结应分别版本化。

### R5 / P1：实验比较器把结果当控制变量，同时漏检实际配置

证据：`scripts/compare_experiments.py:24` 先 latest_per_bug 丢掉重复；`:37` 将 verdict/status/tokens/rounds/duration 放进需相等字段；`:73` 对任何未允许差异报错，却没有展开 config_snapshot，也没比较 llm_thinking。

离线探针：同一题两臂仅 verdict 不同被拒绝；context_window_tokens 从 16000 改成 128000、结果相同时反而通过。这既阻止合法效应进入比较，又可能放过真实混杂。现有四项比较器测试仍全绿。

建议：只比较固定输入、代码/任务/环境/提示与工具协议身份及登记控制变量；结果字段进入统计。按 bug/arm/repetition 保存所有观察，不只取最新一次。

### R6 / P2：单条回执先被 8k 字符裁掉，扩大历史窗口救不回来

证据：`plain_loop.py:473` 先 json.dumps 工具结果，再 fold_output；`app/tools/output_filter.py:100` 默认 hard_cap=8000。JSON 中的源码换行是转义字符，头尾“行”折叠通常看不到源码的真实行，随后前缀硬裁还会截掉末尾元数据。

探针：12,035 字符的 read 类 JSON 被裁成 8,015 字符，JSON 不再完整，尾部 truncated 标志消失。模型实际得到的内容少于工具报告的读取范围。

建议：按工具类型在序列化前裁剪，保留路径、准确 returned range、truncated/next_offset；较大原始结果落到受控工件，并允许后续按区间读取。失去完整 JSON 本身不等于模型必然失败，但误报读取范围会误导下一步。

### R7 / P2：复现身份的白名单漏掉了模型行为参数

证据：`app/evals/provenance.py` 把 llm_max_tokens、llm_max_retries、llm_base_url、max_read_lines、max_search_results、refine_* 等放到 EXEMPT_KEYS。thinking 虽在顶层记录，比较器也没有检查。

输出上限、读行范围、工具回执裁剪会改变模型看到的内容；“只影响文本长度、不改复现口径”的注释是错误的。模型别名相同也不能证明服务端模型版本未变化。

建议：记录去敏后的端点身份、返回的模型版本、能力/计价版本、请求参数、提示/schema 哈希、所有可见层限制、镜像 digest 和任务输入哈希；未知服务端参数明确标 unknown。不能靠一项 model_name 宣称可复现。

## 4. 与 Claude Code 等落地 Agent 的差距

以下是源码与官方功能资料的能力比较，没有运行双方同题测试，不能推出修复率排名。

| 维度 | PatchPilot 当前能力与限制 | 对落地的影响 |
|---|---|---|
| 控制循环 | 固定 LOCALIZE→PLAN→PROPOSE；阶段间新会话；预算耗尽作为多种退出信号 | 缺少按任务进展交错调查/行动/验证的策略；P1 协议问题优先于加节点 |
| 上下文 | 确定性存根/丢组、Python 骨架、跨阶段短摘要 | 缺少保留关键发现/假设/验证结果的摘要契约与按需取回；64k/128k 只是候选档位 |
| 开发工具 | 九项工具，测试只能选预定义 failed/regression/all；Python AST | 无构建、lint/typecheck、依赖诊断、临时 repro、引用查找、服务/浏览器验收能力；需带权限扩展 |
| 用户输入 | 自定义任务要求先提供失败与回归测试；创建/查询/取消 API | 无 issue-only 诊断入口、运行中澄清/纠偏、暂停后继续授权的交互闭环 |
| 项目知识 | Repo map 与 issue；没有目标仓库指令分层装配 | 缺少项目构建约定、目录规则和可审查的持久任务知识；不是给系统提示塞更多字即可 |
| 长任务执行 | 多工具调用串行；取消在下一 turn 边界；同步模型调用 | 无后台进程会话、输出持续读取、及时取消在途请求；超时/取消的实际响应边界较粗 |
| 编辑与恢复 | 块锚定协议、候选 hash、checkpoint、恢复重验已具备 | 这是已有价值；多轮失败则整体 reset、只保留改动形状，缺少保留已证实局部进展的策略 |
| 验证 | 测试身份匹配、F2P/P2P、门禁和预算共同判 resolved | pytest 与有限测试集之外的行为正确性未覆盖；禁改既有验收测试合理，但还需独立新增 repro/回归能力 |
| 扩展与调度 | 固定注册表、局部分支 best-of-N、SQLite/Redis 单机任务服务 | 缺少按需工具/skill/plugin 协议、多语言执行 adapter；多 Agent 是后续能力，不是当前缺陷的前置解法 |
| 安全与部署 | local 默认；Docker 测试隔离；环境白名单；单机鉴权/根白名单 | local 不隔离文件/网络权限，仓库代码仍能以宿主身份执行；不可把 argv 白名单叫 OS 沙箱 |
| 评测 | FakeLLM 完整回放、自选外部题与 provenance | 协议覆盖和测量实现仍有盲区；官方 harness、固定样本同预算对照、失败归因需要独立补证 |

Claude Code 官方描述的循环允许调查、行动、验证交错；接近容量时会清旧工具输出并按需摘要，同时支持用户途中纠偏。因此可借鉴“连续任务上下文 + 可调整循环”，而非机械模仿更多阶段。[工作原理](https://code.claude.com/docs/en/how-claude-code-works)

Claude Code 提供持久项目指令；独立上下文的子 Agent 可承接会污染主会话的调查。二者解决不同问题：项目规则需要装配，跨阶段证据需要传递；增加子 Agent 也会增加请求成本。[项目记忆](https://code.claude.com/docs/en/memory)、[子 Agent](https://code.claude.com/docs/en/sub-agents)

Claude Code 有缓存用量明细与美元预算能力，可作为计量接口参考；这不保证具体网关的账单或缓存命中。[成本管理](https://code.claude.com/docs/en/costs)

安全比较须限定环境：Claude Code 的 shell 沙箱也有覆盖范围，原生 Windows 不启用该 OS 沙箱。PatchPilot 的 Docker 测试隔离与 Claude Code 的宿主命令沙箱职责不同，不能笼统写“对方绝对安全”。[沙箱文档](https://code.claude.com/docs/en/sandboxing)

## 5. 哪些做法合适，哪些应改变

合适且应保留：模型声明与平台验收分离、可信测试身份、输入冻结/任务契约、可审计候选、失败现场、恢复重新验证、FakeLLM 零费用回放、显式付费开关、公布负结果。LangGraph 本身不是错误，阶段边界可用于审计和恢复。

应改变：把阶段表当推理策略、让文本计划依赖工具 finish 才退出、用薄摘要替代共享证据、把原始 tokens 等同费用、把压缩节省当效果上界、用单臂稳定性筛选对照题、让比较器要求两臂结果一致、只要离线回放绿就认为模型协议已正确。

验证器还有自身边界：两次测试一致不防自适应恶意仓库，JUnit 在不可信仓库执行环境中产生。仓库 threat-model 已承认此点；应保持边界陈述，并在需要接入不可信仓库时改进判定证据与执行隔离，不以增加双跑次数代替信任边界。

## 6. 建议顺序与验收标准

1. **先修可离线证明的 P1**：PLAN 正常文本/finish/拒绝工具/截断响应都能明确结束；可见 schema 与阶段权限一致；来源准确入账；比较器允许结果不同并拒绝控制变量漂移、保留重复。新回归要模拟遵守提示的模型行为，不能只有 PLAN 特判 FakeLLM。
2. **再修证据传递与输出裁剪**：跨阶段保留可核对代码/失败证据/已排除假设；所有可见截断附精确元数据；新会话能按引用取回证据；不同局部策略不降低最终门禁。
3. **建端点能力与双账本**：单请求容量、模型输出上限、cache 字段、定价/版本、思考协议分开测量；资金与计算预算分别登记。在线探针须单独明确授权和费用上限。
4. **新版本再比较窗口**：将 16k/32k/64k/128k 作为能力允许的候选，而非先宣布 64k 是正确答案。keep 轮数独立配置；相同预算下大窗口可能更早耗尽累计 token 闸，应明确等美元/等 tokens/等时长的研究问题。
5. **最后补产品能力**：先做 issue-only 诊断和受控 repro、必要构建/静态检查、用户中途纠偏与适当取消；需要多语言/多实例时再扩 adapter、工具协议和服务调度。

新的实验应固定题单，保留稳定两极题，交错执行两臂并记录所有重复；另报资源/环境失败与模型候选错误。主指标至少含严格 resolved、首次补丁时间、正常 PLAN 完成率、重复读取、每题请求数/真实费用/延迟。小样本只能形成该证据集内的探索性判断。

本轮已完成评审与离线复现，没有实施上述语义修复，也没有新增在线性能主张。
