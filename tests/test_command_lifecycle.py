"""G09 受控命令生命周期:原始输出落盘、分页取回、超时/取消终止、env 清理与隔离边界。

全部离线:local 后端用真子进程(python -c / 登记的 repro),docker 只测
"守护进程不可用 ⇒ 能力不可用"的拒绝路径(真实容器隔离由 scripts/ 的集成验证负责)。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from app.context.evidence import build_store
from app.executor.command_runner import run_controlled_command
from app.tools.base import ToolContext
from app.tools.tracker import Tracker

PYTHON = sys.executable


def _make_ctx(tmp_path: Path) -> tuple[ToolContext, Path]:
    ws = tmp_path / "workspace"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "mod.py").write_text("VALUE = 41\n", encoding="utf-8")
    reports = tmp_path / "reports"
    reports.mkdir()
    ctx = ToolContext(
        task_id="t-cmd",
        workspace=ws,
        baseline_commit="0" * 40,
        tracker=Tracker(reports / "traj.jsonl", task_id="t-cmd"),
        report_dir=reports,
        python_exe=PYTHON,
    )
    return ctx, ws


def _run(ctx: ToolContext, name: str, args: dict):
    from app.tools.registry import execute

    return execute(ctx, name, args, round_no=1, state="LOCALIZE")


# ---------- 工具层:profile 执行与隔离 ----------


def test_run_command_executes_registered_repro(tmp_path: Path) -> None:
    ctx, _ws = _make_ctx(tmp_path)
    r = _run(ctx, "write_repro", {"content": "from src.mod import VALUE\nprint('V=', VALUE)\n"})
    assert r.ok
    r = _run(
        ctx, "run_command", {"profile_id": "python_repro", "parameters": {"repro": "repro-1.py"}}
    )
    assert r.ok, r.error
    assert r.output["exit_code"] == 0
    assert "V= 41" in r.output["stdout"]["tail"]
    # 回执只陈述事实,不携带任何验收语义
    assert r.output["role"] == "auxiliary"
    assert "not acceptance" in r.output["note"]


def test_run_command_never_pollutes_workspace_or_patch(tmp_path: Path) -> None:
    ctx, ws = _make_ctx(tmp_path)
    _run(ctx, "write_repro", {"content": "open('side-effect.txt', 'w').write('x')\n"})
    r = _run(
        ctx, "run_command", {"profile_id": "python_repro", "parameters": {"repro": "repro-1.py"}}
    )
    assert r.ok and r.output["exit_code"] == 0
    # 副作用落在 scratch 的诊断副本里,工作区一字未动 ⇒ patch diff 域没有新文件
    assert not (ws / "side-effect.txt").exists()
    assert not any(ws.rglob(".pp-repro"))
    diag_files = list((ctx.report_dir.parent / "scratch").glob("ws-diag-*/side-effect.txt"))
    assert len(diag_files) == 1


def test_unregistered_repro_is_rejected(tmp_path: Path) -> None:
    ctx, _ws = _make_ctx(tmp_path)
    r = _run(ctx, "run_command", {"profile_id": "python_repro", "parameters": {"repro": "nope.py"}})
    assert not r.ok
    assert "not registered" in (r.error or "")


def test_raw_output_saved_and_pageable_beyond_tail(tmp_path: Path) -> None:
    ctx, _ws = _make_ctx(tmp_path)
    script = "print('HEAD-MARKER')\nprint('" + "y" * 6000 + "')\nprint('TAIL-MARKER')\n"
    assert len(script) > 2000
    _run(ctx, "write_repro", {"content": script})
    r = _run(
        ctx, "run_command", {"profile_id": "python_repro", "parameters": {"repro": "repro-1.py"}}
    )
    assert r.ok and r.output["exit_code"] == 0
    stdout = r.output["stdout"]
    assert stdout["retrievable"] and stdout.get("evidence_id")
    assert len(stdout["tail"]) <= 2000  # 模型可见 tail 有界
    # 原文经分页取回后逐字节拼回
    store = build_store(ctx.report_dir, ctx.task_id)
    chunks: list[str] = []
    cursor = ""
    for _ in range(50):
        page = store.read(stdout["evidence_id"], cursor=cursor)
        chunks.append(page.text)
        if page.eof:
            break
        cursor = page.next_cursor or ""
    raw = "".join(chunks)
    assert raw.startswith("HEAD-MARKER")
    assert "TAIL-MARKER" in raw


def test_environment_not_ready_when_docker_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.tools import commands as cmd_mod

    ctx, _ws = _make_ctx(tmp_path)
    ctx.execution_backend = "docker"
    monkeypatch.setattr(cmd_mod, "docker_available", lambda: False)
    r = _run(
        ctx,
        "run_command",
        {"profile_id": "python_compile", "parameters": {"paths": ["src/mod.py"]}},
    )
    assert not r.ok
    assert (r.error or "").startswith("environment_not_ready")
    assert "never falls back" in (r.error or "")


def test_host_secrets_never_reach_command_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATCHPILOT_LLM_API_KEY", "sk-secret-do-not-leak")
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "leak-me-not")
    ctx, _ws = _make_ctx(tmp_path)
    script = (
        "import os\n"
        "print('K=', ','.join(sorted(os.environ)))\n"
        "print('LEAK=', any('SECRET' in k or 'API_KEY' in k for k in os.environ))\n"
    )
    _run(ctx, "write_repro", {"content": script})
    r = _run(
        ctx, "run_command", {"profile_id": "python_repro", "parameters": {"repro": "repro-1.py"}}
    )
    assert r.ok
    tail = r.output["stdout"]["tail"]
    assert "sk-secret-do-not-leak" not in tail
    assert "leak-me-not" not in tail
    assert "LEAK= False" in tail


# ---------- 执行器:生命周期(直接驱动 run_controlled_command) ----------


def test_timeout_terminates_process(tmp_path: Path) -> None:
    started = time.monotonic()
    result = run_controlled_command(
        [PYTHON, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        scratch_dir=tmp_path / "scratch",
        timeout_seconds=1,
        output_cap_bytes=1_000_000,
    )
    elapsed = time.monotonic() - started
    assert result.timed_out and not result.cancelled
    assert result.exit_code != 0
    assert elapsed < 10


def test_cancel_event_terminates_process(tmp_path: Path) -> None:
    cancel = threading.Event()
    timer = threading.Timer(0.5, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        result = run_controlled_command(
            [PYTHON, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            scratch_dir=tmp_path / "scratch",
            timeout_seconds=30,
            output_cap_bytes=1_000_000,
            cancel_event=cancel,
        )
    finally:
        timer.cancel()
    elapsed = time.monotonic() - started
    assert result.cancelled and not result.timed_out
    assert elapsed < 10


def test_raw_streams_captured_with_dropped_count(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    result = run_controlled_command(
        [PYTHON, "-c", "print('O' * 3000); import sys; sys.stderr.write('E' * 3000)"],
        cwd=tmp_path,
        scratch_dir=scratch,
        timeout_seconds=30,
        output_cap_bytes=1000,
    )
    assert result.exit_code == 0
    assert result.stdout_bytes == 1000
    assert result.stdout_dropped_bytes > 0
    assert result.stderr_dropped_bytes > 0
    assert (scratch / "cmd-0-stdout.raw").read_text(encoding="utf-8").strip() == "O" * 1000


def test_missing_executable_is_exec_error(tmp_path: Path) -> None:
    from app.errors import ExecError

    with pytest.raises(ExecError):
        run_controlled_command(
            ["definitely-not-a-real-binary-xyz"],
            cwd=tmp_path,
            scratch_dir=tmp_path / "s",
            timeout_seconds=5,
            output_cap_bytes=1000,
        )
