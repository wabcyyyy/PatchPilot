"""真实语料上的工作记忆反事实测量(A2 卡,零成本、不起模型)。

用法:
    PYTHONPATH=. .venv/Scripts/python.exe scripts/measure_context_counterfactual.py
    可选:--grid=8000,16000,24000,32000 --keeps=4,6,8 --json-out=<路径>

数据 = runs/swe-* 的 25 份真实模型 run(llm_enabled + provider=openai,轨迹里
context_compact 事件 0 次 ⇒ **压缩未介入**的干净前对照语料,跑在升级前 commit 80981f0)。
逐轮消息列表的重建与尺度标定见 scripts/context_replay.py(A1)。

回答三个此前只能"信"的问题:
1. `context_window_tokens=16000`(生产默认)在真实 run 上第几轮才触发、能砍掉多少额度;
2. "压到 16000"这件事本身做不做得到(钉住的最近平回合可能就比阈值大);
3. 压缩后**金补丁要改的文件**还在不在模型眼前 —— ADR-0004 否决语义摘要的理由就是
   "摘要可能丢掉补丁要的锚点",这条一直没用真实数据验过。

**口径与边界(先读再引用数字)**
- **静态动作序列反事实**:假设模型行为不变,只把工作记忆换成压缩后的版本。所以能主张
  "少花多少额度 / 晚几轮撞墙 / 锚点在不在",**不能**主张"这样就能修好"。
- 压缩的触发与存根决策全在 `messages_tokens`(len//4)口径里发生,所以主体计算也在这一
  口径;要和"真实预算"比,按 A1 逐实例量出的尺度折算,并在 V2 如实报折算误差。
- **只有死在额度闸上的 run 才谈存活**。死因按 report.json 的 error 原文分类;死在
  max_turns 上的压缩帮不上忙(约束是回合数),明确排除、不缩分母。

**判据在看结果之前写死**:
- V1 锚点(有牙齿):额度型判死的 error 带 `agent loop tokens X exceed budget Y`,而
  X = 该循环已耗真值 + 代码当时算出的**估算口径上下文**,于是 `X − Σ真值` 就是那次请求
  的真实估算值 —— 零换算,直接对撞我的重建。判据:相对误差 ≤ 15%。
  **负对照**:同一重放里故意丢掉工具回执(只留头部),锚点必须明显偏离;若"错得离谱的
  重建"也能过锚,说明锚没牙齿,V1 改判不可信。
- V2:尺度折算用**逐实例**的 real/est 比值,不用一个全局系数 —— 实测这个比值在 1.26~1.90
  之间散,拿中位数折算单个 run 的绝对额度会差到 ±30%。所以绝对额度只在"比例"意义上看。
- 本卡**不选阈值**:只给曲线与触盘,裁决留给使用者。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
for _p in (str(REPO), str(REPO / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from context_replay import (  # noqa: E402
    LOCALIZE_STATE,
    Run,
    build_instances,
    head_for,
    load_events,
    median,
    run_commit,
)

from app.config import get_settings  # noqa: E402
from app.context.token_window import STUB_PREFIX, compact_messages  # noqa: E402
from app.evals.bugset import load_bug  # noqa: E402
from app.llm.base import messages_tokens  # noqa: E402
from app.tools.output_filter import fold_output  # noqa: E402

GOLD_DIFF = "expected/reference.diff"
HUNK_FILE = re.compile(r"^\+\+\+ b/(.+)$")
GATE_ERROR = re.compile(r"agent loop tokens (\d+) exceed budget (\d+)")
TURN_ERROR = re.compile(r"agent loop exceeded max_turns=(\d+)")


def load_runs() -> list[Run]:
    runs: list[Run] = []
    for report_path in sorted(REPO.glob("runs/swe-*/*/report.json")):
        report = json.loads(report_path.read_text(encoding="utf-8"))
        prov = report.get("provenance") or {}
        if prov.get("llm_enabled") is not True or report.get("model_provider") != "openai":
            continue
        run_dir = report_path.parent
        events = load_events(run_dir)
        llm = [e for e in events if e.get("tool") == "llm"]
        runs.append(
            Run(
                bug_id=str(report.get("bug_id") or run_dir.name),
                batch=run_dir.parent.name,
                run_dir=run_dir,
                report=report,
                instances=build_instances(events),
                llm_events=len(llm),
                tokens_used_recorded=sum(
                    int((e.get("output_summary") or {}).get("tokens") or 0) for e in llm
                ),
            )
        )
    return runs


def classify_death(report: dict[str, Any]) -> dict[str, Any] | None:
    """按 error 原文把判死分成"额度闸"与"回合闸",并取回闸口读数。"""
    if report.get("status") != "BUDGET_EXCEEDED":
        return None
    error = str(report.get("error") or "")
    stage = error.split(":")[0] if ":" in error else "one_shot"
    m = GATE_ERROR.search(error)
    if m:
        return {
            "kind": "token-gate",
            "stage": stage,
            "gate": int(m.group(1)),
            "budget": int(m.group(2)),
        }
    t = TURN_ERROR.search(error)
    if t:
        return {
            "kind": "max-turns",
            "stage": stage,
            "gate": 0,
            "budget": 0,
            "max_turns": int(t.group(1)),
        }
    return {"kind": "unknown", "stage": stage, "gate": 0, "budget": 0}


def append_turn(
    messages: list[dict[str, object]],
    turn: Any,
    settings: Any,
    drop_tools: bool = False,
    id_prefix: str = "call_",
) -> None:
    """把一回合的 assistant + 工具回执按**当时进 messages 的形态**接回去(原地改)。

    drop_tools 只服务负对照:模拟"重建里根本没有工具回执"这种错法。
    id_prefix 让 tool_call_id 全局唯一 —— 真实的 id 是端点给的长串,这里用"回合号+序号"
    顶替(长度差是重建的一处已知小额低估:每条工具调用少算约 20 字符)。
    """
    payload: dict[str, object] = {"role": "assistant", "content": turn.content}
    if turn.tools:
        payload["tool_calls"] = [
            {
                "type": "function",
                "id": f"{id_prefix}{i}",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }
            for i, (name, args, _out) in enumerate(turn.tools)
        ]
    messages.append(payload)
    if drop_tools:
        return
    for i, (name, _args, out) in enumerate(turn.tools):
        if name == "finish":
            continue
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"{id_prefix}{i}",
                "content": fold_output(
                    json.dumps(out, ensure_ascii=False, default=str),
                    head=settings.refine_head_lines,
                    tail=settings.refine_tail_lines,
                ),
            }
        )


def illegal_requests(messages: list[dict[str, object]]) -> int:
    """压缩后的请求合法性:每条 role=tool 必须能在**它之前**的 assistant 里找到配对的 id。

    这是"孤儿 tool 消息"检查 —— 严格端点(DeepSeek 等)见到孤儿直接 400,而这个项目已经为
    过一次"请求形态不合法"的付费教训(reasoning_content 未回传,commit 4a4093e)。
    """
    seen: set[str] = set()
    bad = 0
    for msg in messages:
        role = msg.get("role")
        if role == "assistant":
            for call in msg.get("tool_calls") or []:
                if isinstance(call, dict):
                    seen.add(str(call.get("id") or ""))
        elif role == "tool" and str(msg.get("tool_call_id") or "") not in seen:
            bad += 1
    return bad


def replay(
    turns: list[Any],
    system: str,
    user: str,
    settings: Any,
    threshold: int,
    keep: int,
    drop_tools: bool = False,
) -> dict[str, Any]:
    """逐回合重放工作记忆;返回逐轮账目 + 最后一次请求时模型实际看到的那份列表。"""
    messages: list[dict[str, object]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    rows: list[dict[str, int]] = []
    illegal = 0
    for index, turn in enumerate(turns):
        before = messages_tokens(messages)
        after, stubbed, dropped, still_over = before, 0, 0, 0
        if threshold > 0 and before > threshold:
            result = compact_messages(
                messages, max_context_tokens=threshold, keep_recent_turns=keep
            )
            messages = result.messages
            after, stubbed, dropped = result.after_tokens, result.stubbed, result.dropped
            still_over = int(after > threshold)
            illegal += illegal_requests(messages)
        rows.append(
            {
                "turn": turn.turn_no,
                "before": before,
                "after": after,
                "stubbed": stubbed,
                "dropped": dropped,
                "compressed": int(after != before),
                # attempted = 阈值判定成立、真的调用了一次压缩。它才是"压不到位"的分母:
                # 已经压到底的回合 after == before(compressed=0),用 compressed 当分母会把
                # 这些回合漏掉,比率能算到 100% 以上去。
                "attempted": int(threshold > 0 and before > threshold),
                "still_over": still_over,
                "illegal": illegal,
                "resp_est": messages_tokens([{"role": "assistant", "content": turn.content}]),
                "real": turn.real_tokens,
            }
        )
        append_turn(messages, turn, settings, drop_tools=drop_tools, id_prefix=f"c{index}_")
    return {
        "rows": rows,
        "final_est": messages_tokens(messages),  # 下一次(可能没发出去的)请求会看到多大
        "final_messages": [dict(m) for m in messages],
        "illegal": illegal,
    }


def gate_turn(rows: list[dict[str, int]], budget_est: int, field: str) -> int:
    """估算口径下复刻门禁:累计已耗 + 本次请求 > 预算 ⇒ 返回撞墙回合;0 = 没撞。"""
    spent = 0
    for r in rows:
        incoming = r[field] + r["resp_est"]
        if budget_est > 0 and spent + incoming > budget_est:
            return r["turn"]
        spent += incoming
    return 0


def gold_files(bug_id: str) -> list[str]:
    path = REPO / "bugs" / bug_id / GOLD_DIFF
    if not path.exists():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = HUNK_FILE.match(line)
        if m and m.group(1) != "/dev/null":
            out.append(m.group(1).strip())
    return out


def anchor_state(messages: list[dict[str, object]], targets: list[str]) -> dict[str, str]:
    """金补丁文件的可见性:full=内容还在,stub=只剩存根,lost=连路径都没了。"""
    state = dict.fromkeys(targets, "lost")
    for msg in messages:
        content = msg.get("content")
        if msg.get("role") not in ("tool", "assistant", "user") or not isinstance(content, str):
            continue
        for target in targets:
            if target not in content:
                continue
            is_stub = f"{STUB_PREFIX} tool=" in content
            if state[target] == "lost" or (state[target] == "stub" and not is_stub):
                state[target] = "stub" if is_stub else "full"
    return state


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--grid", default="8000,16000,24000,32000")
    ap.add_argument("--keeps", default="4,6,8")
    ap.add_argument("--prod-threshold", type=int, default=16000)
    ap.add_argument("--prod-keep", type=int, default=6)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    settings = get_settings()

    cases: list[dict[str, Any]] = []
    commits: set[str] = set()
    for run in load_runs():
        commits.add(run_commit(run))
        inst = next((i for i in run.instances if i.state == LOCALIZE_STATE), None)
        if inst is None or not inst.turns:
            continue
        try:
            bug = load_bug(run.bug_id)
            system_prompt, localize_prompt = head_for(run_commit(run))
            user_text = localize_prompt.format(
                issue_text=bug.issue_text,
                failed_tests="\n".join(f"- {t}" for t in bug.failed_tests),
            )
        except Exception as exc:
            print(f"  HEAD-FAIL {run.bug_id}: {type(exc).__name__}: {exc}")
            continue
        off = replay(inst.turns, system_prompt, user_text, settings, 0, args.prod_keep)
        real_sum = sum(r["real"] for r in off["rows"])
        est_sum = sum(r["before"] + r["resp_est"] for r in off["rows"])
        cases.append(
            {
                "bug_id": run.bug_id,
                "batch": run.batch,
                "status": run.report.get("status"),
                "turns": len(inst.turns),
                "off": off,
                "turns_obj": inst.turns,
                "user": user_text,
                "system": system_prompt,
                "commit": run_commit(run)[:8],
                "real_sum": real_sum,
                "rho": real_sum / est_sum if est_sum else 1.0,
                "gold": gold_files(run.bug_id),
                "death": classify_death(run.report),
            }
        )

    print(f"corpus: {len(cases)} LOCALIZE instances")
    print(
        f"corpus commits: {sorted(c[:8] for c in commits)}"
        " —— 头部按各 run 自己的 provenance 取(A1 的 F0 已证六个 commit 的模板字节同源)"
    )
    deaths = [c for c in cases if c["death"]]
    print("\n=== 判死分类(按 report.error 原文,不猜) ===")
    for c in deaths:
        d = c["death"]
        reading = (
            f"{d['gate']:,} > {d['budget']:,}"
            if d["kind"] == "token-gate"
            else (f"max_turns={d.get('max_turns')}" if d["kind"] == "max-turns" else "无法解析")
        )
        print(
            f"  {c['batch']:20}{c['bug_id'][:32]:34}{d['stage'] + '/' + d['kind']:>18}  {reading}"
        )
    kinds: dict[str, int] = {}
    for c in deaths:
        kinds[f"{c['death']['stage']}/{c['death']['kind']}"] = (
            kinds.get(f"{c['death']['stage']}/{c['death']['kind']}", 0) + 1
        )
    print(f"  合计 {len(deaths)}:{kinds}")

    # ---- V1 锚点 ----
    print("\n=== V1 锚点:代码当时算出的估算值 vs 我的重建(同口径、零换算) ===")
    token_localize = [
        c
        for c in deaths
        if c["death"]["kind"] == "token-gate" and c["death"]["stage"] == "localize"
    ]
    token_ids = {id(c) for c in token_localize}
    v1_ok = 0
    for c in token_localize:
        actual = c["death"]["gate"] - c["real_sum"]
        pred = c["off"]["final_est"]
        ctrl = replay(
            c["turns_obj"], c["system"], c["user"], settings, 0, args.prod_keep, drop_tools=True
        )["final_est"]
        err = abs(pred - actual) / actual if actual else 1.0
        ok = err <= 0.15
        v1_ok += int(ok)
        print(
            f"  {'ok  ' if ok else 'MISS'}{c['batch']}/{c['bug_id'][:30]:30} "
            f"代码={actual:>8,} 重建={pred:>8,} 误差={100 * err:5.1f}%  负对照={ctrl:>7,}"
        )
    print(f"  锚点 {v1_ok}/{len(token_localize)} 命中(门槛:全部 ≤15%,且负对照须明显偏离)")

    rhos = sorted(c["rho"] for c in cases)
    print(
        f"\n=== V2 逐实例尺度 real/est:median={median(rhos):.3f} "
        f"min={rhos[0]:.3f} max={rhos[-1]:.3f} ==="
    )

    # ---- 扫描 ----
    print("\n=== 扫描:每个 (阈值, 保留回合) 单元格的表现 ===")
    print(
        f"{'thr':>7}{'keep':>6}{'触发':>7}{'首次触发':>10}{'节省/触发':>11}{'节省/全体':>11}"
        f"{'压不到阈值':>12}{'额度死存活':>14}{'非法请求':>10}{'锚点 全/降级/没了':>22}"
    )
    grid = [int(x) for x in args.grid.split(",") if x.strip()]
    keeps = [int(x) for x in args.keeps.split(",") if x.strip()]
    grid_out: list[dict[str, Any]] = []
    for keep in keeps:
        for thr in [0, *grid]:
            triggered: list[int] = []
            savings: list[float] = []
            savings_all: list[float] = []
            still_over = attempts = survived = illegal = 0
            anchors = {"full": 0, "stub": 0, "lost": 0, "never_full": 0}
            for c in cases:
                on = (
                    replay(c["turns_obj"], c["system"], c["user"], settings, thr, keep)
                    if thr
                    else c["off"]
                )
                rows_on = on["rows"]
                off_sum = sum(r["before"] + r["resp_est"] for r in c["off"]["rows"])
                on_sum = sum(r["after"] + r["resp_est"] for r in rows_on)
                # 锚点分母与"有没有触发"无关:每个带金补丁的实例都要算,
                # 否则"阈值太高没人触发"会伪装成"锚点一个都没丢"。
                if thr and c["gold"]:
                    off_st = anchor_state(c["off"]["final_messages"], c["gold"])
                    on_st = anchor_state(on["final_messages"], c["gold"])
                    for target in c["gold"]:
                        anchors["never_full" if off_st[target] != "full" else on_st[target]] += 1
                illegal += int(on["illegal"])
                if thr:
                    # 分母 = 调用过压缩的回合(含"压到底了没变小"的那些),不是变小了的
                    attempts += sum(r["attempted"] for r in rows_on)
                    still_over += sum(r["still_over"] for r in rows_on)
                    savings_all.append(1 - on_sum / off_sum if off_sum else 0.0)
                if thr and any(r["compressed"] for r in rows_on):
                    triggered.append(next(r["turn"] for r in rows_on if r["compressed"]))
                    savings.append(1 - on_sum / off_sum if off_sum else 0.0)
                    if id(c) in token_ids:
                        budget_est = int(c["death"]["budget"] / c["rho"])
                        if gate_turn(rows_on, budget_est, "after") == 0:
                            survived += 1
            prod = "  <- 生产默认" if thr == args.prod_threshold and keep == args.prod_keep else ""
            anchor_total = (
                anchors["full"] + anchors["stub"] + anchors["lost"] + anchors["never_full"]
            )
            print(
                f"{thr:>7}{keep:>6}{(len(triggered) if thr else '-'):>7}"
                f"{(f'{median([float(t) for t in triggered]):.0f}' if triggered else '-'):>10}"
                f"{(f'{100 * median(savings):.0f}%' if savings else '-'):>10}"
                f"{(f'{100 * median(savings_all):.0f}%' if savings_all else '-'):>10}"
                f"{(f'{100 * still_over / max(1, attempts):.0f}%' if thr else '-'):>12}"
                f"{(f'{survived}/{len(token_localize)}' if thr else '-'):>14}"
                f"{illegal:>10}"
                f"  {anchors['full']}/{anchors['stub']}/{anchors['lost']}"
                f"(分母 {anchor_total},其中 {anchors['never_full']} 个本来就没读到){prod}"
            )
            grid_out.append(
                {
                    "threshold": thr,
                    "keep": keep,
                    "triggered": len(triggered) if thr else None,
                    "median_first_trigger_turn": median([float(t) for t in triggered])
                    if triggered
                    else None,
                    "median_savings_triggered": median(savings) if savings else None,
                    "median_savings_all": median(savings_all) if savings_all else None,
                    "still_over_share": still_over / max(1, attempts) if thr else None,
                    "token_deaths_survived": survived if thr else None,
                    "illegal_requests": illegal,
                    "anchors": anchors,
                }
            )

    print("\n=== 生产默认 (16000 / keep=6) 逐实例 ===")
    print(
        f"{'bug':34}{'turns':>6}{'判死':>18}{'off末 est':>11}{'on末 est':>10}{'节省':>7}{'锚点全/降级/没了':>18}"
    )
    for c in sorted(cases, key=lambda x: -x["off"]["final_est"]):
        on = replay(
            c["turns_obj"], c["system"], c["user"], settings, args.prod_threshold, args.prod_keep
        )
        off_sum = sum(r["before"] + r["resp_est"] for r in c["off"]["rows"])
        on_sum = sum(r["after"] + r["resp_est"] for r in on["rows"])
        off_st = anchor_state(c["off"]["final_messages"], c["gold"])
        on_st = anchor_state(on["final_messages"], c["gold"])
        counts = {"full": 0, "stub": 0, "lost": 0}
        for t in c["gold"]:
            if off_st[t] == "full":
                counts[on_st[t]] += 1
        kind = f"{c['death']['stage']}/{c['death']['kind']}" if c["death"] else "-"
        print(
            f"{c['bug_id'][:33]:34}{c['turns']:>6}{kind:>18}{c['off']['final_est']:>11,}"
            f"{on['final_est']:>10,}{100 * (1 - on_sum / off_sum):>6.0f}%  "
            f"{counts['full']}/{counts['stub']}/{counts['lost']}"
        )

    if args.json_out:
        target = Path(args.json_out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            json.dumps(
                {
                    "rho_median": median(rhos),
                    "v1": [c["bug_id"] for c in token_localize],
                    "grid": grid_out,
                },
                ensure_ascii=False,
                indent=1,
            ).encode("utf-8")
        )
        print(f"\nwrote {args.json_out}")

    if not token_localize or v1_ok < len(token_localize):
        print("\nGATE V1 FAIL - 重建在锚点上不过关,以上数字只作形态参考,不作主张。")
        return 1
    print("\nGATE V1 PASS - 锚点全部命中(负对照明显偏离,说明这个锚有牙齿)。")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
