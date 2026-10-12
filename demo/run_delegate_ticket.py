"""演示:轻量角色委派——主线把"确认根因"委托给 researcher 子任务(G13)。

与 run_api_ticket 的分工:那三个走完整 API 面;委派的子任务回放脚本按题目目录
`replay/subtasks.json` 装配(G13 的冻结口径),本脚本搭一个临时题目目录走**真实
装载路径**(subtask_model_factory_for),引擎直连。全程离线 FakeLLM。

流程:
  1. 复制 BUG-001 为临时题目,主脚本在 LOCALIZE 调 delegate(researcher);
  2. 子任务在自己的会话/轮次/额度里跑 researcher 脚本(只读,无 apply_patch);
  3. 主线吸收子任务摘要与证据引用,继续 PROPOSE → 验证 → resolved;
  4. 报告 subtasks 段:角色/终止原因/计量;子任务轨迹单独落盘;父账本入账。
前置:PATCHPILOT_DELEGATION_ENABLED=true(委派默认关闭,这是显式开启的演示)。
任一环节失败返回非零退出码。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

# 委派默认关闭;演示即"显式开启"这件事本身,必须在任何 app 模块导入前设好环境。
os.environ.setdefault("PATCHPILOT_DELEGATION_ENABLED", "true")

ROOT = Path(__file__).resolve().parent.parent
BUG001 = ROOT / "bugs" / "BUG-001"


def _step(title: str, ok: bool, detail: str = "") -> None:
    mark = "OK  " if ok else "FAIL"
    print(f"[{mark}] {title}" + (f" — {detail}" if detail else ""))
    if not ok:
        raise AssertionError(f"demo step failed: {title}: {detail}")


def _make_bug(dest_root: Path) -> Path:
    """复制 BUG-001 为临时题目:主脚本 LOCALIZE 调 delegate,子任务配冻结脚本。"""
    bug_dir = dest_root / "BUG-SUB-DEMO"
    shutil.copytree(BUG001, bug_dir)
    manifest = (
        (bug_dir / "manifest.yaml")
        .read_text(encoding="utf-8")
        .replace("id: BUG-001", "id: BUG-SUB-DEMO", 1)
    )
    (bug_dir / "manifest.yaml").write_text(manifest, encoding="utf-8", newline="\n")
    # 已验证可用的补丁直接取自 BUG-001 的回放脚本(与 demo 工作区无关,不手写锚点)
    steps = json.loads((BUG001 / "replay" / "graph-script.json").read_text(encoding="utf-8"))
    patch_text = next(s["args"]["patch_text"] for s in steps if s.get("tool") == "apply_patch")
    localize = [
        {"tool": "search_code", "args": {"keyword": "parse_date"}},
        {
            "tool": "delegate",
            "args": {"role": "researcher", "task": "确认根因", "evidence_ids": ["ev1"]},
        },
        {"tool": "finish", "args": {"success": True, "summary": "根因:空输入未处理"}},
    ]
    propose = [
        {"tool": "apply_patch", "args": {"patch_text": patch_text}},
        {"tool": "run_tests", "args": {"test_set": "failed"}},
        {"tool": "run_tests", "args": {"test_set": "regression"}},
        {"tool": "finish", "args": {"success": True, "summary": "补上空输入处理"}},
    ]
    (bug_dir / "replay" / "graph-script.json").write_text(
        json.dumps(localize + propose, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    (bug_dir / "replay" / "subtasks.json").write_text(
        json.dumps(
            {
                "researcher": [
                    {"tool": "read_file", "args": {"path": "src/dateparse.py"}},
                    {"tool": "finish", "args": {"success": True, "summary": "子任务确认根因"}},
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
        newline="\n",
    )
    return bug_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="轻量角色委派演示(离线)")
    parser.add_argument(
        "--out",
        default=str(ROOT / "runs" / f"demo-delegate-{datetime.now().strftime('%Y%m%d-%H%M%S')}"),
        help="本次演示的输出目录(临时题目与 runs 落在这里)",
    )
    args = parser.parse_args(argv)
    out_root = Path(args.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    from app.agent.subtasks import subtask_model_factory_for
    from app.config import get_settings
    from app.evals.bugset import load_bug, load_replay_script
    from app.graph.runner import run_task_graph
    from app.llm.fake import FakeLLM

    settings = get_settings()
    _step(
        "委派已显式开启(PATCHPILOT_DELEGATION_ENABLED)",
        bool(getattr(settings, "delegation_enabled", False)),
    )

    bug_dir = _make_bug(out_root / "bug")
    bug = load_bug(bug_dir, out_root / "bug")
    result = run_task_graph(
        bug,
        FakeLLM(load_replay_script(bug, kind="graph")),
        runs_root=out_root / "runs",
        subtask_model_factory=subtask_model_factory_for(bug, "fake", settings),
    )

    _step(
        "主线吸收子任务结论后 resolved",
        result.verdict == "resolved",
        f"{result.status}/{result.verdict} {result.error or ''}",
    )
    _step("恰好一次委派", len(result.subtasks) == 1, f"{len(result.subtasks)} subtask(s)")
    sub = result.subtasks[0]
    _step(
        "子任务:researcher / finished / 有计量",
        sub["role"] == "researcher" and sub["ok"] and sub["termination"] == "finished",
        f"turns={sub.get('turns')} tokens={sub.get('tokens_used')}",
    )
    sub_traj = Path(result.run_dir) / "reports" / "sub1" / "trajectory.jsonl"
    _step("子任务轨迹单独落盘", sub_traj.is_file(), str(sub_traj.name))
    # 落选/拒绝面在既有测试覆盖(test_subagent_wiring);这里再核账本维度:
    report = json.loads((Path(result.run_dir) / "report.json").read_text(encoding="utf-8"))
    subtasks_in_report = report.get("subtasks") or []
    _step(
        "报告 subtasks 段逐条登记贡献",
        len(subtasks_in_report) == 1 and subtasks_in_report[0].get("role") == "researcher",
    )

    print("\n轻量角色委派 demo: ALL PASS")
    print(f"> 产物目录:{out_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
