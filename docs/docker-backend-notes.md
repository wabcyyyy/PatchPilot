# Docker 执行后端:接线点分析与设计笔记(N6 骨架的白天续作)

> 状态:骨架已落地(`app/executor/backend.py` + `Settings.execution_backend`),
> **未接入任何真实执行路径**。本文档记录哪些调用点可以切、哪些在禁区、白天怎么接。

## 现状

- `Settings.execution_backend: str = "local"`(local|docker,非法值在构造 `Settings()` 时报错);
- `app/executor/backend.py`:
  - `build_docker_command(command, *, image, workspace, memory, cpus)` 把宿主命令包进
    `docker run --rm --network none --memory ... --cpus ... -v ws:ws -w ws <image> ...`;
  - `run_tests_by_backend(command, cwd, timeout)` 按 settings 分发;
    docker 路径先 `docker_available()` 探测,再经 `local_runner.run_tests` 执行
    `docker run` 子进程(与 `docker_runner.run_tests_in_container` 的内部实现同构)。
- 隔离旗标与 `docker_runner` 一致;镜像默认 `Settings.docker_image`(python:3.11-slim)。

## 执行调用点全景(谁在真正跑 pytest)

| 调用点 | 位置 | 禁区? | 说明 |
|---|---|---|---|
| 基线/验证测试 | `app/graph/nodes.py` → `run_pytest` | `app/graph/` 语义冻结 | 只允许"加参数/加字段"式扩展,换后端需要讨论 |
| plain 引擎验证 | `app/evals/driver.py` → `run_pytest` | 判定逻辑在 metrics,driver 本体可改 | 与上共用 `run_pytest` |
| Agent 自跑测试 | `app/tools/execution.py` → `run_tests` | `app/tools/` 边界校验冻结 | 命令白名单边界不可动 |
| 执行器本体 | `app/executor/local_runner.py` | 边界冻结(签名/进程树清理) | 不改,`docker run` 也由它执行 |
| 隔离执行器(已有) | `app/executor/docker_runner.py` | 非禁区但本次未动 | 挂载 reports 传回 junit 的现成实现 |

关键事实:两条链路最终都收敛到 `app/adapters/pytest_adapter.py::run_pytest`
(签名规则冻结),它内部调用 `local_runner.run_tests`。**最小侵入的接线点就是
`run_pytest` 内部的这一次 `run_tests` 调用**。

## 白天接线方案(建议)

1. **方案 A(最小侵入,推荐)**:在 `pytest_adapter.run_pytest` 里把
   `run_tests(...)` 换成 `backend.run_tests_by_backend(...)`,签名不变、判定不变。
   - 需要解决:junit 报告路径。容器内写 `/reports/junit-*.xml` 才能传回宿主
     (见 `docker_runner.run_tests_in_container` 的挂载做法),`run_pytest` 的
     junit 输出路径参数需要做"宿主路径 → 容器路径"映射;
   - 需要解决:镜像内依赖。`python:3.11-slim` 没有被测项目的依赖与 pytest,
     要么构建含依赖的题内镜像,要么约定 `pip install -e .` 的预跑步骤。
2. **方案 B(复用现有实现)**:`run_pytest` 增加 `in_container: bool = False`
   参数,为真时走 `docker_runner.run_tests_in_container`(向后兼容的加参数扩展,
   符合禁区约束),由上层(driver/nodes)决定是否传 True。
3. **不建议**:在 `tools/execution.py` 层切换后端——Agent 自跑测试属于
   白名单边界保护的攻击面,容器化它需要单独的威胁模型讨论。

## 已知坑(白天验证清单)

- [ ] Windows 宿主路径挂载语法(`D:\...` → `//d/...` 或 `-v //c/...`),`docker run` 的
      `-v` 对盘符路径的兼容性要在真实环境验证;
- [ ] 容器内进程的用户/权限与挂载目录的写权限(junit 写 `/reports`);
- [ ] `--network none` 下 pytest 插件是否尝试联网(缓存/遥测)导致慢或失败;
- [ ] 超时语义:`local_runner` 杀的是宿主进程树,`docker run` 的子进程在容器内,
      超时后要确认容器确实被 `--rm` 回收(必要时改用 `--name` + `docker kill`);
- [ ] `docker_available()` 有 lru_cache:守护进程中途启动/停掉时进程内缓存不会刷新。

## 测试怎么保持离线

`tests/test_backend.py` 对 docker 路径全部 monkeypatch(`run_tests` 捕获拼装命令、
`docker_available` 打桩),不拉真容器;local 路径只跑 `sys.executable -c` 级别的真子进程。
