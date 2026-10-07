"""Python AST 大纲的共享层:仓库骨架(repo_map)与符号工具(tools/symbols)同一套口径。

M3 之前这些助手长在 `app/context/repo_map.py` 里。新增 `describe_file` / `find_symbol`
要复用同一条"签名重建 + 行区间 + 类嵌套深度"规则,复制一份必然漂移(同一文件在骨架里
叫 `def f(a, b=1)`、在工具里叫 `def f(a, b=1)` 的可能性极低),故集中到这里:
骨架负责**渲染文本**,工具负责**结构化条目**,两者都从这一个实现取数。

红线:本文件的渲染助手必须与迁移前逐字一致——`tests/test_repo_map.py` 钉着真实行号
与渲染格式(12 个用例)。改名只发生在"私有前缀"层面(`_params` → `build_params`),
表达式本体一字未动。
"""

from __future__ import annotations

import ast
from typing import Any

NESTED_CLASS_DEPTH = 1  # 顶层类内再嵌一层类为限;函数体内的局部函数一律不出
SYMBOL_KINDS: tuple[str, ...] = (
    "class",
    "function",
    "method",
)  # 工具 schema 与 kind 过滤的唯一出处


# ---------- 渲染助手:repo_map 的骨架文本用(与迁移前逐字等价) ----------


def line_range(node: ast.AST) -> str:
    end = getattr(node, "end_lineno", None) or node.lineno
    return f"L{node.lineno}-L{end}"


def decorator_prefix(node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    return "".join(f"@{ast.unparse(dec)} " for dec in node.decorator_list)


def build_params(args: ast.arguments) -> str:
    """ast.arguments → 紧凑签名:posonly `/`、默认值、*args、bare `*`、kwonly、**kwargs。"""
    positional = [a.arg for a in args.posonlyargs] + [a.arg for a in args.args]
    first_default = len(positional) - len(args.defaults)
    parts: list[str] = []
    for index, name in enumerate(positional):
        slot = index - first_default
        parts.append(name if slot < 0 else f"{name}={ast.unparse(args.defaults[slot])}")
    if args.posonlyargs:
        parts.insert(len(args.posonlyargs), "/")
    if args.vararg:
        parts.append(f"*{args.vararg.arg}")
    elif args.kwonlyargs:
        parts.append("*")  # 无 *args 时的裸 * 决定 kwonly 边界,不能省
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        parts.append(arg.arg if default is None else f"{arg.arg}={ast.unparse(default)}")
    if args.kwarg:
        parts.append(f"**{args.kwarg.arg}")
    return ", ".join(parts)


def signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    keyword = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = "" if node.returns is None else f" -> {ast.unparse(node.returns)}"
    return f"{decorator_prefix(node)}{keyword} {node.name}({build_params(node.args)}){returns}"


def class_header(node: ast.ClassDef) -> str:
    """类条目的签名口径:与骨架里 `class Outer (L9-L19)` 的头部同一条表达式(去掉行区间)。"""
    return f"{decorator_prefix(node)}class {node.name}"


def render_symbols(body: list[ast.stmt], indent: int, depth: int) -> list[str]:
    """模块级与类内符号。类嵌套最多一层;函数体整体不进(局部函数不是仓库结构)。"""
    out: list[str] = []
    for child in body:
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            out.append(f"{' ' * indent}{signature(child)} ({line_range(child)})")
        elif isinstance(child, ast.ClassDef) and depth <= NESTED_CLASS_DEPTH:
            header = f"{class_header(child)} ({line_range(child)})"
            out.append(f"{' ' * indent}{header}")
            out.extend(render_symbols(child.body, indent + 2, depth + 1))
    return out


# ---------- 结构化助手:describe_file / find_symbol 用 ----------


def parse_source(text: str, filename: str) -> tuple[ast.Module | None, str | None]:
    """`ast.parse` 的安全外壳:返回 (tree, error)。

    异常必须是**可用的结果而不是一次崩溃**:半截文件(SyntaxError)、超深嵌套
    (RecursionError)、非 UTF-8 与编码怪癖(ValueError/OSError)都收敛成错误串,
    让工具把"这个文件解析不了"如实告诉模型。
    """
    try:
        return ast.parse(text, filename=filename), None
    except Exception as exc:  # 解析层的一切异常都是"该文件不可用",不是任务级失败
        return None, f"{type(exc).__name__}: {exc}"


def _entry(
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef, kind: str, sig: str
) -> dict[str, Any]:
    end = getattr(node, "end_lineno", None) or node.lineno
    return {
        "name": node.name,
        "kind": kind,
        "start_line": node.lineno,
        "end_line": end,
        "signature": sig,
    }


def symbol_entries(tree: ast.Module) -> list[dict[str, Any]]:
    """顶层 class/def + 类内方法的结构化大纲:name/kind/start_line/end_line/signature。

    遍历深度与 `render_symbols` 同一条规则(类嵌套最多再进一层、函数体整体不进),
    所以同一个文件在骨架里看到的符号集合,与 describe_file/find_symbol 给出的完全一致。
    `kind`:模块级函数 = `function`,类内 = `method`,类 = `class`。
    """
    out: list[dict[str, Any]] = []
    _collect(tree.body, out, 0)
    return out


def _collect(body: list[ast.stmt], out: list[dict[str, Any]], depth: int) -> None:
    for child in body:
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            out.append(_entry(child, "function" if depth == 0 else "method", signature(child)))
        elif isinstance(child, ast.ClassDef) and depth <= NESTED_CLASS_DEPTH:
            out.append(_entry(child, "class", class_header(child)))
            _collect(child.body, out, depth + 1)
