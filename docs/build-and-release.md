# 可复现构建与供应链发布指南 (P15)

本文档定义 PatchPilot 企业内网受控生产试点的构建、依赖锁定、镜像发布与回滚兼容矩阵规范。

## 1. 运行时与依赖声明

- **目标运行时**：Python 3.11 (与 Dockerfile `python:3.11-slim` 和 `ruff` target 严格对齐)；
- **声明事实源**：`pyproject.toml` 是项目依赖的唯一声明源；
- **生产与开发分离**：
  - 运行依赖：`[project] dependencies` (`fastapi`, `uvicorn`, `pydantic`, `langgraph`, `redis`, `openai` 等)；
  - 开发依赖：`[dependency-groups] dev` (`pytest`, `ruff`, `httpx` 等)，`uv sync` 默认安装；
  - 一致性门禁：由 `tests/test_docs_anchors.py::test_uv_lock_is_tracked_and_in_sync` 保证锁与声明同步。
- **依赖版本锁**：`uv.lock` 由 uv 解析并固化所有直接与传递依赖的确切版本，CI 与镜像以
  `uv sync --frozen` 安装，防止上游悄悄升级导致行为漂移（2026-10-16 起替代原
  `requirements.txt`/`requirements-dev.txt`/`constraints.lock` 入口；`constraints.lock`
  保留为 P15 当时的 pin 快照）。

## 2. Docker 镜像构建与 Digest 追溯

生产部署必须基于确定构建上下文制作容器镜像，并通过不可变 SHA-256 Digest 引用：

### 2.1 API 与 Worker 镜像

```bash
# 构建 API/Worker 镜像
docker build -t patchpilot-api:v0.1.0 -f docker/api.Dockerfile .
# 查看镜像 digest
docker image inspect --format='{{index .RepoDigests 0}}' patchpilot-api:v0.1.0
```

### 2.2 Executor 沙箱镜像

```bash
# 构建测试执行器沙箱镜像 (无外部网络、非 root 用户 1000 运行)
docker build -t patchpilot-executor:latest -f docker/executor.Dockerfile docker/
```

在生产环境中，`PATCHPILOT_TRUSTED_DOCKER_IMAGES` 必须精确声明允许拉取或启动的受信任镜像列表。

## 3. 回滚兼容矩阵 (Rollback Compatibility Matrix)

| 组件 | 版本回滚前置动作 | Schema 兼容性 | 工件影响 |
| :--- | :--- | :--- | :--- |
| **API 服务** | 置 `PATCHPILOT_EMERGENCY_STOP=true` 停接新单 | Schema V2/V3 向后兼容，旧版可读取新版表中的历史列 | 保持只读，不丢弃已生成取证工件 |
| **Worker 进程** | 发送优雅停机信号 (SIGINT/SIGTERM) 释放租约 | Fencing token 保证旧实例不能覆盖新实例终态 | 仅清理已声明的可弃集 (`workspace/`) |
| **SQLite 数据库** | 执行热备份 `python -m scripts.backup_restore backup` | 严禁破坏性 DROP/回滚已应用 schema；迁移为 forward-only | 备份包携带清单与 SHA256 完整性证明 |

### 回滚标准步骤：
1. **停止接单**：在 API 配置中设置 `PATCHPILOT_EMERGENCY_STOP=true`，切断新任务流入；
2. **停止 Worker 领取**：优雅停止当前 Worker，等待正在执行的任务在超时前完成或进入安全中断；
3. **制作现场快照**：运行 `python -m scripts.backup_restore backup --target /data/backups` 留存当前一致状态；
4. **回退版本**：重新部署受控的上一个稳定镜像 digest；
5. **恢复接单**：确认健康检查探针 `/api/health/ready` 返回 200 后，解除急停恢复服务。
