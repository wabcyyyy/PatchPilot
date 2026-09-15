"""读取工作区改动:`git_diff` 工具的底层实现。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from app.gitops.cmd import run_git

log = logging.getLogger(__name__)


@dataclass
class DiffResult:
    """工作区当前改动的 unified diff。"""

    diff_text: str
    changed_files: list[str]
    is_empty: bool


def working_tree_diff(workspace: Path | str) -> DiffResult:
    """返回工作区相对基线的 unified diff(含新增文件)。

    通过 `git add -A -N`(intent-to-add)让未跟踪文件也出现在 diff 中;
    该操作只改 index 标记,不影响工作树内容,回滚时一并清理。
    """
    ws = Path(workspace)
    run_git(ws, "add", "-A", "-N")
    _, diff_out, _ = run_git(ws, "diff", "--no-color")
    _, names_out, _ = run_git(ws, "diff", "--name-only")
    changed = [line.strip() for line in names_out.splitlines() if line.strip()]
    result = DiffResult(diff_text=diff_out, changed_files=changed, is_empty=not diff_out.strip())
    log.debug("working_tree_diff: %d file(s) changed", len(changed))
    return result
