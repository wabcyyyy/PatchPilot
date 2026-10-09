"""S10a/F8:同引擎两臂的消融不变量。

F8 的缺陷是"agent 臂走 graph、one_shot 走 plain"的跨引擎混杂。修复后两臂同引擎、
同状态机、同门禁、同终局验收,唯一差异是登记的策略对象。本文件逐条钉住不变量:
共有阶段的模型看到的东西一致、one_shot 拿不到执行反馈、验证失败只有 agent 重试、
双跑复核两臂都遵守。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.evals.bugset import load_bug
from app.graph.runner import run_task_graph
from app.llm.fake import FakeLLM
from tests.conftest import block
from tests.test_graph import BUG_ROOT, _comment_diff, _fix_diff, _localize_script


class _RecordingFake(FakeLLM):
    """记录每次 complete 收到的 messages(供两臂共有阶段一致性断言)。"""

    def __init__(self, script: list[dict[str, Any]]):
        super().__init__(script)
        self.seen: list[list[dict[str, Any]]] = []

    def complete(self, messages, tools):
        self.seen.append([dict(m) for m in messages])
        return super().complete(messages, tools)


def _events(run_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in Path(run_dir, "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _fix_replay() -> list[dict[str, Any]]:
    return [
        {"tool": "apply_patch", "args": {"patch_text": block(_fix_diff())}},
        {"tool": "run_tests", "args": {"test_set": "failed"}},
        {"tool": "run_tests", "args": {"test_set": "regression"}},
        {"tool": "finish", "args": {"success": True, "summary": "fixed"}},
    ]


def _wrong_replay() -> list[dict[str, Any]]:
    return [
        {"tool": "apply_patch", "args": {"patch_text": block(_comment_diff())}},
        {"tool": "run_tests", "args": {"test_set": "failed"}},
        {"tool": "finish", "args": {"success": True, "summary": "wrong"}},
    ]


def test_shared_stage_messages_are_identical_across_arms(tmp_path: Path) -> None:
    """两臂共有阶段(LOCALIZE/PLAN)的模型消息逐字一致——差异只允许出现在 PROPOSE。"""
    bug = load_bug("BUG-001", BUG_ROOT)
    agent_model = _RecordingFake(_localize_script() + _fix_replay())
    run_task_graph(bug, agent_model, runs_root=tmp_path / "agent", arm="agent")

    one_shot_model = _RecordingFake(_localize_script() + _fix_replay())
    run_task_graph(bug, one_shot_model, runs_root=tmp_path / "one-shot", arm="one_shot")

    assert len(agent_model.seen) == len(one_shot_model.seen)
    for index, (a, b) in enumerate(zip(agent_model.seen, one_shot_model.seen, strict=False)):
        if index < 2:  # LOCALIZE + PLAN(回放共享阶段):逐字一致
            assert a == b, f"共有阶段第 {index} 次请求不一致"


def test_one_shot_cannot_invoke_run_tests(tmp_path: Path) -> None:
    """对照臂在 PROPOSE 阶段调 run_tests → 工具白名单拒绝(执行反馈通道关闭)。"""
    bug = load_bug("BUG-001", BUG_ROOT)
    model = FakeLLM(
        [
            *_localize_script(),
            {"tool": "apply_patch", "args": {"patch_text": block(_fix_diff())}},
            {"tool": "run_tests", "args": {"test_set": "failed"}},
            {"tool": "finish", "args": {"success": True, "summary": "试图自测"}},
        ]
    )
    result = run_task_graph(bug, model, runs_root=tmp_path / "os", arm="one_shot")
    events = _events(Path(result.run_dir))
    blocked = [
        e
        for e in events
        if e["tool"] == "run_tests" and (e.get("input") or {}).get("blocked") is True
    ]
    assert blocked, "one_shot 的 run_tests 必须被工具白名单拦截"


def test_verification_failure_retries_only_for_agent(tmp_path: Path) -> None:
    """同一份"修不好"的回放:agent 进入第二轮重试,one_shot 首次验证失败即终局。"""
    bug = load_bug("BUG-001", BUG_ROOT)
    agent = run_task_graph(
        bug,
        FakeLLM(_localize_script() + _wrong_replay() + _fix_replay()),
        runs_root=tmp_path / "agent",
        max_rounds=2,
        arm="agent",
    )
    assert agent.status == "FINISHED" and agent.rounds == 2, "agent 臂:首次失败后必须重试"

    one_shot = run_task_graph(
        bug,
        FakeLLM(_localize_script() + _wrong_replay()),
        runs_root=tmp_path / "one-shot",
        max_rounds=2,
        arm="one_shot",
    )
    assert one_shot.status == "VERIFY_FAILED"
    assert "one_shot policy" in (one_shot.error or "")
    assert one_shot.rounds == 1, "对照臂不消耗第二轮"
    names = [e["tool"] for e in _events(Path(one_shot.run_dir))]
    assert "one_shot_stop" in names and "reset_workspace" not in names, "不 reset:证据原样保留"


def test_double_run_consistency_honored_by_both_arms(tmp_path: Path) -> None:
    """双跑复核是共享验收的一部分:两臂在双双转绿的路径上都真跑 verify_double_run。"""
    bug = load_bug("BUG-001", BUG_ROOT)
    replay = _localize_script() + _fix_replay()
    agent = run_task_graph(bug, FakeLLM(replay), runs_root=tmp_path / "agent", arm="agent")
    one_shot = run_task_graph(bug, FakeLLM(replay), runs_root=tmp_path / "one-shot", arm="one_shot")
    for result in (agent, one_shot):
        assert result.status == "FINISHED" and result.verdict == "resolved"
        names = [e["tool"] for e in _events(Path(result.run_dir))]
        assert "verify_double_run" in names, "两臂的 resolved 都必须经过双跑复核"
