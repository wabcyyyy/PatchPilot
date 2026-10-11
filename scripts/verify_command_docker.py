"""G09 Docker 集成验证(离线、零拉取):真实容器里验证受控命令的隔离与生命周期。

用法(需要本地已有 patchpilot-executor:latest,不自动 build/pull):
    .venv/Scripts/python.exe scripts/verify_command_docker.py --image patchpilot-executor:latest

场景:容器内 pytest_collect/pytest_selected/python_repro 正确执行;ruff 缺依赖如实
environment_not_ready;网络关闭(socket 必败);宿主密钥不进容器 env;timeout/cancel
按容器名精确终止本任务的容器;工作区零污染。结果写
docs/evidence/agent-upgrade/probe-after-g09-docker.json;docker 不可用退出码 2 并如实写
unverified —— mock 测试不算容器验证,缺证据就明说。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_PATH = REPO_ROOT / "docs/evidence/agent-upgrade/probe-after-g09-docker.json"


def _make_ctx(root: Path, task_id: str):
    from app.tools.base import ToolContext
    from app.tools.tracker import Tracker

    ws = root / "workspace"
    (ws / "src").mkdir(parents=True, exist_ok=True)
    (ws / "tests").mkdir(exist_ok=True)
    (ws / "src" / "mod.py").write_text("VALUE = 7\n", encoding="utf-8")
    (ws / "tests" / "test_mod.py").write_text(
        "from src.mod import VALUE\n\ndef test_value():\n    assert VALUE == 7\n",
        encoding="utf-8",
    )
    reports = root / "reports"
    reports.mkdir(exist_ok=True)
    ctx = ToolContext(
        task_id=task_id,
        workspace=ws,
        baseline_commit="0" * 40,
        tracker=Tracker(reports / "trajectory.jsonl", task_id=task_id),
        report_dir=reports,
        execution_backend="docker",
    )
    return ctx, ws


def _run_tool(ctx, name: str, args: dict):
    from app.tools.registry import execute

    return execute(ctx, name, args, round_no=1, state="DIAGNOSE")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="patchpilot-executor:latest")
    args = parser.parse_args()
    os.environ["PATCHPILOT_DOCKER_IMAGE"] = args.image

    from app.executor.docker_runner import docker_available

    started_at = datetime.now(UTC).isoformat()
    readings: dict[str, object] = {"started_at": started_at, "image": args.image}
    if not docker_available():
        readings["status"] = "unverified"
        readings["reason"] = "docker daemon unavailable; container execution not verified"
        EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE_PATH.write_text(
            json.dumps(readings, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(readings, ensure_ascii=False, indent=2))
        return 2

    import tempfile

    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="pp-docker-g09-") as tmp:
        ctx, ws = _make_ctx(Path(tmp), "docker-g09")
        ctx.repro_files = {}

        # 1) 容器内 pytest_collect / pytest_selected
        r = _run_tool(ctx, "run_command", {"profile_id": "pytest_collect", "parameters": {}})
        ok1 = (
            r.ok and r.output["exit_code"] == 0 and "1 test collected" in r.output["stdout"]["tail"]
        )
        readings["pytest_collect"] = {"ok": ok1, "backend": r.output["backend"] if r.ok else None}
        if not ok1:
            failures.append(f"pytest_collect: {r.error if not r.ok else r.output}")

        r = _run_tool(
            ctx,
            "run_command",
            {
                "profile_id": "pytest_selected",
                "parameters": {"test_ids": ["tests/test_mod.py::test_value"]},
            },
        )
        ok2 = r.ok and r.output["exit_code"] == 0 and "1 passed" in r.output["stdout"]["tail"]
        readings["pytest_selected"] = {"ok": ok2}
        if not ok2:
            failures.append(
                f"pytest_selected: {r.error if not r.ok else r.output['stdout']['tail'][:200]}"
            )

        # 2) 容器内 python_repro(PYTHONPATH 指向挂载点 /ws)
        r = _run_tool(
            ctx,
            "write_repro",
            {"content": "from src.mod import VALUE\nprint('REPRO', VALUE * 6)\n"},
        )
        r = _run_tool(
            ctx,
            "run_command",
            {"profile_id": "python_repro", "parameters": {"repro": "repro-1.py"}},
        )
        ok3 = r.ok and r.output["exit_code"] == 0 and "REPRO 42" in r.output["stdout"]["tail"]
        readings["python_repro"] = {"ok": ok3}
        if not ok3:
            failures.append(f"python_repro: {r.error if not r.ok else r.output}")

        # 3) ruff 缺依赖:如实 environment_not_ready(镜像只预装 pytest)
        r = _run_tool(
            ctx,
            "run_command",
            {"profile_id": "ruff_check", "parameters": {"paths": ["src/mod.py"]}},
        )
        ok4 = r.ok and r.output.get("environment", "").startswith("environment_not_ready")
        readings["ruff_missing_dependency"] = {
            "ok": ok4,
            "honest": r.output.get("environment") if r.ok else None,
        }
        if not ok4:
            failures.append(
                f"ruff missing-dependency honesty: {r.error if not r.ok else r.output.get('environment')}"
            )

        # 4) 网络关闭:socket 必败且快速失败
        r = _run_tool(
            ctx,
            "write_repro",
            {
                "content": (
                    "import socket\n"
                    "s = socket.socket()\n"
                    "s.settimeout(5)\n"
                    "try:\n"
                    "    s.connect(('example.com', 80))\n"
                    "    print('NETWORK-OPEN')\n"
                    "except OSError as exc:\n"
                    "    print('NETWORK-BLOCKED', type(exc).__name__)\n"
                )
            },
        )
        r = _run_tool(
            ctx,
            "run_command",
            {"profile_id": "python_repro", "parameters": {"repro": "repro-2.py"}},
        )
        tail = r.output["stdout"]["tail"] if r.ok else ""
        ok5 = r.ok and "NETWORK-BLOCKED" in tail and "NETWORK-OPEN" not in tail
        readings["network_disabled"] = {"ok": ok5, "tail": tail[-120:]}
        if not ok5:
            failures.append(f"network isolation: {tail[-200:]!r}")

        # 5) 宿主密钥不进容器 env
        os.environ["PATCHPILOT_LLM_API_KEY"] = "sk-host-secret"
        r = _run_tool(
            ctx,
            "write_repro",
            {
                "content": "import os\nprint('HASKEY=', any('PATCHPILOT' in k or 'SECRET' in k for k in os.environ))\n"
            },
        )
        r = _run_tool(
            ctx,
            "run_command",
            {"profile_id": "python_repro", "parameters": {"repro": "repro-3.py"}},
        )
        tail = r.output["stdout"]["tail"] if r.ok else ""
        ok6 = r.ok and "HASKEY= False" in tail and "sk-host-secret" not in tail
        readings["env_isolation"] = {"ok": ok6}
        if not ok6:
            failures.append(f"env isolation: {tail[-200:]!r}")
        os.environ.pop("PATCHPILOT_LLM_API_KEY", None)

        # 6) cancel 精确终止本任务的容器(按名字,无全局 kill)。不断言 stdout 里有
        # SLEEPING:非 tty 下 python 块缓冲,kill -9 会把缓冲丢掉——这是预期形态,
        # 生命周期看的是 cancelled/exit_code/耗时,不是输出内容。
        cancel = threading.Event()
        ctx.cancel_event = cancel  # 工具在子进程等待循环里轮询的就是这一个事件
        threading.Timer(3.0, cancel.set).start()
        r = _run_tool(
            ctx, "write_repro", {"content": "import time\nprint('SLEEPING')\ntime.sleep(60)\n"}
        )
        t0 = time.monotonic()
        r = _run_tool(
            ctx,
            "run_command",
            {"profile_id": "python_repro", "parameters": {"repro": "repro-4.py"}},
        )
        elapsed = time.monotonic() - t0
        ok7 = (
            r.ok
            and r.output["cancelled"] is True
            and r.output["exit_code"] not in (0, None)
            and elapsed < 30
        )
        readings["cancel_kills_container"] = {
            "ok": ok7,
            "elapsed_s": round(elapsed, 1),
            "exit_code": r.output.get("exit_code") if r.ok else None,
        }
        if not ok7:
            failures.append(f"cancel lifecycle: ok={r.ok} out={r.output if r.ok else r.error}")

        # 7) 工作区零污染
        strays = [p.name for p in ws.rglob("side-effect*")] + [
            p.name for p in ws.rglob(".pp-repro")
        ]
        ok8 = not strays
        readings["workspace_clean"] = {"ok": ok8, "strays": strays}
        if not ok8:
            failures.append(f"workspace polluted: {strays}")

    readings["status"] = "verified" if not failures else "failed"
    readings["failures"] = failures
    readings["finished_at"] = datetime.now(UTC).isoformat()
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(json.dumps(readings, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(readings, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
