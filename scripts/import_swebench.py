"""SWE-bench 人工数据准备入口(T10.3;联网与克隆都由人执行,本脚本只校验)。

用法:
    python scripts/import_swebench.py --jsonl data.jsonl --checkouts-root ./checkouts

约定:--checkouts-root 下每个实例一个目录(目录名 = instance_id),
内容是该实例 base_commit 的本地 checkout。本脚本逐实例校验映射可行性并打印摘要,
不下载、不联网、不真实调用模型;真实评测由人按 README 的方式触发。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.errors import TaskError  # noqa: E402
from app.evals.swebench import load_instances, to_bug_task  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验 SWE-bench 本地数据准备")
    parser.add_argument("--jsonl", required=True, help="SWE-bench 格式的本地 jsonl")
    parser.add_argument("--checkouts-root", required=True, help="实例 checkout 的根目录")
    parser.add_argument("--instance", default="", help="只处理指定 instance_id(默认全部)")
    args = parser.parse_args(argv)

    instances = load_instances(args.jsonl)
    if args.instance:
        instances = [i for i in instances if i.instance_id == args.instance]
    root = Path(args.checkouts_root)

    ready, missing = [], []
    for instance in instances:
        try:
            task = to_bug_task(instance, root / instance.instance_id)
        except TaskError as exc:
            missing.append(str(exc))
            continue
        ready.append(task)
        print(
            f"[ready] {task.id}: failed={len(task.failed_tests)}"
            f" pass_to_pass={len(task.regression_tests)} repo={instance.repo}@{instance.base_commit[:8]}"
        )
    for problem in missing:
        print(f"[missing] {problem}")

    print(f"\n{len(ready)} ready / {len(missing)} missing (total {len(instances)})")
    print("next: 真实评测需 PATCHPILOT_LLM_ENABLED=true 并用 run_single 逐题人工触发")
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
