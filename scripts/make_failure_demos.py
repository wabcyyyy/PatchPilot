"""生成持久的失败/拦截/转人工演示运行(简历准入的轨迹证据)。

用法:python scripts/make_failure_demos.py [输出根目录,默认 runs/m9/failures]
产出三种非 resolved 终态,各带完整轨迹与报告:
  1. gate-intercepted   门禁拦截(试图修改测试文件)
  2. budget-exceeded    预算耗尽(含回滚事件)
  3. needs-review       转人工(定位阶段声明失败)
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.evals.bugset import load_bug  # noqa: E402  (需先注入 sys.path)
from app.graph.runner import run_task_graph  # noqa: E402
from app.llm.fake import FakeLLM  # noqa: E402

BUGS = ROOT / "bugs"


def _localize_ok() -> list[dict]:
    return [
        {"tool": "search_code", "args": {"keyword": "parse_date"}},
        {"tool": "finish", "args": {"success": True, "summary": "根因:parse_date 未处理空白输入"}},
    ]


def _comment_diff() -> str:
    """不修复问题的合法补丁(只加注释),制造 verify 失败以触发回滚。"""
    import difflib

    original = (BUGS / "BUG-001" / "repo" / "src" / "dateparse.py").read_text(encoding="utf-8")
    changed = original.replace(
        '    raise ValueError(f"unrecognized date format: {value!r}")\n',
        '    raise ValueError(f"unrecognized date format: {value!r}")  # noqa: 非修复改动\n',
    )
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            changed.splitlines(keepends=True),
            fromfile="a/src/dateparse.py",
            tofile="b/src/dateparse.py",
        )
    )


def main() -> int:
    out_root = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "runs" / "eval-evidence"
    bug = load_bug(BUGS / "BUG-001")

    cases = [
        (
            "gate-intercepted",
            bug,
            FakeLLM(
                _localize_ok()
                + [
                    {
                        "tool": "apply_patch",
                        "args": {
                            "diff_text": (
                                "diff --git a/tests/test_dateparse.py b/tests/test_dateparse.py\n"
                                "--- a/tests/test_dateparse.py\n"
                                "+++ b/tests/test_dateparse.py\n"
                                "@@ -1,3 +1,4 @@\n"
                                " import pytest\n"
                                "+\n"
                                " from src.dateparse import parse_date\n"
                            )
                        },
                    },
                    {"tool": "finish", "args": {"success": True, "summary": "试图改测试作弊"}},
                ]
            ),
            {},
        ),
        (
            "budget-exceeded",
            bug,
            FakeLLM(
                _localize_ok()
                + [
                    {"tool": "apply_patch", "args": {"diff_text": _comment_diff()}},
                    {"tool": "run_tests", "args": {"test_set": "failed"}},
                    {"tool": "finish", "args": {"success": True, "summary": "以为修好了"}},
                ]
            ),
            {"max_rounds": 1},
        ),
        (
            "needs-review",
            bug,
            FakeLLM([{"tool": "finish", "args": {"success": False, "summary": "定位失败,转人工"}}]),
            {},
        ),
    ]

    tmp = Path(tempfile.mkdtemp(prefix="demo-"))
    results = []
    try:
        for name, b, model, kwargs in cases:
            # 直接落盘到目标目录(唯一后缀),避免 Windows 上跨盘搬移 git 只读对象
            import uuid

            result = run_task_graph(
                b,
                model,
                runs_root=out_root,
                task_id=f"demo-{name}-{uuid.uuid4().hex[:6]}",
                **kwargs,
            )
            results.append((name, result.status, result.verdict))
            print(f"[demo] {name}: {result.status}/{result.verdict} -> {result.run_dir}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    expected_status = {
        "gate-intercepted": (
            "PATCH_REJECTED",
            "VERIFY_FAILED",
        ),  # 终态可能是重试耗尽后的 VERIFY_FAILED
        "budget-exceeded": ("BUDGET_EXCEEDED",),
        "needs-review": ("NEEDS_REVIEW",),
    }
    ok = all(status in expected_status[name] for name, status, _ in results)
    # 拦截案例的核心证据是轨迹中的 apply_gate 拒绝事件,单独校验:
    intercepted = [r for r in Path(out_root).glob("demo-gate-intercepted-*/trajectory.jsonl")]
    ok &= any(
        '"tool": "apply_gate"' in p.read_text(encoding="utf-8")
        or '"apply_gate"' in p.read_text(encoding="utf-8")
        for p in intercepted
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
