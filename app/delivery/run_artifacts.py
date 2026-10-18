"""Portable views of the existing authoritative run artifacts; no new acceptance rules."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.adapters.pytest_adapter import parse_junit_xml
from app.graph.gates import parse_diff_files


def _json(path: Path, value: object) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _tests(run_dir: Path, label: str, report: dict[str, Any]) -> dict[str, Any]:
    path = run_dir / "reports" / f"verify-{label}.xml"
    if not path.is_file():
        return {"status": "not_run", "artifact": None}
    parsed = parse_junit_xml(path)
    key = "verify_failed_ok" if label == "failed" else "verify_regression_ok"
    return {
        "status": "passed" if report.get(key) else "failed",
        "artifact": path.relative_to(run_dir).as_posix(),
        "passed": parsed.passed,
        "failed": parsed.failed,
        "errors": parsed.errors,
        "skipped": parsed.skipped,
        "collected": parsed.collected,
        "failed_tests": [case.test_id for case in parsed.failed_cases],
    }


def _patch_quality(diff: str, report: dict[str, Any]) -> dict[str, Any]:
    added = removed = 0
    findings: list[dict[str, str]] = []
    path = ""
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
            if re.search(r"\b(?:print|breakpoint|pdb\.set_trace)\s*\(", line[1:]):
                findings.append({"path": path, "added_line": line[1:]})
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return {
        "gate_status": report.get("gate_status", "not_run"),
        "gate_violations": report.get("gate_violations", []),
        "syntax_status": "passed" if report.get("verdict") == "resolved" else "unknown",
        "syntax_basis": "successful patch application and final acceptance",
        "changed_files": parse_diff_files(diff),
        "added_lines": added,
        "removed_lines": removed,
        "debug_scan": {
            "status": "findings" if findings else "no_findings",
            "findings": findings,
            "scope": "heuristic scan of added lines; informational, not an acceptance gate",
        },
    }


def export_run_artifacts(run_dir: Path, report: dict[str, Any]) -> None:
    """Export final, failed, cancelled and invalid runs from the common report exit."""
    source = run_dir / "diff.patch"
    diff = source.read_text(encoding="utf-8") if source.is_file() else ""
    (run_dir / "patch.diff").write_bytes(source.read_bytes() if source.is_file() else b"")
    trajectory = run_dir / "trajectory.jsonl"
    events = (
        [
            json.loads(line)
            for line in trajectory.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if trajectory.is_file()
        else []
    )
    _json(run_dir / "trajectory.json", events)
    attempts = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((run_dir / "attempts").glob("attempt-*.json"))
    ]
    provenance = report.get("provenance") or {}
    evaluation = {
        "schema_version": 1,
        "task_id": report["task_id"],
        "status": report.get("status"),
        "verdict": report.get("verdict"),
        "error": report.get("error"),
        "evidence_scope": (
            "scripted FakeLLM replay with actual test execution"
            if report.get("model_provider") == "fake-replay"
            else "agent run"
        ),
        "engine": report.get("engine"),
        "model_provider": report.get("model_provider"),
        "provenance": provenance,
        "target_tests": _tests(run_dir, "failed", report),
        "regression_tests": _tests(run_dir, "regression", report),
        "patch_quality": _patch_quality(diff, report),
        "patch_role": "accepted" if report.get("verdict") == "resolved" else "unaccepted_evidence",
        "resources": report.get("resources", {}),
        "resource_status": report.get("resource_status", "unknown"),
        "validation_status": report.get("validation_status", "not_run"),
        "candidate_id": report.get("candidate_id", ""),
        "candidate_hash": report.get("candidate_hash", ""),
        "verification_attempt_id": report.get("verification_attempt_id", ""),
        "attempts": attempts,
        "artifacts": {
            "patch": "patch.diff",
            "trajectory": "trajectory.json",
            "source_report": "report.json",
        },
    }
    _json(run_dir / "evaluation_report.json", evaluation)
