"""Durable negative memory for failed candidates, independent of rollback strategy."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.graph.gates import parse_diff_files


@dataclass(frozen=True)
class AttemptRecord:
    round_no: int
    changed_files: list[str]
    patch_diff: str
    patch_hash: str
    test_diagnosis: dict[str, Any]
    feedback: str
    candidate_id: str = ""

    @classmethod
    def create(cls, *, round_no: int, patch_diff: str, state: dict[str, Any]) -> AttemptRecord:
        return cls(
            round_no=round_no,
            changed_files=list(dict.fromkeys(parse_diff_files(patch_diff))),
            patch_diff=patch_diff,
            patch_hash=hashlib.sha256(patch_diff.encode("utf-8")).hexdigest(),
            test_diagnosis=(
                dict(state.get("test_diagnosis") or {}) if state.get("status") == "VERIFY" else {}
            ),
            feedback=str(state.get("feedback") or ""),
            candidate_id=str(state.get("candidate_id") or ""),
        )

    def save(self, run_dir: Path) -> dict[str, Any]:
        """Persist exact diff; checkpoint/prompt carry only its reference and diagnosis."""
        relative = f"attempts/attempt-{self.round_no}.json"
        path = run_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        reference = asdict(self)
        reference.pop("patch_diff")
        reference["artifact"] = relative
        return reference


def repeated_patch(attempts: list[dict[str, Any]], *, limit: int = 2) -> bool:
    if limit < 2 or len(attempts) < limit:
        return False
    window = attempts[-limit:]
    current = window[-1]
    return bool(current.get("changed_files")) and all(
        attempt["patch_hash"] == current["patch_hash"] for attempt in window
    )


def negative_memory(attempts: list[dict[str, Any]]) -> str:
    if not attempts:
        return ""
    lines = ["失败尝试记忆：不要重复相同的无效修改；先解释失败原因，再修订计划。"]
    for attempt in attempts[-3:]:
        diagnosis = attempt.get("test_diagnosis") or {}
        lines.append(
            f"- 第 {attempt['round_no']} 轮，文件 {attempt.get('changed_files', [])}，"
            f"patch={attempt['patch_hash'][:16]}，工件 {attempt.get('artifact', '')}；"
            f"失败测试 {diagnosis.get('failed_test_names', [])}；"
            + str(attempt.get("feedback") or "")[:1000]
        )
    return "\n".join(lines)
