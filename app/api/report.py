"""报告渲染:TaskResult dict → Markdown(与 report.json 同源)。"""

from __future__ import annotations

from typing import Any

VERDICT_BADGE = {
    "resolved": "✅ resolved",
    "failed": "❌ failed",
    "needs_review": "👤 needs_review",
}


def render_markdown(result: dict[str, Any]) -> str:
    """把 report.json 的内容渲染为可读 Markdown。"""
    bug_id = result.get("bug_id", "?")
    lines = [
        f"# 任务报告:{result.get('task_id', '?')}",
        "",
        f"- 结论:**{VERDICT_BADGE.get(result.get('verdict'), result.get('verdict'))}**(状态 {result.get('status')})",
        f"- 题目:{bug_id} | 引擎:{result.get('engine')} | 模型:{result.get('model_provider')}",
        f"- 轮数:{result.get('rounds')} | 步数:{result.get('turns')} | token:{result.get('tokens_used')}"
        f" | 耗时:{result.get('duration_ms')}ms",
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
    for path in result.get("changed_files", []) or ["(无)"]:
        lines.append(f"- `{path}`")
    if result.get("gate_violations"):
        lines += ["", "## 门禁违规", ""]
        lines += [f"- {v}" for v in result["gate_violations"]]
    if result.get("error"):
        lines += ["", "## 错误信息", "", f"> {result['error']}"]
    lines += ["", f"> 轨迹与补丁:{result.get('run_dir', '.')}"]
    return "\n".join(lines) + "\n"
