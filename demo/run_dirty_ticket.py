"""演示:把「归一化后的工单」作为自定义任务喂给 PatchPilot(graph 引擎,离线 FakeLLM)。

真实系统里这一步可能是:
  工单系统 webhook → 归一化服务 → POST /api/tasks
这里为了离线可复现,用 build_custom_bug + FakeLLM 回放直接驱动 graph 引擎。

用法(spec S08):
    python -m demo.run_dirty_ticket --out runs/demo-dirty
失败(非 resolved)返回非零退出码;输出含任务 ID、基线、变更文件、门禁结果、
双测试集与资源状态、报告/补丁路径。演示目录 demo/workspace-copy 全程零写入。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

from app.config import get_settings
from app.evals.bugset import build_custom_bug
from app.gitops.blockpatch import unified_to_block
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

# 正确修复的 unified diff —— 经 blockpatch 转成 apply_patch 的块协议文本。
# 工具层单入口只认 patch_text+块协议(spec F7:旧字段 diff_text 已不存在);
# 上下文必须与仓库文件逐字一致,否则 git apply --check 会拒。
_FIX_DIFF = (
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

# 离线演示:模拟模型「定位 → 打补丁 → 自测 → 声明完成」的工具序列。
REPLAY_SCRIPT = [
    {"tool": "search_code", "args": {"keyword": "parse_date"}},
    {"tool": "read_file", "args": {"path": "src/dateparse.py"}},
    {
        "tool": "finish",
        "args": {"success": True, "summary": "根因在 parse_date 未处理空/空白字符串"},
    },
    {
        "tool": "apply_patch",
        "args": {"patch_text": unified_to_block(_FIX_DIFF)},
    },
    {"tool": "run_tests", "args": {"test_set": "failed"}},
    {"tool": "run_tests", "args": {"test_set": "regression"}},
    {
        "tool": "finish",
        "args": {"success": True, "summary": "空/空白输入已返回 None,回归保持通过"},
    },
]


def _dir_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PatchPilot 离线工单演示(graph 引擎)")
    parser.add_argument(
        "--out",
        default=str(ROOT / "runs" / f"demo-dirty-{datetime.now().strftime('%Y%m%d-%H%M%S')}"),
        help="本次演示的输出目录(runs 根)",
    )
    args = parser.parse_args(argv)
    out_root = Path(args.out).resolve()

    before = _dir_hash(REPO)
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
        runs_root=out_root,
        model_name="",
    )
    after = _dir_hash(REPO)
    if after != before:
        print("DEMO FAILED: demo workspace-copy was mutated by the run", file=sys.stderr)
        return 2

    summary = {
        "task_id": result.task_id,
        "status": result.status,
        "verdict": result.verdict,
        "baseline_failed": result.baseline_failed,
        "changed_files": result.changed_files,
        "gate_violations": result.gate_violations,
        "verify_failed_ok": result.verify_failed_ok,
        "verify_regression_ok": result.verify_regression_ok,
        "validation_status": result.validation_status,
        "gate_status": result.gate_status,
        "resource_status": result.resource_status,
        "report": str(Path(result.run_dir) / "report.json"),
        "diff": str(Path(result.run_dir) / "diff.patch"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    ok = result.status == "FINISHED" and result.verdict == "resolved"
    print(f"\n[{bug.id}] {'RESOLVED' if ok else 'FAILED'} -> {result.run_dir}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
