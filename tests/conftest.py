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
    特定 basetemp 位置下才绿。系统临时目录本来就在仓库外,但**本机的
    `%TEMP%\\pytest-of-*` ACL 是坏的(PM-004,至今未修)**,所以本地跑要显式传
    仓库外且可写的落点:`pytest -q --basetemp=D:/tmp/pt`(pyproject 里不钉落点,
    否则就等于把"仓库内"这个坏姿势固化进共享配置)。
    """
    try:
        root = tmp_path_factory.getbasetemp().resolve()
    except OSError as exc:  # 落点不可用时要说人话,别让 WinError 5 单独站在那里
        raise OSError(
            f"pytest 临时根不可用:{exc} —— 传一个仓库外且可写的落点,"
            f"例如 pytest -q --basetemp=D:/tmp/pt"
        ) from exc
    repo_root = Path(__file__).resolve().parents[1]
    assert repo_root not in (root, *root.parents), (
        f"basetemp 在仓库内:{root} —— 落点必须在仓库外且可写,"
        f"本地用 pytest -q --basetemp=D:/tmp/pt(CI 的系统临时目录本来就在仓库外)"
    )


@pytest.fixture(scope="session", autouse=True)
def _isolated_nested_temp(
    tmp_path_factory: pytest.TempPathFactory, _basetemp_outside_repo: None
) -> Iterator[None]:
    """把 TEMP/TMP/TMPDIR 指到本次会话私有目录,让**嵌套 pytest** 不再共用机器默认根。

    平台侧 `run_pytest` 是给被诊断执行显式追加 `--basetemp=<报告目录>/basetemp` 的
    (`app/adapters/pytest_adapter.py:255`,PM-004 的整改),所以走适配器的执行不落机器根。
    真正共用机器根的是**不经适配器的那两处**:本次套件自己的 `tmp_path`,以及
    `tests/test_bugset.py:53` 直接 subprocess 起的裸 `python -m pytest`(基线校验,没带
    `--basetemp`)。它们都落在 `<机器临时>/pytest-of-<user>/pytest-N/` 这一个共享根下,而
    pytest 只保留最近 3 个编号目录;套件里有并发路径,别人的清理动作会让本次
    `os.scandir` 撞 `PermissionError [WinError 5]` —— 本夜连续两轮同一处
    `test_bugset[BUG-015]` 因此红,与外层 `--basetemp` 是否互不相同无关。

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


# ---- 分档:慢用例登记表 → `slow` 标记(快档 `-m "not slow"` 跳过,断言不变) ----

SLOW_REGISTRY = Path(__file__).parent / "slow_tests.txt"
# 跑完时点名"超过这个时长却没登记"的用例,防止快档悄悄变慢
UNREGISTERED_SLOW_SECONDS = 5.0


def load_slow_registry() -> set[str]:
    """读登记表:`tests/x.py::test_y` 或整文件 `tests/x.py`;# 开头为注释。"""
    entries: set[str] = set()
    for raw in SLOW_REGISTRY.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            entries.add(line)
    return entries


def _registry_key(nodeid: str) -> tuple[str, str]:
    """nodeid → (文件, 文件::函数);参数化后缀 `[...]` 去掉,按函数整体登记。"""
    base = nodeid.split("[", 1)[0]
    return base.split("::", 1)[0], base


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    registry = load_slow_registry()
    for item in items:
        file_key, func_key = _registry_key(item.nodeid)
        if file_key in registry or func_key in registry:
            item.add_marker(pytest.mark.slow)


_unregistered_slow: list[tuple[str, float]] = []


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if (
        report.when == "call"
        and report.duration >= UNREGISTERED_SLOW_SECONDS
        and "slow" not in report.keywords
    ):
        _unregistered_slow.append((report.nodeid, report.duration))


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    if not _unregistered_slow:
        return
    terminalreporter.section("未登记的慢用例(请加进 tests/slow_tests.txt)")
    for nodeid, duration in sorted(_unregistered_slow, key=lambda pair: -pair[1]):
        terminalreporter.write_line(f"{duration:6.1f}s  {nodeid}")
