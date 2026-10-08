"""M11.5 测出来的那个缺陷,现在有测试守着:并发执行不得共用临时根。

被测事实(M11.5.1 实测:共享 report_dir 的并发执行 18 次里 9 次伪失败,隔离组 0/18):
pytest 在 `--basetemp` 已存在时**无条件先整棵删掉再建**,所以两条执行只要落到同一个
basetemp,后起步那次就会把先起步那次正在写的 `tmp_path` 删光 —— 表现为测试中途
`FileNotFoundError` 或会话级 error,在平台看来就是"这道题没修好"(假 VERIFY_FAILED)。

这两条用例都是**跑真 pytest 子进程**的(不 mock):被删的是文件系统事实,mock 不出来。
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.adapters.pytest_adapter import run_pytest

_TEST_ID = "tests/test_temp.py::test_writes_and_reads_back"
_SOURCE = '''"""只使用 tmp_path 的被诊断测试(写后读回,删了就会红)。"""

from pathlib import Path

FILES = 40


def test_writes_and_reads_back(tmp_path: Path) -> None:
    payload = "x" * 512
    for index in range(FILES):
        target = tmp_path / f"artifact{index}.txt"
        target.write_text(payload, encoding="utf-8")
        assert target.read_text(encoding="utf-8") == payload
'''


def _repo(root: Path) -> Path:
    workspace = root / "repo"
    tests = workspace / "tests"
    tests.mkdir(parents=True)
    (tests / "test_temp.py").write_text(_SOURCE, encoding="utf-8", newline="\n")
    return workspace


def _basetemp_of(command: list[str]) -> Path:
    arg = next(item for item in command if item.startswith("--basetemp="))
    return Path(arg.split("=", 1)[1])


def test_each_execution_has_its_own_recycled_basetemp(tmp_path: Path) -> None:
    """同一个报告目录里的两次执行:临时根必须互不相同,且执行完不留在盘上。"""
    reports = tmp_path / "reports"
    reports.mkdir()

    paths = []
    for index in range(2):
        workspace = _repo(tmp_path / f"case{index}")
        report, run = run_pytest(
            sys.executable, workspace, [_TEST_ID], reports / f"junit{index}.xml"
        )
        assert report.all_passed, run.stdout_tail[-1200:]
        basetemp = _basetemp_of(run.command)
        paths.append(basetemp)
        assert reports in basetemp.parents, "临时根要落在报告目录里,不进工作区、不落系统临时目录"
        assert workspace not in basetemp.parents and basetemp not in workspace.parents
        assert not basetemp.exists(), "执行结束应回收自己的临时根(否则多轮任务一路涨盘)"
    assert len({str(p) for p in paths}) == 2, f"两次执行共用了同一个临时根:{paths}"


def test_concurrent_executions_in_one_report_dir_all_pass(tmp_path: Path) -> None:
    """并发回归用例:三条执行挤同一个报告目录,谁也不许把谁的临时文件删掉。

    修复前这个形态的实测伪失败率约 50%;随机后缀让它们在结构上不可能相遇,
    所以本用例断言的是"全绿",而不是"允许偶发失败"。
    """
    reports = tmp_path / "reports"
    reports.mkdir()
    workspaces = [_repo(tmp_path / f"case{index}") for index in range(3)]

    def _run(index: int, workspace: Path) -> tuple[bool, str]:
        report, run = run_pytest(
            sys.executable, workspace, [_TEST_ID], reports / f"junit{index}.xml"
        )
        return report.all_passed, f"{run.stdout_tail}\n{run.stderr_tail}"

    with ThreadPoolExecutor(max_workers=3) as pool:
        outcomes = list(pool.map(lambda pair: _run(*pair), enumerate(workspaces)))

    for all_passed, output in outcomes:
        assert all_passed, output[-1200:]
    joined = "".join(output for _, output in outcomes)
    for marker in ("FileNotFoundError", "PermissionError", "INTERNALERROR"):
        assert marker not in joined, f"临时根被并发执行删掉了({marker})"
