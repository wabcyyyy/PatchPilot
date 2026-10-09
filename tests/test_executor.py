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
        check_cmd_allowed(
            [PYTHON, "-c", "x']; import os; os.system('pwned')"], whitelist=("python",)
        )
    with pytest.raises(GateError):
        check_cmd_allowed([PYTHON, "-m", "pytest", "a;rm -rf /"], whitelist=("python",))
    with pytest.raises(GateError):
        check_cmd_allowed([PYTHON, "-m", "pytest", ">out.txt"], whitelist=("python",))
    with pytest.raises(GateError):
        check_cmd_allowed([PYTHON, "-m", "pytest", "$(rm -rf /)"], whitelist=("python",))


def test_whitelist_allows_parametrized_test_ids() -> None:
    """复盘 P1-4:test_x[(1,2)] 是 pytest 合法参数化 id,括号无 shell 语义
    (shell=False 参数列表),不得误判为注入;$ 路径见上,仍拦截。"""
    check_cmd_allowed(
        [PYTHON, "-m", "pytest", "tests/test_x.py::test_x[(1, 2)]"], whitelist=("python",)
    )


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
        f'\'import time; open(r"{pid_file.as_posix()}", "w").write(str(__import__("os").getpid())); time.sleep(120)\'])\n'
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
        probe = subprocess.run(
            ["tasklist", "/FI", f"PID eq {child_pid}"], capture_output=True, check=False
        )
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
        PYTHON,
        demo_ws,
        test_ids=["tests/test_dateparse.py::test_does_not_exist"],
        report_path=tmp_path / "n.xml",
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


# ---------- 审计整改(P0-1①):all_passed 按期望 id 集合判定 ----------


def _write_junit(tmp_path: Path, cases: list[tuple[str, str, str]]) -> Path:
    """cases: (file, name, status),status ∈ passed | failure | skipped。

    S01 起判定要求 file+类链一致,classname 写实为 tests.test_a(与 file 对应的
    点分路径一致,xunit1 的真实形态),不再是与匹配无关的占位符。
    """
    body = []
    for file, name, status in cases:
        if status == "passed":
            body.append(f'<testcase classname="tests.test_a" name="{name}" file="{file}"/>')
        elif status == "skipped":
            body.append(
                f'<testcase classname="tests.test_a" name="{name}" file="{file}"><skipped/></testcase>'
            )
        else:
            body.append(
                f'<testcase classname="tests.test_a" name="{name}" file="{file}">'
                '<failure message="boom">x</failure></testcase>'
            )
    xml = '<testsuite tests="{}" failures="{}" errors="0" skipped="{}">{}</testsuite>'.format(
        len(cases),
        sum(1 for c in cases if c[2] == "failure"),
        sum(1 for c in cases if c[2] == "skipped"),
        "".join(body),
    )
    path = tmp_path / "junit.xml"
    path.write_text(xml, encoding="utf-8")
    return path


def test_all_passed_rejects_skip_injection(tmp_path: Path) -> None:
    """conftest 把用例 skip 后 rc 仍为 0——判定必须看"真实跑过并通过"。"""
    from app.adapters.pytest_adapter import parse_junit_xml

    report = parse_junit_xml(_write_junit(tmp_path, [("tests/test_a.py", "test_x", "skipped")]))
    report.requested_ids = ["tests/test_a.py::test_x"]
    assert not report.all_passed


def test_all_passed_rejects_deselected_id(tmp_path: Path) -> None:
    """conftest deselect 用例后 junit 里没有该 id——缺失即不通过。"""
    from app.adapters.pytest_adapter import parse_junit_xml

    report = parse_junit_xml(_write_junit(tmp_path, [("tests/test_a.py", "test_other", "passed")]))
    report.requested_ids = ["tests/test_a.py::test_x"]
    assert not report.all_passed


def test_all_passed_accepts_real_pass(tmp_path: Path) -> None:
    from app.adapters.pytest_adapter import parse_junit_xml

    report = parse_junit_xml(_write_junit(tmp_path, [("tests/test_a.py", "test_x", "passed")]))
    report.requested_ids = ["tests/test_a.py::test_x"]
    assert report.all_passed


def test_all_passed_rejects_ambiguous_identity(tmp_path: Path) -> None:
    """S01/F3:同一请求 id 被两个不同 testcase 三元组同时满足 = 身份歧义,不许通过。

    旧实现"任何一个 testcase passed 就算数":othertests/test_a.py 会顶替
    tests/test_a.py 的同名用例。新口径每条请求 id 必须恰好对应一个去重三元组。
    """
    from app.adapters.pytest_adapter import parse_junit_xml

    report = parse_junit_xml(
        _write_junit(
            tmp_path,
            [("tests/test_a.py", "test_x", "passed"), ("tests/test_a.py", "test_x", "passed")],
        )
    )
    # 手工把其中一个 case 的 file 换成同后缀的别的目录(junit 层面两条不同三元组)
    raw = (tmp_path / "junit.xml").read_text(encoding="utf-8")
    raw = raw.replace('file="tests/test_a.py"', 'file="pkg/tests/test_a.py"', 1)
    (tmp_path / "junit.xml").write_text(raw, encoding="utf-8")
    report = parse_junit_xml(tmp_path / "junit.xml")
    report.requested_ids = ["tests/test_a.py::test_x"]
    assert not report.all_passed


# ---------- S01/F3:真实 pytest 仓库上的身份判定 ----------


def _write_identity_repo(root: Path) -> Path:
    """小仓库:同文件两个同名方法分属 TestA/TestB(一红一绿)+ 模块同名函数 + 参数化。"""
    tests_dir = root / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_x.py").write_text(
        "import pytest\n"
        "def test_same():\n"
        "    assert True\n"
        "class TestA:\n"
        "    def test_same(self):\n"
        "        assert False, 'TestA still broken'\n"
        "class TestB:\n"
        "    def test_same(self):\n"
        "        assert True\n"
        "class TestOuter:\n"
        "    class TestInner:\n"
        "        def test_deep(self):\n"
        "            assert True\n",
        encoding="utf-8",
    )
    (tests_dir / "test_tuple.py").write_text(
        "import pytest\n"
        "@pytest.mark.parametrize('v', [(1, 2), (3, 4)], ids=['(1,2)', '(3,4)'])\n"
        "def test_tuple(v):\n"
        "    assert v != (1, 2), 'tuple param still broken'\n",
        encoding="utf-8",
    )
    return root


def test_f3_real_repo_same_name_class_does_not_cross_match(tmp_path: Path) -> None:
    """F3 端到端:请求 TestA::test_same,只有 TestB::test_same 通过 → 不得 all_passed。"""
    ws = _write_identity_repo(tmp_path)
    report, _ = run_pytest(
        PYTHON, ws, ["tests/test_x.py::TestA::test_same"], report_path=tmp_path / "f3.xml"
    )
    assert report.exit_code == 1
    assert not report.all_passed, "TestB 的同名通过不得顶替 TestA 的失败"
    assert report.failed == 1
    # 反向:请求的是真的通过的那条 → 通过
    ok_report, _ = run_pytest(
        PYTHON, ws, ["tests/test_x.py::TestB::test_same"], report_path=tmp_path / "f3-ok.xml"
    )
    assert ok_report.all_passed


def test_f3_real_repo_module_function_vs_class_method(tmp_path: Path) -> None:
    """模块函数 test_same(绿)不得顶替类方法 TestA::test_same(红),双向。"""
    ws = _write_identity_repo(tmp_path)
    report, _ = run_pytest(
        PYTHON, ws, ["tests/test_x.py::TestA::test_same"], report_path=tmp_path / "m1.xml"
    )
    assert not report.all_passed
    mod_report, _ = run_pytest(
        PYTHON, ws, ["tests/test_x.py::test_same"], report_path=tmp_path / "m2.xml"
    )
    assert mod_report.all_passed


def test_f3_real_repo_nested_class_chain(tmp_path: Path) -> None:
    ws = _write_identity_repo(tmp_path)
    report, _ = run_pytest(
        PYTHON,
        ws,
        ["tests/test_x.py::TestOuter::TestInner::test_deep"],
        report_path=tmp_path / "n1.xml",
    )
    assert report.all_passed


def test_f3_real_repo_parametrized_tuple_ids(tmp_path: Path) -> None:
    """合法参数化 id `test_tuple[(1,2)]` 可执行且按参数精确判定。"""
    ws = _write_identity_repo(tmp_path)
    bad, _ = run_pytest(
        PYTHON, ws, ["tests/test_tuple.py::test_tuple[(1,2)]"], report_path=tmp_path / "p1.xml"
    )
    assert bad.failed == 1 and not bad.all_passed
    good, _ = run_pytest(
        PYTHON, ws, ["tests/test_tuple.py::test_tuple[(3,4)]"], report_path=tmp_path / "p2.xml"
    )
    assert good.all_passed and good.passed == 1
    both, _ = run_pytest(
        PYTHON,
        ws,
        ["tests/test_tuple.py::test_tuple[(1,2)]", "tests/test_tuple.py::test_tuple[(3,4)]"],
        report_path=tmp_path / "p3.xml",
    )
    assert not both.all_passed and both.failed == 1


def test_f3_real_repo_missing_id_rejected(tmp_path: Path) -> None:
    """请求不存在的类:collection error(rc 非 0),绝不通过。"""
    ws = _write_identity_repo(tmp_path)
    report, _ = run_pytest(
        PYTHON, ws, ["tests/test_x.py::TestMissing::test_same"], report_path=tmp_path / "x.xml"
    )
    assert report.exit_code != 0 and not report.all_passed


def test_build_pytest_cmd_forces_xunit1() -> None:
    """file 属性是期望 id 匹配的主判据,junit_family 必须钉在 xunit1。"""
    cmd = build_pytest_cmd("python", ["tests/test_a.py::test_x"], Path("j.xml"))
    assert "junit_family=xunit1" in cmd


def test_run_tests_strips_inherited_secrets_from_env(tmp_path: Path) -> None:
    """N-8 整改:测试子进程不得继承平台密钥环境变量;系统级白名单保留。"""
    code = (
        "import os; print('LEAKED' if os.environ.get('PATCHPILOT_LLM_API_KEY') else 'CLEAN');"
        "print('HASPATH' if os.environ.get('PATH') else 'NOPATH')"
    )
    monkey_env = {**os.environ, "PATCHPILOT_LLM_API_KEY": "sk-secret"}
    original = os.environ.copy()
    os.environ.update(monkey_env)
    try:
        result = run_tests([PYTHON, "-c", code], tmp_path, 30)
    finally:
        os.environ.clear()
        os.environ.update(original)
    assert "CLEAN" in result.stdout_tail and "LEAKED" not in result.stdout_tail
    assert "HASPATH" in result.stdout_tail


def test_run_tests_timeout_fallback_closes_pipes(monkeypatch: pytest.MonkeyPatch) -> None:
    """复盘 R-4(N-15 兜底路径):逃逸的分离孙子进程持有管道写端时,
    kill 后最后一次 communicate 仍超时 → 关管道回收,不永久挂死。"""
    import io
    import subprocess as real_subprocess

    from app.executor import local_runner
    from app.executor.local_runner import TestRunResult, run_tests

    class _PipeHoggingProc:
        """模拟:kill 杀不到的孙子进程仍持有 stdout 写端,communicate 永远超时。"""

        def __init__(self) -> None:
            self.pid = 4242
            self.returncode = -9  # TestRunResult 组装时读取
            self.stdout = io.BytesIO(b"partial")
            self.stderr = io.BytesIO(b"")
            self.killed = False

        def communicate(self, input=None, timeout=None):  # type: ignore[no-untyped-def]
            raise real_subprocess.TimeoutExpired("cmd", timeout)

        def kill(self) -> None:
            self.killed = True

        def wait(self, timeout=None):  # type: ignore[no-untyped-def]
            raise real_subprocess.TimeoutExpired("cmd", timeout)

    fake = _PipeHoggingProc()
    real_popen = real_subprocess.Popen

    def fake_popen(command, *args, **kwargs):  # type: ignore[no-untyped-def]
        # 被测目标:run_tests 的测试进程 → 假货;
        # _kill_tree 在 Windows 上的 taskkill → 真 Popen(taskkill 找不到 pid,
        # check=False 下静默失败,与真实"杀不到"语义一致)
        if next(iter(command)) == "taskkill":
            return real_popen(command, *args, **kwargs)
        return fake

    monkeypatch.setattr(local_runner.subprocess, "Popen", fake_popen)

    result = run_tests(["python", "x"], cwd=".", timeout_seconds=1)

    assert isinstance(result, TestRunResult)
    assert result.timed_out is True
    assert fake.killed  # proc.kill 已尝试
    assert fake.stdout.closed  # 管道已关闭回收
    assert result.stdout_tail == ""  # 未永久挂死,按超时口径返回


def test_local_missing_junit_carries_cause_and_keeps_verdict(monkeypatch, tmp_path: Path) -> None:
    """local 后端(默认后端)有同一个盲点:junit 没生成时报告只剩症状没有原因。

    这一条比 docker 那面墙更常碰到——**超时是被当成正常业务结果设计的**,而被杀时
    junit 往往根本没写出来。钉两件事:(1) stderr 里的死因进 `traceback`;
    (2) 判定层不动(`signature` 与 `all_passed` 保持原样,不会因挂原因而翻成通过)。
    """
    import app.adapters.pytest_adapter as adapter
    from app.executor.local_runner import TestRunResult

    def fake_run_tests(command, cwd, timeout_seconds=None):
        return TestRunResult(
            command=command,
            exit_code=1,
            stdout_tail="",
            stderr_tail="INTERNALERROR> IndexError: conftest blew up",
            duration_ms=1,
        )

    monkeypatch.setattr(adapter, "run_tests", fake_run_tests)
    ws = tmp_path / "ws"
    ws.mkdir()
    report, _ = run_pytest(PYTHON, ws, ["t.py::t"], report_path=tmp_path / "j.xml")

    assert report.errors == 1 and not report.all_passed
    case = report.failed_cases[0]
    assert case.signature == "error: no junit xml"  # 判定口径没被改动
    assert "conftest blew up" in case.traceback and "退出码 1" in case.traceback
