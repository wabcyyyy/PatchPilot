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

# G04:provenance 的结构版本。新增控制身份分节必须升它——旧批次报告没有这些节,
# 比较器对"跨结构版本的批次互比"必须拒绝,而不是把缺席字段读成"相同"。
PROVENANCE_SCHEMA_VERSION = 2


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
    # G03 预算模式与新增维度:它们决定"在哪一次请求上停手",同题跨模式不可比,
    # 属于运行身份而非复现豁免(价目本身另由 pricing.PRICING_VERSION 记,见 G04)
    "budget_mode",
    "cost_budget_micro_usd",
    "max_model_calls",
    "model_context_tokens",
    "capacity_margin_tokens",
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
    # M1 上下文压缩:软阈值与尾部钉住轮数直接决定"模型第 k 轮看到多少历史",
    # 同题开/关压缩的轨迹不可比(与 localize_budget_share 同类,而非 refine_* 同类——
    # 后者只截单条工具回执,前者会整段丢弃历史),故进快照而非豁免
    "context_window_tokens",
    "context_keep_recent_turns",
    # M17 额度门禁的单位换算:这个系数决定"闸门在哪一次请求上判死",同一题 1.0 与 1.47
    # 的轨迹长度、终态与成本都不可比(它不比 refine_* 那类只截单条回执的键轻),故进快照
    "token_estimate_factor",
    # M2 仓库骨架:这三键决定"模型在第一轮就看到哪些文件与符号",同题开/关骨架的轨迹
    # 不可比(与 context_window_tokens 同类:改变的是模型可见上下文本身,不是单条回执);
    # 缺省即"结构从未注入"(PROGRESS.md D.4),付费实跑因此把额度花在重新发现仓库上
    "repo_map_enabled",
    "repo_map_max_chars",
    # 文件数上限同样改变可见内容(裁掉的文件模型就看不见),且成本可忽略,一并进快照
    "repo_map_max_files",
    # 目录汇总深度:大仓库里它决定模型看得见多细的结构档,同上不可比
    "repo_map_dir_depth",
    # M3 检索引擎:这三键决定"模型每一轮检索看到什么内容"——引擎(rg 与 Python 的输出被
    # 等价性用例钉成同一份,但回落与否仍是轨迹差异)、上下文档位(直接决定可见行数)、
    # 符号条数上限(决定 find_symbol 给几条)。与 repo_map_* 同族(改变模型可见上下文本身),
    # 不进豁免:同题换引擎/换档位的两次跑法不可比
    "search_engine",
    "max_search_context_lines",
    "max_symbol_results",
    # M3.6:遍历域上限决定"哪些文件在模型的可见范围内"——同一题换这个值就是换仓库大小,
    # 两次跑法不可比(裁过的仓库会被工具如实标 scope_truncated,但结论仍不同)
    "max_search_files",
    # M5 PLAN 阶段:这两键直接决定"模型在动手前看到什么、多做一次什么请求"——
    # 开关改变提示与轨迹形状(有无计划块、有无一次额外请求),份额改变计划段能花多少额度
    # (份额小→更早降级为空/暂定计划)。与 localize_budget_share 同族,不进豁免:
    # 同题开/关计划阶段的两次跑法不可比
    "plan_stage_enabled",
    "plan_budget_share",
    # M6 崩溃恢复:loop_snapshot_enabled 决定"续跑的那次运行看到哪些工作记忆与累计量",
    # resume_on_restart 决定僵尸任务被收敛成 NEEDS_REVIEW 还是重新入队续跑——两者都改变
    # 复现口径与终态,与 context_window_tokens 同族(改变运行看到什么),不进豁免
    "loop_snapshot_enabled",
    "resume_on_restart",
    # G05 有界视图与证据仓:这五键决定"模型一屏 receipt 里能看到多少行、被裁掉的原文
    # 还能不能取回来"。换任何一个数都是另一种运行条件(与 refine_head_lines 同族,而
    # G04 已把那一族从豁免里挪出来了),尤其 evidence_enabled=false 会让"取回"这件事
    # 从能力变成不可用 —— 绝不能留在豁免里悄悄改变两臂的可见面。
    "tool_visible_max_chars",
    "evidence_enabled",
    "evidence_max_item_bytes",
    "evidence_max_total_bytes",
    "evidence_page_bytes",
    # G06 连续会话与任务记忆:开关决定"下一阶段的请求里有没有上一阶段的观测",
    # 预算决定这块能占多少字符 —— 两者都改变模型可见上下文本身,与 context_window_tokens
    # 同族,不是运维豁免(同一题开/关记忆的两次跑法不可比)
    "task_memory_enabled",
    "task_memory_max_chars",
    # G07 能力声明与显式重试:声明的容量来源决定"我们相信这个端点能装多少"(没有来源就
    # 只能落保守档),能力清单决定工具/思考/缓存明细是否被声明,输出上界决定预留与请求参数,
    # 显式重试次数决定在途消耗的形态与可观测性(与 llm_max_retries 同族)。
    # 四条都是运行身份,不是豁免:换来源/换清单就是换一批可以合法发起的请求。
    "model_capacity_source",
    "model_capabilities",
    "model_declared_output_ceiling",
    "llm_platform_retries",
    # G07 运行 profile 名:它是"这一批用的是哪一套窗口/轮数/预留口径"的总开关,换 profile
    # 就是换运行条件。它**不进** protocol_identity —— v1/v2 对照正是要把它作为登记变量差出来
    # (与 context_window_tokens 同一处置:额度/窗口类旋钮在快照里,协议形状才进控制身份)。
    "agent_runtime_profile",
    # G08 仓库说明:开关决定"目标仓库写给 Agent 的规则有没有进请求",两档字节决定能进多少。
    # 与 task_memory_* 同族(改的是模型可见上下文本身),不是运维豁免。
    "repo_instructions_enabled",
    "repo_instructions_max_file_bytes",
    "repo_instructions_max_total_bytes",
    # G09 受控命令:回退超时、单条流原始输出的落盘额度、模型可见 tail 字符数、scratch
    # 原始文件的总量额度——四个都决定"一条命令在回执里长什么样、原文能不能取回"。
    # 与 tool_visible_max_chars/evidence_* 同族(改变模型可见面与取证面),不进豁免;
    # profile 集本身的身份由 effective_policy.command_profiles_digest 单独记(它不是 Settings 键)。
    "command_timeout_seconds",
    "command_output_max_bytes",
    "command_tail_chars",
    "command_scratch_max_bytes",
    # G10 验收等待窗:决定 diagnose_then_repair 是"等待批准"还是"立即 needs_review",
    # 两种口径的终态与产物形状不同(与 task_timeout_seconds 同族,不进豁免)。
    "acceptance_wait_seconds",
    # G11 用户等待窗与绝对寿命:两者决定"等待中的任务是继续等还是结构化 needs_review"、
    # 以及等待顺延能不能越过绝对上限——同样改变终态与轨迹形状,与 acceptance_wait_seconds
    # 同族,不进豁免。
    "user_wait_seconds",
    "task_absolute_lifetime_seconds",
    # G13 轻量角色委派:总开关决定"这条轨迹里有没有子任务在跑",并发/次数/轮次上限决定
    # 轨迹的形状与成本(子任务数、并行度、每子任务烧多少),份额决定"子任务合计能花多少
    # 额度"、模型决定子任务的身份与价目——全部属于运行身份,同一题开/关不可比,故进快照
    "delegation_enabled",
    "max_subagent_concurrency",
    "max_delegations",
    "subtask_budget_share",
    "subtask_max_turns",
    "subtask_model",
    # G14 有界停止:同一组失败连续多少轮后收敛 NEEDS_REVIEW,决定轨迹在哪个轮次、
    # 以什么终态收尾(改这个数就是改运行条件,与 task_timeout_seconds 同族,不进豁免)
    "max_identical_failures",
    # P 线补分类(2026-10-16,CI 红清单):容器资源/安全限额是执行包络的一部分,
    # 限额大小能翻转 verify 结果(OOM/PID 耗尽杀掉测试进程),与 docker_image 同族进快照
    "docker_cpus",
    "docker_memory",
    "docker_pids_limit",
    "docker_cap_drop",
    "docker_security_opt",
    # deployment_mode 决定鉴权/受信镜像等强制面的开与关,同一任务在两种模式下
    # 连"能否被受理"都不同,属运行身份而非运维豁免
    "deployment_mode",
    # 任务级美元预算上限:决定"在哪一次请求上停手",与 cost_budget_micro_usd 同族
    "task_budget_cap_usd",
    # 主体的日累计额度:超限会改变批内后续任务的停手点与终态,进快照可解释
    # "为什么这次停在半途"
    "principal_daily_budget_usd",
    "principal_daily_token_budget",
)
SECRET_KEYS: frozenset[str] = frozenset({"llm_api_key", "api_token"})
EXEMPT_KEYS: frozenset[str] = frozenset(
    {
        "runs_root",
        "db_path",
        # P 线补分类(2026-10-16,CI 红清单):
        # 受理/准入边界:超限在受理即拒绝,不会产生"跑了但不可比"的轨迹,
        # 与 intake_timeout_seconds/intake_max_files 同族
        "intake_max_file_bytes",
        "intake_max_total_bytes",
        "intake_min_free_disk_bytes",
        "trusted_docker_images",
        "allowed_model_endpoints",
        "admission_queue_limit",
        "admission_timeout_seconds",
        "emergency_stop",
        # 部署拓扑与吞吐旋钮:改并发/轮询/进程形态不改单任务轨迹,
        # 与 task_max_workers 同族
        "admission_concurrency_limit",
        "worker_enabled",
        "job_lease_duration_seconds",
        "job_poll_interval_seconds",
        # 可观测性与运维生命周期:与 log_level/recycle_* 同族
        "log_format",
        "readiness_min_disk_free_bytes",
        "recycle_dry_run_only",
        "recycle_evidence_retention_days",
        "recycle_failed_retention_days",
        # 治理标签:声明数据处理口径的版本,不参与执行与判定路径
        "data_policy_version",
        # 以下为 P 线之前既有豁免
        # S06 受理冻结的体量/耗时保护:运维边界而非复现口径(超限在受理即拒绝,
        # 不会产生"跑了但不可比"的轨迹),与 runs_root/db_path 同族进豁免
        "intake_timeout_seconds",
        "intake_max_files",
        # 原始 base_url 可能内嵌凭据(query token / userinfo),**值**永不进快照;
        # 它的去敏身份由 endpoint_identity() 单独记(G04)
        "llm_base_url",
        "redis_url",
        "allowed_repo_roots",
        # 价目覆盖文件的**路径**是机器相关的本地信息,值不进快照;
        # 它是否被使用、内容是什么,由 pricing_identity() 以 hash 形式登记(G04)
        "price_overrides",
        "log_level",
        "task_max_workers",
        "recycle_finished_workspace",
        "recycle_grace_seconds",
    }
)


# G04 重分类:以下键此前躺在 EXEMPT 里,理由是"只影响喂给模型的文本长度,不改判定与
# 复现口径"。这个理由站不住:它们决定模型**能看到多少、能写多少、一次请求花多少**,
# 换个数就是另一种运行条件(审查 R5 的"遗漏配置"正指这一类)。现在全部进快照。
# SPEC §7 G04:"Settings 键必须分类,文本裁剪/输出上限不是复现豁免"。
RECLASSIFIED_TO_SNAPSHOT: tuple[str, ...] = (
    "llm_max_tokens",  # 单次输出上限:决定补丁能否完整写出,也决定输出预留
    "llm_max_retries",  # 重试上界:决定在途消耗的形态与可观测性(G02)
    "llm_thinking",  # 思考档位:改变请求体与回传协议,也改变思维链占的容量
    "max_read_lines",
    "max_search_results",
    "max_output_chars",
    "refine_head_lines",
    "refine_tail_lines",
    "docker_image",  # 执行环境身份(容器身份;digest 另由 container_identity 记)
)

SNAPSHOT_KEYS = SNAPSHOT_KEYS + RECLASSIFIED_TO_SNAPSHOT


def endpoint_identity(base_url: str) -> dict[str, Any]:
    """端点的去敏身份:规范化 host+path,原始值的 hash 用来发现"只改了 query 的漂移"。

    绝不含 userinfo、query 与任何 token(SPEC §4 持久化 / T24)。空 URL 如实记 absent。
    """
    from urllib.parse import urlsplit

    if not base_url:
        return {"present": False, "host": "", "path": "", "url_sha256": ""}
    parts = urlsplit(base_url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    path = (parts.path or "").rstrip("/")
    digest = hashlib.sha256(base_url.encode("utf-8")).hexdigest()
    return {
        "present": True,
        "scheme": parts.scheme,
        "host": host,
        "path": path,
        # query/userinfo 被丢弃是去敏;整条 URL 的 hash 保留"换了 query 也算换端点"的能力
        "url_sha256": digest[:16],
    }


def pricing_identity() -> dict[str, Any]:
    """价目与覆盖文件的身份:版本常量 + 覆盖内容 hash(不写路径,不写价格明文)。"""
    from app.config import get_settings as _get_settings
    from app.evals.pricing import PRICING_VERSION

    path = _get_settings().price_overrides
    digest = ""
    if path:
        try:
            digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
        except OSError:
            digest = "unreadable"
    return {
        "pricing_version": PRICING_VERSION,
        "overrides_present": bool(path),
        "overrides_sha256": digest,
    }


def tool_schema_identity() -> dict[str, Any]:
    """模型可见工具 schema 的指纹:换工具或换参数形状 = 换运行条件(§G04 请求/工具协议)。"""
    from app.task_spec import stable_hash
    from app.tools.registry import REGISTRY, tool_schemas

    return {
        "tool_names": sorted(REGISTRY),
        "schema_sha256": stable_hash(tool_schemas())[:16],
    }


def prompt_identity() -> dict[str, Any]:
    from app.task_spec import prompt_set_hash

    return {"prompts_sha256": prompt_set_hash()[:16]}


def container_identity(settings: Any | None = None) -> dict[str, Any]:
    """容器身份:镜像引用 + 本机 daemon 报出的 digest。

    只查本地 Docker 元数据,**不联网、不拉取镜像**;daemon 不可用或查不到时如实记
    `digest_status=unavailable`(与"检查通过"区分)。local 后端整段标 not_applicable,
    绝不把宿主执行读成"同一个容器环境"(§7 G09 的同一条口径)。
    """
    settings = settings if settings is not None else get_settings()
    image = settings.docker_image
    if settings.execution_backend != "docker":
        return {
            "backend": settings.execution_backend,
            "image": image,
            "digest_status": "not_applicable",
            "digest": "",
        }
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{index .RepoDigests 0}}", image],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"backend": "docker", "image": image, "digest_status": "unavailable", "digest": ""}
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out or out == "[]":
        # 本地构建的镜像常常没有 RepoDigests:这不是"检查失败",如实写成 tag_only
        return {"backend": "docker", "image": image, "digest_status": "tag_only", "digest": ""}
    return {"backend": "docker", "image": image, "digest_status": "checked", "digest": out[:128]}


def model_identity(model_provider: str, model_name: str) -> dict[str, Any]:
    """请求侧模型身份 + 一句明确的不可证明声明。

    `alias_proves_served_version=false`:模型别名/名相同**不能**证明服务端版本没漂移
    (§7 G04)。这里是**发起前**的登记位,不可能知道返回名,所以如实记 None;
    G07 采集到的 `served_model` 是逐次响应的观测事实,落在轨迹的 llm 事件
    (`ModelResponseEnvelope.as_report_dict()` / `_record_thought`)与
    `app/llm/profiles.py` 的能力档案读数里,不回填到这里冒充"预登记时就已知"。
    """
    return {
        "provider": model_provider,
        "requested_model": model_name or None,
        "served_model": None,
        "alias_proves_served_version": False,
    }


def protocol_identity(
    arm: str, policy: Any | None = None, settings: Any | None = None
) -> dict[str, Any]:
    """请求/工具协议与策略指纹:两臂比较时除登记变量外必须逐字段相同。"""
    settings = settings if settings is not None else get_settings()
    described = None
    policy_hash = None
    if policy is not None:
        described = policy.describe() if hasattr(policy, "describe") else dict(policy)
        from app.task_spec import stable_hash

        policy_hash = stable_hash(described)[:16]
    return {
        "arm": arm,
        "experiment_policy": described,
        "experiment_policy_sha256": policy_hash,
        "budget_mode": settings.budget_mode,
        "llm_max_tokens": settings.llm_max_tokens,
        "llm_max_retries": settings.llm_max_retries,
        # G07:平台显式重试与能力声明属于**请求协议形状**(重试归属、tools/reasoning 字段
        # 是否出现在请求里)。容量的来源与输出上界不在这张表里 —— 它们与
        # `model_context_tokens` 同族(额度/容量旋钮,只在快照里),v1/v2 对照正要按臂登记它们。
        "llm_platform_retries": settings.llm_platform_retries,
        "model_capabilities": settings.model_capabilities,
        "llm_thinking": settings.llm_thinking,
        "refine_head_lines": settings.refine_head_lines,
        "refine_tail_lines": settings.refine_tail_lines,
        "max_output_chars": settings.max_output_chars,
        # G05:一屏 receipt 的可见上限与"被裁的行还能不能取回",都属于模型可见面
        "tool_visible_max_chars": settings.tool_visible_max_chars,
        "evidence_enabled": settings.evidence_enabled,
        "evidence_page_bytes": settings.evidence_page_bytes,
        # G06:阶段之间带不带工作记忆、带多少字符,同样是模型可见面
        "task_memory_enabled": settings.task_memory_enabled,
        "task_memory_max_chars": settings.task_memory_max_chars,
        # G08:仓库自带说明进不进请求、能占多少字节 —— 与 task_memory_* 同族
        "repo_instructions_enabled": settings.repo_instructions_enabled,
        "repo_instructions_max_file_bytes": settings.repo_instructions_max_file_bytes,
        "repo_instructions_max_total_bytes": settings.repo_instructions_max_total_bytes,
        "max_read_lines": settings.max_read_lines,
        "max_search_results": settings.max_search_results,
        **tool_schema_identity(),
        **prompt_identity(),
    }


def config_snapshot(settings: Any | None = None) -> dict[str, Any]:
    """批次取证用的配置白名单快照(E2/P3-11):只收影响复现与判定的键。"""
    settings = settings if settings is not None else get_settings()
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
    policy: Any | None = None,
    settings: Any | None = None,
) -> dict[str, Any]:
    """任务开始时取证:同样配置能否复跑,取决于这里的字段是否被记录。

    P3-2:外加工作树状态(worktree_dirty/dirty_fingerprint)——锚点 commit
    单独一项不足以复现脏树批次;None = 无法判定,空指纹 = 干净。

    arm 记录的是"执行体"(agent 循环 / 消融对照臂);它不是 engine 的一部分,
    因为两臂跑的是同一个引擎,区别只在循环形状。

    复盘 P0-3:max_turns 是批次级参数(非 Settings 键),由调用方传入运行时
    实际值;temperature/seed 当前客户端从不设置(走提供方默认),显式记 None
    表明"采样不可重放"这一事实,不猜测数值。

    G04 新增的控制身份(比较器逐字段核对,不许"结果相同就当配置相同"):
    model_identity / endpoint_identity / protocol_identity(含工具 schema 与提示词
    指纹、预算模式与裁剪档位、策略 hash)/ pricing_identity / container_identity。
    全部去敏:没有 key、没有 Authorization、没有 query token、没有思维链原文、
    没有本机绝对路径。
    """
    settings = settings if settings is not None else get_settings()
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
        "config_snapshot": config_snapshot(settings),
        "started_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "worktree_dirty": worktree_dirty(),
        "dirty_fingerprint": dirty_fingerprint(),
        # G04:控制身份分节登记(缺任何一节都是"不可比",不是"没这回事")
        "provenance_schema_version": PROVENANCE_SCHEMA_VERSION,
        "model_identity": model_identity(model_provider, model_name),
        "endpoint_identity": endpoint_identity(settings.llm_base_url),
        "protocol_identity": protocol_identity(arm, policy, settings),
        "pricing_identity": pricing_identity(),
        "container_identity": container_identity(settings),
        "budget_identity": {
            "budget_mode": settings.budget_mode,
            "token_budget": settings.token_budget,
            "cost_budget_micro_usd": settings.cost_budget_micro_usd,
            "max_model_calls": settings.max_model_calls,
            "model_context_tokens": settings.model_context_tokens,
        },
    }
