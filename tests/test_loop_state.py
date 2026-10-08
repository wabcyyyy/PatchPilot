"""M6 A 级恢复的序列化契约:LoopSnapshot 读写、原子性、降级路径。

这一层是"纯序列化"测试(不跑循环、不碰图):快照机制最危险的失效模式不是
"续不上",而是"续错了还被信任"——所以读侧的四个 None 分支与写侧的
"任何失败都不上扬"必须逐条钉住,而不是靠上层用例间接覆盖。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.graph.loop_state import (
    MAX_SNAPSHOT_BYTES,
    SNAPSHOT_FILENAME,
    SNAPSHOT_VERSION,
    LoopSnapshot,
    delete_loop_snapshot,
    load_loop_snapshot,
    save_loop_snapshot,
    snapshot_path,
)
from app.tools.base import ToolContext
from app.tools.tracker import Tracker


def _ctx(tmp_path: Path, *, tracker_path: Path | object | None = ...) -> ToolContext:
    """最小 ToolContext:快照只用到 tracker 的目录口径,不需要真工作区。

    `tracker_path=...`(未传)→ 轨迹落 tmp_path,快照与它同目录;
    `tracker_path=None` → 内存 Tracker,没有任何目录可用(降级路径)。
    """
    path = tmp_path / "trajectory.jsonl" if tracker_path is ... else tracker_path
    return ToolContext(
        task_id="T-SNAP",
        workspace=tmp_path / "ws",
        baseline_commit="0" * 40,
        tracker=Tracker(path, task_id="T-SNAP"),
        report_dir=tmp_path / "reports",
    )


def _snapshot(turn_no: int = 3, stage: str = "PROPOSE_PATCH", round_no: int = 1) -> LoopSnapshot:
    return LoopSnapshot(
        stage=stage,
        round_no=round_no,
        turn_no=turn_no,
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "issue"},
            {"role": "assistant", "content": "第 3 轮"},
        ],
        tokens_spent=1_234,
        tokens_prompt=1_000,
        tokens_completion=234,
        last_content="最后一轮的实质文本",
        task_id="T-SNAP",
    )


def test_snapshot_lands_next_to_trajectory(tmp_path: Path) -> None:
    """落盘位置复用 Tracker 的目录口径(与 trajectory.jsonl 同目录),不另造路径体系。"""
    ctx = _ctx(tmp_path)
    assert ctx.tracker.path is not None
    assert snapshot_path(ctx) == ctx.tracker.path.parent / SNAPSHOT_FILENAME
    save_loop_snapshot(ctx, _snapshot())
    assert (tmp_path / SNAPSHOT_FILENAME).is_file()
    # 原子写的中间产物不得残留
    assert not (tmp_path / f"{SNAPSHOT_FILENAME}.tmp").exists()


def test_snapshot_roundtrip_keeps_every_field(tmp_path: Path) -> None:
    save_loop_snapshot(tmp_path, _snapshot(turn_no=7))
    loaded = load_loop_snapshot(tmp_path, stage="PROPOSE_PATCH", round_no=1, task_id="T-SNAP")
    assert loaded is not None
    assert loaded.version == SNAPSHOT_VERSION
    assert loaded.turn_no == 7
    assert loaded.stage == "PROPOSE_PATCH"
    assert loaded.round_no == 1
    assert loaded.tokens_spent == 1_234
    assert loaded.tokens_prompt == 1_000
    assert loaded.tokens_completion == 234
    assert loaded.last_content == "最后一轮的实质文本"
    assert loaded.task_id == "T-SNAP"
    assert loaded.written_at  # 由 save 补时间戳
    assert loaded.messages == _snapshot().messages
    assert isinstance(load_loop_snapshot(tmp_path), LoopSnapshot)


def test_pathless_tracker_degrades_to_no_snapshot(tmp_path: Path) -> None:
    """内存 Tracker(无落盘路径)→ 没有目录就没有快照,且不得抛。"""
    ctx = _ctx(tmp_path, tracker_path=None)
    assert snapshot_path(ctx) is None
    save_loop_snapshot(ctx, _snapshot())
    assert load_loop_snapshot(ctx) is None
    delete_loop_snapshot(ctx)


def test_missing_file_returns_none(tmp_path: Path) -> None:
    assert load_loop_snapshot(tmp_path, task_id="T-SNAP") is None


@pytest.mark.parametrize(
    "corrupt",
    ["{不是 JSON", "", "[]", '{"version": 1}', json.dumps({"version": 99})],
)
def test_unusable_payloads_return_none(tmp_path: Path, corrupt: str, caplog) -> None:
    """缺字段/坏 JSON/版本不符一律"没有快照";降级必须带 task_id 打 warning。"""
    (tmp_path / SNAPSHOT_FILENAME).write_text(corrupt, encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert load_loop_snapshot(tmp_path, task_id="T-SNAP") is None
    assert any("T-SNAP" in record.message for record in caplog.records)


def test_messages_must_be_objects_not_scalars(tmp_path: Path) -> None:
    """messages 里混进非对象条目(截断/手工改动)→ 整份作废,而不是带着脏历史续跑。"""
    payload = {
        "version": SNAPSHOT_VERSION,
        "stage": "PLAN",
        "round_no": 2,
        "turn_no": 1,
        "messages": ["不是对象"],
    }
    (tmp_path / SNAPSHOT_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    assert load_loop_snapshot(tmp_path, task_id="T-SNAP") is None


def test_stage_and_round_mismatch_returns_none(tmp_path: Path) -> None:
    save_loop_snapshot(tmp_path, _snapshot(stage="LOCALIZE", round_no=1))
    # 阶段不符:绝不能把定位段的工作记忆灌进补丁段
    assert load_loop_snapshot(tmp_path, stage="PROPOSE_PATCH", round_no=1) is None
    # 轮次不符:第 2 轮的补丁段不是第 1 轮的续点
    assert load_loop_snapshot(tmp_path, stage="LOCALIZE", round_no=2) is None
    # 两者都符才可用
    assert load_loop_snapshot(tmp_path, stage="LOCALIZE", round_no=1) is not None


def test_oversized_payload_is_not_written(tmp_path: Path, monkeypatch, caplog) -> None:
    """体量超限 → 跳过写盘、不抛、不留文件;恢复方顶多用不到快照,运行不受影响。"""
    monkeypatch.setattr("app.graph.loop_state.MAX_SNAPSHOT_BYTES", 64)
    with caplog.at_level("DEBUG"):
        save_loop_snapshot(
            tmp_path,
            LoopSnapshot(
                stage="LOOP", messages=[{"role": "user", "content": "x" * 5_000}], task_id="T-SNAP"
            ),
        )
    assert not (tmp_path / SNAPSHOT_FILENAME).exists()
    assert MAX_SNAPSHOT_BYTES > 64  # 只改了模块内常量,真实上界仍是 2MiB 档
    assert any("not written" in record.message for record in caplog.records)


def test_replace_failure_leaves_no_trusted_file(tmp_path: Path, monkeypatch, caplog) -> None:
    """崩溃正好发生在原子替换那一步:旧文件不得被半截内容顶替,且异常不上扬。"""
    save_loop_snapshot(tmp_path, _snapshot(turn_no=2))
    original = (tmp_path / SNAPSHOT_FILENAME).read_text(encoding="utf-8")

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("simulated crash during os.replace")

    monkeypatch.setattr(os, "replace", _boom)
    with caplog.at_level("WARNING"):
        save_loop_snapshot(tmp_path, _snapshot(turn_no=5))  # 不得抛
    assert any("resume unavailable" in record.message for record in caplog.records)
    # 读到的仍是上一份完整快照(turn 2),不是 turn 5 的半截,也没有 .tmp 残留
    loaded = load_loop_snapshot(tmp_path, stage="PROPOSE_PATCH", round_no=1)
    assert loaded is not None and loaded.turn_no == 2
    assert (tmp_path / SNAPSHOT_FILENAME).read_text(encoding="utf-8") == original
    assert not (tmp_path / f"{SNAPSHOT_FILENAME}.tmp").exists()


def test_delete_removes_snapshot_and_is_idempotent(tmp_path: Path) -> None:
    save_loop_snapshot(tmp_path, _snapshot())
    delete_loop_snapshot(tmp_path, task_id="T-SNAP")
    assert not (tmp_path / SNAPSHOT_FILENAME).exists()
    delete_loop_snapshot(tmp_path, task_id="T-SNAP")  # 再删一次不得抛
    assert load_loop_snapshot(tmp_path) is None
