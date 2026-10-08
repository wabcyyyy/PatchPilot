"""共享 fixture:物化演示仓库(带 git 历史)到临时目录。"""

from __future__ import annotations

import os
from collections.abc import Iterator
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


@pytest.fixture(scope="session", autouse=True)
def _basetemp_outside_repo(tmp_path_factory: pytest.TempPathFactory) -> None:
    """basetemp 不许落在仓库之内(CI 与本地都靠它当场报错,而不是悄悄假通过)。

    成因见 PROGRESS 的 M10 卡:临时目录的父链上有本仓库的 `.git`,
    `git -C <临时目录> add -A -N` 就能沿父链找到外层仓库、又因忽略规则加不到任何文件,
    于是"工作区其实不是 git 仓库"这类夹具缺陷既不报错、diff 也恒为空——用例只在
    特定 basetemp 位置下才绿。默认 basetemp(系统临时目录)本来就在仓库外。
    """
    root = tmp_path_factory.getbasetemp().resolve()
    repo_root = Path(__file__).resolve().parents[1]
    assert repo_root not in (root, *root.parents), (
        f"basetemp 在仓库内:{root} —— 别传 --basetemp=<仓库内路径>,用默认值即可"
    )


@pytest.fixture(scope="session", autouse=True)
def _isolated_nested_temp(
    tmp_path_factory: pytest.TempPathFactory, _basetemp_outside_repo: None
) -> Iterator[None]:
    """把 TEMP/TMP/TMPDIR 指到本次会话私有目录,让**嵌套 pytest** 不再共用机器默认根。

    被诊断仓库的测试不带 `--basetemp`(平台侧 `build_pytest_command` 没设,见 TODO M11),
    它们的 `tmp_path` 因此全落在 `<机器临时>/pytest-of-<user>/pytest-N/` 这一个共享根下,
    而 pytest 只保留最近 3 个编号目录。套件里有并发路径(候选并行、双跑复核、上一用例
    留下的后台线程),别人的清理动作会让本次 `os.scandir` 撞
    `PermissionError [WinError 5]` —— 本夜连续两轮同一处 `test_bugset[BUG-015]` 因此红,
    与外层 `--basetemp` 是否互不相同无关。

    执行器的 env 白名单(`app/executor/local_runner.py` 的 `_ENV_ALLOWLIST`)本来就透传
    TEMP/TMP/TMPDIR,所以在测试侧改这三个变量即可隔离,不必动生产代码;
    生产侧的同类缺口(每次执行应有私有临时根)仍按 M11.2 等裁决。
    """
    root = tmp_path_factory.mktemp("nested-temp")
    saved = {key: os.environ.get(key) for key in ("TEMP", "TMP", "TMPDIR")}
    for key in ("TEMP", "TMP", "TMPDIR"):
        os.environ[key] = str(root)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
