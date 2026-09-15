# Docker 隔离边界实验记录

- 日期:2026-09-16
- 环境:Windows 11 + Docker Desktop,Server 29.6.2
- 执行器镜像:`patchpilot-executor:latest`(python:3.11-slim + pytest,见 `docker/executor.Dockerfile`)
- 实验脚本:`scripts/docker_isolation_check.py`(可一键重跑)

## 实验结果(2026-09-16 实测)

| 实验 | 命令要点 | 期望 | 实测 |
|---|---|---|---|
| 联网阻断 | `--network none` + `socket.create_connection(('1.1.1.1', 80))` | 连接失败 | ✅ PASS:`NETWORK-BLOCKED: OSError` |
| 宿主文件不可见 | 未挂载任何宿主目录,列 `/host-windows` | 不存在 | ✅ PASS:`HOST-HIDDEN` |
| 挂载目录之外不可达 | 仅挂载工作区 `rw`,容器内写挂载内文件 | 只有挂载内可写 | ✅ PASS:写入成功且仅发生在挂载内 |
| 内存限额 | `--memory 16m` + 分配 64MB | 分配失败/进程被杀 | ✅ PASS:进程被 kill,无 `ALLOC-OK` |
| 正常执行 pytest | `--rm --network none` + 挂载 BUG-003 工作区跑失败测试 | 报告 1 failed | ✅ PASS:`1 failed in 0.24s` |

## 结论:Docker 隔离了什么、没隔离什么(企划书 4.2 问题 5)

**隔离了:**
- 文件系统:容器内仅能看到显式挂载的任务工作区(实验 2、3);
- 网络:`--network none` 下无任何外连能力(实验 1);
- 进程:容器内进程与宿主进程互不可见;
- 资源:内存/CPU 限额防止失控测试拖垮宿主(实验 4);
- 生命周期:`--rm` 用后即删,工作区与容器状态均不残留。

**没有隔离(仍存在的风险):**
- **内核共享**:容器与宿主共享 Linux 内核,内核态漏洞(逃逸类 CVE)不在防护范围;
- **显式挂载目录**:挂载为 `rw` 的工作区可被容器内进程任意改写——这就是为什么工作区本身必须是
  源仓库的副本而不是源仓库(见 `app/gitops/snapshot.py`);
- **资源旁路**:`--cpus/--memory` 限制单容器,但不限制"短时间内拉起大量容器";
- **时间/随机数等侧信道**:容器与宿主共享时钟,依赖时间的测试可能产生环境敏感结果;
- **Windows 下的隔离层次**:Docker Desktop 经 WSL2 虚拟机提供隔离,边界在 VM 与容器两层。

## 执行器使用方式

```bash
# 构建执行器镜像
docker build -t patchpilot-executor:latest -f docker/executor.Dockerfile docker

# 测试用例(tests/test_docker.py)演示了完整的容器内 VERIFY:
# 基线失败集 → 应用修复 → 失败集 + 回归集全绿,junit 报告经挂载目录传回宿主
pytest tests/test_docker.py -v
```
