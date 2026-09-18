#!/usr/bin/env bash
# compose 隔离闭环冒烟(M11.3):API 容器经 docker.sock 把测试派发进临时执行容器。
#
# 前置:Docker 守护进程可达;8001 端口空闲。
# 流程:构建执行器镜像 → 起 compose(backend=docker)→ 等健康检查 →
#       API 建 BUG-001(fake/graph)任务 → 轮询终态 → 校验报告 verdict 与 provenance。
# 用法:bash scripts/compose_smoke.sh   (Windows 上在 Git Bash 中执行)

set -euo pipefail

REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_DIR/docker"

# 冒烟专用宿主端口(8001 常被本机其他服务/开发服务器占用)
HOST_PORT=${PATCHPILOT_API_HOST_PORT:-18001}
API="http://127.0.0.1:${HOST_PORT}"

# 解析 python(JSON 解析用):优先仓库 venv;Windows 的 Store stub(退出码 49)必须排除
PYTHON=${PYTHON:-}
if [ -z "$PYTHON" ]; then
    if [ -x "../.venv/Scripts/python.exe" ]; then
        PYTHON="../.venv/Scripts/python.exe"
    elif command -v python3 >/dev/null 2>&1; then
        PYTHON=python3
    elif command -v python >/dev/null 2>&1 && python -c "" 2>/dev/null; then
        PYTHON=python
    else
        echo "[smoke] 未找到可用 python(设置 PYTHON 环境变量指定)"; exit 1
    fi
fi

# Git Bash 会转换以 / 开头的参数路径,compose YAML 不受影响,但保险起见对本脚本关闭
export MSYS_NO_PATHCONV=1

# runs 目录"同路径 bind"换算(M11 关键约束):API 容器传给 docker run -v 的路径
# 必须与宿主守护进程看到的一致。Docker Desktop 的守护进程在 /run/desktop/mnt/host/<盘>/ 下
# 看到各 Windows 盘;Linux 宿主则天然同路径。
WIN_DIR=$(cd "$REPO_DIR" && pwd -W 2>/dev/null || true)
if [[ "$WIN_DIR" == [A-Za-z]:* ]]; then
    HOST_RUNS="$WIN_DIR/runs"
    RUNS_ROOT="/run/desktop/mnt/host/$(printf '%s' "$WIN_DIR" | sed -e 's|^\([A-Za-z]\):/|\L\1/|')/runs"
else
    HOST_RUNS="$REPO_DIR/runs"
    RUNS_ROOT="$HOST_RUNS"
fi
mkdir -p "$REPO_DIR/runs"
export PATCHPILOT_RUNS_HOST_DIR="$HOST_RUNS" PATCHPILOT_RUNS_ROOT="$RUNS_ROOT"
echo "[smoke] runs 同路径映射:$RUNS_ROOT (宿主: $HOST_RUNS)"

echo "[smoke] 1/5 构建执行器镜像(宿主守护进程需可见)"
docker build -q -t patchpilot-executor:latest -f executor.Dockerfile . >/dev/null

cleanup() {
    docker compose down >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "[smoke] 2/5 启动 compose(backend=docker)"
PATCHPILOT_EXECUTION_BACKEND=docker \
PATCHPILOT_DOCKER_IMAGE=patchpilot-executor:latest \
PATCHPILOT_API_HOST_PORT="$HOST_PORT" \
    docker compose up -d --build >/dev/null

echo "[smoke] 3/5 等待 API 健康"
for _ in $(seq 1 60); do
    if curl -sf "$API/api/health" >/dev/null; then break; fi
    sleep 1
done
curl -sf "$API/api/health" >/dev/null || { echo "[smoke] API 未就绪"; exit 1; }

echo "[smoke] 4/5 创建 BUG-001 任务(fake/graph)"
task_json=$(curl -sf -X POST "$API/api/tasks" \
    -H "Content-Type: application/json" \
    -d '{"bug_id":"BUG-001","engine":"graph","model":"fake"}')
task_id=$(printf '%s' "$task_json" | "$PYTHON" -c "import sys,json;print(json.load(sys.stdin)['task_id'])")
echo "[smoke] task_id=$task_id"

echo "[smoke] 5/5 轮询终态并校验报告"
for _ in $(seq 1 120); do
    status=$(printf '%s' "$(curl -sf "$API/api/tasks/$task_id")" \
        | "$PYTHON" -c "import sys,json;print(json.load(sys.stdin)['status'])")
    case "$status" in
        RUNNING|QUEUED) sleep 2 ;;
        *) break ;;
    esac
done

"$PYTHON" - "$task_id" "$HOST_PORT" <<'PYEOF'
import json
import sys
import urllib.request

task_id, port = sys.argv[1], sys.argv[2]
report = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/tasks/{task_id}/report"))
prov = report.get("provenance") or {}
assert report["verdict"] == "resolved", f"verdict={report['verdict']}"
assert prov.get("execution_backend") == "docker", f"backend={prov.get('execution_backend')}"
print(f"[smoke] OK: {task_id} resolved, provenance.execution_backend=docker")
PYEOF

echo "[smoke] 通过:compose 隔离闭环(API 容器 → docker.sock → 临时执行容器)端到端可用"
