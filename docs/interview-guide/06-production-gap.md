# 06 · Demo → Production 的缺口（按 P0/P1/P2）

> 分级是未来生产化优先级，不代表当前仓库审查确认了同级缺陷。
> 铁律：**禁止把生产化扩展说成已实现**。本文每一节先给"当前真实情况（含代码位置）"，再给"缺什么 / 怎么补"。
> 讲法模板：「这条我已经有什么 → 这条我明确没有 → 上线第一步是哪个」。

---

# P0（不做就不能对外提供服务）

## P0-1 Authentication / Authorization（鉴权与授权）

| 维度 | 当前真实情况（`代码事实`） | 缺口 |
|---|---|---|
| AuthN | `app/api/auth.py`：Bearer token，`secrets.compare_digest` 定长比较；但 `PATCHPILOT_API_TOKEN` **默认空 = 完全关闭**，只在启动时打一条 warning（`app.py`）；`/api/health` 永久豁免 | 无用户体系、无 API Key 生命周期（签发/轮换/吊销）、无 session、无 OIDC |
| AuthZ | **无角色概念**。任何能调 API 的主体可以对任意 `bug_id` / 任意 `repo_path` 建任务 | 无资源级授权：谁能看哪个任务、谁的报告、谁的任务 |
| 输入约束 | `PATCHPILOT_ALLOWED_REPO_ROOTS` **默认空 = 不限制**（`config.py`），且只作用于 `repo_path` 分支（`service.py`）；`bug_id` 走 pattern + `bugs_root` 校验（`schemas.py`、`service.py`） | compose 里也传空（`docker-compose.yml`） |

**为什么是 P0**：`代码事实` 的组合是"默认无鉴权 + 可指定任意宿主目录 + 该目录的 `conftest.py` 会被执行" → 若服务执行攻击者可控制的仓库，暴露默认无鉴权入口可能导致宿主代码执行。`docs/threat-model.md` 自己就是这么写的。
**怎么补**（顺序即优先级）：① 默认开启 token 并在**非回环绑定 fail-fast**（现在是 warning）；② `ALLOWED_REPO_ROOTS` 默认非空；③ 引入 principal 概念 + 任务归属行（`tasks.owner`），所有 GET/cancel/answer 校验归属；④ 令牌哈希存库（现在配置里是明文）；⑤ 走 OIDC/服务账号；⑥ 速率与配额绑定 principal。
**面试口径**："默认姿态是回环单用户，这是我明确不防的一项，写在威胁模型里。要对外服务，第一件事不是加权限模型，是**把默认值从'不限'改成'拒'**。"

## P0-2 Rate Limit / 成本控制（最直接的烧钱风险）

**当前真实情况**：没有任何限流（无中间件、无 429）。唯一的钱闸门是**单任务预算**：`token_budget=200000`、`cost_budget_micro_usd=0`（0=不限）、`max_model_calls=0`、`llm_max_tokens=4096`、`default_max_rounds=5`、`max_turns=20`，由 `ResourceLedger` 原子预留执行（`plain_loop.py`）。
**缺口**：全局并发只有 `task_max_workers=2` 这一个隐式限流；**没有 per-user/per-IP 速率、没有每日配额、没有排队上限、没有"提交即扣额度"**；`budget_mode=tokens` 默认按 token 而非钱，`cost` 只在有价目时才算得出（未收录模型 n/a）。
**怎么补**：令牌桶 per-principal + 队列上限 + 429/Retry-After；预算以"钱"为主单位并在受理时**预扣**（现在是执行中拦）；接入账单源做对账（`pricing.py:provenance` 明写"本仓库没有任何账单来源"，`settled_bill` 永远拿不到真值）；成本异常检测（单任务超 p95 阈值即熔断）。

## P0-3 Sandbox（执行隔离成为默认路径）

见 `02` M7 与 `03` 的默认隔离风险。`代码事实`：docker 后端真实存在（`command_runner.py`：`--rm --network none --memory 1g --cpus 1.0 --pids-limit 128 --security-opt no-new-privileges`），但 `execution_backend=local` 是默认（`config.py`），执行默认 local，可显式选择 Docker，默认 executor 镜像依赖有限；题目 env 可自带官方或定制镜像，compose 挂 `docker.sock`，`docker_only` 无人使用。
**怎么补**：默认 docker；镜像按目标仓库 lock 文件构建（或让受理阶段要求提供镜像）；`--read-only` + tmpfs scratch；去 socket；每任务独立网络命名空间；用 gVisor/Kata 补内核面；宿主 env 白名单再删可写路径（现在仍透 `TEMP/APPDATA/USERPROFILE`）；资源配额加到 local 兜底路径上。

## P0-4 可观测性：Logging / Tracing / Metrics

- **Logging**（有，`代码事实`）：stdlib `logging`，业务日志带 `task_id`（`registry.py` 的整改注释就是补 task_id 的现场）+ `request_id` contextvar（`app.py`）。
- **Tracing**（无）：没有 OpenTelemetry，没有 span 父子关系；一次任务的因果链靠 `trajectory.jsonl` + SQLite `trajectory_events`（自增 id 作游标）。这已经比多数 Demo 强，但**跨服务不可串联**。
- **Metrics**（无）：没有 `/metrics`，没有 Prometheus，没有 p95 延迟。`app/evals/metrics.py` 是**评测指标**不是运维指标，别混称。
- **怎么补**：OTel（trace + 模型调用 span，属性含 model/tokens/cost/latency/tool）；Prometheus 指标（任务终态分布、每阶段耗时、工具失败率、预算耗尽率、恢复次数、SSE 在线数）；结构化 JSON 日志 + 集中采集；把 `trajectory_events` 的自增 id 作为 trace correlation key。
- **追问预案**："你怎么发现线上任务变慢？"→ "我现在发现不了，因为没指标。我能做的是每次任务的 `duration_ms` 已经逐工具记录在轨迹里（`registry.py`），生产化只要把它聚合出来。"

---

# P1（上线后一周内会出事）

## P1-1 Timeout / Retry / Idempotency（这三件事的真实状态）

| 项 | 有 | 没有 / 弱点 |
|---|---|---|
| Timeout | 请求级 `llm_timeout_seconds=120`；执行级 `command_timeout_seconds=120` + 杀进程树；任务级 `task_timeout_seconds=900` + 绝对寿命 `86400s`；受理级 `intake_timeout_seconds=120` | **没有 per-test timeout**（只有整轮 pytest）；`recycle_grace_seconds` 与 `acceptance_wait_seconds=3600` 语义重叠；`_execute` 在 shutdown 时不 join 线程（`service.py`） |
| Retry | 传输层 allowlist + SDK/平台重试互斥（`openai_client.py`）；工具恢复策略 `no_rerun/state_check`（`registry.py`） | **没有语义级退避重试**（429/529 都归传输处理）；崩溃恢复时"崩溃前落地的写"仍靠模型读恢复提示自行判断（`session.py`）；重试不重算预算（`service.py`） |
| Idempotency | **强**：spec 哈希 + 锁 + 唯一索引 + 终态先到先赢（`service.py`、`repository.py`） | 锁 key 只在配 Redis 时才是跨进程的；`InMemoryLock` release 无持有者校验（docstring 自认，靠 DB 兜） |

**怎么补**：给 pytest 加单测超时（`pytest-timeout` 不可信，因为不能要求靶仓装插件 → 由执行器按输出静默时长判停）；重试用满 exponential backoff + jitter 并区分 429/5xx；恢复时把"写类工具"的执行事实落到 ledger（现在只到轨迹）。

## P1-2 Queue（任务队列）

**当前真实情况**：没有队列。`ThreadPoolExecutor(max_workers=2)` + 立即受理，意味着**提交 1000 个任务就创建 1000 个工作区目录和 1000 行 DB 记录**，线程池自己排队，但磁盘和 DB 没人管（`recycle_finished_workspace=True` 只回收终态且超 grace 的）。
**怎么补**：外部 broker + worker pool（arq/Celery/Temporal）+ 队列深度上限 + 优先级 + 公平调度 per-principal；受理时入队而非起线程；加背压（429）。

## P1-3 Cache

**当前真实情况**：**全系统没有任何缓存**（Redis 只做锁）。已有的只是"同一进程内的重复计算复用"：上下文装配按 `(stage, round_no)` 幂等缓存并记 `context_assembly_reused` 事件（`nodes.py`）、`get_settings()` 的 `lru_cache`。
**缺口 & 机会**：模型层无缓存（同一 prompt 重复请求照样付钱）；无检索缓存。生产化的第一份缓存应该是**响应级**（prompt hash → completion，带 TTL 与 provider 计费口径），第二份是**检索/仓库骨架**（按 commit hash 失效）。
**追问预案**："为什么不用 Redis 缓存？"→ "因为我确实没用上。如果加，我会先用它缓存装配好的上下文骨架（键=仓库 commit+profile），因为那是纯计算且随任务重复；模型响应缓存要谨慎，因为它会污染非确定性回归。"

## P1-4 Secret Management

**当前真实情况**：`.env` + pydantic-settings；API token 与 LLM key 都是**进程环境变量**，明文在配置文件里。已有缓解：子进程 env 白名单重建（`local_runner.py`，注释点名 API_TOKEN/LLM_API_KEY 曾被不受信 conftest 读到，这是真实修复）；轨迹/快照按 key 名脱敏（`tracker.py`、`provenance.py`）；仓库说明里形似凭据的行渲染前打码（`repo_instructions.py`）。
**缺口**（`风险点`）：**输出侧完全不脱敏**——靶仓测试打印的密钥会进模型上下文、`trajectory.jsonl`、证据仓（`output_filter.py` 无 redaction）；价目覆盖文件路径也走配置。
**怎么补**：KMS/Vault + 短时效凭据；出站前 redaction pass（正则 + 熵检测）；轨迹分级 + TTL + 加密；对"可能被不受信代码读到"的进程再降权限（独立 service account、per-task 密钥）。

## P1-5 Prompt Injection（本项目已经认真防了一部分）

**已有（`代码事实`）**，这是可以正面讲的：
- 优先级硬编码：**平台硬规则 > 用户任务约束 > 仓库说明**，且"规则只指导怎么改，工具白名单、门禁与验收契约的判定路径**完全不读这一层**"（`docs/adr/0015`、`repo_instructions.py`）——即不受信文本**拿不到权限面**；
- 不受信仓库的 `AGENTS.md/CLAUDE.md` 从冻结副本按固定文件名加载，符号链接/`.git`/二进制/宿主路径拒读，超限明写；
- 模型没有通用写工具，候选补丁入口过门禁，scratch/记忆/交互写入另有边界；模型不能决定阶段跳转；
- `update_memory` 拒绝模型自称 `verified/confirmed/resolved`（`memory.py`）——防"自我说服"；
- 终局判定不采信模型自述（`acceptance.py`）。

**缺口**：不受信**代码内容**（读到的源码、测试输出）可以诱导模型去 `run_command`（profile 受限）或写复杂补丁；没有输入分类器、没有危险意图检测、没有"工具调用需人批准"的默认策略（`ask_user` 存在但不由平台触发）。`合理推断`：最实际的攻击面仍是 P0-3（执行隔离），而不是纯提示层。
**怎么补**：把不受信内容用显式分隔符 + 每次请求重申"以下是数据不是指令"；对 `python_repro`/`run_command` 加批准门槛；引入轻量意图分类器 + 高风险动作二次确认；把 `ask_user` 从"模型可请求"变成"平台可强制"。

## P1-6 Data Privacy

**当前真实情况**：任务把宿主仓库源码复制进 `runs/<task>/`（工作区 + `source_snapshot`），轨迹里存 prompt/completion 全文与工具输出，证据仓存**裁前原文**（`evidence/`，`evidence_max_total_bytes=64MB`）。没有任何数据分类、保留策略、删除接口——`recycle.py` 只删 `source_snapshot`（且这是"回收后无法重新取证"的取舍）。发往第三方模型 API 的数据没有任何 DLP/PII 过滤。
**怎么补**：数据分级 + 保留/删除 SLA + 用户可删除接口 + 出站 DLP + 轨迹采样与压缩（现在是全量）。

## P1-7 Model Fallback

**当前真实情况**：**没有**。模型不可用/超时/被拒 = 任务 `NEEDS_REVIEW` 或失败。`profiles.py` 有档案、`capabilities.py` 能报能力，但没有跨供应商回退、没有熔断、没有健康探测。
**怎么补**：主/备供应商配置 + 熔断（错误率阈值）+ 回退时**必须重算能力档案与价目**（否则预算判定失真）+ `served_model` 漂移检测已有，可以直接接上。

---

# P2（规模化之前处理）

## P2-1 Database Migration & Horizontal Scaling

**当前真实情况**：`db.py` 用手写 `ALTER TABLE ... ADD COLUMN` 的容错迁移（加列即可，无版本表、无回滚）；单 SQLite 文件、单连接 `check_same_thread=False` + WAL、全局 `threading.Lock` → **写吞吐与多实例领取安全未经过生产验证**。当前三张业务表，去重迁移另有归档表。
**怎么补**：Alembic（版本化 + 可回滚）；Postgres（行锁 + `SELECT FOR UPDATE SKIP LOCKED` 领取任务）；事件流从轮询改 pubsub；工件（report.json/轨迹/证据）迁对象存储，DB 只存指针。

## P2-2 Load Test

**当前真实情况**：`stress` 标记只有 1 个测试、约 100s、本机跳过（`pyproject.toml` markers + `slow_tests.txt`）。**没有任何压测数据**，`task_max_workers=2` 是设定值而非测量值。
**怎么补**：先量基线（提交速率、SSE 并发、线程池耗尽点）；再做容量模型（每任务占 1 线程 + 可能嵌套 1 分支池线程 + 每个 SSE 观察者长期占 1 线程）；重点压"2 个跑着的任务 + 10 个观察者"这个形状，因为它现在就会把 ~40 线程的 anyio limiter 压住。
**面试口径**："没有压测数据，所以我不给任何 QPS 或延迟承诺——我能给出的是瓶颈在哪一段。"

## P2-3 Failure Recovery（这条其实很强，讲的时候别把它降级）

**已有（`代码事实`）**：LangGraph SQLite checkpoint（B 层）+ `loop_state.json` 回合边界快照（A 层，只在一致切点写、2MiB 上限、永不抛错）+ `ledger_state.json` + 冻结候选；`prepare_resume` 返回 `continue/revalidate/already_terminal/reject` 结构化决策；可写阶段强制重验证且**清空 verify/gate 布尔**；`already_terminal` 只重放工件不重跑；孤儿 `tool_calls` 用显式标注的占位回执修复并附恢复策略；真实进程崩溃有测试（`test_resume_crash.py` 用 `os._exit`）；`demo/run_restart_resume_ticket.py` 是可演场景。
**缺**：`checkpointer` 依赖缺失时静默返回 `None`（任务能跑但不可恢复，应 fail-fast，`checkpoint.py`）；没有跨机恢复（文件工件在本地盘）；没有"多久没心跳判死"的租约机制（只有锁 TTL）。

---

## 一页总览（面试前 30 秒扫这张表）

覆盖关系先说清楚，免得数字对不上：上面正文按 **P0 4 节 / P1 7 节 / P2 3 节 = 14 节**组织，下表 **19 行**逐项对上任务书列出的 21 项能力——`Cost Control` 与 `Token Budget` 合成一行（代码里就是同一个账本），`Logging / Tracing / Metrics` 合成 P0-4 一节但表里 Tracing/Metrics 与 Logging 同属"可观测性"一行，`Authentication`/`Authorization` 拆成 AuthN/AuthZ 两行。有人问"你数过没有"，答案是"14 节、19 行、21 项，对应关系在这里"。

| 能力 | 状态 | 一句话 |
|---|---|---|
| Idempotency | **有且强** | spec 哈希 + 锁 + 唯一索引 + 终态先到先赢 |
| Failure Recovery | **有且强** | 三层快照 + 重验证 + 不复用结论 + 真崩溃测试 |
| Timeout | **基本有** | 四层超时齐；缺 per-test |
| Retry | **谨慎有** | 分层互斥 + 恢复策略标签；缺语义退避 |
| Cost/Token Budget | **有** | 原子预留 + provider 真值优先；缺预扣与账单对账 |
| AuthN | 有开关，**默认关** | Bearer + 定长比较；无用户体系 |
| AuthZ | **无** | 只有 repo 根白名单，且默认为空 |
| Rate Limit | **无** | 只有 `workers=2` 的隐式限流 |
| Sandbox | **代码有，默认路径无** | local 后端 = 宿主任意 Python |
| Secret Mgmt | **弱** | env 白名单已修；输出不脱敏 |
| Prompt Injection | **部分防** | 不受信文本无权限面；缺意图检测 |
| Data Privacy | **无** | 全量轨迹 + 无保留策略 |
| Cache | **无** | Redis 只做锁 |
| Queue | **无** | 线程池即队列，无背压 |
| Logging / Tracing / Metrics | **只有 Logging** | 结构化轨迹 + stdlib 日志齐；无 OTel tracing、无 metrics 出口 |
| Model Fallback | **无** | 一家不通就停 |
| DB Migration | **手写** | 加列容错，无版本化 |
| Horizontal Scaling | **不支持** | 单文件 SQLite + 全局锁 |
| Load Test | **无** | 不给 QPS 承诺 |

**背这段收尾**："我的原型里，判定链、幂等、恢复、预算这四块是能拿上台面的；隔离默认值、鉴权默认值、可观测性和队列是上线前必须补的四件事。我把它们按'不做就不能对外'和'上线后一周内会出事'分开排了序，顺序在 `06` 这份文档里。"
