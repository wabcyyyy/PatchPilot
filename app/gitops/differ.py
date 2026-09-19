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
    # --no-renames(R2 整改):git 默认 rename 检测会把"改名为 pytest.py"渲染成
    # rename 段(无 new file mode 行),影子门禁按新增文件判定的机制会被整体绕过;
    # 关掉后 rename 退化为 delete+new file,旧/新路径都完整进入 diff 与证据链
    _, diff_out, _ = run_git(ws, "diff", "--no-color", "--no-renames")
    _, names_out, _ = run_git(ws, "diff", "--name-only", "--no-renames")
    changed = [line.strip() for line in names_out.splitlines() if line.strip()]
    result = DiffResult(diff_text=diff_out, changed_files=changed, is_empty=not diff_out.strip())
    log.debug("working_tree_diff: %d file(s) changed", len(changed))
    return result
