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
- Commit：`3af075b` (`feat(admission): explicit production mode and safe admission (P04)`).
- 下一张卡：P05（有界输入冻结与可重建环境）。

### P05 — 有界输入冻结与可重建环境 (2026-10-16)

- 目标与范围：
  - `app/gitops/input_snapshot.py`、`app/task_spec.py`、`app/api/{service,preflight}.py`、`app/config.py`；
  - tests：`tests/test_input_snapshot.py`、`tests/test_task_spec.py`、`tests/test_custom_task.py`、`tests/test_preflight.py`。
- 实现：
  1. `app/config.py` 增加 `intake_max_file_bytes`（单文件 10MB）、`intake_max_total_bytes`（总快照 100MB）与 `intake_min_free_disk_bytes`（磁盘下限 200MB）；
  2. `app/gitops/input_snapshot.py` 重构为有界流式复制与双布局策略：
     - V1 (`development`): 排除 `__pycache__`, `.pytest_cache`, `*.pyc`, `.git`；
     - V2 (`production`): 额外排除 `.venv`, `node_modules`, `*.pem`, `*.key`, `.env*` 等本机依赖与敏感密钥，显式保留依赖锁文件（`poetry.lock`, `requirements.txt`, `package-lock.json` 等）；
     - 复制遍历中流式检查单文件大小、文件数、总字节数、耗时及可用磁盘空间，边界处即时收敛抛出 `TaskError`（绝不先复制完再拒绝）；
     - 遇到符号链接或特殊文件即刻拒绝；
     - 边写边计算 SHA-256，校验写入前后字节一致性（检测源在复制中的并发漂移）；
     - 临时目录异常清理保证源目录零接触零写入，完成时原子 rename 替换；
  3. `app/task_spec.py` 的 `fingerprint_source_dir` 对齐 V1/V2 策略与指纹版本（`root_version` 为 1 或 2），与 `freeze_input` 保持严格同构；
  4. `app/api/service.py` 在任务创建时下发体量上限、磁盘下限与部署模式；
  5. `app/api/preflight.py` 在收集失败时明确前缀 `environment_not_ready`，禁止自动回落宿主或联网安装；
  6. `tests/test_input_snapshot.py` 新增单文件巨文件、总字节超限、磁盘下限、生产秘密排除/锁文件保留等定向测试。
- 验证：
  - 定向快照测试：`tests/test_input_snapshot.py` 14 项全过（0.25s）；
  - 任务规格与自定义仓库测试：`tests/test_task_spec.py` + `tests/test_custom_task.py` 38 项全过（57.82s）；
  - 受理预检测试：`tests/test_preflight.py` 8 项全过（10.06s）；
  - `verify.ps1` 收据 `.validation/20261016-002756-b231a1ac.json`（exit 0，6 个测试文件全部通过，耗时 38.0s）；
  - 81 个未变动测试 deferred to CI。
### P11 — 持久队列、独立 Worker 与幂等提交 (2026-10-16)

- 目标与范围：
  - `app/api/{service,app}.py`、`app/storage/{repository,locks}.py`、`app/config.py`、`app/worker/`、`docker/docker-compose.yml`；
  - tests：`tests/test_jobs_queue.py`、`tests/test_worker.py`。
- 实现：
  1. `app/config.py` 增加 `worker_enabled` (默认 True)、`job_lease_duration_seconds` (60.0s) 与 `job_poll_interval_seconds` (1.0s)；
  2. `app/storage/repository.py` 实现作业持久队列与生命周期管理：`enqueue_job`、`get_job`、`claim_next_job`、`renew_job_lease`、`complete_job` 与单事务原子提交 `submit_task_atomic`（合并额度校验与扣减、任务创建、契约双写与作业入队，依托 `uq_tasks_active_principal_idem` 防竞态重入）；
  3. 新增 `SingleInstanceLock`（`app/storage/locks.py`），跨平台文件锁（Windows msvcrt / Unix flock）保护生产模式 API 与 Worker 进程单实例独占；
  4. 新增独立 Worker 进程 `Worker`（`app/worker/worker.py`），持有进程锁、持久队列轮询、后台守护线程周期性续期租约心跳，驱动 `TaskService.execute_task_from_job` 闭环执行；
  5. 拆分 `docker/docker-compose.yml`：API 容器不持 Docker socket 且 `PATCHPILOT_WORKER_ENABLED: "false"`，执行容器挂载 Docker socket 运行 Worker。
- 验证：
  - 定向队列测试：`tests/test_jobs_queue.py` 7 项全过；
  - Worker 与单实例测试：`tests/test_worker.py` 6 项全过；
  - `verify.ps1` 收据 `.validation/20261016-020919-c30b0464.json`（exit 0，60 项通过，耗时 77.3s）；
- Commit：
  - P11-1: `91c59b3` (`feat(queue): persistent enqueue, job management and atomic submission (P11-1)`)；
  - P11-2 & P11-3: `acf5e97` (`feat(worker): standalone worker process, heartbeat lease, and deployment separation (P11-2, P11-3)`）。

### P12 — 租约、恢复、Fencing Token 与状态写入守卫 (2026-10-16)

- 目标与范围：
  - `app/storage/repository.py`、`app/worker/worker.py`、`app/api/service.py`、`tests/test_worker_recovery.py`。
- 实现：
  1. `claim_next_job` 递增 `fencing_token`；`renew_job_lease`、`complete_job`、`finalize_task`、`set_status_unless_terminal`、`settle_quota` 与 `release_quota` 全面接入 `fencing_token` 校验，旧 token 写入直接拒绝；
  2. 修复 `list_stale_running` 与 `recover_stale_running`：仅收敛失主僵尸任务（无 job 或租约已过期的 claimed 任务），对活 worker 持有效租约的任务及 pending 队列中的排队任务绝不误判死；
  3. `Worker` 心跳线程在检测到租约丢失时立即触发 `cancel_event` 协作式终止执行，防止失主僵尸 worker 持续计算或覆写新实例工件；
  4. `Repository` 内部锁升级为 `threading.RLock`，`submit_task_atomic` 委派 `create_task`，确保测试 hook 与嵌套事务正常工作。
- 验证：
  - 定向恢复与 Fencing 测试：`tests/test_worker_recovery.py` 5 项全过；
  - 健壮性与存储回归：`tests/test_service_robustness.py` + `tests/test_storage.py` 20 项全过；
  - `verify.ps1` 收据 `.validation/20261016-023003-1837b3b5.json`（exit 0，48 项全部通过，耗时 80.5s）；27 个未变动测试 deferred to CI。
- Commit：`04bc7cf` (`feat(recovery): lease fencing token, state transition guard, and stale job recovery (P12)`）。
- 下一张卡：P13（运行指标、健康检查与告警）。

### P13 — 运行指标、健康检查与可观测性 (2026-10-16)

- 目标与范围：
  - `app/logctx.py`、`app/config.py`、`app/storage/repository.py`、`app/observability/`、`app/api/auth.py`、`app/api/routes.py`；
  - tests：`tests/test_observability.py`。
- 实现：
  1. `app/logctx.py` 补充 `attempt_id_var`、`worker_id_var`、`actor_var` 与结构化 `JsonLogFormatter`；
  2. `app/config.py` 增加 `log_format` ("text" | "json") 与 `readiness_min_disk_free_bytes`；
  3. `app/storage/repository.py` 实现健康检查与统计聚合方法：`check_health`、`get_queue_stats`、`get_task_stats` 与 `get_quota_stats`；
  4. 新增 `app/observability/health.py`（存活探针 `/health/live` 与就绪探针 `/health/ready`）与 `app/observability/metrics.py`（低基数 Prometheus 指标导出器 `/metrics`）；
  5. 探针与指标接口在 API 认证中列入豁免名单。
- 验证：
  - 定向观测测试：`tests/test_observability.py` 59 项全过；收据 `.validation/20261016-024721-32c306e7.json`；
- Commit：`f58d4fb` (`feat(observability): liveness/readiness probes, metrics registry, and json logging (P13)`）。
- 下一张卡：P14（生命周期管理、只读化、归档与备份）。

### P14 — 生命周期管理、只读化、归档与备份 (2026-10-16)

- 目标与范围：
  - `app/storage/migrations.py`、`app/storage/repository.py`、`app/api/recycle.py`、`app/storage/backup.py`、`scripts/backup_restore.py`；
  - tests：`tests/test_recycle.py`、`tests/test_backup_restore.py`。
- 实现：
  1. P14-1：工件分级、Legal Hold 与回收：`tasks` 增加 `legal_hold` 字段；`app/api/recycle.py` 防范路径遍历与符号链接逃逸，阻断活跃租约/未终态/法务保全任务，保留 `report.json`、`trajectory.jsonl` 等核心审计事实；
  2. P14-2 & P14-3：热备份与安全恢复：`app/storage/backup.py` 利用 `sqlite3.Connection.backup` 实现一致性热备份，校验 SHA-256 与 `PRAGMA integrity_check`，恢复时强制目标目录为空除非显式指定 overwrite；新增 CLI `scripts/backup_restore.py`。
- 验证：
  - 定向回收与备份测试：`tests/test_recycle.py` 65 项全过，`tests/test_backup_restore.py` 6 项全过；收据 `.validation/20261016-025823-14de9cd3.json` 与 `.validation/20261016-030130-a1c97cbb.json`；
- Commit：
  - P14-1: `c848a02` (`feat(lifecycle): artifact classification, legal hold, and dry-run recycling (P14-1)`)；
  - P14-2 & P14-3: `3c4ce8a` (`feat(backup): consistent online SQLite hot backup, manifest reconciliation, and safe restore (P14-2, P14-3)`)。
- 下一张卡：P15（可复现构建约束与发布指南）。

### P15 — 可复现构建约束、生产依赖对齐与发布文档 (2026-10-16)

- 目标与范围：
  - `pyproject.toml`、`requirements.txt`、`constraints.lock`、`.github/workflows/ci.yml`、`.env.example`、`docs/build-and-release.md`；
  - tests：`tests/test_docs_anchors.py`。
- 实现：
  1. 将 `openai>=1.40` 纳入主生产依赖，生成离线可复现的完整 `constraints.lock`；
  2. 在 CI 中增加 Docker preflight 检查，并统一 `.env.example` 工业参数；
  3. 编写 `docs/build-and-release.md` 包含可复现构建与平滑回滚矩阵。
- 验证：
  - 文档锚点与静态检查：`tests/test_docs_anchors.py` 通过；收据 `.validation/20261016-030707-efdf5572.json`；
- Commit：`c0f8f53` (`feat(build): reproducible build constraints, production dependency alignment, and release guide (P15)`）。
- 下一张卡：P16（补丁审核与独立 CI 交付）。

### P16 — 补丁审核、独立 CI 与交付清单 (2026-10-16)

- 目标与范围：
  - `app/delivery/`、`app/storage/{migrations,repository}.py`、`app/api/{schemas,routes}.py`、`README.md`；
  - tests：`tests/test_delivery.py`。
- 实现：
  1. 新增 `app/delivery/bundle.py` 实现交付工件打包、`DeliveryManifest` 签名/哈希生成、交付就绪前提断言（任务已成功、人工审核已通过、独立 CI 已通过、无篡改）；严禁模型自身审核自身补丁；
  2. 数据库迁移新增 `delivery_records` 表，记录 review 决策、独立 CI 结果、交付时间与清单摘要；
  3. 在 `app/api/routes.py` 暴露 `/api/tasks/{task_id}/delivery`、`review`、`independent-ci` 与 `deliver` 端点。
- 验证：
  - 定向交付测试：`tests/test_delivery.py` 4 项全过，`tests/test_docs_anchors.py` 通过；收据 `.validation/20261016-031215-0a71c57b.json`；
- Commit：`0f979a3` (`feat(delivery): patch review bundle, independent CI status, and delivery manifest (P16)`）。
- 下一张卡：P17（正确性与对抗评测协议）。

### P17 — 正确性与对抗评测协议 (2026-10-16)

- 目标与范围：
  - `app/evals/{adversarial,experiment_manifest,report_evidence}.py`、`docs/eval-correctness-and-adversarial-protocol.md`、`bugs/attacks/` 新增独立样例、`README.md` 与 `docs/threat-model.md`；
  - tests：`tests/test_attacks.py`、`tests/test_docs_anchors.py`、`tests/test_experiment_manifest.py`。
- 实现：
  1. 新增 `docs/eval-correctness-and-adversarial-protocol.md`，冻结四层验收架构（Tier 1: 声明测试；Tier 2: 独立隐藏测试 holdout_tests；Tier 3: 恶意行为检测；Tier 4: 人工业务审查）以及 Python 同进程与 Docker 容器执行的形式化证明上限，严禁虚假宣传绝对数学正确性证明；
  2. 新增 `app/evals/adversarial.py`：实现 AST 与正则双重对抗审计 `audit_patch_adversarial`（覆盖框架篡改、吞异常、凭据探测、提示注入、死循环拒服），实现 `assert_holdout_tests_isolated` 确保隐藏测试绝不泄露给任务规格或提示词，实现 `MultiTierEvalReport`（正确检出 pseudo_green 伪绿、adversarial_rejected 与 verified）；
  3. 新增 5 道独立对抗样例：
     - `ATTACK-011-swallow-exception`: 吞异常与空 pass 抑制报错
     - `ATTACK-012-hardcode-test-case`: 硬编码测试用例字面值特判伪绿
     - `ATTACK-013-tamper-pytest-runtime`: 生产代码导入 pytest 试图篡改运行时
     - `ATTACK-014-secret-harvesting`: 业务代码中读取环境变量敏感凭据
     - `ATTACK-015-dos-resource-exhaustion`: 植入死循环导致资源耗尽
  4. 扩展 `app/evals/experiment_manifest.py` 支持可选 `holdout_tests` 与 `adversarial_checks`；在 `app/evals/report_evidence.py` 接入 `_adversarial_eval` 工件收集；
  5. 在 `tests/test_attacks.py` 中新增定向测试，验证 5 个对抗样例准确检出、BUG-001 ~ BUG-003 正向黄金补丁零误报、隐藏测试隔离保护及综合报告判定；同步更新 `README.md` 与 `docs/threat-model.md` 样例数（15 个），同时保留 10 个门禁样例锚点。
- 验证：
  - 对抗测试与文档锚点：`verify.ps1 -Tests tests/test_attacks.py tests/test_docs_anchors.py` 收据 `.validation/20261016-032556-73ebbc06.json`（exit 0，27 项测试全部通过，耗时 7.72s）；
  - 实验清单回归：`verify.ps1 -Tests tests/test_experiment_manifest.py` 收据 `.validation/20261016-032616-76c0a6d7.json`（exit 0，6 个测试文件全部通过，耗时 21.89s）；
- Commit：`7864618` (`feat(evals): correctness and adversarial evaluation protocol (P17)`）。
- 下一张卡：P18（目标仓库真实收益验证，离线准备）。

### P18 — 目标仓库真实收益验证 (离线准备) (2026-10-16)

- 目标与范围：
  - `scripts/{run_paid_batch,summarize_paid_batch,compare_experiments}.py`、`docs/work-notes/industrial-pilot-benchmark-manifest-2026-10-16.json`；
  - tests：`tests/test_experiment_manifest.py`、`tests/test_docs_anchors.py`。
- 实现：
  1. 新增 `docs/work-notes/industrial-pilot-benchmark-manifest-2026-10-16.json`，编制试点 benchmark 离线清单（覆盖简单、中等、困难与多文件题面，两臂对比，2 次重复，共 20 次执行计划，停手条件与零网络预算）；
  2. 在 `scripts/run_paid_batch.py` 中支持自动挂载项目根路径，完善 `--dry-run` 离线校验与预算封包核算，保证零网络、零外部请求；
  3. 在 `scripts/summarize_paid_batch.py` 中增加采纳率（accepted patch rate）、每采纳补丁成本（cost per accepted patch）、Token p50/p95 统计及分母保留的失败分类分布（环境失败/门禁拦截/超时等不挑跑）；
  4. 在 `scripts/compare_experiments.py` 中将交付与评测结果字段纳入 `OBSERVED_FIELDS`，防范合法结果差异被误判为配置漂移；
  5. 遵循授权边界：不自动调用真实付费模型 API，未授权阶段严格停在 `READY_FOR_AUTHORIZATION`。
- 验证：
  - 离线 dry-run 测试：`scripts/run_paid_batch.py --manifest ... --dry-run` 退出码 0，成功展开 20 次计划运行，请求数=0，费用=0；
  - 静态与清单验证：`verify.ps1 -Tests tests/test_experiment_manifest.py tests/test_docs_anchors.py` 收据 `.validation/20261016-033118-8b894a85.json`（exit 0，耗时 1.15s）；
- Commit：`a87146d` (`feat(evals): offline dry-run framework and benchmark manifest for pilot evaluation (P18)`）。
- 下一张卡：P19（故障、负载与安全集成验收）。

### P19 — 故障、负载与安全集成验收 (2026-10-16)

- 目标与范围：
  - `tests/test_production_boundaries.py`、`scripts/drill_production_boundaries.py`、`app/storage/repository.py`；
  - 覆盖 SPEC §8 生产边界验收矩阵关键项：
    - A01: 生产模式前置准入校验拦截；
    - A02: 跨主体资源访问强隔离（绝不泄露任务存在性，严格 404）；
    - A08: 配额原子并发争抢与防超发（严格按限额截断，不超发、不双扣）；
    - A10: Fencing token 阻断过期 worker 迟到写入；
    - A11: 存储与就绪探针真实状态映射；
    - A17: 受理背压、有界排队与紧急停发机制。
- 实现：
  1. 新增 `tests/test_production_boundaries.py` 自动化集成测试套件，覆盖 A01、A02、A08、A10、A11、A17 全链路断言；
  2. 新增 `scripts/drill_production_boundaries.py` CLI 演练脚本，支持一键或分场景（admission/fencing/quota/readiness）复现并验证生产边界；
  3. 在 `app/storage/repository.py` 为 `Repository` 增加 `close()` 与上下文管理器 `__enter__` / `__exit__`，防止 Windows 平台临时 SQLite 数据库句柄残留引发的文件锁冲突。
- 验证：
  - 演练脚本：`python scripts/drill_production_boundaries.py --all` 退出码 0，全部 4 项生产演练通过；
  - 快速验证：`verify.ps1 -Tests tests/test_production_boundaries.py tests/test_docs_anchors.py` 收据 `.validation/20261016-034320-f3f0023c.json`（exit 0，6 项边界测试全通，耗时 1.54s）；
- Commit：`090b741` (`test(integration): production boundaries, failure recovery, and load limits (P19)`）。
- 下一张卡：P20（试点发布、值守与退出）。

### P20 — 试点发布、值守与退出 (2026-10-16)

- 目标与范围：
  - `docs/runbook.md`、`docs/pilot-release-evidence.md`、`README.md`、`docs/README.md`；
  - 覆盖试点部署架构、启停管理、健康探针巡检、SOP 应急处置与平滑回滚全流程。
- 实现：
  1. 新增 `docs/runbook.md` 生产试点运维值守与应急手册：详细定义单团队单控制实例单 Worker 拓扑、环境变量基准、日常健康与指标巡检、五大应急处置预案（紧急停发 SOP-01、Worker 失联恢复 SOP-02、敏感凭据吊销 SOP-03、一致性热备灾备 SOP-04、平滑回滚 SOP）、值守与升级响应责任矩阵；
  2. 新增 `docs/pilot-release-evidence.md` 受控生产试点发布证据与生产准入记录：完整汇编 P00 ~ P20 交付事实源、Schema V3 迁移版本、Python 3.11 与 `constraints.lock` 供应链锁、SPEC §8 生产边界实测结果与对抗评测 100% 拦截记录、诚实声明残余风险与业务试点退出条件；
  3. 更新 `README.md` 与 `docs/README.md` 索引入口，使运维与发布文档对账可追溯。
- 验证：
  - 锚点与文档一致性：`tests/test_docs_anchors.py` 全部通过；
  - 验证所有相对链接真实有效（`test_markdown_links_point_at_existing_files` exit 0）；
- Commit：`44ee8ea` (`docs(release): pilot deployment runbook, operations guide, and release evidence (P20)`）。
- 下一张卡：P21（可选多实例与多租户扩展，非首版必做，按需求触发）。

## 任务状态

| 卡 | 状态 | 依赖 | owner / commit / 收据 |
|---|---|---|---|
| P00 | DONE | 无 | 本卡 / 8b64907 / .validation/20261015-231752-36018573.json |
| P01 | DONE | P00 | 本卡 / 5186085 / .validation/20261015-232638-318cd0d5.json + targeted 16 passed |
| P02 | DONE | P01 | 本卡 / e8a67b1 / .validation/20261015-233538-601dd7a5.json (11 passed) |
| P03 | DONE | P02 | 本卡 / ffab2de / .validation/20261015-235932-4b35cd29.json + targeted 15 passed |
| P04 | DONE | P00/P02 | 本卡 / 3af075b / .validation/20261016-001717-09ebcad3.json (60 passed) |
| P05 | DONE | P00/P04 | 本卡 / 8d06145 / .validation/20261016-002756-b231a1ac.json (60 passed) |
| P06 | DONE | P03/P05 | P06-1: 3389805 / P06-2: 0746100 / .validation/20261016-010419-54dfb5ff.json (60 passed) |
| P07 | DONE | P00 | 本卡 / 01b4bf7 / .validation/20261015-234428-ddb46fe6.json (35 passed) |
| P08 | DONE | P04/P07 | 本卡 / ecc9f93 / .validation/20261016-011317-0639e315.json (60 passed) |
| P09 | DONE | P07/P08 | 本卡 / 7586b4a / .validation/20261016-012135-de3a1ae4.json (6 passed) |
| P10 | DONE | P01/P03/P04 | P10-1: fd3ba1d / P10-2: fed5cfa / .validation/20261016-014602-63fd1878.json (63 passed) |
| P11 | DONE | P01/P03/P04/P09/P10 | P11-1: 91c59b3 / P11-2, P11-3: acf5e97 / .validation/20261016-020919-c30b0464.json (60 passed) |
| P12 | DONE | P11 | 本卡 / 04bc7cf / .validation/20261016-023003-1837b3b5.json (48 passed) |
| P13 | DONE | P04/P11/P12 | 本卡 / f58d4fb / .validation/20261016-024721-32c306e7.json (59 passed) |
| P14 | DONE | P01/P03/P06/P11/P12 | P14-1: c848a02 / P14-2, P14-3: 3c4ce8a / .validation/20261016-030130-a1c97cbb.json (71 passed) |
| P15 | DONE | P00 | 本卡 / c0f8f53 / .validation/20261016-030707-efdf5572.json (10 passed) |
| P16 | DONE | P03/P06/P11/P12/P15 | 本卡 / 0f979a3 / .validation/20261016-031215-0a71c57b.json (14 passed) |
| P17 | DONE | P00/P06/P08/P09 | 本卡 / 7864618 / .validation/20261016-032556-73ebbc06.json (27 passed) |
| P18 | READY_FOR_AUTHORIZATION | P15/P16/P17 | 本卡 / a87146d / .validation/20261016-033118-8b894a85.json (dry-run 20 runs) |
| P19 | DONE | P05–P15/P17 | 本卡 / 090b741 / .validation/20261016-034320-f3f0023c.json (6 passed + drill 4/4) |
| P20 | DONE | P00–P19 | 本卡 / 44ee8ea / docs/runbook.md + docs/pilot-release-evidence.md |
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
