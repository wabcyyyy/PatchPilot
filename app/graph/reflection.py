"""失败反思的工作记忆:把"上一轮已经试过并被回滚的改动"压成可读摘要(M4)。

缺陷出处(PROGRESS.md D.3/D.9 与本轮复核):verify 失败后 `rollback` 会把工作区硬复位到基线
(`app/graph/nodes.py` 的 rollback 节点),下一轮 PROPOSE 是**全新会话**,只拿到失败用例的
签名与堆栈——"我上一轮到底改了什么"这个信息随着工作区一起消失了。于是模型很容易把同一个
不成立的假设再写一遍:`repeat_streak`(同一组失败连续多轮完全一致)就是这个循环原地打转的
实测信号,而现有处置只是加一句"请换思路"的文字提示,没有任何"上一轮改了什么"的事实。

本模块只做一件事:**从回滚前保真的 diff 里取出形状信息**(哪些文件、各加了/删了几行、
第一个 hunk 的上下文),不给模型补丁正文。正文会重新诱导模型逐字重放上一版;
形状信息才是"这条路过不通"的证据。零请求、零副作用、纯文本函数。
"""

from __future__ import annotations

_MAX_FILES = 8
_MAX_CHARS = 1200
_MAX_HUNK_CHARS = 72

_TITLE = "上一轮补丁已回滚(工作区已复位到基线)。它当时改了这些:"
_DISCLAIMER = (
    "以上是**已经试过且不成立**的改动形状,不是要你重放的补丁;"
    "下一轮必须换一个不同的假设(不同的文件、不同的分支条件或不同的前置修复)。"
)


def _file_path(block: list[str]) -> str:
    """一个文件块的目标路径:`+++ b/<path>` 优先,退回 `diff --git a/X b/X` 的 b 侧。"""
    for line in block:
        if line.startswith("+++ b/"):
            return line[len("+++ b/") :].strip()
    first = block[0] if block else ""
    if first.startswith("diff --git "):
        tail = first[len("diff --git ") :]
        if " b/" in tail:
            return tail.split(" b/", 1)[1].strip()
    return ""


def _counts(block: list[str]) -> tuple[int, int]:
    added = sum(1 for line in block if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in block if line.startswith("-") and not line.startswith("---"))
    return added, removed


def _first_hunk(block: list[str]) -> str:
    for line in block:
        if line.startswith("@@"):
            return line[:_MAX_HUNK_CHARS]
    return ""


def diff_digest(diff_text: str, *, max_files: int = _MAX_FILES, max_chars: int = _MAX_CHARS) -> str:
    """统一 diff → 反思摘要;空 diff 或解析不出文件时返回空串(调用方据此不追加)。

    确定性:文件按 diff 里的出现顺序列出(不做排序,保留"模型先改哪个"的意图顺序)。
    长度上限按整行裁:宁可少列几个文件,也不把一行截成半句。
    """
    if not diff_text.strip() or max_chars <= 0:
        return ""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if current:
                blocks.append(current)
            current = [line]
        elif current is not None:
            current.append(line)
    if current:
        blocks.append(current)

    rows: list[str] = []
    for block in blocks:
        path = _file_path(block)
        if not path:
            continue
        added, removed = _counts(block)
        hunk = _first_hunk(block)
        rows.append(f"- {path}  +{added} -{removed}{f'  {hunk}' if hunk else ''}")
    if not rows:
        return ""

    lines = [_TITLE, *rows[:max_files]]
    if len(rows) > max_files:
        lines.append(f"… 另有 {len(rows) - max_files} 个文件未列出")
    lines.append(_DISCLAIMER)

    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > max_chars:
            break
        kept.append(line)
        used += len(line) + 1
    if len(kept) < 2:  # 连"标题 + 一条文件"都放不下:这段反思就没有意义
        return ""
    return "\n".join(kept)


def with_discarded_patch(feedback: str, diff_text: str) -> str:
    """把反思摘要追加到既有反馈末尾;摘要为空时**逐字返回原文**。

    逐字返回是刻意的:没有回滚过、或回滚的 diff 为空时,反馈必须与今天完全一致,
    不能凭空多出一段"你上一轮改了什么"的噪声(也是本改动的回归钉子)。
    """
    digest = diff_digest(diff_text)
    if not digest:
        return feedback
    if not feedback.strip():
        return digest
    return f"{feedback}\n\n{digest}"
