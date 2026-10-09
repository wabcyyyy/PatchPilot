"""指标计算(企划书 11.2):从 runs 目录的 report.json 汇总评测行与指标。

S09(ADR-0009 §6,review F6):指标语义变更必须带版本。
- `metrics_version=2`(当前):`localized_strict = bool(touched) and touched <= expected`
  ——空触碰不再算"严格定位成功";语义是"**未触碰参考范围外文件**",不是找全根因;
- `metrics_version=1`(显式重算模式):历史口径 `touched <= expected`,
  空 touched 在 expected 非空时也为 True(F6 缺陷现场,只为历史对账保留);
- 无 reference.diff 的题:两版都退化为"非空触碰即命中",并标注 `reference_missing`;
- 附加 `expected_coverage = |touched ∩ expected| / |expected|`(v2,有 expected 才算,
  无 expected 为 None)——度量的是"期望文件覆盖了几成",与 strict 正交;
- 汇总携带版本与各指标分子/分母,不产出无版本的新旧混排数字。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.graph.gates import parse_diff_files

log = logging.getLogger(__name__)

METRICS_VERSION = 2


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
    finished_stamp: float = 0.0  # report.json 修改时间:目录名格式各异,以文件时间为准
    provenance: dict[str, Any] = field(default_factory=dict)  # T10.1 批次溯源(透传)
    localized: bool = False
    localized_strict: bool = False
    patch_applied: bool = False
    regression_introduced: bool = False
    security_blocked: bool = False
    expected_files: list[str] = field(default_factory=list)
    # S09:参考缺失标注与期望文件覆盖率(仅评测使用 reference,不进模型上下文)
    reference_missing: bool = False
    expected_coverage: float | None = None


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
        finished_stamp=report_path.stat().st_mtime,
        provenance=data.get("provenance") if isinstance(data.get("provenance"), dict) else {},
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
    """同一 bug 多次运行时取最新一次(按 report.json 的 mtime,见 RunRow.finished_stamp)。

    复盘 R-1:去重键含消融臂(provenance.arm,缺省 agent)——同一根目录混入
    agent/one_shot 两臂时,此前按 bug_id 静默丢一臂,指标分母失真。引擎混批
    (m9 的 plain/graph)维持既有"每题取最新"披露口径,不在此扩展。
    """

    def sort_key(row: RunRow) -> tuple[str, str, float]:
        return row.bug_id, str(row.provenance.get("arm") or "agent"), row.finished_stamp

    latest: dict[tuple[str, str], RunRow] = {}
    for row in sorted(rows, key=sort_key):
        latest[(row.bug_id, str(row.provenance.get("arm") or "agent"))] = row
    return list(latest.values())


def expected_files_for(bug_id: str, bugs_root: Path | str = Path("bugs")) -> list[str]:
    """期望修改范围:题目参考 diff 触碰的文件(用于定位成功判定)。"""
    reference = Path(bugs_root) / bug_id / "expected" / "reference.diff"
    if not reference.exists():
        return []
    return parse_diff_files(reference.read_text(encoding="utf-8"))


def _strict_logic(
    touched: set[str], expected: set[str], *, version: int
) -> tuple[bool, bool, float | None]:
    """返回 (localized, localized_strict, expected_coverage)。

    - v2:strict 要求非空触碰且触碰集 ⊆ 期望集(F6:空补丁不再是严格成功);
    - v1:触碰集 ⊆ 期望集(空集 ⊆ 任何集合——历史口径,仅为对账保留);
    - 无 expected:两版都退化为"非空触碰即命中",coverage 无分母记 None;
    - coverage 只在 expected 非空时计算(分子 |touched∩expected|,分母 |expected|)。
    """
    localized = bool(touched & expected) if expected else bool(touched)
    if version >= 2:
        strict = bool(touched) and touched <= expected if expected else bool(touched)
    else:
        strict = (touched <= expected) if expected else bool(touched)
    coverage = (len(touched & expected) / len(expected)) if expected else None
    return localized, strict, coverage


def annotate(row: RunRow, bugs_root: Path | str = Path("bugs")) -> RunRow:
    """派生判定字段(v2):定位成功/严格定位/覆盖率/补丁应用/回归引入/安全拦截。"""
    row.expected_files = expected_files_for(row.bug_id, bugs_root)
    touched = set(row.changed_files)
    expected = set(row.expected_files)
    row.reference_missing = not expected
    row.localized, row.localized_strict, row.expected_coverage = _strict_logic(
        touched, expected, version=METRICS_VERSION
    )
    row.patch_applied = bool(touched) and not row.gate_violations
    # 回归引入:原失败测试修好了,但回归集出现新失败
    row.regression_introduced = row.verify_failed_ok and not row.verify_regression_ok
    row.security_blocked = bool(row.gate_violations)
    return row


def annotate_v1(row: RunRow, bugs_root: Path | str = Path("bugs")) -> RunRow:
    """显式 v1 重算模式:历史口径(空 touched 在 expected 非空时也是 strict 成功)。

    只为历史对账存在;新汇总一律走 annotate(v2)。不回写任何历史产物。
    """
    row.expected_files = expected_files_for(row.bug_id, bugs_root)
    touched = set(row.changed_files)
    expected = set(row.expected_files)
    row.reference_missing = not expected
    row.localized, row.localized_strict, row.expected_coverage = _strict_logic(
        touched, expected, version=1
    )
    row.patch_applied = bool(touched) and not row.gate_violations
    row.regression_introduced = row.verify_failed_ok and not row.verify_regression_ok
    row.security_blocked = bool(row.gate_violations)
    return row


def compute_metrics(rows: list[RunRow]) -> dict[str, Any]:
    """指标汇总(v2):带版本与分子/分母,缺失项如实记 None。"""
    total = len(rows)

    def rate(numerator: int) -> float | None:
        return round(numerator / total, 4) if total else None

    # N-13 整改:patch_application_rate 的分子只算真正产出并应用了补丁的运行;
    # 此前把 security_blocked(含"空 diff 被 format 门禁拦下"的运行)也算进分子,
    # 与 regression_denominator 的口径互相矛盾,有拦截样本时指标被系统性抬高
    applied_rows = [r for r in rows if r.patch_applied]
    regression_denominator = [r for r in rows if r.patch_applied]
    applied_count = len(applied_rows)
    coverage_scores = [r.expected_coverage for r in rows if r.expected_coverage is not None]

    return {
        "metrics_version": METRICS_VERSION,
        "total_runs": total,
        "localization_rate": rate(sum(1 for r in rows if r.localized)),
        "localization_strict_rate": rate(sum(1 for r in rows if r.localized_strict)),
        # strict 的语义:未触碰参考范围外的文件——不是"找全了根因";
        # 找全程度看 expected_coverage(仅对有 reference 的样本,分母=|expected|)
        "expected_coverage_avg": (
            round(sum(coverage_scores) / len(coverage_scores), 4) if coverage_scores else None
        ),
        "expected_coverage_denominator": len(coverage_scores),
        "reference_missing_count": sum(1 for r in rows if r.reference_missing),
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
        # 失败分类(S09):成本停止≠模型能力不足,分母/分子按真实字段
        "failure_breakdown": {
            "budget_exhausted": sum(1 for r in rows if r.status == "BUDGET_EXCEEDED"),
            "gate_rejected": sum(1 for r in rows if r.security_blocked),
            "no_patch": sum(1 for r in rows if not r.patch_applied and not r.security_blocked),
            "verify_failed": sum(
                1
                for r in rows
                if r.patch_applied
                and not r.security_blocked
                and not (r.verify_failed_ok and r.verify_regression_ok)
            ),
        },
    }
