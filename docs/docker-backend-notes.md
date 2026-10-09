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

## 非 root 执行器(M11.2,2026-09-19)

执行器镜像以非 root 用户 `pp`(uid 1000)运行被测代码:被测仓库的 pytest
不应拥有容器内 root 权限。配套改动:

- junit 回传目录由 `docker_runner` 在宿主侧 `chmod 0o755`(R2 整改从 0o777 收紧:
  junit 不可被无关用户改写)。收紧后"容器写得进"就不再靠权限,而靠**身份对齐**:
  见下面 2026-10-09 那节;
- 被测工作区(/ws 挂载)若不可写,Python 跳过 `__pycache__` 落盘(静默,无影响),
  pytest 临时文件走容器内 /tmp。

## 依赖策略(--network=none 的必然约束)

容器执行测试时断网,无法 `pip install`,因此:

| 被测仓库依赖 | 做法 |
|---|---|
| 仅标准库(现正式集 28 题与候选集均如此) | 默认执行器镜像直接跑 |
| 第三方依赖(固定、通用) | 扩展 `docker/executor.Dockerfile` 预装后自建 |
| 第三方依赖(题目特定) | 为该题构建专用镜像,任务经 `PATCHPILOT_DOCKER_IMAGE`(全局)指定 |

## 服务化部署(docker-compose)的隔离闭环(M11.1/M11.3)

- `docker/api.Dockerfile` 已装 docker-ce-cli,API 容器可经挂载的宿主
  `/var/run/docker.sock` 派发临时容器(见 `docker/docker-compose.yml`);
- **路径命名空间约束(真机踩出来的坑)**:API 容器把工作区路径字符串原样传给
  `docker run -v`,宿主守护进程按**宿主同名路径**解析——API 容器里的命名卷路径
  (如 /data/runs)在守护进程侧并不存在,执行容器会拿到空目录。因此 compose 的
  runs 目录必须 bind 挂载且**容器内 target 与宿主守护进程视角路径一致**
  (`PATCHPILOT_RUNS_ROOT` == bind target):Linux 宿主天然同路径;
  Docker Desktop 上守护进程视角是 `/run/desktop/mnt/host/<盘符小写>/...`,
  `scripts/compose_smoke.sh` 有现成换算;
- 冒烟验收:`bash scripts/compose_smoke.sh` —— 构建执行器镜像 → 起 compose
  (backend=docker,端口可经 PATCHPILOT_API_HOST_PORT 避让)→ API 建 BUG-001 →
  轮询终态 → 断言 report verdict=resolved 且 provenance.execution_backend=docker;
- 注意:挂载 sock 等于把宿主守护进程的等价权限交给 API 容器——这是把隔离边界
  从"进程"升级为"容器"的代价,compose 默认仍绑回环地址,redis 无宿主暴露面;
- Windows/Docker Desktop 的 `/var/run/docker.sock` 挂载路径可用(桌面版已内置转发)。

## 测试怎么保持离线

- `tests/test_backend.py`:docker 路径全部 monkeypatch(run_tests_in_container 捕获、
  docker_available 打桩),不拉真容器;
- `tests/test_docker.py::test_run_pytest_docker_backend_end_to_end`:真实容器回归,
  守护进程不可用时自动 skip(与该文件既有约定一致);
- 全量 pytest 套件在 backend 默认 local 下运行,不依赖 docker。

## 报告目录权限与 uid 1000(复盘 2026-10-07 实测补记)

`docker_runner.py` 对宿主报告目录 `chmod(0o755)`(R2 整改:0o777 → 0o755,
junit 不可被无关用户改写),容器以 uid 1000 写 junit 回传。真机实证
(`docker run alpine`,uid 1000 对 root 属主 0755 目录 `touch` →
`Permission denied`,exit=1):

- **Docker Desktop(本仓当前开发/评测形态)**:文件共享层不按宿主 uid 强校验,
  实测批次(swe-real 等 5+25 题)junit 全部正常落盘——无问题;
- **原生 Linux 宿主(潜在部署形态)**:若运行 API 的服务用户 uid ≠ 1000,
  0755 下 other 无写权 → "docker_available 通过、每次执行必败"。
  缓解选项(按侵入性排序):服务进程以 uid 1000 运行;部署前置
  `chown 1000` runs 目录;或接受该边界、仅在有 Docker Desktop/同 uid 的环境
  用 docker 后端。改回 0o777 已被 R2 以安全理由否决,不重开。

## 身份对齐落地(M20,2026-10-09:把上面那条"部署约束"改成代码契约)

CI(run #5..#17)的 docker 簇就是上面"uid ≠ 1000"那条分支被真实触发的结果,
容器复现与 CI 特征逐字一致:pytest 跑完 → `PermissionError` → **exit 1 且无 junit**;
工作区若为 0700 则连进去都做不到,同样 exit 1 无 junit。

`docker_runner._container_identity_cmd_flags` 现在的规则(报告目录仍是 0o755,
**没有**为了写得进而放开权限):

| 宿主 | 做法 | 被测代码是否 root |
|---|---|---|
| 非 posix(Docker Desktop) | 不加任何身份参数(bind mount 不校验宿主 uid,加了 `--user` 反而撞上镜像里没有的 gid) | 否(pp) |
| posix,本进程非 root | `--user <本进程 uid:gid>` + `-e HOME=/tmp` | 否 |
| posix,本进程是 root(compose 里 api 容器没有 USER) | 保持 pp(1000),把**本次** workspace 与 reports `chown 1000:1000`(含子项) | 否 |

两条 posix 分支都不给被测代码 root 权限,M11.2 的立意不变。`HOME=/tmp` 是配套:
对齐来的 uid 并不拥有镜像里的 `/home/pp`,写 `$HOME` 缓存的测试会因此失败。

**观测边界(不许当作证据)**:本地 Windows/Docker Desktop 全绿**不能**证明 Linux
分支正确——文件共享层根本不执行 unix 属主校验。docker 簇的验收只能看 ubuntu CI,
这也是 M20 判据里"CI 连续 3 次全绿"不是形式的原因。
