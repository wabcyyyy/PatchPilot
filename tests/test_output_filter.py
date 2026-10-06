"""卡3 提纯层测试:帧过滤、折叠、junit traceback 字段与反馈渲染。

边界:提纯只作用于模型可见层——`failure_signature`/`all_passed` 的判定与
junit/stderr 原始证据必须逐字不变,所以这里同时钉住"签名没被改动"。
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from app.adapters.pytest_adapter import parse_junit_xml
from app.prompts import build_feedback
from app.tools.output_filter import fold_output, refine_traceback


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    return tmp_path


PYTEST_TB = textwrap.dedent(
    """\
    ___ test_chunk ___

        def test_chunk():
            from src.slicing import chunk
    >       assert chunk([1, 2, 3], 1) == [[1], [2], [3]]
    E       assert [[1, 2], [3], []] == [[1], [2], [3]]

    src/slicing.py:6: AssertionError

      File "C:/py/Lib/site-packages/_pytest/python.py", line 153, in pytest_pyfunc_call
        result = testfunction(**testargs)
      File "C:/py/Lib/enum.py", line 663, in __missing__
        raise KeyError(name)
    KeyError: 'B2'
    """
)


def test_refine_keeps_project_frames_and_final_exception(root: Path) -> None:
    refined = refine_traceback(PYTEST_TB, root)
    assert "src/slicing.py:6: AssertionError" in refined
    assert "assert [[1, 2], [3], []]" in refined
    assert "KeyError: 'B2'" in refined
    assert "site-packages" not in refined
    assert "enum.py" not in refined


def test_refine_drops_frames_outside_project_root(root: Path) -> None:
    tb = 'Traceback (most recent call last):\n  File "D:/other/x.py", line 1, in f\n    pass\nValueError: x\n'
    refined = refine_traceback(tb, root)
    assert "D:/other/x.py" not in refined
    assert refined.startswith("Traceback")
    assert "ValueError: x" in refined


def test_refine_keeps_in_root_cpython_frames(root: Path) -> None:
    inside = (root / "src" / "slicing.py").as_posix()
    tb = (
        "Traceback (most recent call last):\n"
        f'  File "{inside}", line 6, in chunk\n'
        "    return [items[i:i + n]]\n"
        "IndexError: list index out of range\n"
    )
    refined = refine_traceback(tb, root)
    assert "slicing.py" in refined and "IndexError" in refined


def test_refine_truncates_with_omitted_marker(root: Path) -> None:
    tb = "\n".join(f"    frame line {i}" for i in range(60))
    refined = refine_traceback(tb, root, max_lines=10)
    lines = refined.split("\n")
    assert len(lines) == 10
    assert lines[:9] == [f"    frame line {i}" for i in range(9)]
    assert lines[9] == "... (51 行已省略) ..."


def test_refine_handles_empty_input(root: Path) -> None:
    assert refine_traceback("", root) == ""
    assert refine_traceback("   \n  ", root) == ""


def test_fold_short_text_is_untouched() -> None:
    text = "a\nb\nc"
    assert fold_output(text, head=40, tail=15) == text


def test_fold_keeps_head_and_tail_with_marker() -> None:
    text = "\n".join(str(i) for i in range(100))
    folded = fold_output(text, head=3, tail=2)
    lines = folded.split("\n")
    assert lines[:3] == ["0", "1", "2"]
    assert lines[-2:] == ["98", "99"]
    assert lines[3] == "... (95 行已省略) ..."


def test_fold_hard_cap_is_the_outer_bound() -> None:
    text = "\n".join(["x" * 200] * 60)
    folded = fold_output(text, head=40, tail=15, hard_cap=1000)
    assert len(folded) <= 1000 + len("... (truncated)")
    assert folded.endswith("... (truncated)")


def test_fold_zero_tail_emits_head_only() -> None:
    folded = fold_output("\n".join(str(i) for i in range(10)), head=2, tail=0)
    assert folded.startswith("0\n1\n... (8 行已省略) ...")


JUNIT_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="1" failures="1">
  <testcase classname="tests.test_demo" name="test_chunk" file="tests/test_demo.py" time="0.1">
    <failure message="AssertionError: assert 0 == 1">___ test_chunk ___

    def test_chunk():
&gt;       assert chunk([1, 2], 1) == [[1], [2]]
E       assert [[1, 2]] == [[1], [2]]

src/slicing.py:6: AssertionError
</failure>
  </testcase>
</testsuite>
"""


def test_parse_junit_xml_carries_traceback_without_touching_signature(
    tmp_path: Path,
) -> None:
    path = tmp_path / "junit.xml"
    path.write_text(JUNIT_XML, encoding="utf-8")
    report = parse_junit_xml(path)
    case = report.failed_cases[0]
    # 判定口径不变:签名仍是 kind + 消息首行(截 160)
    assert case.signature == "failure: AssertionError: assert 0 == 1"
    # 新增:完整堆栈进入模型可见层的原料
    assert "src/slicing.py:6: AssertionError" in case.traceback
    assert "assert [[1, 2]] == [[1], [2]]" in case.traceback


def test_parse_junit_xml_default_traceback_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "junit.xml"
    path.write_text(
        '<testsuite tests="0"></testsuite>',
        encoding="utf-8",
    )
    report = parse_junit_xml(path)
    assert report.failed_cases == []


def test_build_feedback_renders_refined_traceback() -> None:
    cases = [
        {
            "name": "test_chunk",
            "signature": "failure: assert 0 == 1",
            "traceback": "src/slicing.py:6: AssertionError\nE   assert 0 == 1",
        }
    ]
    text = build_feedback(cases)
    assert "- test_chunk: failure: assert 0 == 1" in text
    assert "  src/slicing.py:6: AssertionError" in text  # 缩进 2 空格
    assert "  E   assert 0 == 1" in text


def test_build_feedback_omits_blank_traceback_line() -> None:
    text = build_feedback([{"name": "t", "signature": "failure: x"}])
    assert text.splitlines() == ["上一轮补丁应用后仍有失败:", "- t: failure: x"]


def test_build_feedback_keeps_repeat_streak_hint() -> None:
    text = build_feedback(
        [{"name": "t", "signature": "failure: x", "traceback": ""}], repeat_streak=2
    )
    assert "换一种定位方向" in text


def test_build_feedback_extra_note_positional_still_works() -> None:
    """第二个位置参数是 extra_note(旧调用方口径),不能被 traceback 渲染改动打乱。"""
    text = build_feedback([{"name": "t", "signature": "failure: x"}], "补充说明")
    assert text.splitlines()[-1] == "补充说明"
