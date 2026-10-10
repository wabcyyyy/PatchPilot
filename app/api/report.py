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
        # 没有验证过的任务不得因无 gate_violations 而展示"门禁已通过"
        f"- 报告结构版本:{result.get('report_schema_version', '(旧报告,无版本)')}",
        f"- 验证状态 validation_status:{result.get('validation_status') or 'not_run'}",
        f"- 门禁状态 gate_status:{result.get('gate_status') or 'not_run'}",
        f"- 资源状态 resource_status:{result.get('resource_status') or 'unknown'}",
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
    lines += ["", f"> 轨迹与补丁:{result.get('run_dir', '.')}"]
    return "\n".join(lines) + "\n"
