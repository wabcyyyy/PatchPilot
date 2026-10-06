"""工具层公共类型:ToolContext 与 ToolResult。"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.executor.whitelist import DEFAULT_WHITELIST
from app.tools.tracker import Tracker


@dataclass
class ToolContext:
    """一次任务执行期内所有工具共享的上下文。

    workspace 内的一切操作都被限制在该目录;测试执行只允许
    manifest 预定义的 test_sets,不允许 Agent 任意指定用例。
    """

    task_id: str
    workspace: Path
    baseline_commit: str
    tracker: Tracker
    report_dir: Path = field(
        default_factory=lambda: Path("runs")
    )  # junit 等报告的落盘目录(工作区外)
    python_exe: str = field(default_factory=lambda: sys.executable)
    test_sets: dict[str, list[str]] = field(default_factory=dict)
    allowed_paths: list[str] | None = None
    whitelist: tuple[str, ...] = DEFAULT_WHITELIST
    max_read_lines: int = 400
    max_search_results: int = 50
    max_patch_files: int = 5
    test_timeout_seconds: int = 120
    forbid_test_files: bool = True
    # 自适应分支触发信号(卡5):连续"补丁应用失败"的次数(门禁/协议拒绝不计——
    # 那是可修正的单线反馈,只有真应用失败才说明当前思路在原地打转)
    patch_fail_streak: int = 0


@dataclass
class ToolResult:
    ok: bool
    output: Any = None
    error: str | None = None

    @classmethod
    def fail(cls, error: str) -> ToolResult:
        return cls(ok=False, output=None, error=error)
