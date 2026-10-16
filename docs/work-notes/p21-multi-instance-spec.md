# PatchPilot P21 规格说明书 — 可选多实例与多租户扩展 (Multi-Instance & Multi-Tenant Expansion SPEC)

**文档状态**：DRAFT / SPECIFICATION  
**优先级**：P2（企业级扩展，按需触发）  
**前序依赖**：P00 ~ P20 全部验收完成（已达成）  
**关联规划**：`docs/work-notes/industrialization-plan-2026-10-15.md` §5 P21  

---

## 1. 背景、触发条件与核心目标

### 1.1 现状与演进驱动
在 P00 ~ P20 建设完成后，PatchPilot 已具备受控单团队生产试点的全套能力（Schema V3、RBAC、Fencing Token、原子配额结算、独立 Worker 进程、Docker 硬化沙箱、热备灾备与自动化演练）。
当业务接入规模从单一研发小组扩展至**跨团队/多租户（Multi-Tenant）**，或单机吞吐量与可用性出现瓶颈时，需要平滑升级至多实例与多租户分布式架构。

### 1.2 启动触发条件（准入准则）
严格禁止以“工业级必须上复杂分布式/Kubernetes”为由盲目开工。必须满足下列实测事实之一方可启动实施：
1. **并发容量瓶颈**：单 Worker 进程单机并发执行导致任务平均排队等待时间超过 SLA（如 p95 超过 15 分钟）；
2. **高可用性（HA）需求**：业务方明确要求控制面具备 Active-Active 冗余，控制实例故障时 RTO < 30 秒无感切换；
3. **严格组织多租户需求**：多个业务线共用集群，要求数据、配额、计费、密钥与工件实现严格租户隔离。

### 1.3 核心继承不变量（Invariants）
无论架构如何扩展，以下语义与安全规范绝对不变：
1. **合取判定唯一性**：`app/graph/acceptance.py` 的显式合取逻辑为修复成功（`resolved`）的唯一裁决准则；
2. **质量门禁不放宽**：语法检查、测试行为、安全扫描等门禁拦截规则绝不退化；
3. **沙箱边界硬化**：所有执行单元必须在无特权、无外网、只读根文件系统与 tmpfs 隔离容器内运行；
4. **单次提交与代码体量**：每次实现代码保持在 300 行约束以内，测试驱动。

---

## 2. 架构拓扑演进

```
                       [ Load Balancer / Ingress (TLS Termination) ]
                                      |
                 +--------------------+--------------------+
                 |                                         |
     [ API Control Instance 1 ]                [ API Control Instance 2 ]
     (Stateless, Admission & RBAC)             (Stateless, Admission & RBAC)
                 |                                         |
                 +--------------------+--------------------+
                                      |
        +-----------------------------+-----------------------------+
        |                             |                             |
[ PostgreSQL HA Cluster ]    [ Distributed Lock / Redis ]   [ S3 / MinIO Object Store ]
(Tasks, Quotas, Audit Logs,  (Lease Coordination, Fencing   (Reports, Trajectories,
 Schema V4 Forward Migrated)  Tokens & Heartbeats)           Patches, Legal Hold)
        |                             |                             |
        +-----------------------------+-----------------------------+
                                      |
                 +--------------------+--------------------+
                 |                                         |
        [ Worker Node 1 ]                         [ Worker Node 2 ]
      (Docker Sandbox Pool)                     (Docker Sandbox Pool)
```

---

## 3. 子任务分卡分解（P21-1 至 P21-6）

### P21-1: 关系存储适配器与向后兼容迁移 (PostgreSQL Backend)
- **目标**：实现统一存储抽象 `StorageBackend`，支持 SQLite 与 PostgreSQL 自动切换，完成 Schema V4 向前迁移。
- **关键设计**：
  - 提取 `app/storage/backend.py` 抽象基类，保留现有 `Repository` API 签名；
  - 引入连接池（如 `psycopg` / `asyncpg` 连接池），支持毫秒级重试与死锁断开恢复；
  - 编写迁移脚本 `scripts/migrate_sqlite_to_postgres.py`，支持离线一致性数据校验与双写校验；
  - 本地测试与 CI 继续默认使用 SQLite 内存/临时文件，保障日常 `verify.ps1` 极速通过。

### P21-2: 分布式作业分发与强一致性租约管理器 (Distributed Jobs & Fencing)
- **目标**：支持跨节点多 Worker 并发安全抢单，杜绝双花计算与脑裂迟到写。
- **关键设计**：
  - 作业领取升级为 `SELECT ... FOR UPDATE SKIP LOCKED` 或 Redis Streams 消费组；
  - 租约生成全局单调递增 `fencing_token`；Worker 在心跳与写入数据库/对象存储时必须附带最新 token；
  - 存储层更新增加乐观锁前置条件（`WHERE fencing_token = :current_token`），任何旧 token 的写操作直接抛出 `StaleLeaseError` 并阻断。

### P21-3: 分布式对象存储与工件保全服务 (S3 / MinIO Artifact Storage)
- **目标**：解耦本地文件系统 `runs_root`，所有取证工件上传至分布式高可用对象存储。
- **关键设计**：
  - 实现 `ObjectStoreClient`，封装 `put_artifact`、`get_artifact_stream`、`presign_download_url`；
  - 工件落盘计算并绑定 SHA-256 校验和，写入数据库 `artifacts` 索引表；
  - 深度集成法务保全（`legal_hold`）：若任务处于保全状态，对象存储与数据库级均禁止生命周期回收策略清除；
  - 敏感取证事实（内部 CoT、未脱敏日志）与公开交付工件（`sanitized`）实行分桶或前缀严格 ACL 隔离。

### P21-4: 多租户隔离模型与组织级配额信封 (Multi-Tenancy & Quota Envelopes)
- **目标**：实现组织与租户（Org/Tenant）逻辑与数据强隔离，防止跨租户越权。
- **关键设计**：
  - 核心表（`tasks`、`principals`、`quotas`、`audit_logs`）增加 `tenant_id` 索引字段；
  - 请求上下文自动挂载 `tenant_id`，查询自动追加租户过滤；跨租户访问请求统一返回 404（不泄露任务存在性）；
  - 配额信封支持租户级配额池与主体级配额池二级联动，扣减必须维持原子事务；
  - 容器执行环境按租户分配独立命名空间（Network Namespace）或专用 Worker 节点标签。

### P21-5: 双控制实例 Active-Active 容灾与零停机滚动升级 (HA & Rolling Upgrades)
- **目标**：API 控制面实现无状态横向伸缩与平滑重启。
- **关键设计**：
  - 完善 `/health/live` 与 `/health/ready` 探针（纳入 PostgreSQL 连接池就绪、对象存储心跳与租约管理器连接）；
  - 数据库迁移采用“向前兼容两阶段迁移”（Expand-Contract 模式）：
    - 阶段一（Expand）：新增可空字段或新表，旧版 API 与新版 API 均可安全读写；
    - 阶段二：滚动更新 API 与 Worker 节点；
    - 阶段三（Contract）：清理废弃列与兼容代码；
  - 确保任一 API 实例崩溃退出时，客户端重试可无缝被对端 API 承接。

### P21-6: 分布式混沌测试与多租户安全验收套件 (Chaos & Security Acceptance)
- **目标**：建立完备的自动化演练与攻击测试，形成客观验收收据。
- **必测场景**：
  1. **跨租户逃逸拦截**：模拟恶意主体使用合法 Token 尝试读取/篡改其他租户的 Task、工件与配额，确保 100% 拦截并审计告警；
  2. **Worker 脑裂与迟到写（Split-Brain Late-Write）**：模拟 Worker-A 因网络抖动丢失租约，Worker-B 抢单成功并更新结果，随后 Worker-A 恢复并尝试回写，断言 Worker-A 写入被拒且不破坏 Worker-B 数据；
  3. **并发抢占无超发（Atomic Settlement）**：高并发压测租户配额与队列，断言绝不产生双扣、超发或死锁；
  4. **Active-Active 节点瞬断**：测试过程中强行 kill 实例 1，断言实例 2 接管并完成所有在途任务，端到端结果一致。

---

## 4. 实施纪律与非目标（Non-Goals）

1. **非目标**：
   - 本卡不引入微服务网格（如 Istio/Envoy Sidecar）或复杂的外部 RPC 框架，保持轻量高效；
   - 本卡不改变任何本地开发的验证节奏，`verify.ps1` 仍保持 <90 秒预算和离线执行；
2. **实施前置**：
   - 必须由团队负责人批准实施计划，且正式生产环境已具备 PostgreSQL 与 MinIO/S3 基础设施资源。
