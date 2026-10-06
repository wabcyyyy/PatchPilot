"""共享 fixture:物化演示仓库(带 git 历史)到临时目录。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.gitops.blockpatch import unified_to_block
from app.gitops.testing import materialize_repo

FIXTURES = Path(__file__).parent / "fixtures"


def block(diff_text: str) -> str:
    """把测试里手写的 unified diff 外包成 apply_patch 块协议文本。

    工具层只认块格式(单入口),但测试想表达的仍是"一个普通补丁"——
    原 diff 文本一字不动,只转换;这样用例的可读性与可核对性都保留。
    """
    return unified_to_block(diff_text)


# fixtures/ 下的模板仓库自带"基线故意失败"的测试,不属于主测试套件
collect_ignore = ["fixtures"]


@pytest.fixture(scope="session")
def demo_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """带 2 个 commit 的演示仓库;基线中 test_empty_string_returns_none 故意失败。"""
    dest = tmp_path_factory.mktemp("demo") / "demo_repo"
    materialize_repo(FIXTURES / "demo_repo", dest)
    return dest


@pytest.fixture(autouse=True)
def _fresh_settings():
    """每个用例前后清 Settings 缓存。

    Settings 是全局 lru_cache,而测试会用 monkeypatch.setenv 改配置、且任务在
    后台线程跑完全程——线程可能在"用例内 finally 清缓存之后、monkeypatch 撤销
    环境变量之前"的窗口用带配置的 environ 重建缓存,污染后续用例(实际踩过:
    repo 根白名单测试泄漏,后续任务被 422)。setup/teardown 各清一次,确定性封死。
    """
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
