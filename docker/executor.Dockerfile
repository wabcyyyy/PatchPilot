# PatchPilot 执行器镜像:在临时容器中运行目标仓库的 pytest
# 用法(由 app/executor/docker_runner.py 组装,或手动验证):
#   docker build -t patchpilot-executor:latest .
#   docker run --rm --network=none -v <workspace>:/ws -w /ws patchpilot-executor:latest \
#       python -m pytest -q
FROM python:3.11-slim

WORKDIR /ws
RUN pip install --no-cache-dir pytest

# 默认命令:无参数时不执行任何测试(执行器始终显式传入测试命令)
CMD ["python", "-m", "pytest", "--co", "-q"]
