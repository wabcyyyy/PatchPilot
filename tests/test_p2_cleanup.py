"""复盘 P2 小清理的行为钉子:路径门禁冒号、SKIP_DIRS 相对段、截断早失败。

三项各自独立成 commit,但测试集中一处:都是"复盘发现的口径修正",
不再分散进各模块的存量测试文件。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.errors import TaskError
from app.graph.gates import run_gates
from app.llm.openai_client import OpenAICompatModel
from app.tools.base import ToolContext
from app.tools.files import list_files
from app.tools.tracker import Tracker


def _diff_for(path: str) -> str:
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-x\n+y\n"


def test_path_gate_allows_posix_colon_filename() -> None:
    """复盘 P2:weird:name.py 是 POSIX 合法文件名,冒号只按 Windows 盘符拒绝。"""
    gate = run_gates(_diff_for("weird:name.py"))
    assert gate.ok, gate.violations


def test_path_gate_still_rejects_windows_drive_letter() -> None:
    gate = run_gates(_diff_for("C:/evil.py"))
    assert not gate.ok
    assert any(v.gate == "paths" for v in gate.violations)


def test_skip_dirs_match_repo_relative_segments_only(tmp_path: Path) -> None:
    """复盘 P2:SKIP_DIRS 只按仓库内相对段匹配;workspace 本身落在
    node_modules/ 之类的目录下时不得全量误伤。"""
    ws = tmp_path / "node_modules" / "ws"  # workspace 祖先路径含 SKIP_DIRS 成员
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (ws / "cache" / "node_modules").mkdir(parents=True)
    (ws / "cache" / "node_modules" / "b.py").write_text("y = 2\n", encoding="utf-8")
    ctx = ToolContext(
        task_id="T-P2",
        workspace=ws,
        baseline_commit="",
        tracker=Tracker(None, task_id="T-P2"),
        report_dir=tmp_path,
        test_sets={},
        allowed_paths=None,
    )
    result = list_files(ctx)
    assert result.ok, result.error
    files = result.output["files"]
    assert "src/a.py" in files  # 祖先段不参与匹配
    assert all(not f.startswith("cache/") for f in files)  # 仓库内相对段照常跳过


def _model() -> OpenAICompatModel:
    from app.config import Settings

    return OpenAICompatModel(
        Settings(llm_enabled=True, llm_model="test-model", llm_api_key="k", llm_base_url="http://x")
    )


def test_truncated_tool_calls_fail_early(monkeypatch: pytest.MonkeyPatch) -> None:
    """复盘 P2:截断时若仍有工具调用,早失败(TaskError)而不是让坏参数流进 apply。"""
    model = _model()

    def fake_create(**kwargs: Any) -> SimpleNamespace:
        tool_call = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(
                name="apply_patch", arguments='{"patch_text": "diff --git a/x.py'
            ),
        )
        message = SimpleNamespace(content=None, tool_calls=[tool_call])
        choice = SimpleNamespace(message=message, finish_reason="length")
        return SimpleNamespace(choices=[choice], usage=SimpleNamespace(total_tokens=99))

    monkeypatch.setattr(model._client.chat.completions, "create", fake_create)
    with pytest.raises(TaskError, match="truncated"):
        model.complete([{"role": "user", "content": "hi"}], [])


def test_truncated_text_only_still_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    """纯文本截断无工具调用:不致命,照常返回回合。"""
    model = _model()

    def fake_create(**kwargs: Any) -> SimpleNamespace:
        message = SimpleNamespace(content="被截断的纯文本…", tool_calls=None)
        choice = SimpleNamespace(message=message, finish_reason="length")
        return SimpleNamespace(choices=[choice], usage=SimpleNamespace(total_tokens=42))

    monkeypatch.setattr(model._client.chat.completions, "create", fake_create)
    turn = model.complete([{"role": "user", "content": "hi"}], [])
    assert turn.content and not turn.is_tool_call
