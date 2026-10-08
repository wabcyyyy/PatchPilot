"""真实模型语料的上下文重建与 token 估算器标定(A1 卡,零成本、不起模型)。

背景:架构升级的每个机制都只证到"接线不破行为",对额度/上下文尺寸的作用一条都没量过
(TODO M1.5/M8.1、ADR-0004 都写着"fake 语料够不到阈值")。但 `runs/swe-*` 躺着 25 份
真实模型 run(provenance 无 context_window_tokens、轨迹里 context_compact 事件 0 次
⇒ 这批是**压缩未介入**的干净前对照语料),每条都带逐轮全文轨迹与 provider 报的真 token。

本卡只做一件事:把逐轮**请求侧消息列表**从轨迹里重建出来,并证明重建可信,然后量
`app.llm.base.messages_tokens`(len//4)相对真实计费的偏差。阈值反事实交给
`scripts/measure_context_counterfactual.py` 复用本模块。

跑法:
    PYTHONPATH=. .venv/Scripts/python.exe scripts/context_replay.py
    PYTHONPATH=. ... scripts/context_replay.py --json-out=<落盘路径>   # 机器可读产物

**判据在看到结果之前写死在这里**(改判据要改这段文字并说明理由):
- F1 逐 run 守恒:轨迹里所有 `llm` 事件的 tokens 之和 == report.json 的 tokens_used。
  任一道不成立 = 装载/分组写错了,后面的数一律不算数。唯一的**登记过的例外**:
  report.tokens_used 恒 0 而轨迹有值 —— 那是 commit 4a4093e 修过的"崩溃路径用量不入账"
  缺陷实物(对外文档"不能写"一节里点过的那份报告),单独列示,既不算装载失败也不算通过。
- F2 动作序列对齐:每个 `llm` 事件记录的 tool_calls 名字,必须等于它之后、下一个 `llm`
  之前那些工具事件的名字序列(允许 finish 提前收尾)。错配率 > 2% 即分组不可信。
- F3 重建可用门槛:对"重建估算 vs 真实用量"做最小二乘拟合,R² ≥ 0.90 且中位相对误差
  ≤ 15% 才允许报**点估计**;达不到就只报**上下界**,并在此文件里如实写没达到。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app.config import get_settings  # noqa: E402
from app.evals.bugset import load_bug  # noqa: E402
from app.llm.base import messages_tokens  # noqa: E402
from app.tools.output_filter import fold_output  # noqa: E402

# 真实批次跑在**六个不同 commit**上(3f17a9cd / 1a2646e2 / 80981f04 / 58472254 / 293cd649 /
# 66e6e309,见 F0 自检),所以提示词必须按**每个 run 自己的 provenance.git_commit** 取,
# 不能钉死一个常量。这里只作 F0 的默认回退值。
REAL_COMMIT = "80981f04b94e7212bf4e572c8b16951cd5147823"
PROMPT_KEYS = ("SYSTEM_PROMPT", "LOCALIZE_PROMPT", "PROPOSE_PROMPT")
_prompt_cache: dict[str, dict[str, Any]] = {}
LOCALIZE_STATE = "LOCALIZE"
# 循环内会进消息流的工具;节点级记录(baseline/verify/apply_gate…)不属于工作记忆。
LOOP_TOOLS = {
    "list_files",
    "search_code",
    "read_file",
    "git_diff",
    "run_tests",
    "apply_patch",
    "finish",
}
THOUGHT_CAP = 2000  # plain_loop._MAX_THOUGHT_CHARS:轨迹里 thought 的截断上限
ARG_CAP = 300  # registry._summarize_input:字符串入参的截断上限
TRUNC_MARK = re.compile(r"\.\.\. \((\d+) chars\)$")
DIFF_MARK = re.compile(r"^<(\d+) chars, see patches>$")


@dataclass
class Turn:
    turn_no: int
    content: str
    tool_names: list[str]
    tools: list[tuple[str, dict[str, Any], Any]]
    real_tokens: int
    missing_chars: int  # 轨迹记录形式必然少掉的字符(截断的 thought / 入参 / diff 占位)


@dataclass
class LoopInstance:
    """一次 run_plain_loop 调用:同 (state, round) 的事件流共享一个消息列表。"""

    state: str
    round_no: int
    turns: list[Turn] = field(default_factory=list)

    @property
    def real_tokens_total(self) -> int:
        return sum(t.real_tokens for t in self.turns)


@dataclass
class Run:
    bug_id: str
    batch: str
    run_dir: Path
    report: dict[str, Any]
    instances: list[LoopInstance]
    llm_events: int
    tokens_used_recorded: int

    @property
    def localize(self) -> LoopInstance | None:
        for inst in self.instances:
            if inst.state == LOCALIZE_STATE:
                return inst
        return None


def git_text(rel_path: str, commit: str) -> str:
    raw = subprocess.run(
        ["git", "-C", str(REPO), "show", f"{commit}:{rel_path}"],
        check=True,
        capture_output=True,
    ).stdout
    return raw.decode("utf-8")


def prompts_at_commit(commit: str) -> dict[str, Any]:
    ns: dict[str, Any] = {}
    exec(git_text("app/prompts.py", commit), ns)  # 读自家历史常量,不是外部输入
    return ns


def prompts_for(commit: str) -> dict[str, Any]:
    """按 run 自己的 commit 取提示词模板(语料跨六个 commit,不能钉死一个)。"""
    if commit not in _prompt_cache:
        _prompt_cache[commit] = prompts_at_commit(commit)
    return _prompt_cache[commit]


def prompt_signature(commit: str) -> str:
    """F0 用的模板指纹:三段提示词拼接后的 sha256 前 12 位。"""
    import hashlib

    ns = prompts_for(commit)
    blob = "".join(f"{k}={ns.get(k, '')}" for k in PROMPT_KEYS)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def load_events(run_dir: Path) -> list[dict[str, Any]]:
    lines = (run_dir / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _truncated_chars(value: str, cap: int) -> int:
    """从 "... (N chars)" 标记还原被记录形式丢掉的字符数。"""
    m = TRUNC_MARK.search(value)
    if not m:
        return 0
    return max(0, int(m.group(1)) - cap)


def _missing_in_payload(obj: Any) -> int:
    """扫描记录摘要里的截断/占位标记,累计"真实请求里更长"的字符差。"""
    missing = 0
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, str):
                if key == "diff":
                    m = DIFF_MARK.match(value)
                    if m:
                        # 真 diff 会把 JSON 顶过 fold 的 8000 硬上限;差值按 8000 记
                        missing += 8000
                else:
                    missing += _truncated_chars(value, ARG_CAP)
            missing += _missing_in_payload(value)
    elif isinstance(obj, list):
        for item in obj:
            missing += _missing_in_payload(item)
    return missing


def build_instances(events: list[dict[str, Any]]) -> list[LoopInstance]:
    """按 (state, round) 分组;组内以 llm 事件为回合边界,后续工具事件是该回合的回执。"""
    instances: list[LoopInstance] = []
    current: LoopInstance | None = None
    open_turn: Turn | None = None
    turn_tools: list[tuple[str, dict[str, Any], Any]] = []
    turn_names: list[str] = []
    turn_missing = 0

    def close_turn() -> None:
        nonlocal open_turn, turn_tools, turn_names, turn_missing
        if open_turn is None:
            return
        open_turn.tools = turn_tools
        open_turn.tool_names = turn_names
        open_turn.missing_chars += turn_missing
        turn_tools, turn_names, turn_missing = [], [], 0
        open_turn = None

    for ev in events:
        tool = ev.get("tool")
        state = str(ev.get("state") or "")
        round_no = int(ev.get("round") or 0)
        if tool == "llm":
            # 每个新回合开始时先结转到合上一个回合:它的回执已经在事件流里到齐了。
            # (漏掉这一步会让所有工具回执都不进消息列表,重建出来的上下文就"永远不涨")
            close_turn()
            if current is None or (current.state, current.round_no) != (state, round_no):
                current = LoopInstance(state=state, round_no=round_no)
                instances.append(current)
            assert current is not None
            out = ev.get("output_summary") or {}
            content = str(out.get("content") or "")
            recorded_turn = int((ev.get("input") or {}).get("turn") or 0)
            open_turn = Turn(
                turn_no=recorded_turn or len(current.turns) + 1,
                content=content,
                tool_names=[str(n) for n in (out.get("tool_calls") or [])],
                tools=[],
                real_tokens=int(out.get("tokens") or 0),
                missing_chars=_truncated_chars(content, THOUGHT_CAP),
            )
            turn_tools, turn_names, turn_missing = [], [], 0
            current.turns.append(open_turn)
            continue
        if tool in LOOP_TOOLS and open_turn is not None:
            turn_tools.append((str(tool), dict(ev.get("input") or {}), ev.get("output_summary")))
            turn_names.append(str(tool))
            turn_missing += _missing_in_payload(ev.get("input")) + _missing_in_payload(
                ev.get("output_summary")
            )
            if tool == "finish":  # finish 直接 return,该 turn 之后不再有回执
                close_turn()
    close_turn()
    return [i for i in instances if i.turns]


def load_runs() -> list[Run]:
    runs: list[Run] = []
    for report_path in sorted(REPO.glob("runs/swe-*/*/report.json")):
        report = json.loads(report_path.read_text(encoding="utf-8"))
        prov = report.get("provenance") or {}
        if prov.get("llm_enabled") is not True or report.get("model_provider") != "openai":
            continue  # fake 回放批不在这个口径里
        run_dir = report_path.parent
        events = load_events(run_dir)
        llm_events = [e for e in events if e.get("tool") == "llm"]
        runs.append(
            Run(
                bug_id=str(report.get("bug_id") or run_dir.name),
                batch=run_dir.parent.name,
                run_dir=run_dir,
                report=report,
                instances=build_instances(events),
                llm_events=len(llm_events),
                tokens_used_recorded=sum(
                    int((e.get("output_summary") or {}).get("tokens") or 0) for e in llm_events
                ),
            )
        )
    return runs


def head_for(commit: str) -> tuple[str, str]:
    """(SYSTEM_PROMPT, LOCALIZE_PROMPT) —— A2 复用同一入口,保证两边头部同源。"""
    ns = prompts_for(commit)
    return str(ns["SYSTEM_PROMPT"]), str(ns["LOCALIZE_PROMPT"])


def run_commit(run: Run) -> str:
    return str((run.report.get("provenance") or {}).get("git_commit") or REAL_COMMIT)


def rebuild_messages(
    inst: LoopInstance, system: str, user: str, settings: Any
) -> list[tuple[int, int, int, int]]:
    """复现 plain_loop 的消息增长,返回每回合 (turn_no, 请求前上下文估算, 消息条数, 本轮响应估算)。

    工具回执按**当时进入 messages 的形态**重建:registry 记的是 fold 前的原文,
    所以要自己套 fold_output(与 plain_loop 同一组参数)。json.dumps 让折叠实际退化成
    8000 字符硬截(JSON 里没有真空行),这正是模型看到的样子。
    """
    messages: list[dict[str, object]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    rows: list[tuple[int, int, int, int]] = []
    for turn in inst.turns:
        context = messages_tokens(messages)
        rows.append((turn.turn_no, context, len(messages), _assistant_tokens(turn)))
        payload: dict[str, object] = {"role": "assistant", "content": turn.content}
        if turn.tools:
            payload["tool_calls"] = [
                {
                    "type": "function",
                    "id": f"call_{i}",
                    "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
                }
                for i, (name, args, _out) in enumerate(turn.tools)
            ]
        messages.append(payload)
        for i, (name, _args, out) in enumerate(turn.tools):
            if name == "finish":
                continue
            content = fold_output(
                json.dumps(out, ensure_ascii=False, default=str),
                head=settings.refine_head_lines,
                tail=settings.refine_tail_lines,
            )
            messages.append({"role": "tool", "tool_call_id": f"call_{i}", "content": content})
    return rows


def _assistant_tokens(turn: Turn) -> int:
    """本轮响应体的估算(它不进下一次请求的"上下文",但计费里算 completion)。"""
    return messages_tokens([{"role": "assistant", "content": turn.content}])


def ols(points: list[tuple[float, float, float]]) -> tuple[float, float, float, float]:
    """最小二乘拟合 y = a*x1 + c*x2 + b;points 为 (估算上下文+估算响应, 消息条数, 真实 tokens)。"""
    n = float(len(points))
    sx1 = sum(p[0] for p in points)
    sx2 = sum(p[1] for p in points)
    sy = sum(p[2] for p in points)
    sx1x1 = sum(p[0] * p[0] for p in points)
    sx2x2 = sum(p[1] * p[1] for p in points)
    sx1x2 = sum(p[0] * p[1] for p in points)
    sx1y = sum(p[0] * p[2] for p in points)
    sx2y = sum(p[1] * p[2] for p in points)
    # 3x3 正规方程 + 高斯消元(不引新依赖)
    mat = [
        [sx1x1, sx1x2, sx1, sx1y],
        [sx1x2, sx2x2, sx2, sx2y],
        [sx1, sx2, n, sy],
    ]
    for i in range(3):
        piv = next((r for r in range(i, 3) if abs(mat[r][i]) > 1e-12), None)
        if piv is None:
            raise SystemExit("fit matrix is singular - corpus too small?")
        mat[i], mat[piv] = mat[piv], mat[i]
        for r in range(3):
            if r == i:
                continue
            factor = mat[r][i] / mat[i][i]
            for c in range(i, 4):
                mat[r][c] -= factor * mat[i][c]
    a = mat[0][3] / mat[0][0]
    c = mat[1][3] / mat[1][1]
    b = mat[2][3] / mat[2][2]
    mean = sy / n
    ss_tot = sum((p[2] - mean) ** 2 for p in points)
    ss_res = sum((p[2] - (a * p[0] + c * p[1] + b)) ** 2 for p in points)
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0
    return a, c, b, r2


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", default="", help="把逐 run 结果写成 JSON 便于下游复用")
    parser.add_argument("--threshold", type=int, default=16000, help="顺带打印首次超阈的回合")
    args = parser.parse_args()

    settings = get_settings()
    runs = load_runs()
    print(f"corpus: {len(runs)} real-model runs ({sum(r.llm_events for r in runs)} llm turns)")

    # F0:语料跨六个 provenance commit。头部按**每个 run 自己的 commit** 取模板,
    # 这里核对三段提示词的字节指纹是否真的同源(尺寸相同不等于内容相同)。
    commits = sorted({run_commit(r) for r in runs})
    sigs = {c[:8]: prompt_signature(c) for c in commits}
    print(f"=== F0 头部同源核对:{len(commits)} 个 commit,模板指纹 {sorted(set(sigs.values()))} ===")
    for short, sig in sigs.items():
        print(f"  {short}  模板指纹={sig}")
    if len(set(sigs.values())) != 1:
        print("  ! 模板内容不一致 —— 逐 run 用自己的版本重建(尺寸相同也不算同源)")

    f1_bad: list[Any] = []
    f1_zero_accounting: list[Any] = []
    f2_bad, f2_total = 0, 0
    points: list[tuple[float, float, float]] = []
    per_run: list[dict[str, Any]] = []
    missing_total = 0
    est_total = 0

    for run in runs:
        reported = int(run.report.get("tokens_used") or 0)
        ok = run.tokens_used_recorded == reported
        if not ok:
            entry = (run.batch, run.bug_id, run.tokens_used_recorded, reported)
            if reported == 0 and run.tokens_used_recorded > 0:
                # 已知缺陷的实物:崩溃路径把 turns/tokens 记成 0(commit 4a4093e 修的就是它,
                # 见 docs/interview-evidence-2026-10-07.md "不能写"一节)。
                # 这是**账目缺陷**不是装载缺陷,单独归类,不算 F1 失败也不算"通过"。
                f1_zero_accounting.append(entry)
            else:
                f1_bad.append(entry)
        inst = run.localize
        if inst is None:
            continue
        try:
            bug = load_bug(run.bug_id)
            system_prompt, localize_prompt = head_for(run_commit(run))
            user_text = localize_prompt.format(
                issue_text=bug.issue_text,
                failed_tests="\n".join(f"- {t}" for t in bug.failed_tests),
            )
        except Exception as exc:  # 头部重建失败要如实报,不能悄悄用空串蒙过去
            print(f"  HEAD-FAIL {run.bug_id}: {type(exc).__name__}: {exc}")
            continue
        rows = rebuild_messages(inst, system_prompt, user_text, settings)
        est_localize = 0
        for turn, (_turn_no, context, n_msg, comp_est) in zip(inst.turns, rows, strict=True):
            if turn.real_tokens <= 0:
                continue
            # F2:动作序列对齐
            f2_total += 1
            if [n for n, _a, _o in turn.tools] != turn.tool_names:
                got = [n for n, _a, _o in turn.tools]
                # finish 会提前收尾:允许"记录的 tool_calls 末尾是 finish 且执行流里没有回执"
                if got != turn.tool_names[: len(got)]:
                    f2_bad += 1
            est_total += context + comp_est
            est_localize += context + comp_est
            missing_total += turn.missing_chars
            points.append((context + comp_est, n_msg, turn.real_tokens))
        first_over = next((t[0] for t in rows if t[1] > args.threshold), None)
        per_run.append(
            {
                "bug_id": run.bug_id,
                "batch": run.batch,
                "status": run.report.get("status"),
                "token_budget": (run.report.get("provenance") or {}).get("token_budget"),
                "localize_turns": len(inst.turns),
                "context_est_first": rows[0][1] if rows else 0,
                "context_est_last": rows[-1][1] if rows else 0,
                "context_est_max": max((r[1] for r in rows), default=0),
                "first_turn_over_threshold": first_over,
                "real_tokens_localize": inst.real_tokens_total,
                "est_tokens_localize": est_localize,
                "ratio_real_over_est": (
                    inst.real_tokens_total / est_localize if est_localize else 0.0
                ),
                "run_tokens_used": run.report.get("tokens_used"),
                "f1_ok": ok,
                "missing_chars": sum(t.missing_chars for t in inst.turns),
            }
        )

    a, c, b, r2 = ols(points)
    errs = [abs(p[2] - (a * p[0] + c * p[1] + b)) / p[2] for p in points if p[2] > 0]
    med_err = median(errs)
    ratios = sorted(r["ratio_real_over_est"] for r in per_run if r["ratio_real_over_est"] > 0)

    def q(vals: list[float], frac: float) -> float:
        if not vals:
            return 0.0
        return vals[min(len(vals) - 1, int(frac * (len(vals) - 1)))]

    print("\n=== F1 守恒(轨迹逐轮 tokens 之和 == report.tokens_used) ===")
    print(f"unexplained failures: {len(f1_bad)}" + ("" if not f1_bad else f" -> {f1_bad[:3]}"))
    for batch, bug, recorded, reported in f1_zero_accounting:
        print(
            f"  KNOWN DEFECT(崩溃路径把用量记成 0,4a4093e):{batch}/{bug} "
            f"轨迹={recorded:,} report={reported}"
        )
    print(
        f"=== F2 动作序列对齐: {f2_bad}/{f2_total} 错配 "
        f"({100.0 * f2_bad / max(1, f2_total):.2f}%) ==="
    )
    print("=== F3 重建可用性(y = a*估算 + c*消息条数 + b) ===")
    print(f"points={len(points)}  R2={r2:.4f}  median|err|={100 * med_err:.1f}%")
    print(
        f"  拟合系数 a={a:.3f} b={b:.0f} c={c:.1f} —— 注意 est 与消息条数高度共线,"
        "b/c 的劈分不可当结论用,尺度只用下面的逐 run 比值"
    )
    print(
        f"=== 估算器尺度(chars//4 vs 真实计费,逐 run 比值 real/est,n={len(ratios)}) ==="
        f"\n  median={median(ratios):.3f}  p10={q(ratios, 0.10):.3f}  p90={q(ratios, 0.90):.3f}"
        f"  min={ratios[0]:.3f}  max={ratios[-1]:.3f}"
    )
    med_ratio = median(ratios)
    print(
        f"  ⇒ 生产阈值 context_window_tokens={args.threshold}(估算口径)对应的真实上下文规模"
        f" ≈ {args.threshold * med_ratio:,.0f} tokens"
        f"(区间 {args.threshold * q(ratios, 0.1):,.0f} ~ {args.threshold * q(ratios, 0.9):,.0f})"
    )
    print(
        f"undercount from record form: {missing_total:,} chars "
        f"= {100.0 * missing_total / max(1, est_total * 4):.2f}% of estimated corpus volume"
    )
    gate = (not f1_bad) and (f2_bad / max(1, f2_total) <= 0.02) and (r2 >= 0.90 and med_err <= 0.15)
    print(f"\nGATE: {'PASS - 重建可用于点估计' if gate else 'FAIL - 只能报上下界,见上'}")

    print("\n=== 逐 run(LOCALIZE 段,估算口径) ===")
    header = (
        f"{'bug':34}{'turns':>6}{'head tok':>10}{'max tok':>10}"
        f"{'>16k at':>9}{'real/est':>10}{'status':>18}"
    )
    print(header)
    for r in sorted(per_run, key=lambda x: -x["context_est_max"]):
        over = r["first_turn_over_threshold"]
        over_text = "-" if over is None else f"{over}"
        status = f"{r['status'] or ''}"[:17]
        print(
            f"{r['bug_id'][:33]:34}{r['localize_turns']:>6}{r['context_est_first']:>10}"
            f"{r['context_est_max']:>10}{over_text:>9}"
            f"{r['ratio_real_over_est']:>10.2f}{status:>18}"
        )
    if args.json_out:
        target = Path(args.json_out)
        target.parent.mkdir(parents=True, exist_ok=True)
        # 仓库外产物用 write_bytes:AGENTS.md 禁的是仓库内文件被 write_text 换成 CRLF
        target.write_bytes(
            json.dumps(
                {
                    "commits": commits,
                    "gate_pass": gate,
                    "fit": {"a": a, "b": b, "c": c, "r2": r2, "median_err": med_err},
                    "f1_failed": f1_bad,
                    "f2_mismatch": [f2_bad, f2_total],
                    "runs": per_run,
                },
                ensure_ascii=False,
                indent=1,
            ).encode("utf-8"),
        )
        print(f"\nwrote {args.json_out}")
    return 0 if not f1_bad else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
