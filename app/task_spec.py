"""规范任务契约 TaskSpec(S05a/F4 前半,ADR-0009 §4)。

缺陷现场(review F4,P1):custom 任务 ID 把 failed/regression/allowed_paths 的元素
**裸拼接**后散列——failed=[a],regression=[b,c] 与 failed=[a,b],regression=[c]
得到同一个 ID;持久化只存截到 500 字符的 issue,完整测试清单/回放脚本不落盘,
任务不可忠实重建。

本模块是任务身份的唯一权威(spec S05,两卡共用同一套哈希算法,不重复定义):

- **具名 JSON**:字段边界由 JSON 对象结构表达,绝不用分隔符拼接;canonical 形态 =
  `json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`,
  对其 UTF-8 字节取 SHA256;
- **内容身份不含时间**:`created_at` 与 `task_spec_hash` 本身不入哈希载荷;
- **源码身份**:S05a 阶段绑定"源目录内容指纹"(实际文件字节,不随 mtime,
  不跟随符号链接,排除 .git);S06 冻结快照落地后由快照身份取代——在那天之前
  不可声称已有不可变快照;
- 测试列表**保留实际执行顺序**(顺序变化 = 身份变化);allowed_paths 空列表按
  既有 None 语义规范化(不趁机改 scope);
- 回放脚本完整入约(fake);真实模型为 None;
- 受理时的有效策略整份冻结(恢复不得重读 .env 换模型/预算)。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.errors import TaskError
from app.prompts import (
    LOCALIZE_PROMPT,
    ONE_SHOT_PROPOSE_PROMPT,
    PLAN_PROMPT,
    PROPOSE_PROMPT,
    SYSTEM_PROMPT,
)

if TYPE_CHECKING:
    from app.evals.bugset import BugTask

SCHEMA_VERSION = 1
# 规则版本:判定语义变更时必须递增(ADR-0009 §6 同源纪律),旧证据按旧版本追溯
GATE_POLICY_VERSION = "1"
TEST_POLICY_VERSION = "1"
# 指纹排除的目录名:来源仓库的版本库元数据不是被诊断的代码
_FINGERPRINT_SKIP_DIRS = frozenset({".git"})


def canonical_json(obj: Any) -> str:
    """任务的规范序列化形态(哈希与落盘共用,杜绝两种口径)。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def prompt_set_hash() -> str:
    """提示词模板指纹:模板原文进身份,改提示词 = 改实验条件。"""
    return stable_hash(
        {
            "system": SYSTEM_PROMPT,
            "localize": LOCALIZE_PROMPT,
            "propose": PROPOSE_PROMPT,
            "plan": PLAN_PROMPT,
            "one_shot_propose": ONE_SHOT_PROPOSE_PROMPT,
        }
    )


def fingerprint_source_dir(root: Path | str) -> str:
    """源目录内容指纹:排序的 (相对路径, 类型, 内容摘要) 清单再取总哈希。

    - 只看**实际文件字节**:mtime/权限变化不改变身份;删除/新增/内容修改都会改变;
    - 不跟随符号链接(链接按 link 记录,不读目标——不得借链接读工作区外文件);
    - 排除 `.git`(版本库元数据不是被诊断的代码);
    - 不可读的文件按不可读记录,不静默跳过(缺文件与读不了都要留痕)。
    """
    entries: list[list[str]] = []
    base = Path(root)
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in _FINGERPRINT_SKIP_DIRS)
        rel_dir = Path(dirpath).relative_to(base).as_posix()
        for name in sorted(filenames):
            rel = f"{rel_dir}/{name}" if rel_dir != "." else name
            full = Path(dirpath) / name
            if full.is_symlink():
                entries.append([rel, "link", ""])
                continue
            try:
                digest = hashlib.sha256(full.read_bytes()).hexdigest()
            except OSError as exc:
                entries.append([rel, "unreadable", f"{type(exc).__name__}"])
                continue
            entries.append([rel, "file", digest])
    return stable_hash({"root_version": 1, "entries": entries})


def read_source_commit(repo_dir: Path | str) -> str | None:
    """来源 git commit(辅助身份);无 .git 或读取失败返回 None,不编造。"""
    if not (Path(repo_dir) / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    commit = (out.stdout or "").strip()
    return commit if out.returncode == 0 and commit else None


def validate_budget_policy(settings: Any, model_name: str, *, online: bool) -> None:
    """按 §5 检查预算模式的可执行性;不满足就在**受理处**拒绝,不发起任何请求。

    为什么在受理时判而不是跑到一半再判:`cost|hybrid` 的意义是"现金上限真的会拦",
    没有价目就没有上限可算,而跑到一半才发现等于已经花出去了。
    `online=False`(fake 回放)不受此约束 —— 零花费路径不该被价目表缺失挡住,
    否则离线验收与既有回放批次全得停摆。
    """
    mode = str(getattr(settings, "budget_mode", "tokens") or "tokens")
    if mode == "tokens":
        return
    if not online:
        return
    from app.evals.pricing import price_card_for

    limit = int(getattr(settings, "cost_budget_micro_usd", 0) or 0)
    if limit <= 0:
        raise TaskError(
            f"budget_mode={mode} requires cost_budget_micro_usd > 0;"
            " refusing to accept an online task with an unbounded money limit"
            "(没有金额上限的现金闸不是闸,只会让人以为它拦过)"
        )
    if price_card_for(model_name) is None:
        raise TaskError(
            f"budget_mode={mode} has no known price for model '{model_name}';"
            " 拒绝受理(不自动联网查价,也不猜价目)"
        )


def _effective_policy(settings: Any) -> dict[str, Any]:
    """受理时冻结的有效策略(全部具名;getattr 兜底让测试替身落在关闭/默认侧)。"""
    keys = (
        "token_budget",
        "budget_mode",
        "cost_budget_micro_usd",
        "max_model_calls",
        "model_context_tokens",
        "capacity_margin_tokens",
        "task_timeout_seconds",
        "test_timeout_seconds",
        "max_patch_files",
        "max_read_lines",
        "max_search_results",
        "max_output_chars",
        "context_window_tokens",
        "context_keep_recent_turns",
        "token_estimate_factor",
        "localize_budget_share",
        "plan_budget_share",
        "plan_stage_enabled",
        "adaptive_branching_enabled",
        "branch_candidates",
        "loop_snapshot_enabled",
        "resume_on_restart",
        "repo_map_enabled",
        "search_engine",
        "execution_backend",
        # G07 能力声明与运行 profile:受理时就冻结"这一发请求被允许多大、按哪套口径压历史"。
        # 只记声明与来源,不记观测:返回模型名(served_model)是逐次响应的观测事实,
        # 落在轨迹的 llm 事件里,不在这份契约里 —— 发起前不可能知道,写了就是假事实。
        "agent_runtime_profile",
        "model_capacity_source",
        "model_capabilities",
        "model_declared_output_ceiling",
        "llm_platform_retries",
        # G08:仓库自带说明进不进请求、按哪两档字节冻结(每份说明的路径/哈希/作用域/裁剪
        # 读数在轨迹的 repo_instructions_loaded 事件里,不在这里重复)
        "repo_instructions_enabled",
        "repo_instructions_max_file_bytes",
        "repo_instructions_max_total_bytes",
    )
    policy = {k: getattr(settings, k, None) for k in keys}
    # 有效值而不是配置值:repair_v2 的目标窗口会被声明容量夹小,报告里必须写出**实际采用**
    # 的那个数(以及它被夹过这件事),否则"配置写着 64k"会被读成"跑了 64k"。
    from app.agent.runtime import TaskRuntime

    policy["runtime_profile"] = TaskRuntime.build(settings).as_dict()
    return policy


@dataclass(frozen=True)
class TaskSpec:
    """一个任务的完整可重建契约(schema_version=1)。"""

    # —— 输入 ——
    source_kind: str  # manifest | custom_dir | snapshot(S06)
    issue_text: str  # 完整原文,永不截断
    failed_tests: list[str]  # 实际执行顺序
    regression_tests: list[str]
    allowed_paths: list[str] | None  # None = 不设白名单(空列表按 None 规范化)
    # —— 源码 ——
    source_path: str  # 解析后的绝对路径(逻辑定位符;custom 为用户原始目录)
    source_commit: str | None  # 可空:fixture 无 .git 不编造
    source_snapshot_hash: str  # S05a=目录内容指纹;S06 起为冻结快照身份
    # —— 执行 ——
    engine: str
    arm: str
    model_provider: str
    model_name: str  # 真实模型名;fake 回放为空串
    max_rounds: int
    max_turns: int
    effective_policy: dict[str, Any] = field(default_factory=dict)
    # —— 环境 ——
    trusted_env: dict[str, Any] = field(default_factory=dict)
    gate_policy_version: str = GATE_POLICY_VERSION
    test_policy_version: str = TEST_POLICY_VERSION
    verify_double_run: bool = True
    # —— 回放 ——
    replay: list[dict[str, Any]] | None = None  # fake=完整脚本;真实模型=None
    # —— 溯源(不参与内容身份的部分单列在 content_dict 之外) ——
    platform_commit: str = ""
    prompts_hash: str = ""
    # S06:冻结副本相对 run_dir 的引用(常量 "source_snapshot")——执行与恢复
    # 只读物化这份副本;空串=未冻结(manifest 题,平台自有目录)。
    # 引用是常量而非绝对路径,同输入重提的哈希不受时间戳影响(幂等键稳定)。
    source_snapshot_ref: str = ""

    def content_dict(self) -> dict[str, Any]:
        """参与内容身份的具名字典(created_at/task_spec_hash 刻意不在内)。"""
        data = asdict(self)
        data.pop("task_spec_hash", None)
        return data

    @property
    def task_spec_hash(self) -> str:
        return stable_hash(self.content_dict())

    def to_json_dict(self) -> dict[str, Any]:
        """落盘/入库形态:内容 + 身份 + 记录时间(时间不参与身份)。"""
        return {
            **self.content_dict(),
            "schema_version": SCHEMA_VERSION,
            "task_spec_hash": self.task_spec_hash,
            "created_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
        }

    @staticmethod
    def from_json_dict(data: dict[str, Any]) -> TaskSpec:
        """从落盘形态重建;schema_version 不符即拒绝(不猜旧数据)。"""
        if int(data.get("schema_version", 0)) != SCHEMA_VERSION:
            raise ValueError(f"unsupported task_spec schema_version: {data.get('schema_version')}")
        fields = set(TaskSpec.__dataclass_fields__)
        missing = sorted(fields - set(data))
        if missing:
            raise ValueError(f"task_spec missing fields: {missing}")
        kwargs = {name: data[name] for name in fields}
        spec = TaskSpec(**kwargs)
        recorded = data.get("task_spec_hash")
        if recorded and recorded != spec.task_spec_hash:
            raise ValueError(
                f"task_spec_hash mismatch: recorded {recorded} != computed {spec.task_spec_hash}"
            )
        return spec

    @staticmethod
    def read_file(path: Path | str) -> TaskSpec:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return TaskSpec.from_json_dict(data)


def build_task_spec(
    bug: BugTask,
    *,
    engine: str,
    arm: str,
    model_provider: str,
    model_name: str = "",
    max_turns: int,
    max_rounds: int | None = None,
    replay: list[dict[str, Any]] | None = None,
    settings: Any = None,
    platform_commit: str = "",
    source_snapshot_ref: str = "",
) -> TaskSpec:
    """从 BugTask + 执行参数构建冻结契约(S05a 唯一构建口径,两引擎共用)。

    replay 语义:fake 回放必须给完整脚本(任务身份的一部分);
    真实模型传 None。allowed_paths 的空列表按 None 规范化(与既有 scope 语义一致)。
    """
    if settings is None:
        from app.config import get_settings

        settings = get_settings()
    # 预算模式的可执行性在受理口径上判(契约建不起来就不会有后续请求)
    validate_budget_policy(
        settings,
        model_name,
        online=bool(getattr(settings, "llm_enabled", False)) and model_provider != "fake-replay",
    )
    allowed = bug.allowed_paths
    if allowed is not None and not allowed:
        allowed = None
    from app.evals.provenance import git_commit

    return TaskSpec(
        source_kind="manifest" if bug.category != "custom" else "custom_dir",
        issue_text=bug.issue_text,
        failed_tests=list(bug.failed_tests),
        regression_tests=list(bug.regression_tests),
        allowed_paths=allowed,
        source_path=str(Path(bug.repo_dir).resolve()),
        source_commit=read_source_commit(bug.repo_dir),
        source_snapshot_hash=fingerprint_source_dir(bug.repo_dir),
        source_snapshot_ref=source_snapshot_ref,
        engine=engine,
        arm=arm,
        model_provider=model_provider,
        model_name=model_name,
        max_rounds=int(max_rounds if max_rounds is not None else bug.max_rounds),
        max_turns=int(max_turns),
        effective_policy=_effective_policy(settings),
        trusted_env={
            "execution_backend": getattr(settings, "execution_backend", "local"),
            "docker_image": getattr(settings, "docker_image", ""),
            "platform_python": sys.executable,
        },
        verify_double_run=bool(getattr(settings, "verify_double_run", True)),
        replay=list(replay) if replay is not None else None,
        platform_commit=platform_commit or (git_commit() or ""),
        prompts_hash=prompt_set_hash(),
    )


def write_task_spec_file(run_dir: Path | str, spec: TaskSpec) -> str:
    """原子写 run_dir/task_spec.json,返回 task_spec_hash(tmp+os.replace,轮询方不会读到半截)。"""
    payload = canonical_json(spec.to_json_dict())
    path = Path(run_dir) / "task_spec.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)
    return spec.task_spec_hash
