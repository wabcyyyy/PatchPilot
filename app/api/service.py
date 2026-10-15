"""任务服务:后台执行、幂等、状态流转与落库。

设计:
- 幂等键 = bug_id + engine + model(同键任务未到终态时直接返回原任务);
- 锁:Redis(可用时)或进程内兜底,防止同键任务并发执行;
- 崩溃恢复:服务启动时处理 RUNNING/QUEUED 僵尸任务——留有可用循环快照(M6)的 graph 任务
  **重新入队按检查点续跑**,其余仍按旧行为标记 NEEDS_REVIEW(Settings.resume_on_restart
  关闭时全部走旧行为);
- cancel:先置 CANCELLED,再经 CancelRegistry 通知执行线程在 turn 边界协作式中断
  (正在跑的一次 pytest/LLM 调用先完成),终态回写时让位于 CANCELLED,保留现场。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.agent.control import (
    KIND_ANSWER,
    KIND_STEER,
    ApprovalRegistry,
    InteractionChannel,
    InteractionIntegrityError,
    InteractionStore,
    QuestionRegistry,
)
from app.api.cancellation import CancelRegistry
from app.api.preflight import run_preflight
from app.api.recycle import recycle_run_dir
from app.config import get_settings
from app.errors import (
    ConflictError,
    InvalidRequestError,
    PatchPilotError,
    TaskCancelled,
    TaskError,
)
from app.evals.bugset import BUGS_ROOT, BugTask, build_custom_bug, load_bug, load_replay_script
from app.gitops.input_snapshot import SNAPSHOT_DIRNAME, freeze_input
from app.logctx import request_id_var, reset_task_context, set_task_context
from app.storage.locks import BaseLock, build_lock
from app.storage.repository import TERMINAL_STATUSES, Repository
from app.task_spec import (
    MODE_DIAGNOSE_THEN_REPAIR,
    MODE_REPAIR,
    SCHEMA_VERSION,
    TaskSpec,
    build_task_spec,
    canonical_json,
    write_task_spec_file,
)

if TYPE_CHECKING:  # 仅标注用:BugTask 已在运行时导入面之外,避免给服务层加导入负担
    from app.evals.bugset import BugTask

log = logging.getLogger(__name__)

# API 受理的任务与引擎默认轮内步数保持同一口径(runner/驱动器的默认值)
DEFAULT_MAX_TURNS = 20


def ensure_repo_allowed(resolved: Path, *, deployment_mode: str | None = None) -> None:
    """repo_path 根白名单(T12.2):配置 PATCHPILOT_ALLOWED_REPO_ROOTS(逗号分隔绝对
    路径)后,自定义任务只允许指向这些根之内;空配置在开发模式下不限制(个人本地模式,兼容现状),
    但在生产模式下强制要求配置白名单且目标路径必须在白名单内。

    比较经 os.path.normcase:Windows 路径大小写/斜杠不敏感,否则大小写不同的
    合法配置(如 `d:\\repos` vs `D:\\repos\\proj`)会被误拒。
    """
    settings = get_settings()
    mode = deployment_mode or settings.deployment_mode
    raw = settings.allowed_repo_roots
    if not raw:
        if mode == "production":
            raise InvalidRequestError(
                "production mode requires non-empty PATCHPILOT_ALLOWED_REPO_ROOTS"
            )
        return
    resolved_n = os.path.normcase(str(resolved))
    for part in raw.split(","):
        if not part.strip():
            continue
        root = Path(part.strip())
        if not root.is_absolute():
            raise InvalidRequestError(
                f"PATCHPILOT_ALLOWED_REPO_ROOTS entries must be absolute paths: {part!r}"
            )
        root_n = os.path.normcase(str(root.resolve()))
        if resolved_n == root_n or resolved_n.startswith(root_n + os.sep):
            return
    raise InvalidRequestError(f"repo_path outside allowed repo roots: {resolved}")


class TaskService:
    def __init__(
        self,
        *,
        repo: Repository,
        runs_root: Path | None = None,
        bugs_root: Path | str = BUGS_ROOT,
        redis_url: str = "",
        lock: BaseLock | None = None,
        max_workers: int | None = None,
    ) -> None:
        self.repo = repo
        self.runs_root = (runs_root or get_settings().runs_root).resolve()
        self.bugs_root = Path(bugs_root)
        self.lock = lock or build_lock(redis_url or get_settings().redis_url)
        self._cancels = CancelRegistry()
        # G10:验收批准的等待登记(同进程内唯一唤醒通道;CLI/HTTP 批准经服务方法
        # 冻结 manifest 后唤醒对应任务的等待线程)
        self._approvals = ApprovalRegistry()
        # G11:ask_user 的回答等待登记(与批准登记同构;回答落盘后经它唤醒等待线程)
        self._questions = QuestionRegistry()
        # E4:并发上限配置化——显式传参优先(既有调用方行为不变),
        # 未传时读 Settings.task_max_workers(默认 2,行为向后兼容)
        resolved_workers = (
            max_workers if max_workers is not None else get_settings().task_max_workers
        )
        self._pool = ThreadPoolExecutor(
            max_workers=resolved_workers, thread_name_prefix="patchpilot"
        )
        # N-21 整改:task_id → (Future, lock_key),供停机时收敛未开始的任务
        self._futures: dict[str, tuple[Any, str]] = {}

    # ---------- 创建 ----------

    @staticmethod
    def _real_model_name(model: str) -> str:
        """spec 里的真实模型身份:openai 取 Settings.llm_model,fake 回放为空串。"""
        return get_settings().llm_model if model == "openai" else ""

    @staticmethod
    def _replay_for_spec(
        bug: BugTask, engine: str, model: str, replay_script: list[dict[str, Any]] | None
    ) -> list[dict[str, Any]] | None:
        """spec 的回放字段:fake=完整脚本(custom 用请求里的,manifest 从题目目录装载);
        真实模型 None。回放不同 ⇒ task_spec_hash 不同 ⇒ 不 coalesce(F4)。"""
        if model != "fake":
            return None
        if replay_script:
            return replay_script
        return load_replay_script(bug, kind=engine)

    def create_task(
        self,
        *,
        bug_id: str | None = None,
        engine: str = "graph",
        model: str = "fake",
        max_rounds: int | None = None,
        repo_path: str | None = None,
        issue_text: str | None = None,
        failed_tests: list[str] | None = None,
        regression_tests: list[str] | None = None,
        allowed_paths: list[str] | None = None,
        replay_script: list[dict[str, Any]] | None = None,
        agent_profile: str | None = None,
        arm: str = "agent",
        owner: str = "legacy-unowned",
        principal_id: str = "legacy-unowned",
        budget_usd: float | None = None,
        token_budget: int | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """创建任务;返回 (任务, 是否本次新建)。

        幂等键 = 完整 TaskSpec 哈希,同键任务未到终态时返回 (原任务, False)。
        G10:repo_path 且双测试集皆空 = issue-only 受理(diagnose_then_repair 模式,
        仅 graph 引擎);失败/回归集必须经诊断提议与 trusted_user 批准后才冻结生产。
        G16:agent_profile 请求显式选运行 profile(None = Settings);arm 在 API 面
        只受理 agent —— one_shot 是评测 CLI 的消融臂,这里显式拒绝,不静默降级。
        """
        mode = MODE_REPAIR
        settings = get_settings()
        if settings.deployment_mode == "production":
            if settings.execution_backend != "docker":
                raise InvalidRequestError(
                    "production mode requires execution_backend='docker'; "
                    "local execution is strictly forbidden in production"
                )
            if not settings.allowed_repo_roots.strip():
                raise InvalidRequestError(
                    "production mode requires non-empty PATCHPILOT_ALLOWED_REPO_ROOTS"
                )
            if settings.token_budget <= 0:
                raise InvalidRequestError("production mode requires positive token_budget")
            trusted_images = settings.get_trusted_docker_images()
            docker_image = settings.docker_image.strip()
            if not docker_image or (trusted_images and docker_image not in trusted_images):
                raise InvalidRequestError(
                    f"docker_image '{docker_image}' is not in trusted_docker_images in production mode"
                )

        if repo_path is not None:
            resolved = Path(repo_path).resolve()
            if not resolved.is_dir():
                raise TaskError(f"repo_path not found or not a directory: {repo_path}")
            ensure_repo_allowed(resolved)
            if model == "fake" and not replay_script:
                raise InvalidRequestError(
                    "custom repo task with model='fake' requires a replay_script;"
                    " provide replay_script or use model='openai'"
                )
            failed = list(failed_tests or [])
            regression = list(regression_tests or [])
            if not failed and not regression:
                if engine != "graph":
                    raise InvalidRequestError(
                        "issue-only intake (no failed/regression tests) requires engine=graph;"
                        " the plain engine cannot wait for acceptance approval"
                    )
                mode = MODE_DIAGNOSE_THEN_REPAIR
            bug = build_custom_bug(
                repo_path=resolved,
                issue_text=issue_text or "",
                failed_tests=failed,
                regression_tests=regression,
                allowed_paths=allowed_paths,
            )
            # S06:custom 任务的源码身份在受理时绑定(S05a 指纹),冻结副本在
            # 拿到 run_dir 之后再生成(见下方 _freeze_custom_input);此处不动。
        else:
            bug = load_bug(bug_id, self.bugs_root)  # TaskError → 404/422 由路由层转
            # N-2 整改:bug_id 语义上只能是 bugs_root 内的题目目录;
            # schema 层已限 ^BUG-xxx$,此处兜底防绕过(与 repo_path 的根白名单同罪同防)
            bugs_root_resolved = Path(self.bugs_root).resolve()
            if not bug.root.resolve().is_relative_to(bugs_root_resolved):
                raise TaskError(f"bug directory outside bugs root: {bug_id}")
            if settings.deployment_mode == "production":
                ensure_repo_allowed(bug.root.resolve())
        if max_rounds is not None:
            # N-9 整改:max_rounds 此前只落库不生效(假活键);
            # 引擎两路(graph 的 ensure_budget/plain 的驱动器)都读 bug.max_rounds,
            # 在构建后覆写即可对执行行为生效
            bug.max_rounds = max_rounds
        if model == "openai":
            # 前置校验:总开关未开/凭据缺失时同步失败,不建任务、不拿锁、不烧钱
            from app.llm.openai_client import build_model

            build_model(model, get_settings())
        if arm != "agent":
            # G16:one_shot 未接线到 API 受理 —— 显式拒绝并登记(评测 CLI 才可用),
            # 不静默按 agent 跑(schema Literal 已拦 HTTP 面,这里兜直连调用)。
            raise InvalidRequestError(
                f"the API accepts only arm='agent', got {arm!r}; one_shot is an"
                " eval-CLI-only ablation arm (未接线组合在受理处显式拒绝)"
            )
        # S05b/F4:幂等键 = 受理时冻结的 TaskSpec **完整哈希**(字段边界由具名 JSON
        # 结构表达,不再是裸拼接的 bug_id|engine|model)——执行参数(max_rounds)与
        # fake 回放脚本进身份,不同不得 coalesce;同输入同源码同策略才命中在途任务。
        replay_for_spec = self._replay_for_spec(bug, engine, model, replay_script)
        spec = build_task_spec(
            bug,
            engine=engine,
            arm=arm,
            model_provider=model,
            model_name=self._real_model_name(model),
            max_turns=DEFAULT_MAX_TURNS,
            max_rounds=max_rounds if max_rounds is not None else bug.max_rounds,
            replay=replay_for_spec,
            source_snapshot_ref=SNAPSHOT_DIRNAME if repo_path is not None else "",
            mode=mode,
            profile=agent_profile or None,
        )
        idem_key = spec.task_spec_hash

        existing = self.repo.find_by_idem_key(idem_key, principal_id=principal_id)
        if existing and existing["status"] not in TERMINAL_STATUSES:
            return existing, False  # 幂等:同键任务仍在途

        lock_key = f"task:{principal_id}:{idem_key}"
        if not self.lock.acquire(lock_key, ttl_seconds=get_settings().task_timeout_seconds + 60):
            # 锁被占但库里查不到(极小窗口):按幂等冲突处理
            raise PatchPilotError(f"task for {bug.id} is already running")

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        task_id = f"{bug.id}-{stamp}-{uuid.uuid4().hex[:6]}"
        run_dir = self.runs_root / task_id

        # P10-2: 主体/组织额度预留与任务现金上限校验
        requested_cash = (
            budget_usd
            if budget_usd is not None
            else (
                settings.cost_budget_micro_usd / 1_000_000
                if settings.cost_budget_micro_usd > 0
                else 0.0
            )
        )
        task_cap = settings.task_budget_cap_usd
        if task_cap > 0.0 and requested_cash > task_cap:
            self.lock.release(lock_key)
            from app.errors import QuotaExceededError

            raise QuotaExceededError(
                f"requested task budget {requested_cash} USD exceeds task cap {task_cap} USD"
            )

        requested_tokens = token_budget if token_budget is not None else settings.token_budget
        if hasattr(self.repo, "reserve_quota"):
            try:
                self.repo.reserve_quota(
                    task_id=task_id,
                    principal_id=principal_id,
                    amount_usd=requested_cash,
                    tokens=requested_tokens,
                    daily_budget_usd=settings.principal_daily_budget_usd,
                    daily_token_budget=settings.principal_daily_token_budget,
                )
            except Exception:
                self.lock.release(lock_key)
                raise

        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            if repo_path is not None:
                # S06:custom 任务在受理窗口冻结源码副本(执行/恢复只读这份),
                # 预检(解释器/pytest/双集收集)对副本执行;失败即 422,不建任务行、
                # 不调模型、不留假 RUNNING。冻结副本落在 run_dir 内,终态后随可弃集回收。
                intake_started = time.monotonic()
                frozen_fingerprint = freeze_input(
                    resolved,
                    run_dir,
                    max_files=settings.intake_max_files,
                    timeout_seconds=settings.intake_timeout_seconds,
                    max_file_bytes=settings.intake_max_file_bytes,
                    max_total_bytes=settings.intake_max_total_bytes,
                    min_free_disk_bytes=settings.intake_min_free_disk_bytes,
                    mode=settings.deployment_mode,
                )
                if frozen_fingerprint != spec.source_snapshot_hash:
                    raise TaskError(
                        "source changed during intake; nothing was accepted"
                        f" ({spec.source_snapshot_hash[:12]} -> {frozen_fingerprint[:12]})"
                    )
                bug.repo_dir = run_dir / SNAPSHOT_DIRNAME
                run_preflight(
                    python_exe=bug.env.python if bug.env and bug.env.python else sys.executable,
                    workspace=run_dir / SNAPSHOT_DIRNAME,
                    failed_tests=list(bug.failed_tests),
                    regression_tests=list(bug.regression_tests),
                    container_image=bug.env.image
                    if bug.env
                    else getattr(settings, "docker_image", None),
                    execution_backend=str(spec.effective_policy["execution_backend"]),
                    timeout_seconds=max(settings.intake_timeout_seconds // 2, 30),
                    deployment_mode=settings.deployment_mode,
                    allowed_repo_roots=settings.get_allowed_repo_roots(),
                    trusted_docker_images=settings.get_trusted_docker_images(),
                )
                log.info(
                    "intake done for %s in %d ms (snapshot=%s)",
                    task_id,
                    int((time.monotonic() - intake_started) * 1000),
                    run_dir / SNAPSHOT_DIRNAME,
                )
            # S05b/F4:契约双写——run_dir/task_spec.json(执行与恢复读这份)与
            # tasks 表三列(重启后可查)。两边哈希同源(spec.task_spec_hash);
            # _execute 开跑前再核一次,不一致不调模型。
            write_task_spec_file(run_dir, spec)
            self.repo.create_task(
                task_id=task_id,
                idem_key=idem_key,
                bug_id=bug.id,
                repo_path=str(bug.repo_dir),
                issue_text=bug.issue_text[:500],
                max_rounds=max_rounds or bug.max_rounds,
                engine=engine,
                model_provider=model,
                run_dir=str(run_dir),
                owner=owner,
                principal_id=principal_id,
            )
            self.repo.set_task_spec(
                task_id,
                spec_json=canonical_json(spec.to_json_dict()),
                spec_hash=idem_key,
                schema_version=SCHEMA_VERSION,
            )
            # N-7/R2 整改保留:RUNNING 置位已挪入 _execute 首行(P3-10)——
            # 此前在 create_task 置 RUNNING,语义是"已受理进线程池"而非执行中,
            # 池内排队在 DB 不可见;现在排队中如实保持 QUEUED
            # 提交前先注册取消事件,保证 create 返回后的任何 cancel 都不会丢失
            cancel_event = self._cancels.register(task_id)
            # 复盘 P1-8:请求上下文里的 request_id 随任务下发(HTTP 中间件生成),
            # 线程池线程不继承请求 contextvar,必须在提交前捕获
            request_id = request_id_var.get()
            future = self._pool.submit(
                self._execute,
                task_id,
                bug,
                engine,
                model,
                run_dir,
                lock_key,
                cancel_event,
                replay_script,
                request_id,
            )
            # N-21 整改:登记 future,停机时可识别"已受理但从未开始"的任务
            self._futures[task_id] = (future, lock_key)
        except Exception as exc:
            # 落库/提交失败必须回滚:否则锁要挂到 TTL(约 16 分钟),任务行卡死
            self._rollback_created(task_id, lock_key)
            if isinstance(exc, RuntimeError):
                # 线程池已关停(服务退出与受理请求的竞态):409,而不是裸 500
                raise PatchPilotError(
                    f"service is shutting down; task {task_id} not scheduled"
                ) from exc
            raise
        return self.repo.get_task(task_id), True  # type: ignore[return-value]

    def _rollback_created(self, task_id: str, lock_key: str) -> None:
        """创建中途失败的回收:任务行(若已落库)标记 NEEDS_REVIEW,锁与取消事件释放。"""
        log.exception("task %s failed to schedule; rolling back", task_id)
        try:
            if self.repo.get_task(task_id) is not None:
                # finalize_task 守卫任一终态不可覆写(P3-4):回滚不得覆盖
                # 已到终态的任务(含并发取消与并发回滚)
                self.repo.finalize_task(task_id, "NEEDS_REVIEW", "needs_review")
        except Exception:
            log.exception("task %s rollback failed", task_id)
        finally:
            try:
                if hasattr(self.repo, "release_quota"):
                    self.repo.release_quota(task_id)
            except Exception:
                log.exception("task %s quota release during rollback failed", task_id)
            self._cancels.unregister(task_id)
            self.lock.release(lock_key)

    # ---------- 执行 ----------

    def _execute(
        self,
        task_id: str,
        bug,
        engine: str,
        model_name: str,
        run_dir: Path,
        lock_key: str,
        cancel_event,
        replay_script: list[dict[str, Any]] | None = None,
        request_id: str = "",
        resume: bool = False,
    ) -> None:
        # P3-9:工作线程首行设置日志上下文——本任务在此线程内产生的业务日志
        # 都带 task_id(AGENTS 约定的装配面);线程复用,finally 必须 reset。
        # 复盘 P1-8:request_id 在提交前于请求上下文捕获并传入,日志 req= 随之生效
        context_tokens = set_task_context(task_id, request_id)
        try:
            self._execute_inner(
                task_id,
                bug,
                engine,
                model_name,
                run_dir,
                lock_key,
                cancel_event,
                replay_script,
                resume,
            )
        finally:
            reset_task_context(context_tokens)

    def _execute_inner(
        self,
        task_id: str,
        bug,
        engine: str,
        model_name: str,
        run_dir: Path,
        lock_key: str,
        cancel_event,
        replay_script: list[dict[str, Any]] | None = None,
        resume: bool = False,
    ) -> None:
        # P3-10 整改:置 RUNNING 挪到执行线程首行——DB 的 RUNNING = 真正开始执行
        # (受理但排队中保持 QUEUED)。条件写让位于并发取消/回滚:返回 False
        # 说明任务已到终态(如排队窗口内被取消),不再执行,直接收敛清理。
        if not self.repo.set_status_unless_terminal(task_id, "RUNNING"):
            log.info("task %s already terminal before start; skipping execution", task_id)
            self._futures.pop(task_id, None)
            self._cancels.unregister(task_id)
            self.lock.release(lock_key)
            return
        try:
            settings = get_settings()
            # S05b/F4:DB 与文件的契约哈希一致才调模型——不一致说明受理证据损坏
            # (半写/被改),按 INVALID_TASK 收敛,绝不带着可疑身份烧模型。
            stored_spec = self.repo.get_task_spec(task_id)
            spec_path = run_dir / "task_spec.json"
            file_hash = ""
            if spec_path.is_file():
                try:
                    file_hash = TaskSpec.read_file(spec_path).task_spec_hash
                except (ValueError, OSError):
                    file_hash = ""
            db_hash = str((stored_spec or {}).get("task_spec_hash") or "")
            if not file_hash or not db_hash or file_hash != db_hash:
                raise TaskError(
                    f"task_spec inconsistent (db={db_hash[:12] or 'none'}"
                    f" file={file_hash[:12] or 'none'}); model not invoked"
                )
            from app.task_spec import settings_for_policy

            accepted = TaskSpec.read_file(spec_path)
            settings = settings_for_policy(settings, accepted.effective_policy)
            if model_name == "openai":
                settings = settings.model_copy(update={"llm_model": accepted.model_name})
            script = replay_script
            if model_name == "fake" and script is None and stored_spec is not None:
                # G16 迁移收口:恢复路径按**受理契约**复原回放脚本。replay 是任务身份的
                # 一部分(进 task_spec_hash);custom 任务没有 bugs/ 目录可回读,此前重启
                # 恢复在这一步直接 TaskError 判死;契约优先也保证题目目录事后被改动时,
                # 恢复不会自动用上新脚本(与"旧任务恢复不可自动使用新提示"同一条纪律)。
                try:
                    accepted = TaskSpec.from_json_dict(
                        json.loads(str(stored_spec["task_spec_json"]))
                    )
                    script = list(accepted.replay) if accepted.replay else None
                except ValueError:
                    script = None
            if model_name == "fake" and script is None:
                # 直连执行(非服务受理路径)的正式题:从 bugs/ 目录加载
                script = load_replay_script(bug, kind=engine)
            from app.llm.openai_client import build_model

            model = build_model(model_name, settings, script=script)
            # 真实模型名:openai 时取 Settings.llm_model,供成本核算(fake 为空 → 成本恒 n/a)
            real_model_name = settings.llm_model if model_name == "openai" else ""

            if engine == "graph":
                from app.agent.subtasks import branch_model_factory_for, subtask_model_factory_for
                from app.graph.runner import run_task_graph

                # G10:诊断等待接线——批准事件登记 + 等待前把生命周期置 WAITING_INPUT
                # (非终态条件写;批准后服务写回 RUNNING,超时/取消由终态收敛)
                approval_event = self._approvals.register(task_id)
                # G11:ask_user 的等待通道——回答端点经同一服务实例的 QuestionRegistry
                # 唤醒等待线程;等待窗由 Settings 在**执行启动时**冻结(受理后改环境变量
                # 不改变这一次运行)。status 先例与批准等待一致:on_waiting 置 WAITING_INPUT。
                interaction = InteractionChannel(
                    registry=self._questions,
                    task_id=task_id,
                    wait_seconds=settings.user_wait_seconds,
                    on_waiting=lambda: self.repo.set_status_unless_terminal(
                        task_id, "WAITING_INPUT"
                    ),
                )
                try:
                    result = run_task_graph(
                        bug,
                        model,
                        runs_root=self.runs_root,
                        task_id=task_id,
                        run_dir=run_dir,
                        model_name=real_model_name,
                        cancel_event=cancel_event,
                        resume=resume,
                        tracker_sink=self._live_event_sink(task_id),
                        arm="agent",
                        # G13:分支/委派模型工厂与 CLI 同源(API 此前只接了 runner 默认,
                        # 未接分支工厂;SPEC G13"修复 API 未接线"在此收口)
                        branch_model_factory=branch_model_factory_for(bug, model_name, settings),
                        subtask_model_factory=subtask_model_factory_for(bug, model_name, settings),
                        approval_event=approval_event,
                        on_waiting=lambda: self.repo.set_status_unless_terminal(
                            task_id, "WAITING_INPUT"
                        ),
                        interaction=interaction,
                    )
                finally:
                    self._approvals.release(task_id)
            else:
                from app.evals.driver import run_task

                result = run_task(
                    bug,
                    model,
                    runs_root=self.runs_root,
                    engine=engine,
                    task_id=task_id,
                    run_dir=run_dir,
                    model_name=real_model_name,
                    cancel_event=cancel_event,
                    tracker_sink=self._live_event_sink(task_id),
                )

            # 先落产物,再翻终态:轮询方见到终态时轨迹/报告必然已可查。
            # N-7 整改:终态回写用原子守卫——任务已被取消时 finalize 返回 False,
            # 自然完成不得覆盖 CANCELLED(此前先读后写是 check-then-act,双向可打穿)
            self._persist_artifacts(task_id, result, run_dir)
            finalized = self.repo.finalize_task(task_id, result.status, result.verdict)
            self._settle_quota(task_id, run_dir, result=result)
            # P3-12:终态回收——紧跟终态回写、仅在 FINISHED 且赢了终态竞争时
            # 回收可弃集(workspace/checkpoints);取证文件(report/diff/轨迹/
            # junit)永久保留,失败/取消现场一律不回收(策略见 app/api/recycle.py)。
            # 不放进 finally 子句:崩溃/取消路径的现场必须留给复盘,只有终态为
            # FINISHED 才回收;停机/崩溃漏收的由启动 Grace 扫描补齐。
            if finalized and result.status == "FINISHED" and settings.recycle_finished_workspace:
                recycle_run_dir(run_dir)
        except TaskError as exc:
            self.repo.finalize_task(task_id, "INVALID_TASK", "failed")
            log.error("task %s invalid: %s", task_id, exc)
        except TaskCancelled as exc:
            # 协作式取消是正常业务结局:info 级,不得记成 "task crashed" 告警噪声(N-18)
            self.repo.finalize_task(task_id, "CANCELLED", "cancelled")
            log.info("task %s cancelled at turn boundary", task_id)
            _ = exc
        except Exception as exc:
            # N-7:同上,崩溃收敛也走原子守卫,不得覆盖 CANCELLED
            self.repo.finalize_task(task_id, "NEEDS_REVIEW", "needs_review")
            log.exception("task %s crashed", task_id)
            _ = exc
        finally:
            try:
                self._settle_quota(task_id, run_dir)
            except Exception:
                log.exception("task %s quota settlement in finally failed", task_id)
            self._futures.pop(task_id, None)
            self._cancels.unregister(task_id)
            self.lock.release(lock_key)

    def _live_event_sink(self, task_id: str):
        """S07:实时事件 sink——Tracker 每条事件(锁外)同步写入 SQLite 并推进进度列。

        幂等:(task_id, event_id) 唯一索引兜底,重复事件不产生重复行;
        失败不抛(sink 内部计数),收尾 _persist_artifacts 会补录全部缺口。
        """

        def _sink(event: dict[str, Any]) -> None:
            self.repo.upsert_event_live(task_id, event)

        return _sink

    def _persist_artifacts(self, task_id: str, result: Any, run_dir: Path) -> None:
        """轨迹 JSONL → 入库;补丁行 → 入库(评测读口径见 design.md §7:
        tasks=生命周期真相,report.json=引擎取证;evaluations 表已删,P3-8)。"""
        traj_path = run_dir / "trajectory.jsonl"
        if traj_path.exists():
            events = [
                json.loads(line) for line in traj_path.read_text(encoding="utf-8").splitlines()
            ]
            if events:
                backfilled = self.repo.insert_events(task_id, events)
                if backfilled:
                    # S07:收尾补录数 > 0 = 运行期实时 sink 有缺口(降级可观测),
                    # 不谎称"实时入库成功"——缺口的行在这一刻补齐,总量不缺。
                    log.warning(
                        "task %s: live sink missed %d event(s); backfilled at finalize",
                        task_id,
                        backfilled,
                    )
        try:
            report = json.loads((Path(result.run_dir) / "report.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.repo.insert_patch(
            task_id=task_id,
            round_no=report.get("rounds", 1),
            diff_path=str(Path(result.run_dir) / "diff.patch"),
            changed_files=report.get("changed_files", []),
            gate_result="passed"
            if not report.get("gate_violations")
            else "; ".join(report["gate_violations"])[:500],
            applied=bool(report.get("changed_files")),
        )

    def _settle_quota(self, task_id: str, run_dir: Path, result: Any | None = None) -> None:
        """P10-2: 结算任务配额。优先从 result 结算, fallback 从 run_dir 账本/报告读取。"""
        if not hasattr(self.repo, "get_quota_reservation") or not hasattr(
            self.repo, "settle_quota"
        ):
            return
        reservation = self.repo.get_quota_reservation(task_id)
        if not reservation or reservation.get("status") != "reserved":
            return

        actual_tokens = 0
        actual_cost_usd = 0.0

        if result is not None:
            actual_tokens = getattr(result, "tokens_used", 0) or 0
            actual_cost_usd = getattr(result, "cost_usd", 0.0) or 0.0
            resources = getattr(result, "resources", None)
            if isinstance(resources, dict):
                actual_tokens += int(resources.get("unknown_held_tokens", 0) or 0)
                actual_cost_usd += (
                    int(resources.get("unknown_held_cost_micro_usd", 0) or 0)
                ) / 1_000_000
        else:
            ledger_file = run_dir / "ledger_state.json"
            report_file = run_dir / "report.json"
            if ledger_file.exists():
                try:
                    ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
                    actual_tokens = int(ledger.get("tokens_used", 0) or 0) + int(
                        ledger.get("unknown_held_tokens", 0) or 0
                    )
                    actual_cost_usd = (
                        int(ledger.get("cost_micro_usd", 0) or 0)
                        + int(ledger.get("unknown_held_cost_micro_usd", 0) or 0)
                    ) / 1_000_000
                except Exception:
                    log.warning("failed to parse ledger_state.json for task %s", task_id)
            elif report_file.exists():
                try:
                    rep = json.loads(report_file.read_text(encoding="utf-8"))
                    actual_tokens = int(rep.get("tokens_used", 0) or 0)
                    actual_cost_usd = float(rep.get("cost_usd", 0.0) or 0.0)
                    res = rep.get("resources")
                    if isinstance(res, dict):
                        actual_tokens += int(res.get("unknown_held_tokens", 0) or 0)
                        actual_cost_usd += (
                            int(res.get("unknown_held_cost_micro_usd", 0) or 0)
                        ) / 1_000_000
                except Exception:
                    log.warning("failed to parse report.json for task %s", task_id)

        self.repo.settle_quota(
            task_id=task_id,
            actual_amount_usd=actual_cost_usd,
            actual_tokens=actual_tokens,
        )

    # ---------- 查询与控制 ----------

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return self.repo.get_task(task_id)

    def list_tasks(
        self,
        limit: int = 50,
        owner: str | None = None,
        principal_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.repo.list_tasks(limit=limit, owner=owner, principal_id=principal_id)

    def trajectory(self, task_id: str, limit: int, offset: int) -> list[dict[str, Any]]:
        return self.repo.get_trajectory(task_id, limit, offset)

    def events_since(self, task_id: str, after_id: int, limit: int = 200) -> list[dict[str, Any]]:
        """G12 事件流的增量读方(与轨迹同一张表、同一组事实)。"""
        return self.repo.events_since(task_id, after_id, limit)

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        task = self.repo.get_task(task_id)
        if task is None:
            raise TaskError(f"task not found: {task_id}")
        # N-7 整改:取消用原子守卫——任务已到终态时写入不生效并返回冲突;
        # 此前"先读后写"窗口内,执行线程写入的 FINISHED 会被无条件改写成 CANCELLED
        if task["status"] in TERMINAL_STATUSES or not self.repo.cancel_task_row(task_id):
            raise PatchPilotError(f"task {task_id} already finished ({task['status']})")
        # 状态落库后通知执行线程;中断在下个 turn 边界生效
        self._cancels.request_cancel(task_id)
        return self.repo.get_task(task_id)  # type: ignore[return-value]

    def approve_acceptance(
        self,
        task_id: str,
        *,
        proposal_id: str,
        content_hash: str,
        expected_revision: int,
        approved_by: str = "trusted_user",
    ) -> dict[str, Any]:
        """G10:trusted_user 批准验收清单——冻结 manifest 并唤醒等待中的执行线程。

        批准是**冻结工件**而不是一条消息:先落盘(幂等/冲突语义全在 freeze_manifest),
        再唤醒。进程内没有 waiter(崩溃/重启后的遗留等待)时按重新入队恢复,
        resume 的 diagnose 节点从盘上 manifest 直接续跑,批准不会丢在半路。
        """
        row = self.repo.get_task(task_id)
        if row is None:
            raise TaskError(f"task not found: {task_id}")
        status = str(row.get("status") or "")
        if status in TERMINAL_STATUSES:
            raise ConflictError(f"task {task_id} already finished ({status})")
        run_dir = Path(str(row.get("run_dir") or ""))
        if not run_dir.is_dir():
            raise TaskError(f"task {task_id}: run_dir missing, acceptance cannot be approved")
        if self._accepted_mode(run_dir) != MODE_DIAGNOSE_THEN_REPAIR:
            raise InvalidRequestError(
                "task is not in diagnose_then_repair mode; its acceptance list was fixed at intake"
            )
        from app.diagnosis.proposal import (
            ManifestIntegrityError,
            freeze_manifest,
            load_proposal,
        )

        try:
            on_file = load_proposal(run_dir)
        except ManifestIntegrityError as exc:
            raise ConflictError(f"proposal on file failed integrity check: {exc}") from exc
        if on_file is None:
            raise InvalidRequestError("no acceptance proposal on file for this task")
        if on_file.proposal_id != proposal_id:
            raise ConflictError(
                f"proposal_id mismatch: submitted {proposal_id[:12]},"
                f" on-file proposal is {on_file.proposal_id[:12]}"
            )
        manifest, already_frozen = freeze_manifest(
            run_dir,
            expected_hash=content_hash,
            expected_revision=expected_revision,
            approved_by=approved_by,
        )
        resumed = False
        if self._approvals.has_waiter(task_id):
            # 同进程等待线程:冻结先于唤醒(醒来必能读到匹配 manifest)
            self._approvals.approve(task_id)
            self.repo.set_status_unless_terminal(task_id, "RUNNING")
        elif status == "WAITING_INPUT":
            # 无 waiter 但行仍是等待态(进程曾崩溃/重启):重新入队按检查点恢复
            resumed = self._requeue_for_resume(row)
        log.info(
            "task %s: acceptance approved (%s%s)",
            task_id,
            manifest.manifest_hash[:12],
            ", already frozen" if already_frozen else "",
        )
        return {
            "task_id": task_id,
            "proposal_id": proposal_id,
            "manifest_hash": manifest.manifest_hash,
            "already_frozen": already_frozen,
            "resumed": resumed,
            "status": str((self.repo.get_task(task_id) or {}).get("status") or ""),
        }

    def submit_message(
        self,
        task_id: str,
        *,
        kind: str,
        text: str,
        question_id: str = "",
        expected_revision: int = 0,
        idem_key: str = "",
    ) -> dict[str, Any]:
        """G11:回答澄清问题(answer)或追加途中纠偏(steer)。

        语义要点(SPEC G11):
        - 回答只作为**问题工件**的写入(POST /messages),trusted_user 来源由平台赋;
          模型文字、仓库文件里的"用户已批准"永远不构成输入;
        - 回答落盘先于唤醒:`resume 通过问题 ID、expected_revision、幂等键消费一次`;
          无 waiter 且行仍是 WAITING_INPUT(进程曾重启)时按检查点重新入队,
          循环会在恢复后的同一闸门里消费答案(见 run_plain_loop 的 _interaction_gate);
        - 终态任务的输入一律拒绝(终态不可复活);纠偏只追加线索,
          不改变测试、权限与预算(消费端是文本消息,不参与任何判定)。
        """
        row = self.repo.get_task(task_id)
        if row is None:
            raise TaskError(f"task not found: {task_id}")
        status = str(row.get("status") or "")
        if status in TERMINAL_STATUSES:
            raise ConflictError(
                f"task {task_id} already finished ({status}); input is refused on terminal tasks"
            )
        run_dir = Path(str(row.get("run_dir") or ""))
        if not run_dir.is_dir():
            raise TaskError(f"task {task_id}: run_dir missing, input cannot be recorded")
        store = InteractionStore(run_dir)
        if kind == KIND_ANSWER:
            try:
                question, already = store.submit_answer(
                    question_id=question_id,
                    expected_revision=expected_revision,
                    text=text,
                    idem_key=idem_key,
                )
            except InteractionIntegrityError as exc:
                raise ConflictError(
                    f"interaction state on file failed integrity check: {exc}"
                ) from exc
            woke = False
            resumed = False
            if not already:
                if self._questions.has_waiter(task_id):
                    woke = self._questions.answer(task_id, question_id)
                    if woke:
                        # 等待线程在同一进程内:批准先落盘(上面),此处只唤醒并回写状态
                        self.repo.set_status_unless_terminal(task_id, "RUNNING")
                elif status == "WAITING_INPUT":
                    # 无 waiter 但行仍是等待态(进程曾崩溃/重启):重新入队按检查点恢复,
                    # 循环在恢复后的闸门里消费这条答案(与 G10 批准的重启路径同一处置)
                    resumed = self._requeue_for_resume(row)
            log.info(
                "task %s: answer recorded for question %s (already=%s woke=%s resumed=%s)",
                task_id,
                question_id[:12],
                already,
                woke,
                resumed,
            )
            return {
                "task_id": task_id,
                "kind": kind,
                "accepted": True,
                "already": already,
                "woke_waiter": woke,
                "resumed": resumed,
                "question_id": str(question.get("question_id") or question_id),
                "status": str((self.repo.get_task(task_id) or {}).get("status") or ""),
            }
        if kind == KIND_STEER:
            try:
                already = store.submit_steer(text=text, idem_key=idem_key)
            except InteractionIntegrityError as exc:
                raise ConflictError(
                    f"interaction state on file failed integrity check: {exc}"
                ) from exc
            log.info("task %s: steer recorded (already=%s)", task_id, already)
            return {
                "task_id": task_id,
                "kind": kind,
                "accepted": True,
                "already": already,
                "woke_waiter": False,
                "resumed": False,
                "question_id": "",
                "status": status,
            }
        raise InvalidRequestError(f"unsupported message kind: {kind!r} (answer | steer)")

    def pending_question(self, task_id: str) -> dict[str, Any]:
        """G11:当前未回答问题与输入版本(CLI/API 展示用);损坏的工件如实报冲突。"""
        row = self.repo.get_task(task_id)
        if row is None:
            raise TaskError(f"task not found: {task_id}")
        run_dir = Path(str(row.get("run_dir") or ""))
        store = InteractionStore(run_dir)
        try:
            state = store.read()
        except InteractionIntegrityError as exc:
            raise ConflictError(f"interaction state on file failed integrity check: {exc}") from exc
        open_question = next(
            (q for q in state.get("questions", []) if q.get("status") == "open"), None
        )
        pending_steers = sum(1 for s in state.get("steers", []) if not s.get("consumed_at"))
        return {
            "task_id": task_id,
            "status": str(row.get("status") or ""),
            "revision": int(state.get("revision", 0)),
            "question": open_question,
            "pending_steers": pending_steers,
        }

    @staticmethod
    def _accepted_mode(run_dir: Path) -> str:
        """从受理契约读 mode(run_dir/task_spec.json 是执行与恢复的权威副本)。"""
        try:
            spec_path = run_dir / "task_spec.json"
            if spec_path.is_file():
                return TaskSpec.read_file(spec_path).mode
        except (ValueError, OSError):
            log.warning("task spec unreadable under %s; treating as repair", run_dir)
        return MODE_REPAIR

    def recover_stale(self) -> int:
        """启动恢复:僵尸任务分两路收敛,并同步清掉残留任务锁(N-20 整改)。

        此前只修状态不清锁——崩溃重启后同键重试会被 409 卡死到 TTL(约 16 分钟),
        且报错语义错误("already running"而任务已是 NEEDS_REVIEW)。

        M6 分流(返回值 = 本次处理掉的僵尸行数,含两条路):
        - **有可用循环快照的 graph 任务** → 重新入队,按检查点 + 工作记忆续跑
          (不再判死;这是"跑了 19 轮崩溃后从零开始"这条真实代价的修法);
        - **其余(含快照损坏/版本不符/非 graph 引擎/抢不到锁)** → 与分流引入前逐字相同:
          recover_stale_running 收敛 NEEDS_REVIEW + force_release 旧锁。
        """
        stale_rows = self.repo.list_stale_running()
        resumable = [row for row in stale_rows if self._resumable_row(row)]
        stale_keys = self.repo.recover_stale_running([str(row["task_id"]) for row in resumable])
        for idem_key in stale_keys:
            try:
                self.lock.force_release(f"task:{idem_key}")
            except Exception:  # 单把锁清理失败不得打断其余恢复
                log.exception("failed to force-release stale lock task:%s", idem_key)

        requeued = 0
        for row in resumable:
            task_id = str(row["task_id"])
            if self._requeue_for_resume(row):
                requeued += 1
                continue
            # 退回旧路径:抢不到锁(已有恢复方)或题面无法重建时,宁可判死也不要
            # 留一行"既不跑也没判"的僵尸
            self.repo.finalize_task(task_id, "NEEDS_REVIEW", "needs_review")
        if requeued:
            log.warning("requeued %d stale task(s) for checkpoint resume", requeued)
        # 语义与分流引入前一致:本次"处理掉"的僵尸行数(判死 + 重新入队两条路都算)
        return len(stale_rows)

    def _resumable_row(self, row: dict[str, Any]) -> bool:
        """僵尸行是否值得续跑:开关开着、graph 引擎、run_dir 里有**通过校验**的快照。

        这里用 load_loop_snapshot 而不是只看文件存在——损坏/版本不符的快照必须退回
        判死路径,不能把一份错乱的工作记忆当真(读侧宁缺勿信,见 app/graph/loop_state.py)。
        """
        if not get_settings().resume_on_restart:
            return False
        if row.get("engine") != "graph":
            # plain 引擎没有图检查点,B 级恢复无从定位"下一个该跑的节点"(如实限制)
            return False
        run_dir = row.get("run_dir")
        if not run_dir:
            return False
        run_dir_path = Path(run_dir)
        # S03:可恢复判定 = 合法的 checkpoint+candidate 边界,不是只看 loop_state 文件。
        # 受理契约(task_spec.json,S05a)是身份证据:缺失说明是旧任务,
        # 缺所需输入/资源证据,一律不自动放行(判死路径)。
        if not (run_dir_path / "task_spec.json").is_file():
            return False
        from app.diagnosis.proposal import PROPOSAL_FILENAME

        if (run_dir_path / PROPOSAL_FILENAME).is_file():
            # G10:等待批准的 issue-only 任务(checkpoint next=diagnose)——盘上提议
            # 就是可续位置;冻结与否由 resume 的 diagnose 节点重读 manifest 判定
            return True
        if (run_dir_path / "candidates").is_dir():
            # 有候选工件:apply/verify/finish 边界崩溃(循环快照已被正常删除),
            # runner 的 revalidate 会按候选重应用并完整重验——值得重新入队
            return True
        from app.graph.loop_state import load_loop_snapshot

        return load_loop_snapshot(run_dir_path, task_id=str(row.get("task_id") or "")) is not None

    def _requeue_for_resume(self, row: dict[str, Any]) -> bool:
        """把僵尸行重新入队续跑;返回 False = 调用方须按旧路径判死。

        双恢复拦截用的是**既有任务锁**(app/storage/locks.py),不新造锁:
        先 `acquire` 成功才入队——同一 idem_key 已被别的恢复方持有(前进程的锁
        还没过期、或另一个实例同时重启)时直接放弃,由调用方收敛 NEEDS_REVIEW。
        注意这里刻意不做 force_release:强制清锁正是"两个进程同时续跑同一任务"的入口。
        """
        task_id = str(row["task_id"])
        run_dir_value = str(row.get("run_dir") or "")
        idem_key = str(row.get("idem_key") or "")
        lock_key = f"task:{idem_key}"
        if not self.lock.acquire(lock_key, ttl_seconds=get_settings().task_timeout_seconds + 60):
            log.warning("task %s: resume lock already held; not requeued", task_id)
            return False
        try:
            bug = self._bug_from_row(row)
            run_dir = Path(run_dir_value)
            run_dir.mkdir(parents=True, exist_ok=True)
            cancel_event = self._cancels.register(task_id)
            request_id = request_id_var.get()
            future = self._pool.submit(
                self._execute,
                task_id,
                bug,
                str(row.get("engine") or "graph"),
                str(row.get("model_provider") or ""),
                run_dir,
                lock_key,
                cancel_event,
                None,
                request_id,
                True,
            )
            self._futures[task_id] = (future, lock_key)
        except Exception as exc:
            log.exception("task %s: failed to requeue for resume", task_id)
            self._cancels.unregister(task_id)
            self.lock.release(lock_key)
            _ = exc
            return False
        log.info("task %s requeued for resume (run_dir=%s)", task_id, run_dir_value)
        return True

    def _bug_from_row(self, row: dict[str, Any]) -> BugTask:
        """从任务行重建 BugTask;S05b 起优先走持久化 TaskSpec(F4 收口)。

        - tasks 表带契约 → 按契约重建:manifest 题回读题面并**核对身份一致**(漂移即拒);
          custom 任务从契约忠实重建(issue 完整、测试清单、scope、源指纹重算),
          不再"无从重建一律判死"。
        - 旧行无契约 → 退回旧行为:正式题回读 manifest;custom 缺证据判死(不猜题面)。
        """
        task_id = str(row.get("task_id") or "")
        if not task_id:
            raise TaskError(f"task {row.get('task_id')}: empty task_id, resume impossible")
        stored = self.repo.get_task_spec(task_id)
        if stored is not None:
            try:
                spec = TaskSpec.from_json_dict(json.loads(str(stored["task_spec_json"])))
            except (ValueError, OSError) as exc:
                raise TaskError(f"task {task_id}: stored task_spec unusable: {exc}") from exc
            if spec.source_kind == "manifest":
                bug = load_bug(str(row.get("bug_id") or ""), self.bugs_root)
                if (
                    bug.issue_text != spec.issue_text
                    or bug.failed_tests != list(spec.failed_tests)
                    or bug.regression_tests != list(spec.regression_tests)
                ):
                    raise TaskError(
                        f"task {task_id}: bugs_root manifest drifted from accepted"
                        " task_spec; resume impossible"
                    )
            else:
                bug = self._bug_from_custom_spec(spec, task_id, Path(str(row.get("run_dir") or "")))
            row_rounds = row.get("max_rounds")
            bug.max_rounds = int(row_rounds) if row_rounds else int(spec.max_rounds)
            return bug
        bug_id = str(row.get("bug_id") or "")
        if not bug_id:
            raise TaskError(f"task {task_id}: empty bug_id, resume impossible")
        try:
            bug = load_bug(bug_id, self.bugs_root)
        except TaskError as exc:
            raise TaskError(
                f"task {task_id}: bug {bug_id!r} not reloadable and no stored task_spec;"
                " resume impossible"
            ) from exc
        max_rounds = row.get("max_rounds")
        if max_rounds:
            # 与 create_task 同口径:轮数上限只落库不生效是假活键(N-9),续跑同样要覆写
            bug.max_rounds = int(max_rounds)
        return bug

    @staticmethod
    def _bug_from_custom_spec(spec: Any, task_id: str, run_dir: Path) -> BugTask:
        """从持久化契约重建 custom 任务;执行指向**冻结副本**,不读用户原始目录(S06)。

        - 契约声明了快照引用 → 校验 run_dir/source_snapshot 的指纹仍与受理一致;
        - 无快照引用的旧契约 → 退回校验原始目录(源变了即拒)。
        """
        from app.task_spec import fingerprint_source_dir

        if spec.source_snapshot_ref:
            repo_dir = run_dir / spec.source_snapshot_ref
            if not repo_dir.is_dir():
                raise TaskError(f"task {task_id}: frozen source snapshot gone: {repo_dir}")
            if fingerprint_source_dir(repo_dir) != spec.source_snapshot_hash:
                raise TaskError(
                    f"task {task_id}: frozen snapshot fingerprint mismatch; resume impossible"
                )
            return BugTask(
                id=task_id,
                root=repo_dir,
                repo_dir=repo_dir,
                issue_text=spec.issue_text,
                failed_tests=list(spec.failed_tests),
                regression_tests=list(spec.regression_tests),
                allowed_paths=list(spec.allowed_paths) if spec.allowed_paths else None,
                max_rounds=int(spec.max_rounds),
                category="custom",
                replay_script_path=None,
            )
        repo_dir = Path(spec.source_path)
        if not repo_dir.is_dir():
            raise TaskError(f"task {task_id}: source dir gone: {repo_dir}")
        if fingerprint_source_dir(repo_dir) != spec.source_snapshot_hash:
            raise TaskError(f"task {task_id}: source changed since acceptance; resume impossible")
        return BugTask(
            id=task_id,
            root=repo_dir,
            repo_dir=repo_dir,
            issue_text=spec.issue_text,
            failed_tests=list(spec.failed_tests),
            regression_tests=list(spec.regression_tests),
            allowed_paths=list(spec.allowed_paths) if spec.allowed_paths else None,
            max_rounds=int(spec.max_rounds),
            category="custom",
            replay_script_path=None,
        )

    def shutdown(self) -> None:
        """优雅停机(N-21 整改):向在途任务传播协作式取消,收敛未开始的任务。

        此前 cancel_futures=True 只砍掉"尚未开始"的 future——这些任务的
        _execute 永不运行,其任务行永久滞留 RUNNING;在途任务则收不到任何
        通知,非 daemon 工作线程把进程退出阻塞到任务自然结束。
        """
        # ① 在途任务:turn 边界协作中断(正在跑的一次 pytest/LLM 先完成)
        for task_id in self._cancels.ids():
            self._cancels.request_cancel(task_id)
        # ② 已受理但未开始:future 可取消 → 任务行收敛 CANCELLED(没跑过,
        #    无产物无成本,与运行期取消排队任务的语义一致),锁与事件释放
        for task_id, (future, lock_key) in list(self._futures.items()):
            if not future.cancel():
                continue
            self._futures.pop(task_id, None)
            try:
                self.repo.cancel_task_row(task_id)
            except Exception:  # 单个清理失败不得打断其余
                log.exception("task %s: failed to converge row on shutdown", task_id)
            finally:
                self._cancels.unregister(task_id)
                self.lock.release(lock_key)
        # ③ 兜底:不等待在途线程(它们会经 ① 的 turn 边界尽快自行收敛)
        self._pool.shutdown(wait=False, cancel_futures=True)
