"""评测批次溯源(T10.1;E2 扩展溯源强制)。

目的:真实模型批次的报告必须自带"如何复现本批次"的取证口径——
provider/engine/backend/commit 全部来自运行产物本身,而不是报告作者手写。
"""

from __future__ import annotations

import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import get_settings

log = logging.getLogger(__name__)

# git 取证锚定在本仓库根:与进程 CWD 无关(API 可能从任意目录被拉起)
_REPO_ROOT = Path(__file__).resolve().parents[2]


def git_commit() -> str:
    """当前代码 commit;非 git 环境(如裁剪过的容器)返回 "unknown" 并告警(E2)。

    空 commit 会让批次报告失去复现锚点,审计时无从对账——宁可显式暴露
    unknown 也不能让缺溯源的记录看起来"正常"。
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=_REPO_ROOT, capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        log.warning("provenance: git rev-parse failed (git unavailable), commit=unknown")
        return "unknown"
    if proc.returncode != 0:
        log.warning("provenance: git rev-parse rc=%s, commit=unknown", proc.returncode)
        return "unknown"
    return proc.stdout.strip()


# P3-11:Settings 配置键三分类。每个 Settings 键必须归入其一,由
# tests/test_driver.py 的分类快照测试钉住——新增 Settings 键不三选一即 CI 失败:
# - SNAPSHOT_KEYS:影响复现与判定,必须进 config_snapshot(E2 遗漏的
#   verify_double_run 经 git 考古证实是漏,此处补上);
# - SECRET_KEYS:任何情况下不得进快照(report.json 会被汇编进 docs 随仓库分发);
# - EXEMPT_KEYS:声明为与复现判定无关/机器相关,缺席是设计使然。
# 已知结构性缺口(如实记录,不装已修):max_turns 不是 Settings 键,
# 升格属结构变更,另行评审。
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


def build_provenance(model_provider: str, model_name: str, engine: str) -> dict[str, Any]:
    """任务开始时取证:同样配置能否复跑,取决于这里的字段是否被记录。"""
    settings = get_settings()
    return {
        "git_commit": git_commit(),
        "model_provider": model_provider,
        "model_name": model_name,
        "engine": engine,
        "execution_backend": settings.execution_backend,
        "llm_enabled": settings.llm_enabled,
        "llm_thinking": settings.llm_thinking,
        "token_budget": settings.token_budget,
        "generated_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
        # E2:配置快照与任务起始时间——复现口径从"散落的平级字段"收敛为
        # 一份白名单字典,started_at 与批次 manifest 的 started_at 同口径
        "config_snapshot": config_snapshot(),
        "started_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
    }
