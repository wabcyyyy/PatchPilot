"""单任务命令行入口。

用法:
    python -m app.evals.run_single --bug BUG-001 --model fake --out runs
    python -m app.evals.run_single --bug bugs/BUG-002 --model openai --out runs
    (--model openai 需在 .env 配好端点凭据,且 PATCHPILOT_LLM_ENABLED=true)
    python -m app.evals.run_single --bug BUG-001 --model fake --arm one_shot --out runs
    (--arm one_shot = 消融对照臂:单发补丁、无执行反馈、无重试,仅 plain 引擎)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.config import get_settings
from app.evals.bugset import load_bug, load_replay_script
from app.evals.driver import run_task
from app.evals.provenance import require_model_name
from app.llm.base import Model
from app.llm.fake import FakeLLM
from app.llm.openai_client import build_model


def _branch_model_factory(bug, model_kind: str, settings) -> object | None:
    """卡5b 的候选模型工厂(在 eval 入口接线,graph 层不 import 测试替身)。

    - 题目录入时带了 `replay/graph-branches.json`(形态 `{"0": [steps...], "1": [...]}`)
      → 每个候选一个 FakeLLM,分支路径可离线确定性复现;缺某序号的脚本就给空脚本
      (FakeLLM 立刻声明失败 → 该候选"未修好",而不是崩在循环里)。
    - 真实模型 → 每候选一个独立客户端(候选之间不共享会话)。
    - 其余(真实模型未开 / 无分支脚本)→ None:`_should_branch` 短路,单线与 V1 一致。
    """
    branches_path = bug.root / "replay" / "graph-branches.json"
    if branches_path.exists():
        branches = json.loads(branches_path.read_text(encoding="utf-8"))

        def _fake(index: int) -> Model:
            return FakeLLM(list(branches.get(str(index)) or []))

        return _fake
    if model_kind == "openai" and settings.llm_enabled:
        return lambda index: build_model("openai", settings)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PatchPilot 单任务驱动器")
    parser.add_argument("--bug", required=True, help="BUG-xxx 编号或 bugs/ 目录路径")
    parser.add_argument("--model", default="fake", choices=["fake", "openai"])
    parser.add_argument("--engine", default="plain", choices=["plain", "graph"])
    parser.add_argument("--out", default="runs", help="runs 根目录")
    parser.add_argument("--max-turns", type=int, default=20)
    parser.add_argument(
        "--arm",
        default="agent",
        choices=["agent", "one_shot"],
        help="执行体:agent=默认工具循环;one_shot=消融对照臂(单发补丁,无执行反馈),只作用于 plain 引擎",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    bug = load_bug(args.bug)
    script = load_replay_script(bug, kind=args.engine) if args.model == "fake" else None
    model = build_model(args.model, settings, script=script)
    # P1-2 整改:CLI 批次必须携带真实模型名,否则 report.json 无成本与模型身份,
    # "真实模型成绩"的证据链断裂(API 侧 service.py 一直传了)
    real_model_name = settings.llm_model if args.model == "openai" else ""
    # E2 fail-fast:真实模型在线调用缺 model_name 时,发起任务前即拒绝;
    # fake 回放零花费,不受该守卫约束
    require_model_name(real_model_name, settings.llm_enabled and args.model == "openai")

    if args.engine == "graph":
        if args.arm != "agent":
            # 对照臂消融的是"循环形状",而 graph 的循环写在状态机里(禁区)。
            # 要在 graph 上做同样的消融得改节点转移,不能在这里顺手假称支持。
            parser.error("--arm one_shot 只作用于 --engine plain")
        from app.graph.runner import run_task_graph

        result = run_task_graph(
            bug,
            model,
            runs_root=Path(args.out),
            max_turns=args.max_turns,
            model_name=real_model_name,
            branch_model_factory=_branch_model_factory(bug, args.model, settings),
        )
    else:
        # 对照臂按需 import:它的 LOCALIZE 工具集来自 app.graph.nodes(langgraph),
        # 默认臂跑 plain 时不必为此付导入成本
        arm_kwargs: dict[str, object] = {}
        if args.arm == "one_shot":
            from app.evals.single_shot import one_shot_agent

            arm_kwargs = {"arm": "one_shot", "agent": one_shot_agent}
        result = run_task(
            bug,
            model,
            runs_root=Path(args.out),
            max_turns=args.max_turns,
            model_name=real_model_name,
            **arm_kwargs,  # type: ignore[arg-type]
        )
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    print(f"\n[{bug.id}] status={result.status} verdict={result.verdict} -> {result.run_dir}")
    return 0 if result.verdict == "resolved" else 1


if __name__ == "__main__":
    sys.exit(main())
