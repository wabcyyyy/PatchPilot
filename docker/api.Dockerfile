# PatchPilot API 服务镜像(docker-compose 的 api 服务)
FROM python:3.11-slim

# gitops 需要 git(快照/补丁/回滚都在容器内执行);
# docker-ce-cli 让 API 容器经挂载的宿主 docker.sock 把测试派发进临时容器(M11 隔离闭环)
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# 依赖安装走 uv,uv.lock 固化全部传递依赖(与 CI 同一入口);
# 运行镜像不带 dev 组,pytest 是执行被测测试的运行时需要,单独补装(与原 pip 行为一致)
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev \
    && uv pip install --python /app/.venv/bin/python pytest
ENV PATH="/app/.venv/bin:$PATH"

COPY app ./app
COPY bugs ./bugs

ENV PATCHPILOT_RUNS_ROOT=/data/runs \
    PATCHPILOT_DB_PATH=/data/patchpilot.sqlite3
VOLUME ["/data"]

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
