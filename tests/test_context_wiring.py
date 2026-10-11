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

import json
from pathlib import Path
from typing import Any

import pytest

from app.config import get_settings
from app.errors import BudgetError
from app.evals.bugset import load_bug
from app.gitops.testing import materialize_repo
from app.graph.loop_state import load_loop_snapshot
from app.graph.plain_loop import run_plain_loop
from app.graph.runner import run_task_graph
from app.llm.fake import FakeLLM
from app.llm.profiles import SOURCE_PROBED
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


# ---------- G07:声明容量、运行 profile 与"压不动的那一段" ----------


def test_declared_capacity_rejects_an_oversized_protected_region_before_thrashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPEC §5:保护区本身超出声明容量的可用余量时给 context_capacity_exceeded,不反复压缩。

    判据排在压缩**之前**:压不动的东西压一遍只会白烧一次额度再原地拒绝,而"反复压缩
    直至耗尽预算"正是这条卡要堵死的失效模式。
    """
    monkeypatch.setattr(get_settings(), "loop_snapshot_enabled", False)
    workspace, baseline = _big_repo(tmp_path)
    ctx = _ctx(workspace, baseline, tmp_path)
    model = FakeLLM(_read_script(10))

    with pytest.raises(BudgetError, match="context_capacity_exceeded"):
        run_plain_loop(
            ctx,
            model,
            "解析空字符串应当返回 None",
            max_turns=14,
            state_label="LOCALIZE",
            context_window_tokens=1_500,
            context_keep_recent_turns=2,
            capacity_ceiling_tokens=200,  # 连系统提示 + 首条 issue 都装不下
        )

    events = [e for e in ctx.tracker.events if e.tool == "context_capacity_exceeded"]
    assert len(events) == 1, "拒绝一次就收,不在每个 turn 边界重复空转"
    assert events[0].output_summary["protected_tokens"] > 200
    assert not [e for e in ctx.tracker.events if e.tool == "context_compact"], (
        "压缩必须排在容量判据之后:先压再拒就是把额度花在注定装不下的请求上"
    )


def test_a_generous_declared_capacity_leaves_todays_path_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """对照臂:同一份历史,容量足够时就该照常压缩、照常收束 —— 这条拒绝不是新增的全局闸。"""
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
        context_window_tokens=1_500,
        context_keep_recent_turns=2,
        capacity_ceiling_tokens=100_000,
    )
    assert [e.tool for e in ctx.tracker.events].count("context_compact") > 0
    assert outcome.finish_declared, "容量足够时,G07 不许改变压缩与收束的既有行为"


def test_repair_v2_profile_reaches_the_graph_and_the_task_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T12 端到端:目标 64k 落在一个声明了 6k 容量的端点上,请求前就被拒。

    同时钉三件读数,少一件都算"写成愿望":
    - 终态与理由串(区分"钱花光了"与"这一发本来就装不下");
    - TaskSpec 里**实际采用**的窗口(不是配置里的 64k)与被夹过这件事;
    - 轨迹里的 runtime_profile 事件(报告面能查到这一跑用的是哪套口径)。
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "agent_runtime_profile", "repair_v2")
    monkeypatch.setattr(settings, "model_context_tokens", 6_000)
    monkeypatch.setattr(settings, "model_capacity_source", SOURCE_PROBED)

    bug = load_bug("BUG-001", BUG_ROOT)
    run_dir = tmp_path / "run-v2"
    model = FakeLLM(_localize_script() + _propose_script(_fix_diff()))
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs", run_dir=run_dir)

    assert result.status == "BUDGET_EXCEEDED", (result.status, result.error)
    assert "context_capacity_exceeded" in (result.error or "")

    spec = json.loads((run_dir / "task_spec.json").read_text(encoding="utf-8"))
    profile = spec["effective_policy"]["runtime_profile"]
    assert profile["profile_name"] == "repair_v2"
    assert profile["window"]["target_tokens"] == 64_000
    assert profile["window"]["adopted_tokens"] == 6_000 - 4_096 - 1_024
    assert profile["window"]["clamped_by_capacity"] is True
    assert profile["model"]["capacity_source"] == SOURCE_PROBED

    events = [
        json.loads(line)
        for line in (run_dir / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    recorded = [e for e in events if e["tool"] == "runtime_profile"]
    assert len(recorded) == 1, "每任务一次:多了就是阶段重跑,没了就是报告查不到口径"
    assert recorded[0]["output_summary"]["window"]["clamped_by_capacity"] is True


def test_legacy_v1_profile_does_not_start_doing_capacity_checks(
    tmp_path: Path,
) -> None:
    """默认仍是 legacy_v1:没有声明容量就没有容量比较,图路径与升级前同形。

    SPEC §7 G07 明写"新默认切换须到 G16 收口后"。这条用例是那颗钉子的行为版本 ——
    哪天有人把默认 profile 改成 repair_v2,或者给 legacy 路径接上保护区判据,这里立刻红。
    (对照:上一条 repair_v2 用例在同一个循环代码上因为容量判据而 BUDGET_EXCEEDED。)
    """
    bug = load_bug("BUG-001", BUG_ROOT)
    run_dir = tmp_path / "run-v1"
    model = FakeLLM(_localize_script() + _propose_script(_fix_diff()))
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs", run_dir=run_dir)

    assert result.verdict == "resolved", (result.status, result.error)
    spec = json.loads((run_dir / "task_spec.json").read_text(encoding="utf-8"))
    profile = spec["effective_policy"]["runtime_profile"]
    assert profile["profile_name"] == "legacy_v1"
    assert profile["window"]["capacity_checked"] is False
    assert profile["window"]["capacity_ceiling_tokens"] == 0, "legacy 不做容量比较"
    assert profile["model"]["capacity_source"] == "unverified"


def test_loop_snapshot_binds_the_profile_that_wrote_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """崩溃前那套窗口/轮数/输出预留口径要能被恢复方查出来:profile 名进快照。"""
    workspace, baseline = _big_repo(tmp_path)
    # 快照落在轨迹同目录,所以这里必须是**有文件位置**的 Tracker(内存桩没有目录可言)
    ctx = ToolContext(
        task_id="T-WIRING",
        workspace=workspace,
        baseline_commit=baseline,
        tracker=Tracker(tmp_path / "trajectory.jsonl", task_id="T-WIRING"),
        report_dir=tmp_path / "reports",
        test_sets={},
        allowed_paths=None,
    )
    monkeypatch.setattr(get_settings(), "loop_snapshot_enabled", True)
    model = FakeLLM(_read_script(6))

    with pytest.raises(BudgetError):
        run_plain_loop(
            ctx,
            model,
            "解析空字符串应当返回 None",
            max_turns=2,
            state_label="LOCALIZE",
            context_window_tokens=1_500,
            context_keep_recent_turns=2,
            runtime_profile="repair_v2",
        )

    snapshot = load_loop_snapshot(ctx, stage="LOCALIZE", round_no=0)
    assert snapshot is not None
    assert snapshot.runtime_profile == "repair_v2"
