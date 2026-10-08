"""轨迹摘要口径的钉子测试(M16.9 与 M19.1 同族缺陷的回归保护)。

摘要链上有三处会把内容变小:`_summarize_input` 的 300 字符上限、
`_summarize_output` 的 8 个键上限、以及 diff 字段的 `<N chars, see patches>` 替换。
它们必须**留下原文尺寸**,否则"模型当时看到过什么"在证据链里不可复原 ——
`finish` 的 summary 当年就是在调用点被 `[:200]` 无声截断,让补丁段的头部尺寸只能报下界
(M16 的 9 个实例里 7 个因此只能当下界读)。
"""

from __future__ import annotations

from app.tools.base import ToolResult
from app.tools.registry import FINISH_TOOL, _summarize_input, _summarize_output


def test_long_input_string_keeps_its_length() -> None:
    """入参摘要:>300 截断必须带 `... (N chars)`,长度可复原。"""
    value = "根因:空输入未处理。" + "x" * 500
    summary = _summarize_input(FINISH_TOOL, {"success": True, "summary": value})
    rendered = str(summary["summary"])
    assert rendered == value[:300] + f"... ({len(value)} chars)"


def test_short_input_string_is_recorded_verbatim() -> None:
    """短文不许被动过 —— 否则逐字重建类分析(如 A-1 锚)会静默失配。"""
    value = "读完了,根因在 parse_date。"
    summary = _summarize_input(FINISH_TOOL, {"success": True, "summary": value})
    assert summary["summary"] == value


def test_output_within_key_ceiling_loses_nothing() -> None:
    """8 键以内(今天 `run_tests` 正好压在这个数上)不产生任何额外字段。"""
    output = {f"k{i}": i for i in range(8)}
    summary = _summarize_output("some_tool", ToolResult(ok=True, output=output))
    assert isinstance(summary, dict)
    assert summary == output


def test_output_beyond_key_ceiling_marks_the_loss() -> None:
    """M19.1:超过 8 个键时,被丢掉的键数必须写进轨迹。

    静默丢键的话,报告上看不出"缺了什么"——这跟 `[:200]` 那条是同族缺陷。
    """
    output = {f"k{i}": i for i in range(11)}
    summary = _summarize_output("some_tool", ToolResult(ok=True, output=output))
    assert isinstance(summary, dict)
    kept = {k: v for k, v in summary.items() if k != "_omitted_keys"}
    assert kept == {f"k{i}": i for i in range(8)}
    assert summary["_omitted_keys"] == "+3 keys omitted"


def test_diff_field_is_replaced_with_its_size() -> None:
    """diff 全文进 `patches/`,轨迹里留尺寸与去处;同批其它键不丢。"""
    diff = "*** Begin Patch\n" + "y" * 4000
    summary = _summarize_output(
        "apply_patch", ToolResult(ok=True, output={"diff": diff, "files": 2})
    )
    assert isinstance(summary, dict)
    assert summary["diff"] == f"<{len(diff)} chars, see patches>"
    assert summary["files"] == 2


def test_failed_result_keeps_the_error_text() -> None:
    summary = _summarize_output("run_tests", ToolResult(ok=False, error="[format] diff is empty"))
    assert summary == {"error": "[format] diff is empty"}
