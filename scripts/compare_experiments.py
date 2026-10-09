"""S10a 实验对照比较器:登记差异之外的一切不同都直接报错。

公平对照的前提是"除登记变量外全部相同"。本脚本比较两个批次目录的逐题
provenance:engine/model/上下文参数/环境必须一致,arm(及其派生策略描述)是
**唯一**允许的差异;发现未登记差异即非零退出并列出字段——绝不静默放行。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from app.evals.metrics import collect_runs, latest_per_bug

# 允许的差异字段:arm 是实验的登记变量;experiment_policy 由 arm 派生
ALLOWED_DIFFERENCES: frozenset[str] = frozenset({"arm", "experiment_policy"})


def _load_runs(runs_root: Path) -> dict[str, dict[str, Any]]:
    """按 (bug_id, arm) 取每题最新一次运行的 (report ∪ provenance) 扁平字典。"""
    rows = latest_per_bug(collect_runs(runs_root))
    paired: dict[str, dict[str, Any]] = {}
    for row in rows:
        flat: dict[str, Any] = {
            "bug_id": row.bug_id,
            "arm": str(row.provenance.get("arm") or "agent"),
            "experiment_policy": row.provenance.get("experiment_policy"),
            "engine": row.provenance.get("engine") or row.engine,
            "model_provider": row.provenance.get("model_provider") or row.model_provider,
            "model_name": row.provenance.get("model_name"),
            "max_turns": row.provenance.get("max_turns"),
            "execution_backend": row.provenance.get("execution_backend"),
            "git_commit": row.provenance.get("git_commit"),
            "verdict": row.verdict,
            "status": row.status,
            "tokens_used": row.tokens_used,
            "rounds": row.rounds,
            "duration_ms": row.duration_ms,
        }
        paired[f"{row.bug_id}@{flat['arm']}"] = flat
    return paired


def compare_batches(batch_a: Path, batch_b: Path) -> dict[str, Any]:
    """比较两个批次;返回配对摘要。发现未登记差异抛 SystemExit(2)。"""
    runs_a = _load_runs(batch_a)
    runs_b = _load_runs(batch_b)
    arms_a = {v["arm"] for v in runs_a.values()}
    arms_b = {v["arm"] for v in runs_b.values()}
    if arms_a != {"agent"} or arms_b != {"one_shot"}:
        raise SystemExit(
            f"batch_a must contain only agent runs {sorted(arms_a)},"
            f" batch_b only one_shot runs {sorted(arms_b)}"
        )
    keys_a = {k.split("@", 1)[0] for k in runs_a}
    keys_b = {k.split("@", 1)[0] for k in runs_b}
    missing = sorted(keys_a ^ keys_b)
    if missing:
        raise SystemExit(
            f"unpaired bugs between batches: {missing}"
            " — 两臂必须预先固定同一题单,缺失/多出的题不进入对照"
        )

    unregistered: list[str] = []
    pairs: list[dict[str, Any]] = []
    for bug_id in sorted(keys_a):
        a = runs_a[f"{bug_id}@agent"]
        b = runs_b[f"{bug_id}@one_shot"]
        pair: dict[str, Any] = {"bug_id": bug_id}
        for field in sorted(set(a) | set(b)):
            va, vb = a.get(field), b.get(field)
            if field in ALLOWED_DIFFERENCES:
                pair[field] = {"agent": va, "one_shot": vb}
                continue
            if va != vb:
                unregistered.append(f"{bug_id}: {field} differs: {va!r} != {vb!r}")
            pair[field] = va
        pair["verdicts"] = {"agent": a["verdict"], "one_shot": b["verdict"]}
        pair["tokens"] = {"agent": a["tokens_used"], "one_shot": b["tokens_used"]}
        pairs.append(pair)

    if unregistered:
        raise SystemExit(
            "unregistered differences between arms:\n  " + "\n  ".join(unregistered)
        )
    return {"pairs": pairs, "batch_a": str(batch_a), "batch_b": str(batch_b)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="同引擎两臂对照比较(未登记差异即报错)")
    parser.add_argument("batch_a", help="agent 臂批次目录")
    parser.add_argument("batch_b", help="one_shot 臂批次目录")
    parser.add_argument("--out", default="", help="可选:配对摘要 JSON 输出路径")
    args = parser.parse_args(argv)

    summary = compare_batches(Path(args.batch_a), Path(args.batch_b))
    print(
        json.dumps(
            summary,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    print(f"\npaired bugs: {len(summary['pairs'])}")
    if args.out:
        Path(args.out).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
