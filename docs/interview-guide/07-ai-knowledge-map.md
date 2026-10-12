# 07 · AI 应用开发知识地图（由本项目外扩）

> 分级：**P0 = 3 天内必须掌握**（不掌握就会在项目追问里崩）；**P1 = 建议掌握**（能加分）；**P2 = 以后补**。
> 每一项给：核心解释 + **本项目的落点**（有则指代码，无则标 `面试知识`/`生产化扩展`）。
> 用法：P0 条目必须做到"能解释 + 能指到自己项目的代码或明确说没做"。

---

# P0（26 条）

## A. 模型调用基础

**1. Token 与 Context Window**
token 是计费与容量的单位，不是字数；`context_window` 是**输入+输出**的总容量，很多失败源于只算输入。
本项目落点（`代码事实`）：**四个概念各有名字和读数**——单请求硬容量、软压缩目标、任务累计额度、单次输出上限（`docs/adr/0014`）；`token_estimate_factor=1.0` 只做本地估算与 provider 真值的换算；估算不出就记 `unknown` 且 `unknown` 一票否决 resolved。
`面试口径`：不要说"我的模型窗口是 128k"，要说"我不从模型名推断窗口，能力必须带来源声明，没来源落保守档"。

**2. Temperature / top_p / 采样参数**
影响确定性；评测时温度越高越需要重复次数；结构化输出场景常用 0。
`面试知识`：**本项目没有暴露 temperature 配置**——真实模型批次的可复现性因此受限（诚实答案）。生产化应把采样参数纳入 TaskSpec/manifest（现在纳入的是 profile、max_rounds、预算）。

**3. Structured Output**
三种做法：JSON mode、function calling、约束解码。陷阱：模型截断（`finish_reason=length`）时 JSON 不完整、参数是字符串不是对象、多余自由文本。
本项目落点（`代码事实`）：用 **native function calling**（`openai_client.py`）；参数非法 JSON → 包成 `{"_raw":...}` 并计 `tool_arguments_invalid`；**截断且仍有 pending tool_call → 硬失败 `ModelResponseError`，但带 usage 信封**——因为钱已经花了。
`面试知识`：JSON schema 校验 + repair retry 的通用套路；本项目 `MAX_PROTOCOL_REPAIRS=1`（`plain_loop.py`）。

**4. Function/Tool Calling vs MCP**
Function calling = 供应商协议里的"模型输出结构化调用意图"，执行由你的运行时做；MCP = 工具/资源/提示的**跨进程标准协议**（client-server，可插拔），解决的是生态复用而不是模型能力。
本项目落点：手写 `REGISTRY` 产 OpenAI function schema，**"可见 == 可执行"用同一份白名单**（`registry.py` + `plain_loop.py`）。
`面试知识`：如果要把 16 个工具做成 MCP server，需要处理的是生命周期、鉴权、以及"阶段白名单在 server 侧还是 client 侧"——我会坚持放在 client 侧，因为可见性是我安全模型的一部分。
`风险点`：别说"我用了 MCP"。没有。

**5. Prompt Engineering vs Context Engineering**
前者写指令，后者决定**每一次请求里真正出现什么**。AI 应用的效果差异大多来自后者。
本项目落点（`代码事实`，很好的差异化素材）：三阶段装配（骨架→大纲→观测）+ 按 `(stage, round_no)` 幂等缓存 + 优先级"平台规则 > 用户约束 > 仓库说明" + 工作记忆只传**平台观测**不传模型自述 + 观测带 staleness 与 `read_evidence` 回取指针 + 超限明写省略了多少行、保留哪些行号区间。

## B. Agent 机制

**6. ReAct**
Reason + Act 交替：思考 → 调工具 → 观察 → 再思考。它的失败模式是循环、观察误读、过早终止。
本项目落点：`plain_loop.py` 的回合循环就是 ReAct 的受控实现；`finish` 做成伪工具让"终止"成为一次显式动作（`registry.py`、`plain_loop.py`）。

**7. Agent vs Workflow**
判据：**下一步由模型决定还是由代码决定**。Workflow 可预期、可审计；Agent 灵活但难保证终止。
本项目落点：见 `02` M2 —— "Workflow 定阶段，Agent 定动作"，8 个 `route_*` 全硬编码。**这道题你必须答得干脆**，犹豫就会显得不理解自己的架构。

**8. Agent State**
状态 = 每一步读写的事实集合。设计要点：可序列化（才能 checkpoint）、无隐藏对象、字段有 owner。
本项目落点：`TypedDict total=False`（`state.py`），无 reducer → last-write-wins；不可序列化对象放闭包 `TaskNodes`。
`面试知识`：LangGraph 的 reducer/append 语义（`Annotated[list, operator.add]`）——本项目**没用**，因为阶段间是覆盖语义不是累加语义。这是被追问时的准确回答。

**9. Memory**
区分三种：会话内短期（消息历史）、任务内工作记忆（跨阶段观测）、跨任务长期（用户偏好/知识库）。
本项目落点：`TaskMemory` 是**任务内跨阶段工作记忆**（`task_memory.py`），且严格区分"平台观测"与"模型假设"（后者必须带引用，自称已验证被拒）；**没有跨任务记忆**。
`风险点`：不要把 `SessionState`（消息序列修复，`session.py`）说成 Memory。

**10. Termination**
Agent 必须有多层终止：最大步数、墙钟、预算、无进展检测、人工中止。
本项目落点（`代码事实`，这是你最该背熟的一条链）：`max_turns=20`/回合 → `task_timeout_seconds=900` + 绝对寿命 `86400s` → `token/cost/calls` 账本 → **无进展检测 = 失败签名 streak**（≥2 提示换思路、≥3 收敛 NEEDS_REVIEW）→ `recursion_limit=5N+8` 图级 → `TaskCancelled` 协作式（回合边界 + 命令轮询，>5s 才升级为 `cancellation_pending`）。

**11. Infinite Loop / Repeated Tool Call**
根因通常是"反馈不可比"（看不出是同一个失败）或"失败信息不足"。
本项目落点：`failure_signature = kind + 首行空白折叠[:160]`（`pytest_adapter.py`）；签名规则是受保护代码，因为 streak 依赖它稳定。
`面试知识`：更进阶的做法是"动作哈希去重 + 强制策略变化"（我用了签名，没用动作哈希，可以答出来作为延伸）。

**12. Tool Failure 处理**
三种：抛穿（任务死）、重试（可能重复副作用）、把失败作为观察喂回（模型决定）。
本项目落点：`execute` 把所有异常收敛成 `ToolResult`（`registry.py`）+ `RECOVERY_POLICY` 标注 `no_rerun/state_check`+ 崩溃恢复时孤儿回执带恢复提示。
`面试知识`：副作用工具的幂等键/去重表是生产做法，本项目**只做了 LLM 层重试的互斥**，没做工具层幂等。

**13. Multi-Agent**
真实需求通常是：角色隔离（工具集不同）、上下文隔离、预算隔离、并行。反模式是"对话式群聊"。
本项目落点：`roles.py` 三套冻结工具集、子任务无 `delegate/ask_user` ⇒ 深度 1；`SubtaskManager` 管槽位/并发/父切子预算/独立 ToolContext/独立轨迹/自有工作区；默认 `delegation_enabled=False`。
`面试口径`："有界子任务委派，不是 Multi-Agent。"（详见 `02` M12）

## C. 护栏与可靠性

**14. Guardrail**
输入护栏（分类/黑名单）、动作护栏（白名单/参数校验/审批）、输出护栏（脱敏/格式）、判定护栏（不接受模型自述）。
本项目落点：动作护栏最强（16 个受控工具、候选补丁写入口、七项门禁、阶段白名单）；输入有仓库说明来源/作用域与冻结验收校验，但没有通用prompt注入检测；输出护栏弱（结构裁剪有，仓库说明部分脱敏，工具输出无通用脱敏）。

**15. Hallucination（在代码修复场景的特异形态）**
不是"编造事实"，而是：引用不存在的文件/符号、给不存在的测试 id、把"我以为跑过"当事实。
本项目落点（`代码事实`，很精彩）：测试 id 必须唯一解析到 `(file, classname, name)` 三元组，0 命中=被 deselect、≥2=歧义，都判不过（`pytest_adapter.py`）；`update_memory` 自称 verified 被拒；影子模块门禁防"新建同名模块劫持判定"。

**16. Retry 的副作用问题**
"Retry 可能重复执行副作用"是必考题。答案要素：区分只读/幂等写/非幂等写；非幂等要么带幂等键要么禁止盲重跑。
本项目落点：`RECOVERY_POLICY`（见 §12）+ SDK/平台重试互斥 + 恢复不重算预算。

**17. Idempotency**
本项目落点：spec 内容哈希作幂等键 + 唯一索引 + 锁 + 终态先到先赢。
`面试知识`：fencing token、幂等表 TTL、"幂等键冲突但请求体不同应返回 409 而不是静默复用"（本项目是"同键同体才复用"，逻辑等价，但没显式测异体冲突）。

## D. 评测与数据

**18. 检索 vs 生成分别评估**
要素：检索层用 Recall@k / Precision@k / Hit Rate / MRR（金标=应该命中的文档集合），生成层用 faithfulness / answer relevance / citation correctness。混在一起看"端到端准不准"会导致改了检索却看不见收益。
本项目落点：**没有向量检索**（`代码事实`），但我有等价分层：`localized/strict/coverage` 评定位、`resolved` 评结果（见 `05` §4-5）。
`面试知识`：RAGAS 的三个指标（faithfulness / answer relevance / context relevance）；MRR 计算 = 首个正确结果排名的倒数取平均。被问公式时要能写出来。

**19. Chunking / Hybrid / BM25 / Rerank / Metadata Filter**
`面试知识`（本项目**全部未实现**，但岗位常问）：chunk 策略（固定长度 vs 语义/AST 边界 vs 父子块）、overlap、BM25 稀疏检索、hybrid（RRF 融合）、rerank（cross-encoder 重排 top-N）、metadata filter（预过滤减少召回噪声）、长文档（map-reduce / refine / 引用聚合）、知识更新（增量索引与版本失效）。
**本项目可对照的说法**："我用 AST 骨架 + rg 文件级预筛 + Python 逐行了断替代了这一整套，因为代码仓库内定位需要精确行号而不是语义相近片段。**未实现向量语义检索，也未测量语义召回质量**，如果 bug 描述和代码命名对不上，我这层就没有兜底——这正是我要引入 hybrid 检索做对照的原因。"

**20. Benchmark 的可信构建**
要素：样本代表性、GT 来源独立性、指标分母诚实、重复次数与置信区间、dev/hidden 分集、预登记判据、负面结果记录。
本项目落点：`代码事实` 有**预登记文化**（`experiment-preregistration-v2`、`paid-batch-preregistration` 含一次停手负面结果）、控制身份 vs 观测结果分离（`compare_experiments.py`）、指标版本化（`METRICS_VERSION=2`，v1 缺陷只作显式重算）、可疑好结果的哨兵（`report.py`）。**但样本代表性是零分**（3–5 文件合成题 + 回放 embed 答案）。

**21. LLM as Judge**
适用：无法程序化判定的开放输出。风险：位置偏好、冗长偏好、自我偏好、评分漂移、被输出里的"指令"操纵。缓解：成对比较 + 顺序交换、多裁判投票、锚定样例、与人工标注校准一致性、把裁判输出当**信号**不当**判定**。
本项目落点：`代码事实` **不用 LLM 做判定**；最接近的是 `propose_acceptance`（模型提验收清单 → 平台真跑基线验证 red/green → 人工审批 → manifest 哈希冻结，`app/diagnosis/proposal.py`）。被问"为什么不用 LLM judge"时答："我不想引入一个不可复现的判定源，我的整个判定链都在证明'结论必须是平台事实'。但我认为生产上应该把它当**排序信号**用，比如给多个候选补丁排序——那正是 Best-of-N 现在用规则排序的位置。"

**22. Badcase Analysis**
要素：按终止原因分桶（预算/门禁/验证失败/待审/取消）、按阶段定位瓶颈、区分"能力失败"与"平台失败"、每类留一条可复现轨迹。
本项目落点：`代码事实` 终态本身就分桶（INVALID_TASK / PATCH_REJECTED / VERIFY_FAILED / BUDGET_EXCEEDED / NEEDS_REVIEW / CANCELLED），轨迹逐工具记 `duration_ms/error`，报告三态显式（已验证/未运行/不确定）。`swe-ablation-evidence-2026-10-07.md` 是一次真实的瓶颈定位记录。

## E. 服务与工程

**23. FastAPI 的 async 真相**
`async def` 路由在事件循环上跑；`def` 路由被丢进 anyio 线程池；同步生成器响应体被 `iterate_in_threadpool` 包裹（每流占一线程）；依赖注入是 `Depends`；中间件 vs 路由依赖的鉴权位置差异。
本项目落点（`代码事实`，见 `02` M1）：路由全同步、`async def` 只在 ASGI 管道、并发靠 `ThreadPoolExecutor(2)`、SSE 是 0.5s 轮询 DB。
**这就是"知道是什么但不一定能解释为什么"的典型条目**，务必吃透这段——被问"async 给你带来了什么"时的满分答案在 `03` 的服务并发风险。

**24. SSE**
`text/event-stream`、`id:`/`retry:`/`event:` 字段、断线重连用 `Last-Event-ID`、心跳防代理超时、背压与连接数上限。
本项目落点：`routes.py` + `events.py`，`trajectory_events.id` 自增号作续传游标（`last_event_id` 参数）。
`面试知识`：WebSocket vs SSE 的取舍（单向进度用 SSE 更合适，鉴权与自动重连是它的优势；双向交互才需要 WS）。

**25. Redis / Qdrant / Docker / CI-CD**
- Redis：本项目只有锁（`03` 的 Redis 风险 已详述）。`面试知识`：缓存三大问题（穿透/雪崩/击穿）、`SET NX EX` + Lua 释放、Redlock 争议、fencing。
- Qdrant：`代码事实` **项目里没有**。`面试知识`：HNSW vs IVF、cosine vs dot vs euclidean、payload 索引与预过滤、`limit/offset` 与召回一致性。
- Docker：`代码事实` 两个 Dockerfile + compose（api+redis，回环绑定），默认executor镜像依赖有限，题目env可自带依赖镜像；CI build 镜像 + 跑 docker 单测（skipif）；uid 漂移有专门实验（`ADR-0002`、`docker-isolation-notes`）。
- CI/CD：`代码事实` 单 job ubuntu-24.04 + Python 3.11，lint → 全量 pytest → fake 冒烟 → artifact 上传（`if-no-files-found: error`）。缺：OS 矩阵、覆盖率、部署步骤、真实模型冒烟。

**26. Cost / Latency / Quality 三角**
`面试知识` + 本项目落点：
- Cost：`token_budget`、`cost_budget_micro_usd`（默认 0=不限，是个洞）、`pricing.py` micro-USD 向上取整、未收录模型 n/a、**`settled_bill` 无来源**（代码自述）。
- Latency：逐工具 `duration_ms`、`llm_timeout_seconds=120`、`task_timeout_seconds=900`；**但没有任何 p50/p95 聚合**，因为没指标系统。
- Quality：`resolved`、终止原因分布、双跑一致性。
**必背 trade-off 例子**（都在代码里）：① Best-of-N增加候选和成本，质量收益未证明，延迟受并发与工具执行影响，且默认只在 `branching_min_token_reserve=30000` 富余时才开（`config.py`）；② 双跑复核提可信度、成本近乎翻倍（`verify_double_run`）；③ 大输出"先裁剪再落盘 + 可按游标取回"省上下文但增加一次取回延迟（`registry.py`）；④ 压不动就 `context_capacity_exceeded` 停下，而不是烧钱反复压缩。

---

# P1（建议掌握，按被问概率排）

| 条目 | 为什么值得 | 与项目的连接点 |
|---|---|---|
| Agent 评测方法（轨迹级、pass@k、pass^k） | Agent 岗高频 | 我有终态分桶但没 pass@k；重复批次是缺口 |
| 非确定性回归（噪声地板、k 次重复、置信区间） | 评测岗核心 | `paid-batch-preregistration` 的噪声地板批设计（7 题 × 3 次）可讲 |
| Prompt/Context 版本与实验管理（manifest、A/B） | 团队实践题 | `experiment_manifest.py` + 控制身份/观测分离 |
| 幂等与副作用（工具层幂等键） | 工程题 | `RECOVERY_POLICY`，承认没做工具层幂等 |
| 流式输出的 tool_call 增量拼装 | 追问 async 时会到 | 我没做流式，能讲为什么不做 |
| Embedding 选型与评测（MTEB、领域微调、成本） | RAG 题 | 我没用，但能说"什么条件下必须用" |
| 长文档 / 聚合型问答（map-reduce、refine） | RAG 题 | 对应"我的骨架按目录聚合"这个相似设计 |
| 观测：OTel GenAI 语义约定、trace 关联 | 生产化必问 | 我没有，但轨迹自增 id 可作 correlation key |
| 成本归因（per-user/per-task） | 商业意识 | 账本按 task/stage/round 已归因，缺 principal |
| 数据合规与 DLP | 生产化 | 输出不脱敏是已知洞（`06` P1-4） |
| 依赖注入与测试替身设计 | 工程题 | `create_app(db_path, runs_root, bugs_root)` 工厂 + FakeLLM + 临时 git 靶仓 |
| pytest 高级用法（fixture scope、autouse、钩子、标记治理） | 测试岗 | `conftest.py` 的 `_basetemp_outside_repo`、外部登记表打标、"跑了≥5s 没登记"告警 |

# P2（以后补）

微调/RAG 之外的训练类（LoRA、蒸馏）、推理引擎与显存、模型对齐与安全评估、多模态、离线评测集自动化构建、K8s/HPA、Service Mesh、形式化验证与差分测试、SAST/SCA 集成、gVisor/Kata 细节、Redlock 与分布式共识。

---

## 3 天里的用法

1. **先过 P0 的 26 条**，每条问自己两个问题：*我能用一句自己的话解释吗？* 和 *我能指到项目里的哪一行，或者能说清"我没做，因为……"吗？* 两个都做不到就标红。
2. **第 19/18/21 条是"本项目没有但岗位爱问"的三条**，属于纯 `面试知识`，必须靠背诵而不是靠项目回答——不要把它们伪装成项目实现（任务卡明令禁止）。
3. **P0 里最容易被项目本身撑住的其实是 6–17 条**（Agent 机制），因为每条都有代码落点。把"有代码落点"的条目优先练熟，性价比最高。
