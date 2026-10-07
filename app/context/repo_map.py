"""仓库骨架(持久记忆):纯本地、确定性、零 LLM 调用的结构预注入。

缺陷出处(PROGRESS.md D.4):仓库结构**从未**进过提示——没有文件树/模块大纲/符号索引,
结构认知只能靠模型自己调 list_files 现场重建(app/tools/files.py:36-45,上限 500 条),
`SYSTEM_PROMPT` 也只是"建议"它去做(app/prompts.py:43)。付费实跑的后果:LOCALIZE 在
16-19 轮纯 read/search 上烧掉 417,894 tokens(份额顶 400k)且一次补丁都不提
(runs/swe-hard-graph*)。本模块把"重新发现仓库"从模型的 turn 里挪走:第 1 档是文件树
(相对 POSIX 路径,排序后天然按顶层目录聚组),第 2 档是 `*.py` 的 stdlib `ast` 符号大纲
(模块级 class/def/async def 的行区间与签名)。只读、不越出 workspace、不跟随符号链接;
单文件解析失败只让该文件降级为"进树不进大纲"。骨架是**优化项**,异常不得带走任务。
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

from app.context.ast_outline import render_symbols
from app.tools.files import SKIP_DIRS
from app.tools.paths import looks_like_text

log = logging.getLogger(__name__)

# 自描述头部:让模型知道这段是自动生成的只读上下文,并且可能不完整
_HEADER = (
    "<repo_skeleton> 自动生成的只读仓库骨架:文件树 + Python 符号大纲(超大仓库可能不完整)。"
    "(L起-L止) 是定义的行区间,精读请用 read_file 的 offset;不要把它当作完整索引。"
)
_EMPTY = "Repository is empty."
_NOTE = "… skeleton truncated: {count} files omitted"


def _scoped_files(workspace: Path, max_files: int) -> tuple[list[str], int]:
    """仓库内文件的相对 POSIX 路径(排序即按目录聚组)与被 max_files 裁掉的条数。

    SKIP_DIRS 直接 import 自 `app.tools.files`,骨架与 list_files 对"仓库里有什么"永不两样。
    排序用相对路径字符串而非 Path 对象:Windows 的 Path 比较会折叠大小写,跨平台不等价。
    """
    rels: list[str] = []
    for path in workspace.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(workspace).as_posix()
        if any(seg in SKIP_DIRS for seg in rel.split("/")):
            continue
        rels.append(rel)
    rels.sort()
    if len(rels) <= max_files:
        return rels, 0
    return rels[:max_files], len(rels) - max_files


def _outline_block(workspace: Path, rel: str) -> list[str]:
    """一个 .py 的大纲块:`rel [lines N]` + 缩进条目;不合格或解析失败 → 空列表。

    符号渲染助手(`render_symbols` 及其签名/行区间实现)住在 `app/context/ast_outline.py`:
    M3 起 describe_file / find_symbol 用同一套口径,骨架的形状与迁移前逐字一致。
    """
    path = workspace / rel
    if not rel.endswith(".py") or path.is_symlink() or not looks_like_text(path):
        return []  # 非 Python / 符号链接(目标可能在 workspace 外) / 二进制:只进树
    try:
        text = path.read_text(encoding="utf-8")  # 非 UTF-8 抛错,同样只进树
        tree = ast.parse(text, filename=rel)
        entries = render_symbols(tree.body, 0, 0)
    except Exception:
        # 一个坏文件不得带走整份骨架:SyntaxError(半截文件)、RecursionError(超深嵌套)、
        # ValueError/OSError(渲染与读盘异常)都只让该文件降级为"只进树"。
        return []
    if not entries:
        return []  # 没有模块级符号:不出空块,把预算留给有内容的文件
    return [f"{rel} [lines {len(text.splitlines())}]", *entries]


def build_repo_map(workspace: Path, *, max_chars: int, max_files: int = 200) -> str:
    """渲染仓库骨架文本;max_chars <= 0 时返回空串(功能关闭)。

    两档共用同一份预算:先文件树(装多少列多少),再大纲(按文件整块进出,不发半截块)。
    超预算时末尾必发一行 `… skeleton truncated: X files omitted`——截断对模型必须可见,
    否则它会以为骨架是完整的。返回值长度永不超过 max_chars(放不下头部就不出骨架)。
    """
    if max_chars <= 0:
        return ""
    files, capped = _scoped_files(workspace, max_files)
    if not files:
        empty_block = f"{_HEADER}\n{_EMPTY}"
        return empty_block if len(empty_block) <= max_chars else ""
    blocks = [(rel, blk) for rel in files if (blk := _outline_block(workspace, rel))]
    tree_cost = sum(len(rel) + 1 for rel in files)
    outline_cost = sum(len(line) + 1 for _, blk in blocks for line in blk)
    if capped == 0 and len(_HEADER) + tree_cost + outline_cost <= max_chars:
        return "\n".join([_HEADER, *files, *(line for _, blk in blocks for line in blk)])

    note_worst = _NOTE.format(count=capped + len(files) + len(blocks))  # 最坏情况的宽度
    budget = max_chars - len(note_worst) - 1
    if budget <= len(_HEADER):
        return ""  # 连"头部 + 截断行"都放不下:宁可不发,也不发误导性的半份信息

    lines = [_HEADER]
    used = len(_HEADER)
    listed = 0
    for rel in files:
        if used + len(rel) + 1 > budget:
            break
        lines.append(rel)
        used += len(rel) + 1
        listed += 1
    omitted = capped + len(files) - listed
    in_tree = set(files[:listed])
    for rel, blk in blocks:
        if rel not in in_tree:
            continue  # 该文件的树条目已被裁,omitted 里已计过,不重复计
        cost = sum(len(line) + 1 for line in blk)
        if used + cost > budget:
            omitted += 1
            continue  # 整块放不下就跳过它,继续试后面的小块
        lines.extend(blk)
        used += cost
    lines.append(_NOTE.format(count=omitted))
    return "\n".join(lines)


def repo_map_for_workspace(workspace: Path, max_chars: int, *, max_files: int = 200) -> str:
    """`build_repo_map` 的"永不致命"外壳:任何异常都退化为空骨架,不向上抛。

    任务因为一个可选上下文块崩掉是可靠性的倒退;max_files 开口让 Settings 的
    repo_map_max_files 真被接线(默认值与 build_repo_map 一致)。
    """
    try:
        return build_repo_map(workspace, max_chars=max_chars, max_files=max_files)
    except Exception:
        log.warning("repo_map: 骨架生成失败,降级为无骨架 workspace=%s", workspace, exc_info=True)
        return ""
