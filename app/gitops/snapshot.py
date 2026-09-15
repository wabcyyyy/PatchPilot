"""工作区快照:把源仓库复制为隔离工作区并固定基线 commit。

为什么复制而不是在源仓库上直接打补丁:验证失败时可整体丢弃,源仓库永远只读。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from app.errors import TaskError
from app.gitops.cmd import run_git

log = logging.getLogger(__name__)

_COPY_IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc", ".ruff_cache")


def is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def resolve_commit(source: Path, commit: str | None) -> str:
    """解析源仓库的基线 commit(默认 HEAD)。"""
    rc, out, _ = run_git(source, "rev-parse", commit or "HEAD", check=False)
    if rc != 0:
        raise TaskError(f"cannot resolve commit {commit!r} in {source}")
    return out.strip()


def create_workspace(source: Path | str, workspace: Path | str, commit: str | None = None) -> str:
    """复制源仓库到 workspace(含 .git),检出指定 commit,返回基线 commit。

    workspace 必须不等于源仓库,且不能是源仓库的祖先目录。
    """
    src = Path(source).resolve()
    dst = Path(workspace).resolve()
    if not src.exists() or not is_git_repo(src):
        raise TaskError(f"source is not a git repository: {src}")
    if dst == src or dst in src.parents or src in dst.parents:
        # 两棵树必须互不包含:工作区在源仓库内会造成递归复制并污染源仓库;
        # 源仓库在工作区内则会在清理工作区时删掉源仓库。
        raise TaskError(f"workspace {dst} and source {src} must not contain each other")
    if dst.exists():
        shutil.rmtree(dst)

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, ignore=_COPY_IGNORE)

    if commit:
        rc, _, err = run_git(dst, "checkout", "--force", commit, check=False)
        if rc != 0:
            raise TaskError(f"cannot checkout {commit!r} in workspace: {err.strip()}")

    rc, out, _ = run_git(dst, "rev-parse", "HEAD", check=False)
    if rc != 0:
        raise TaskError(f"workspace copy lost git history: {dst}")
    baseline = out.strip()
    log.info("workspace ready at %s (baseline=%s)", dst, baseline[:12])
    return baseline
