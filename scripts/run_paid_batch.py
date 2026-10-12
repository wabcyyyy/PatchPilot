"""付费批驱动器:**只认显式 manifest**,并提供零网络 dry-run。

历史口径(SPEC §7 G04 的修订对象):本脚本原来把 Q0 的题单、额度、停手条件与环境
写在代码里(`--phase q0`),新实验不显式换判据就会**继承上一批的停手条件**——
"这批按什么判停"由代码常量决定,而不是由登记决定。现在:

- `--manifest` 必填:题单、臂与登记变量、重复数、额度、停手条件、授权范围、产物目录
  全部来自那一份可 hash 的文件;台账逐条记 manifest_hash,事后能对上"当时批的口径";
- `--dry-run` 只展开矩阵与额度包(含控制身份与哈希),**不创建锁、不写台账、
  不发起任何子进程与网络请求**;
- 旧的 `--phase` 常量保留在文件里作**只读历史记录**(历史批次与预登记文档仍在读),
  但用它启动真实运行一律拒绝——静默继承旧判据比报错更贵。

用法:
  PYTHONPATH=. python scripts/run_paid_batch.py --manifest docs/.../exp.json --dry-run
  PYTHONPATH=. python scripts/run_paid_batch.py --manifest docs/.../exp.json
退出码:0 全部完成(或 dry-run);3 触发停手条件;4 触顶额度;1 参数/环境/manifest 问题。
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
RUN_TIMEOUT_SECONDS = 2400  # 任务级 1800s + 容器起停余量

# ---------------- 只读历史记录(不再驱动运行) ----------------
# 出处:docs/archive/paid-batch-preregistration-2026-10-08.md 与
# runs/paid-batch-ledger-2026-10-09.jsonl。删掉它们会让历史台账的 phase 字段无从解释,
# 所以保留;新批的一切判据来自 manifest,不许从这里"顺手抄一份"。
LEGACY_Q0_BUGS = [
    "SWE-sphinx-doc__sphinx-7590",
    "SWE-sphinx-doc__sphinx-7748",
    "SWE-astropy__astropy-8707",
    "SWE-sphinx-doc__sphinx-8593",
    "SWE-sphinx-doc__sphinx-9461",
    "SWE-sphinx-doc__sphinx-8548",
    "SWE-pydata__xarray-3095",
]
LEGACY_BASE_ENV = {
    "PATCHPILOT_LLM_ENABLED": "true",
    # 2026-10-09 钉死:端点侧对思考模式的 reasoning_content 回传校验是**间歇性**的
    # (同一请求先 400 后 200,探针 docs/evidence/probe-thinking-400/ T0 实证),无法用客户端形状
    # 确定性规避;disabled 下端点每轮 reasoning_content=None(P3/probe_disabled_effect 实证)。
    "PATCHPILOT_LLM_THINKING": "disabled",
    "PATCHPILOT_TOKEN_BUDGET": "400000",
    "PATCHPILOT_TASK_TIMEOUT_SECONDS": "1800",
    "PATCHPILOT_EXECUTION_BACKEND": "docker",
    "PATCHPILOT_LOCALIZE_BUDGET_SHARE": "0.6",
    "PATCHPILOT_PLAN_BUDGET_SHARE": "0.15",
    "PATCHPILOT_CONTEXT_WINDOW_TOKENS": "16000",
    "PATCHPILOT_CONTEXT_KEEP_RECENT_TURNS": "6",
    "PATCHPILOT_TOKEN_ESTIMATE_FACTOR": "1.0",
}
LEGACY_PHASE_ENV: dict[str, dict[str, str]] = {
    "q0": {},
    "q1os": {},
    "q2off": {"PATCHPILOT_CONTEXT_WINDOW_TOKENS": "0"},
    "q3f": {"PATCHPILOT_TOKEN_ESTIMATE_FACTOR": "1.47"},
}
LEGACY_PHASE_ARM = {"q0": "agent", "q1os": "one_shot", "q2off": "agent", "q3f": "agent"}
LEGACY_PHASE_OUT = {
    "q0": "runs/q0-noise-2026-10-08",
    "q1os": "runs/q1-one-shot-2026-10-09",
    "q2off": "runs/q2-compress-off-2026-10-09",
    "q3f": "runs/q3-factor-147-2026-10-09",
}
# ----------------------------------------------------------------


def _cumulative_tokens() -> int:
    if not LEDGER.exists():
        return 0
    return sum(
        int(json.loads(line).get("tokens_used") or 0)
        for line in LEDGER.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def _cumulative_cost_micro_usd() -> int:
    if not LEDGER.exists():
        return 0
    total = 0
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        total += int((entry.get("resources") or {}).get("cost_micro_usd") or 0)
    return total


def _done_attempts(manifest_hash: str) -> set[tuple[str, str, int]]:
    """该 manifest 已有 report 的 (bug, arm, rep) —— 已入账的尝试不重做(失败计分母)。

    键里带 manifest_hash:换了登记口径就是另一批实验,不能借旧台账少跑几次。
    """
    done: set[tuple[str, str, int]] = set()
    if not LEDGER.exists():
        return done
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("manifest_hash") == manifest_hash and entry.get("report"):
            done.add((str(entry["bug"]), str(entry["arm"]), int(entry["rep"])))
    return done


def _is_protocol_400(error_text: str) -> bool:
    """预登记停手条件 1 的 400 信号:openai SDK 形态 'Error code: 400 - …'
    或思考模式 reasoning_content 协议错。不能裸匹配 "400"——预算数字(400000)会误中。"""
    lowered = error_text.lower()
    return "error code: 400" in lowered or "reasoning_content" in lowered


def _latest_report(out_root: Path, bug_id: str) -> Path | None:
    candidates = sorted(out_root.glob(f"{bug_id}-*/report.json"), key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None


def _append(entry: dict[str, object]) -> None:
    with LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _render_plan(manifest: object) -> dict[str, object]:
    """dry-run 的展示面:矩阵、额度包、控制身份与哈希。纯计算,零副作用。"""
    matrix = manifest.run_matrix()  # type: ignore[attr-defined]
    return {
        "manifest_id": manifest.manifest_id,  # type: ignore[attr-defined]
        "manifest_hash": manifest.manifest_hash,  # type: ignore[attr-defined]
        "question": manifest.question,  # type: ignore[attr-defined]
        "engine": manifest.engine,  # type: ignore[attr-defined]
        "model": manifest.model,  # type: ignore[attr-defined]
        "budget_envelope": manifest.budget_envelope(),  # type: ignore[attr-defined]
        "planned_runs": [
            {k: entry[k] for k in ("bug", "arm", "repetition", "variables")} for entry in matrix
        ],
        "network_requests": 0,
        "model_requests": 0,
        "note": "dry-run 不建锁、不写台账、不发起子进程;这一行本身也是被登记的事实",
    }


def main(argv: list[str] | None = None) -> int:
    from app.evals.experiment_manifest import ManifestError, read_manifest

    # 本机控制台默认 GBK,而台账/计划里会出现"⇒"这类非 GBK 字符:打印不是失败点,
    # 让它降级成替换字符,而不是把一次好好的 dry-run 崩成 UnicodeEncodeError。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="按显式 manifest 驱动一批运行")
    parser.add_argument("--manifest", required=True, help="实验 manifest JSON(必填)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只展开矩阵/额度/哈希,不发任何请求(零网络、零子进程、零台账写入)",
    )
    parser.add_argument("--phase", default="", help="历史 phase 名:仅用于拒绝旧命令")
    args = parser.parse_args(argv)

    if args.phase:
        print(
            f"[reject] --phase {args.phase!r} 不再驱动运行:那会静默继承 Q0 的题单与停手条件。"
            f" 请为该批写一份显式 manifest(历史常量仍在本文件里作只读记录)。",
            flush=True,
        )
        return 1

    try:
        manifest = read_manifest(Path(args.manifest))
    except (OSError, ManifestError) as exc:
        print(f"[manifest] 读取/校验失败:{exc}", flush=True)
        return 1

    if args.dry_run:
        print(json.dumps(_render_plan(manifest), ensure_ascii=False, indent=2, default=str))
        return 0

    max_turns = manifest.budget.get("max_turns")  # 批次级参数:manifest 没写就不许跑
    if not isinstance(max_turns, int) or max_turns <= 0:
        print("[manifest] budget.max_turns 缺失:批次级参数也要登记,不用默认值凑", flush=True)
        return 1

    out_root = REPO / manifest.outputs_root
    cap_tokens = manifest.budget.get("batch_cap_tokens")
    cap_cost = manifest.authorization.get("max_cost_micro_usd")
    online = str(manifest.model.get("provider", "")) == "openai"
    if online and not str(os.environ.get("PATCHPILOT_LLM_API_KEY") or ""):
        print("[stop] 在线批缺 PATCHPILOT_LLM_API_KEY:不猜凭据,拒绝发起", flush=True)
        return 1

    manifest_hash = manifest.manifest_hash
    done = _done_attempts(manifest_hash)
    cumulative = _cumulative_tokens()
    spent_cost = _cumulative_cost_micro_usd()
    plan = manifest.run_matrix()
    print(
        f"[batch] manifest={manifest.manifest_id} hash={manifest_hash[:12]} "
        f"runs={len(plan)} 已累计 tokens={cumulative}(批上限 {cap_tokens})"
        f" 已花 {spent_cost} micro-USD(授权上限 {cap_cost});跳过已入账={len(done)}",
        flush=True,
    )

    LOCK.parent.mkdir(parents=True, exist_ok=True)
    try:
        LOCK.write_text(str(os.getpid()), encoding="utf-8")
    except OSError as exc:
        print(f"[lock] 无法创建运行锁({exc})——已有批在跑?停。", flush=True)
        return 1

    stop_code = 0
    try:
        for entry_plan in plan:
            bug = str(entry_plan["bug"])
            arm = str(entry_plan["arm"])
            rep = int(entry_plan["repetition"])
            key = (bug, arm, rep)
            if key in done:
                print(f"[skip] {bug} {arm} rep{rep}: 台账已有该尝试,不重做。", flush=True)
                continue
            if isinstance(cap_tokens, int) and cumulative >= cap_tokens:
                print(f"[stop] 累计 {cumulative} 触顶 {cap_tokens},终止整批。", flush=True)
                return 4
            if isinstance(cap_cost, int) and cap_cost > 0 and spent_cost >= cap_cost:
                print(
                    f"[stop] 累计费用 {spent_cost} micro-USD 触顶授权 {cap_cost},终止整批。",
                    flush=True,
                )
                return 4

            env = dict(os.environ)
            env.update({k: str(v) for k, v in entry_plan["variables"].items()})
            env["PATCHPILOT_EXPERIMENT_MANIFEST_HASH"] = manifest_hash
            cmd = [
                sys.executable,
                "app/evals/run_single.py",
                "--bug",
                bug,
                "--model",
                str(manifest.model.get("provider", "")).split("-")[0] or "openai",
                "--engine",
                manifest.engine,
                "--arm",
                arm,
                "--max-turns",
                str(max_turns),
                "--out",
                manifest.outputs_root,
            ]
            print(
                f"[run] {bug} {arm} rep{rep} overlay={entry_plan['variables'] or '无'}", flush=True
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
            ledger_entry: dict[str, object] = {
                "manifest_id": manifest.manifest_id,
                "manifest_hash": manifest_hash,
                "model": manifest.model,
                "bug": bug,
                "rep": rep,
                "arm": arm,
                "rc": rc,
                "duration_s": duration_s,
            }
            if report_path is None:
                ledger_entry.update({"report": None, "note": "无 report.json(运行时崩溃类)"})
                print(f"[warn] {bug} {arm} rep{rep}: 无产物,{tail[-400:]}", flush=True)
            else:
                report = json.loads(report_path.read_text(encoding="utf-8"))
                error_text = str(report.get("error") or "")
                tokens = int(report.get("tokens_used") or 0)
                turns = int(report.get("turns") or 0)
                resources = report.get("resources") or {}
                ledger_entry.update(
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
                        "resources": {
                            "cost_micro_usd": resources.get("cost_micro_usd"),
                            "cost_by_basis": resources.get("cost_by_basis"),
                            "price_provenance": resources.get("price_provenance"),
                            "unknown_held_tokens": resources.get("unknown_held_tokens"),
                            "resource_status": resources.get("resource_status"),
                        },
                        "provenance_identity": {
                            "git_commit": (report.get("provenance") or {}).get("git_commit"),
                            "dirty_fingerprint": (report.get("provenance") or {}).get(
                                "dirty_fingerprint"
                            ),
                        },
                        "error": error_text[:500],
                    }
                )
                cumulative += tokens
                spent_cost += int(resources.get("cost_micro_usd") or 0)
                if _is_protocol_400(error_text):
                    print(f"[stop] {bug} {arm} rep{rep}: 400 类协议错误 ⇒ 立刻停批。", flush=True)
                    stop_code = 3
                elif turns == 0 and tokens > 0:
                    print(
                        f"[stop] {bug} {arm} rep{rep}: turns=0 但 tokens={tokens}(花钱不入账)⇒ 停批。",
                        flush=True,
                    )
                    stop_code = 3
                elif turns == 0:
                    print(
                        f"[warn] {bug} {arm} rep{rep}: turns=0 且 tokens=0(未发起模型调用就崩),"
                        f"按登记单列不计分母;tail={tail[-300:]}",
                        flush=True,
                    )
            _append(ledger_entry)
            print(
                f"[done] {bug} {arm} rep{rep} rc={rc} {duration_s}s "
                f"verdict={ledger_entry.get('verdict')} tokens={ledger_entry.get('tokens_used')} "
                f"累计={cumulative}",
                flush=True,
            )
            if stop_code:
                return stop_code
    finally:
        LOCK.unlink(missing_ok=True)
    print(f"[batch] manifest={manifest.manifest_id} 完成,累计 tokens={cumulative}。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
