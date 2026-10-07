"""零成本回放批次的逐题对照工具(夜间升级循环的判定/成本回归闸)。

用法:
    python scripts/compare_batches.py <基线批目录> <对照批目录> [--tools]

口径:
- **判定字段**(status/verdict/rounds/gate_violations/changed_files/verify 双旗标…)必须逐题相等,
  任一不等即退出码 1——这些字段变了就是行为回归,不是"成本变了";
- **成本字段**(turns/tokens/duration)允许变,只列差异不改退出码;
  注意 fake 批的 tokens 是"回放脚本自身长度/4"的估算,只用于趋势对比,不是行为指标;
- `--tools` 额外比对 `trajectory.jsonl` 的逐阶段工具调用直方图(定位阶段真的少查了吗),
  并统计 `context_compact` 事件数。

题 id 以 `report.json` 的 `bug_id` 为键;两侧题集不同只报"缺题",不静默丢题。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

JUDGEMENT_FIELDS = (
    "status",
    "verdict",
    "outcome",
    "rounds",
    "gate_violations",
    "changed_files",
    "baseline_failed",
    "baseline_regression_ok",
    "verify_failed_ok",
    "verify_regression_ok",
    "error",
)
COST_FIELDS = ("turns", "tokens_used", "duration_ms")


def load_batch(root: Path) -> dict[str, dict[str, Any]]:
    """按 bug_id 收一份批次的 report.json;同一 bug_id 出现多次直接报错(消融臂混入的坑)。"""
    out: dict[str, dict[str, Any]] = {}
    for report in sorted(root.glob("*/report.json")):
        data = json.loads(report.read_text(encoding="utf-8"))
        bug_id = str(data["bug_id"])
        if bug_id in out:
            raise SystemExit(f"{root}: bug_id {bug_id} 出现多次(目录混了两批?),拒绝静默去重")
        data["_run_dir"] = str(report.parent)
        out[bug_id] = data
    return out


def tool_histogram(run_dir: str) -> dict[str, Counter[str]]:
    """逐阶段(state)的工具调用计数,来自 trajectory.jsonl。"""
    path = Path(run_dir) / "trajectory.jsonl"
    if not path.exists():
        return {}
    hist: dict[str, Counter[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        state = str(event.get("state") or "-")
        tool = str(event.get("tool") or "-")
        hist.setdefault(state, Counter())[tool] += 1
    return hist


def compact_events(run_dir: str) -> int:
    path = Path(run_dir) / "trajectory.jsonl"
    if not path.exists():
        return 0
    return sum(
        1
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("tool") == "context_compact"
    )


def _fmt(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ",".join(str(v) for v in value) + "]"
    return str(value)


def compare(base: Path, cand: Path, with_tools: bool) -> int:
    a, b = load_batch(base), load_batch(cand)
    missing = sorted(set(a) - set(b))
    extra = sorted(set(b) - set(a))
    judgement_diffs = 0
    print(f"基线 {base}: {len(a)} 题 | 对照 {cand}: {len(b)} 题")
    if missing or extra:
        print(f"[缺题] 只在基线: {missing or '-'} | 只在对照: {extra or '-'}")
        judgement_diffs += len(missing) + len(extra)

    for bug_id in sorted(set(a) & set(b)):
        rows_a, rows_b = a[bug_id], b[bug_id]
        hard = [f for f in JUDGEMENT_FIELDS if _fmt(rows_a.get(f)) != _fmt(rows_b.get(f))]
        soft = [f for f in COST_FIELDS if _fmt(rows_a.get(f)) != _fmt(rows_b.get(f))]
        if not hard and not soft:
            continue
        judgement_diffs += len(hard)
        parts = [f"{f}: {_fmt(rows_a.get(f))} -> {_fmt(rows_b.get(f))}" for f in hard + soft]
        print(f"[{'JUDGE' if hard else 'cost '}] {bug_id} " + " | ".join(parts))
        if with_tools:
            ha, hb = tool_histogram(rows_a["_run_dir"]), tool_histogram(rows_b["_run_dir"])
            for state in sorted(set(ha) | set(hb)):
                ca, cb = ha.get(state, Counter()), hb.get(state, Counter())
                if ca != cb:
                    keys = sorted(set(ca) | set(cb))
                    delta = ", ".join(f"{k}:{ca.get(k, 0)}->{cb.get(k, 0)}" for k in keys)
                    print(f"        tools/{state}: {delta}")
            na, nb = compact_events(rows_a["_run_dir"]), compact_events(rows_b["_run_dir"])
            if na or nb:
                print(f"        context_compact: {na} -> {nb}")

    turns_total = [sum(int(rows.get("turns") or 0) for rows in batch.values()) for batch in (a, b)]
    tokens_total = [
        sum(int(rows.get("tokens_used") or 0) for rows in batch.values()) for batch in (a, b)
    ]
    print(
        f"合计 turns {turns_total[0]} -> {turns_total[1]} | "
        f"tokens {tokens_total[0]} -> {tokens_total[1]}"
    )
    if judgement_diffs:
        print(f"判定字段差异 {judgement_diffs} 处 —— 行为回归,不通过")
        return 1
    print("判定字段逐题一致(成本/轨迹差异见上,属预期)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="逐题对照两个零成本回放批次")
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--tools", action="store_true", help="附带逐阶段工具调用直方图对照")
    args = parser.parse_args(argv)
    for path in (args.baseline, args.candidate):
        if not path.is_dir():
            parser.error(f"目录不存在: {path}")
    return compare(args.baseline, args.candidate, args.tools)


if __name__ == "__main__":
    raise SystemExit(main())
