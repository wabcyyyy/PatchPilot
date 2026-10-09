"""付费批驱动器(预登记:docs/paid-batch-preregistration-2026-10-08.md)。

逐题串行、逐运行记账、停手条件强制执行。判据计算不在本脚本——驱动只负责
按登记参数发起运行并把每次结果如实追加进台账,批间裁决由人按预登记判据做。

用法:
  PYTHONPATH=. python scripts/run_paid_batch.py --phase q0 --model fake   # 零成本冒烟
  PYTHONPATH=. python scripts/run_paid_batch.py --phase q0 --model openai # Q0 真实
  PYTHONPATH=. python scripts/run_paid_batch.py --phase q1os --model openai \
      --bugs SWE-... --bugs SWE-...                                       # Q1 对照臂
退出码:0 全部完成;3 触发停手条件(400/花钱不入账);4 触顶 20M;1 参数/环境问题。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LEDGER = REPO / "runs" / "paid-batch-ledger-2026-10-09.jsonl"
LOCK = REPO / "runs" / "paid-batch.lock"
BUDGET_CAP_TOKENS = 20_000_000
RUN_TIMEOUT_SECONDS = 2400  # 任务级 1800s + 容器起停余量

Q0_BUGS = [
    "SWE-sphinx-doc__sphinx-7590",
    "SWE-sphinx-doc__sphinx-7748",
    "SWE-astropy__astropy-8707",
    "SWE-sphinx-doc__sphinx-8593",
    "SWE-sphinx-doc__sphinx-9461",
    "SWE-sphinx-doc__sphinx-8548",
    "SWE-pydata__xarray-3095",
]

# 预登记第四节的环境基线(两臂同值);阶段覆盖只许改登记表里那一个变量。
BASE_ENV = {
    "PATCHPILOT_LLM_ENABLED": "true",
    "PATCHPILOT_TOKEN_BUDGET": "400000",
    "PATCHPILOT_TASK_TIMEOUT_SECONDS": "1800",
    "PATCHPILOT_EXECUTION_BACKEND": "docker",
    "PATCHPILOT_LOCALIZE_BUDGET_SHARE": "0.6",
    "PATCHPILOT_PLAN_BUDGET_SHARE": "0.15",
    "PATCHPILOT_CONTEXT_WINDOW_TOKENS": "16000",
    "PATCHPILOT_CONTEXT_KEEP_RECENT_TURNS": "6",
    "PATCHPILOT_TOKEN_ESTIMATE_FACTOR": "1.0",
}
PHASE_ENV: dict[str, dict[str, str]] = {
    "q0": {},
    "q1os": {},
    "q2off": {"PATCHPILOT_CONTEXT_WINDOW_TOKENS": "0"},
    "q3f": {"PATCHPILOT_TOKEN_ESTIMATE_FACTOR": "1.47"},
}
PHASE_ARM = {"q0": "agent", "q1os": "one_shot", "q2off": "agent", "q3f": "agent"}
PHASE_OUT = {
    "q0": "runs/q0-noise-2026-10-08",
    "q1os": "runs/q1-one-shot-2026-10-09",
    "q2off": "runs/q2-compress-off-2026-10-09",
    "q3f": "runs/q3-factor-147-2026-10-09",
}


def _cumulative_tokens() -> int:
    if not LEDGER.exists():
        return 0
    return sum(
        int(json.loads(line).get("tokens_used") or 0)
        for line in LEDGER.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def _latest_report(out_root: Path, bug_id: str) -> Path | None:
    candidates = sorted(out_root.glob(f"{bug_id}-*/report.json"), key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None


def _append(entry: dict[str, object]) -> None:
    with LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", required=True, choices=list(PHASE_ENV))
    parser.add_argument("--model", required=True, choices=["fake", "openai"])
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--max-turns", type=int, default=24)
    parser.add_argument("--bugs", action="append", default=None, help="缺省用该相的登记题单")
    args = parser.parse_args(argv)

    bugs = args.bugs if args.bugs else Q0_BUGS
    out_root = REPO / PHASE_OUT[args.phase]
    out_root.mkdir(parents=True, exist_ok=True)

    LOCK.parent.mkdir(parents=True, exist_ok=True)
    try:
        LOCK.write_text(str(os.getpid()), encoding="utf-8")
    except OSError as exc:
        print(f"[lock] 无法创建运行锁({exc})——已有批在跑?停。", flush=True)
        return 1

    cumulative = _cumulative_tokens()
    print(
        f"[batch] phase={args.phase} model={args.model} runs={len(bugs) * args.reps} "
        f"已累计 tokens={cumulative}(上限 {BUDGET_CAP_TOKENS})",
        flush=True,
    )
    stop_code = 0
    try:
        for bug in bugs:
            for rep in range(1, args.reps + 1):
                if cumulative >= BUDGET_CAP_TOKENS:
                    print(
                        f"[stop] 累计 {cumulative} 触顶 {BUDGET_CAP_TOKENS},终止整批。", flush=True
                    )
                    return 4
                env = dict(os.environ)
                env.update(BASE_ENV)
                env.update(PHASE_ENV[args.phase])
                cmd = [
                    sys.executable,
                    "app/evals/run_single.py",
                    "--bug",
                    bug,
                    "--model",
                    args.model,
                    "--engine",
                    "graph",
                    "--arm",
                    PHASE_ARM[args.phase],
                    "--max-turns",
                    str(args.max_turns),
                    "--out",
                    PHASE_OUT[args.phase],
                ]
                print(
                    f"[run] {bug} rep{rep} phase={args.phase} arm={PHASE_ARM[args.phase]} "
                    f"overlay={PHASE_ENV[args.phase] or '无'}",
                    flush=True,
                )
                started = time.monotonic()
                try:
                    proc = subprocess.run(
                        cmd,
                        cwd=REPO,
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=RUN_TIMEOUT_SECONDS,
                    )
                    rc, tail = proc.returncode, proc.stdout[-2000:] + proc.stderr[-2000:]
                except subprocess.TimeoutExpired:
                    rc, tail = -9, f"driver timeout after {RUN_TIMEOUT_SECONDS}s"
                duration_s = round(time.monotonic() - started, 1)

                report_path = _latest_report(out_root, bug)
                entry: dict[str, object] = {
                    "phase": args.phase,
                    "model": args.model,
                    "bug": bug,
                    "rep": rep,
                    "arm": PHASE_ARM[args.phase],
                    "rc": rc,
                    "duration_s": duration_s,
                }
                if report_path is None:
                    entry.update({"report": None, "note": "无 report.json(运行时崩溃类)"})
                    print(f"[warn] {bug} rep{rep}: 无产物,{tail[-400:]}", flush=True)
                else:
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    error_text = str(report.get("error") or "")
                    tokens = int(report.get("tokens_used") or 0)
                    turns = int(report.get("turns") or 0)
                    entry.update(
                        {
                            "report": str(report_path.relative_to(REPO)),
                            "task_id": report.get("task_id"),
                            "status": report.get("status"),
                            "verdict": report.get("verdict"),
                            "turns": turns,
                            "tokens_used": tokens,
                            "tokens_prompt": report.get("tokens_prompt"),
                            "tokens_completion": report.get("tokens_completion"),
                            "rounds": report.get("rounds"),
                            "error": error_text[:500],
                        }
                    )
                    cumulative += tokens
                    # 停手条件 1:400 协议错误,或"花了钱/发了请求却零回合"(出处 4a4093e)。
                    if "400" in error_text or "reasoning_content" in error_text:
                        print(f"[stop] {bug} rep{rep}: 400 类协议错误 ⇒ 立刻停批。", flush=True)
                        stop_code = 3
                    elif turns == 0 and tokens > 0:
                        print(
                            f"[stop] {bug} rep{rep}: turns=0 但 tokens={tokens}(花钱不入账)⇒ 停批。",
                            flush=True,
                        )
                        stop_code = 3
                    elif turns == 0:
                        print(
                            f"[warn] {bug} rep{rep}: turns=0 且 tokens=0(未发起模型调用就崩),"
                            f"按预登记单列不计分母;tail={tail[-300:]}",
                            flush=True,
                        )
                _append(entry)
                print(
                    f"[done] {bug} rep{rep} rc={rc} {duration_s}s "
                    f"verdict={entry.get('verdict')} tokens={entry.get('tokens_used')} "
                    f"累计={cumulative}",
                    flush=True,
                )
                if stop_code:
                    return stop_code
    finally:
        LOCK.unlink(missing_ok=True)
    print(f"[batch] phase={args.phase} 完成,累计 tokens={cumulative}。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
