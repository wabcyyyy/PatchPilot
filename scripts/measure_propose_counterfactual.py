"""把 PROPOSE 段补进上下文反事实(M15.8 登记的那条"未覆盖",A3 卡)。

零成本、不起模型。复用 A1 的重建与 A2 的机器件,只是把头换成补丁阶段的头。

为什么这一段比 LOCALIZE 难,以及难点在哪(先说清,免得被当成"顺带就跑完了"):
补丁阶段的 user 消息是 `PROPOSE_PROMPT.format(round_no, issue_text, findings, feedback)`,
其中 **findings 是上一阶段的产物**。轨迹里 `finish` 事件的 summary 被 `[:200]` **无声截断**
(没有长度标记),所以走"定位成功"路径的实例,findings 的真实尺寸不可知 ⇒ 头部有一个
未知常量。只有走**降级**路径的实例留下硬锚:`localize_degraded` 事件记着 `findings_chars`,
而那两个 run 恰好就是补丁段额度判死的那两个(swe-hard-graph2 / swe-hard-graph3)。

**判据在看结果之前写死在这里**:
- A-1 头部锚:重建出的 findings 字符串**字节数** == 轨迹里 `localize_degraded.findings_chars`
  (graph2 在 commit 58472254 用 `last_content.strip()`;graph3 在 293cd649 用
  `_provisional_findings()` 拼"已调查线索"清单 —— 两者都只依赖轨迹,可确定性复现)。
- A-2 请求锚:同 A2 的 V1 —— 该循环已耗真值之和 + 代码当时算出的估算值 == error 原文里的 X,
  于是 `X − Σ真值` 与我的 `final_est` 直接对撞,零换算。判据:相对误差 ≤ 15%。
- A-1 或 A-2 不过 ⇒ 只报形态、**不报存活与节省数字**,并以退出码 1 结束。
- 范围:只覆盖 graph 臂的 `PROPOSE_PATCH`。one-shot 臂的 `PROPOSE_ONE_SHOT`(8 个实例)
  用的是 `app/evals/single_shot.py` 自己的提示构造,本卡不装懂,如实留在未覆盖清单里。

跑法:
    PYTHONPATH=. .venv/Scripts/python.exe scripts/measure_propose_counterfactual.py
    可选:--threshold=16000 --keep=6 --json-out=<路径>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
for _p in (str(REPO), str(REPO / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from context_replay import (  # noqa: E402
    build_instances,
    head_for,
    load_events,
    prompts_for,
)
from measure_context_counterfactual import (  # noqa: E402
    classify_death,
    replay,
)

from app.config import get_settings  # noqa: E402
from app.evals.bugset import load_bug  # noqa: E402

PROPOSE_STATE = "PROPOSE_PATCH"
DEGRADE_TOOL = "localize_degraded"
CLUE_KEYS = ("path", "keyword", "subdir", "glob")
TOOL_BUCKETS = ("read_file", "search_code", "list_files")
EMPTY_LAST = "(定位未在额度内收敛;以下为已调查线索,请先确认根因再按块协议提交补丁)"
CLUE_HEADER = "定位阶段已调查线索(未收敛,仅作起点):"
_builder_cache: dict[str, bool] = {}


def uses_builder(commit: str) -> bool:
    """该版本的降级路径有没有"已调查线索"清单构造器 —— 去源码里探,不举 commit 白名单。

    `58472254` 只用 `last_content.strip()`;`293cd649` 起才追加清单。两种写法算出的
    findings 字节数不同,而 A-1 锚吃的正是字节数,所以必须按 run 自己的版本走。
    """
    if commit not in _builder_cache:
        from context_replay import git_text

        try:
            src = git_text("app/graph/nodes.py", commit)
        except Exception:
            src = ""
        _builder_cache[commit] = "def _provisional_findings" in src
    return _builder_cache[commit]


def last_localize_content(events: list[dict[str, Any]]) -> str:
    """降级时 exc.last_content = 定位阶段最后一次有实质文本的模型输出。"""
    for ev in reversed(events):
        if ev.get("tool") == "llm" and ev.get("state") == "LOCALIZE":
            content = str((ev.get("output_summary") or {}).get("content") or "")
            if content.strip():
                return content
    return ""


def findings_for(commit: str, events: list[dict[str, Any]]) -> str:
    """按该 run 自己的 commit 复现 provisional findings 的构造(逐字符,才能对锚)。"""
    last = last_localize_content(events).strip()
    if not uses_builder(commit):
        return last or "(定位未在额度内收敛;请先用只读工具确认根因,再按块协议提交补丁)"
    counts: dict[str, dict[str, int]] = {t: {} for t in TOOL_BUCKETS}
    seen: dict[str, int] = {}
    for ev in events:
        tool = ev.get("tool")
        if ev.get("state") != "LOCALIZE" or tool not in counts:
            continue
        args = ev.get("input") or {}
        key = str(next((args[k] for k in CLUE_KEYS if args.get(k)), "") or "").strip()
        if not key:
            continue
        key = key[:80]
        counts[tool][key] = counts[tool].get(key, 0) + 1
        seen.setdefault(key, len(seen))
    lines = [last or EMPTY_LAST]
    rendered = False
    for tool in TOOL_BUCKETS:
        bucket = counts[tool]
        if not bucket:
            continue
        top = sorted(bucket.items(), key=lambda kv: (-kv[1], seen.get(kv[0], 0)))[:8]
        lines.append(f"- {tool}: " + ", ".join(f"{k} ×{c}" for k, c in top))
        rendered = True
    if rendered:
        lines.insert(1, CLUE_HEADER)
    return "\n".join(lines)


def recorded_findings_chars(events: list[dict[str, Any]]) -> int | None:
    for ev in events:
        if ev.get("tool") == DEGRADE_TOOL:
            value = (ev.get("output_summary") or {}).get("findings_chars")
            if value is not None:
                return int(value)
    return None


def propose_cases() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for report_path in sorted(REPO.glob("runs/swe-*/*/report.json")):
        report = json.loads(report_path.read_text(encoding="utf-8"))
        prov = report.get("provenance") or {}
        if prov.get("llm_enabled") is not True or report.get("model_provider") != "openai":
            continue
        run_dir = report_path.parent
        events = load_events(run_dir)
        commit = str(prov.get("git_commit") or "")
        system, _ = head_for(commit)
        # 分组复用 A1 的 build_instances(它已被 F1/F2/A-2 锚验过):
        # 自己另写一份"按 llm 事件切回合"的逻辑,正是这次 A-2 对不上的原因。
        propose_instances = [
            inst for inst in build_instances(events) if inst.state == PROPOSE_STATE and inst.turns
        ]
        if not propose_instances:
            continue
        try:
            bug = load_bug(str(report.get("bug_id")))
        except Exception as exc:
            print(f"  BUG-FAIL {report.get('bug_id')}: {type(exc).__name__}: {exc}")
            continue
        death = classify_death(report)
        for inst in propose_instances:
            findings = findings_for(commit, events)
            anchor_chars = recorded_findings_chars(events)
            feedback = "" if inst.round_no <= 1 else "(反馈文本未单独入库,无法重建)"
            ns = prompts_for(commit)
            user = ns["PROPOSE_PROMPT"].format(
                round_no=inst.round_no,
                issue_text=bug.issue_text,
                findings=findings,
                feedback=feedback,
            )
            out.append(
                {
                    "bug_id": str(report.get("bug_id")),
                    "batch": run_dir.parent.name,
                    "run_dir": run_dir,
                    "commit": commit[:8],
                    "round_no": inst.round_no,
                    "report": report,
                    "death": death,
                    "system": system,
                    "user": user,
                    "findings": findings,
                    "findings_exact": anchor_chars is not None,
                    "findings_anchor_chars": anchor_chars,
                    "turns_obj": inst.turns,
                    "turns": len(inst.turns),
                }
            )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--threshold", type=int, default=16000)
    ap.add_argument("--keep", type=int, default=6)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    cases = propose_cases()
    settings = get_settings()
    print(
        f"corpus: {len(cases)} PROPOSE_PATCH instances"
        f"(其中 findings 可精确重建:{sum(1 for c in cases if c['findings_exact'])} 个)"
    )
    print(
        "未覆盖:one-shot 臂 PROPOSE_ONE_SHOT 实例用的是 app/evals/single_shot.py 自己的提示构造,"
        "本卡不覆盖;走'定位成功'路径的补丁阶段 findings 被 summary[:200] 无声截断,头部有未知常量。"
    )

    print("\n=== A-1 findings 字节锚(只有降级路径的 run 留了 findings_chars) ===")
    a1_ok = 0
    a1_total = 0
    for c in cases:
        c["off"] = replay(c["turns_obj"], c["system"], c["user"], settings, 0, args.keep)
        if c["findings_anchor_chars"] is None:
            continue
        a1_total += 1
        got = len(c["findings"])
        want = c["findings_anchor_chars"]
        ok = got == want
        a1_ok += int(ok)
        print(
            f"  {'ok  ' if ok else 'MISS'}{c['batch']}/{c['bug_id'][:34]:36} 重建={got} 记录={want}"
        )

    print("\n=== A-2 请求锚:补丁阶段额度判死的 error 读数 vs 重建 final_est(零换算) ===")
    a2_ok = a2_total = 0
    for c in cases:
        d = c["death"]
        if not d or d.get("kind") != "token-gate" or d.get("stage") != "propose":
            continue
        if c["round_no"] != 1:
            continue  # 多轮时任务级账本混进上一阶段,Σ真值对不上单次循环
        real_sum = sum(int(t.real_tokens) for t in c["turns_obj"])
        actual = d["gate"] - real_sum
        pred = c["off"]["final_est"]
        err = abs(pred - actual) / actual if actual else 1.0
        ok = err <= 0.15
        a2_ok += int(ok)
        a2_total += 1
        print(
            f"  {'ok  ' if ok else 'MISS'}{c['batch']}/{c['bug_id'][:30]:30}"
            f" 代码={actual:>8,} 重建={pred:>8,} 误差={100 * err:5.1f}%"
        )
        c["anchor_actual"] = actual

    if a1_total == 0 or a1_ok < a1_total or a2_total == 0 or a2_ok < a2_total:
        print("\nGATE A-1/A-2 FAIL - 头部或请求对不上,只报形态,不报节省与存活。")
        return 1

    print("\n=== 反事实(阈值 16000 / keep=6,与生产默认同组参数) ===")
    print(
        f"{'bug':36}{'turns':>6}{'findings':>9}{'off末':>9}{'on末':>8}{'节省':>7}"
        f"{'压不到位':>10}{'首次触发':>10}{'下次请求撞墙':>14}"
    )
    rows_out = []
    for c in sorted(cases, key=lambda x: -x["off"]["final_est"]):
        on = replay(c["turns_obj"], c["system"], c["user"], settings, args.threshold, args.keep)
        off_sum = sum(r["before"] + r["resp_est"] for r in c["off"]["rows"])
        on_sum = sum(r["after"] + r["resp_est"] for r in on["rows"])
        attempts = sum(r["attempted"] for r in on["rows"])
        over = sum(r["still_over"] for r in on["rows"])
        first = next((r["turn"] for r in on["rows"] if r["compressed"]), None)
        real_sum = sum(int(t.real_tokens) for t in c["turns_obj"])
        est_sum = sum(r["before"] + r["resp_est"] for r in c["off"]["rows"])
        rho = real_sum / est_sum if est_sum else 1.0
        died = "-"
        nxt = {}
        if (
            c["death"]
            and c["death"].get("kind") == "token-gate"
            and c["death"].get("stage") == "propose"
        ):
            budget_est = int(c["death"]["budget"] / rho)
            # 真实判死发生在**记录之外的下一次请求**:闸门读数 = 累计已耗 + 待发那一次。
            # 只看记录内的回合会把这种死法整个漏掉(前一版就在这儿糊过去了)。
            nxt = {
                "budget_est": budget_est,
                "off_next_reading": off_sum + c["off"]["final_est"],
                "on_next_reading": on_sum + on["final_est"],
            }
            died = "{}→{}".format(
                "撞" if nxt["off_next_reading"] > budget_est else "过",
                "撞" if nxt["on_next_reading"] > budget_est else "活",
            )
        note = "锚" if c["findings_exact"] else "下界"
        print(
            f"{c['bug_id'][:35]:36}{c['turns']:>6}{len(c['findings']):>8}{note}"
            f"{c['off']['final_est']:>9,}{on['final_est']:>8,}"
            f"{100 * (1 - on_sum / off_sum) if off_sum else 0:>6.0f}%"
            f"{(f'{100 * over / attempts:.0f}%' if attempts else '-'):>10}"
            f"{(str(first) if first else '-'):>10}{died:>11}"
        )
        rows_out.append(
            {
                "bug_id": c["bug_id"],
                "batch": c["batch"],
                "commit": c["commit"],
                "round_no": c["round_no"],
                "findings_chars": len(c["findings"]),
                "findings_exact": c["findings_exact"],
                "turns": c["turns"],
                "off_final_est": c["off"]["final_est"],
                "on_final_est": on["final_est"],
                "savings": (1 - on_sum / off_sum) if off_sum else None,
                "attempts": attempts,
                "still_over": over,
                "first_trigger_turn": first,
                "next_request_check": nxt,
                "death": c["death"],
                "anchor_actual": c.get("anchor_actual"),
            }
        )

    exact_rows = [r for r in rows_out if r["findings_exact"]]
    print(
        f"\n可锚定的补丁阶段实例 n={len(exact_rows)};"
        f"节省中位 {100 * (sorted(r['savings'] for r in exact_rows)[len(exact_rows) // 2] if exact_rows else 0):.0f}%"
        "(其余实例的头部含未知的 findings 长度,那一批只能当下界读)。"
    )
    if args.json_out:
        target = Path(args.json_out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(json.dumps(rows_out, ensure_ascii=False, indent=1).encode("utf-8"))
        print(f"wrote {args.json_out}")
    print("\nGATE A-1/A-2 PASS - 头部字节锚与请求估算锚全部命中。")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
