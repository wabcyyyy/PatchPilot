"""工具注册表:名称、schema、边界校验与统一入口。

`finish` 是循环控制用的伪工具:出现在模型可见的 schema 里,
由 Agent 循环本身消费,不会触达工作区。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.tools.base import ToolContext, ToolResult
from app.tools.execution import run_tests
from app.tools.files import list_files, read_file, search_code
from app.tools.patching import apply_patch, git_diff, reset_to_baseline

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
            description="在仓库中做大小写不敏感的子串搜索,返回匹配行与位置。",
            parameters=_schema(
                "search_code",
                "搜索代码",
                {"keyword": {"type": "string"}, "glob": {"type": "string"}},
                ["keyword"],
            ),
            handler=search_code,
        ),
        "read_file": ToolSpec(
            name="read_file",
            description="读取文本文件的一段内容(offset 为 1-based 行号),单次有行数上限。",
            parameters=_schema(
                "read_file",
                "读取文件",
                {"path": {"type": "string"}, "offset": {"type": "integer"}},
                ["path"],
            ),
            handler=read_file,
        ),
        "apply_patch": ToolSpec(
            name="apply_patch",
            description="应用 unified diff 补丁。禁止修改测试文件;路径与文件数受门禁限制。",
            parameters=_schema(
                "apply_patch",
                "应用补丁",
                {"diff_text": {"type": "string"}},
                ["diff_text"],
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
            result = spec.handler(ctx, **args)
        except TypeError as exc:
            result = ToolResult.fail(f"bad arguments for {name}: {exc}")
        except Exception as exc:  # noqa: BLE001 - 工具层必须把异常收敛为结果
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
    log.info("tool %s ok=%s (%sms) err=%s", name, result.ok, duration_ms, result.error)
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
        return {k: output[k] for k in list(output)[:8]}
    return output
