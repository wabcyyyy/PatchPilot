# PatchPilot 企业内网受控生产试点发布证据 (P20 Release Evidence)

本文档是 PatchPilot 工业化升级（SPEC 2026-10-15，P00 ~ P20）的最终发布凭据与生产准入证明。

---

## 1. 发布基线与供应链身份

- **发布范围**：企业内网受控生产试点（单团队、单控制实例 API、单 Worker、无跨租户共享）；
- **基线 Commit**：`090b741`（P19 生产边界与故障恢复集成验收完成）；
- **运行时环境**：Python 3.11.x（`python:3.11-slim`）；
- **依赖固化事实源**：
  - 声明文件：`pyproject.toml`
  - 生产依赖入口：`requirements.txt`
  - 开发测试入口：`requirements-dev.txt`
  - 传递依赖锁：`constraints.lock`（全量锁定直接与间接依赖精确版本）
- **数据库 Schema 契约**：
  - 数据库引擎：SQLite 3（WAL 模式 + 强外键约束 + 单实例进程排他锁）
  - Schema 版本：V3（由 `schema_migrations` 表显式版本控制，禁止破坏性回滚）
  - 关键数据表：`tasks`, `principals`, `credentials`, `audit_logs`, `quota_reservations`, `jobs`, `delivery_records`, `retention_legal_holds`

---

## 2. 工业化架构与模块交付全景 (P00 ~ P20)

| 批次与卡号 | 核心交付物 | 质量门禁与安全验证 | 证据与收据 |
| :--- | :--- | :--- | :--- |
| **P00 ~ P02 身份与契约** | 工作负载清单、向前迁移器、主体与凭据模型 | 显式 Schema 版本控制、Token 派生加盐哈希存储 | 单元测试全通、向前迁移通过 |
| **P03 ~ P06 权限与边界** | 资源 RBAC、生产准入 Preflight、只读快照摄入、数据脱敏 | 跨主体越权严格 404、本地执行生产强拦截、原始凭据清洗 | `test_resource_authorization.py`, `test_data_policy.py` |
| **P07 ~ P09 执行与沙箱** | 有界输出管道、Docker 沙箱加固、容器生命周期收割 | 非 root `1000:1000`、cap drop ALL、tmpfs、单任务收割 | `test_bounded_streams.py`, `test_container_lifecycle.py` |
| **P10 ~ P12 调度与状态** | 准入背压控制器、原子配额扣减、持久队列、Worker 租约 | 队列超限拒绝、配额原子防超扣、Fencing Token 防迟到写 | `test_quota_settlement.py`, `test_jobs_queue.py`, `test_worker.py` |
| **P13 ~ P14 运维与灾备** | 探针与 JSON 日志、工件分类与法律保留、一致性热备与恢复 | readiness 探针、180天取证保留、SQLite 热备 SHA256 校验 | `test_observability.py`, `test_recycle.py`, `test_backup_restore.py` |
| **P15 ~ P16 供应链与交付** | 约束锁定、生产指南、补丁审查清单、独立 CI 解耦端点 | 严禁模型自审、人工审核前置、签名清单防篡改 | `test_delivery.py`, `docs/build-and-release.md` |
| **P17 ~ P18 评测与基准** | 四层对抗评测协议、15道对抗攻击拦截、离线 dry-run 清单 | 15个对抗攻击 100% 拦截、20次离线规划运行 0 外部网络 0 费用 | `test_attacks.py`, `industrial-pilot-benchmark-manifest-2026-10-16.json` |
| **P19 ~ P20 边界验收与发布** | 生产边界集成测试矩阵、故障演练 CLI、运维 Runbook | A01/A02/A08/A10/A11/A17 矩阵测试通过、演练脚本退出码 0 | `test_production_boundaries.py`, `docs/runbook.md` |

---

## 3. 生产准入核心证据与实测结果

### 3.1 跨卡生产边界矩阵 (SPEC §8)
自动化集成套件 `tests/test_production_boundaries.py` 及演练工具 `scripts/drill_production_boundaries.py` 验证结论：
1. **A01 生产模式前置拦截**：本地执行后端在 `deployment_mode="production"` 下被立即拒绝，无模型调用发生；
2. **A02 跨主体隔离**：不同主体间的任务、报告、轨迹与取消请求严格返回 404（`code: "invalid_task"`），绝不泄露资源存在性；
3. **A08 配额并发争抢**：多线程并发预留配额时，严格按日限额精确截断（恰好 3 个成功，2 个超限拒绝），总持有额度分毫不差；
4. **A10 租约与 Fencing Token 防篡改**：Worker 1 租约过期后由 Worker 2 抢占，Worker 1 随后迟到提交的结果被数据库基于 Token 不匹配坚决拒绝；
5. **A11 就绪探针状态映射**：磁盘空间不足时，就绪探针即刻由 `ready` 切换为 `unready`，且详情绝不包含敏感系统凭据；
6. **A17 有界排队与紧急停发**：启用紧急停发时，新任务受理立即抛出 `EmergencyStopError`；并发排队饱和后立即返回 `AdmissionQueueFullError` 背压拒绝。

### 3.2 对抗与安全性证据
- **四层评测体系 (Tier 1~4)**：声明测试、隐藏测试、对抗审计与人工审查解耦，严禁虚假证明；
- **攻击样例拦截**：`bugs/attacks/` 共 15 个独立攻击样例（覆盖框架篡改、吞异常、特判伪绿、敏感凭据窃取、DoS 资源耗尽），在 `tests/test_attacks.py` 中 100% 检出拦截，正向黄金补丁零误报。

---

## 4. 诚实边界与已知残余风险 (Honest Boundaries)

按 AGENTS 规范与 SPEC 要求，在此如实记录未验证项与受控限制：
1. **模型端点授权状态**：当前生产试点基线处于 `READY_FOR_AUTHORIZATION` 阶段，严格未调用外部商业付费模型 API，所有离线基准测试均通过 `FakeLLM` 和 dry-run 封包核算验证；
2. **独立 CI 依赖解耦**：系统内嵌 `/api/tasks/{task_id}/independent-ci` 回填与交付门禁，交付包已生成可验证 SHA256 清单；真实生产外部 PR 自动合并需在获得具体仓库特权审批后方可开启；
3. **单实例容量上限**：当前架构严格基于单控制节点与单 Worker 设计，单实例推荐上限并发为 4，队列容量 20；若未来业务容量持续超限，应依据 P21 独立立项扩展，禁止在本次试点中无序扩张。

---

## 5. 试点准入签署与值守确认

- **支持范围**：企业内网指定白名单仓库，且仅限 Python 3.11 测试框架项目；
- **回滚保障**：现场具备基于 SQLite 在线热备份 SOP 与回滚手册（`docs/runbook.md`）；
- **准入结论**：**准予在受控内网环境中启动首批小规模单团队生产试点。**
