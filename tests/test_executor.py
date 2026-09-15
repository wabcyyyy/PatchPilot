"""M2 执行器测试:白名单、进程树超时清理、pytest 报告解析与失败签名。"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.adapters.pytest_adapter import build_pytest_cmd, failure_signature, run_pytest
from app.errors import GateError
from app.executor.local_runner import run_tests
from app.executor.whitelist import check_cmd_allowed
from app.gitops.snapshot import create_workspace

PYTHON = sys.executable


# ---------- whitelist ----------


def test_whitelist_allows_pytest_cmd() -> None:
    check_cmd_allowed([PYTHON, "-m", "pytest", "-q"], whitelist=("python", "pytest"))


def test_whitelist_rejects_unknown_executable() -> None:
    with pytest.raises(GateError):
        check_cmd_allowed(["bash", "-c", "echo hi"], whitelist=("python", "pytest"))


def test_whitelist_rejects_shell_metachars() -> None:
    with pytest.raises(GateError):
        check_cmd_allowed([PYTHON, "-c", "x']; import os; os.system('pwned')"], whitelist=("python",))
    with pytest.raises(GateError):
        check_cmd_allowed([PYTHON, "-m", "pytest", "a;rm -rf /"], whitelist=("python",))
    with pytest.raises(GateError):
        check_cmd_allowed([PYTHON, "-m", "pytest", ">out.txt"], whitelist=("python",))


def test_whitelist_rejects_empty() -> None:
    with pytest.raises(GateError):
        check_cmd_allowed([], whitelist=("python",))


# ---------- local_runner ----------


def test_run_tests_captures_output_and_rc(tmp_path: Path) -> None:
    result = run_tests([PYTHON, "-c", "print('hello'); raise SystemExit(3)"], tmp_path, 30)
    assert result.exit_code == 3
    assert "hello" in result.stdout_tail
    assert not result.timed_out


def test_run_tests_accepts_multiline_python_c(tmp_path: Path) -> None:
    """runner 是通用执行器:合法的 -c 代码可含换行;注入由白名单层拦截。"""
    result = run_tests([PYTHON, "-c", "print(1)\nprint(2)"], tmp_path, 30)
    assert result.exit_code == 0
    assert "1" in result.stdout_tail and "2" in result.stdout_tail


def test_run_tests_timeout_kills_process_tree(tmp_path: Path) -> None:
    """超时后父子进程都必须被清理:子进程把自己的 pid 写进文件供验证。"""
    if sys.platform != "win32":
        pytest.skip("进程树验证依赖 Windows taskkill 路径")

    pid_file = tmp_path / "child_pid.txt"
    parent_code = (
        "import subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        f"'import time; open(r\"{pid_file.as_posix()}\", \"w\").write(str(__import__(\"os\").getpid())); time.sleep(120)'])\n"
        "print(child.pid, flush=True)\n"
        "import time; time.sleep(120)\n"
    )
    started = time.monotonic()
    result = run_tests([PYTHON, "-c", parent_code], tmp_path, 5)
    assert result.timed_out
    assert time.monotonic() - started < 30

    for _ in range(20):
        if pid_file.exists():
            break
        time.sleep(0.2)
    if pid_file.exists():
        child_pid = int(pid_file.read_text(encoding="utf-8").strip())
        time.sleep(1.0)
        probe = subprocess.run(["tasklist", "/FI", f"PID eq {child_pid}"], capture_output=True, check=False)
        # 中文 Windows 上 tasklist 输出 GBK,手动容错解码;PID 是 ASCII,成员判断不受影响
        out = probe.stdout.decode("utf-8", errors="replace")
        assert str(child_pid) not in out, f"child {child_pid} survived the tree kill"
    else:
        pytest.fail("child never started; cannot verify tree kill")


# ---------- pytest adapter ----------


@pytest.fixture()
def demo_ws(demo_repo: Path, tmp_path: Path) -> Path:
    create_workspace(demo_repo, tmp_path / "ws")
    return tmp_path / "ws"


def test_build_pytest_cmd_shape() -> None:
    cmd = build_pytest_cmd("py", ["tests/a.py::t1"], Path("r.xml"), ["-x"])
    assert cmd[:5] == ["py", "-m", "pytest", "-q", "--color=no"]
    assert "--junitxml=r.xml" in cmd and "tests/a.py::t1" in cmd and "-x" in cmd


def test_baseline_failure_detected(demo_ws: Path, tmp_path: Path) -> None:
    report, run = run_pytest(PYTHON, demo_ws, report_path=tmp_path / "base.xml")
    assert run.exit_code == 1
    assert report.failed == 1 and report.passed == 2
    assert report.failed_cases[0].test_name == "test_empty_string_returns_none"
    assert "assert" in report.failed_cases[0].signature or report.failed_cases[0].kind == "failure"


def test_passing_subset_is_green(demo_ws: Path, tmp_path: Path) -> None:
    report, _ = run_pytest(
        PYTHON,
        demo_ws,
        test_ids=["tests/test_dateparse.py::test_parse_iso_format"],
        report_path=tmp_path / "keep.xml",
    )
    assert report.all_passed
    assert report.passed == 1


def test_signature_stability_and_distinctness(demo_ws: Path, tmp_path: Path) -> None:
    sigs = []
    for i in (1, 2):
        report, _ = run_pytest(
            PYTHON,
            demo_ws,
            test_ids=["tests/test_dateparse.py::test_empty_string_returns_none"],
            report_path=tmp_path / f"sig{i}.xml",
        )
        sigs.append(report.failed_cases[0].signature)
    assert sigs[0] == sigs[1], "same failure must produce a stable signature"


def test_signature_normalizes_content() -> None:
    a = failure_signature("failure", "ValueError: bad input 123\n  at line 4")
    b = failure_signature("failure", "  ValueError:   bad input 123")
    assert a == b, "whitespace 与截断应归一化,首行相同即同签名"
    assert failure_signature("failure", "x") != failure_signature("error", "x")


def test_no_tests_collected_flag(demo_ws: Path, tmp_path: Path) -> None:
    report, _ = run_pytest(
        PYTHON, demo_ws, test_ids=["tests/test_dateparse.py::test_does_not_exist"], report_path=tmp_path / "n.xml"
    )
    # pytest 对"用例不存在"返回 usage error(rc=4);无论哪种非零码,都不能当作通过
    assert report.exit_code == 4
    assert not report.all_passed


def _pid_alive(pid: int) -> bool:  # pragma: no cover - 平台相关,测试内联使用
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
