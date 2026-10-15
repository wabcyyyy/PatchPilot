"""验证测试执行器输出边界与 JUnit XML 防护 (P07)。"""

import sys
from pathlib import Path

from app.adapters.pytest_adapter import MAX_JUNIT_BYTES, parse_junit_xml
from app.executor.local_runner import run_tests


def test_run_tests_bounds_large_output(tmp_path: Path) -> None:
    """子进程生成大量输出时父进程不爆内存，并记录 dropped_bytes。"""
    # 打印约 2MB 文本到 stdout
    cmd = [
        sys.executable,
        "-c",
        "import sys; chunk = 'A' * 65536; [sys.stdout.write(chunk) for _ in range(32)]",
    ]
    result = run_tests(cmd, cwd=tmp_path, timeout_seconds=10)
    assert result.exit_code == 0
    assert result.stdout_bytes == 65536 * 32  # 2MB
    assert result.stdout_dropped_bytes > 0
    assert result.dropped_bytes == result.stdout_dropped_bytes
    assert len(result.stdout_tail) <= 20000
    assert result.timed_out is False


def test_run_tests_continuous_stream_without_newlines(tmp_path: Path) -> None:
    """无换行符的连续数据流分块正常消费，不产生阻塞。"""
    cmd = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.write('X' * 200000); sys.stderr.write('Y' * 100000)",
    ]
    result = run_tests(cmd, cwd=tmp_path, timeout_seconds=10)
    assert result.exit_code == 0
    assert result.stdout_bytes == 200000
    assert result.stderr_bytes == 100000
    assert result.stdout_dropped_bytes > 0
    assert result.stderr_dropped_bytes > 0
    assert result.stdout_tail.endswith("XXXX")
    assert result.stderr_tail.endswith("YYYY")


def test_parse_junit_xml_exceeding_max_bytes(tmp_path: Path) -> None:
    """超过 10MB 的超大 JUnit XML 报告被拦截并记为结构化错误。"""
    oversized_file = tmp_path / "huge_junit.xml"
    # 创建一个刚好超过 MAX_JUNIT_BYTES 的文件 (使用 sparse file / seek 快速创建)
    with oversized_file.open("wb") as f:
        f.seek(MAX_JUNIT_BYTES + 1024)
        f.write(b"</testsuite>")

    report = parse_junit_xml(oversized_file)
    assert report.errors == 1
    assert not report.all_passed
    assert len(report.failed_cases) == 1
    assert report.failed_cases[0].signature == "error: junit xml too large"
    assert "exceeds maximum size limit" in report.failed_cases[0].message_first_line


def test_parse_junit_xml_malformed(tmp_path: Path) -> None:
    """畸形或损坏的 JUnit XML 转为结构化错误，不崩溃。"""
    malformed_file = tmp_path / "broken_junit.xml"
    malformed_file.write_text("<testsuite><testcase name='unclosed'", encoding="utf-8")

    report = parse_junit_xml(malformed_file)
    assert report.errors == 1
    assert not report.all_passed
    assert len(report.failed_cases) == 1
    assert report.failed_cases[0].signature == "error: invalid junit xml"
    assert "malformed junit xml" in report.failed_cases[0].message_first_line


def test_parse_junit_xml_valid(tmp_path: Path) -> None:
    """合法的 JUnit XML 保持原有正常解析行为。"""
    valid_xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<testsuite name="pytest" errors="0" failures="0" skipped="0" tests="1" time="0.01">\n'
        '  <testcase classname="tests.test_mod" file="tests/test_mod.py" name="test_ok" time="0.005"/>\n'
        "</testsuite>\n"
    )
    xml_path = tmp_path / "junit.xml"
    xml_path.write_text(valid_xml, encoding="utf-8")

    report = parse_junit_xml(xml_path)
    report.requested_ids = ["tests/test_mod.py::test_ok"]
    assert report.errors == 0
    assert report.failed == 0
    assert report.passed == 1
    assert report.all_passed
