"""Engineering upgrade: exercise the real tool, prompt, retry and artifact paths offline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.adapters.pytest_adapter import FailedCase, PytestReport
from app.adapters.pytest_output import PytestOutputParser
from app.context.evidence_lifecycle import invalidate_evidence
from app.context.evidence_messages import compact_evidence_messages
from app.context.task_memory import TaskMemory
from app.gitops.blockpatch import BlockPatchError, compile_block_patch, parse_block_patch
from app.gitops.cmd import run_git
from app.gitops.snapshot import create_workspace
from app.graph.attempts import negative_memory, repeated_patch
from app.tools.base import ToolContext
from app.tools.registry import execute, model_payload
from app.tools.symbols import find_symbol
from app.tools.tracker import Tracker


def _ctx(tmp_path: Path) -> ToolContext:
    ws = tmp_path / "ws"
    ws.mkdir()
    for name in ("a", "b"):
        (ws / f"{name}.py").write_text(f"value = '{name}'\n", encoding="utf-8")
    run_git(ws, "init")
    run_git(ws, "config", "user.name", "Test")
    run_git(ws, "config", "user.email", "test@example.com")
    run_git(ws, "add", ".")
    run_git(ws, "commit", "-m", "baseline")
    _, commit, _ = run_git(ws, "rev-parse", "HEAD")
    return ToolContext(
        task_id="upgrade",
        workspace=ws,
        baseline_commit=commit.strip(),
        tracker=Tracker(tmp_path / "trajectory.jsonl", task_id="upgrade"),
        report_dir=tmp_path / "reports",
        memory=TaskMemory("upgrade"),
    )


def test_symbol_details_include_exact_source_and_annotations(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    source = '@classmethod\nasync def build(x: str, *, n: int = 2) -> str:\n    """Build a value."""\n    return x * n\n'
    (ctx.workspace / "a.py").write_text(source, encoding="utf-8")
    result = find_symbol(ctx, "build", include_details=True)
    item = result.output["matches"][0]
    assert item["start_line"] == 1 and item["end_line"] == 4
    assert item["docstring"] == "Build a value."
    assert "x: str" in item["signature"] and "-> str" in item["signature"]
    assert item["snippet"] == source.rstrip()


def test_block_syntax_rejected_without_writing_any_file(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    patch = "*** Begin Patch\n*** Update File: a.py\n-value = 'a'\n+def broken(:\n*** Add File: notes.txt\n+should not exist\n*** End Patch\n"
    with pytest.raises(BlockPatchError, match=r"a\.py:1:"):
        compile_block_patch(ctx.workspace, parse_block_patch(patch), validate_python=True)
    result = execute(ctx, "apply_patch", {"patch_text": patch})
    assert not result.ok and "python_syntax_error" in str(result.error)
    assert (ctx.workspace / "a.py").read_text() == "value = 'a'\n"
    assert not (ctx.workspace / "notes.txt").exists()
    assert ctx.workspace_revision == 0


def test_hash_invalidation_preserves_other_files_and_retrieval(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    assert ctx.memory is not None
    for path in ("a.py", "b.py"):
        assert execute(ctx, "read_file", {"path": path}).ok
    a, b = ctx.memory.evidence
    assert a.file_hash and a.evidence_id and a.retrievable
    patch = (
        "*** Begin Patch\n*** Update File: a.py\n-value = 'a'\n+value = 'changed'\n*** End Patch\n"
    )
    assert execute(ctx, "apply_patch", {"patch_text": patch}).ok
    assert a.is_stale and not b.is_stale
    old = execute(ctx, "read_evidence", {"evidence_id": a.evidence_id})
    unchanged = execute(ctx, "read_evidence", {"evidence_id": b.evidence_id})
    assert old.output["stale"] and not unchanged.output["stale"]
    assert any(e.tool == "evidence_invalidated" for e in ctx.tracker.events)
    # Reading an old receipt must never relabel that content as current code.
    assert ctx.memory.evidence[-2].stale


def test_rollback_invalidates_candidate_observations(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    (ctx.workspace / "a.py").write_text("value = 'candidate'\n", encoding="utf-8")
    execute(ctx, "read_file", {"path": "a.py"})
    assert execute(ctx, "reset_workspace", {}).ok
    assert ctx.memory is not None and ctx.memory.evidence[0].stale


def test_messages_fold_stale_or_old_results_and_keep_pairing(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    receipt = execute(ctx, "read_file", {"path": "a.py"})
    assert ctx.memory is not None
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "rules"},
        {"role": "assistant", "tool_calls": [{"id": "read", "function": {"name": "read_file"}}]},
        {"role": "tool", "tool_call_id": "read", "content": json.dumps(model_payload(receipt))},
        {"role": "assistant", "content": "next"},
    ]
    aged = compact_evidence_messages(messages, ctx.memory, keep_recent_turns=1)
    assert "旧回合输出已折叠" in str(aged[2]["content"])
    (ctx.workspace / "a.py").write_text("value = 42\n", encoding="utf-8")
    ctx.workspace_revision += 1
    invalidate_evidence(ctx, trigger="test")
    stale = compact_evidence_messages(messages, ctx.memory, keep_recent_turns=10)
    assert "旧观测已失效" in str(stale[2]["content"])
    assert "E1" in str(stale[0]["content"])
    assert stale[2]["tool_call_id"] == "read" and len(stale) == len(messages)
    embedded = [{"role": "system", "content": "rules\n\n[evidence_status]\nrepository text"}]
    amended = compact_evidence_messages(embedded, ctx.memory, keep_recent_turns=10)
    assert "repository text" in str(amended[0]["content"])
    restored = TaskMemory.from_snapshot(ctx.memory.snapshot())
    assert restored is not None and restored.state_hash() == ctx.memory.state_hash()


def test_pytest_parser_keeps_assertion_and_bounds_total_payload(tmp_path: Path) -> None:
    trace = (
        f'  File "{tmp_path / "a.py"}", line 1, in f\n    return 0\n'
        '  File "/opt/site-packages/lib.py", line 2, in call\n    noise()\n'
        + "\n".join(f"detail {i}" for i in range(80))
        + "\nAssertionError: expected 1, got 0\nE assert 0 == 1"
    )
    report = PytestReport(
        exit_code=1,
        failed=30,
        failed_cases=[
            FailedCase(
                f"test_{i}",
                f"tests/test_a.py::test_{i}",
                "failure",
                "AssertionError: wrong",
                "failure: wrong",
                trace,
            )
            for i in range(30)
        ],
    )
    diagnosis = PytestOutputParser(tmp_path, max_chars=2500).parse(report)
    assert diagnosis["failure_count"] == 30
    assert diagnosis["omitted_cases"] > 0
    assert len(json.dumps(diagnosis, ensure_ascii=False)) <= 2500
    first = diagnosis["failed_cases"][0]
    assert first["error_type"] == "AssertionError" and "assert 0 == 1" in first["traceback"]
    assert "site-packages" not in first["traceback"]


def test_mixed_patch_syntax_rejection_restores_non_python_files(
    demo_repo: Path, tmp_path: Path
) -> None:
    from app.gitops.differ import working_tree_diff
    from app.gitops.patcher import apply_patch

    ws1, ws2 = tmp_path / "source", tmp_path / "target"
    create_workspace(demo_repo, ws1)
    create_workspace(demo_repo, ws2)
    (ws1 / "notes.txt").write_text("new notes\n", encoding="utf-8")
    (ws1 / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    result = apply_patch(ws2, working_tree_diff(ws1).diff_text)
    assert not result.applied
    assert not (ws2 / "notes.txt").exists() and not (ws2 / "broken.py").exists()


def test_graph_failure_memory_reaches_next_plan_and_exports_real_results(tmp_path: Path) -> None:
    from app.evals.bugset import load_bug
    from app.graph.runner import run_task_graph
    from app.llm.base import AssistantTurn
    from app.llm.fake import FakeLLM
    from scripts.sample_run import sample_script

    requests: list[str] = []

    class CapturingLLM(FakeLLM):
        def complete(
            self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
        ) -> AssistantTurn:
            requests.append(json.dumps(messages, ensure_ascii=False))
            return super().complete(messages, tools)

    result = run_task_graph(
        load_bug("BUG-001"),
        CapturingLLM(sample_script()),
        runs_root=tmp_path / "runs",
        max_rounds=3,
    )
    assert result.verdict == "resolved", result.error
    assert result.rounds == 2 and not result.artifact_errors
    run_dir = Path(result.run_dir)
    attempt = json.loads((run_dir / "attempts/attempt-1.json").read_text(encoding="utf-8"))
    assert 'if value == ""' in attempt["patch_diff"]
    assert attempt["test_diagnosis"]["failure_count"] > 0
    assert any("失败尝试记忆" in request and "patch=" in request for request in requests)
    report = json.loads((run_dir / "evaluation_report.json").read_text(encoding="utf-8"))
    assert report["target_tests"]["status"] == report["regression_tests"]["status"] == "passed"
    assert report["patch_role"] == "accepted"
    assert report["patch_quality"]["syntax_status"] == "passed"
    events = json.loads((run_dir / "trajectory.json").read_text(encoding="utf-8"))
    assert any(e["tool"] == "node_transition" and e["duration_ms"] >= 0 for e in events)
    assert any(e["tool"] == "failed_attempt" for e in events)
    assert (run_dir / "patch.diff").read_bytes() == (run_dir / "diff.patch").read_bytes()


def test_same_failed_candidate_stops_on_second_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import get_settings
    from app.evals.bugset import load_bug
    from app.graph.runner import run_task_graph
    from app.llm.fake import FakeLLM
    from scripts.sample_run import sample_script

    monkeypatch.setattr(get_settings(), "max_identical_failures", 2)
    script = sample_script()
    first_failure = script[:8]
    # G14 retains the named candidate. Resubmitting it unchanged must not spend round 3.
    same_candidate = [{"tool": "finish", "args": {"success": True, "summary": "same candidate"}}]
    result = run_task_graph(
        load_bug("BUG-001"),
        FakeLLM(first_failure + same_candidate),
        runs_root=tmp_path / "runs",
        max_rounds=4,
    )
    assert result.status == "NEEDS_REVIEW" and result.rounds == 2, result.error
    assert "same ineffective patch" in str(result.error)
    report = json.loads(
        (Path(result.run_dir) / "evaluation_report.json").read_text(encoding="utf-8")
    )
    assert report["patch_role"] == "unaccepted_evidence" and len(report["attempts"]) == 2
    assert repeated_patch(report["attempts"])
    assert "不要重复" in negative_memory(report["attempts"])


def test_invalid_run_still_exports_three_artifacts(tmp_path: Path) -> None:
    from app.evals.driver import TaskResult, _write_report

    result = TaskResult(task_id="invalid", bug_id="bad", status="INVALID_TASK")
    _write_report(result, tmp_path)
    report = json.loads((tmp_path / "evaluation_report.json").read_text(encoding="utf-8"))
    assert report["target_tests"]["status"] == "not_run"
    assert report["patch_quality"]["syntax_status"] == "unknown"
    assert (tmp_path / "patch.diff").read_text() == ""
    assert json.loads((tmp_path / "trajectory.json").read_text()) == []


def test_small_nonfile_and_failed_receipts_remain_retrievable(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    execute(ctx, "list_files", {})
    execute(ctx, "read_file", {"path": "absent.py"})
    assert ctx.memory is not None
    for item in list(ctx.memory.evidence):
        assert item.retrievable and item.evidence_id
        result = execute(ctx, "read_evidence", {"evidence_id": item.evidence_id})
        assert result.ok
    assert "error" in str(result.output)
