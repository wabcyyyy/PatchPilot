"""单任务命令行入口。

用法:
    python -m app.evals.run_single --bug BUG-001 --model fake --out runs
    python -m app.evals.run_single --bug bugs/BUG-002 --model openai --out runs
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.config import get_settings
from app.evals.bugset import load_bug, load_replay_script
from app.evals.driver import run_task
from app.llm.openai_client import build_model


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PatchPilot 单任务驱动器")
    parser.add_argument("--bug", required=True, help="BUG-xxx 编号或 bugs/ 目录路径")
    parser.add_argument("--model", default="fake", choices=["fake", "openai"])
    parser.add_argument("--engine", default="plain", choices=["plain", "graph"])
    parser.add_argument("--out", default="runs", help="runs 根目录")
    parser.add_argument("--max-turns", type=int, default=20)
    args = parser.parse_args(argv)

    settings = get_settings()
    bug = load_bug(args.bug)
    script = load_replay_script(bug, kind=args.engine) if args.model == "fake" else None
    model = build_model(args.model, settings, script=script)

    if args.engine == "graph":
        from app.graph.runner import run_task_graph

        result = run_task_graph(bug, model, runs_root=Path(args.out))
    else:
        result = run_task(bug, model, runs_root=Path(args.out), max_turns=args.max_turns)
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    print(f"\n[{bug.id}] status={result.status} verdict={result.verdict} -> {result.run_dir}")
    return 0 if result.verdict == "resolved" else 1


if __name__ == "__main__":
    sys.exit(main())
