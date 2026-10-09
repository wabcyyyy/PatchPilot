"""付费批台账汇总(预登记判据的代码化计算,不靠眼睛)。

用法:
  PYTHONPATH=. python scripts/summarize_paid_batch.py --phase q0 --model openai
输出:逐题逐次一行 + Q0 判据(res_i 分布、可判别题 k、k>=2 是否允许启动 Q1)、
tokens 极差/p50,以及无 report 的事故条目单列。判据原文见
docs/paid-batch-preregistration-2026-10-08.md 第五节,本脚本不改判据,只执行它。
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LEDGER = REPO / "runs" / "paid-batch-ledger-2026-10-09.jsonl"


def _load(phase: str, model: str) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    attempts: list[dict[str, object]] = []
    incidents: list[dict[str, object]] = []
    if not LEDGER.exists():
        return attempts, incidents
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("phase") != phase or entry.get("model") != model:
            continue
        if entry.get("report"):
            attempts.append(entry)
        else:
            incidents.append(entry)
    return attempts, incidents


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", default="q0")
    parser.add_argument("--model", default="openai")
    args = parser.parse_args(argv)

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
