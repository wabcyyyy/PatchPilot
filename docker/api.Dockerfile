# PatchPilot API 服务镜像(docker-compose 的 api 服务)
FROM python:3.11-slim

# gitops 需要 git(快照/补丁/回滚都在容器内执行)
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
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
