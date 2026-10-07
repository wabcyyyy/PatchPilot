"""只读类工具:list_files / search_code / read_file。

`search_code` 的取数逻辑住在 `app/tools/search.py`(ripgrep 加速 + Python 回落两条路径);
本模块只负责"能用哪些文件"(工作区边界、SKIP_DIRS、glob、500 条上限)与参数夹扣,
两条路径因此永远共享同一份文件序列。
"""

from __future__ import annotations

import fnmatch
import logging
import re
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.tools.base import ToolContext, ToolResult
from app.tools.paths import looks_like_text, relpath_within
from app.tools.search import ENGINE_KEY, compile_pattern, grep_files

log = logging.getLogger(__name__)

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules", ".venv"}
MAX_LIST_FILES = 500


def collect_repo_files(workspace: Path, glob: str | None, limit: int) -> tuple[list[str], bool]:
    """仓库内文件的相对 POSIX 路径 + **是否被 limit 裁过**。

    只按仓库内相对段匹配 SKIP_DIRS(复盘 P2):`path.parts` 含 workspace 绝对路径段,
    workspace 落在 node_modules/.venv 等目录下时会全量误伤。

    `limit` 的两个用途必须分开(M3.6,证据见 PROGRESS.md D.12):`list_files` 用它裁**输出体量**
    (500 条),而检索与符号查找必须用独立的高上限裁**遍历域**——两者共用 500 时,
    排序第 500 个之后的文件对模型彻底不可见(9 道真实难题里 4 道的必改文件正是如此)。
    返回值里的 `capped` 让调用方能把"这次没搜全"如实告诉模型:一份声称穷尽仓库却漏了半仓的
    空结果是谎言,比慢一点危害大得多。
    """
    out: list[str] = []
    for path in sorted(workspace.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(workspace).as_posix()
        if any(part in SKIP_DIRS for part in rel.split("/")):
            continue
        if glob and not fnmatch.fnmatch(rel, glob):
            continue
        out.append(rel)
        if len(out) >= limit:
            return out, True
    return out, False


def search_scope(workspace: Path, glob: str | None = None) -> tuple[list[str], bool]:
    """检索/符号查找的遍历域:上限来自 `Settings.max_search_files`,与列表输出体量无关。"""
    limit = max(1, get_settings().max_search_files)
    return collect_repo_files(workspace, glob, limit)


def _iter_repo_files(workspace: Path, glob: str | None) -> list[str]:
    """兼容入口:列表口径(500 条上限),只给 `list_files` 与骨架用。"""
    files, _ = collect_repo_files(workspace, glob, MAX_LIST_FILES)
    return files


def list_files(ctx: ToolContext, subdir: str = "", glob: str | None = None) -> ToolResult:
    """列出仓库文件(相对路径),供 Agent 建立结构认知。

    `truncated=True` 表示清单被 500 条上限裁过(此前只给 count 不告知裁过,模型会把
    "前 500 条"当成整个仓库);大仓库的真确形状请用 `find_symbol`/`search_code`。
    """
    root = ctx.workspace
    if subdir:
        base = relpath_within(root, subdir)
        if base is None or not base.is_dir():
            return ToolResult.fail(f"subdir not found or outside workspace: {subdir!r}")
        root = base
    files, capped = collect_repo_files(root, glob, MAX_LIST_FILES)
    return ToolResult(ok=True, output={"count": len(files), "files": files, "truncated": capped})


def _non_negative_int(value: object, label: str) -> int:
    """模型传入的可选整数参数:None/缺省 → 0(即旧行为),负数按 0 处理,非整数报错点名参数。"""
    if value is None:
        return 0
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be an integer, got {value!r}") from None
    return max(0, number)


def read_file(ctx: ToolContext, path: str, offset: int = 1, limit: int | None = None) -> ToolResult:
    """读取仓库内文本文件的一段(1-based offset),拒绝二进制与越界路径。

    `limit` 把窗口收到 N 行,并与 `ctx.max_read_lines` 取小——**天花板由 Settings 决定,
    模型传更大的数只会拿到更少**,不可能拿到更多;不传 limit 时与旧行为逐字一致。
    """
    target = relpath_within(ctx.workspace, path)
    if target is None or not target.is_file():
        return ToolResult.fail(f"file not found or outside workspace: {path!r}")
    if not looks_like_text(target):
        return ToolResult.fail(f"refusing to read binary file: {path!r}")

    window = ctx.max_read_lines
    if limit is not None:
        try:
            requested = int(limit)
        except (TypeError, ValueError):
            return ToolResult.fail(f"limit must be an integer, got {limit!r}")
        if requested < 1:
            return ToolResult.fail(f"limit must be >= 1, got {requested}")
        window = min(requested, ctx.max_read_lines)

    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(lines)
    start = max(1, offset)
    end = min(total, start + window - 1)
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


def search_code(
    ctx: ToolContext,
    keyword: str,
    glob: str | None = None,
    context_lines: int = 0,
    per_file_cap: int = 0,
    regex: bool = False,
) -> ToolResult:
    """仓库检索。默认口径与 M3 之前逐字一致:大小写不敏感的**字面子串**扫描。

    - `regex=True`:按正则匹配(IGNORECASE);非法模式直接失败并把原因点名给模型,
      模型能自己改回来。rg 的正则方言与 Python 不完全同,rg 侧失败一律回落 Python 定案。
    - `context_lines=n`:每条命中附上下各 n 行(默认 0 = 不出 before/after 键);
      上界由 `Settings.max_search_context_lines` 夹扣(夹扣不报错),模型要 10000 行
      也只能拿到天花板——上下文是成本,不是越便宜越好。
    - `per_file_cap=n`:单文件最多贡献 n 条命中(默认 0 = 不限,与旧行为一致),
      用于让结果摊开到多个文件,而不是被一个巨型文件占满。
    - 总数仍受 `ctx.max_search_results`,超出即 `truncated=True`。
    - `engine` 如实标注这条查询由谁服务:`rg` = ripgrep 给了候选文件集,`python` = 整仓扫描
      (行级的文本/行号/裁剪永远只有一份 Python 实现,所以它不是"两种结果");
      回落原因进日志(带 task_id),模型只需要知道有没有加速。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return ToolResult.fail("keyword is empty")
    settings = get_settings()
    try:
        context = _non_negative_int(context_lines, "context_lines")
        cap = _non_negative_int(per_file_cap, "per_file_cap")
    except ValueError as exc:
        return ToolResult.fail(str(exc))
    ceiling = max(0, settings.max_search_context_lines)
    context = min(context, ceiling)  # 夹扣,不报错:模型不会因为你给了它 5 行而不是 5000 行就崩
    if regex:
        try:
            compile_pattern(keyword)
        except re.error as exc:
            return ToolResult.fail(f"invalid regex {keyword!r}: {exc}")

    meta: dict[str, Any] = {}
    scope, scope_capped = search_scope(ctx.workspace, glob)
    matches, truncated = grep_files(
        ctx.workspace,
        keyword,
        files=scope,
        max_results=ctx.max_search_results,
        context_lines=context,
        per_file_cap=cap,
        regex=bool(regex),
        engine=settings.search_engine,
        meta=meta,
    )
    if meta.get("fallback"):
        log.warning("task %s search_code 回落 Python 路径:%s", ctx.task_id, meta["fallback"])
    output: dict[str, Any] = {
        "matches": matches,
        "total": len(matches),
        "truncated": truncated,
        # 遍历域是否被裁过:没搜全时必须让模型看见"这次不是整仓",否则一个空结果会被
        # 读成"仓库里没有",而真相可能是后半仓根本没进视线(D.12 的缺陷就是这么藏的)
        "scope_truncated": scope_capped,
        ENGINE_KEY: meta.get(ENGINE_KEY, "python"),
    }
    if context > 0:
        output["context_lines"] = context  # 夹扣后的实际值:模型以为自己传了 5000,得看见真给了几行
    return ToolResult(ok=True, output=output)
