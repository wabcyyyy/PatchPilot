"""Offline review probes and read-only aggregation of historical Q0 artifacts."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from app.config import Settings
from app.errors import BudgetError
from app.graph.plain_loop import run_plain_loop
from app.graph.resources import ResourceLedger
from app.llm.fake import FakeLLM
from app.llm.openai_client import OpenAICompatModel
from app.prompts import PLAN_PROMPT
from app.tools.base import ToolContext
from app.tools.output_filter import fold_output
from app.tools.registry import tool_schemas
from app.tools.tracker import Tracker
from scripts import compare_experiments


def comparison_probe(*, changed_result: bool) -> str:
    provenance = {
        "engine": "graph",
        "model_provider": "openai",
        "model_name": "offline",
        "max_turns": 24,
        "execution_backend": "docker",
        "git_commit": "same-commit",
    }
    rows = [
        SimpleNamespace(
            bug_id="probe",
            provenance={
                **provenance,
                "arm": arm,
                "config_snapshot": {"context_window_tokens": 16000 if arm == "agent" else 128000},
            },
            engine="graph",
            model_provider="openai",
            verdict="failed" if changed_result and arm == "one_shot" else "resolved",
            status="FINISHED",
            tokens_used=100,
            rounds=1,
            duration_ms=100,
        )
        for arm in ("agent", "one_shot")
    ]
    with (
        patch.object(compare_experiments, "collect_runs", side_effect=[[rows[0]], [rows[1]]]),
        patch.object(compare_experiments, "latest_per_bug", side_effect=lambda items: items),
    ):
        try:
            compare_experiments.compare_batches(Path("agent"), Path("one_shot"))
        except SystemExit as exc:
            return f"rejected: {exc}"
        return "accepted"


def offline_probes() -> dict[str, Any]:
    settings = Settings(_env_file=None, llm_enabled=False, loop_snapshot_enabled=False)
    tracker = Tracker(None, task_id="offline-audit")
    ctx = ToolContext("offline-audit", Path.cwd(), "", tracker)
    plan_text = "1. Inspect f.\n2. Fix its boundary.\n3. Verify both test sets."
    model = FakeLLM([{"content": plan_text} for _ in range(3)])
    with (
        patch("app.graph.plain_loop.get_settings", return_value=settings),
        patch("app.llm.fake._plan_request", return_value=""),
    ):
        try:
            run_plain_loop(
                ctx,
                model,
                PLAN_PROMPT,
                max_turns=3,
                state_label="PLAN",
                allowed_tools=["finish"],
                token_budget=0,
                context_window_tokens=0,
            )
        except BudgetError as exc:
            plan_result = {"calls": model.consumed, "error": str(exc)}
        else:
            raise AssertionError("Expected plain-text PLAN to exhaust its turn limit")

    message = SimpleNamespace(
        content=None,
        reasoning_content=None,
        tool_calls=[
            SimpleNamespace(
                id="mock_finish",
                function=SimpleNamespace(
                    name="finish", arguments='{"success":false,"summary":"offline probe"}'
                ),
            )
        ],
    )
    response = SimpleNamespace(
        usage=None, choices=[SimpleNamespace(message=message, finish_reason="tool_calls")]
    )
    client = OpenAICompatModel.__new__(OpenAICompatModel)
    client._client = Mock()
    client._client.chat.completions.create.return_value = response
    client._max_tokens = 4096
    client._extra_body = None
    client.model_name = "offline-mocked-client"
    ledger = ResourceLedger("offline-audit", token_limit=100000)
    with (
        patch("app.graph.plain_loop.get_settings", return_value=settings),
        patch("app.graph.plain_loop._has_patch", return_value=False),
    ):
        run_plain_loop(
            ctx, client, "offline usage probe", max_turns=1, ledger=ledger, token_budget=0
        )
    snapshot = ledger.snapshot()
    shown_tools = client._client.chat.completions.create.call_args.kwargs["tools"]

    raw = json.dumps({"content": "A" * 12000, "truncated": False})
    shown = fold_output(raw, head=40, tail=15)
    try:
        json.loads(shown)
        valid_json = True
    except json.JSONDecodeError:
        valid_json = False
    return {
        "plan_plain_text": plan_result,
        "plan_visible_schema_count": len(tool_schemas()),
        "usage_missing_labeled": snapshot["calls"][0]["usage_source"],
        "usage_missing_estimated_tokens": snapshot["tokens_used"],
        "mocked_http_calls": client._client.chat.completions.create.call_count,
        "sent_schema_count": len(shown_tools),
        "comparison_changed_outcome": comparison_probe(changed_result=True),
        "comparison_changed_context_only": comparison_probe(changed_result=False),
        "fold_output": {
            "raw_chars": len(raw),
            "visible_chars": len(shown),
            "valid_json": valid_json,
            "tail_marker_preserved": '"truncated": false' in shown,
        },
    }


def aggregate_runs(root: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    stages: Counter[str] = Counter()
    verdicts: Counter[str] = Counter()
    commits: set[str] = set()
    prompt_total = completion_total = plan_tokens = 0
    for report_path in sorted(root.rglob("report.json")):
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("model_provider") != "openai":
            continue
        events = [
            json.loads(line)
            for line in (report_path.parent / "trajectory.jsonl").read_text("utf-8").splitlines()
            if line.strip()
        ]
        llm = [e for e in events if e["tool"] == "llm"]
        plan = [e for e in llm if e["state"] == "PLAN"]
        counts = Counter(e["state"] for e in llm)
        stages.update(counts)
        verdicts.update([report["verdict"]])
        commits.add(report["provenance"]["git_commit"])
        prompt_total += report["tokens_prompt"]
        completion_total += report["tokens_completion"]
        plan_spent = sum(e["output_summary"].get("tokens", 0) for e in plan)
        plan_tokens += plan_spent
        rows.append(
            {
                "report": str(report_path.resolve()),
                "bug_id": report["bug_id"],
                "verdict": report["verdict"],
                "tokens": report["tokens_used"],
                "reported_turns": report["turns"],
                "llm_events": len(llm),
                "stages": dict(counts),
                "plan_tokens": plan_spent,
                "plan_degraded": any(e["tool"] == "plan_degraded" for e in events),
                "plan_finish": any(
                    "finish" in e["output_summary"].get("tool_calls", []) for e in plan
                ),
                "compactions": sum(e["tool"] == "context_compact" for e in events),
                "max_call_total_tokens": max(e["output_summary"].get("tokens", 0) for e in llm),
                "error": report.get("error"),
            }
        )
    resolved_calls = [r["llm_events"] for r in rows if r["verdict"] == "resolved"]
    total = prompt_total + completion_total
    return {
        "root": str(root.resolve()),
        "commits": sorted(commits),
        "runs": len(rows),
        "verdicts": dict(verdicts),
        "tokens": total,
        "prompt_tokens": prompt_total,
        "completion_tokens": completion_total,
        "prompt_fraction": prompt_total / total,
        "stage_calls": dict(stages),
        "plan_tokens": plan_tokens,
        "plan_token_fraction": plan_tokens / total,
        "plan_degraded_runs": sum(r["plan_degraded"] for r in rows),
        "plan_finish_runs": sum(r["plan_finish"] for r in rows),
        "compacted_runs": sum(bool(r["compactions"]) for r in rows),
        "failed_without_compaction": sum(
            r["verdict"] == "failed" and not r["compactions"] for r in rows
        ),
        "resolved_call_range": [min(resolved_calls), max(resolved_calls)],
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = {"offline_probes": offline_probes()}
    if args.runs is not None:
        result["historical_q0"] = aggregate_runs(args.runs)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {k: {a: b for a, b in v.items() if a != "rows"} for k, v in result.items()},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
