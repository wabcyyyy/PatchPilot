"""回放语料迁移:`apply_patch` step 的 unified diff → 块协议(卡2 的机械转换)。

用法:
    python scripts/convert_replays.py --dry-run      # 只报会改到哪些文件与 step 数
    python scripts/convert_replays.py                 # 原地写回

规则:
- 递归 `--bugs-root` 下所有 `replay/*.json`(顶层 BUG-*、candidates/*、attacks/* 都算);
- 顶层为 list 或 `{"steps": [...]}` 两种形态都支持;
- 只动 `tool == "apply_patch"` 且 args 里有 `diff_text` 的 step:
  `patch_text = unified_to_block(diff_text)`,`diff_text` 键删除;
- 幂等:已迁移过(args 只有 `patch_text`)的 step 直接跳过,可反复重跑核对;
- 写回保持原文件形态:UTF-8 无 BOM、LF、indent=2、ensure_ascii=False、结尾无换行——
  与现存 73 个回放文件逐字节同构,避免整文件重写噪声。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.gitops.blockpatch import (  # noqa: E402  (需先注入 sys.path)
    BlockPatchError,
    unified_to_block,
)


def _steps_of(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [s for s in data if isinstance(s, dict)]
    if isinstance(data, dict):
        steps = data.get("steps")
        if isinstance(steps, list):
            return [s for s in steps if isinstance(s, dict)]
    raise ValueError("顶层既不是 list 也不是 dict-with-steps")


def convert_replay_file(path: Path, dry: bool) -> int:
    """转换单个回放文件,返回被改动的 apply_patch step 数。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    steps = _steps_of(data)
    changed = 0
    for step in steps:
        if step.get("tool") != "apply_patch":
            continue
        args = step.get("args")
        if not isinstance(args, dict):
            continue
        if "diff_text" not in args:
            continue  # 已迁移(只有 patch_text)或形态异常:幂等跳过
        block = unified_to_block(str(args["diff_text"]))
        if not dry:
            args.pop("diff_text")
            args["patch_text"] = block
        changed += 1
    if changed and not dry:
        text = json.dumps(data, ensure_ascii=False, indent=2)
        path.write_bytes(text.encode("utf-8"))  # 无 BOM、LF、不追加结尾换行
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回放语料迁移到 apply_patch 块协议")
    parser.add_argument("--bugs-root", default="bugs", help="题目根目录(默认 bugs)")
    parser.add_argument("--dry-run", action="store_true", help="只统计,不写回")
    args = parser.parse_args(argv)

    root = Path(args.bugs_root)
    files = sorted(root.glob("**/replay/*.json"))
    if not files:
        print(f"[convert_replays] {root} 下没有找到 replay/*.json", file=sys.stderr)
        return 1

    total_steps = 0
    touched = 0
    failures: list[str] = []
    for path in files:
        try:
            changed = convert_replay_file(path, args.dry_run)
        except (BlockPatchError, ValueError, json.JSONDecodeError) as exc:
            failures.append(f"{path.as_posix()}: {type(exc).__name__}: {exc}")
            continue
        if changed:
            touched += 1
            total_steps += changed
            print(f"{path.as_posix()}: {changed} step")

    mode = "dry-run" if args.dry_run else "written"
    print(
        f"[convert_replays] {mode}: {touched}/{len(files)} 文件, {total_steps} 个 apply_patch step"
    )
    for line in failures:
        print(f"  FAIL {line}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
