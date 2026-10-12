"""工具注册表:名称、schema、边界校验与统一入口。

`finish` 是循环控制用的伪工具:出现在模型可见的 schema 里,
由 Agent 循环本身消费,不会触达工作区。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.context.ast_outline import SYMBOL_KINDS
from app.context.evidence import EvidenceNotStored, EvidenceRef, build_store
from app.diagnosis.tool import propose_acceptance
from app.tools.base import ToolContext, ToolResult
from app.tools.commands import run_command, write_repro
from app.tools.delegation import delegate
from app.tools.execution import run_tests
from app.tools.files import list_files, read_evidence, read_file, search_code
from app.tools.interaction import ask_user
from app.tools.memory import SELF_RECORDING_TOOLS, describe_receipt, update_memory
from app.tools.output_filter import bound_structure
from app.tools.patching import apply_patch, git_diff, reset_to_baseline
from app.tools.symbols import describe_file, find_symbol

log = logging.getLogger(__name__)

FINISH_TOOL = "finish"

#: 会改动工作区的工具:成功一次就让 workspace_revision +1(G05 的 stale 判定就靠它)。
#: 写代数住在 registry 而不是 patching.py,是为了让"什么算一次写入"只有一处定义 ——
#: 门禁拒绝/协议拒绝走的是 fail 分支,不留代数,也就不产生任何"看起来改过了"的旧引用。
MUTATING_TOOLS = frozenset({"apply_patch", "reset_workspace"})

#: 工具恢复语义(G12):崩溃/恢复时"这条 tool_call 还要不要重放、怎么判断"的声明面。
#: 每条 tool_call 在恢复时的占位回执按这里选措辞(见 SessionState.repair_orphans),
#: 而不是让模型自己猜"上次那条到底跑没跑过"。
#: - safe_replay:只读/幂等,重放安全(检索、读文件、取证据、登记记忆、跑测试集);
#: - state_check:有工作区副作用,先看状态再决定(apply_patch 看 git_diff/候选 hash,
#:   reset_workspace 看 diff 是否已空);
#: - no_rerun:结果不可知且可能有副作用,不许盲目重跑(受控命令的进程可能已经执行)。
RECOVERY_POLICY: dict[str, str] = {
    "list_files": "safe_replay",
    "search_code": "safe_replay",
    "find_symbol": "safe_replay",
    "describe_file": "safe_replay",
    "read_file": "safe_replay",
    "read_evidence": "safe_replay",
    "update_memory": "safe_replay",
    "ask_user": "safe_replay",
    "propose_acceptance": "safe_replay",
    "git_diff": "safe_replay",
    "run_tests": "safe_replay",
    "write_repro": "safe_replay",
    "apply_patch": "state_check",
    "reset_workspace": "state_check",
    "run_command": "no_rerun",
    "delegate": "no_rerun",  # G13:子任务可能已经执行/改过工作区,不许盲目重跑
    FINISH_TOOL: "safe_replay",
}


def recovery_policy(name: str) -> str:
    """取一条工具调用的恢复策略;未登记的名字保守按 no_rerun(不认识就不许重跑)。"""
    return RECOVERY_POLICY.get(name, "no_rerun")


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., ToolResult]


def _schema(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required},
    }


def _build_registry() -> dict[str, ToolSpec]:
    return {
        "list_files": ToolSpec(
            name="list_files",
            description="列出仓库文件(相对路径)。可传 subdir 限定子目录、glob 过滤,如 '*.py'。",
            parameters=_schema(
                "list_files",
                "列出仓库文件",
                {"subdir": {"type": "string"}, "glob": {"type": "string"}},
                [],
            ),
            handler=list_files,
        ),
        "search_code": ToolSpec(
            name="search_code",
            description=(
                "在仓库中检索代码。默认做大小写不敏感的**字面子串**匹配(不是正则)。"
                "keyword 必填;glob 限定文件范围(如 '*.py')。"
                "context_lines=2 可直接看到命中行上下各两行,省掉一轮 read_file;"
                "per_file_cap=3 防止一个巨型文件占满结果、把别的文件挤掉;"
                "regex=true 才按正则匹配(模式非法会报错并原样点名,改一下再来)。"
                "条数有上限,truncated=true 表示被裁——换更具体的关键词或加 glob,别重复同一查询。"
            ),
            parameters=_schema(
                "search_code",
                "搜索代码",
                {
                    "keyword": {"type": "string"},
                    "glob": {"type": "string"},
                    "context_lines": {"type": "integer"},
                    "per_file_cap": {"type": "integer"},
                    "regex": {"type": "boolean"},
                },
                ["keyword"],
            ),
            handler=search_code,
        ),
        "find_symbol": ToolSpec(
            name="find_symbol",
            description=(
                "符号定义跳转:回答'X 在哪里定义'。返回 {name, kind, path, line, signature},"
                "按路径确定序。优先精确名(大小写不敏感),没有精确命中才回退前缀匹配"
                "(输出里的 match 字段说明是哪一种);kind 可选 class/function/method,"
                "只想要方法就传 kind='method'。想知道某个函数/类在哪,先用它,而不是用 "
                "search_code 全文扫一遍再猜。"
            ),
            parameters=_schema(
                "find_symbol",
                "查找符号定义",
                {
                    "name": {"type": "string"},
                    "kind": {"type": "string", "enum": list(SYMBOL_KINDS)},
                },
                ["name"],
            ),
            handler=find_symbol,
        ),
        "describe_file": ToolSpec(
            name="describe_file",
            description=(
                "单个 Python 文件的 AST 大纲:顶层 class/function 加类内方法,"
                "每条含 name/kind/start_line/end_line/signature,另有 total_lines。"
                "读大文件之前先用它定位行区间,再用 read_file 的 offset+limit 精读那一段,"
                "不要整文件灌进上下文。解析失败不报错,而是给出 parse_error——"
                "那种文件改用 search_code 或 read_file 直接看。"
            ),
            parameters=_schema(
                "describe_file",
                "查看文件结构大纲",
                {"path": {"type": "string"}},
                ["path"],
            ),
            handler=describe_file,
        ),
        "read_file": ToolSpec(
            name="read_file",
            description=(
                "读取文本文件的一段内容(offset 为 1-based 行号),单次有行数上限。"
                "limit 只要更少(上限仍由平台决定,传大了只会拿到更少,拿不到更多)。"
                "大文件别整读:先 describe_file 拿区间,再带 offset/limit 精读需要的那几十行。"
            ),
            parameters=_schema(
                "read_file",
                "读取文件",
                {
                    "path": {"type": "string"},
                    "offset": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
                ["path"],
            ),
            handler=read_file,
        ),
        "read_evidence": ToolSpec(
            name="read_evidence",
            description=(
                "分页取回某条回执的**原始内容**(上一句里 evidence.evidence_id 指的那份)。"
                "被裁掉的行没有丢:这条工具读的是落盘原文,不是模型可见的那一屏。"
                "evidence_id 必填;cursor 用上一次返回的 next_cursor 原样传回来,别自己编;"
                "limit_bytes 只能要更少(上限由平台决定)。"
                "注意 stale=true 时这份内容是**工作区改动前**的,不能当现状用——"
                "要现状就重新 read_file。查某个文件的另外几十行请用 read_file(offset),"
                "read_evidence 是给「这一屏当时到底写了什么」用的。"
            ),
            parameters=_schema(
                "read_evidence",
                "取回原始回执",
                {
                    "evidence_id": {"type": "string"},
                    "cursor": {"type": "string"},
                    "limit_bytes": {"type": "integer"},
                },
                ["evidence_id"],
            ),
            handler=read_evidence,
        ),
        "apply_patch": ToolSpec(
            name="apply_patch",
            description=(
                "应用补丁(只接受 apply_patch 块协议:*** Begin/End Patch + "
                "Update/Add/Delete File 段 + 空格/+/− 行;unified diff 会被直接拒)。"
                "上下文行必须与文件逐字一致且全文件唯一;禁止修改测试文件;"
                "路径与文件数受门禁限制;引入 Python 语法错误的补丁会被拒绝并还原。"
            ),
            parameters=_schema(
                "apply_patch",
                "应用补丁",
                {"patch_text": {"type": "string"}},
                ["patch_text"],
            ),
            handler=apply_patch,
        ),
        "run_tests": ToolSpec(
            name="run_tests",
            description="执行预定义测试集:test_set ∈ {'failed', 'regression', 'all'}。",
            parameters=_schema(
                "run_tests",
                "执行测试",
                {"test_set": {"type": "string", "enum": ["failed", "regression", "all"]}},
                [],
            ),
            handler=run_tests,
        ),
        "write_repro": ToolSpec(
            name="write_repro",
            description=(
                "把复现脚本写进任务 scratch(工作区之外,永不进补丁 diff),"
                "返回登记名与内容 hash。之后可用 run_command(profile_id='python_repro') 运行它;"
                "同名不同内容的脚本会被拒绝,换名字即可。"
            ),
            parameters=_schema(
                "write_repro",
                "登记复现脚本",
                {"content": {"type": "string"}, "name": {"type": "string"}},
                ["content"],
            ),
            handler=write_repro,
        ),
        "run_command": ToolSpec(
            name="run_command",
            description=(
                "按受控 profile 执行诊断命令:profile_id ∈ {pytest_collect, pytest_selected,"
                " python_repro, python_compile, ruff_check}。不接受任意 shell 字符串;"
                "参数按 profile 校验(测试 id / 工作区内相对路径 / 已登记的 repro)。"
                "命令在任务的诊断副本内运行,改动不进候选;原始输出已落盘,"
                "可用 read_evidence 按 evidence_id 分页取回。"
                "注意 exit_code=0 只是事实,不构成验收——平台 verify 才是唯一验收链。"
            ),
            parameters=_schema(
                "run_command",
                "执行受控命令",
                {
                    "profile_id": {"type": "string"},
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "target": {"type": "string"},
                            "test_ids": {"type": "array", "items": {"type": "string"}},
                            "paths": {"type": "array", "items": {"type": "string"}},
                            "repro": {"type": "string"},
                        },
                    },
                },
                ["profile_id"],
            ),
            handler=run_command,
        ),
        "git_diff": ToolSpec(
            name="git_diff",
            description="查看当前工作区相对基线的全部改动(只读)。",
            parameters=_schema("git_diff", "查看改动", {}, []),
            handler=git_diff,
        ),
        "reset_workspace": ToolSpec(
            name="reset_workspace",
            description="把工作区恢复到基线快照,丢弃全部改动。",
            parameters=_schema("reset_workspace", "回滚工作区", {}, []),
            handler=reset_to_baseline,
        ),
        "update_memory": ToolSpec(
            name="update_memory",
            description=(
                "把你这一轮的**假设**与**已排除方向**登记进任务记忆(它是跨阶段带过去的,"
                "下一阶段的请求里会原样出现)。hypotheses 每项要 text + status"
                "(hypothesis|supported|refuted)+ refs(用观测行里的 E 引用号,不能编);"
                "excluded 每项要 text + reason。"
                "这里**不能**声明已修复或已验证 —— 判定只由平台的验收链给,写 verified 会被点名拒绝。"
            ),
            parameters=_schema(
                "update_memory",
                "登记假设与排除方向",
                {
                    "hypotheses": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                                "status": {
                                    "type": "string",
                                    "enum": ["hypothesis", "supported", "refuted"],
                                },
                                "refs": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["text"],
                        },
                    },
                    "excluded": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                                "reason": {"type": "string"},
                                "refs": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["text", "reason"],
                        },
                    },
                },
                [],
            ),
            handler=update_memory,
        ),
        "propose_acceptance": ToolSpec(
            name="propose_acceptance",
            description=(
                "提交验收清单提议(diagnose_then_repair 任务专用):给出你认为的失败测试、"
                "回归测试清单与依据。平台会在未修改的快照上重新校验红/绿基线,"
                "然后等待可信用户批准;批准冻结之前不会生产任何补丁。"
                "测试 id 必须精确(模块路径::用例名),两个集合不得重叠。"
            ),
            parameters=_schema(
                "propose_acceptance",
                "提交验收清单提议",
                {
                    "failed_tests": {"type": "array", "items": {"type": "string"}},
                    "regression_tests": {"type": "array", "items": {"type": "string"}},
                    "rationale": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                },
                ["failed_tests", "regression_tests"],
            ),
            handler=propose_acceptance,
        ),
        "ask_user": ToolSpec(
            name="ask_user",
            description=(
                "向可信用户提一个澄清问题(比如两种根因靠代码无从区分、需要用户补充"
                "复现步骤或使用场景)。问题会保存并等待用户回答,等待期间不消耗模型请求;"
                "一次只允许一个未回答问题,回答会作为线索进入会话但**不会**改变测试集、"
                "权限与预算。能靠读代码确认的事不要问它。"
            ),
            parameters=_schema(
                "ask_user",
                "向用户提问",
                {
                    "question": {"type": "string"},
                    "choices": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                },
                ["question"],
            ),
            handler=ask_user,
        ),
        FINISH_TOOL: ToolSpec(
            name=FINISH_TOOL,
            description="结束任务。success=true 表示你确信补丁已修复问题并通过测试。",
            parameters=_schema(
                FINISH_TOOL,
                "结束任务",
                {"success": {"type": "boolean"}, "summary": {"type": "string"}},
                ["success", "summary"],
            ),
            handler=lambda ctx, **kw: ToolResult(ok=True, output=kw),
        ),
        # G13:委派子执行者。不进任何静态阶段白名单(READ_TOOLS/WRITE_TOOLS 都没有它),
        # 由节点在 delegation_enabled 且非 one_shot 时按开关追加——one_shot 天然禁委派。
        "delegate": ToolSpec(
            name="delegate",
            description=(
                "把一项调查/评审/候选修复委派给子执行者(同步等待有界结论)。"
                "role 决定能力:researcher 只读调查+受控诊断、reviewer 只读冻结候选+跑测试、"
                "repair_candidate 在自己的隔离工作区修改并验证。task 给精简委派说明,"
                "evidence_ids 引用父任务已登记的证据(可空),candidate_id 用于指认要评审的"
                "候选(可空)。返回 {role, subtask_id, ok, conclusion, evidence_ids, risks, "
                "candidate_id, candidate_hash, termination, error, ...};termination=refused/"
                "unconfigured 表示名额/预算/配置不足,结论按需吸收,子任务测试通过不自动"
                "成为最终修复依据。"
            ),
            parameters=_schema(
                "delegate",
                "委派子执行者",
                {
                    "role": {
                        "type": "string",
                        "enum": ["researcher", "reviewer", "repair_candidate"],
                    },
                    "task": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "candidate_id": {"type": "string"},
                },
                ["role", "task"],
            ),
            handler=delegate,
        ),
    }


REGISTRY: dict[str, ToolSpec] = _build_registry()


def tool_schemas(allowed_tools: list[str] | None = None) -> list[dict[str, Any]]:
    """模型可见的工具 schema 列表(OpenAI function 格式),按**本阶段能力集合**装配。

    G01/R2 的立身点是"可见面 == 执行面":`run_plain_loop` 把同一个 `allowed_tools`
    既喂这里、又在执行前按同一集合挡人。此前这里无条件吐全部 10 个 schema,阶段白名单
    只在执行侧兜底——模型每次都先调一个本阶段根本没有的手段、拿到一条 "not allowed in
    phase" 再重想,一轮真实请求就这么烧掉(reset_workspace 尤其:它注册在案却不在任何
    阶段白名单里,过去每个阶段都对模型可见、每用必被挡)。

    白名单里出现注册表没有的名字一律当场报错。静默少发一个 schema 是更难查的那类错:
    工具存在、模型看不见、真去调又成 "unknown tool"。`None` = 全量,只留给不区分阶段的
    调用方(与注册表面本身的对账用例)。
    """
    if allowed_tools is None:
        names = list(REGISTRY)
    else:
        unknown = [name for name in allowed_tools if name not in REGISTRY]
        if unknown:
            raise KeyError(f"phase whitelist names are not registered: {unknown}")
        allowed = set(allowed_tools)
        names = [name for name in REGISTRY if name in allowed]
    return [REGISTRY[name].parameters for name in names]


def execute(
    ctx: ToolContext, name: str, args: dict[str, Any], *, round_no: int = 0, state: str = "-"
) -> ToolResult:
    """工具统一入口:边界校验、执行、异常收敛、轨迹记录都从这里过。"""
    started = time.monotonic()
    spec = REGISTRY.get(name)
    if spec is None:
        result = ToolResult.fail(f"unknown tool: {name}")
    else:
        # G12:执行**之前**先说一声。SSE 的 tool_started 与轨迹共享这一条事实
        # (没有第二条进度来源);崩溃在 handler 中途时,事件流上"已开始、无结果"
        # 与孤儿 tool_call 的恢复语义(见 SessionState.repair_orphans)对得上。
        ctx.tracker.record(
            tool="tool_started",
            round_no=round_no,
            state=state,
            input_payload={"tool": name, "call": _summarize_input(name, args)},
        )
        try:
            # R2 整改:参数校验用签名绑定,只把"绑定失败"报成 bad arguments——
            # 此前 except TypeError 会把 handler 函数体内的业务 TypeError 也误报
            bound = inspect.signature(spec.handler).bind(ctx, **args)
        except TypeError as exc:
            result = ToolResult.fail(f"bad arguments for {name}: {exc}")
        else:
            try:
                result = spec.handler(*bound.args, **bound.kwargs)
            except Exception as exc:
                result = ToolResult.fail(f"{type(exc).__name__}: {exc}")

    duration_ms = int((time.monotonic() - started) * 1000)
    if name in MUTATING_TOOLS and result.ok:
        ctx.workspace_revision += 1
    # 有界视图 + 原文落盘(G05):必须在轨迹记录之前做完,后面那条 evidence_bounded
    # 事件才有 id 可指,而主工具事件仍按**未裁**的 output 记摘要(裁过的形状进不了取证链,
    # 会重演 M16.9 那类"模型当时看到过什么不可复原")。
    _apply_visible_view(ctx, name, result, round_no=round_no, state=state)
    # 观测登记(G06)紧随其后:失效标记已经打在代数递增那一行,这条观测因此带着正确的代数
    _record_memory(ctx, name, args, result, round_no=round_no, state=state)
    ctx.tracker.record(
        tool=name,
        round_no=round_no,
        state=state,
        input_payload=_summarize_input(name, args),
        output_summary=_summarize_output(name, result),
        duration_ms=duration_ms,
        error=result.error,
    )
    # AGENTS.md:业务日志必须带 task_id(R2 整改,此前工具日志无法归因到任务)
    log.info(
        "task %s tool %s ok=%s (%sms) err=%s",
        ctx.task_id,
        name,
        result.ok,
        duration_ms,
        result.error,
    )
    return result


def _apply_visible_view(
    ctx: ToolContext, name: str, result: ToolResult, *, round_no: int, state: str
) -> object | None:
    """把这条回执裁进模型可见上限,同时把**裁前的原文**落进任务证据仓。

    三条硬约束:
    1. 裁在数据结构上、序列化之前 —— 发出去的消息永远是合法 JSON(旧路径切在 JSON 中间);
    2. 只有真的动了才加 `_bounded`/`evidence` 两个键。装得下的回执形状一字不变,
       存量轨迹与两臂对照解析的形状不受影响(也符合"不新增未登记的差异");
    3. 落盘失败(超体积/超配额)必须**明说** `retrievable=false`,不做静默丢弃。
    """
    settings = get_settings()
    cap = settings.tool_visible_max_chars
    if cap <= 0:
        return None
    payload: Any = result.output if result.ok else {"error": result.error}
    report = bound_structure(
        payload,
        max_chars=cap,
        head_lines=settings.refine_head_lines,
        tail_lines=settings.refine_tail_lines,
    )
    if not report.changed:
        return None
    raw_json = json.dumps(payload, ensure_ascii=False, default=str)
    ref: EvidenceRef | None = None
    if settings.evidence_enabled:
        if ctx.evidence is None:
            ctx.evidence = build_store(ctx.report_dir, ctx.task_id)
        try:
            ref = ctx.evidence.save(
                raw_json, source=name, workspace_revision=ctx.workspace_revision
            )
        except EvidenceNotStored as exc:
            ref = exc.ref  # 拒落也带着身份:字节数/hash 仍然可对上,只是取不回来
    visible = report.value if isinstance(report.value, dict) else {"value": report.value}
    visible["_bounded"] = report.metadata(visible_max_chars=cap)
    if ref is None:
        visible["_bounded"]["raw_saved"] = False
        visible["_bounded"]["note"] = (
            "evidence store is disabled: the omitted lines are not retrievable in this run"
        )
    else:
        visible["evidence"] = ref.as_dict()
    result.visible = visible
    result.evidence = ref
    excerpt_json = json.dumps(visible, ensure_ascii=False, default=str)
    summary: dict[str, Any] = {
        "evidence_id": ref.evidence_id if ref is not None else "",
        "retrievable": bool(ref.retrievable) if ref is not None else False,
        "raw_chars": report.raw_chars,
        "shown_chars": len(excerpt_json),
        "omitted_lines": report.omitted_lines,
        "dropped_items": report.dropped_items,
        "raw_sha256": _sha256(raw_json),
        "excerpt_sha256": _sha256(excerpt_json),
        "workspace_revision": ref.workspace_revision if ref is not None else ctx.workspace_revision,
    }
    if ref is not None and not ref.retrievable:
        summary["reason"] = ref.reason
    # 这条事件回答的是取证问题:"原文是什么形状、excerpt 是什么形状、还能不能取回"。
    # 主工具事件仍按未裁的 output 记摘要,两者一起才拼得出"模型当时看到的那一屏"。
    ctx.tracker.record(
        tool="evidence_bounded",
        round_no=round_no,
        state=state,
        input_payload={"tool": name},
        output_summary=summary,
    )
    return visible


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def model_payload(result: ToolResult) -> Any:
    """发回给模型的那一份:被裁过就是「有界视图 + 元数据」,没裁过就是原 output。

    循环层只走这一个入口。留着第二个各自 `json.dumps(result.output)` 的地方,就等于留着
    第二条绕过 G05 有界协议的路 —— 而旧缺陷恰恰长在"序列化之后再按字符硬切"那一步。
    """
    if not result.ok:
        return {"error": result.error}
    return result.visible if result.visible is not None else result.output


def _record_memory(
    ctx: ToolContext,
    name: str,
    args: dict[str, Any],
    result: ToolResult,
    *,
    round_no: int,
    state: str,
) -> None:
    """把这条真实回执登记成任务记忆里的一条**观测**(G06;模型一个字都没参与)。

    `ctx.memory` 为 None(候选 ctx、离线脚本、既有测试夹具)时直接返回 —— 没接记忆的每一次
    运行必须与今天逐字节相同,这是"默认不改变行为"的接线纪律,不是可选优化。
    """
    memory = ctx.memory
    if memory is None:
        return
    if name in MUTATING_TOOLS and result.ok:
        # 写之前的观测全部失效:补丁改了文件,那些行范围就不再是现状
        memory.mark_stale(from_revision=ctx.workspace_revision)
    if name in SELF_RECORDING_TOOLS:
        return
    detail, path, line_range, verdict = describe_receipt(
        name, args, result.output, error=result.error
    )
    if not detail:
        return
    ref = result.evidence
    bounded = result.visible.get("_bounded") if isinstance(result.visible, dict) else None
    entry = memory.record_evidence(
        source=name,
        stage=state,
        round_no=round_no,
        detail=detail,
        path=path,
        line_range=line_range,
        # 写类工具的这条观测描述的是**写完之后**的状态,所以带新代数;而 mark_stale 已经把
        # 写之前那些观测标失效了 —— 两者一起才让"哪条还是现状"这一问题有确定答案
        workspace_revision=ctx.workspace_revision,
        evidence_id=ref.evidence_id if ref is not None else "",
        sha256=ref.sha256 if ref is not None else "",
        retrievable=bool(ref.retrievable) if ref is not None else False,
        # 观测里写"模型那一屏被折掉了多少行",下一阶段的模型才知道上一阶段看到的是摘要
        omitted_lines=int(bounded.get("omitted_lines") or 0) if isinstance(bounded, dict) else 0,
        verdict=verdict,
    )
    if ctx.session is not None:
        ctx.session.memory_version = memory.version
        ctx.session.note_evidence(entry.ref)


def _summarize_input(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """轨迹里的输入摘要:大字段(diff、长文本)截断。"""
    out: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, str) and len(value) > 300:
            out[key] = value[:300] + f"... ({len(value)} chars)"
        else:
            out[key] = value
    return out


def _summarize_output(name: str, result: ToolResult) -> object:
    if not result.ok:
        return {"error": result.error}
    output = result.output
    if isinstance(output, dict) and "diff" in output:
        summary = dict(output)
        summary["diff"] = f"<{len(summary['diff'])} chars, see patches>"
        return summary
    if isinstance(output, dict):
        keys = list(output)
        summary = {k: output[k] for k in keys[:8]}
        if len(keys) > 8:
            # 丢键必须留痕:静默截断会让"模型当时看到过什么"在证据链里不可复原(M16.9 同族)
            summary["_omitted_keys"] = f"+{len(keys) - 8} keys omitted"
        return summary
    return output
