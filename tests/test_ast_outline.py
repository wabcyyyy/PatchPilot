"""`app/context/ast_outline.py` 的直接用例:骨架与符号工具必须共用同一套口径。

这个模块存在的理由就是"复制一份必然漂移"(模块 docstring 语),而它的渲染侧此前只被
`test_repo_map.py` 间接覆盖、结构化侧只被 `test_search_tools.py` 经工具间接覆盖 ——
**"两处集合必须一致"这条不变量本身没有任何用例钉着**。这里补的正是这条与解析层的收敛行为。
"""

from __future__ import annotations

import ast

from app.context.ast_outline import (
    NESTED_CLASS_DEPTH,
    build_params,
    parse_source,
    render_symbols,
    signature,
    symbol_entries,
)

SOURCE = '''
"""模块文档。"""
import os


@dataclass
class Outer:
    """顶层类。"""

    def method(self, a, b=2, /, *args, key=None, **kw) -> int:
        def helper():  # 局部函数不是仓库结构,两边都不该出现
            return 1

        return helper()

    class Inner:
        def deep(self):
            pass

    class InnerInner:
        class TooDeep:
            pass


def top(x: str) -> bool:
    return bool(x)


async def fetch(session) -> bytes:
    return b""
'''


def _tree() -> ast.Module:
    tree, error = parse_source(SOURCE, "sample.py")
    assert error is None, error
    assert tree is not None
    return tree


def test_names_and_order_are_identical_between_skeleton_and_tools() -> None:
    """同一份源码:骨架渲染出的符号集合与工具给出的结构化集合**逐项相等**(含顺序)。"""
    tree = _tree()
    rendered = [line.strip() for line in render_symbols(tree.body, 0, 0)]
    entries = symbol_entries(tree)
    assert len(entries) == len(rendered), (entries, rendered)
    for entry, line in zip(entries, rendered, strict=True):
        assert line.startswith(entry["signature"]), line
        assert line.endswith(f"(L{entry['start_line']}-L{entry['end_line']})"), line


def test_local_functions_are_absent_from_both_surfaces() -> None:
    """函数体内的局部函数一律不出 —— 这是"仓库结构"与"实现细节"的分界。"""
    names = {entry["name"] for entry in symbol_entries(_tree())}
    assert "helper" not in names
    rendered = "\n".join(render_symbols(_tree().body, 0, 0))
    assert "helper" not in rendered


def test_class_nesting_is_capped_at_one_extra_level() -> None:
    """类嵌套只再进一层(NESTED_CLASS_DEPTH=1):Inner 在内,InnerInner 的类体内一切在外。"""
    assert NESTED_CLASS_DEPTH == 1
    entries = symbol_entries(_tree())
    names = [entry["name"] for entry in entries]
    assert "Inner" in names and "deep" in names
    assert "InnerInner" in names  # 第一层嵌套的类本身仍然可见
    assert "TooDeep" not in names, "第二层嵌套里的类不再展开"


def test_kinds_distinguish_module_level_from_methods() -> None:
    kinds = {entry["name"]: entry["kind"] for entry in symbol_entries(_tree())}
    assert kinds["top"] == "function" and kinds["fetch"] == "function"
    assert kinds["Outer"] == "class" and kinds["method"] == "method"
    assert set(kinds.values()) <= {"class", "function", "method"}


def test_signatures_keep_decorators_async_and_return_annotation() -> None:
    entries = {entry["name"]: entry["signature"] for entry in symbol_entries(_tree())}
    assert entries["Outer"].startswith("@dataclass class Outer"), entries["Outer"]
    assert entries["fetch"].startswith("async def fetch(session)"), entries["fetch"]
    assert entries["top"] == "def top(x) -> bool", entries["top"]


def test_parameter_annotations_are_deliberately_dropped_from_outlines() -> None:
    """签名保名字/默认值/形参类别(posonly、kwonly、*、**),**不**保参数类型标注。

    这是移植前就定下的口径(`tests/test_repo_map.py` 钉着渲染格式),取舍是骨架要替仓库规模
    付token:返回值标注留着(它是"这函数产出什么"的最短信息),参数标注丢了(模型要看得更细
    就 read_file)。改它等于改模型每次请求看到的东西,不当作 bug 顺手改。
    """
    tree = _tree()
    outer = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    method = next(node for node in outer.body if isinstance(node, ast.FunctionDef))
    annotated = ast.get_source_segment(SOURCE, method)
    assert "b=2" in signature(method) and "key=None" in signature(method)
    assert "-> int" in signature(method)
    assert annotated is not None and "self, a, b=2" in annotated


def test_build_params_covers_posonly_bare_star_and_defaults() -> None:
    """posonly `/`、默认值、*args 与 **kwargs 都要留在签名里(行的位置不是)。"""
    tree = _tree()
    outer = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    method = next(node for node in outer.body if isinstance(node, ast.FunctionDef))
    assert build_params(method.args) == "self, a, b=2, /, *args, key=None, **kw"
    assert signature(method).startswith("def method(self, a, b=2, /, *args, key=None, **kw) -> int")


def test_parse_source_converts_syntax_errors_into_a_usable_result() -> None:
    """半截文件必须是**可用结果**而不是一次崩溃:工具要能如实告诉模型"这文件解析不了"。"""
    tree, error = parse_source("def broken(:\n    pass\n", "broken.py")
    assert tree is None and error is not None
    assert error.startswith("SyntaxError: ")


def test_parse_source_never_raises_on_pathological_inputs() -> None:
    """契约是"永不抛出":树为空当且仅当有错误串。

    刻意不钉具体异常名 —— 400 层括号在 3.11 与 3.12 分别是 RecursionError 与 SyntaxError,
    把解释器内部当成断言对象,用例就会随 Python 版本红,而我们要防的是"异常漏出去"。
    """
    cases = {
        "半截文件": "def broken(:\n    pass\n",
        "空字节": "x = 1\x00\n",
        "括号深渊": "x = " + "(" * 400 + "1" + ")" * 400 + "\n",
        "缩进与语法混排": "if True:\n\treturn\n",
        "只有断句": "...\n",
    }
    for name, text in cases.items():
        tree, error = parse_source(text, f"{name}.py")
        assert (tree is None) == (error is not None), (name, tree, error)
        if tree is None:
            assert error is not None and error.split(":")[0], (name, error)


def test_empty_module_yields_no_symbols_rather_than_noise() -> None:
    tree, error = parse_source('"""只有文档字符串。"""\n', "empty.py")
    assert error is None and tree is not None
    assert symbol_entries(tree) == [] and render_symbols(tree.body, 0, 0) == []
