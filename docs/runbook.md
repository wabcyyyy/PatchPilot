# PatchPilot 生产试点运维值守与应急手册 (P20 Runbook)

本文档面向 PatchPilot 受控生产试点（单团队、单控制实例 API、单 Worker）的系统运维人员与值守工程师。

---

## 1. 生产架构与组件清单

```
                       +-----------------------------+
                       |   Nginx / API Gateway       |
                       +--------------+--------------+
                                      | (Bearer Token / API Key)
                                      v
                       +-----------------------------+
                       |   PatchPilot API (FastAPI)  | <--- SingleInstanceLock (api.lock)
                       +-------+--------------+------+
                               |              |
                    (Enqueue)  |              | (Readiness / Metrics)
                               v              v
                       +-----------------------------+
                       | SQLite (WAL) + Jobs Queue   |
                       +-------+---------------------+
                               ^
                    (Claim Job | Heartbeat Lease | Fencing Token)
                               |
                       +-------+---------------------+
                       |  PatchPilot Worker Process  | <--- SingleInstanceLock (worker.lock)
                       +--------------+--------------+
                                      |
                      (Isolated exec) | tmpfs + non-root 1000 + cap drop
                                      v
                       +-----------------------------+
                       | Docker Sandbox (Executor)   |
                       +-----------------------------+
```

### 关键约束
- **部署模式**：`PATCHPILOT_DEPLOYMENT_MODE=production`
- **执行隔离**：必须使用 Docker 执行器（`execution_backend=docker`），严禁本地直接执行代码
- **单实例运行**：API 与 Worker 各自使用文件锁保证同一时刻单实例运行，避免并发写冲突

---

## 2. 环境变量配置基准

生产部署 `.env` 必须包含以下配置：

```bash
# 运行模式
PATCHPILOT_DEPLOYMENT_MODE=production
PATCHPILOT_EXECUTION_BACKEND=docker
PATCHPILOT_ALLOWED_REPO_ROOTS=/data/repos
PATCHPILOT_DOCKER_IMAGE=patchpilot-executor:latest
PATCHPILOT_TRUSTED_DOCKER_IMAGES=patchpilot-executor:latest

# 身份与鉴权 (严禁空 Token)
PATCHPILOT_API_TOKEN=prod_secret_token_change_me_immediately

# 进程角色划分 (API 禁用内嵌 worker)
PATCHPILOT_WORKER_ENABLED=false

# 准入背压与配额熔断
PATCHPILOT_ADMISSION_CONCURRENCY_LIMIT=4
PATCHPILOT_ADMISSION_QUEUE_LIMIT=20
PATCHPILOT_PRINCIPAL_DAILY_BUDGET_USD=20.0
PATCHPILOT_PRINCIPAL_DAILY_TOKEN_BUDGET=500000
PATCHPILOT_TASK_BUDGET_CAP_USD=5.0
PATCHPILOT_EMERGENCY_STOP=false

# 可观测性与合规
PATCHPILOT_LOG_FORMAT=json
PATCHPILOT_LOG_LEVEL=INFO
PATCHPILOT_DATA_EXPORT_POLICY=mask_strict
PATCHPILOT_READINESS_MIN_DISK_FREE_BYTES=104857600
```

---

## 3. 服务日常启停标准流程

### 3.1 启动顺序
1. **检查宿主环境**：确认 Docker 守护进程运行正常，且沙箱镜像已构建完毕：
   ```bash
   docker image inspect patchpilot-executor:latest > /dev/null
   ```
2. **启动 API 服务**：
   ```bash
   python -m uvicorn app.api.app:create_app --host 0.0.0.0 --port 8000
   ```
3. **验证 API 就绪状态**：
   ```bash
   curl -s -f http://127.0.0.1:8000/api/health/ready | grep '"status":"ready"'
   ```
4. **启动独立 Worker**：
   ```bash
   python -m app.worker.cli --worker-id worker-prod-01 --db-path patchpilot.sqlite3 --poll-interval 2.0
   ```

### 3.2 优雅停机顺序
1. **停 Worker**：向 Worker 发送 `SIGINT` (Ctrl+C) 或 `SIGTERM`，Worker 将停止领取新 Job，并在当前回合并发点释放租约；
2. **停 API**：向 API 进程发送 `SIGTERM`，ASGI Lifespan 自动注销资源并释放文件锁。

---

## 4. 监控、巡检与告警指标

| 监控项 | 访问入口 | 正常标准 | 异常处理动作 |
| :--- | :--- | :--- | :--- |
| **存活探针** | `GET /api/health/live` | HTTP 200 `status: alive` | 若超时或挂死，由 systemd/容器自动拉起 |
| **就绪探针** | `GET /api/health/ready` | HTTP 200 `status: ready` | 检查 DB 锁、工件盘剩余空间是否 `<100MB` |
| **队列堆积** | `GET /api/metrics` | `pending_jobs <= 10` | 若积压严重，检查 Worker 存活状态与租约 |
| **异常扣费** | `GET /api/metrics` | 单主体用量 `<` 日限额 | 达到 80% 发出预警，触顶由配额系统自动拦截 |
| **审计日志** | 进程标准输出 (JSON) | 包含 `task_id`、`request_id` | 审查是否存在跨主体 404 越权探测记录 |

---

## 5. 应急故障处置预案 (SOP)

### SOP-01: 紧急停发 (Emergency Stop)
- **触发场景**：出现大规模异常补丁、模型供应商服务故障或安全警报。
- **操作**：
  1. 修改 `.env` 或环境：`PATCHPILOT_EMERGENCY_STOP=true`；
  2. 重载 API 进程；
  3. API 即刻对所有新任务受理请求返回 HTTP 503 `EmergencyStopError`；
  4. 已在途任务在当前 turn 结束后自然停止。

### SOP-02: Worker 失联与僵尸任务恢复
- **触发场景**：Worker 进程宿主机 OOM 或断网，任务卡在 `running`。
- **机制保障**：任务携带带租约时间戳与 Fencing Token。
- **处置**：
  1. 租约到期后（默认 60s），重新拉起的 Worker 会自动重新抢占任务并使 `fencing_token` 递增；
  2. 旧 Worker 若滞后唤醒，其提交操作将因 Fencing Token 不匹配被数据库强行拒绝，绝不双写。

### SOP-03: 敏感信息泄漏应急撤销
- **操作**：立即废除受影响 API Key：
  ```bash
  python -m app.agent.cli admin revoke-key --db-path patchpilot.sqlite3 --key-id <KEY_ID>
  ```
  凭据即刻失效，拒绝后续请求。

### SOP-04: 数据一致性备份与恢复
- **在线热备份**（不中断服务）：
  ```bash
  python scripts/backup_restore.py backup --db-path patchpilot.sqlite3 --target /data/backups/
  ```
- **校验备份包完整性**：
  ```bash
  python scripts/backup_restore.py verify --source /data/backups/backup_20261016_xxxxxx/
  ```
- **冷灾难恢复**（目标库必须为空或指定 `--overwrite`）：
  ```bash
  python scripts/backup_restore.py restore --source /data/backups/backup_20261016_xxxxxx/ --target-db patchpilot.sqlite3 --overwrite
  ```

---

## 6. 平滑回滚标准规范 (Rollback SOP)

> [!CAUTION]
> 严禁在有在途任务或未确认额度时直接替换容器镜像。严禁执行不可逆的破坏性 DROP 操作。

1. **第 1 步：切断新流量**：设置 `PATCHPILOT_EMERGENCY_STOP=true`；
2. **第 2 步：排空在途**：停止 Worker，确认 `jobs` 表中无 `running` 状态作业；
3. **第 3 步：制作现场备份**：执行 `scripts/backup_restore.py backup` 保全证据；
4. **第 4 步：镜像回退**：将容器镜像切回上一个通过准入测试的 stable digest；
5. **第 5 步：探针验证与恢复**：检查 `/api/health/ready` 返回 200 后解除停发。

---

## 7. 值守责任与升级响应

- **值守角色**：工业化试点运维工程师（负责日常探针巡检、备份演练与紧急停发）；
- **研发梯队**：PatchPilot 核心安全与评测团队（负责对抗攻击拦截分析、四层评测报告复核）；
- **告警升级通道**：内网 Webhook 机器人及短信通知通道；
- **响应时限**：P0 故障（数据库损坏/严重越权）15 分钟内响应并紧急停发；P1 故障（Worker 挂起/配额超限）30 分钟内恢复。
