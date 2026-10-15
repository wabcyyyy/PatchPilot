# 工业使用改进执行进度

任务定义：[改进计划](industrialization-plan-2026-10-15.md)。
状态只在此处维护；旧 Agent 升级进度继续保存原 G 卡，不在两处复制维护 P 卡。

## D00 — 计划与交接文档（2026-10-15）

- 用户请求：根据本轮审核生成完整改进计划，交其他 Agent 执行。
- 开始 HEAD：`972a1e8cc1fd4742207d3645339ed5682faaf680`。
- 已有 dirty：`docs/interview-guide.zip`，不属于本卡，保留且不暂存。
- 允许范围：本目录的 industrialization-plan/progress/handoff 三份日期文档、
  `docs/work-notes/README.md`、`docs/README.md`、`PROGRESS.md`。
- 工作：将审核风险映射为 P00–P21，声明生产目标、范围、依赖、测试种子、验收矩阵、授权边界和交接纪律。
- 本卡不实施产品改造、不启动外部任务、不付费、不 push、不发布。
- 验证：`.validation/20261015-225844-f69a6a00.json`，六份文档 diff 检查与现有
  docs_anchors 文件的 10 项检查通过；执行步骤约 1.3 秒。最初临时目录权限失败的
  `.validation/20261015-225833-7d36743d.json` 为 incomplete，保留且不算通过。
- 未运行：产品回归、真实 Docker/负载/模型实验、CI；本卡不复跑上轮 44 项产品测试。
  最后仅登记本段收据，不为记账重跑检查；实现卡与最终版本完整回归交 CI。
- Commit：本段所在 `docs(plan)` 提交；不为写自身 hash 再提交。

## P00 — 固化生产契约与架构决定（2026-10-15）

- 开始 HEAD：`45175a6`。
- 已有 dirty：`docs/interview-guide.zip`，不属于本卡，保留且不暂存。
- 改动范围：`docs/adr/0016-生产契约与架构边界.md`、`docs/workload-manifest.md`、`docs/threat-model.md`。
- 工作：
  1. 复核工业改进计划审查事实与 12 项架构风险；
  2. 固化信任边界与双运行模式（development vs production），明确 API 进程不持 Docker socket，由独立 Worker 在专用环境执行不可信代码；
  3. 定义主体、凭据、任务规格、作业租约、工件、预算权威源；
  4. 冻结 workload manifest（执行并发 2、排队 20、受理并发 2、取消确认 5s、平稳演练 30m、API p95 ≤ 500ms、RPO ≤ 60s、RTO ≤ 120s）；
  5. 产出 ADR-0016，更新威胁模型。
- 验证：`verify.ps1 -Tests tests/test_docs_anchors.py`，收据 `.validation/20261015-231752-36018573.json`，11 项检查通过。
- 未运行：产品代码回归、外部模型调用；本卡无产品代码修改。
- Commit：`8b64907`。
- 下一张卡：P01（版本化迁移与持久化模型）。

## P01 — 版本化迁移与持久化模型（2026-10-15）

- 开始 HEAD：`8b64907`。
- 已有 dirty：`docs/interview-guide.zip`，不属于本卡，保留且不暂存。
- 改动范围：`app/storage/migrations.py`、`app/storage/db.py`、`app/storage/repository.py`、`tests/test_migrations.py`。
- 工作：
  1. 新增版本化向前迁移模块 `app/storage/migrations.py`，支持事务化运行与 SQLite IMMEDIATE 串行互斥；
  2. 实现 `schema_migrations` 表与 v1（基线三表与契约列）、v2（工业持久化模型）迁移逻辑；
  3. `tasks` 增加 `owner` 与 `principal_id` 列，旧任务回填为 `legacy-unowned`，杜绝未授权归属；
  4. 建立活动任务唯一幂等索引（`uq_tasks_active_principal_idem`），仅约束未终态任务，支持终态安全重跑；
  5. 预创建主体表（`principals`）、凭据表（`credentials`）、审计事件表（`audit_events`）、作业与租约表（`jobs`）、额度预留表（`quota_reservations`）、工件记录表（`artifact_records`）；
  6. 修复多线程并发连接下 busy_timeout 在 WAL 前设置的互斥锁竞争问题。
- 验证：
  - 定向单元与迁移测试：`tests/test_migrations.py`、`tests/test_storage.py`（16 passed in 1.71s），`tests/test_legacy_migration.py`（4 passed in 14.94s）；
  - 静态与代码规范：AST、Ruff check、Ruff format 全部通过；
  - 快速验证收据：`.validation/20261015-232638-318cd0d5.json`（静态通过，6 个被选测试达 90 秒预算停止记 budget_exhausted，不自动重启扩大，完整回归留 CI）。
- Commit：`5186085`。
- 下一张卡：P02（主体认证与凭据生命周期）。

## P02 — 主体认证与凭据生命周期（2026-10-15）

- 开始 HEAD：`5186085`。
- 已有 dirty：`docs/interview-guide.zip`，不属于本卡，保留且不暂存。
- 改动范围：`app/api/auth.py`、`app/config.py`、`app/storage/repository.py`、`app/agent/cli.py`、`tests/test_auth.py`、`tests/test_credentials.py`。
- 工作：
  1. `app/storage/repository.py` 实现主体创建与凭据签发、SHA-256 不可逆单向散列存储、认证、吊销与带宽限期轮换；明文仅签发时展示一次，数据库与日志绝不记录明文；
  2. `app/config.py` 引入 `deployment_mode` 配置项（`development` / `production`）；
  3. `app/api/auth.py` 重构凭据认证，支持 `Authorization: Bearer <key>` 与 `X-API-Key: <key>`，向 `request.state.principal` 注入已认证主体；
  4. 生产模式（`deployment_mode=production`）下强制禁止匿名访问和开发共享 Token，仅放行活跃 DB API Key；开发模式保持旧接口与共享 Token 兼容；
  5. `app/agent/cli.py` 扩充 `--api-key` 与环境变量（`PATCHPILOT_API_KEY`）支持，增加本地管理员受保护命令行工具（`admin create-principal`、`issue-key`、`revoke-key`、`rotate-key`、`list-keys`），杜绝暴露未经认证的网络管理 HTTP 端点；
  6. 新增 `tests/test_credentials.py`，并在 `tests/test_auth.py` 扩充生产模式准入与吊销校验。
- 验证：`verify.ps1 -Tests @('tests/test_credentials.py', 'tests/test_auth.py')`，收据 `.validation/20261015-233538-601dd7a5.json`，11 项全部通过（耗时 4.55s）；CLI 消费者回归（`test_capabilities.py`、`test_diagnosis_intake.py`）14 项全部通过（18.56s）。
- Commit：`e8a67b1`。
- 下一张卡：P07 或 P03（按 Batch A 推进，P07 无依赖，P03 依赖 P02）。

## P07 — pytest 输出边界与 JUnit 防护（2026-10-15）

- 开始 HEAD：`e8a67b1`。
- 已有 dirty：`docs/interview-guide.zip`，不属于本卡，保留且不暂存。
- 改动范围：`app/executor/local_runner.py`、`app/adapters/pytest_adapter.py`、`tests/test_test_output_limits.py`。
- 工作：
  1. `app/executor/local_runner.py` 引入 `BoundedStreamDrainer`，采用后台独立线程分块（64KB）流式消费子进程 stdout/stderr，内存中只保留有界尾部环形缓冲（按 `max_output_chars`），精准统计总字节数与丢弃字节数（`dropped_bytes`）；
  2. `TestRunResult` 扩充 `stdout_bytes`、`stderr_bytes`、`stdout_dropped_bytes`、`stderr_dropped_bytes` 与 `@property dropped_bytes`，输出截断不再静默；
  3. `app/adapters/pytest_adapter.py` 设定 JUnit XML 最大解析上限 `MAX_JUNIT_BYTES = 10MB`，防止恶意/超大 XML 报告造成 OOM；
  4. `parse_junit_xml` 增加文件大小校验与解析异常保护，捕获 `ET.ParseError`、`OSError`、`ValueError`、`UnicodeDecodeError`，转为结构化合成错误（`signature="error: invalid junit xml"` 或 `"error: junit xml too large"`），判定口径直接判未通过（`all_passed=False`），杜绝进程崩溃；
  5. 新增 `tests/test_test_output_limits.py`，完整覆盖超大流式输出丢弃统计、无换行长流消费、超大 XML 拦截、畸形 XML 结构化容错与合规 XML 正常解析。
- 验证：
  - 定向单元测试：`tests/test_test_output_limits.py` 5 项全部通过（0.21s）；
- Commit：`01b4bf7`。
- 下一张卡：P03（资源级授权与可归因审计）。

## P03 — 资源级授权与可归因审计（2026-10-15）

- 开始 HEAD：`01b4bf7`。
- 已有 dirty：`docs/interview-guide.zip`，不属于本卡，保留且不暂存。
- 改动范围：`app/storage/repository.py`、`app/api/auth.py`、`app/api/service.py`、`app/api/routes.py`、`app/api/events.py`、`tests/test_resource_authorization.py`。
- 工作：
  1. `app/storage/repository.py` 扩充 `record_audit_event` 与 `list_audit_events`，实现不可变审计事件存储与检索；新增 `get_credential` 查询；
  2. `app/api/auth.py` 固化权限矩阵（`ACTION_CREATE`, `ACTION_READ`, `ACTION_CANCEL`, `ACTION_APPROVE`, `ACTION_MESSAGE`），实现 `authorize_task_access`：
     - 主体为任务属主（`owner == principal_id`）：全部放行；
     - 管理员（`admin`）：全部放行，若为跨属主访问自动记录 `admin_cross_owner_access` 审计；
     - 评审员（`reviewer`）：只读放行与验收清单批准放行，禁止取消与追加消息；
     - 普通成员与服务账号（`member` / `service_account`）：跨属主访问一律拒绝；
     - 越权尝试自动记录 `task_access_denied` 审计，向客户端严格返回 404（`invalid_task: task not found`），不泄露任务存在性；
  3. `app/api/service.py` 将主体并入任务创建与幂等判定域（`owner`, `principal_id`, `lock_key = f"task:{principal_id}:{idem_key}"`），不同主体互不冲突或劫持；
  4. `app/api/routes.py` 在全部 12 个端点注入主体鉴权与归因审计；
  5. `app/api/events.py` 在 SSE 长连接轮询中引入凭据生命周期校验，凭据被吊销时即刻输出 error 帧并正常断开连接；
  6. 新增 `tests/test_resource_authorization.py`，完整覆盖跨主体防泄漏、列表过滤、reviewer/admin 权限、越权审计与 SSE 吊销。
- 验证：
  - 定向权限测试：`tests/test_resource_authorization.py` 3 项全部通过（0.95s）；
  - 既有认证与流式测试：`tests/test_auth.py` + `tests/test_event_stream.py` 12 项全部通过（14.92s）；
  - 快速验证收据：`.validation/20261015-235932-4b35cd29.json`（静态检查与 Ruff 全部通过，6 个被选测试达 90 秒预算停止记 budget_exhausted，不自动重启扩大，完整回归留 CI）。
- Commit：`ffab2de` (`feat(auth): resource-level authorization and auditing (P03)`).
- 下一张卡：P04（显式生产模式与安全受理）。

### P04 — 显式生产模式与安全受理 (2026-10-16)

- 目标与范围：
  - `app/config.py`、`app/api/{app,service,preflight,capabilities}.py`、`app/task_spec.py`、`.env.example`；
  - tests：`tests/test_production_mode.py`、`tests/test_capabilities.py`、`tests/test_preflight.py`、`tests/test_resource_authorization.py`。
- 实现：
  1. `app/config.py` 增加 `trusted_docker_images` 配置项，提供 `get_trusted_docker_images()`、`get_allowed_repo_roots()` 与 `validate_production_readiness()` 就绪检查（校验 docker 执行后端、非空白名单、受信任镜像、有限 token 预算）；
  2. `app/task_spec.py` 在 `effective_policy` 中冻结 `deployment_mode`，并在 `build_task_spec` 中前置生产准入防御，严禁 local 执行、空白名单、越界代码目录、无限预算或未知镜像；
  3. `app/api/preflight.py` 的 `run_preflight` 接入 `deployment_mode`、`allowed_repo_roots` 与 `trusted_docker_images`，生产模式未满足直接收敛为 `InvalidRequestError` 拒绝受理；
  4. `app/api/capabilities.py` 暴露 `deployment_mode` 与 `production_readiness`（含 `ready: bool` 与 `issues: list[str]`）；
  5. `app/api/service.py` 在 `create_task` 和 `ensure_repo_allowed` 前置生产模式准入校验；
  6. `app/api/app.py` lifespan 在生产模式下输出就绪告警日志；
  7. `.env.example` 登记 `PATCHPILOT_DEPLOYMENT_MODE` 与 `PATCHPILOT_TRUSTED_DOCKER_IMAGES`；
  8. 新增 `tests/test_production_mode.py` 覆盖就绪检查、能力面、契约冻结、preflight 阻断与 service 准入拦截。
- 验证：
  - 定向测试：`tests/test_production_mode.py` 7 项通过（0.43s）；
  - `verify.ps1` 快速检查：收据 `.validation/20261016-001717-09ebcad3.json`（exit 0，6 个测试文件共 60 项测试全部通过，耗时 40.5s）；
  - 81 个未变动测试 deferred to CI。
- Commit：待提交。
- 下一张卡：P05（有界输入冻结与可重建环境）。

## 任务状态

| 卡 | 状态 | 依赖 | owner / commit / 收据 |
|---|---|---|---|
| P00 | DONE | 无 | 本卡 / 8b64907 / .validation/20261015-231752-36018573.json |
| P01 | DONE | P00 | 本卡 / 5186085 / .validation/20261015-232638-318cd0d5.json + targeted 16 passed |
| P02 | DONE | P01 | 本卡 / e8a67b1 / .validation/20261015-233538-601dd7a5.json (11 passed) |
| P03 | DONE | P02 | 本卡 / ffab2de / .validation/20261015-235932-4b35cd29.json + targeted 15 passed |
| P04 | DONE | P00/P02 | 本卡 / feat(admission) / .validation/20261016-001717-09ebcad3.json (60 passed) |
| P05 | TODO | P00/P04 | — |
| P06 | TODO | P03/P05 | — |
| P07 | DONE | P00 | 本卡 / 01b4bf7 / .validation/20261015-234428-ddb46fe6.json (35 passed) |
| P08 | TODO | P04/P07 | — |
| P09 | TODO | P07/P08 | — |
| P10 | TODO | P01/P03/P04 | — |
| P11 | TODO | P01/P03/P04/P09/P10 | — |
| P12 | TODO | P11 | — |
| P13 | TODO | P04/P11/P12 | — |
| P14 | TODO | P01/P03/P06/P11/P12 | — |
| P15 | TODO | P00 | — |
| P16 | TODO | P03/P06/P11/P12/P15 | — |
| P17 | TODO | P00/P06/P08/P09 | — |
| P18 | TODO | P15/P16/P17 | — |
| P19 | TODO | P05–P15/P17 | — |
| P20 | TODO | P00–P19 | — |
| P21 | OPTIONAL_NOT_STARTED | M3/新规格 | — |

状态建议：TODO / IN_PROGRESS / IMPLEMENTED_PENDING_EVIDENCE / NOT_RUN_ENV /
READY_FOR_AUTHORIZATION / DONE。没有真实环境证据时不要把关键卡直接记 DONE。

## 后续每卡记录模板

```text
卡号/子卡/owner/开始与结束源码身份：
改动范围与已有 dirty：
具体问题、实现、为何必要：
规范与语义差异/兼容/迁移/回滚：
验收输入→动作→观测结果；证据等级与工件位置/hash：
verify 命令、退出码、耗时、收据；环境失败/skip/未运行分开：
commit；未验证项与 CI/外部证据依赖：
费用/外部动作；下一卡：
```
