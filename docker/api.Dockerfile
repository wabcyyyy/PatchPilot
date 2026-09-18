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
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt pytest

COPY app ./app
COPY bugs ./bugs

ENV PATCHPILOT_RUNS_ROOT=/data/runs \
    PATCHPILOT_DB_PATH=/data/patchpilot.sqlite3
VOLUME ["/data"]

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
