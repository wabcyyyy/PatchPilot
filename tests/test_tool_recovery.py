"""G12:工具恢复语义——tool_call 状态机、恢复策略措辞与"unknown 不盲目重跑"。

覆盖(SPEC §7 G12 测试面):工具调用持久 tool_call_id 与 planned/running/completed/
unknown;read/search 可安全重放、apply_patch 依候选/hash 判断、命令 unknown 不盲目重跑;
恢复重验候选不复用旧绿结果(候选面由既有 candidate 测试链覆盖,这里钉措辞与状态机)。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.agent.session import (
    CALL_COMPLETED,
    CALL_RUNNING,
    CALL_UNKNOWN,
    ORPHAN_REPLY,
    SessionState,
)
from app.graph.loop_state import LoopSnapshot
from app.graph.plain_loop import run_plain_loop
from app.tools.base import ToolContext
from app.tools.registry import FINISH_TOOL, RECOVERY_POLICY, REGISTRY, execute, recovery_policy
from app.tools.tracker import Tracker

TASK = "T-RECOVERY"


def _ctx(demo_repo: Path, tmp_path: Path, **overrides) -> ToolContext:
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
        **overrides,
    )


def test_recovery_policy_covers_every_registered_tool() -> None:
    """每个注册工具都必须声明恢复策略;未登记名字保守按 no_rerun。"""
    assert set(RECOVERY_POLICY) == set(REGISTRY), "新增工具必须声明恢复策略(声明面与注册面同宽)"
    assert recovery_policy("run_command") == "no_rerun"
    assert recovery_policy("apply_patch") == "state_check"
    assert recovery_policy("read_file") == "safe_replay"
    assert recovery_policy("some_future_tool") == "no_rerun"


def test_orphan_placeholder_carries_policy_specific_guidance() -> None:
    messages = [
        {"role": "user", "content": "x"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"type": "function", "id": "a1", "function": {"name": "read_file"}},
                {"type": "function", "id": "a2", "function": {"name": "apply_patch"}},
                {"type": "function", "id": "a3", "function": {"name": "run_command"}},
            ],
        },
    ]
    repaired, added = SessionState.repair_orphans(messages, recovery_policy)
    assert added == 3
    by_id = {m["tool_call_id"]: m["content"] for m in repaired if m.get("role") == "tool"}
    assert ORPHAN_REPLY in by_id["a1"]
    assert "safe to replay" in by_id["a1"]
    assert "check git_diff" in by_id["a2"] and "frozen candidate" in by_id["a2"]
    assert "do not blindly rerun" in by_id["a3"]
    # 默认(不传策略)= 与接入前逐字相同的占位文本
    plain, _ = SessionState.repair_orphans(messages)
    assert all(m["content"] == ORPHAN_REPLY for m in plain if m.get("role") == "tool")


def test_call_status_machine_and_snapshot_roundtrip() -> None:
    session = SessionState(TASK, "s-1")
    session.declare_call("c1", "read_file", stage="LOCALIZE", round_no=0, turn_no=1)
    session.declare_call("c2", "run_command", stage="LOCALIZE", round_no=0, turn_no=1)
    session.mark_running("c2")
    session.answer_call("c1")
    assert [c.status for c in session.calls] == [CALL_COMPLETED, CALL_RUNNING]
    unknown = session.mark_open_calls_unknown()
    assert unknown == ["c2"] and session.calls[1].status == CALL_UNKNOWN

    # 快照往返:状态原样带回;旧快照缺 status 时按 answered 推导
    rows = session.snapshot()["calls"]
    restored = SessionState(TASK, "s-1").restore_calls(rows)
    assert restored == 2
    legacy = SessionState.from_snapshot(
        {
            "protocol_version": "repair_v2.session.1",
            "task_id": TASK,
            "calls": [{"call_id": "x", "name": "read_file", "answered": True}],
        }
    )
    assert legacy is not None and legacy.calls[0].status == CALL_COMPLETED


def test_tool_started_event_precedes_the_tool_event(demo_repo: Path, tmp_path: Path) -> None:
    ctx = _ctx(demo_repo, tmp_path)
    result = execute(ctx, "read_file", {"path": "README.md"})
    assert result.ok is True
    tools = [e.tool for e in ctx.tracker.events]
    assert tools == ["tool_started", "read_file"]
    started = ctx.tracker.events[0]
    assert started.input["tool"] == "read_file"


def test_crashed_command_call_is_unknown_on_resume_and_never_rerun(
    demo_repo: Path, tmp_path: Path
) -> None:
    """崩溃切在 run_command 执行中:恢复后占位回执说"不许盲目重跑",且确实没有重跑。"""
    session = SessionState(TASK, "s-1")
    ctx = _ctx(demo_repo, tmp_path, session=session)
    snapshot = LoopSnapshot(
        stage="LOCALIZE",
        round_no=0,
        turn_no=1,
        task_id=TASK,
        # 崩溃前那本工具调用账(唯一来源;恢复由 restore_calls 带回内存)
        tool_calls=[
            {
                "call_id": "c-cmd",
                "name": "run_command",
                "stage": "LOCALIZE",
                "round_no": 0,
                "turn_no": 1,
                "answered": False,
                "status": CALL_RUNNING,
            }
        ],
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "issue"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "type": "function",
                        "id": "c-cmd",
                        "function": {
                            "name": "run_command",
                            "arguments": json.dumps(
                                {
                                    "profile_id": "pytest_collect",
                                    "parameters": {"target": "tests"},
                                }
                            ),
                        },
                    }
                ],
            },
        ],
    )

    class _Model:
        def __init__(self) -> None:
            self.seen: list[list[dict]] = []

        def complete(self, messages, tools):
            from app.llm.base import AssistantTurn, ToolCall

            self.seen.append([dict(m) for m in messages])
            return AssistantTurn(
                content="",
                tool_calls=[
                    ToolCall(
                        id="c-next", name=FINISH_TOOL, arguments={"success": True, "summary": "ok"}
                    )
                ],
                finish_reason="tool_calls",
            )

    model = _Model()
    outcome = run_plain_loop(
        ctx, model, "issue", max_turns=4, state_label="LOCALIZE", resume_snapshot=snapshot
    )
    assert outcome.finish_declared
    first_request = json.dumps(model.seen[0], ensure_ascii=False)
    assert "do not blindly rerun" in first_request, "unknown 的命令调用必须给出不许盲重跑的判据"
    assert "c-cmd" in first_request
    # 确实没有重跑:轨迹里没有 run_command 的执行事件(只有恢复记账本身)
    assert not any(e.tool == "run_command" for e in ctx.tracker.events)
    restored_events = [e for e in ctx.tracker.events if e.tool == "tool_calls_restored"]
    assert restored_events and restored_events[0].output_summary["unknown_calls"] == ["c-cmd"]


def test_apply_patch_orphan_points_at_candidate_state(demo_repo: Path, tmp_path: Path) -> None:
    """apply_patch 的孤儿回执指向 git_diff/候选判断,而不是"重来一次"。"""
    session = SessionState(TASK, "s-1")
    ctx = _ctx(demo_repo, tmp_path, session=session)
    snapshot = LoopSnapshot(
        stage="PROPOSE_PATCH",
        round_no=1,
        turn_no=2,
        task_id=TASK,
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "issue"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "type": "function",
                        "id": "p1",
                        "function": {
                            "name": "apply_patch",
                            "arguments": json.dumps(
                                {"patch_text": "*** Begin Patch\n*** End Patch"}
                            ),
                        },
                    }
                ],
            },
        ],
    )

    class _Model:
        def __init__(self) -> None:
            self.seen: list[list[dict]] = []

        def complete(self, messages, tools):
            from app.llm.base import AssistantTurn, ToolCall

            self.seen.append([dict(m) for m in messages])
            return AssistantTurn(
                content="",
                tool_calls=[
                    ToolCall(
                        id="n1", name=FINISH_TOOL, arguments={"success": True, "summary": "ok"}
                    )
                ],
                finish_reason="tool_calls",
            )

    model = _Model()
    run_plain_loop(
        ctx,
        model,
        "issue",
        max_turns=4,
        round_no=1,
        state_label="PROPOSE_PATCH",
        resume_snapshot=snapshot,
    )
    first_request = json.dumps(model.seen[0], ensure_ascii=False)
    assert "check git_diff" in first_request
    assert not any(e.tool == "apply_patch" for e in ctx.tracker.events), "孤儿补丁不得被重放"


def test_tool_started_events_share_the_trajectory_facts(demo_repo: Path, tmp_path: Path) -> None:
    """事件流与轨迹同源:tool_started 是轨迹行,不是第二条进度来源。"""
    ctx = _ctx(demo_repo, tmp_path)
    execute(ctx, "list_files", {})
    from app.api.events import classify_event, event_frame

    started = ctx.tracker.events[0]
    frame = event_frame(
        {
            "id": 1,
            "event_id": started.request_id,
            "tool": started.tool,
            "state": started.state,
            "input": started.input,
            "output_summary": started.output_summary,
        }
    )
    assert "event: tool_started" in frame
    assert classify_event(started.tool) == "tool_started"
