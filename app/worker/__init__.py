"""PatchPilot Worker 模块 (P11-2)。

提供独立 Worker 进程能力：持久队列轮询、租约维持心跳、单实例保护与任务执行。
"""

from __future__ import annotations

from app.worker.worker import Worker

__all__ = ["Worker"]
