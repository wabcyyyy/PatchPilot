"""S02b:持久化时间预算与执行边界(ADR-0009 §3)。

核心口径:deadline 是执行启动时建立并持久化进 checkpoint 的**墙钟时刻**;
恢复沿用原值的剩余时间(停机间隔计入,不重授 900s);每次 run_pytest 前后复查,
最后一次 rerun 之后越过截止时刻同样不得 resolved。
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.adapters.pytest_adapter import PytestReport
from app.evals.bugset import load_bug
from app.evals.driver import run_task
from app.gitops.testing import materialize_repo
from app.graph.builder import build_graph
from app.graph.checkpoint import make_sqlite_checkpointer
from app.graph.nodes import TaskNodes
from app.graph.runner import run_task_graph
from app.llm.fake import FakeLLM
from app.tools.tracker import Tracker

BUG_ROOT = Path("bugs")


def _seed_checkpoint_at(
    tmp_path: Path,
    *,
    next_node: str,
    deadline_epoch: float | None,
) -> tuple[Path, str]:
    """造一个合法检查点(可选 deadline 字段,模拟新/旧格式)并返回 (run_dir, baseline)。"""
    bug = load_bug("BUG-001", BUG_ROOT)
    run_dir = tmp_path / "run"
    workspace = run_dir / "workspace"
    baseline = materialize_repo(bug.repo_dir, workspace, extra_commit=False)
    checkpointer = make_sqlite_checkpointer(run_dir / "checkpoints.sqlite")
    assert checkpointer is not None
    nodes = TaskNodes(
        bug,
        FakeLLM([]),
        workspace,
        Tracker(run_dir / "trajectory.jsonl", task_id="T-TB"),
        run_dir / "reports",
        bug.max_rounds,
        20,
    )
    graph = build_graph(nodes, checkpointer=checkpointer)
    state = {
        "bug_id": bug.id,
        "issue_text": bug.issue_text,
        "failed_tests": bug.failed_tests,
        "regression_tests": bug.regression_tests,
        "baseline_commit": baseline,
        "max_rounds": bug.max_rounds,
        "round_no": 1,
        "status": "LOCALIZE",
        "tokens_used": 100,
        "turns": 1,
    }
    if deadline_epoch is not None:
        state["deadline_epoch"] = deadline_epoch
    config = {"configurable": {"thread_id": "T-TB"}}
    graph.update_state(config, state, as_node="baseline")
    checkpointer.conn.close()
    return run_dir, baseline


def test_resume_honors_persisted_deadline_and_does_not_regrant(tmp_path: Path) -> None:
    """恢复沿用持久化 deadline 的剩余时间:已过截止时刻的检查点续跑即判超时,
    而不是领到一份新的 900s。"""
    run_dir, _ = _seed_checkpoint_at(tmp_path, next_node="localize", deadline_epoch=time.time() - 5)
    bug = load_bug("BUG-001", BUG_ROOT)
    result = run_task_graph(
        bug,
        FakeLLM([{"tool": "finish", "args": {"success": True, "summary": "x"}}]),
        runs_root=tmp_path,
        task_id="T-TB",
        run_dir=run_dir,
        resume=True,
    )
    assert result.status == "BUDGET_EXCEEDED", (result.status, result.error)
    assert "deadline" in (result.error or ""), result.error
    events = [
        __import__("json").loads(line)
        for line in (run_dir / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    names = [e["tool"] for e in events]
    assert "resume_from_checkpoint" in names, "走的是恢复路径,不是冷启动"
    assert names.count("create_workspace") <= 1, "恢复不得重新物化工作区(冷启动特征)"


def test_resume_without_persisted_deadline_converges_needs_review(tmp_path: Path) -> None:
    """旧格式检查点没有可信 deadline:拒绝恢复(判 NEEDS_REVIEW),不冷启动重授额度。"""
    run_dir, _ = _seed_checkpoint_at(tmp_path, next_node="localize", deadline_epoch=None)
    bug = load_bug("BUG-001", BUG_ROOT)
    result = run_task_graph(
        bug,
        FakeLLM([]),
        runs_root=tmp_path,
        task_id="T-TB",
        run_dir=run_dir,
        resume=True,
    )
    assert result.status == "NEEDS_REVIEW", (result.status, result.error)
    assert "resume rejected" in (result.error or ""), result.error
    events = [
        __import__("json").loads(line)
        for line in (run_dir / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    unavailable = [e for e in events if e["tool"] == "resume_unavailable"]
    assert unavailable and "deadline" in unavailable[-1]["input"].get("reason", "")


def _green_report(requested: list[str]) -> PytestReport:
    return PytestReport(
        exit_code=0,
        passed=len(requested),
        collected=len(requested),
        requested_ids=list(requested),
        case_results=[("tests/test_a.py", "tests.test_a", "test_x", "passed")],
    )


def test_verify_after_last_rerun_over_deadline_keeps_evidence_not_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spec 必测"最后复核越时":双跑两次 rerun 跑完之后越过截止时刻——
    已有测试证据保留(verify 双 True),资源终态 exhausted,不得 resolved。"""
    clock = {"now": time.time()}
    monkeypatch.setattr("app.graph.nodes.time", SimpleNamespace(time=lambda: clock["now"]))
    monkeypatch.setattr(
        "app.graph.nodes.get_settings",
        lambda: SimpleNamespace(task_timeout_seconds=900, verify_double_run=True),
    )
    green = _green_report(["tests/test_a.py::test_x"])

    calls = {"n": 0}

    def _fake_pytest(*args: object, **kwargs: object) -> tuple[PytestReport, None]:
        calls["n"] += 1
        if calls["n"] >= 3:  # 第 3 次是 failed 集 rerun:跑完把时钟推过截止时刻
            clock["now"] += 10_000
        return green, None

    monkeypatch.setattr("app.graph.nodes.run_pytest", _fake_pytest)
    nodes = TaskNodes(
        bug=SimpleNamespace(failed_tests=[], regression_tests=[]),
        model=None,  # type: ignore[arg-type]
        workspace=tmp_path,
        tracker=Tracker(None, task_id="T-RERUN"),
        report_dir=tmp_path,
        max_rounds=2,
        max_turns=5,
        deadline_epoch=clock["now"] + 900,
    )
    nodes.ctx = SimpleNamespace(python_exe="python", env=None)  # type: ignore[assignment]

    update = nodes.verify({"round_no": 1})  # type: ignore[arg-type]
    assert update["status"] == "BUDGET_EXCEEDED"
    assert update["verify_failed_ok"] is True and update["verify_regression_ok"] is True
    assert update["resource_status"] == "exhausted"
    assert nodes.route_verify({"status": "BUDGET_EXCEEDED"}) == "end"  # type: ignore[arg-type]


def test_plain_driver_verify_skipped_when_deadline_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """plain 驱动器:deadline 已过时验证段一次 pytest 都不再发起。"""
    import app.evals.driver as driver_mod

    clock = {"now": time.time()}
    real_monotonic = time.monotonic
    monkeypatch.setattr(
        driver_mod,
        "time",
        SimpleNamespace(time=lambda: clock["now"], monotonic=real_monotonic),
    )
    monkeypatch.setattr(
        driver_mod,
        "get_settings",
        lambda: SimpleNamespace(
            llm_enabled=False,
            token_budget=200_000,
            llm_max_tokens=0,
            task_timeout_seconds=900,
            test_timeout_seconds=120,
            max_patch_files=5,
            max_read_lines=400,
            max_search_results=50,
            execution_backend="local",
            verify_double_run=True,
        ),
    )
    red = PytestReport(
        exit_code=1,
        passed=0,
        failed=1,
        collected=1,
        requested_ids=["tests/test_a.py::test_x"],
        case_results=[("tests/test_a.py", "tests.test_a", "test_x", "failure")],
    )
    green = _green_report(["tests/test_a.py::test_x"])

    def _pytest_by_set(python_exe, cwd, test_ids, report_path, **kwargs):
        if "baseline-regression" in str(report_path):
            return green, None
        return red, None

    monkeypatch.setattr(driver_mod, "run_pytest", _pytest_by_set)

    def _agent(ctx, model, b, **kwargs):
        clock["now"] += 10_000  # 补丁段把时间烧穿:verify 入口必须拦住
        from app.graph.plain_loop import LoopOutcome

        return LoopOutcome(
            success=True,
            summary="s",
            turns=1,
            tokens_used=10,
            patch_applied=True,
            finish_declared=True,
        )

    ws_src = tmp_path / "src-repo"
    materialize_repo(BUG_ROOT / "BUG-001" / "repo", ws_src, extra_commit=False)
    real_bug = load_bug("BUG-001", BUG_ROOT)
    bug = SimpleNamespace(
        id="BUG-STUB",
        repo_dir=ws_src,
        issue_text="x",
        failed_tests=real_bug.failed_tests,
        regression_tests=real_bug.regression_tests,
        allowed_paths=None,
        max_rounds=2,
        env=None,
        test_sets={"failed": real_bug.failed_tests, "regression": real_bug.regression_tests},
    )

    class _DummyModel:
        provider = "fake-replay"

    result = run_task(bug, _DummyModel(), runs_root=tmp_path / "runs", engine="plain", agent=_agent)
    assert result.status == "BUDGET_EXCEEDED", (result.status, result.error)
    assert result.resource_status == "exhausted"
    reports_dir = Path(result.run_dir) / "reports"
    assert not (reports_dir / "verify-failed.xml").exists(), "deadline 已过,verify 不得发起"
