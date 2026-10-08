"""M4 失败反思用例:回滚保真的 diff 压成"上一轮改过什么"的形状,附进下一轮反馈。

被保护的既有语义:`repeat_streak`/`_feedback_streak` 的计数口径、verify 生成的失败用例文本、
末轮取证用的 `preserved_diff` 一律不动。本用例只证明**多出来的那段**形状摘要形状正确、
上限受控,以及"没有可回滚内容时反馈逐字不变"。
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from app.evals.bugset import load_bug
from app.gitops.testing import materialize_repo
from app.graph.nodes import TaskNodes
from app.graph.reflection import diff_digest, with_discarded_patch
from app.llm.fake import FakeLLM
from app.tools.tracker import Tracker

BUG_ROOT = Path("bugs")

TWO_FILE_DIFF = (
    "diff --git a/src/dateparse.py b/src/dateparse.py\n"
    "--- a/src/dateparse.py\n"
    "+++ b/src/dateparse.py\n"
    "@@ -14,6 +14,9 @@ def parse_date(value):\n"
    "-    return None\n"
    "+    if not value:\n"
    "+        return None\n"
    "diff --git a/src/util.py b/src/util.py\n"
    "--- a/src/util.py\n"
    "+++ b/src/util.py\n"
    "@@ -1,3 +1,4 @@\n"
    "+import re\n"
)


def test_digest_lists_files_with_counts_and_hunk_context() -> None:
    digest = diff_digest(TWO_FILE_DIFF)

    assert digest.startswith("上一轮补丁已回滚")
    assert "- src/dateparse.py  +2 -1  @@ -14,6 +14,9 @@ def parse_date(value)" in digest
    assert "- src/util.py  +1 -0  @@ -1,3 +1,4 @@" in digest
    # 顺序 = diff 里的出现顺序(保留"模型先改哪个"的意图),不做字母序重排
    assert digest.index("src/dateparse.py") < digest.index("src/util.py")
    assert "已经试过且不成立" in digest


def test_digest_counts_exclude_the_plus_plus_minus_minus_header_lines() -> None:
    """`+++`/`---` 是文件头不是改动行:把它们算进增删会让模型误判改动规模。"""
    assert "+2 -1" in diff_digest(TWO_FILE_DIFF)


def test_digest_caps_files_and_says_how_many_are_missing() -> None:
    diff = "".join(
        f"diff --git a/f{i}.py b/f{i}.py\n--- a/f{i}.py\n+++ b/f{i}.py\n@@ -0,0 +1 @@\n+line{i}\n"
        for i in range(10)
    )

    digest = diff_digest(diff, max_files=3)

    assert digest.count("- f") == 3
    assert "… 另有 7 个文件未列出" in digest


def test_digest_never_emits_a_partial_line() -> None:
    diff = "".join(
        f"diff --git a/pkg/module_with_a_long_name_{i}.py b/pkg/module_with_a_long_name_{i}.py\n"
        f"--- a/pkg/module_with_a_long_name_{i}.py\n"
        f"+++ b/pkg/module_with_a_long_name_{i}.py\n"
        "@@ -1,2 +1,3 @@ def some_function_name_here()\n"
        "+changed\n"
        for i in range(6)
    )
    complete = set(diff_digest(diff).splitlines())

    for max_chars in (120, 200, 320, 600):
        cut = diff_digest(diff, max_chars=max_chars)
        assert len(cut) <= max_chars or cut == ""
        for line in cut.splitlines():
            assert line in complete, f"半截行:{line!r}"


def test_digest_is_empty_for_nothing_to_reflect_on() -> None:
    assert diff_digest("") == ""
    assert diff_digest("   \n  \n") == ""
    assert diff_digest("not a diff at all\njust prose\n") == ""
    assert diff_digest(TWO_FILE_DIFF, max_chars=0) == ""


def test_with_discarded_patch_returns_feedback_untouched_when_nothing_was_rolled_back() -> None:
    """回归钉子:没有回滚内容时,反馈文本必须与今天逐字一致。"""
    original = "上一轮补丁应用后仍有失败:\n- test_a|AssertionError"

    assert with_discarded_patch(original, "") == original
    assert with_discarded_patch(original, "prose only\n") == original


def test_with_discarded_patch_appends_after_a_blank_line() -> None:
    original = "上一轮补丁应用后仍有失败:\n- test_a|AssertionError"

    merged = with_discarded_patch(original, TWO_FILE_DIFF)

    assert merged.startswith(original)
    assert "\n\n上一轮补丁已回滚" in merged
    assert "src/util.py" in merged


def test_with_discarded_patch_with_empty_feedback_is_the_digest_alone() -> None:
    assert with_discarded_patch("", TWO_FILE_DIFF).startswith("上一轮补丁已回滚")


def test_rollback_attaches_the_discarded_patch_shape_to_next_round_feedback(
    tmp_path: Path,
) -> None:
    """节点级行为验证:真回滚一个真工作区,下一轮反馈里要带上"上一轮改了什么"。

    `branching_used=True` 是为了把自适应分支挡在断言之外(本用例只验反思文本),
    分支自身的形状由 tests/test_branching.py 负责。
    """
    bug = load_bug("BUG-001", BUG_ROOT)
    workspace = tmp_path / "ws"
    baseline = materialize_repo(bug.repo_dir, workspace)
    target = workspace / "src" / "dateparse.py"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n# a change that did not fix anything\n",
        encoding="utf-8",
    )
    nodes = TaskNodes(
        bug=bug,
        model=FakeLLM([]),
        workspace=workspace,
        tracker=Tracker(None, task_id="T-REFLECT"),
        report_dir=tmp_path / "reports",
        max_rounds=3,
        max_turns=5,
        deadline_epoch=time.time() + 900,
    )
    nodes.baseline_commit = baseline
    state = {
        "round_no": 1,
        "max_rounds": 3,
        "feedback": "上一轮补丁应用后仍有失败:\n- test_empty|AssertionError",
        "repeat_streak": 1,
        "patch_fail_streak": 0,
        "branching_used": True,
    }

    update = nodes.rollback(state)  # type: ignore[arg-type]

    assert update["status"] == "PROPOSE_PATCH"
    assert update["round_no"] == 2
    assert update["feedback"].startswith("上一轮补丁应用后仍有失败")
    assert "上一轮补丁已回滚" in update["feedback"]
    # 形状行按 "+N -M" 断言而不是写死数字:本机 core.autocrlf=true,追加一行 LF 会让
    # 整个文件按 CRLF→LF 重写(实测 +19 -17)。写死数字会把环境行为误当成代码回归
    shape = next(
        line for line in update["feedback"].splitlines() if line.startswith("- src/dateparse.py")
    )
    assert re.match(r"^- src/dateparse\.py  \+\d+ -\d+", shape), shape
    # 反思只是文本追加,工作区仍必须被复位到基线(不因追加而漏回滚)
    assert "# a change that did not fix anything" not in (
        workspace / "src" / "dateparse.py"
    ).read_text(encoding="utf-8")


def test_rollback_on_last_round_does_not_become_a_retry_path(tmp_path: Path) -> None:
    """轮数耗尽仍是 BUDGET_EXCEEDED 终点,并照旧保真 diff——反思不得改变终止判定。"""
    bug = load_bug("BUG-001", BUG_ROOT)
    workspace = tmp_path / "ws"
    baseline = materialize_repo(bug.repo_dir, workspace)
    (workspace / "src" / "dateparse.py").write_text(
        (workspace / "src" / "dateparse.py").read_text(encoding="utf-8") + "\n# x\n",
        encoding="utf-8",
    )
    nodes = TaskNodes(
        bug=bug,
        model=FakeLLM([]),
        workspace=workspace,
        tracker=Tracker(None, task_id="T-REFLECT-LAST"),
        report_dir=tmp_path / "reports",
        max_rounds=1,
        max_turns=5,
        deadline_epoch=time.time() + 900,
    )
    nodes.baseline_commit = baseline

    update = nodes.rollback({"round_no": 1, "max_rounds": 1, "feedback": ""})  # type: ignore[typeddict-item]

    assert update["status"] == "BUDGET_EXCEEDED"
    assert update["outcome"] == "failed"
    assert "feedback" not in update  # 终止路径不追加反思
    assert "# x" in update["preserved_diff"]  # N-12 取证仍然保全
