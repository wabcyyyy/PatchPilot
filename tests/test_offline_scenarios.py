"""G16-C5:离线场景集索引防腐——`offline-scenarios.md` 点名的每个测试必须真实存在。

SPEC G16 要求离线集覆盖十三类场景;场景与测试入口的对齐表在
`docs/evidence/agent-upgrade/offline-scenarios.md`。清单最大的失效模式是
"文档里有、套件里没有"(改名/删用例后表格静默烂掉),所以索引本身要有测试钉:
每个 `tests/...::test_x` 引用都必须是真实文件里的真实用例。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCENARIO_DOC = REPO_ROOT / "docs" / "evidence" / "agent-upgrade" / "offline-scenarios.md"

# 只认 tests/ 下的 node id(文件::用例),避免把 demo 路径/普通反引号误当测试
_NODE_ID = re.compile(r"`(tests/[A-Za-z0-9_]+\.py)::([A-Za-z0-9_]+)`")


def test_scenario_index_entries_all_exist() -> None:
    text = SCENARIO_DOC.read_text(encoding="utf-8")
    pairs = _NODE_ID.findall(text)
    assert len(pairs) >= 15, f"场景索引的测试引用过少({len(pairs)}),表格可能被清空"

    missing: list[str] = []
    for file_rel, func in pairs:
        path = REPO_ROOT / file_rel
        if not path.is_file():
            missing.append(f"{file_rel} (文件不存在)")
            continue
        if f"def {func}(" not in path.read_text(encoding="utf-8"):
            missing.append(f"{file_rel}::{func} (用例不存在)")
    assert not missing, "场景索引引用了不存在的测试入口:" + "; ".join(missing)


def test_scenario_index_covers_thirteen_declared_scenarios() -> None:
    """SPEC 点名的十三类场景,每一类都在索引表格里有一行。"""
    text = SCENARIO_DOC.read_text(encoding="utf-8")
    table_rows = [
        line
        for line in text.splitlines()
        if line.startswith("|") and not line.startswith("|--") and "---" not in line
    ]
    body = [row for row in table_rows if not row.startswith("| #")]
    declared = [
        "单文件",
        "跨文件",
        "长输出",
        "错误假设",
        "无补丁",
        "身份错配",
        "门禁拒绝",
        "环境失败",
        "预算停止",
        "输入等待",
        "取消",
        "恢复",
        "落选分支",
    ]
    for scenario in declared:
        assert any(scenario in row for row in body), f"场景[{scenario}]不在索引表格中"
