"""G12:在途取消——晚到回复只计量不行动、取消不覆盖终态、命令终止延迟如实上报。

验收目标(SPEC §7 G12):"空闲/等待状态取消 1 秒内收敛"(等待态面见
tests/test_waiting_input.py);"受控命令在 cancel 请求后 5 秒内终止,超出则报告
cancellation_pending"。LLM transport 的中断边界如实声明:同步 SDK 调用不可中途打断,
timeout 是上界,晚到的回复只入账不入动作(本文件第一条用例钉住这一点)。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from app.errors import PatchPilotError, TaskCancelled
from app.executor import command_runner
from app.executor.command_runner import run_controlled_command
from app.graph.loop_state import SNAPSHOT_FILENAME
from app.graph.plain_loop import run_plain_loop
from app.storage.repository import Repository
from app.tools.base import ToolContext
from app.tools.tracker import Tracker

TASK = "T-CANCEL"


class _BlockingModel:
    """一个可以被外部放行的模型桩:模拟"回复还在路上"的窗口。"""

    def __init__(self, tool_call: dict, usage_tokens: int = 50) -> None:
        self.tool_call = tool_call
        self.usage_tokens = usage_tokens
        self.entered = threading.Event()
        self.release = threading.Event()
        self.seen: list[list[dict]] = []

    def complete(self, messages, tools):
        from app.llm.base import AssistantTurn, ToolCall

        self.seen.append([dict(m) for m in messages])
        self.entered.set()
        self.release.wait(timeout=30)
        return AssistantTurn(
            content="我把补丁应用到这个文件",
            tool_calls=[
                ToolCall(id="late-1", name=self.tool_call["tool"], arguments=self.tool_call["args"])
            ],
            finish_reason="tool_calls",
            usage_tokens=self.usage_tokens,
            prompt_tokens=10,
            completion_tokens=self.usage_tokens,
        )


def _ctx(demo_repo: Path, tmp_path: Path, *, cancel_event: threading.Event) -> ToolContext:
    from app.gitops.snapshot import create_workspace

    workspace = tmp_path / "ws"
    baseline = create_workspace(demo_repo, workspace)
    return ToolContext(
        task_id=TASK,
        workspace=workspace,
        baseline_commit=baseline,
        tracker=Tracker(tmp_path / "trajectory.jsonl", task_id=TASK),
        report_dir=tmp_path / "report",
        test_sets={},
        allowed_paths=None,
        cancel_event=cancel_event,
    )


def test_late_reply_is_metered_but_no_tool_runs(demo_repo: Path, tmp_path: Path) -> None:
    """取消与回复同时到达:这一次花费入账,工具一个不执行,快照留在可续位置。"""
    cancel_event = threading.Event()
    ctx = _ctx(demo_repo, tmp_path, cancel_event=cancel_event)
    model = _BlockingModel({"tool": "read_file", "args": {"path": "README.md"}})
    result: dict = {}

    def _run() -> None:
        try:
            run_plain_loop(
                ctx, model, "issue", max_turns=4, state_label="LOCALIZE", cancel_event=cancel_event
            )
        except BaseException as exc:  # 断言真实异常类型;先收下再在主线程断言
            result["exc"] = exc

    thread = threading.Thread(target=_run)
    thread.start()
    assert model.entered.wait(timeout=15), "模型调用应已经开始"
    cancel_event.set()  # 取消请求到达,而回复还在路上
    model.release.set()  # 晚到的回复终于回来了
    thread.join(timeout=20)
    assert not thread.is_alive()

    assert isinstance(result.get("exc"), TaskCancelled)
    events = ctx.tracker.events
    llm_events = [e for e in events if e.tool == "llm"]
    assert llm_events and llm_events[-1].output_summary["tokens"] == 50, "晚到消耗必须入账"
    blocked = [e for e in events if e.tool == "read_file"]
    assert blocked and blocked[0].input.get("blocked") is True and blocked[0].error
    assert not any(e.tool == "tool_started" for e in events), "被取消挡下的调用不得真的执行"
    assert (tmp_path / SNAPSHOT_FILENAME).is_file(), "取消点仍必须是可续位置"


def test_controlled_command_reports_kill_latency_and_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正常取消:如实报终止耗时且 pending=False;超过目标窗口:pending=True。"""
    argv = [sys.executable, "-c", "import time; time.sleep(30)"]

    cancel_event = threading.Event()
    thread = threading.Thread(target=lambda: time.sleep(0.5) or cancel_event.set())
    thread.start()
    result = run_controlled_command(
        argv,
        cwd=tmp_path,
        scratch_dir=tmp_path / "scratch",
        timeout_seconds=60,
        output_cap_bytes=100_000,
        cancel_event=cancel_event,
    )
    thread.join(timeout=5)
    assert result.cancelled is True
    assert result.cancellation_pending is False, "本机正常取消应在目标窗口内收敛"
    assert 0 <= result.kill_latency_ms < command_runner.CANCELLATION_PENDING_THRESHOLD_MS

    # 慢速终止:把门槛钳小 + 让 kill 路径真的花掉时间,验证上报而不是虚报"及时"
    monkeypatch.setattr(command_runner, "CANCELLATION_PENDING_THRESHOLD_MS", 50)
    original_kill = command_runner._kill_tree

    def _slow_kill(proc) -> None:
        time.sleep(0.2)
        original_kill(proc)

    monkeypatch.setattr(command_runner, "_kill_tree", _slow_kill)
    cancel_event2 = threading.Event()
    thread2 = threading.Thread(target=lambda: time.sleep(0.5) or cancel_event2.set())
    thread2.start()
    slow = run_controlled_command(
        argv,
        cwd=tmp_path,
        scratch_dir=tmp_path / "scratch2",
        timeout_seconds=60,
        output_cap_bytes=100_000,
        cancel_event=cancel_event2,
    )
    thread2.join(timeout=5)
    assert slow.cancelled is True
    assert slow.kill_latency_ms >= 150
    assert slow.cancellation_pending is True, "超过目标窗口必须如实报 pending"


def test_cancel_after_terminal_does_not_overwrite(tmp_path: Path) -> None:
    """取消×自然完成只有一个有效状态转移:终态行不得被 CANCELLED 覆盖。"""
    from app.api.service import TaskService

    repo = Repository(tmp_path / "db.sqlite3")
    repo.create_task(
        task_id="T-finished",
        idem_key="k1",
        bug_id="T-finished",
        repo_path=".",
        issue_text="x",
        max_rounds=2,
        engine="graph",
        model_provider="fake",
        run_dir=str(tmp_path / "runs" / "T-finished"),
    )
    assert repo.finalize_task("T-finished", "FINISHED", "resolved")
    service = TaskService(repo=repo, runs_root=tmp_path / "runs")
    try:
        with pytest.raises(PatchPilotError):
            service.cancel_task("T-finished")
    finally:
        service.shutdown()
    row = repo.get_task("T-finished")
    assert row is not None and row["status"] == "FINISHED" and row["verdict"] == "resolved"
