"""评测批次溯源(T10.1;E2 扩展溯源强制)。

目的:真实模型批次的报告必须自带"如何复现本批次"的取证口径——
provider/engine/backend/commit 全部来自运行产物本身,而不是报告作者手写。
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import get_settings

log = logging.getLogger(__name__)

# git 取证锚定在本仓库根:与进程 CWD 无关(API 可能从任意目录被拉起)
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_git(args: list[str], timeout: float = 10) -> str | None:
    """在仓库根执行一条只读 git 命令:成功返回 stdout,失败/超时/git 缺失返回 None。

    text 输出锁定 UTF-8(errors="replace"):Windows 区域编码(GBK)下解码含中文的
    diff 输出会在读取线程里抛 UnicodeDecodeError,stdout 静默变空——脏树指纹会
    因此少算"改动全文",失去识别脏树的效力(实测:修复前指纹只含状态条目名单)。
    降级口径由调用方决定(现状:git_commit 返 "unknown",脏树取证返 None=无法判定)。
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def git_commit() -> str:
    """当前代码 commit;非 git 环境(如裁剪过的容器)返回 "unknown" 并告警(E2)。

    空 commit 会让批次报告失去复现锚点,审计时无从对账——宁可显式暴露
    unknown 也不能让缺溯源的记录看起来"正常"。
    """
    out = _run_git(["rev-parse", "HEAD"])
    if out is None:
        log.warning("provenance: git rev-parse failed (git unavailable/rc!=0), commit=unknown")
        return "unknown"
    return out.strip()


def worktree_dirty() -> bool | None:
    """工作树是否有**未提交的 tracked 改动**(P3-2);git 不可用/命令失败返回
    None = 无法判定。

    语义边界(实测校准):只统计 tracked 文件的修改/暂存/重命名/删除;
    **未跟踪文件不计入**——它们不影响"锚点 commit 能否复现本批"这一判据
    (本仓库长期存在未跟踪的 .idea//demo/,若计入则本标志永远为真、失去操作性);
    未跟踪内容若恰是批次输入,由 manifest 的 input_anchor_missing 暴露。

    证据链意义:脏工作树跑出的批次,`git_commit` 锚点不能代表实际执行的输入——
    fake36 批即跑在"E5 已迁移文件、ca17ccc 未提交"的脏树上,锚点 commit 上
    不存在所跑的 7 道新题(审计 R1-Q2-1)。**None 与 False 必须区分**:
    无法判定不得被读成"干净"。
    """
    out = _run_git(["status", "--porcelain"])
    if out is None:
        return None
    return any(line.strip() and not line.startswith("??") for line in out.splitlines())


def dirty_fingerprint() -> str:
    """脏树指纹(P3-2):sha256(状态条目名单 + tracked 改动全文)前 16 位;
    **当且仅当 worktree_dirty 为 True 时非空**(干净/仅未跟踪/无法判定 → 空串)。

    指纹相同 ⇒ 状态条目与 tracked 改动相同,足以识别"是哪棵脏树";
    口径边界如实声明:未跟踪文件只进名单不进内容,指纹不用于复现未跟踪内容。
    """
    porcelain = _run_git(["status", "--porcelain"])
    if porcelain is None or worktree_dirty() is not True:
        return ""
    tracked_diff = _run_git(["diff", "HEAD"]) or ""
    digest = hashlib.sha256((porcelain + "\n" + tracked_diff).encode("utf-8")).hexdigest()
    return digest[:16]


def missing_inputs_at_commit(commit: str, bug_ids: list[str]) -> list[str] | None:
    """manifest 落盘前的输入存在性反查(P3-2):每个题目录必须存在于锚点 commit。

    返回锚点 commit 上缺失的题 id 列表(空列表 = 全部存在);
    commit 为 unknown 或命令失败返回 None = 未检查(与"检查通过"区分)。
    每批 1 次 subprocess:`git ls-tree -r --name-only <commit> -- bugs/`。
    """
    if not commit or commit == "unknown":
        return None
    out = _run_git(["ls-tree", "-r", "--name-only", commit, "--", "bugs/"])
    if out is None:
        return None
    present: set[str] = set()
    for line in out.splitlines():
        parts = line.strip().split("/")
        if len(parts) >= 2 and parts[0] == "bugs":
            present.add(parts[1])
    return [bug_id for bug_id in bug_ids if bug_id not in present]


# P3-11:Settings 配置键三分类。每个 Settings 键必须归入其一,由
# tests/test_driver.py 的分类快照测试钉住——新增 Settings 键不三选一即 CI 失败:
# - SNAPSHOT_KEYS:影响复现与判定,必须进 config_snapshot(E2 遗漏的
#   verify_double_run 经 git 考古证实是漏,此处补上);
# - SECRET_KEYS:任何情况下不得进快照(report.json 会被汇编进 docs 随仓库分发);
# - EXEMPT_KEYS:声明为与复现判定无关/机器相关,缺席是设计使然。
# max_turns 不是 Settings 键(批次级参数):复盘 P0-3 起作为独立字段随
# build_provenance 记录,不再依赖 config_snapshot 覆盖。
SNAPSHOT_KEYS: tuple[str, ...] = (
    "llm_model",
    "llm_enabled",
    "execution_backend",
    "test_timeout_seconds",
    "llm_timeout_seconds",
    "token_budget",
    "max_patch_files",
    "verify_double_run",
    "task_timeout_seconds",
    "default_max_rounds",
    # 卡5 自适应分支:这三项直接决定轨迹形状与成本(同一题开不开分支不可比),
    # 故进快照而非豁免
    "adaptive_branching_enabled",
    "branch_candidates",
    "branching_min_token_reserve",
    # 定位段份额同样决定轨迹形状(份额小→更早降级取暂定结论),跨批次必须可比
    "localize_budget_share",
)
SECRET_KEYS: frozenset[str] = frozenset({"llm_api_key", "api_token"})
EXEMPT_KEYS: frozenset[str] = frozenset(
    {
        "runs_root",
        "db_path",
        "llm_base_url",
        "llm_max_tokens",
        "llm_max_retries",
        "llm_thinking",
        "redis_url",
        "docker_image",
        "allowed_repo_roots",
        "price_overrides",
        "log_level",
        "max_read_lines",
        "max_search_results",
        "max_output_chars",
        # 模型可见层的输出折叠头尾行数:与 max_output_chars 同族(只影响喂给模型的
        # 文本长度,不改判定与复现口径),故同样缺席快照
        "refine_head_lines",
        "refine_tail_lines",
        "task_max_workers",
        "recycle_finished_workspace",
        "recycle_grace_seconds",
    }
)


def config_snapshot() -> dict[str, Any]:
    """批次取证用的配置白名单快照(E2/P3-11):只收影响复现与判定的键。"""
    settings = get_settings()
    return {key: getattr(settings, key) for key in SNAPSHOT_KEYS}


def require_model_name(model_name: str | None, llm_enabled: bool) -> None:
    """fail-fast 守卫(E2):真实评测(llm_enabled=True)必须显式携带 model_name。

    缺名字的批次生不出可追溯的报告(审计 P0-2:runs/real 28 份 report.json
    model_name 全空)——在发起任何任务之前拒绝,避免花了钱买回不可信记录。
    """
    if llm_enabled and not (model_name or "").strip():
        raise ValueError(
            "真实评测必须带 model_name:PATCHPILOT_LLM_ENABLED=true 时,"
            "缺 model_name 的任务/批次拒绝发起(否则 report.json 无模型身份,证据链断裂)。"
        )


def build_provenance(
    model_provider: str,
    model_name: str,
    engine: str,
    arm: str = "agent",
    max_turns: int | None = None,
) -> dict[str, Any]:
    """任务开始时取证:同样配置能否复跑,取决于这里的字段是否被记录。

    P3-2:外加工作树状态(worktree_dirty/dirty_fingerprint)——锚点 commit
    单独一项不足以复现脏树批次;None = 无法判定,空指纹 = 干净。

    arm 记录的是"执行体"(agent 循环 / 消融对照臂);它不是 engine 的一部分,
    因为两臂跑的是同一个引擎,区别只在循环形状。

    复盘 P0-3:max_turns 是批次级参数(非 Settings 键),由调用方传入运行时
    实际值;temperature/seed 当前客户端从不设置(走提供方默认),显式记 None
    表明"采样不可重放"这一事实,不猜测数值。
    """
    settings = get_settings()
    return {
        "git_commit": git_commit(),
        "model_provider": model_provider,
        "model_name": model_name,
        "engine": engine,
        "arm": arm,
        "max_turns": max_turns,
        "temperature": None,
        "seed": None,
        "execution_backend": settings.execution_backend,
        "llm_enabled": settings.llm_enabled,
        "llm_thinking": settings.llm_thinking,
        "token_budget": settings.token_budget,
        "generated_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
        # E2:配置快照与任务起始时间——复现口径从"散落的平级字段"收敛为
        # 一份白名单字典,started_at 与批次 manifest 的 started_at 同口径
        "config_snapshot": config_snapshot(),
        "started_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "worktree_dirty": worktree_dirty(),
        "dirty_fingerprint": dirty_fingerprint(),
    }
