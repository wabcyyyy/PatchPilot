"""Regression cases found while reviewing the engineering upgrade."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.context.evidence import EvidenceRef, EvidenceStore
from app.delivery.run_artifacts import export_run_artifacts
from app.gitops.snapshot import create_workspace
from app.graph.resume import prepare_resume
from app.tools.registry import execute, model_payload
from tests.test_engineering_upgrade import _ctx
from tests.test_resume import _StubGraph


def test_evidence_write_failure_preserves_successful_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path)
    execute(ctx, "read_file", {"path": "a.py"})

    def denied(self: EvidenceStore, *args: Any, **kwargs: Any) -> EvidenceRef:
        raise PermissionError("evidence directory is read-only")

    monkeypatch.setattr(EvidenceStore, "save", denied)
    patch = (
        "*** Begin Patch\n*** Update File: a.py\n-value = 'a'\n+value = 'fixed'\n*** End Patch\n"
    )
    result = execute(ctx, "apply_patch", {"patch_text": patch})
    assert result.ok and "fixed" in (ctx.workspace / "a.py").read_text()
    payload = model_payload(result)
    assert payload["evidence"]["retrievable"] is False
    assert "PermissionError" in payload["evidence"]["reason"]
    assert ctx.memory is not None and ctx.memory.evidence[0].stale
    assert any(event.tool == "apply_patch" for event in ctx.tracker.events)


def test_large_receipt_write_failure_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import get_settings

    ctx = _ctx(tmp_path)
    (ctx.workspace / "a.py").write_text("value = '" + "x" * 12000 + "'\n", encoding="utf-8")
    monkeypatch.setattr(get_settings(), "tool_visible_max_chars", 1000)

    def full(self: EvidenceStore, *args: Any, **kwargs: Any) -> EvidenceRef:
        raise OSError("disk full")

    monkeypatch.setattr(EvidenceStore, "save", full)
    result = execute(ctx, "read_file", {"path": "a.py"})
    assert result.ok
    assert not model_payload(result)["evidence"]["retrievable"]


def test_resume_reset_invalidates_only_changed_dependencies(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    run_dir = tmp_path / "run"
    ctx.baseline_commit = create_workspace(ctx.workspace, run_dir / "workspace")
    ctx.workspace = run_dir / "workspace"
    ctx.workspace_revision = 7
    (ctx.workspace / "a.py").write_text("value = 'partial'\n", encoding="utf-8")
    for path in ("a.py", "b.py"):
        execute(ctx, "read_file", {"path": path})
    nodes = SimpleNamespace(
        ctx=ctx,
        tracker=ctx.tracker,
        memory=ctx.memory,
        session=None,
        runtime=None,
        resume_snapshot=None,
        restore_runtime=lambda state, baseline: None,
    )
    graph = _StubGraph(
        {
            "baseline_commit": ctx.baseline_commit,
            "round_no": 1,
            "deadline_epoch": time.time() + 300,
        },
        "propose",
    )
    decision = prepare_resume(
        nodes, graph, run_dir=run_dir, config={"recursion_limit": 100}, task_id=ctx.task_id
    )
    assert decision.action == "continue"
    assert ctx.workspace_revision == 8
    assert ctx.memory is not None
    assert ctx.memory.evidence[0].stale and not ctx.memory.evidence[1].stale
    assert (ctx.workspace / "a.py").read_text() == "value = 'a'\n"


@pytest.mark.parametrize("verified,expected", [(False, "inconclusive"), (True, "passed")])
def test_green_xml_requires_platform_identity_confirmation(
    tmp_path: Path, verified: bool, expected: str
) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "verify-failed.xml").write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0">'
        '<testcase classname="tests.test_a" name="test_a"/></testsuite>',
        encoding="utf-8",
    )
    export_run_artifacts(tmp_path, {"task_id": "partial", "verify_failed_ok": verified})
    evaluation = json.loads((tmp_path / "evaluation_report.json").read_text())
    assert evaluation["target_tests"]["status"] == expected
    assert evaluation["regression_tests"]["status"] == "not_run"
