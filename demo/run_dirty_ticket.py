"""演示：把「归一化后的工单」作为自定义任务喂给 PatchPilot。

真实系统里这一步可能是：
  工单系统 webhook → 归一化服务 → POST /api/tasks
这里为了离线可复现，用 build_custom_bug + FakeLLM 回放直接驱动 graph 引擎。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.config import get_settings
from app.evals.bugset import build_custom_bug
from app.graph.runner import run_task_graph
from app.llm.openai_client import build_model

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT / "demo" / "workspace-copy"
ISSUE = (ROOT / "demo" / "dirty-ticket" / "normalized-issue.md").read_text(encoding="utf-8")

FAILED = [
    "tests/test_dateparse.py::test_empty_string_returns_none",
    "tests/test_dateparse.py::test_whitespace_returns_none",
]
REGRESSION = [
    "tests/test_dateparse.py::test_iso_format",
    "tests/test_dateparse.py::test_slash_format",
    "tests/test_dateparse.py::test_invalid_format_raises",
    "tests/test_dateparse.py::test_none_returns_none",
]

# 离线演示：模拟模型「定位 → 打补丁 → 自测 → 声明完成」的工具序列。
# 真实模型（model=openai）时不需要脚本，模型自己决定调用什么。
REPLAY_SCRIPT = [
    {"tool": "search_code", "args": {"keyword": "parse_date"}},
    {"tool": "read_file", "args": {"path": "src/dateparse.py"}},
    {
        "tool": "finish",
        "args": {"success": True, "summary": "根因在 parse_date 未处理空/空白字符串"},
    },
    {
        "tool": "apply_patch",
        "args": {
            # 上下文必须与仓库文件逐字一致，否则 git apply --check 会拒
            # （上一次演示里英文 docstring 就是这么被拦下的）
            "diff_text": (
                "--- a/src/dateparse.py\n"
                "+++ b/src/dateparse.py\n"
                "@@ -9,6 +9,8 @@ def parse_date(value):\n"
                '     """解析日期字符串;空输入返回 None,非法格式抛 ValueError。"""\n'
                "     if value is None:\n"
                "         return None\n"
                "+    if not value.strip():\n"
                "+        return None\n"
                "     for fmt in DATE_FORMATS:\n"
                "         try:\n"
                "             return datetime.strptime(value, fmt).date()\n"
            )
        },
    },
    {"tool": "run_tests", "args": {"test_set": "failed"}},
    {"tool": "run_tests", "args": {"test_set": "regression"}},
    {
        "tool": "finish",
        "args": {"success": True, "summary": "空/空白输入已返回 None，回归保持通过"},
    },
]


def main() -> None:
    bug = build_custom_bug(
        repo_path=REPO,
        issue_text=ISSUE,
        failed_tests=FAILED,
        regression_tests=REGRESSION,
        allowed_paths=["src/**"],
        max_rounds=5,
    )
    model = build_model("fake", get_settings(), script=REPLAY_SCRIPT)
    result = run_task_graph(
        bug,
        model,
        runs_root=ROOT / "runs" / "dirty-demo",
        model_name="",
    )
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    print(f"\n[{bug.id}] status={result.status} verdict={result.verdict} -> {result.run_dir}")


if __name__ == "__main__":
    main()
