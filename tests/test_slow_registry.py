"""慢用例登记表自检:每个登记项都必须指向真实存在的用例文件/函数。

登记表只决定"快档跳过谁";登记项一旦指向已删除或改名的用例,对应的慢用例会
悄悄回到快档(变慢),或者登记表里留着死条目误导人——两种都在这里当场报出来。
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.conftest import load_slow_registry

REPO_ROOT = Path(__file__).resolve().parents[1]


def _test_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test")
    }


def test_registry_entries_point_at_existing_tests() -> None:
    entries = load_slow_registry()
    assert entries, "登记表为空:快档会把所有端到端用例都跑一遍"
    dangling: list[str] = []
    for entry in sorted(entries):
        file_part, _, func = entry.partition("::")
        path = REPO_ROOT / file_part
        if not path.is_file() or (func and func not in _test_functions(path)):
            dangling.append(entry)
    assert not dangling, f"登记表里有指向不存在用例的条目:{dangling}"


def test_stress_cases_are_also_slow() -> None:
    """stress 档用例必须同时登记为 slow,否则会混进快档。"""
    registry = load_slow_registry()
    stress = [
        f"tests/{path.name}::{node.name}"
        for path in sorted((REPO_ROOT / "tests").glob("test_*.py"))
        for node in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(node, ast.FunctionDef)
        and any(ast.unparse(d) == "pytest.mark.stress" for d in node.decorator_list)
    ]
    assert stress, "没找到任何 stress 用例:标记被删了?"
    missing = [s for s in stress if s not in registry and s.split("::")[0] not in registry]
    assert not missing, f"stress 用例没有登记为 slow:{missing}"
