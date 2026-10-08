"""M11.5.1 证据测量:并发 pytest 执行会不会删掉彼此的临时根。

零成本:不起模型,只走 `run_pytest` 的真实代码路径(命令构造 → 执行器白名单 → junit 解析)。
唯一变量是 **report_dir 是否共享**(basetemp 由 `junit.parent` 派生,共享 report_dir 即共享
basetemp);工作区每次另拷一份,排除"复用同一工作区"这个混淆项。判据在 TODO.md M11.5.1 写死。

放大器登记(只放大时序,不改平台代码):
- 被诊断测试往 `tmp_path` 连续写 200 个文件、每个之间 sleep 15ms ⇒ 一次执行约 3-4 秒,
  这是"输入仓库的测试本来就慢",不是平台行为;另跑一版**无 sleep 的快版**作为不依赖放大器的对照。
- 并发执行之间错开 0.4s 启动 ⇒ 复现"一次长验证正在跑,另一个执行此刻起步"(pytest 在
  `--basetemp` 已存在时无条件先 `rm_rf` 再建,见 `_pytest/tmpdir.py:154-159`)。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
# 本地后端才有 basetemp 派生逻辑;docker 后端在容器内跑,临时根本来就是隔离的
os.environ["PATCHPILOT_EXECUTION_BACKEND"] = "local"

from app.adapters.pytest_adapter import run_pytest  # noqa: E402

TEST_ID = "tests/test_probe.py::test_probe"
_MARKERS = (
    "PermissionError",
    "FileNotFoundError",
    "NotADirectoryError",
    "INTERNALERROR",
    "system cannot find the path",
    "all files failed",
)

_BODY = '''"""由 measure_basetemp_contention.py 生成:只使用 tmp_path 的被诊断测试。"""
import time
from pathlib import Path

FILES = {files}
GAP = {gap}


def test_probe(tmp_path: Path) -> None:
    payload = "x" * 2048
    for index in range(FILES):
        target = tmp_path / f"artifact-{{index}}.txt"
        target.write_text(payload, encoding="utf-8")
        assert target.read_text(encoding="utf-8") == payload
        if GAP:
            time.sleep(GAP)
    final = tmp_path / "final.txt"
    final.write_text("done", encoding="utf-8")
    assert final.read_text(encoding="utf-8") == "done"
'''


def _machine_root_dirs() -> int:
    """`<系统临时目录>/pytest-of-*/pytest-<n>` 的目录数(泄漏计数用)。"""
    temproot = Path(tempfile.gettempdir())
    count = 0
    for candidate in temproot.glob("pytest-of-*/pytest-*"):
        suffix = candidate.name[len("pytest-") :]
        if candidate.is_dir() and suffix.isdigit():
            count += 1
    return count


def _materialize(ws: Path, files: int, gap: float) -> None:
    tests = ws / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "test_probe.py").write_text(
        _BODY.format(files=files, gap=gap), encoding="utf-8", newline="\n"
    )


def _execute(report_dir: Path, ws: Path, tag: str, delay: float) -> dict[str, Any]:
    if delay:
        time.sleep(delay)
    junit = report_dir / f"junit-{tag}.xml"
    started = time.monotonic()
    report, run = run_pytest(
        sys.executable,
        ws,
        [TEST_ID],
        junit,
        timeout_seconds=180,  # 远大于用例自身耗时:超时不该被误读成临时根竞争
    )
    blob = f"{run.stdout_tail}\n{run.stderr_tail}"
    basetemp_arg = next((arg for arg in run.command if arg.startswith("--basetemp=")), "")
    return {
        "tag": tag,
        "rc": report.exit_code,
        "passed": report.passed,
        "failed": report.failed,
        "errors": report.errors,
        "all_passed": report.all_passed,
        "timed_out": report.timed_out,
        "seconds": round(time.monotonic() - started, 2),
        "markers": [marker for marker in _MARKERS if marker in blob],
        "basetemp": basetemp_arg.split("=", 1)[1] if basetemp_arg else "(none)",
    }


def _arm(
    name: str,
    work: Path,
    round_no: int,
    k: int,
    share_report_dir: bool,
    files: int,
    gap: float,
) -> list[dict[str, Any]]:
    """同一个 report_dir(共享 basetemp)或各自 report_dir(隔离),并发 K 次执行。"""
    tag_prefix = f"{name}-r{round_no}"
    shared = work / f"{tag_prefix}-reports"
    if share_report_dir:
        shared.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=k) as pool:
        futures = []
        for index in range(k):
            ws = work / f"{tag_prefix}-ws{index}"
            _materialize(ws, files, gap)
            report_dir = shared if share_report_dir else ws / "reports"
            if not share_report_dir:
                report_dir.mkdir(parents=True, exist_ok=True)
            futures.append(
                pool.submit(_execute, report_dir, ws, f"{tag_prefix}-e{index}", index * 0.4)
            )
        results = [future.result() for future in futures]
    false_failed = [r for r in results if not r["all_passed"]]
    print(
        f"  {name} 第{round_no}轮 K={k} "
        f"{'共享' if share_report_dir else '隔离'}report_dir: "
        f"伪失败 {len(false_failed)}/{k}"
        + (f"  标记 {sorted({m for r in results for m in r['markers']})}" if false_failed else "")
    )
    for item in results:
        if not item["all_passed"]:
            print(
                f"    {item['tag']}: rc={item['rc']} passed={item['passed']} "
                f"failed={item['failed']} errors={item['errors']} "
                f"timed_out={item['timed_out']} markers={item['markers']}"
            )
    return results


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="m115-basetemp-"))
    print(f"工作目录: {work}")
    machine_before = _machine_root_dirs()

    baseline: dict[str, Any] = {}
    collected: dict[str, list[dict[str, Any]]] = {}
    for variant, (files, gap) in (("慢版(放大器)", (200, 0.015)), ("快版(无 sleep)", (200, 0.0))):
        print(f"\n== {variant} ==")
        ws = work / f"control-{variant}"
        _materialize(ws, files, gap)
        control_dir = ws / "reports"
        control_dir.mkdir(parents=True, exist_ok=True)
        baseline[variant] = _execute(control_dir, ws, "control", 0.0)
        print(
            f"  控制组: all_passed={baseline[variant]['all_passed']} "
            f"耗时={baseline[variant]['seconds']}s "
            f"basetemp={baseline[variant]['basetemp']}"
        )
        for share in (True, False):
            name = f"{variant}-{'shared' if share else 'isolated'}"
            for k in (2, 4):
                for round_no in (1, 2, 3):
                    collected.setdefault(name, []).extend(
                        _arm(name, work, round_no, k, share, files, gap)
                    )

    machine_after = _machine_root_dirs()
    print("\n== 判据 ==")
    for variant, control in baseline.items():
        rows = collected[f"{variant}-shared"]
        bad = [r for r in rows if control["all_passed"] and not r["all_passed"]]
        print(f"(A) {variant} 同 report_dir 并发: 伪失败 {len(bad)}/{len(rows)}")
        isolated = collected[f"{variant}-isolated"]
        print(
            f"(B) {variant} 隔离 report_dir 并发: 伪失败 "
            f"{len([r for r in isolated if control['all_passed'] and not r['all_passed']])}"
            f"/{len(isolated)}"
        )
    print(f"(C) 机器默认临时根目录数: 前 {machine_before} → 后 {machine_after}")
    print("    三条判据的解释与决策规则见 TODO.md M11.5.1(本脚本只出数字)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
