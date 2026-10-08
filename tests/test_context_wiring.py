"""上下文机制在**图路径**上的接线验证。

M1(压缩)与 M2(仓库骨架)都有纯函数级与循环级用例,但"从 Settings 走到真实图执行、
再落到轨迹事件与提示词"这条链此前只有源码级断言。这里补两条行为证据:
1. 阈值经 `run_plain_loop` 走进图执行,并在轨迹里留下可核对的 `context_compact` 事件;
2. 仓库骨架真的出现在发给模型的那次 PLAN 请求里(计划段要能点名文件与符号)。

阈值不在这里标定:fake 语料每题只 7-9 轮,自然到不了生产默认 16000,
所以第 1 条用显式小阈值证明"链路通";默认值的实际效果要真实模型批才能主张
(见 ADR-0004 的反方条目与 `docs/adr/0007` 的"未证明"清单)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.config import get_settings
from app.evals.bugset import load_bug
from app.gitops.testing import materialize_repo
from app.graph.plain_loop import run_plain_loop
from app.graph.runner import run_task_graph
from app.llm.fake import FakeLLM
from app.prompts import PLAN_MARKER
from app.tools.base import ToolContext
from app.tools.tracker import Tracker
from tests.test_graph import BUG_ROOT, _fix_diff, _localize_script, _propose_script

TARGET = "src/dateparse.py"


def _ctx(workspace: Path, baseline_commit: str, tmp_path: Path) -> ToolContext:
    return ToolContext(
        task_id="T-WIRING",
        workspace=workspace,
        baseline_commit=baseline_commit,
        tracker=Tracker(None, task_id="T-WIRING"),
        report_dir=tmp_path / "reports",
        test_sets={},
        allowed_paths=None,
    )


def _big_repo(tmp_path: Path) -> tuple[Path, str]:
    """一个 400 行的文件(单次 read_file 折叠后仍有 ~3k 字符,便于用小阈值触发压缩),
    并且是**真 git 工作区**。

    `run_plain_loop` 收尾要用 `working_tree_diff` 判定 patch_applied,非 git 目录会当场抛
    GitCmdError。本卡初版把这里写成普通临时目录,成了一条与临时目录位置相关的假通过
    (机制与复盘见 PROGRESS 的 M10 卡),夹具必须与 basetemp 落在哪儿无关。
    """
    template = tmp_path / "repo-src"
    (template / "src").mkdir(parents=True)
    body = "\n".join(f"value_{index} = {index}" for index in range(400))
    (template / TARGET).write_text(f"{body}\n", encoding="utf-8")
    dest = tmp_path / "repo"
    return dest, materialize_repo(template, dest, extra_commit=False)


def _read_script(turns: int) -> list[dict[str, Any]]:
    script: list[dict[str, Any]] = [
        {"tool": "read_file", "args": {"path": TARGET, "offset": 1}} for _ in range(turns)
    ]
    script.append({"tool": "finish", "args": {"success": True, "summary": "读完了"}})
    return script


def test_compaction_engages_through_the_loop_and_records_a_trajectory_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """链路:Settings 阈值 → run_plain_loop → 压缩 → 轨迹事件,且循环仍能收束。"""
    monkeypatch.setattr(get_settings(), "loop_snapshot_enabled", False)
    workspace, baseline = _big_repo(tmp_path)
    ctx = _ctx(workspace, baseline, tmp_path)
    model = FakeLLM(_read_script(10))

    outcome = run_plain_loop(
        ctx,
        model,
        "解析空字符串应当返回 None",
        max_turns=14,
        state_label="LOCALIZE",
        context_window_tokens=1500,
        context_keep_recent_turns=2,
    )

    events = [e for e in ctx.tracker.events if e.tool == "context_compact"]
    assert events, "超过阈值必须压缩并留轨迹事件,否则默认值形同虚设"
    summary: dict[str, Any] = events[0].output_summary
    assert summary["stubbed"] > 0
    assert summary["state"] == "LOCALIZE"
    assert outcome.finish_declared, "压缩不能把循环打成不可收束"
    assert outcome.patch_applied is False, "只读轨迹不该被判定成打过补丁(这一步真跑 git)"


def test_skeleton_reaches_the_model_in_the_plan_stage(tmp_path: Path) -> None:
    """图路径行为证据:PLAN 那次请求的 system 里必须有仓库骨架与真实符号名。"""
    captured: list[list[dict[str, Any]]] = []

    class ProbeLLM(FakeLLM):
        def complete(self, messages, tools):  # type: ignore[no-untyped-def]
            captured.append([dict(m) for m in messages])
            return super().complete(messages, tools)

    bug = load_bug("BUG-001", BUG_ROOT)
    model = ProbeLLM(_localize_script() + _propose_script(_fix_diff()))
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs")

    assert result.verdict == "resolved", (result.status, result.error)
    plan_requests = [
        messages
        for messages in captured
        if any(PLAN_MARKER in str(m.get("content", "")) for m in messages)
    ]
    assert plan_requests, "计划段必须真的发过一次带标记的请求"
    system = str(plan_requests[0][0]["content"])
    assert "<repo_skeleton>" in system, "计划段没拿到骨架就只能写「改那个模块」这类空话"
    assert "def parse_date(" in system, "骨架里要有真符号名才能点名要改谁"


def test_disabled_switches_keep_the_old_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两个新机制同时关闭时,图路径必须与引入它们之前同形(判定与补丁落点都不动)。"""
    monkeypatch.setattr(get_settings(), "repo_map_enabled", False)
    monkeypatch.setattr(get_settings(), "context_window_tokens", 0)
    monkeypatch.setattr(get_settings(), "plan_stage_enabled", False)
    monkeypatch.setattr(get_settings(), "loop_snapshot_enabled", False)

    bug = load_bug("BUG-001", BUG_ROOT)
    model = FakeLLM(_localize_script() + _propose_script(_fix_diff()))
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs")

    assert result.verdict == "resolved", (result.status, result.error)
    assert result.changed_files == [TARGET]


def test_plan_marker_constant_is_exported() -> None:
    """`[[plan_stage]]` 由脚本模型与提示词共用一个常量——两处各写一份字面量迟早漂移。"""
    assert PLAN_MARKER == "[[plan_stage]]"
