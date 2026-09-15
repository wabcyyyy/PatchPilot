"""checkpoint:SqliteSaver 封装,任务中断后可从最近状态恢复。"""

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
    except Exception as exc:  # noqa: BLE001
        log.warning("checkpointer init failed (%s); running without checkpoint", exc)
        return None
