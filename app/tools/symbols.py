"""符号级只读工具:describe_file(单文件 AST 大纲)与 find_symbol(全仓定义跳转)。

动机(PROGRESS.md D.6):仓库里**没有任何**结构化检索能力——`ast` 唯一的用处是补丁应用后的
语法预检(`app/gitops/patcher.py:138`)。M2 把骨架塞进了系统提示,但骨架是"一次性的地图",
模型要精确回答"X 定义在哪一行、这个文件里都有谁"仍得自己 read_file 逐屏翻
(付费实跑:LOCALIZE 16-19 轮、417,894 tokens、0 补丁)。这两个工具把骨架变成可查询的索引:
`find_symbol` 给位置,`describe_file` 给行区间,之后 `read_file(offset, limit)` 精确取数。

边界口径与 read_file/list_files 完全一致,不新增任何读文件以外的通道:
路径只经 `relpath_within`(越界与逃逸出 workspace 的符号链接一律拒),二进制只经
`looks_like_text` 拒绝;`find_symbol` 的遍历复用 `_iter_repo_files`(同一份 SKIP_DIRS 与
500 条上限),并**跳过符号链接条目**——仓库内一个指向仓库外的软链被解析一次,就等于
把"外部文件的大纲"引进来了,而它从来不是这道工具该负责的东西(read_file 同样拒它)。
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings
from app.context.ast_outline import SYMBOL_KINDS, parse_source, symbol_entries
from app.tools.base import ToolContext, ToolResult
from app.tools.files import search_scope
from app.tools.paths import looks_like_text, relpath_within

log = logging.getLogger(__name__)


def describe_file(ctx: ToolContext, path: str) -> ToolResult:
    """一个 Python 文件的 AST 大纲:顶层类/函数 + 类内方法,含真实行区间与签名。

    用法约定(写进工具描述):大文件先 describe_file 拿到 `start_line/end_line`,
    再 read_file 带 offset/limit 精读,而不是整文件灌进上下文。
    解析失败是**可用的结果**不是错误:`parse_error` 说明原因,模型改用 search/read。
    """
    target = relpath_within(ctx.workspace, path)
    if target is None or not target.is_file():
        return ToolResult.fail(f"file not found or outside workspace: {path!r}")
    if target.suffix != ".py":
        return ToolResult.fail(f"describe_file only parses Python files: {path!r}")
    if not looks_like_text(target):
        return ToolResult.fail(f"refusing to read binary file: {path!r}")
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ToolResult.fail(f"cannot read file: {path!r}: {exc}")

    tree, error = parse_source(text, path)
    symbols = symbol_entries(tree) if tree is not None else []
    return ToolResult(
        ok=True,
        output={
            "path": path,
            "total_lines": len(text.splitlines()),
            "count": len(symbols),
            "symbols": symbols,
            "parse_error": error,
        },
    )


def find_symbol(ctx: ToolContext, name: str, kind: str | None = None) -> ToolResult:
    """全仓符号定义搜索:回答"X 在哪里定义"。结果按路径确定序,受 max_symbol_results 上限。

    匹配语义:先要**精确名**(大小写不敏感);精确名为空才退到**前缀名**——
    搜 `parse_date` 的模型不该被 `parse_datetime`、`parse_date_range` 淹没,
    但一个名字都没有时,"最像的邻居"比"零结果"有用得多(故回应的 `match` 字段如实标注)。
    """
    name = (name or "").strip()
    if not name:
        return ToolResult.fail("name is empty")
    wanted = (kind or "").strip().lower() or None
    if wanted is not None and wanted not in SYMBOL_KINDS:
        return ToolResult.fail(f"unknown kind: {kind!r} (expected one of {list(SYMBOL_KINDS)})")

    cap = max(0, get_settings().max_symbol_results)
    exact: list[dict[str, Any]] = []
    prefix: list[dict[str, Any]] = []
    target = name.lower()
    # 遍历域走 search_scope(高上限),不用 list_files 的 500 条输出口径:
    # 第 501 个文件里的定义也是定义,查不到就是假阴性(PROGRESS.md D.12)
    scope, scope_capped = search_scope(ctx.workspace, "*.py")
    for rel in scope:
        file = ctx.workspace / rel
        if file.is_symlink() or not looks_like_text(file):
            continue  # 符号链接/二进制:与 describe_file 的拒绝口径同,不给外部内容开口子
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        tree, _ = parse_source(text, rel)
        if tree is None:
            continue  # 解析失败的文件不参与符号索引(模型可用 search_code 兜底)
        for entry in symbol_entries(tree):
            if wanted is not None and entry["kind"] != wanted:
                continue
            lowered = entry["name"].lower()
            if lowered != target and not lowered.startswith(target):
                continue
            item = {
                "name": entry["name"],
                "kind": entry["kind"],
                "path": rel,
                "line": entry["start_line"],
                "signature": entry["signature"],
            }
            (exact if lowered == target else prefix).append(item)

    found = exact or prefix
    truncated = len(found) > cap
    return ToolResult(
        ok=True,
        output={
            "symbol": name,
            "kind": wanted,
            "match": "exact" if exact else "prefix" if found else "none",
            "matches": found[:cap],
            "total": min(len(found), cap),
            "truncated": truncated,
            "scope_truncated": scope_capped,
        },
    )
