"""M9 指标计算测试:从合成 report.json 计算七项指标与定位判定。"""

from __future__ import annotations

import json
from pathlib import Path

from app.evals.metrics import (
    RunRow,
    annotate,
    annotate_v1,
    collect_runs,
    compute_metrics,
    latest_per_bug,
)


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
    _write_report(
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
        bug_id="BUG-002",
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
    # 回归引入率的分母是"补丁已应用"的运行:该批次无回归引入,有分母则为 0.0
    assert metrics["regression_introduction_rate"] == 0.0

    # BUG-001 同题两次运行取最新
    bug1 = next(r for r in per_bug if r.bug_id == "BUG-001")
    assert bug1.run_dir.name.endswith("bbbb")


def test_latest_per_bug_keeps_both_arms(tmp_path: Path) -> None:
    """复盘 R-1:同一 bug 的 agent 臂与 one_shot 臂是两条独立记录,
    按 bug_id 静默丢一臂会让指标分母失真;同臂多次运行仍取最新。"""
    _write_report(tmp_path / "BUG-001-20260916-000007-aaaa", changed_files=["src/dateparse.py"])
    oneshot_dir = _write_report(
        tmp_path / "BUG-001-20260916-000008-bbbb", changed_files=["src/dateparse.py"]
    )
    _write_report(tmp_path / "BUG-001-20260916-000009-cccc", changed_files=["src/dateparse.py"])
    rep_path = oneshot_dir / "report.json"
    payload = json.loads(rep_path.read_text(encoding="utf-8"))
    payload["provenance"] = {"arm": "one_shot"}
    rep_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    rows = [annotate(r, Path("bugs")) for r in collect_runs(tmp_path)]
    per_bug = latest_per_bug(rows)
    assert len(per_bug) == 2
    arms = {str(r.provenance.get("arm") or "agent") for r in per_bug}
    assert arms == {"agent", "one_shot"}


def test_empty_runs_give_none_rates(tmp_path: Path) -> None:
    metrics = compute_metrics([])
    assert metrics["total_runs"] == 0
    assert metrics["final_resolution_rate"] is None  # 不产出漂亮假数字


def test_gate_blocked_runs_do_not_inflate_patch_application_rate(tmp_path: Path) -> None:
    """N-13 整改:被门禁拦截(含空 diff)的运行不算进 patch_application_rate 分子,
    与 regression_denominator 口径一致;拦截数只体现在 security_blocked_count。"""
    _write_report(
        tmp_path / "BUG-001-20260916-000003-aaaa",
        changed_files=[],
        gate_violations=["[format] diff is empty"],
        status="PATCH_REJECTED",
    )
    rows = [annotate(r, Path("bugs")) for r in collect_runs(tmp_path)]
    metrics = compute_metrics(rows)
    assert rows[0].security_blocked and not rows[0].patch_applied
    assert metrics["patch_application_rate"] == 0.0
    assert metrics["security_blocked_count"] == 1


def test_localized_dual_criterion(tmp_path: Path) -> None:
    """复盘 P0-2:localized(相交即可,弱信号)与 localized_strict(触碰集⊆期望集)并存,
    旧口径数值不得改变;无 reference.diff 时两者同走"碰了即命中"退化路径。"""
    # 相交但含期望外文件 → 弱口径命中,严格口径不命中
    _write_report(
        tmp_path / "BUG-001-20260916-000004-aaaa",
        changed_files=["src/dateparse.py", "src/unrelated.py"],
    )
    # 触碰集 ⊆ 期望集(BUG-001 期望仅 src/dateparse.py)→ 双口径命中
    _write_report(
        tmp_path / "BUG-001-20260916-000005-bbbb",
        changed_files=["src/dateparse.py"],
    )
    rows = [annotate(r, Path("bugs")) for r in collect_runs(tmp_path)]
    weak, strict = sorted(rows, key=lambda r: r.run_dir.name)
    assert weak.localized and not weak.localized_strict
    assert strict.localized and strict.localized_strict
    metrics = compute_metrics(rows)
    assert metrics["localization_rate"] == 1.0
    assert metrics["localization_strict_rate"] == 0.5

    # 无 reference.diff 的 bugs 根:双口径同为 bool(touched) 退化路径
    _write_report(
        tmp_path / "BUG-001-20260916-000006-cccc",
        changed_files=["anything.py"],
    )
    degraded = [annotate(r, tmp_path / "no-such-bugs") for r in collect_runs(tmp_path)]
    for row in degraded:
        assert row.localized == bool(row.changed_files)
        assert row.localized_strict == row.localized


# ---------- S09/F6:metrics v2 的严格定位与历史口径 ----------


def _row_with_changes(bug_id: str, changed: list[str], tmp_path: Path) -> RunRow:
    run_dir = tmp_path / bug_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.json").write_text("{}", encoding="utf-8")  # annotate 不读 report
    return RunRow(
        task_id=f"{bug_id}-1",
        bug_id=bug_id,
        run_dir=run_dir,
        verdict="failed",
        status="BUDGET_EXCEEDED",
        model_provider="fake-replay",
        engine="graph",
        rounds=1,
        turns=0,
        tokens_used=0,
        duration_ms=0,
        changed_files=changed,
        gate_violations=[],
        verify_failed_ok=False,
        verify_regression_ok=False,
    )


def _ref_bug(tmp_path: Path, expected: list[str]) -> str:
    """造一个带 reference.diff 的题目目录;返回 bug_id。"""
    bug_id = "BUG-REF-X"
    ref_dir = tmp_path / bug_id / "expected"
    ref_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"diff --git a/{f} b/{f}" for f in expected]
    ref_dir.joinpath("reference.diff").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return bug_id


def test_f6_empty_touched_is_not_strict_success_in_v2(tmp_path: Path) -> None:
    """F6 精确复现翻转:无改动 + BUDGET_EXCEEDED,v2 strict=False(v1 曾为 True)。"""
    bug_id = _ref_bug(tmp_path, ["src/dateparse.py"])
    row = annotate(_row_with_changes(bug_id, [], tmp_path), tmp_path)
    assert row.localized is False
    assert row.localized_strict is False, "空补丁不再是严格定位成功"
    assert row.expected_coverage == 0.0
    # 显式 v1 重算:历史口径可复现(空集 ⊆ 期望集),但不回写产物
    old = annotate_v1(_row_with_changes(bug_id, [], tmp_path), tmp_path)
    assert old.localized_strict is True


def test_strict_semantics_matrix(tmp_path: Path) -> None:
    bug_id = _ref_bug(tmp_path, ["src/a.py", "src/b.py"])
    # 范围内非空:strict 成功,coverage=1.0
    inside = annotate(_row_with_changes(bug_id, ["src/a.py", "src/b.py"], tmp_path), tmp_path)
    assert inside.localized_strict is True and inside.expected_coverage == 1.0
    # 部分命中:strict 成功(没碰范围外),coverage=0.5
    partial = annotate(_row_with_changes(bug_id, ["src/a.py"], tmp_path), tmp_path)
    assert partial.localized_strict is True and partial.expected_coverage == 0.5
    # 范围外:strict 失败
    outside = annotate(_row_with_changes(bug_id, ["src/other.py"], tmp_path), tmp_path)
    assert outside.localized_strict is False
    # 混合:范围内外都碰 → strict 失败,coverage 只看命中
    mixed = annotate(_row_with_changes(bug_id, ["src/a.py", "src/other.py"], tmp_path), tmp_path)
    assert mixed.localized_strict is False and mixed.expected_coverage == 0.5


def test_reference_missing_degrades_and_is_counted(tmp_path: Path) -> None:
    """无 reference.diff:退化为"非空触碰即命中",coverage=None,汇总计数缺失项。"""
    row = annotate(_row_with_changes("BUG-NO-REF", ["src/whatever.py"], tmp_path), tmp_path)
    assert row.reference_missing is True
    assert row.localized_strict is True
    assert row.expected_coverage is None
    rows = compute_metrics([row])
    assert rows["reference_missing_count"] == 1
    assert rows["expected_coverage_avg"] is None
    assert rows["expected_coverage_denominator"] == 0


def test_metrics_summary_carries_version_and_failure_breakdown(tmp_path: Path) -> None:
    ok_row = _row_with_changes("BUG-OK", ["src/a.py"], tmp_path)
    ok_row.verdict = "resolved"
    ok_row.status = "FINISHED"
    ok_row.verify_failed_ok = True
    ok_row.verify_regression_ok = True
    annotate(ok_row, tmp_path)
    budget_row = _row_with_changes("BUG-NOCHANGE", [], tmp_path)
    annotate(budget_row, tmp_path)
    summary = compute_metrics([ok_row, budget_row])
    assert summary["metrics_version"] == 2
    assert summary["failure_breakdown"]["budget_exhausted"] == 1
    assert summary["failure_breakdown"]["no_patch"] == 1
    assert summary["final_resolution_rate"] == 0.5
