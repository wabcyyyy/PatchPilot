"""受控命令与复现工件工具(G09):write_repro / run_command 的工具层语义。

- ``write_repro`` 把复现脚本写进 run_dir/scratch(绑定内容 hash),不写工作区、
  不进 patch diff;
- ``run_command`` 只按 profile 执行:先构造诊断副本(工作区的拷贝),命令在副本里跑,
  改动不进候选;原始 stdout/stderr 先落 scratch,再作为证据进 EvidenceStore(受磁盘
  额度约束,超额如实标注);回执只陈述事实——exit_code=0 不构成验收,
  平台最终 verify 由既有的 verify 链执行,辅助命令不能替代它。
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

from app.context.evidence import KIND_STDERR, KIND_STDOUT, EvidenceNotStored, EvidenceRef
from app.errors import ExecError
from app.executor.command_profiles import CommandProfileError, build_command, repro_script_name
from app.executor.command_runner import run_controlled_command
from app.executor.docker_runner import docker_available
from app.tools.base import ToolContext, ToolResult

# 结构化失败/降级分类串:缺 Docker、缺依赖、起不了进程都用它开场,
# 上层(G10 诊断、报告)按前缀区分"环境不可用"与"业务失败"
ENVIRONMENT_NOT_READY = "environment_not_ready"

# 登记脚本在诊断副本内的暂存子目录(每次 run_command 重建副本,随之干净)
REPRO_STAGING_SUBDIR = ".pp-repro"

log = logging.getLogger(__name__)

REPRO_NAME_SCHEMA = "^[A-Za-z0-9_.\\-]+$ (write_repro 分配的名字;默认 repro-<n>.py)"

# 模型可见回执里 stdout/stderr 的 tail 字符数;原文经 evidence_id 可分页取回
_TAIL_CHARS_DEFAULT = 2000


def _scratch_root(ctx: ToolContext) -> Path:
    """任务 scratch 根:report_dir 同级(run_dir/scratch),永远在工作区之外。"""
    scratch = ctx.report_dir.parent / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


def write_repro(ctx: ToolContext, content: str, name: str | None = None) -> ToolResult:
    """把复现脚本写进任务 scratch 并登记(名字 → 内容 hash);返回相对引用与 hash。"""
    text = str(content or "")
    if not text.strip():
        return ToolResult.fail("repro content is empty")
    repro_dir = _scratch_root(ctx) / "repro"
    repro_dir.mkdir(parents=True, exist_ok=True)
    if name is None or not str(name).strip():
        seq = len(ctx.repro_files) + 1
        name = f"repro-{seq}.py"
    name = str(name)
    if "/" in name or "\\" in name or name.startswith("."):
        return ToolResult.fail(f"invalid repro name {name!r}; use a flat file name")
    sha256 = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    target = repro_dir / name
    if name in ctx.repro_files and ctx.repro_files[name] != sha256:
        # 同名不同内容:拒绝覆盖,让模型换名字——静默替换会毁掉"登记名=内容"的绑定
        return ToolResult.fail(
            f"repro {name!r} already exists with different content; use a new name"
        )
    if not target.exists():
        target.write_text(text, encoding="utf-8")
    ctx.repro_files[name] = sha256
    return ToolResult(
        ok=True,
        output={
            "repro": name,
            "sha256": sha256[:16],
            "bytes": len(text.encode("utf-8", errors="replace")),
            "note": "repro lives in task scratch (outside the workspace); never part of the patch",
        },
    )


def _save_stream_evidence(
    ctx: ToolContext, path: Path, *, source: str, kind: str
) -> tuple[EvidenceRef | None, str]:
    """把执行层已落盘的原始输出存进 EvidenceStore;超额拒落时如实标注不可取回。"""
    if ctx.evidence is None:
        from app.context.evidence import build_store

        ctx.evidence = build_store(ctx.report_dir, ctx.task_id)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, f"raw file unreadable: {exc}"
    try:
        ref = ctx.evidence.save(
            text, source=source, kind=kind, workspace_revision=ctx.workspace_revision
        )
        return ref, ""
    except EvidenceNotStored as exc:
        return exc.ref, exc.ref.reason


def _scratch_gc(scratch: Path, max_bytes: int) -> int:
    """scratch 原始输出的总量回收(最旧优先):返回本轮让位的文件数。

    证据仓(evidence/)不在回收域——原文可取回是取证契约,回收只针对
    scratch 里命令已消费完的 .raw 落盘;真缺额度时 EvidenceStore 自己仍会拒落并如实标注。
    """
    if max_bytes <= 0:
        return 0
    raws = sorted(
        (p for p in scratch.glob("cmd-*-*.raw") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
    )
    total = sum(p.stat().st_size for p in raws)
    evicted = 0
    for path in raws:
        if total <= max_bytes:
            break
        total -= path.stat().st_size
        path.unlink(missing_ok=True)
        evicted += 1
    return evicted


def _diag_copy(ctx: ToolContext, seq: int) -> Path:
    """重建诊断副本:每次命令都从当前工作区拷一份,改动不进候选、也不跨命令泄漏。"""
    diag = _scratch_root(ctx) / f"ws-diag-{seq}"
    if diag.exists():
        shutil.rmtree(diag)
    shutil.copytree(ctx.workspace, diag, symlinks=False, ignore=shutil.ignore_patterns(".git"))
    return diag


def _missing_dependency_hint(argv: list[str], stderr_tail: str) -> str | None:
    """镜像/宿主缺依赖的结构化识别:返回说明或 None(不猜,只认执行环境自报)。

    两种真实形态(Python 工具链与容器 exec 各有一种):
    - 宿主/容器内 python 模块缺失:`No module named 'xxx'`;
    - 容器内没有这个可执行文件:docker 拒绝创建进程,`exec: "xxx": executable file
      not found`(exit 127)。
    """
    marker = "No module named"
    exec_marker = "executable file not found"
    if argv[0] == "ruff" and (marker in stderr_tail or exec_marker in stderr_tail):
        return "environment_not_ready: ruff is not installed in this execution environment"
    if marker in stderr_tail and "pytest" in stderr_tail:
        return "environment_not_ready: pytest is not installed in this execution environment"
    return None


def _resolve_executable(role: str, argv0: str, ctx: ToolContext, backend: str) -> str:
    """把 profile 里的可执行角色名替换成实际可执行:python 用任务冻结解释器,
    ruff 用平台 venv 内的(缺席时留原名,由依赖探测给出 environment_not_ready)。
    docker 后端不替换——容器里跑的是镜像自带的解释器/工具,宿主路径进容器必然
    exec 失败(exit 127,Docker 集成验证抓到的真实缺陷)。替换只由平台做,
    模型永远不给可执行名。"""
    if backend == "docker":
        return argv0
    if argv0 == "python":
        return ctx.python_exe or argv0
    if argv0 == "ruff":
        sibling = Path(ctx.python_exe or sys.executable).parent / "ruff.exe"
        if sibling.is_file():
            return str(sibling)
        which_ruff = shutil.which("ruff")
        if which_ruff:
            return which_ruff
    return argv0


def run_command(
    ctx: ToolContext, profile_id: str, parameters: dict[str, Any] | None = None
) -> ToolResult:
    """按 profile 执行受控命令;回执只陈述事实,exit_code=0 不构成验收。"""
    from app.config import get_settings

    settings = get_settings()
    seq = ctx.command_seq + 1
    ctx.command_seq = seq
    repro_files = dict(ctx.repro_files)
    params = dict(parameters or {})
    backend = ctx.execution_backend or settings.execution_backend
    if backend == "docker":
        if not docker_available():
            return ToolResult.fail(
                f"{ENVIRONMENT_NOT_READY}: docker daemon is unavailable;"
                " the command capability requires docker and never falls back to the host"
            )
        image = settings.docker_image
    else:
        image = ""
    env_extra: dict[str, str] = {}
    try:
        # 原始输出落盘前的总量回收(最旧优先):证据仓不在回收域
        _scratch_gc(_scratch_root(ctx), settings.command_scratch_max_bytes)
        cwd = _diag_copy(ctx, seq)
        if profile_id == "python_repro":
            # 登记脚本拷进诊断副本的固定子目录再运行:python 的 sys.path[0] 是**脚本
            # 所在目录**而不是 cwd,不给 PYTHONPATH 复现脚本就 import 不到仓库代码。
            # 两种后端同构:docker 的挂载点恒为 /ws,local 直接用诊断副本路径。
            name = repro_script_name(params, repro_files)
            staged = cwd / REPRO_STAGING_SUBDIR
            staged.mkdir(exist_ok=True)
            shutil.copy2(_scratch_root(ctx) / "repro" / name, staged / name)
            params = {"repro": name}
            env_extra = {"PYTHONPATH": "/ws" if backend == "docker" else str(cwd)}
            built = build_command(
                profile_id, params, repro_files=repro_files, repro_subdir=REPRO_STAGING_SUBDIR
            )
        else:
            built = build_command(profile_id, params, repro_files=repro_files)
    except CommandProfileError as exc:
        return ToolResult.fail(str(exc))

    argv = list(built.argv)
    argv[0] = _resolve_executable(built.profile.executable, argv[0], ctx, backend)
    timeout = built.profile.timeout_seconds or settings.command_timeout_seconds
    try:
        result = run_controlled_command(
            argv,
            cwd=cwd,
            scratch_dir=_scratch_root(ctx),
            timeout_seconds=timeout,
            output_cap_bytes=settings.command_output_max_bytes,
            cancel_event=ctx.cancel_event,
            backend=backend,
            image=image,
            seq=seq,
            env_extra=env_extra,
        )
    except ExecError as exc:
        return ToolResult.fail(f"{ENVIRONMENT_NOT_READY}: {exc}")

    # 原始输出进证据仓(先于模型可见 tail):超额拒落也带身份,不做静默丢弃
    stdout_ref, stdout_note = _save_stream_evidence(
        ctx,
        Path(result.stdout_path),
        source=f"run_command/{built.profile.profile_id}",
        kind=KIND_STDOUT,
    )
    stderr_ref, stderr_note = _save_stream_evidence(
        ctx,
        Path(result.stderr_path),
        source=f"run_command/{built.profile.profile_id}",
        kind=KIND_STDERR,
    )
    tail_limit = settings.command_tail_chars
    stdout_text = Path(result.stdout_path).read_text(encoding="utf-8", errors="replace")
    stderr_text = Path(result.stderr_path).read_text(encoding="utf-8", errors="replace")

    def _stream(
        ref: EvidenceRef | None, note: str, text: str, raw_bytes: int, dropped: int
    ) -> dict[str, Any]:
        out: dict[str, Any] = {
            "raw_bytes": raw_bytes,
            "dropped_bytes": dropped,
            "tail": text[-tail_limit:] if tail_limit > 0 else "",
        }
        if ref is not None:
            out["evidence_id"] = ref.evidence_id
            out["retrievable"] = bool(ref.retrievable)
            if not ref.retrievable:
                out["reason"] = ref.reason
        else:
            out["retrievable"] = False
            out["reason"] = note
        return out

    dep_hint = _missing_dependency_hint(built.argv, stderr_text[-4000:])
    output = {
        "role": "auxiliary",
        "note": (
            "exit_code=0 is not acceptance; the platform's verify chain is the only acceptance path"
        ),
        "profile_id": built.profile.profile_id,
        "argv": built.argv,
        "argv_sha256": built.argv_sha256,
        "backend": backend,
        "executor": result.container_name or f"pid:{result.pid}",
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "cancelled": result.cancelled,
        "duration_ms": result.duration_ms,
        "stdout": _stream(
            stdout_ref, stdout_note, stdout_text, result.stdout_bytes, result.stdout_dropped_bytes
        ),
        "stderr": _stream(
            stderr_ref, stderr_note, stderr_text, result.stderr_bytes, result.stderr_dropped_bytes
        ),
        "run_ref": {"seq": seq, "run_id": uuid.uuid4().hex[:12]},
    }
    if dep_hint:
        output["environment"] = dep_hint
    return ToolResult(ok=True, output=output)
