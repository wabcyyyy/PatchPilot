"""M9 指标计算测试:从合成 report.json 计算七项指标与定位判定。"""

from __future__ import annotations

import json
from pathlib import Path

from app.evals.metrics import annotate, collect_runs, compute_metrics, latest_per_bug


def _write_report(run_dir: Path, **fields) -> Path:
    run_dir.mkdir(parents=True)
    payload = {
        "task_id": run_dir.name,
        "bug_id": "BUG-001",
        "verdict": "failed",
        "status": "VERIFY_FAILED",
        "model_provider": "fake-replay",
        "engine": "graph",
        "rounds": 1,
        "turns": 6,
        "tokens_used": 800,
        "duration_ms": 7000,
        "changed_files": [],
        "gate_violations": [],
        "verify_failed_ok": False,
        "verify_regression_ok": True,
        "error": None,
    }
    payload.update(fields)
    (run_dir / "report.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return run_dir


def test_collect_and_annotate_with_real_bug(tmp_path: Path) -> None:
    run_dir = _write_report(
        tmp_path / "BUG-001-20260916-000000-aaaa",
        changed_files=["src/dateparse.py"],
        verdict="resolved",
        status="FINISHED",
        verify_failed_ok=True,
    )
    rows = [annotate(r, Path("bugs")) for r in collect_runs(tmp_path)]
    assert len(rows) == 1
    row = rows[0]
    # 期望范围来自 bugs/BUG-001/expected/reference.diff
    assert "src/dateparse.py" in row.expected_files
    assert row.localized and row.patch_applied
    assert not row.regression_introduced and not row.security_blocked


def test_metrics_on_mixed_rows(tmp_path: Path) -> None:
    _write_report(
        tmp_path / "BUG-001-20260916-000001-aaaa",
        changed_files=["src/dateparse.py"],
        verdict="resolved",
        status="FINISHED",
        verify_failed_ok=True,
    )
    _write_report(
        tmp_path / "BUG-001-20260916-000002-bbbb",
        changed_files=[],
        verdict="failed",
        status="PATCH_REJECTED",
        gate_violations=["[files] test file"],
    )
    _write_report(
        tmp_path / "BUG-002-20260916-000003-cccc",
        changed_files=["src/unrelated.py"],
        verdict="failed",
        status="VERIFY_FAILED",
        verify_failed_ok=False,
        verify_regression_ok=False,
    )

    rows = [annotate(r, Path("bugs")) for r in collect_runs(tmp_path)]
    per_bug = latest_per_bug(rows)  # BUG-001 取时间戳较新的失败运行
    assert len(per_bug) == 2

    metrics = compute_metrics(per_bug)
    assert metrics["total_runs"] == 2
    assert metrics["final_resolution_rate"] == 0.0
    assert metrics["security_blocked_count"] == 1
    # 回归引入率的分母是"补丁已应用"的运行
    assert metrics["regression_introduction_rate"] in (0.0, None)

    # BUG-001 同题两次运行取最新
    bug1 = next(r for r in per_bug if r.bug_id == "BUG-001")
    assert bug1.run_dir.name.endswith("bbbb")


def test_empty_runs_give_none_rates(tmp_path: Path) -> None:
    metrics = compute_metrics([])
    assert metrics["total_runs"] == 0
    assert metrics["final_resolution_rate"] is None  # 不产出漂亮假数字
