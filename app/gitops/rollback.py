"""工作区回滚:`reset_workspace` 工具的底层实现。

用 `reset --hard <baseline>` + `clean -fdx` 而不是 `revert`:
工作区是整体丢弃式的临时副本,需要恢复到逐字节等于基线的状态,
包括删除 Agent 新增的未跟踪文件——revert 只生成反向提交,做不到这一点。
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.gitops.cmd import run_git

log = logging.getLogger(__name__)


def reset_workspace(workspace: Path | str, baseline_commit: str) -> None:
    """把工作区恢复到基线 commit:已跟踪改动还原,未跟踪文件全部删除。"""
    ws = Path(workspace)
    run_git(ws, "reset", "--hard", baseline_commit)
    # -ff:连带清掉未跟踪的内嵌 git 仓库(默认 clean 会跳过它们)
    run_git(ws, "clean", "-ffdx")
    log.info("workspace reset to baseline %s", baseline_commit[:12])


def working_tree_is_clean(workspace: Path | str) -> bool:
    """工作区是否与 HEAD 一致(用于回滚后的自证)。"""
    _, out, _ = run_git(Path(workspace), "status", "--porcelain")
    return not out.strip()
