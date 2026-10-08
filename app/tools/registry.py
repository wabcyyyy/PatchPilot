"""工具注册表:名称、schema、边界校验与统一入口。

`finish` 是循环控制用的伪工具:出现在模型可见的 schema 里,
由 Agent 循环本身消费,不会触达工作区。
"""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.context.ast_outline import SYMBOL_KINDS
from app.tools.base import ToolContext, ToolResult
from app.tools.execution import run_tests
from app.tools.files import list_files, read_file, search_code
from app.tools.patching import apply_patch, git_diff, reset_to_baseline
from app.tools.symbols import describe_file, find_symbol

log = logging.getLogger(__name__)

FINISH_TOOL = "finish"


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
    }


REGISTRY: dict[str, ToolSpec] = _build_registry()


def tool_schemas(include_finish: bool = True) -> list[dict[str, Any]]:
    """模型可见的工具 schema 列表(OpenAI function 格式)。"""
    return [
        spec.parameters for name, spec in REGISTRY.items() if include_finish or name != FINISH_TOOL
    ]


def execute(
    ctx: ToolContext, name: str, args: dict[str, Any], *, round_no: int = 0, state: str = "-"
) -> ToolResult:
    """工具统一入口:边界校验、执行、异常收敛、轨迹记录都从这里过。"""
    started = time.monotonic()
    spec = REGISTRY.get(name)
    if spec is None:
        result = ToolResult.fail(f"unknown tool: {name}")
    else:
        try:
            # R2 整改:参数校验用签名绑定,只把"绑定失败"报成 bad arguments——
            # 此前 except TypeError 会把 handler 函数体内的业务 TypeError 也误报
            bound = inspect.signature(spec.handler).bind(ctx, **args)
            result = spec.handler(*bound.args, **bound.kwargs)
        except TypeError as exc:
            result = ToolResult.fail(f"bad arguments for {name}: {exc}")
        except Exception as exc:
            result = ToolResult.fail(f"{type(exc).__name__}: {exc}")

    duration_ms = int((time.monotonic() - started) * 1000)
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
