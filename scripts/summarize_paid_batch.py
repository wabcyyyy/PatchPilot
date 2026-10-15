"""付费批台账汇总。

两条路径,分得很清:

- `--manifest-hash <hash>`(G04 起的新口径):按 manifest 聚合逐题逐次结果,只描述
  **观测分布**(每次运行的 verdict/tokens/cost),不套用任何批的判据,也不写"修复率提升";
- `--phase q0 --model openai`(历史只读口径):Q0 预登记判据的代码化计算,原文见
  docs/archive/paid-batch-preregistration-2026-10-08.md 第五节。它只适用于那份 7 题 × 3 次
  的旧批;**新批不得继承它**——这正是 SPEC §7 G04 要结束的"隐式选择旧 Q0 判据"。

本脚本不改判据也不改台账,只读。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
LEDGER = REPO / "runs" / "paid-batch-ledger-2026-10-09.jsonl"


def _entries() -> list[dict[str, Any]]:
    if not LEDGER.exists():
        return []
    return [
        json.loads(line) for line in LEDGER.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _summarize_manifest(entries: list[dict[str, Any]], manifest_hash: str) -> int:
    """按 (bug, arm, rep) 列出该 manifest 的每一次运行:一条都不折、一条都不丢。"""
    rows = [e for e in entries if e.get("manifest_hash") == manifest_hash]
    if not rows:
        print(f"[manifest] 台账里没有 manifest_hash={manifest_hash} 的运行")
        return 1
    cells: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entry in rows:
        cells.setdefault((str(entry["bug"]), str(entry["arm"])), []).append(entry)
    costs = [int(((e.get("resources") or {}).get("cost_micro_usd")) or 0) for e in rows]
    tokens = [int(e.get("tokens_used") or 0) for e in rows]
    resolved_count = sum(1 for e in rows if str(e.get("verdict")) == "resolved")
    delivered_count = sum(
        1
        for e in rows
        if str(e.get("delivery_status")) == "delivered"
        or (str(e.get("verdict")) == "resolved" and str(e.get("review_status")) == "approved")
    )
    p50_tok = statistics.median(tokens) if tokens else 0
    p95_tok = (
        statistics.quantiles(tokens, n=20)[18]
        if len(tokens) >= 20
        else (max(tokens) if tokens else 0)
    )

    status_counts: dict[str, int] = {}
    for e in rows:
        st = str(e.get("status") or "UNKNOWN")
        status_counts[st] = status_counts.get(st, 0) + 1

    cost_per_accepted = (sum(costs) / delivered_count) if delivered_count > 0 else 0.0

    print(
        f"== manifest_hash={manifest_hash[:12]} runs={len(rows)} cells={len(cells)} ==\n"
        f"指标概览:\n"
        f"  - 解决用例数: {resolved_count}/{len(rows)} ({resolved_count / len(rows) * 100:.1f}%)\n"
        f"  - 交付/采纳数: {delivered_count}/{len(rows)} (采纳率 {delivered_count / len(rows) * 100:.1f}%)\n"
        f"  - 总费用: {sum(costs)} micro-USD (平均每采纳补丁成本: {cost_per_accepted:.0f} micro-USD)\n"
        f"  - Token消耗: p50={p50_tok:.0f}, p95={p95_tok:.0f}, 合计={sum(tokens)}\n"
        f"  - 失败分类分布: {status_counts}"
    )
    for (bug, arm), runs in sorted(cells.items()):
        runs_sorted = sorted(runs, key=lambda e: int(e["rep"]))
        print(
            f"{bug} @{arm}: reps={len(runs_sorted)} "
            f"verdicts={[str(e.get('verdict')) for e in runs_sorted]} "
            f"tokens={[int(e.get('tokens_used') or 0) for e in runs_sorted]}"
        )
        for e in runs_sorted:
            print(
                f"   rep{e['rep']}: status={e.get('status')} turns={e.get('turns')} "
                f"cost={((e.get('resources') or {}).get('cost_micro_usd'))} "
                f"delivery={e.get('delivery_status')} "
                f"error={str(e.get('error') or '')[:80]}"
            )
    print(
        "\n说明:以上是观测分布与工业指标,不是机制归因。小样本不支持的结论不予外推;\n"
        "要把差异归因到某个改动,必须把该改动登记为变量并另建配对批(SPEC §7 G04/G17)。\n"
        "环境失败与门禁拦截保留分母,不挑结果重跑。"
    )
    return 0


def _load(phase: str, model: str) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    attempts: list[dict[str, object]] = []
    incidents: list[dict[str, object]] = []
    for entry in _entries():
        if entry.get("phase") != phase or entry.get("model") != model:
            continue
        if entry.get("report"):
            attempts.append(entry)
        else:
            incidents.append(entry)
    return attempts, incidents


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="台账汇总(新批按 manifest,旧批按 phase)")
    parser.add_argument("--manifest-hash", default="", help="G04 新口径:按 manifest 聚合每次运行")
    parser.add_argument("--phase", default="", help="历史只读口径(如 q0)")
    parser.add_argument("--model", default="openai")
    args = parser.parse_args(argv)

    entries = _entries()
    if args.manifest_hash:
        return _summarize_manifest(entries, args.manifest_hash)
    if not args.phase:
        parser.error("必须指定 --manifest-hash(新批)或 --phase(历史批)")

    attempts, incidents = _load(args.phase, args.model)
    by_bug: dict[str, list[dict[str, object]]] = {}
    for entry in attempts:
        by_bug.setdefault(str(entry["bug"]), []).append(entry)

    print(f"== {args.phase}({args.model}) 尝试 {len(attempts)} 次,事故 {len(incidents)} 次 ==")
    res_map: dict[str, int] = {}
    for bug, runs in sorted(by_bug.items()):
        runs_sorted = sorted(runs, key=lambda e: int(e["rep"]))  # type: ignore[arg-type]
        tokens = [int(e.get("tokens_used") or 0) for e in runs_sorted]
        verdicts = [str(e.get("verdict")) for e in runs_sorted]
        res = sum(1 for v in verdicts if v == "resolved")
        res_map[bug] = res
        spread = (max(tokens) - min(tokens)) if len(tokens) > 1 else 0
        kills = [
            str(e.get("error") or "")[:60].replace("\n", " ") for e in runs_sorted if e.get("error")
        ]
        print(
            f"{bug}: res={res}/{len(runs_sorted)} tokens={tokens} 极差={spread} "
            f"turns={[e.get('turns') for e in runs_sorted]}"
            + (f" 判死={kills[0]}..." if kills else "")
        )
        for e in runs_sorted:
            print(
                f"   rep{e['rep']}: verdict={e.get('verdict')} status={e.get('status')} "
                f"turns={e.get('turns')} tokens={e.get('tokens_used')} "
                f"error={str(e.get('error') or '')[:80]}"
            )

    for entry in incidents:
        print(
            f"[事故单列] {entry['bug']} rep{entry['rep']}: tokens≈{entry.get('tokens_used')} "
            f"{entry.get('note')}"
        )

    if args.phase == "q0" and len(res_map) == 7 and all(len(v) == 3 for v in by_bug.values()):
        discriminable = sorted(b for b, r in res_map.items() if r in (1, 2))
        k = len(discriminable)
        all_tokens = [int(e.get("tokens_used") or 0) for runs in by_bug.values() for e in runs]
        print("\n== Q0 判据(预登记第五节,跑完不许动) ==")
        print(f"res_i 分布: {res_map}")
        print(f"可判别题 k={k} {discriminable}")
        print(f"tokens 全批 p50={statistics.median(all_tokens):.0f} 合计={sum(all_tokens)}")
        if k >= 2:
            print("判据: k>=2 ⇒ 允许启动 Q1(只在可判别题上,两臂 × k × 3)。")
        else:
            print(
                "判据: k<=1 ⇒ 停止付费。结论:deepseek-flash 在这档难度上的结果方差"
                "淹没有望检测的机制差异,本证据集不支持任何净增益主张。"
            )
    elif args.phase == "q0":
        print(
            f"\n[!] Q0 尚不完整:应有 7 题 × 3 次,现有 {sum(len(v) for v in by_bug.values())} 次——判据暂不评估。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
