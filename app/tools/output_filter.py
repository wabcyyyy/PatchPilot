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

import re
from pathlib import Path

# CPython 帧头:  File "/abs/path.py", line 12, in func
_FRAME_RE = re.compile(r'^\s*File "(?P<path>[^"]+)", line \d+(?:, in .*)?$')
# pytest 位置行:src/foo.py:12: AssertionError(缩进版本也认)
_PYTEST_LOC_RE = re.compile(r"^\s*(?P<path>[^\s:]+\.(?:py|pyx|pxd)):\d+(?::.*)?\s*$")
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
