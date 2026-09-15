"""只读类工具:list_files / search_code / read_file。"""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path

from app.tools.base import ToolContext, ToolResult
from app.tools.paths import looks_like_text, relpath_within

log = logging.getLogger(__name__)

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules", ".venv"}
MAX_LIST_FILES = 500


def _iter_repo_files(workspace: Path, glob: str | None) -> list[str]:
    out: list[str] = []
    for path in sorted(workspace.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(workspace).as_posix()
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if glob and not fnmatch.fnmatch(rel, glob):
            continue
        out.append(rel)
        if len(out) >= MAX_LIST_FILES:
            break
    return out


def list_files(ctx: ToolContext, subdir: str = "", glob: str | None = None) -> ToolResult:
    """列出仓库文件(相对路径),供 Agent 建立结构认知。"""
    root = ctx.workspace
    if subdir:
        base = relpath_within(root, subdir)
        if base is None or not base.is_dir():
            return ToolResult.fail(f"subdir not found or outside workspace: {subdir!r}")
        root = base
    files = _iter_repo_files(root, glob)
    return ToolResult(ok=True, output={"count": len(files), "files": files})


def read_file(ctx: ToolContext, path: str, offset: int = 1) -> ToolResult:
    """读取仓库内文本文件的一段(1-based offset),拒绝二进制与越界路径。"""
    target = relpath_within(ctx.workspace, path)
    if target is None or not target.is_file():
        return ToolResult.fail(f"file not found or outside workspace: {path!r}")
    if not looks_like_text(target):
        return ToolResult.fail(f"refusing to read binary file: {path!r}")

    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(lines)
    start = max(1, offset)
    end = min(total, start + ctx.max_read_lines - 1)
    content = "\n".join(lines[start - 1 : end])
    return ToolResult(
        ok=True,
        output={
            "path": path,
            "total_lines": total,
            "offset": start,
            "returned_lines": end - start + 1 if total else 0,
            "content": content,
            "truncated": end < total,
        },
    )


def search_code(ctx: ToolContext, keyword: str, glob: str | None = None) -> ToolResult:
    """大小写不敏感的子串搜索,返回匹配行与位置(有条数上限)。"""
    keyword = (keyword or "").strip()
    if not keyword:
        return ToolResult.fail("keyword is empty")
    needle = keyword.lower()
    matches: list[dict[str, object]] = []
    truncated = False
    for rel in _iter_repo_files(ctx.workspace, glob):
        if len(matches) >= ctx.max_search_results:
            truncated = True
            break
        path = ctx.workspace / rel
        if not looks_like_text(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if needle in line.lower():
                if len(matches) >= ctx.max_search_results:
                    truncated = True
                    break
                matches.append({"path": rel, "line": line_no, "text": line.strip()[:200]})
        if truncated:
            break
    return ToolResult(
        ok=True, output={"matches": matches, "total": len(matches), "truncated": truncated}
    )
