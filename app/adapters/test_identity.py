"""测试身份的共享解析与匹配(S01/F3)。

缺陷现场(PatchPilot-review.md F3,P0):JUnit 匹配只对 file+方法名,请求
`tests/test_x.py::TestA::test_same` 时只有 `TestB::test_same` 通过也算 all_passed——
错误测试身份被验成通过。修法是把"请求的 node id"解析成结构化身份
(文件 + 完整类链 + 函数 + 参数化 ID),JUnit 的 testcase 三元组逐段核对:
类链缺失、同名不同类、模块函数与类方法互相顶替、参数化 ID 不精确一致,一律不匹配。

刻意边界(ADR-0009 §4 / spec S01):
- 第一版只验收**具体函数/方法** node id(file::func 或 file::Class[::SubClass]::func);
  文件/目录/仅类 selector 在预检(validate_concrete_node_id)明确拒绝,不以
  "跑过一个 testcase"证明整个 selector 完成;
- 参数化 ID 是**数据**:括号内允许空格/逗号/引号等数据字符,但不允许 shell 元字符
  (app/executor/whitelist.py 的拦截集)与控制字符——argv 单元素 + shell=False 下
  这些字符没有合法语义,只有注入语义;
- 本模块不做 I/O、不 import pytest,解析失败返回 None / 校验失败抛 ValueError,
  由调用方(预检层转 InvalidRequestError,匹配层视为不匹配)决定处置。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 参数化段的拒绝字符:与 whitelist.SHELL_METACHARS 同源语义(argv 单元素下只有注入
# 语义),另拒反斜杠(Windows 路径混淆)与控制字符。方括号/圆括号/逗号/引号/等号
# 是 pytest 参数化 ID 的合法数据字符,放行。
_PARAM_FORBIDDEN = set(";|&`><$\\\n\r\t\x00")
_PARAM_RE = re.compile(r"^[\x20-\x7e]+$")
# 类链段与函数段:Python 标识符;函数必须 test* 开头(pytest 收集约定),
# 类链段必须 Test* 开头——中间段只可能是类,不是类的写法本来就是无效 node id
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# 文件段:沿用既有预检的字符域(不含反斜杠——请求 id 一律 POSIX 相对路径)
_FILE_RE = re.compile(r"^[A-Za-z0-9_./\-]+$")


@dataclass(frozen=True)
class TestIdentity:
    """一条具体测试的结构化身份。params 为 None 表示非参数化;否则含括号内原文。"""

    file_path: str
    class_chain: tuple[str, ...]
    function: str
    params: str | None

    @property
    def case_name(self) -> str:
        """JUnit testcase @name 的期望形态(pytest 按 function[params] 渲染)。"""
        return f"{self.function}[{self.params}]" if self.params is not None else self.function

    @property
    def classname_tail(self) -> str:
        """JUnit @classname 的期望尾部:点分模块路径(+类链),相对 rootdir。"""
        module = self.file_path.removesuffix(".py").replace("/", ".")
        if self.class_chain:
            return f"{module}.{'.'.join(self.class_chain)}"
        return module


def parse_node_id(node_id: str) -> TestIdentity | None:
    """解析具体测试 node id;非具体/不合法返回 None(匹配层语义:永不匹配)。

    解析规则:
    - 先摘参数化段:结尾 `]` 且存在与之配对的 `[` 时,最后一个未配对的 `[` 之后
      到结尾是参数段——参数字符串内部的 `::` 与 `.` 不会被当文件/类边界;
    - 其余按 `::` 切:首段文件、末段函数、中间段完整类链;
    - 文件段非空、函数段 test* 开头、类链段 Test* 开头,逐段过标识符/路径字符域。
    """
    if not node_id or node_id != node_id.strip():
        return None
    head, params = node_id, None
    if node_id.endswith("]"):
        depth = 0
        for index in range(len(node_id) - 2, -1, -1):
            ch = node_id[index]
            if ch == "]":
                depth += 1
            elif ch == "[":
                if depth == 0:
                    head, params = node_id[:index], node_id[index + 1 : -1]
                    break
                depth -= 1
        if params is None or not params:
            return None
        if not _PARAM_RE.fullmatch(params) or any(ch in _PARAM_FORBIDDEN for ch in params):
            return None
    parts = head.split("::")
    if len(parts) < 2:
        return None
    file_part, *chain, function = parts
    if not file_part or any(not seg for seg in chain) or not function:
        return None
    if not _FILE_RE.fullmatch(file_part) or not _IDENT_RE.fullmatch(function):
        return None
    if not function.startswith("test"):
        return None
    for seg in chain:
        if not _IDENT_RE.fullmatch(seg) or not seg.startswith("Test"):
            return None
    norm = file_part.replace("\\", "/")
    if (
        ".." in norm.split("/")
        or norm.startswith(("/", "\\"))
        or "//" in norm
        or re.fullmatch(r"[A-Za-z]:.*", norm)
    ):
        return None
    return TestIdentity(file_path=norm, class_chain=tuple(chain), function=function, params=params)


def validate_concrete_node_id(node_id: str) -> None:
    """预检:必须是具体函数/方法 node id,否则 ValueError(消息即拒绝原因)。

    检查顺序:先路径逃逸(带专属消息,便于调用方定位),再结构(具体性)。
    文件/目录/仅类 selector 不再被静默接受——一个 testcase 通过证明不了整个
    selector 完成(spec S01:未展开为冻结的具体 node ids 就明确拒绝)。
    """
    file_part = node_id.split("::", 1)[0].replace("\\", "/")
    if ".." in file_part.split("/") or file_part.startswith("/") or "//" in file_part:
        raise ValueError(f"test id {node_id!r} escapes the workspace (path traversal guard)")
    if re.fullmatch(r"[A-Za-z]:.*", file_part):
        raise ValueError(f"test id {node_id!r} must be workspace-relative")
    if "::" not in node_id:
        raise ValueError(
            f"test id {node_id!r} is not a concrete node id"
            " (file/dir selectors must be expanded to file::func ids)"
        )
    parsed = parse_node_id(node_id)
    if parsed is not None:
        return
    parts = [p for p in node_id.split("::") if p]
    if len(parts) >= 2 and not parts[-1].startswith("test"):
        raise ValueError(f"test id {node_id!r} selects a class/file, not a test function")
    raise ValueError(f"test id {node_id!r} is not a valid concrete pytest node id")


def case_matches(identity: TestIdentity, file_attr: str, classname: str, case_name: str) -> bool:
    """JUnit testcase 三元组是否就是这条身份(F3 的修复点)。

    - @name 必须与 function[params] 精确一致(参数化是身份的一部分);
    - 有 @file 时:junit file == 请求文件或以其为后缀(POSIX 归一),**且** @classname
      以"点分模块路径+完整类链"结尾——类链缺一段或同名不同类都不匹配;
    - 无 @file(rootdir 兼容路径):只按 classname 尾部核对,同样要求完整类链,
      不得以"同后缀方法名"匹配不同类。
    """
    if case_name != identity.case_name:
        return False
    expected_tail = identity.classname_tail
    file_n = (file_attr or "").replace("\\", "/")
    if file_n:
        req = identity.file_path
        if file_n != req and not file_n.endswith("/" + req):
            return False
    if not classname:
        return not identity.class_chain and bool(file_n)
    return classname == expected_tail or classname.endswith("." + expected_tail)
