"""演示:issue-only 全流程——诊断 → 用户确认验收清单 → 补丁 → 反馈重试 → 独立验收 → 报告。

与 run_api_ticket 的分工:那个演示"带双测试集的修复受理";本脚本演示 G10 的
issue-only 受理面(不给失败/回归集,由 Agent 诊断提议、**用户批准后才生产补丁**)
外加 G16 的新产品入口口径(显式 agent_profile=repair_v2)。全程离线 FakeLLM。

流程:
  1. repo + issue 受理(无测试集 → diagnose_then_repair,仅 graph);
  2. 诊断段:读码 → propose_acceptance → 任务 WAITING_INPUT;
  3. 用户读提议、批准(POST /acceptance,绑定 content_hash 与 revision);
  4. 修复段第 1 轮试图改测试文件 → 门禁拒绝 → 反馈重试(第 2 轮正确补丁);
  5. 独立验收(双集双跑复核)→ resolved;
  6. 报告:验收清单(已冻结)、候选、运行身份(profile=repair_v2)、三态呈现。
任一环节失败返回非零退出码。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.report import render_markdown
from app.gitops.blockpatch import unified_to_block

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT / "demo" / "workspace-copy"
ISSUE = (ROOT / "demo" / "dirty-ticket" / "normalized-issue.md").read_text(encoding="utf-8")

_FAILED = [
    "tests/test_dateparse.py::test_empty_string_returns_none",
    "tests/test_dateparse.py::test_whitespace_returns_none",
]
_REGRESSION = [
    "tests/test_dateparse.py::test_iso_format",
    "tests/test_dateparse.py::test_slash_format",
    "tests/test_dateparse.py::test_invalid_format_raises",
    "tests/test_dateparse.py::test_none_returns_none",
]
_GOOD_DIFF = (
    "--- a/src/dateparse.py\n"
    "+++ b/src/dateparse.py\n"
    "@@ -9,6 +9,8 @@ def parse_date(value):\n"
    '     """解析日期字符串;空输入返回 None,非法格式抛 ValueError。"""\n'
    "     if value is None:\n"
    "         return None\n"
    "+    if not value.strip():\n"
    "+        return None\n"
    "     for fmt in DATE_FORMATS:\n"
    "         try:\n"
    "             return datetime.strptime(value, fmt).date()\n"
)
_CHEAT_DIFF = (
    "--- a/tests/test_dateparse.py\n"
    "+++ b/tests/test_dateparse.py\n"
    "@@ -1,3 +1,4 @@\n"
    " import pytest\n"
    "+\n"
    " from src.dateparse import parse_date\n"
)
PAYLOAD = {
    "repo_path": str(REPO),
    "issue_text": ISSUE,
    # issue-only:不给 failed/regression → diagnose_then_repair;plain 引擎无法等待批准
    "engine": "graph",
    "model": "fake",
    # G16:新产品入口显式使用 repair_v2(靠显式 profile 区分,不靠服务端环境变量猜)
    "agent_profile": "repair_v2",
    "replay_script": [
        # 诊断段:调查 → 提议验收清单 → 结束(之后任务等待用户批准)
        {"tool": "read_file", "args": {"path": "src/dateparse.py"}},
        {
            "tool": "propose_acceptance",
            "args": {
                "failed_tests": list(_FAILED),
                "regression_tests": list(_REGRESSION),
                "rationale": "空/空白串未按文档返回 None;其余用例保持通过",
            },
        },
        {"tool": "finish", "args": {"success": True, "summary": "根因:空/空白未返回 None"}},
        # 修复段第 1 轮:试图改测试文件 → 门禁拒绝(反馈重试的"反馈"从这来)
        {"tool": "apply_patch", "args": {"patch_text": unified_to_block(_CHEAT_DIFF)}},
        {"tool": "finish", "args": {"success": True, "summary": "试图改测试凑绿"}},
        # 第 2 轮:正确补丁 → 双集验证
        {"tool": "apply_patch", "args": {"patch_text": unified_to_block(_GOOD_DIFF)}},
        {"tool": "run_tests", "args": {"test_set": "failed"}},
        {"tool": "run_tests", "args": {"test_set": "regression"}},
        {"tool": "finish", "args": {"success": True, "summary": "修复完成,回归保持通过"}},
    ],
}

TERMINAL = {
    "FINISHED",
    "INVALID_TASK",
    "BUDGET_EXCEEDED",
    "VERIFY_FAILED",
    "PATCH_REJECTED",
    "NEEDS_REVIEW",
    "CANCELLED",
}


def _wait(client: TestClient, task_id: str, predicate, timeout_s: float = 180) -> dict:
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        last = client.get(f"/api/tasks/{task_id}").json()
        if predicate(last):
            return last
        time.sleep(0.2)
    raise AssertionError(f"condition not met in {timeout_s}s (last={last})")


def _step(title: str, ok: bool, detail: str = "") -> None:
    mark = "OK  " if ok else "FAIL"
    print(f"[{mark}] {title}" + (f" — {detail}" if detail else ""))
    if not ok:
        raise AssertionError(f"demo step failed: {title}: {detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="issue-only 全流程演示(离线)")
    parser.add_argument(
        "--out",
        default=str(ROOT / "runs" / f"demo-issue-only-{datetime.now().strftime('%Y%m%d-%H%M%S')}"),
        help="本次演示的输出目录(db 与 runs 落在这里)",
    )
    args = parser.parse_args(argv)
    out_root = Path(args.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    app = create_app(db_path=out_root / "api.sqlite3", runs_root=out_root / "runs")
    with TestClient(app) as client:
        created = client.post("/api/tasks", json=PAYLOAD)
        _step("issue-only 受理(201)", created.status_code == 201, str(created.json()))
        task_id = created.json()["task_id"]

        waiting = _wait(client, task_id, lambda t: t["status"] == "WAITING_INPUT")
        _step("诊断完成,任务等待验收清单批准", True, waiting["status"])
        run_dir = Path(waiting["run_dir"])
        proposal = json.loads((run_dir / "acceptance_proposal.json").read_text(encoding="utf-8"))
        print(
            f"      提议 {proposal['proposal_id']} — 失败集 {len(proposal['failed_tests'])} 项,"
            f" 回归集 {len(proposal['regression_tests'])} 项(基线红绿已由平台复核)"
        )
        _step(
            "批准前没有任何候选补丁",
            not (run_dir / "candidates").exists(),
        )

        approved = client.post(
            f"/api/tasks/{task_id}/acceptance",
            json={
                "proposal_id": proposal["proposal_id"],
                "content_hash": proposal["content_hash"],
                "expected_revision": proposal["revision"],
            },
        )
        _step("用户批准(冻结 manifest)", approved.status_code == 200, str(approved.json()))

        final = _wait(client, task_id, lambda t: t["status"] in TERMINAL)
        _step(
            "门禁拒绝后反馈重试,终态 resolved",
            final["status"] == "FINISHED" and final["verdict"] == "resolved",
            f"{final['status']}/{final['verdict']}",
        )

        report = client.get(f"/api/tasks/{task_id}/report").json()
        evidence = report.get("evidence") or {}
        acceptance = evidence.get("acceptance") or {}
        rounds = report.get("rounds")
        _step(
            "报告记录已冻结验收清单与两轮重试",
            bool(acceptance.get("frozen")) and int(rounds or 0) >= 2,
            f"frozen={acceptance.get('frozen')} rounds={rounds}",
        )
        identity = evidence.get("run_identity") or {}
        _step(
            "运行身份:profile=repair_v2(显式入口口径)",
            identity.get("agent_profile") == "repair_v2",
            str(identity.get("profile_name")),
        )
        md = render_markdown(report)
        _step(
            "Markdown 三态呈现(每项翻成 已验证/未运行/不确定,原值保留)",
            "验证状态:已验证(passed)" in md
            and "门禁状态:" in md
            and "资源状态:已验证(within_budget)" in md,
        )

    print("\nissue-only 全流程 demo: ALL PASS")
    print(f"> 产物目录:{out_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
