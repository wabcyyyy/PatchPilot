# PatchPilot 执行器镜像:在临时容器中运行目标仓库的 pytest
# 用法(由 app/executor/docker_runner.py 组装,或手动验证):
#   docker build -t patchpilot-executor:latest .
#   docker run --rm --network=none -v <workspace>:/ws -w /ws patchpilot-executor:latest \
#       python -m pytest -q
#
# 容器以非 root(uid 1000)运行被测代码:被测仓库的测试不应拥有容器内 root 权限。
# 这个 uid 要与宿主给它的两个目录(工作区、报告目录)对得上,否则 Linux 宿主上
# junit 写不进 → 表现为"容器内 collected=0 / no junit xml"。对齐由
# app/executor/docker_runner.py 负责(非 root 宿主传 --user,root 宿主把本次目录
# chown 给 1000),改这里的 uid 必须同时改 docker_runner.EXECUTOR_UID。
# 注意:依赖策略见 docs/docker-backend-notes.md——--network=none 下无法 pip install,
# 本镜像只预装 pytest;被测仓库有第三方依赖时扩展本镜像或换 PATCHPILOT_DOCKER_IMAGE。
FROM python:3.11-slim

RUN pip install --no-cache-dir pytest \
    && useradd --create-home --uid 1000 pp \
    && mkdir -p /ws \
    && chown -R pp:pp /ws

USER pp
WORKDIR /ws

# 默认命令:无参数时不执行任何测试(执行器始终显式传入测试命令)
CMD ["python", "-m", "pytest", "--co", "-q"]
