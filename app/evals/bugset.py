"""Bug 任务集定义与加载(bugs/ 目录的 schema 见 bugs/README.md)。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.errors import TaskError

log = logging.getLogger(__name__)

BUGS_ROOT = Path("bugs")


@dataclass
class BugTask:
    """一道自建 Bug 任务:仓库快照 + issue + 判定所需的测试清单。"""

    id: str
    root: Path
    repo_dir: Path
    issue_text: str
    failed_tests: list[str]
    regression_tests: list[str]
    allowed_paths: list[str] | None = None
    max_rounds: int = 5
    category: str = ""
    difficulty: str = "simple"
    replay_script_path: Path | None = None
    test_sets: dict[str, list[str]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.test_sets = {"failed": self.failed_tests, "regression": self.regression_tests}

    @property
    def all_tests(self) -> list[str]:
        return self.failed_tests + self.regression_tests


def _require(data: dict[str, Any], key: str, ctx: str) -> Any:
    if key not in data:
        raise TaskError(f"manifest missing {key!r} ({ctx})")
    return data[key]


def load_bug(path_or_id: str | Path, root: Path | str = BUGS_ROOT) -> BugTask:
    """按目录路径或 BUG-xxx 编号加载任务;manifest.yaml 缺字段即 TaskError。"""
    path = Path(path_or_id)
    if not path.exists():
        path = Path(root) / str(path_or_id)
    if not path.exists():
        raise TaskError(f"bug not found: {path_or_id}")

    manifest_path = path / "manifest.yaml"
    if not manifest_path.exists():
        raise TaskError(f"manifest.yaml not found in {path}")
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TaskError(f"invalid manifest in {manifest_path}")

    bug_id = str(_require(data, "id", str(path)))
    repo_dir = path / "repo"
    if not repo_dir.exists():
        raise TaskError(f"repo dir missing: {repo_dir}")

    issue_path = path / "issue.md"
    if not issue_path.exists():
        raise TaskError(f"issue.md missing in {path}")

    allowed = data.get("allowed_paths") or None
    replay = path / "replay" / "script.json"

    return BugTask(
        id=bug_id,
        root=path,
        repo_dir=repo_dir,
        issue_text=issue_path.read_text(encoding="utf-8").strip(),
        failed_tests=list(_require(data, "failed_tests", bug_id)),
        regression_tests=list(_require(data, "regression_tests", bug_id)),
        allowed_paths=[str(p) for p in allowed] if allowed else None,
        max_rounds=int(data.get("max_rounds", 5)),
        category=str(data.get("category", "")),
        difficulty=str(data.get("difficulty", "simple")),
        replay_script_path=replay if replay.exists() else None,
    )


def list_bug_ids(root: Path | str = BUGS_ROOT) -> list[str]:
    """枚举所有 BUG-* 目录(不含 attacks)。"""
    base = Path(root)
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and p.name.startswith("BUG-"))


def load_replay_script(bug: BugTask) -> list[dict[str, Any]]:
    if bug.replay_script_path is None:
        raise TaskError(f"no replay script for {bug.id}")
    data = json.loads(bug.replay_script_path.read_text(encoding="utf-8"))
    steps = data if isinstance(data, list) else data.get("steps", [])
    if not steps:
        raise TaskError(f"empty replay script for {bug.id}")
    return steps
