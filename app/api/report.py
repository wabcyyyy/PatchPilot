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
    if result.get("gate_violations"):
        lines += ["", "## 门禁违规", ""]
        lines += [f"- {_md_escape(v)}" for v in result["gate_violations"]]
    if result.get("error"):
        lines += ["", "## 错误信息", "", f"> {_md_escape(result['error'])}"]
    lines += ["", f"> 轨迹与补丁:{result.get('run_dir', '.')}"]
    return "\n".join(lines) + "\n"
