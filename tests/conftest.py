"""共享 fixture:物化演示仓库(带 git 历史)到临时目录。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.gitops.testing import materialize_repo

FIXTURES = Path(__file__).parent / "fixtures"

# fixtures/ 下的模板仓库自带"基线故意失败"的测试,不属于主测试套件
collect_ignore = ["fixtures"]


@pytest.fixture(scope="session")
def demo_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """带 2 个 commit 的演示仓库;基线中 test_empty_string_returns_none 故意失败。"""
    dest = tmp_path_factory.mktemp("demo") / "demo_repo"
    materialize_repo(FIXTURES / "demo_repo", dest)
    return dest
