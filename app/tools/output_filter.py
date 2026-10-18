"""模型可见层的输出提纯:traceback 帧过滤与长输出折叠。

设计边界(重要):
- **原始证据一律不动**——junit XML、stdout/stderr、被 `local_runner._truncate` 处理的
  文本都不在这里改;判定层(`failure_signature`、`all_passed`)也一字不改,
  repeat_streak 依赖签名的稳定性,提纯只影响"给模型看什么"。
- 提纯只发生在模型可见层:junit 的 `<failure>` 节点带完整 traceback,此前被丢弃,
  模型只看到 160 字符的签名首行——本模块把它变成"只看项目帧 + 最终异常块"的堆栈。

两种堆栈形态都要认:CPython 的 `File "x.py", line N, in f` 帧,和 pytest 断言重写
风格的位置行 `src/foo.py:12: AssertionError` / `E   assert 0 == 1`。
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# CPython 帧头:  File "/abs/path.py", line 12, in func
_FRAME_RE = re.compile(r'^\s*File "(?P<path>[^"]+)", line \d+(?:, in .*)?$')
# pytest 位置行:src/foo.py:12: AssertionError(缩进版本也认)
_PYTEST_LOC_RE = re.compile(r"^\s*(?P<path>(?:[A-Za-z]:)?[^\s:]+\.(?:py|pyx|pxd)):\d+(?::.*)?\s*$")
# 最终异常块:E 前缀(pytest 断言重写)或"异常名: 消息"
_EXCEPTION_RE = re.compile(r"^(?:E\s|[\w.]+(?:Error|Exception|Warning|Exit|Interrupt)\b|assert\b)")
# 三方/工具链帧:它们只会把模型的注意力引到它改不了的文件上
_NOISE_MARKERS = ("site-packages", "dist-packages", "/_pytest/", "\\_pytest\\", "sitecustomize")

OMITTED = "... ({n} 行已省略) ..."


def _is_noise_path(path: str, project_root: Path) -> bool:
    normalized = path.replace("\\", "/")
    lowered = normalized.lower()
    if any(marker in lowered for marker in _NOISE_MARKERS):
        return True
    if not path.endswith((".py", ".pyx", ".pxd")):
        return True  # C 扩展/未知来源:不属于项目帧
    root = project_root.resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        candidate.resolve().relative_to(root)
    except (OSError, ValueError):
        return True  # 解析后不在项目根内:第三方或越界
    return False


def _classify(text: str, project_root: Path) -> list[tuple[str, bool]]:
    """逐行标注 (行文本, 是否保留)。

    帧头决定其后一段上下文行的去留:非项目帧连同它的源码引用行一起丢,项目帧与
    头部横幅、最终异常块保留。
    """
    kept: list[tuple[str, bool]] = []
    dropping = False
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        frame = _FRAME_RE.match(line) or _PYTEST_LOC_RE.match(line)
        if frame is not None:
            dropping = _is_noise_path(frame.group("path"), project_root)
            kept.append((line, not dropping))
            continue
        if _EXCEPTION_RE.match(line.strip()):
            dropping = False  # 最终异常块永远保留
            kept.append((line, True))
            continue
        if not line.strip():
            kept.append((line, True))
            continue
        # 帧的上下文行(缩进的源码引用、`>`/`E` 之外的行)跟随其所属帧的去留
        kept.append((line, not dropping))
    return kept


def refine_traceback(text: str, project_root: Path, *, max_lines: int = 25) -> str:
    """提纯 traceback:只留项目帧 + 头部 + 最终异常块,并按 max_lines 截断。"""
    if not text or not text.strip():
        return ""
    marked = _classify(text, project_root)
    lines = [line for line, keep in marked if keep]
    # 折叠连续空行(丢帧后常见),最多留一个
    collapsed: list[str] = []
    for line in lines:
        if not line.strip() and collapsed and not collapsed[-1].strip():
            continue
        collapsed.append(line)
    while len(collapsed) > 1 and not collapsed[0].strip():
        collapsed.pop(0)
    while len(collapsed) > 1 and not collapsed[-1].strip():
        collapsed.pop()
    if max_lines and len(collapsed) > max_lines:
        head = collapsed[: max_lines - 1]
        omitted = len(collapsed) - len(head)
        head.append(OMITTED.format(n=omitted))
        collapsed = head
    return "\n".join(collapsed)


def fold_output(text: str, *, head: int, tail: int, hard_cap: int = 8000) -> str:
    """长文本折叠:头 head 行 + 省略标记 + 尾 tail 行;总长度仍受 hard_cap 兜底。"""
    if not text:
        return ""
    lines = text.split("\n")
    if head + tail >= len(lines):
        folded = text
    else:
        omitted = len(lines) - head - tail
        middle = [OMITTED.format(n=omitted)]
        folded = "\n".join(lines[:head] + middle + lines[-tail:] if tail else lines[:head] + middle)
    if hard_cap and len(folded) > hard_cap:
        folded = folded[:hard_cap] + "... (truncated)"
    return folded


# ---------- G05:序列化之前的结构性裁剪 ----------
#
# 为什么不能再沿用 fold_output:`plain_loop` 喂给它的是 `json.dumps` 之后的**一整行**
# (JSON 里源码的换行是 `\n` 转义,不是真空行),于是"头 40 行 + 尾 15 行"立刻退化成
# `folded[:8000] + "... (truncated)"` —— 切在字符串字面量中间、切在最后一个键值对中间,
# 模型收到的是一段**结构损坏**的 JSON(PROGRESS D.5 记的就是这条)。
# 下面这组函数改在**数据结构**上裁:先按行折长字符串,再丢列表尾巴,最后才序列化,
# 所以发出去的消息永远是合法 JSON,且每一处省略都带明确计数。

#: 给 `_bounded` / `evidence` 元数据预留的量,保证"元数据 + 有界视图"仍不超可见上限
VISIBLE_HEADROOM_CHARS = 1200
#: 单个字符串折叠的下限:再低就折不出任何一行有效内容,不如直接丢列表项
LEAF_FLOOR_CHARS = 200
#: 列表尾巴最少要留几条(留 0 会让"这个工具什么都没查到"变成假陈述)
LIST_KEEP_FLOOR = 1
#: 元数据里最多点名几个字段(元数据本身也要有界)
MAX_LISTED_FIELDS = 12

CHAR_OMITTED = "... ({n} 字符已省略) ..."


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _kept_range(total_lines: int, head_n: int, tail_n: int) -> str:
    """保留下来的**原始行号区间**(SPEC 契约 4 的"行范围"):模型不必从省略标记反推自己看到了哪几行。"""
    if tail_n <= 0:
        return f"1-{head_n}"
    return f"1-{head_n}+{total_lines - tail_n + 1}-{total_lines}"


def fold_text(
    text: str, budget: int, *, head_lines: int = 0, tail_lines: int = 0
) -> tuple[str, int, str]:
    """把一段长文本折到 budget 字符以内,返回 (折后文本, 省略的行数, 保留的行范围)。

    首选形状是既有的 `refine_head_lines` / `refine_tail_lines`(头 40 行 + 尾 15 行),
    装不下才按字符预算退让——这两个键本来就管"模型可见层保留头尾几行",换实现不能把
    它们变成零读者开关。尾部优先于头部保留:异常现场与最终结论都在尾巴上,
    只留头部会把"到底为什么失败"折掉(`attach_missing_report_cause` 同口径)。
    没有行可退的单行长文本退到按字符折。
    """
    if budget <= 0 or len(text) <= budget:
        return text, 0, ""
    lines = text.splitlines()
    if len(lines) <= 1:
        marker_reserve = len(CHAR_OMITTED.format(n=len(text)))
        keep = max(0, budget - marker_reserve)
        head = keep // 2
        tail = keep - head
        omitted_chars = len(text) - keep
        if omitted_chars <= 0:
            return text, 0, ""
        folded = text[:head] + CHAR_OMITTED.format(n=omitted_chars) + text[len(text) - tail :]
        return folded, 0, f"chars 1-{head}+{len(text) - tail}-{len(text)}"

    def assemble(head_count: int, tail_count: int, omitted_count: int) -> str:
        parts = [*lines[:head_count], OMITTED.format(n=omitted_count)]
        if tail_count:
            parts.append("\n".join(lines[len(lines) - tail_count :]))
        return "\n".join(parts)

    if head_lines or tail_lines:
        head_n = min(max(0, head_lines), len(lines))
        tail_n = min(max(0, tail_lines), max(0, len(lines) - head_n))
        omitted = len(lines) - head_n - tail_n
        if omitted > 0 and len(assemble(head_n, tail_n, omitted)) <= budget:
            return (
                assemble(head_n, tail_n, omitted),
                omitted,
                _kept_range(len(lines), head_n, tail_n),
            )

    marker_reserve = len(OMITTED.format(n=len(lines)))
    head_cap = max(0, int((budget - marker_reserve) * 0.6))
    acc = 0
    head_n = 0
    for line in lines:
        cost = len(line) + 1
        if acc + cost > head_cap:
            break
        acc += cost
        head_n += 1
    tail_cap = max(0, budget - marker_reserve - acc)
    tail_n = 0
    acc_tail = 0
    for index in range(len(lines) - 1, head_n - 1, -1):
        cost = len(lines[index]) + 1
        if acc_tail + cost > tail_cap:
            break
        acc_tail += cost
        tail_n += 1
    omitted = len(lines) - head_n - tail_n
    if omitted <= 0:
        return text, 0, ""
    folded = assemble(head_n, tail_n, omitted)
    while len(folded) > budget and head_n > 1:
        head_n -= 1
        omitted += 1
        folded = assemble(head_n, tail_n, omitted)
    return folded, omitted, _kept_range(len(lines), head_n, tail_n)


def _string_leaves(value: Any, prefix: str = "") -> list[tuple[Any, Any, str, str]]:
    """(所在容器, 键, 点路径, 文本)——递归到底,嵌套大字段同样在射程内。"""
    out: list[tuple[Any, Any, str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(item, str):
                out.append((value, key, path, item))
            else:
                out.extend(_string_leaves(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            path = f"{prefix}[{index}]"
            if isinstance(item, str):
                out.append((value, index, path, item))
            else:
                out.extend(_string_leaves(item, path))
    return out


def _list_fields(value: Any, prefix: str = "") -> list[tuple[Any, Any, str, list[Any]]]:
    """(所在容器, 键, 点路径, 列表)——阶段 2 的候选:丢尾巴而不是丢整个字段。"""
    out: list[tuple[Any, Any, str, list[Any]]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(item, list):
                out.append((value, key, path, item))
                for sub_index, sub in enumerate(item):
                    out.extend(_list_fields(sub, f"{path}[{sub_index}]"))
            else:
                out.extend(_list_fields(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            out.extend(_list_fields(item, f"{prefix}[{index}]"))
    return out


@dataclass
class BoundReport:
    """一次结构裁剪的读数:裁了多少、从哪儿裁的,全都得能让模型和取证看见。"""

    value: Any
    raw_chars: int
    final_chars: int
    changed: bool
    omitted_lines: int
    dropped_items: int
    folded_fields: list[str] = field(default_factory=list)
    dropped_fields: dict[str, int] = field(default_factory=dict)
    #: 点路径 → **原始行号**区间(单行长文本给字符区间)。没有它,模型只能从"省略了 N 行"
    #: 反推自己看到了哪几行 —— SPEC 契约 4 要求"行范围"是元数据,不是推理题。
    line_ranges: dict[str, str] = field(default_factory=dict)

    def metadata(self, *, visible_max_chars: int) -> dict[str, Any]:
        """模型可见的 `_bounded` 块(有界:字段名最多点名 MAX_LISTED_FIELDS 个)。"""
        folded = list(self.folded_fields[:MAX_LISTED_FIELDS])
        if len(self.folded_fields) > MAX_LISTED_FIELDS:
            folded.append(f"... +{len(self.folded_fields) - MAX_LISTED_FIELDS} more")
        dropped = dict(list(self.dropped_fields.items())[:MAX_LISTED_FIELDS])
        ranges = {key: self.line_ranges[key] for key in list(self.line_ranges)[:MAX_LISTED_FIELDS]}
        return {
            "visible_max_chars": visible_max_chars,
            "raw_chars": self.raw_chars,
            "shown_chars": self.final_chars,
            "omitted_lines": self.omitted_lines,
            "dropped_items": self.dropped_items,
            "folded_fields": folded,
            "dropped_lists": dropped,
            "line_ranges": ranges,
        }


def bound_structure(
    value: Any,
    *,
    max_chars: int,
    head_lines: int = 0,
    tail_lines: int = 0,
) -> BoundReport:
    """把工具 payload 裁进 max_chars,**同时保持 JSON 合法**。

    顺序有讲究:先折长字符串(信息密度最高的地方),再丢列表尾巴(整条整条地丢)。
    反过来做会让一条 400 行的 read_file 回执把 50 条检索命中全挤掉。
    `max_chars <= 0` = 不裁(与旧实现里 hard_cap=0 的口径一致)。
    """
    raw = _dumps(value)
    if max_chars <= 0 or len(raw) <= max_chars:
        return BoundReport(
            value=value,
            raw_chars=len(raw),
            final_chars=len(raw),
            changed=False,
            omitted_lines=0,
            dropped_items=0,
        )
    target = max(256, max_chars - VISIBLE_HEADROOM_CHARS)
    work = copy.deepcopy(value)
    omitted_lines = 0
    dropped_items = 0
    folded_fields: list[str] = []
    dropped_fields: dict[str, int] = {}
    line_ranges: dict[str, str] = {}

    # 首轮预算 = 整条消息可用的量:这样"一个主导长字段"能按 refine_head/tail_lines 的形状
    # 保留(40+15 行),而不是被一个拍出来的均分系数压到 2k 字符。多个长字段时首轮装不下,
    # 每轮减半再折,收敛到能装为止(上限 6 轮,不会无限折)。
    budget = max(LEAF_FLOOR_CHARS, target)
    for _ in range(6):
        if len(_dumps(work)) <= target:
            break
        leaves = [
            leaf for leaf in _string_leaves(work) if len(leaf[3]) > min(budget, LEAF_FLOOR_CHARS)
        ]
        if not leaves:
            break
        for container, key, path, text in sorted(leaves, key=lambda leaf: -len(leaf[3])):
            folded, cut, kept = fold_text(
                text, budget, head_lines=head_lines, tail_lines=tail_lines
            )
            if not cut and len(folded) >= len(text):
                continue
            container[key] = folded
            omitted_lines += cut
            if path not in folded_fields:
                folded_fields.append(path)
                if kept:
                    line_ranges[path] = kept
            elif kept and path in line_ranges and line_ranges[path] != kept:
                # 同一个字段被折第二次:第二次的行号是相对第一次折后的文本,不能冒充原始行号
                line_ranges[path] = f"{line_ranges[path]} (再次折叠,行号非原始)"
        budget = max(LEAF_FLOOR_CHARS // 2, budget // 2)

    while len(_dumps(work)) > target:
        candidates = [item for item in _list_fields(work) if len(item[3]) > LIST_KEEP_FLOOR]
        if not candidates:
            break
        _container, _key, path, items = max(candidates, key=lambda item: len(item[3]))
        cut = max(1, len(items) // 4)
        del items[len(items) - cut :]
        dropped_items += cut
        dropped_fields[path] = dropped_fields.get(path, 0) + cut

    final = _dumps(work)
    return BoundReport(
        value=work,
        raw_chars=len(raw),
        final_chars=len(final),
        changed=True,
        omitted_lines=omitted_lines,
        dropped_items=dropped_items,
        folded_fields=folded_fields,
        dropped_fields=dropped_fields,
        line_ranges=line_ranges,
    )
