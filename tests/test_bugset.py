"""M4 Bug 集测试:schema 完整性与基线校验(failed 必须失败,regression 必须绿)。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.evals.bugset import list_bug_ids, load_bug, load_replay_script
from app.gitops.testing import materialize_repo

BUG_ROOT = Path("bugs")
PYTHON = sys.executable


def test_bug_inventory() -> None:
    ids = list_bug_ids(BUG_ROOT)
    assert len(ids) >= 5
    assert "BUG-001" in ids


def test_bugset_count_after_e5_expansion() -> None:
    """E5 扩编计数护栏:正式集 35 题(28 + 候选 C101..C107 转正为 BUG-029..035;
    C108 因难度标定不符留在 candidates/,见 bugs/candidates/review-2026-09-25.md)。
    后续增删正式题须同步本断言。"""
    ids = list_bug_ids(BUG_ROOT)
    assert len(ids) == 35
    assert "BUG-028" in ids and "BUG-029" in ids and "BUG-035" in ids


@pytest.mark.parametrize("bug_id", list_bug_ids(BUG_ROOT))
def test_bug_schema(bug_id: str) -> None:
    bug = load_bug(bug_id, BUG_ROOT)
    assert bug.failed_tests and bug.regression_tests
    assert bug.issue_text.strip()
    assert (bug.repo_dir / "src").is_dir()
    script = load_replay_script(bug)
    assert isinstance(script, list) and script[-1]["tool"] == "finish"


@pytest.mark.parametrize("bug_id", list_bug_ids(BUG_ROOT))
def test_bug_baseline(bug_id: str, tmp_path: Path) -> None:
    """failed 集在基线必须失败,regression 集必须通过——这是判定规则的地基。"""
    bug = load_bug(bug_id, BUG_ROOT)
    work = tmp_path / "ws"
    materialize_repo(bug.repo_dir, work, extra_commit=False)

    for ids, expect_pass in ((bug.failed_tests, False), (bug.regression_tests, True)):
        proc = subprocess.run(
            [PYTHON, "-m", "pytest", "-q", "--color=no", *ids],
            cwd=work,
            capture_output=True,
            timeout=120,
        )
        actual_pass = proc.returncode == 0
        assert actual_pass == expect_pass, (
            f"{bug_id} {'regression' if expect_pass else 'failed'} set unexpected:\n"
            f"{proc.stdout.decode('utf-8', errors='replace')[-1500:]}"
        )


def test_load_bug_missing_manifest(tmp_path: Path) -> None:
    from app.errors import TaskError

    with pytest.raises(TaskError):
        load_bug("BUG-DOES-NOT-EXIST", BUG_ROOT)


def test_replay_script_json_shape() -> None:
    bug = load_bug("BUG-001", BUG_ROOT)
    script = json.loads(bug.replay_script_path.read_text(encoding="utf-8"))
    tools = [s["tool"] for s in script]
    assert "apply_patch" in tools and tools[-1] == "finish"


# ---------- 审计整改(P2-5):测试 id 格式白名单 ----------


def test_manifest_with_option_injection_rejected(tmp_path: Path) -> None:
    """failed_tests 里夹带 pytest 选项是注入,装载即拒(不进 argv)。"""
    import shutil

    shutil.copytree(Path("bugs/BUG-001"), tmp_path / "BUG-EVIL")
    manifest = tmp_path / "BUG-EVIL" / "manifest.yaml"
    text = manifest.read_text(encoding="utf-8").replace(
        "tests/test_dateparse.py::test_iso_format", "-p evil_plugin"
    )
    manifest.write_text(text, encoding="utf-8")
    with pytest.raises(Exception, match="invalid test id"):
        load_bug(tmp_path / "BUG-EVIL")


def test_build_custom_bug_validates_test_ids() -> None:
    """API 自定义任务是注入面最大的入口,同样强制校验。"""
    from app.errors import TaskError
    from app.evals.bugset import build_custom_bug

    with pytest.raises(TaskError, match="invalid test id"):
        build_custom_bug(
            repo_path=".",
            issue_text="x",
            failed_tests=["tests/test_a.py::test_x", "--collect-only"],
            regression_tests=[],
        )


def test_validate_test_ids_rejects_workspace_escape() -> None:
    """`..` 段/盘符/UNC/绝对路径会把 pytest 收集范围指到工作区外,一律拒绝。"""
    from app.errors import InvalidRequestError
    from app.evals.bugset import validate_test_ids

    for tid in (
        "../other/tests/test_x.py::t",
        "tests/../../evil.py::t",
        "C:/evil/test_x.py::t",
        "//host/share/test_x.py::t",
        "/abs/test_x.py::t",
    ):
        with pytest.raises(InvalidRequestError, match=r"escapes the workspace|workspace-relative"):
            validate_test_ids([tid], "escape guard")

    # 合法形态(含参数化)不受影响
    validate_test_ids(["tests/test_x.py::test_a", "src/pkg/test_y.py::TestC::test_d[1-2]"], "ok")
