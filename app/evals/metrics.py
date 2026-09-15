"""指标计算(企划书 11.2):从 runs 目录的 report.json 汇总七项指标。"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.graph.gates import parse_diff_files

log = logging.getLogger(__name__)

_RUN_DIR_RE = re.compile(r"^(?P<bug>BUG-\d+)-(?P<stamp>\d{8}-\d{6})-(?P<hash>[0-9a-f]+)$")


@dataclass
class RunRow:
    """一次任务运行的评测行。"""

    task_id: str
    bug_id: str
    run_dir: Path
    verdict: str
    status: str
    model_provider: str
    engine: str
    rounds: int
    turns: int
    tokens_used: int
    duration_ms: int
    changed_files: list[str]
    gate_violations: list[str]
    verify_failed_ok: bool
    verify_regression_ok: bool
    error: str | None = None
    localized: bool = False
    patch_applied: bool = False
    regression_introduced: bool = False
    security_blocked: bool = False
    expected_files: list[str] = field(default_factory=list)


def load_run(run_dir: Path) -> RunRow | None:
    """读取单个 run 目录;report.json 缺失/损坏返回 None。"""
    report_path = run_dir / "report.json"
    if not report_path.exists():
        return None
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("skip unreadable report: %s", report_path)
        return None
    return RunRow(
        task_id=data.get("task_id", run_dir.name),
        bug_id=data.get("bug_id", ""),
        run_dir=run_dir,
        verdict=data.get("verdict", "failed"),
        status=data.get("status", ""),
        model_provider=data.get("model_provider", "unknown"),
        engine=data.get("engine", ""),
        rounds=data.get("rounds", 0),
        turns=data.get("turns", 0),
        tokens_used=data.get("tokens_used", 0),
        duration_ms=data.get("duration_ms", 0),
        changed_files=data.get("changed_files", []),
        gate_violations=data.get("gate_violations", []),
        verify_failed_ok=data.get("verify_failed_ok", False),
        verify_regression_ok=data.get("verify_regression_ok", False),
        error=data.get("error"),
    )


def collect_runs(runs_root: Path | str) -> list[RunRow]:
    """递归收集 runs 根目录下的全部任务报告。"""
    root = Path(runs_root)
    rows: list[RunRow] = []
    if not root.exists():
        return rows
    for report_path in sorted(root.glob("**/report.json")):
        row = load_run(report_path.parent)
        if row is not None:
            rows.append(row)
    return rows


def latest_per_bug(rows: list[RunRow]) -> list[RunRow]:
    """同一 bug 多次运行时取最新一次(按目录名时间戳)。"""

    def sort_key(row: RunRow) -> tuple[str, str]:
        match = _RUN_DIR_RE.match(row.run_dir.name)
        stamp = match.group("stamp") if match else "00000000-000000"
        return row.bug_id, stamp

    latest: dict[str, RunRow] = {}
    for row in sorted(rows, key=sort_key):
        latest[row.bug_id] = row
    return list(latest.values())


def expected_files_for(bug_id: str, bugs_root: Path | str = Path("bugs")) -> list[str]:
    """期望修改范围:题目参考 diff 触碰的文件(用于定位成功判定)。"""
    reference = Path(bugs_root) / bug_id / "expected" / "reference.diff"
    if not reference.exists():
        return []
    return parse_diff_files(reference.read_text(encoding="utf-8"))


def annotate(row: RunRow, bugs_root: Path | str = Path("bugs")) -> RunRow:
    """派生判定字段:定位成功 / 补丁应用 / 回归引入 / 安全拦截。"""
    row.expected_files = expected_files_for(row.bug_id, bugs_root)
    touched = set(row.changed_files)
    expected = set(row.expected_files)
    # 定位成功:补丁触碰了期望修改范围内的文件(相交即可,不要求完全一致)
    row.localized = bool(touched & expected) if expected else bool(touched)
    row.patch_applied = bool(touched) and not row.gate_violations
    # 回归引入:原失败测试修好了,但回归集出现新失败
    row.regression_introduced = row.verify_failed_ok and not row.verify_regression_ok
    row.security_blocked = bool(row.gate_violations)
    return row


def compute_metrics(rows: list[RunRow]) -> dict[str, Any]:
    """七项指标(企划书 11.2)。除法分母为 0 时记 None,不产出漂亮假数字。"""
    total = len(rows)

    def rate(numerator: int) -> float | None:
        return round(numerator / total, 4) if total else None

    applied_rows = [r for r in rows if r.patch_applied or r.security_blocked]
    regression_denominator = [r for r in rows if r.patch_applied]
    applied_count = len(applied_rows)

    return {
        "total_runs": total,
        "localization_rate": rate(sum(1 for r in rows if r.localized)),
        "patch_application_rate": (round(applied_count / total, 4) if total else None),
        "final_resolution_rate": rate(sum(1 for r in rows if r.verdict == "resolved")),
        "regression_introduction_rate": (
            round(
                sum(1 for r in regression_denominator if r.regression_introduced)
                / len(regression_denominator),
                4,
            )
            if regression_denominator
            else None
        ),
        "security_blocked_count": sum(1 for r in rows if r.security_blocked),
        "avg_rounds": round(sum(r.rounds for r in rows) / total, 2) if total else None,
        "avg_tokens": round(sum(r.tokens_used for r in rows) / total) if total else None,
        "avg_duration_ms": round(sum(r.duration_ms for r in rows) / total) if total else None,
        "by_category_providers": sorted({r.model_provider for r in rows}),
    }
