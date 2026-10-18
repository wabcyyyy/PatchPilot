"""Budgeted pytest diagnosis for prompts; verdicts still come only from the adapter."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.adapters.pytest_adapter import PytestReport
from app.tools.output_filter import refine_traceback

_ERROR = re.compile(r"\b([\w.]+(?:Error|Exception|Exit|Interrupt))\b")


def _excerpt(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    marker = "\n... omitted ...\n"
    room = max(0, budget - len(marker))
    head = room // 2
    return text[:head] + marker + text[-(room - head) :] if room else marker[:budget]


class PytestOutputParser:
    """Extract JUnit failures, assertion detail and repository frames within one budget."""

    def __init__(self, project_root: Path, *, max_chars: int = 6000) -> None:
        self.project_root = project_root
        self.max_chars = max(256, max_chars)

    def parse(self, *reports: PytestReport) -> dict[str, Any]:
        cases = [case for report in reports for case in report.failed_cases]
        result: dict[str, Any] = {
            "failure_count": sum(report.failed + report.errors for report in reports),
            "failed_test_names": [],
            "failed_cases": [],
            "omitted_cases": len(cases),
        }
        for case in cases:
            # Keep the final exception even when the adapter's traceback is long.
            trace = refine_traceback(case.traceback, self.project_root, max_lines=0)
            exception = _ERROR.findall(case.message_first_line + "\n" + trace)
            item = {
                "name": case.test_name[:240],
                "test_id": case.test_id[:320],
                "signature": case.signature[:200],
                "error_type": exception[-1] if exception else case.kind,
                "message": _excerpt(case.message_first_line, 400),
                "traceback": _excerpt(trace, min(1800, self.max_chars // 3)),
            }
            candidate = {
                **result,
                "failed_test_names": [*result["failed_test_names"], case.test_id[:320]],
                "failed_cases": [*result["failed_cases"], item],
                "omitted_cases": result["omitted_cases"] - 1,
            }
            if len(json.dumps(candidate, ensure_ascii=False)) > self.max_chars:
                break
            result = candidate
        return result
