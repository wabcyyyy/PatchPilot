"""checkpoint:SqliteSaver 封装,按 superstep 留档 + 崩溃恢复的位置权威。

如实声明(R2 写下、M6 改写本段):恢复分两级——A 级是 Agent 循环的工作记忆
(见 app/graph/loop_state.py,按 turn 边界落 run_dir/loop_state.json),B 级是图状态与
"下一个该跑的节点"(读侧见 app/graph/resume.py,按 thread_id=task_id 取回检查点)。
R2 时代本模块只写不读,所以当时"僵尸任务只能靠 recover_stale 判死"是事实;M6 起该
声明作废。仍然成立的两条边界:
①闭包持有的 ToolContext 等运行时对象不进 state,恢复时按留档产物**重建**
  (TaskNodes.restore_runtime),不是跨进程反序列化对象;
②没有可用循环快照、或题面无法重建的僵尸任务,照旧由 recover_stale 收敛 NEEDS_REVIEW。
连接由调用方(runner)在任务结束时关闭。
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def make_sqlite_checkpointer(db_path: Path | str) -> Any | None:
    """返回 SqliteSaver;依赖缺失或初始化失败时返回 None(任务照常执行,只是不可恢复)。

    直接持有 sqlite3 连接:from_conn_string 返回的是上下文管理器,
    图执行结束后连接会被关闭,不适合跨进程恢复场景。
    """
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError:
        log.warning("langgraph-checkpoint-sqlite not installed; running without checkpoint")
        return None

    try:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        return SqliteSaver(conn)
    except Exception as exc:
        log.warning("checkpointer init failed (%s); running without checkpoint", exc)
        return None
