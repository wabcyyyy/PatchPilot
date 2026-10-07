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
from collections.abc import Sequence
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
# 大仓库口径:结构档是目录汇总(覆盖全部文件),只有取样集内的文件给了逐文件大纲
_LARGE_NOTE = (
    "… {total} files in repo; directory rollup covers all of them, "
    "per-file outlines shown for a {sampled}-file sample ({rendered} rendered) — "
    "use search_code / find_symbol for the rest"
)


def _all_files(workspace: Path) -> list[str]:
    """仓库内全部可见文件的相对 POSIX 路径(排序即按目录聚组)。

    SKIP_DIRS 直接 import 自 `app.tools.files`,骨架与 `list_files` 对"仓库里有什么"永不两样。
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
    return rels


def _dir_rollup(rels: Sequence[str], depth: int) -> list[str]:
    """目录级汇总:`<dir>/  (N files, M py)`,按目录路径排序;根目录文件归入 `./`。

    为什么需要它(M2.5,证据 PROGRESS.md D.12):超过 `max_files` 的大仓库若只列"字母序前
    200 个文件",模型拿到的是只画了角落的半张地图,而它会当成全图来决策。目录汇总的行数由
    **目录数**决定而不是文件数,几十行就能覆盖 1900 个文件的仓库形状,把"有哪些去处"讲完整;
    具体文件则由 `find_symbol`/`search_code`(遍历域已与管理上限解耦)去取。
    """
    counts: dict[str, list[int]] = {}
    for rel in rels:
        segments = rel.split("/")
        directory = "/".join(segments[:-1][:depth]) or "."
        bucket = counts.setdefault(directory, [0, 0])
        bucket[0] += 1
        if rel.endswith(".py"):
            bucket[1] += 1
    return [
        f"{directory}/  ({total} files{f', {py} py' if py else ''})"
        for directory, (total, py) in sorted(counts.items())
    ]


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


def build_repo_map(
    workspace: Path, *, max_chars: int, max_files: int = 200, dir_depth: int = 3
) -> str:
    """渲染仓库骨架文本;`max_chars <= 0` 时返回空串(功能关闭)。

    两档共用同一份预算:先结构档,再符号大纲档。**小仓库**(文件数 ≤ `max_files`)结构档
    就是文件清单,形状与 M2.5 之前逐字一致(它的用例一行未动);**大仓库**改出目录级汇总
    (`_dir_rollup`),因为"字母序前 200 个文件"对 1900 文件的仓库是一张只画了角落的地图,
    而模型会当成全图来决策(M2.5,证据 PROGRESS.md D.12)。
    大纲只给取样集内的文件,取样集外的由 `search_code`/`find_symbol` 覆盖
    (它们的遍历域已与管理上限解耦,见 M3.6)。
    截断对模型必须可见:超预算/取样时末尾必发一行说明,返回值长度永不超过 `max_chars`
    (放不下头部 + 说明行就不出骨架,宁缺不误导)。
    """
    if max_chars <= 0:
        return ""
    all_files = _all_files(workspace)
    if not all_files:
        empty_block = f"{_HEADER}\n{_EMPTY}"
        return empty_block if len(empty_block) <= max_chars else ""
    large = len(all_files) > max_files
    files = all_files[:max_files] if large else all_files
    capped = len(all_files) - len(files)
    tree_lines = _dir_rollup(all_files, dir_depth) if large else files
    blocks = [(rel, blk) for rel in files if (blk := _outline_block(workspace, rel))]
    tree_cost = sum(len(line) + 1 for line in tree_lines)
    outline_cost = sum(len(line) + 1 for _, blk in blocks for line in blk)
    if not large and len(_HEADER) + tree_cost + outline_cost <= max_chars:
        return "\n".join([_HEADER, *files, *(line for _, blk in blocks for line in blk)])

    # 说明行按最坏情况的宽度先扣预算:条数变了文案不能把行挤断
    note_worst = (
        _LARGE_NOTE.format(total=len(all_files), sampled=len(files), rendered=len(blocks))
        if large
        else _NOTE.format(count=capped + len(files) + len(blocks))
    )
    budget = max_chars - len(note_worst) - 1
    if budget <= len(_HEADER):
        return ""  # 连"头部 + 说明行"都放不下:宁可不发,也不发误导性的半份信息

    lines = [_HEADER]
    used = len(_HEADER)
    listed = 0
    for line in tree_lines:
        if used + len(line) + 1 > budget:
            break
        lines.append(line)
        used += len(line) + 1
        listed += 1
    in_tree = set(tree_lines[:listed])
    omitted = capped + len(files) - listed  # 小档口径:没进清单的文件从这里起算
    skipped = 0
    for rel, blk in blocks:
        if not large and rel not in in_tree:
            continue  # 该文件的树条目已被裁,omitted 里已计过,不重复计
        cost = sum(len(line) + 1 for line in blk)
        if used + cost > budget:
            skipped += 1
            continue  # 整块放不下就跳过它,继续试后面的小块
        lines.extend(blk)
        used += cost
    if large:
        lines.append(
            _LARGE_NOTE.format(total=len(all_files), sampled=len(files), rendered=len(blocks))
        )
    else:
        lines.append(_NOTE.format(count=omitted + skipped))
    return "\n".join(lines)


def repo_map_for_workspace(
    workspace: Path, max_chars: int, *, max_files: int = 200, dir_depth: int = 3
) -> str:
    """`build_repo_map` 的"永不致命"外壳:任何异常都退化为空骨架,不向上抛。

    任务因为一个可选上下文块崩掉是可靠性的倒退;`max_files`/`dir_depth` 开口让 Settings 的
    `repo_map_max_files`/`repo_map_dir_depth` 真被接线(默认值与 `build_repo_map` 一致,
    不留无人调用的参数)。
    """
    try:
        return build_repo_map(
            workspace, max_chars=max_chars, max_files=max_files, dir_depth=dir_depth
        )
    except Exception:
        log.warning("repo_map: 骨架生成失败,降级为无骨架 workspace=%s", workspace, exc_info=True)
        return ""
