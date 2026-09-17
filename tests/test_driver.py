"""M4 驱动器测试:resolved 判定、INVALID_TASK 与门禁拒绝路径。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.evals.bugset import load_bug, load_replay_script
from app.evals.driver import run_task
from app.llm.fake import FakeLLM

BUG_ROOT = Path("bugs")


def test_replay_resolves_bug001(tmp_path: Path) -> None:
    bug = load_bug("BUG-001", BUG_ROOT)
    model = FakeLLM(load_replay_script(bug))
    result = run_task(bug, model, runs_root=tmp_path / "runs")

    assert result.verdict == "resolved" and result.status == "FINISHED"
    assert result.changed_files == ["src/dateparse.py"]
    assert result.baseline_failed >= 1
    assert result.verify_failed_ok and result.verify_regression_ok

    run_dir = Path(result.run_dir)
    assert (run_dir / "report.json").exists()
    assert (run_dir / "diff.patch").exists()
    assert (run_dir / "trajectory.jsonl").exists()
    events = [
        json.loads(line)
        for line in (run_dir / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert {"create_workspace", "apply_patch", "run_tests", "finish"} <= {e["tool"] for e in events}


def test_replay_resolves_cross_file_bug005(tmp_path: Path) -> None:
    bug = load_bug("BUG-005", BUG_ROOT)
    model = FakeLLM(load_replay_script(bug))
    result = run_task(bug, model, runs_root=tmp_path / "runs")
    assert result.verdict == "resolved"
    assert "src/helpers.py" in result.changed_files  # 根因文件,症状在 pipeline


def test_already_fixed_bug_is_invalid_task(tmp_path: Path) -> None:
    """把 BUG-003 的仓库预置为已修复 → 基线无失败 → INVALID_TASK。"""
    source = BUG_ROOT / "BUG-003"
    bug_dir = tmp_path / "BUG-003-FIXED"
    shutil.copytree(source, bug_dir)
    labels = bug_dir / "repo" / "src" / "labels.py"
    labels.write_text(
        '"""标签拼接工具。"""\n\n\ndef join_labels(labels, sep=None):\n'
        '    separator = "," if sep is None else sep\n'
        '    result = ""\n'
        "    for index, label in enumerate(labels):\n"
        "        if index > 0:\n"
        "            result += separator\n"
        "        result += label\n"
        "    return result\n",
        encoding="utf-8",
        newline="\n",
    )

    bug = load_bug(bug_dir)
    model = FakeLLM(load_replay_script(bug))
    result = run_task(bug, model, runs_root=tmp_path / "runs")
    assert result.status == "INVALID_TASK"
    assert "already pass" in (result.error or "")


def test_test_file_patch_gets_rejected(tmp_path: Path) -> None:
    """Agent 试图修改测试文件 → 门禁拒绝 → 无法 resolved,违规记录在案。"""
    malicious = [
        {
            "tool": "apply_patch",
            "args": {
                "diff_text": (
                    "diff --git a/tests/test_labels.py b/tests/test_labels.py\n"
                    "--- a/tests/test_labels.py\n"
                    "+++ b/tests/test_labels.py\n"
                    "@@ -1,4 +1,5 @@\n"
                    " from src.labels import join_labels\n"
                    "+\n"
                    " def test_default_separator():\n"
                    '     assert join_labels(["a", "b", "c"]) == "a,b,c"\n'
                )
            },
        },
        {"tool": "finish", "args": {"success": True, "summary": "试图改测试作弊"}},
    ]
    bug = load_bug("BUG-003", BUG_ROOT)
    result = run_task(bug, FakeLLM(malicious), runs_root=tmp_path / "runs")
    assert result.verdict == "failed"
    assert result.status in {"PATCH_REJECTED", "VERIFY_FAILED"}
    assert result.verify_failed_ok is False  # 原失败测试没有被修复
    assert any("test file" in v for v in result.gate_violations) or "empty" in " ".join(
        result.gate_violations
    )


def test_crash_converges_to_needs_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """驱动器把意外异常收敛为 NEEDS_REVIEW,不向上抛。"""
    bug = load_bug("BUG-004", BUG_ROOT)

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.evals.driver.run_plain_loop", boom)
    model = FakeLLM(load_replay_script(bug))
    result = run_task(bug, model, runs_root=tmp_path / "runs")
    assert result.status == "NEEDS_REVIEW" and result.verdict == "needs_review"
    assert "boom" in (result.error or "")


def test_report_contains_token_detail(tmp_path: Path) -> None:
    """N2a:report.json 落盘 tokens_prompt/tokens_completion 且与 fake 语义一致。"""
    bug = load_bug("BUG-001", BUG_ROOT)
    result = run_task(bug, FakeLLM(load_replay_script(bug)), runs_root=tmp_path / "runs")
    report = json.loads((Path(result.run_dir) / "report.json").read_text(encoding="utf-8"))
    assert report["tokens_prompt"] >= 0 and report["tokens_completion"] >= 0
    assert result.tokens_prompt == 0
    assert result.tokens_completion == result.tokens_used > 0
