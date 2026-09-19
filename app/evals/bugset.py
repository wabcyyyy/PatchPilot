"""Bug 任务集定义与加载(bugs/ 目录的 schema 见 bugs/README.md)。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.config import get_settings
from app.errors import InvalidRequestError, TaskError

log = logging.getLogger(__name__)

BUGS_ROOT = Path("bugs")

# P2-5 整改:测试 id 是要拼进 pytest argv 的外部输入(manifest 或 API 请求),
# 必须先过格式白名单——否则 "-p evil" 这类 pytest 选项会构成注入。
# 空格允许:参数化 id 如 test_x[a b] 是合法节点 id,argv 单元素传参无注入语义。
_TEST_ID_RE = re.compile(r"^[A-Za-z0-9_ ./\-\[\]:]+$")
# N-3 整改:id 里的路径部分还可能把 pytest 的收集范围指到工作区之外
# (cwd=物化工作区下,`../x`、盘符、UNC 都是合法 argv 操作数),一并拒绝。
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def validate_test_ids(ids: list[str], ctx: str) -> None:
    """逐条校验测试 id 格式:非空、不以 - 开头、仅含路径/节点 id 合法字符。

    另拒绝 `..` 段与绝对路径(盘符/UNC/`/` 开头):id 会原样进入 pytest argv,
    收集范围逃逸工作区等于把判定权交给工作区外的任意文件。
    """
    for tid in ids:
        if (
            not tid
            or tid != tid.strip()  # 前导空白可掩盖 "-p" 形态,一并拒绝
            or tid.startswith("-")
            or not _TEST_ID_RE.fullmatch(tid)
        ):
            raise InvalidRequestError(
                f"{ctx}: invalid test id {tid!r} (pytest option injection guard)"
            )
        file_part = tid.split("::")[0]
        parts = file_part.split("/")
        if ".." in parts or file_part.startswith(("/", "\\")) or "//" in file_part:
            raise InvalidRequestError(
                f"{ctx}: test id {tid!r} escapes the workspace (path traversal guard)"
            )
        if _DRIVE_RE.match(file_part):
            raise InvalidRequestError(f"{ctx}: test id {tid!r} must be workspace-relative")


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

    failed = list(_require(data, "failed_tests", bug_id))
    regression = list(_require(data, "regression_tests", bug_id))
    validate_test_ids(failed, bug_id)
    validate_test_ids(regression, bug_id)

    return BugTask(
        id=bug_id,
        root=path,
        repo_dir=repo_dir,
        issue_text=issue_path.read_text(encoding="utf-8").strip(),
        failed_tests=failed,
        regression_tests=regression,
        allowed_paths=[str(p) for p in allowed] if allowed else None,
        # P1-4 整改:默认轮数走 Settings,不再是硬编码 5
        max_rounds=int(data.get("max_rounds", get_settings().default_max_rounds)),
        category=str(data.get("category", "")),
        difficulty=str(data.get("difficulty", "simple")),
        replay_script_path=replay if replay.exists() else None,
    )


def _custom_bug_id(
    repo_path: Path,
    issue_text: str,
    failed: list[str],
    regression: list[str],
    allowed_paths: list[str] | None,
) -> str:
    """由任务内容确定性派生自定义任务 id(N-22 整改)。

    纳入 allowed_paths:它是题目身份的一部分且直接决定门禁行为,
    同 repo 同内容但不同 scope 的两个在途任务不能共享幂等键。
    max_rounds 排除:运行参数,与正式题 idem_key 的口径一致。
    12 位 hex(48-bit):碰撞后果是两个不同任务共享幂等键——比重复执行更糟,
    不能为短而牺牲。
    """
    payload = "\x1f".join(
        [
            os.path.normcase(str(Path(repo_path).resolve())),
            issue_text,
            *sorted(failed),
            *sorted(regression),
            *sorted(allowed_paths or []),
        ]
    )
    return "CUSTOM-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def build_custom_bug(
    *,
    repo_path: Path | str,
    issue_text: str,
    failed_tests: list[str],
    regression_tests: list[str],
    allowed_paths: list[str] | None = None,
    max_rounds: int | None = None,
) -> BugTask:
    """内存构造自定义任务(任意仓库接入):不经 bugs/ 目录结构,无回放脚本文件。

    id 由任务内容确定性派生(N-22 整改):此前含随机段导致幂等键每次必不同,
    同一提交因超时重试就会并发跑多份。同内容重提 → 同 id → 在途幂等命中;
    终态后允许重跑(与正式题语义一致)。issue_text 按原文字节参与散列,
    空白差异视为不同任务。安全语义与正式题完全一致
    (禁改测试文件由门禁 forbid_test_files=True 无条件兜底,与本辅助无关)。
    """
    root = Path(repo_path).resolve()
    # P1-4 整改:默认轮数走 Settings;None 才取默认,调用方显式传值不被覆盖
    max_rounds = get_settings().default_max_rounds if max_rounds is None else max_rounds
    failed = list(failed_tests)
    regression = list(regression_tests)
    # 自定义任务直接来自 API 请求体,是注入面最大的入口,同样强制校验
    validate_test_ids(failed, "custom task")
    validate_test_ids(regression, "custom task")
    return BugTask(
        id=_custom_bug_id(root, issue_text, failed, regression, allowed_paths),
        root=root,
        repo_dir=root,
        issue_text=issue_text,
        failed_tests=failed,
        regression_tests=regression,
        allowed_paths=[str(p) for p in allowed_paths] if allowed_paths else None,
        max_rounds=max_rounds,
        category="custom",
        replay_script_path=None,
    )


def list_bug_ids(root: Path | str = BUGS_ROOT) -> list[str]:
    """枚举所有 BUG-* 目录(不含 attacks)。"""
    base = Path(root)
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and p.name.startswith("BUG-"))


def load_replay_script(bug: BugTask, kind: str = "plain") -> list[dict[str, Any]]:
    """加载回放脚本:plain=script.json;graph=graph-script.json(分阶段)。"""
    if bug.replay_script_path is None:
        raise TaskError(f"no replay script for {bug.id}")
    if kind == "graph":
        graph_script = bug.root / "replay" / "graph-script.json"
        if graph_script.exists():
            data = json.loads(graph_script.read_text(encoding="utf-8"))
            steps = data if isinstance(data, list) else data.get("steps", [])
            if steps:
                return steps
    data = json.loads(bug.replay_script_path.read_text(encoding="utf-8"))
    steps = data if isinstance(data, list) else data.get("steps", [])
    if not steps:
        raise TaskError(f"empty replay script for {bug.id}")
    return steps
