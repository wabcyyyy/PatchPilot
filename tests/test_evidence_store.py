"""G05:任务内证据仓(落盘、分页取回、上限、作用域)与模型可见有界视图。

分两层是刻意的:
- 仓本身要能单独证明"原文没丢、能分页拿回、越界读不到";
- registry 的有界视图要证明**发给模型那一份**仍是合法 JSON(旧缺陷正是切在 JSON 中间),
  这条只能在真实工具回执 + 真实 `json.loads` 上验,不能在对字符串的断言上验。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.config import get_settings
from app.context.evidence import (
    INDEX_FILENAME,
    EvidenceLookupError,
    EvidenceNotStored,
    EvidenceStore,
    build_store,
    decode_cursor,
    encode_cursor,
)
from app.graph.nodes import READ_TOOLS
from app.llm.base import AssistantTurn, ToolCall
from app.tools.base import ToolContext
from app.tools.output_filter import bound_structure
from app.tools.registry import execute
from app.tools.tracker import Tracker

TASK = "T-EVID"


def _store(root: Path, **overrides: Any) -> EvidenceStore:
    args: dict[str, Any] = {"max_item_bytes": 200_000, "max_total_bytes": 400_000}
    args.update(overrides)
    return EvidenceStore(root / "evidence", TASK, **args)


def _ctx(workspace: Path, tmp_path: Path, **overrides: Any) -> ToolContext:
    return ToolContext(
        task_id=TASK,
        workspace=workspace,
        baseline_commit="",
        tracker=Tracker(tmp_path / "trajectory.jsonl", task_id=TASK),
        report_dir=tmp_path / "report",
        test_sets={},
        allowed_paths=None,
        **overrides,
    )


def _override(monkeypatch: pytest.MonkeyPatch, **values: Any) -> None:
    """工具层每次现读 Settings 缓存实例,monkeypatch 负责还原(与 test_search_tools 同法)。"""
    settings = get_settings()
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------- 仓:落盘 / 取回 / 分页 ----------


def test_save_writes_artifact_and_index_row(tmp_path: Path) -> None:
    store = _store(tmp_path)
    raw = '{"content": "line one\\nline two"}'
    ref = store.save(raw, source="read_file", workspace_revision=3)
    assert ref.retrievable and ref.task_id == TASK
    assert ref.raw_lines == 1  # JSON 里的换行是转义,不是真空行 —— 这正是旧折叠失灵的根因
    artifact = store.root / f"{ref.evidence_id}.json"
    assert artifact.is_file() and artifact.read_text(encoding="utf-8") == raw
    row = json.loads(store.index_path.read_text(encoding="utf-8").splitlines()[-1])
    assert row["evidence_id"] == ref.evidence_id and row["source"] == "read_file"
    assert row["workspace_revision"] == 3 and row["file"] == artifact.name
    # 索引里不落绝对路径,宿主目录不出现在模型可见元数据里
    assert str(tmp_path) not in json.dumps(ref.as_dict())
    assert "file" not in ref.as_dict() and "root" not in ref.as_dict()


def test_paging_reassembles_the_exact_bytes(tmp_path: Path) -> None:
    """逐页读回拼起来必须**逐字节等于**原文:分页不是有损通道。"""
    store = _store(tmp_path, page_bytes=256)
    raw = "\n".join(f"line {index} {'x' * 40}" for index in range(60))
    ref = store.save(raw, source="run_tests")
    chunks: list[str] = []
    cursor = ""
    for _ in range(200):
        page = store.read(ref.evidence_id, cursor=cursor, limit_bytes=256)
        chunks.append(page.text)
        assert page.returned_bytes <= 256
        if page.next_cursor is None:
            assert page.eof
            break
        cursor = page.next_cursor
    else:
        pytest.fail("pagination never reached eof")
    assert "".join(chunks) == raw


def test_cursor_is_bound_to_its_artifact(tmp_path: Path) -> None:
    store = _store(tmp_path)
    a = store.save("a" * 5000, source="read_file")
    b = store.save("b" * 5000, source="git_diff")
    cursor = encode_cursor(100, a.sha256)
    assert decode_cursor(cursor, a.sha256) == 100
    with pytest.raises(ValueError, match="different evidence"):
        decode_cursor(cursor, b.sha256)
    page = store.read(a.evidence_id, cursor=cursor)
    assert page.offset == 100
    with pytest.raises(EvidenceLookupError, match="malformed cursor"):
        store.read(a.evidence_id, cursor="100")
    with pytest.raises(EvidenceLookupError, match="not an integer"):
        store.read(a.evidence_id, cursor="abc:{a.sha256[:12]}")
    with pytest.raises(EvidenceLookupError, match="past the end"):
        store.read(a.evidence_id, cursor=f"99999:{a.sha256[:12]}")
    with pytest.raises(EvidenceLookupError, match="empty"):
        store.read("")


def test_unknown_or_foreign_id_is_not_found(tmp_path: Path) -> None:
    """未知 id、别的任务的 id、以及拿路径当 id 一律读成 not found,不给"存在但拒读"的旁证。"""
    store = _store(tmp_path)
    ref = store.save("payload", source="read_file")
    other = EvidenceStore(store.root, "T-OTHER")
    with pytest.raises(EvidenceLookupError, match="not registered"):
        other.read(ref.evidence_id)
    for forged in ("../../secrets.env", str(store.root / f"{ref.evidence_id}.json"), "nope" * 8):
        with pytest.raises(EvidenceLookupError, match="not registered"):
            store.read(forged)


def test_tampered_artifact_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ref = store.save("original content", source="read_file")
    (store.root / f"{ref.evidence_id}.json").write_text("tampered!!!!!!", encoding="utf-8")
    with pytest.raises(EvidenceLookupError, match="hash mismatch"):
        store.read(ref.evidence_id)


def test_index_survives_a_new_process(tmp_path: Path) -> None:
    """崩溃恢复/新进程只靠 run_dir 里的索引就能取回上一进程落下的证据。"""
    store = _store(tmp_path)
    ref = store.save("from the previous process", source="run_tests")
    reopened = _store(tmp_path)
    assert reopened.known(ref.evidence_id)
    assert reopened.read(ref.evidence_id).text == "from the previous process"
    assert reopened._used_bytes == len("from the previous process")


def test_oversize_item_is_refused_and_says_so(tmp_path: Path) -> None:
    store = _store(tmp_path, max_item_bytes=1000)
    with pytest.raises(EvidenceNotStored) as raised:
        store.save("y" * 5000, source="git_diff")
    ref = raised.value.ref
    assert ref.retrievable is False and ref.evidence_id == ""
    assert "per-item cap" in ref.reason
    assert ref.raw_bytes == 5000 and ref.sha256  # 字节数与 hash 仍然可对账
    assert list(store.root.glob("*.json")) == [] or store.root.exists()
    assert not store.index_path.exists() or store.index_path.read_text(encoding="utf-8") == ""


def test_total_quota_refuses_the_second_item(tmp_path: Path) -> None:
    store = _store(tmp_path, max_item_bytes=1000, max_total_bytes=1200)
    first = store.save("z" * 900, source="read_file")
    with pytest.raises(EvidenceNotStored) as raised:
        store.save("z" * 900, source="read_file")
    assert "store is full" in raised.value.ref.reason
    assert store.read(first.evidence_id).text == "z" * 900  # 满了不能顺手冲掉已有证据


def test_build_store_follows_the_run_dir_convention(tmp_path: Path) -> None:
    """证据落在 `<run_dir>/evidence`,run_dir 口径与 ledger_state.json 同源(report_dir.parent)。"""
    store = build_store(tmp_path / "report", TASK)
    assert store.root == tmp_path / "evidence"
    assert str(INDEX_FILENAME) in str(store.index_path)


# ---------- registry:有界视图发给模型的仍是合法 JSON ----------


def test_small_receipt_shape_is_untouched(tmp_path: Path) -> None:
    """装得下的回执**不加任何新键**:存量轨迹与两臂对照解析的形状不许被顺带动到。"""
    _write(tmp_path / "ws" / "src" / "small.py", "def f():\n    return 1\n")
    ctx = _ctx(tmp_path / "ws", tmp_path)
    result = execute(ctx, "read_file", {"path": "src/small.py"})
    assert result.ok and set(result.output) == {
        "path",
        "total_lines",
        "offset",
        "returned_lines",
        "content",
        "truncated",
    }
    assert result.visible is None and result.evidence is None
    assert not (tmp_path / "evidence").exists()


def test_big_receipt_stays_valid_json_and_keeps_the_raw(tmp_path: Path) -> None:
    """T09 主判据:超限回执仍是合法 JSON,而被裁掉的原文逐字落在仓里。"""
    body = "\n".join(f"value_{index} = {'u' * 60}" for index in range(400))
    _write(tmp_path / "ws" / "src" / "big.py", body)
    ctx = _ctx(tmp_path / "ws", tmp_path)
    result = execute(ctx, "read_file", {"path": "src/big.py", "limit": 400})
    visible = result.visible
    assert visible is not None, "超限回执必须走有界视图"
    content = json.dumps(visible, ensure_ascii=False, default=str)
    assert len(content) <= get_settings().tool_visible_max_chars
    parsed = json.loads(content)  # 旧实现在这一步就会抛(切在字符串中间)
    assert parsed["_bounded"]["omitted_lines"] > 0
    assert parsed["evidence"]["retrievable"] is True
    ref = result.evidence
    assert ref is not None and parsed["evidence"]["evidence_id"] == ref.evidence_id
    # 原文一行没丢:被裁掉的第 300 行仍在仓里
    raw = (tmp_path / "evidence" / f"{ref.evidence_id}.json").read_text(encoding="utf-8")
    assert "value_300" in raw and len(raw) > len(content)
    # 模型可见的那一屏里,被裁的行确实不在(否则"省略"就是假的)
    assert "value_300" not in parsed["content"]
    assert "已省略" in parsed["content"]


def test_kept_line_ranges_are_reported(tmp_path: Path) -> None:
    """省略标记旁边必须给出**保留的是哪几行**(原始行号),不是让模型自己数。"""
    body = "\n".join(f"row_{index} = {'a' * 60}" for index in range(400))
    _write(tmp_path / "ws" / "src" / "big.py", body)
    ctx = _ctx(tmp_path / "ws", tmp_path)
    result = execute(ctx, "read_file", {"path": "src/big.py", "limit": 400})
    ranges = result.visible["_bounded"]["line_ranges"]  # type: ignore[index]
    assert ranges == {"content": "1-40+386-400"}, ranges
    content = str(result.visible["content"])  # type: ignore[index]
    assert "row_0 =" in content and "row_399" in content and "row_200" not in content


def test_omitted_middle_is_reachable_through_the_tool(tmp_path: Path) -> None:
    """取回不是摆设:从 evidence_id 出发分页,能读到那一屏里被裁掉的具体行。"""
    body = "\n".join(f"marker_{index} = {'v' * 60}" for index in range(400))
    _write(tmp_path / "ws" / "src" / "big.py", body)
    ctx = _ctx(tmp_path / "ws", tmp_path)
    bounded = execute(ctx, "read_file", {"path": "src/big.py", "limit": 400})
    evidence_id = str(bounded.visible["evidence"]["evidence_id"])  # type: ignore[index]
    collected = ""
    cursor = ""
    for _ in range(200):
        page = execute(
            ctx,
            "read_evidence",
            {"evidence_id": evidence_id, "cursor": cursor, "limit_bytes": 4000},
        )
        assert page.ok, page.error
        collected += page.output["text"]  # type: ignore[index]
        cursor = page.output.get("next_cursor", "")  # type: ignore[index]
        if page.output["eof"]:  # type: ignore[index]
            break
    assert "marker_250" in collected
    assert json.loads(collected)["content"].count("marker_") > 300


def test_foreign_evidence_id_cannot_be_read(tmp_path: Path) -> None:
    """别的任务的 id 递过来:读成 not found,并且不落任何对方内容(跨任务引用被拒)。"""
    _write(tmp_path / "ws" / "src" / "big.py", "\n".join(f"a = {'q' * 80}" for _ in range(300)))
    ctx = _ctx(tmp_path / "ws", tmp_path)
    stranger = EvidenceStore(tmp_path / "elsewhere" / "evidence", "T-OTHER")
    ref = stranger.save("top secret payload" * 500, source="read_file")
    result = execute(ctx, "read_evidence", {"evidence_id": ref.evidence_id})
    assert not result.ok
    assert "not registered in this task" in (result.error or "")
    assert "secret" not in (result.error or "")


def test_receipt_from_before_a_write_is_marked_stale(tmp_path: Path) -> None:
    """文件改过之后旧引用仍可取回,但必须标 stale:旧内容不能当现状用。"""
    _write(
        tmp_path / "ws" / "src" / "big.py",
        "\n".join(f"old_line_{i} = {'w' * 70}" for i in range(300)),
    )
    ctx = _ctx(tmp_path / "ws", tmp_path)
    bounded = execute(ctx, "read_file", {"path": "src/big.py", "limit": 400})
    evidence_id = str(bounded.visible["evidence"]["evidence_id"])  # type: ignore[index]
    assert bounded.visible["evidence"]["workspace_revision"] == 0  # type: ignore[index]
    assert ctx.workspace_revision == 0  # 只读工具不加代数
    ctx.workspace_revision += 1  # 与 apply_patch 成功时 registry 做的事一致
    page = execute(ctx, "read_evidence", {"evidence_id": evidence_id})
    assert page.ok and page.output["stale"] is True  # type: ignore[index]
    assert page.output["current_revision"] == 1  # type: ignore[index]
    assert "不能当作当前代码" in page.output["note"]  # type: ignore[index]
    assert "old_line_1" in page.output["text"]  # type: ignore[index]


def test_unicode_and_escaped_newlines_survive_the_bound(tmp_path: Path) -> None:
    """中文与字面量 \\n 混排:裁剪按行做,不能把转义序列劈开。"""
    body = "\n".join(f"消息_{index} = '第一行\\n第二行'  # {'注释' * 30}" for index in range(300))
    _write(tmp_path / "ws" / "src" / "uni.py", body)
    ctx = _ctx(tmp_path / "ws", tmp_path)
    result = execute(ctx, "read_file", {"path": "src/uni.py", "limit": 400})
    content = json.dumps(result.visible, ensure_ascii=False, default=str)
    parsed = json.loads(content)
    assert "\\n" in parsed["content"]  # 转义没被切断成半个 \
    assert "消息_" in parsed["content"]
    raw = (tmp_path / "evidence" / f"{result.evidence.evidence_id}.json").read_text(  # type: ignore[union-attr]
        encoding="utf-8"
    )
    assert json.loads(raw)["total_lines"] == 300


def test_nested_large_fields_are_folded_too(tmp_path: Path) -> None:
    """嵌套大字段(失败用例里的 traceback)也在射程内 —— 不是一套只裁顶层的截断器。"""
    payload = {
        "test_set": "all",
        "all_passed": False,
        "failed_cases": [
            {
                "name": f"t{i}",
                "test_id": f"p::t{i}",
                "signature": f"sig {i}",
                "traceback": "\n".join(f"frame {i}-{j}" for j in range(400)),
            }
            for i in range(12)
        ],
    }
    report = bound_structure(payload, max_chars=8000, head_lines=40, tail_lines=15)
    assert report.changed
    text = json.dumps(report.value, ensure_ascii=False)
    assert len(text) <= 8000 and json.loads(text)
    assert any("failed_cases[" in field for field in report.folded_fields)


def test_error_receipt_is_also_bounded_and_valid(tmp_path: Path) -> None:
    """失败回执同样过有界协议:超长 error 不能变成切坏的 JSON。"""
    ctx = _ctx(tmp_path / "ws", tmp_path)
    from app.tools import registry

    def _huge_error(_ctx: ToolContext) -> Any:
        return registry.ToolResult.fail("boom " + ("detail line\n" * 4000))

    registry.REGISTRY["probe_huge_error"] = registry.ToolSpec(
        name="probe_huge_error",
        description="test only",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=_huge_error,
    )
    try:
        result = execute(ctx, "probe_huge_error", {})
        assert not result.ok
        parsed = json.loads(json.dumps(result.visible, ensure_ascii=False, default=str))
        assert "error" in parsed and parsed["_bounded"]["omitted_lines"] > 0
        assert len(json.dumps(parsed, ensure_ascii=False)) <= get_settings().tool_visible_max_chars
    finally:
        del registry.REGISTRY["probe_huge_error"]


def test_bounding_events_land_in_the_trajectory(tmp_path: Path) -> None:
    """取证闭合:每一条被裁的回执都留一条事件,带原文/excerpt 的 hash 与可取回标志。"""
    _write(tmp_path / "ws" / "src" / "big.py", "\n".join(f"c = {'e' * 90}" for _ in range(300)))
    ctx = _ctx(tmp_path / "ws", tmp_path)
    result = execute(ctx, "read_file", {"path": "src/big.py", "limit": 400})
    events = [
        json.loads(line)
        for line in (tmp_path / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
    ]  # type: ignore[arg-type]
    bounded = [event for event in events if event["tool"] == "evidence_bounded"]
    assert len(bounded) == 1
    summary = bounded[0]["output_summary"]
    assert summary["evidence_id"] == result.evidence.evidence_id  # type: ignore[union-attr]
    assert summary["raw_sha256"] == result.evidence.sha256  # type: ignore[union-attr]
    assert summary["raw_chars"] > summary["shown_chars"] > 0
    assert summary["retrievable"] is True
    # 主工具事件仍按未裁的 output 记:取证要能还原"原文形状"
    tool_event = next(event for event in events if event["tool"] == "read_file")
    assert "_bounded" not in json.dumps(tool_event["output_summary"])


def test_visible_cap_zero_disables_the_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write(tmp_path / "ws" / "src" / "big.py", "\n".join(f"d = {'f' * 90}" for _ in range(300)))
    _override(monkeypatch, tool_visible_max_chars=0)
    ctx = _ctx(tmp_path / "ws", tmp_path)
    result = execute(ctx, "read_file", {"path": "src/big.py", "limit": 400})
    assert result.visible is None and result.evidence is None
    assert "_bounded" not in json.dumps(result.output)


# ---------- 循环里的那一条 tool 消息 ----------


class _RecordingModel:
    """把每次**实际发出去的 messages** 记下来:契约要在真实请求上验,不能只验内部字段。"""

    def __init__(self, steps: list[dict[str, Any]]) -> None:
        self.steps = list(steps)
        self.seen: list[list[dict[str, Any]]] = []

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
        self.seen.append([dict(message) for message in messages])
        if self.steps:
            step = self.steps.pop(0)
            return AssistantTurn(
                content="先看一眼",
                tool_calls=[
                    ToolCall(id=f"c{len(self.seen)}", name=step["tool"], arguments=step["args"])
                ],
                finish_reason="tool_calls",
            )
        return AssistantTurn(
            content="结束",
            tool_calls=[
                ToolCall(id="cf", name="finish", arguments={"success": False, "summary": "done"})
            ],
            finish_reason="tool_calls",
        )


def test_loop_tool_message_is_valid_json(demo_repo: Path, tmp_path: Path) -> None:
    """T09 端到端:模型收到的那一条 tool 消息合法、明确标注省略,并能从仓里取回被裁的行。"""
    from app.gitops.snapshot import create_workspace
    from app.graph.plain_loop import run_plain_loop

    baseline = create_workspace(demo_repo, tmp_path / "ws")
    body = "\n".join(f"needle_{index} = {'z' * 70}" for index in range(400))
    _write(tmp_path / "ws" / "src" / "huge.py", body)
    ctx = _ctx(tmp_path / "ws", tmp_path)
    ctx.baseline_commit = baseline
    model = _RecordingModel([{"tool": "read_file", "args": {"path": "src/huge.py", "limit": 400}}])
    run_plain_loop(ctx, model, "issue", max_turns=4, allowed_tools=READ_TOOLS)

    tool_messages = [m for m in model.seen[-1] if m.get("role") == "tool"]
    assert tool_messages, "循环没有把工具回执发回模型"
    content = str(tool_messages[0]["content"])
    parsed = json.loads(content)  # 旧实现在这一步抛 JSONDecodeError
    assert len(content) <= get_settings().tool_visible_max_chars
    assert parsed["_bounded"]["omitted_lines"] > 0 and "needle_200" not in parsed["content"]
    evidence_id = str(parsed["evidence"]["evidence_id"])

    # 同一份现场里被裁掉的那一行,靠引用取得回来
    collected = ""
    cursor = ""
    for _ in range(200):
        page = execute(ctx, "read_evidence", {"evidence_id": evidence_id, "cursor": cursor})
        assert page.ok, page.error
        collected += page.output["text"]  # type: ignore[index]
        if page.output["eof"]:  # type: ignore[index]
            break
        cursor = str(page.output["next_cursor"])  # type: ignore[index]
    assert "needle_200" in collected


def test_the_old_char_cut_really_broke_the_json() -> None:
    """反向对照:同一份 payload 走旧的 fold_output 路径就是非法 JSON。

    这条用例不改任何行为,它钉的是"缺陷真实存在过、而且新路径确实绕开了它"——
    没有这一条,上面那条通过只能说明新代码自洽,说明不了旧代码坏了。
    """
    from app.tools.output_filter import fold_output

    payload = {"path": "src/huge.py", "content": "\n".join(f"line_{i} = 'x'" for i in range(3000))}
    legacy = fold_output(json.dumps(payload, ensure_ascii=False), head=40, tail=15)
    with pytest.raises(json.JSONDecodeError):
        json.loads(legacy)
    assert legacy.endswith("... (truncated)")
    bounded = json.dumps(
        bound_structure(payload, max_chars=8000, head_lines=40, tail_lines=15).value,
        ensure_ascii=False,
    )
    assert json.loads(bounded) and len(bounded) <= 8000


def test_disabled_store_says_the_raw_is_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """关掉证据仓:照裁,但**明说**取不回来 —— 有信息损失的开关不许装作什么都没发生。"""
    _write(tmp_path / "ws" / "src" / "big.py", "\n".join(f"d = {'f' * 90}" for _ in range(300)))
    _override(monkeypatch, evidence_enabled=False)
    ctx = _ctx(tmp_path / "ws", tmp_path)
    result = execute(ctx, "read_file", {"path": "src/big.py", "limit": 400})
    parsed = json.loads(json.dumps(result.visible, ensure_ascii=False, default=str))
    assert "evidence" not in parsed
    assert parsed["_bounded"]["raw_saved"] is False
    assert "not retrievable" in parsed["_bounded"]["note"]
    assert result.evidence is None
    assert not (tmp_path / "evidence").exists()
