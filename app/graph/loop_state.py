"""循环工作记忆快照(M6 A 级恢复):Agent 循环的可续跑状态落盘。

为什么需要这一层:LangGraph 的 SqliteSaver 只存 **superstep 之间的 TaskState**,
而 `run_plain_loop` 的 `messages` / token 计数 / `last_content` 是函数局部变量,
一次 superstep 内跑 20 轮也只有一个检查点。进程在 PROPOSE 第 19 轮死掉时,
按检查点重放等于把整个阶段冷启动重跑一遍——已花出去的 19 轮全部作废。
本模块把"每个 turn 边界的一致状态"(工具结果已全部回填进 messages)写成
`<run_dir>/loop_state.json`,让恢复从第 20 轮开始而不是从第 1 轮开始。

刻意的设计边界:
- **纯序列化**:除写这一个文件外零副作用,不碰工作区、不碰 DB、不碰轨迹;
- **绝不抛异常**:写失败/超限/目录不可用一律降级为"没有快照",恢复是加速器
  而不是正确性依赖——快照机制本身不得把一次本可完成的运行搞失败;
- **读侧宁缺勿信**:版本、阶段、轮次任一不符即视为无快照(见 `load_loop_snapshot`)。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.tools.base import ToolContext

log = logging.getLogger(__name__)

# 快照格式版本:结构变更必须升此值,旧快照随即失效(读侧按不符处理,不做兼容层)
SNAPSHOT_VERSION = 1
# 与 trajectory.jsonl 同目录(run_dir),文件名固定——恢复方与写入方只认这一个约定
SNAPSHOT_FILENAME = "loop_state.json"
# 体量上界:超过即不写。快照是"可选加速器",不该让一次 turn 边界的写盘
# 变成比任务本身更贵的东西(长任务的 messages 可达数百 KB 至 MB 级)
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024


@dataclass
class LoopSnapshot:
    """一次循环在某个 turn 边界的完整可续跑状态。

    `messages` 是发给模型的工作记忆本体(含 system/user/assistant/tool 各角色,
    OpenAI 线格式);`tokens_*` 是该阶段的**累计**用量(恢复后继续累加,不重置);
    `turn_no` 是"已完成到第几轮"——恢复从 `turn_no + 1` 开始,阶段轮次上限不变。
    """

    version: int = SNAPSHOT_VERSION
    stage: str = ""
    round_no: int = 0
    turn_no: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    tokens_spent: int = 0
    tokens_prompt: int = 0
    tokens_completion: int = 0
    last_content: str = ""
    task_id: str = ""
    written_at: str = ""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def snapshot_path(target: ToolContext | Path) -> Path | None:
    """解析快照落盘位置:与 trajectory.jsonl 同目录(Tracker 的目录口径)。

    ToolContext 走 `tracker.path.parent`(轨迹文件所在目录即 run_dir),Tracker 为
    内存模式(path=None)时返回 None——没有目录就没有快照,不另造路径规则。
    """
    if isinstance(target, Path):
        return target / SNAPSHOT_FILENAME
    tracker_path = target.tracker.path
    return tracker_path.parent / SNAPSHOT_FILENAME if tracker_path is not None else None


def save_loop_snapshot(target: ToolContext | Path, snapshot: LoopSnapshot) -> None:
    """原子写快照(tmp + os.replace);任何失败都只记日志,绝不向上抛。

    原子性是硬要求:崩溃正好发生在写盘中间时,恢复方读到的必须是"完整的旧快照"
    或"完整的新快照",不能是半截 JSON——半截文件被信任等于用错的工作记忆续跑。
    """
    path = snapshot_path(target)
    if path is None:
        log.debug("task %s: no tracker dir; loop snapshot skipped", snapshot.task_id)
        return
    tmp = path.with_name(path.name + ".tmp")
    try:
        payload: dict[str, Any] = asdict(snapshot)
        payload["version"] = SNAPSHOT_VERSION
        payload["written_at"] = payload.get("written_at") or _now_iso()
        text = json.dumps(payload, ensure_ascii=False)
        size = len(text.encode("utf-8"))
        if size > MAX_SNAPSHOT_BYTES:
            # 超限只跳过写盘:旧的上一份快照仍在,恢复方拿到的是更早的 turn 边界,
            # 一致性由"每个边界都完整"保证,不会拿到半截或错阶段的状态
            log.debug(
                "task %s: loop snapshot %dB exceeds cap %dB; not written",
                snapshot.task_id,
                size,
                MAX_SNAPSHOT_BYTES,
            )
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:  # 快照写失败绝不允许打断运行(见模块 docstring)
        log.warning(
            "task %s: failed to write loop snapshot (%s); resume unavailable",
            snapshot.task_id,
            exc,
        )
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)  # 半截 tmp 不得留下被误读(它永远不会被 load 读)


def load_loop_snapshot(
    target: ToolContext | Path,
    *,
    stage: str = "",
    round_no: int | None = None,
    task_id: str = "",
) -> LoopSnapshot | None:
    """读快照;缺失/损坏/版本不符/阶段轮次不符一律返回 None(绝不抛)。

    调用方用 stage/round_no 表达"我要续的是哪个阶段的哪一轮",不符即视为无快照——
    这是防止把 LOCALIZE 的工作记忆灌进 PROPOSE 的唯一闸门,由读侧把守而不是写侧。
    """
    path = snapshot_path(target)
    if path is None or not path.is_file():
        return None
    label = task_id or _peek_task_id(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("task %s: loop snapshot unreadable at %s (%s)", label, path, exc)
        return None
    if not isinstance(raw, dict):
        log.warning("task %s: loop snapshot is not an object; ignoring", label)
        return None
    if raw.get("version") != SNAPSHOT_VERSION:
        log.warning(
            "task %s: loop snapshot version %r != %r; ignoring",
            label,
            raw.get("version"),
            SNAPSHOT_VERSION,
        )
        return None
    if stage and raw.get("stage") != stage:
        log.warning(
            "task %s: loop snapshot stage %r != expected %r; ignoring",
            label,
            raw.get("stage"),
            stage,
        )
        return None
    if round_no is not None and raw.get("round_no") != round_no:
        log.warning(
            "task %s: loop snapshot round %r != expected %r; ignoring",
            label,
            raw.get("round_no"),
            round_no,
        )
        return None
    try:
        messages = raw.get("messages")
        if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
            raise TypeError("messages must be a list of objects")
        return LoopSnapshot(
            version=int(raw.get("version", SNAPSHOT_VERSION)),
            stage=str(raw.get("stage", "")),
            round_no=int(raw.get("round_no", 0)),
            turn_no=int(raw.get("turn_no", 0)),
            messages=[dict(m) for m in messages],
            tokens_spent=int(raw.get("tokens_spent", 0)),
            tokens_prompt=int(raw.get("tokens_prompt", 0)),
            tokens_completion=int(raw.get("tokens_completion", 0)),
            last_content=str(raw.get("last_content", "") or ""),
            task_id=str(raw.get("task_id", "")),
            written_at=str(raw.get("written_at", "")),
        )
    except (TypeError, ValueError) as exc:
        log.warning("task %s: loop snapshot malformed (%s); ignoring", label, exc)
        return None


def delete_loop_snapshot(target: ToolContext | Path, *, task_id: str = "") -> None:
    """阶段正常收尾时删除快照:完成过的阶段永远不该被再续跑(见 plain_loop finish 路径)。"""
    path = snapshot_path(target)
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        # 删不掉只多留一份无人引用的文件,不影响结论;记录以便排查
        log.warning("task %s: failed to remove loop snapshot at %s (%s)", task_id, path, exc)


def _peek_task_id(path: Path) -> str:
    """尽力从损坏文件里取出 task_id 供日志使用;失败返回空串(不影响判定)。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return str(raw.get("task_id", ""))
    except Exception:
        return ""
    return ""
