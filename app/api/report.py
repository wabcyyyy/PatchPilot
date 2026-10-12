"""报告渲染:TaskResult dict → Markdown(与 report.json 同源)。"""

from __future__ import annotations

from typing import Any

VERDICT_BADGE = {
    "resolved": "✅ resolved",
    "failed": "❌ failed",
    "needs_review": "👤 needs_review",
    "cancelled": "🚫 cancelled",
}


def _md_escape(text: Any) -> str:
    """转义 Markdown 结构字符(R2 整改):error/门禁文案来自不可信链路,
    含反引号/换行/竖线会打断渲染结构;报告是只读渲染,无需 HTML 转义。"""
    return str(text).replace("|", "\\|").replace("`", "'").replace("\n", " ")


def _tristate(value: Any, *, kind: str) -> str:
    """三态显式呈现(G16):每一项都翻成 已验证/未运行/不确定,并保留原值。

    报告的读者不该背 validation_status 的取值表:"not_run"必须读得出"没跑过",
    而不是被排版成一种结果。原值留在括号里,程序化核对仍以 report.json 为准。
    """
    raw = str(value or "") or ("unknown" if kind == "resource" else "not_run")
    if kind == "resource":
        if raw in {"within_budget", "exhausted", "exceeded"}:
            return f"已验证({raw})"
        return f"不确定({raw})"
    if raw in {"passed", "failed", "rejected"}:
        return f"已验证({raw})"
    if raw == "not_run":
        return "未运行(not_run)"
    return f"不确定({raw})"


def render_markdown(result: dict[str, Any]) -> str:
    """把 report.json 的内容渲染为可读 Markdown。"""
    bug_id = result.get("bug_id", "?")
    cost = result.get("cost_usd")
    cost_text = f"${cost:.4f}" if isinstance(cost, (int, float)) else "n/a"
    lines = [
        f"# 任务报告:{_md_escape(result.get('task_id', '?'))}",
        "",
        f"- 结论:**{VERDICT_BADGE.get(result.get('verdict'), result.get('verdict'))}**(状态 {result.get('status')})",
        f"- 题目:{bug_id} | 引擎:{result.get('engine')} | 模型:{result.get('model_provider')}",
        f"- 轮数:{result.get('rounds')} | 步数:{result.get('turns')} | token:{result.get('tokens_used')}"
        f" | 耗时:{result.get('duration_ms')}ms",
        f"- 成本(约值):{cost_text} | 输入 token:{result.get('tokens_prompt')}"
        f" | 输出 token:{result.get('tokens_completion')}",
        "",
        "## 状态与验收证据",
        "",
        # S02(spec §3.1):三态缺一即 not_run/unknown——旧报告缺字段也如实显示,
        # 没有验证过的任务不得因无 gate_violations 而展示"门禁已通过"。
        # G16:每项显式翻成 已验证/未运行/不确定,原值保留在括号里。
        f"- 报告结构版本:{result.get('report_schema_version', '(旧报告,无版本)')}",
        f"- 验证状态:{_tristate(result.get('validation_status'), kind='validation')}",
        f"- 门禁状态:{_tristate(result.get('gate_status'), kind='gate')}",
        f"- 资源状态:{_tristate(result.get('resource_status'), kind='resource')}",
        "",
        "## 判定过程",
        "",
        "| 检查项 | 结果 |",
        "|---|---|",
        f"| 基线失败测试数 | {result.get('baseline_failed')} |",
        f"| 基线回归集通过 | {'是' if result.get('baseline_regression_ok') else '否'} |",
        f"| 原失败测试转通过 | {'是' if result.get('verify_failed_ok') else '否'} |",
        f"| 回归集保持通过 | {'是' if result.get('verify_regression_ok') else '否'} |",
        f"| 质量门禁 | {'通过' if not result.get('gate_violations') else '拒绝'} |",
        "",
        "## 变更文件",
        "",
    ]
    lines.extend(f"- `{path}`" for path in result.get("changed_files", []) or ["(无)"])
    # G03:资源维度按"四类资源互不替代"分列。旧报告没有 resources 键,
    # 这种情况如实写"未记录",不拿 token 数冒充现金或调用数。
    resources = result.get("resources") or {}
    lines += ["", "## 资源维度", ""]
    if not resources:
        lines.append("- 本次报告未携带资源账本摘要(旧报告口径,只有 token 与约值成本)")
    else:
        limits = resources.get("limits") or {}
        price = resources.get("price_provenance") or {}
        lines += [
            f"- 预算模式:{resources.get('budget_mode')}",
            f"- token:已用 {resources.get('tokens_used')} / 上限 {limits.get('token_limit')}"
            f" | 含在途预留与未知持有的比较基数:{resources.get('committed_tokens')}",
            f"- 模型调用数:{resources.get('model_calls')} / 上限"
            f" {limits.get('call_limit') or '不限制'}",
            f"- 费用:{(resources.get('cost_micro_usd') or 0) / 1_000_000:.6f}"
            f" {price.get('currency') or 'USD'}(来源标签"
            f" {resources.get('cost_by_basis') or '未折价'})| 价目"
            f" {price.get('price_version') or 'n/a'}/{price.get('price_source') or 'n/a'}"
            f" | 上限 {limits.get('cost_limit_micro_usd') or '不限制'} micro-USD",
            f"- 未知消耗持有:{resources.get('unknown_held_tokens')} token"
            f" / {resources.get('unknown_held_cost_micro_usd')} micro-USD"
            "(崩溃或断连的在途调用继续占额度,恢复不重授)",
            f"- 请求前容量拒绝次数:{resources.get('over_capacity_requests')}"
            f"(声明容量 {limits.get('model_capacity_tokens') or '未声明'})",
            f"- 累计数与明细可重算:{'一致' if resources.get('calls_consistent') else '不一致'}",
        ]
        if resources.get("anomalies"):
            lines.append(f"- 账本异常:{_md_escape(resources['anomalies'])}")
        if resources.get("cost_overrun"):
            lines.append(f"- 现金超支:{_md_escape(resources['cost_overrun'])}")
    if result.get("gate_violations"):
        lines += ["", "## 门禁违规", ""]
        lines += [f"- {_md_escape(v)}" for v in result["gate_violations"]]
    if result.get("error"):
        lines += ["", "## 错误信息", "", f"> {_md_escape(result['error'])}"]
    # G13:逐条列出子任务贡献(委派与分支候选都记;旧报告无该键时如实不显示)
    subtasks = result.get("subtasks") or []
    if subtasks:
        lines += ["", "## 子任务", ""]
        for st in subtasks:
            note = f"`{st.get('subtask_id')}`({st.get('role')}):"
            note += "✅" if st.get("ok") else "❌"
            note += (
                f" {st.get('termination')} | turns={st.get('turns')} tokens={st.get('tokens_used')}"
            )
            if st.get("candidate_id"):
                note += f" | 候选 {st.get('candidate_id')}:{str(st.get('candidate_hash'))[:12]}"
            if st.get("error"):
                note += f" | {_md_escape(st['error'])}"
            lines.append(f"- {note}")
    # G16:report v2 补全段(evidence 键,report_evidence 收集)。旧报告缺该键时
    # 整节不出现,不冒充"没有验收/没有候选"。
    evidence = result.get("evidence") or {}
    if evidence:
        acceptance = evidence.get("acceptance") or {}
        if acceptance.get("proposed") or acceptance.get("frozen"):
            lines += ["", "## 验收清单", ""]
            lines.append(
                "- 状态:**已冻结(用户批准)**"
                if acceptance.get("frozen")
                else "- 状态:已提议,未冻结(生产补丁未放行)"
            )
            if acceptance.get("manifest_hash"):
                lines.append(
                    f"- manifest `{str(acceptance['manifest_hash'])[:16]}…`"
                    f"(revision {acceptance.get('revision')},批准者"
                    f" {acceptance.get('approved_by') or 'n/a'})"
                )
            lines.append(
                f"- 失败集 {len(acceptance.get('failed_tests') or [])} 项 /"
                f" 回归集 {len(acceptance.get('regression_tests') or [])} 项"
                "(明细见 run_dir 工件)"
            )
        candidates = evidence.get("candidates") or []
        if candidates:
            lines += ["", "## 候选", ""]
            for cand in candidates:
                gate_ok = cand.get("gate_ok")
                gate = (
                    "门禁通过" if gate_ok else ("门禁拒绝" if gate_ok is not None else "门禁未运行")
                )
                failed_ok, reg_ok = cand.get("verify_failed_ok"), cand.get("verify_regression_ok")
                if failed_ok is None and reg_ok is None:
                    verify = "验证未运行"
                else:
                    verify = (
                        f"验证({failed_ok if failed_ok is not None else '未运行'}/"
                        f"{reg_ok if reg_ok is not None else '未运行'})"
                    )
                parent = (
                    f",parent `{str(cand['parent_candidate_id'])[:12]}`"
                    if cand.get("parent_candidate_id")
                    else ""
                )
                lines.append(
                    f"- `{cand.get('candidate_id')}`(round {cand.get('round_no')}{parent}):"
                    f"{gate} | {verify} | sha `{str(cand.get('diff_sha256'))[:12]}…`"
                )
        commands = evidence.get("commands") or []
        if commands:
            lines += ["", f"## 受控命令(共 {len(commands)} 条)", ""]
            for cmd in commands[:10]:
                err = f" | {_md_escape(cmd['error'])}" if cmd.get("error") else ""
                lines.append(
                    f"- round {cmd.get('round_no')} `{cmd.get('profile_id') or '?'}`"
                    f" {cmd.get('state')}{err}"
                )
            if len(commands) > 10:
                lines.append(f"- (其余 {len(commands) - 10} 条见轨迹)")
        inputs = evidence.get("inputs") or {}
        if inputs.get("questions") or inputs.get("steers"):
            lines += ["", "## 输入事件", ""]
            for question in inputs.get("questions") or []:
                answered = (
                    "已回答(" + str(question.get("answer_source") or "user") + ")"
                    if (question.get("status") == "answered")
                    else str(question.get("status") or "unknown")
                )
                lines.append(
                    f"- 问题 `{question.get('question_id')}`:{answered} —"
                    f" {_md_escape(question.get('question'))}"
                )
            for steer in inputs.get("steers") or []:
                consumed = "已消费" if steer.get("consumed") else "未消费"
                lines.append(f"- 纠偏({consumed}):{_md_escape(steer.get('text'))}")
        identity = evidence.get("run_identity") or {}
        if identity:
            lines += ["", "## 运行身份", ""]
            lines.append(
                f"- profile:{identity.get('agent_profile') or 'n/a'}"
                f"(指纹 `{str(identity.get('runtime_policy_hash'))[:16]}…`)"
            )
            if identity.get("task_spec_hash"):
                lines.append(f"- task_spec `{str(identity['task_spec_hash'])[:16]}…`")
            if identity.get("platform_commit"):
                lines.append(f"- platform commit `{identity['platform_commit']}`")
            if identity.get("prompts_hash"):
                lines.append(f"- prompts `{str(identity['prompts_hash'])[:16]}…`")
            if identity.get("served_models"):
                lines.append(f"- served_model 观测:{_md_escape(identity['served_models'])}")
    lines += ["", f"> 轨迹与补丁:{result.get('run_dir', '.')}"]
    return "\n".join(lines) + "\n"
