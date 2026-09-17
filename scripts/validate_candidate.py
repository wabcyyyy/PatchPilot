"""候选题目校验:bugs_candidates/ 待审题的入场检查(人工审题前的自动门槛)。

用法:
    .venv/Scripts/python.exe scripts/validate_candidate.py bugs_candidates/CAND-001

检查项(全部通过 exit 0;任何一项不通过 exit 1):
1. manifest.yaml 必填字段齐全且非空;
2. replay/script.json 与 replay/graph-script.json 均为可解析 JSON 的非空步骤列表;
3. repo 物化为 git 工作区后,failed_tests 每一条在基线恰好全部失败;
4. regression_tests 整体在基线全部通过。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REQUIRED_FIELDS = (
    "id",
    "category",
    "difficulty",
    "failed_tests",
    "regression_tests",
    "allowed_paths",
    "max_rounds",
)


def _run_pytest(work: Path, node_ids: list[str], tmp: Path, tag: str) -> int:
    """在物化工作区跑一组测试,返回 pytest returncode(0 = 全过)。"""
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--color=no",
            "-p",
            "no:cacheprovider",
            f"--basetemp={tmp / f'basetemp-{tag}'}",
            *node_ids,
        ],
        cwd=work,
        capture_output=True,
        timeout=300,
    ).returncode


def _replay_problems(bug_dir: Path) -> list[str]:
    problems: list[str] = []
    for name in ("script.json", "graph-script.json"):
        path = bug_dir / "replay" / name
        if not path.exists():
            problems.append(f"replay/{name} missing")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.append(f"replay/{name} is not valid JSON: {exc}")
            continue
        if isinstance(data, list):
            steps = data
        elif isinstance(data, dict):
            steps = data.get("steps", [])
        else:
            steps = []
        if not steps:
            problems.append(f"replay/{name} has no steps")
    return problems


def validate(bug_dir: Path) -> list[str]:
    """返回问题列表;空列表表示全部通过。"""
    if not bug_dir.is_dir():
        return [f"not a directory: {bug_dir}"]
    manifest_path = bug_dir / "manifest.yaml"
    if not manifest_path.exists():
        return [f"manifest.yaml missing in {bug_dir}"]
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return [f"manifest.yaml is not a mapping in {bug_dir}"]

    problems: list[str] = []
    for field in REQUIRED_FIELDS:
        if data.get(field) in (None, "", [], {}):
            problems.append(f"manifest missing/empty field: {field}")
    problems += _replay_problems(bug_dir)
    if problems:
        return problems

    from app.evals.bugset import load_bug
    from app.gitops.testing import materialize_repo

    bug = load_bug(bug_dir)
    tmp = Path(tempfile.mkdtemp(prefix="candcheck-"))
    try:
        work = tmp / "ws"
        materialize_repo(bug.repo_dir, work, extra_commit=False)
        for index, node_id in enumerate(bug.failed_tests):
            rc = _run_pytest(work, [node_id], tmp, f"f{index}")
            if rc == 0:
                problems.append(f"failed_test unexpectedly passes at baseline: {node_id}")
        rc = _run_pytest(work, list(bug.regression_tests), tmp, "reg")
        if rc != 0:
            problems.append("regression_tests not green at baseline")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python scripts/validate_candidate.py <candidate-dir>")
        return 2
    target = Path(argv[0])
    if not target.is_absolute():
        target = Path.cwd() / target
    problems = validate(target)
    for problem in problems:
        print(f"[invalid] {target.name}: {problem}")
    if problems:
        return 1
    print(f"[ok] {target.name} passed candidate validation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
