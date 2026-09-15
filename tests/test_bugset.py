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
