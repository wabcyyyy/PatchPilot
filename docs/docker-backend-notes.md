# Docker 执行后端:设计笔记(已接线,2026-09-18)

> 状态:**已接线并真机验证**。`app/adapters/pytest_adapter.py::run_pytest` 按
> `Settings.execution_backend` 分发:local 直跑(默认,docker 路径零影响);
> docker 时经 `docker_runner.run_tests_in_container` 在临时容器内执行,
> 签名与返回结构不变,验证链路(nodes/driver)与 Agent 工具层(tools/execution)
> 同时容器化。真机验收:BUG-001 全链路回放在 backend=docker 下 FINISHED/resolved
> (`test_run_pytest_docker_backend_end_to_end` 常驻回归,守护进程不可用时自动跳过)。

## 使用前提

```bash
docker build -t patchpilot-executor:latest docker/   # 镜像需预装 pytest(python:3.11-slim 没有)
export PATCHPILOT_EXECUTION_BACKEND=docker
export PATCHPILOT_DOCKER_IMAGE=patchpilot-executor:latest   # 默认 python:3.11-slim 无 pytest
```

## 接线点(当年分析的结论,现照此落地)

| 调用点 | 位置 | 是否容器化 | 说明 |
|---|---|---|---|
| 基线/验证测试 | `app/graph/nodes.py` → `run_pytest` | ✅(经由 run_pytest 分发) | 上层无感知 |
| plain 引擎验证 | `app/evals/driver.py` → `run_pytest` | ✅(同上) | 判定逻辑未动 |
| Agent 自跑测试 | `app/tools/execution.py` → `run_pytest` | ✅(同上) | 命令白名单边界原样保留 |
| 通用执行器 | `app/executor/local_runner.py` | 未动 | `docker run` 子进程也由它执行 |
| 隔离执行器 | `app/executor/docker_runner.py` | 复用 | 双挂载 + junit 回传的现成实现 |

**选择方案 A 的原因**:`run_pytest` 是全部 pytest 执行的唯一汇聚点,在这里分支一次,
三条链路同时生效;签名不变,不触碰禁区语义。

## 已知差异与坑(仍然有效)

- `extra_args` 不下发容器路径(容器内命令由 docker_runner 组装;当前生产调用方未用该参数);
- 容器路径 basetemp 用容器内可弃临时目录(容器销毁即清理,无需指向报告目录);
- Windows 宿主路径挂载:`docker_runner` 直接挂载绝对路径,Docker Desktop 桌面版已验证可用;
- `--network none` 下容器无网:被测仓库的测试若尝试联网会失败——这正是隔离语义;
- 超时杀的是宿主 `docker run` 进程,容器靠 `--rm` 在退出后回收;
- `docker_available()` 带 lru_cache:守护进程中途启停不会刷新(进程内);
- 镜像内只有 pytest:被测仓库若引入第三方依赖,需要扩展 `docker/executor.Dockerfile` 或换题内镜像。

## 测试怎么保持离线

- `tests/test_backend.py`:docker 路径全部 monkeypatch(run_tests_in_container 捕获、
  docker_available 打桩),不拉真容器;
- `tests/test_docker.py::test_run_pytest_docker_backend_end_to_end`:真实容器回归,
  守护进程不可用时自动 skip(与该文件既有约定一致);
- 全量 pytest 套件在 backend 默认 local 下运行,不依赖 docker。
