"""演示:API golden path——离线跑通「工单 → 受理 → 幂等 → 进度 → 报告 → 补丁」。

与 run_dirty_ticket 的分工:那个走引擎直连,本脚本走**完整 API 面**
(FastAPI TestClient 进程内,离线 FakeLLM;本地 HTTP 人工演示见 demo/README.md)。

流程(spec S08):
  1. 归一化工单 → POST /api/tasks(custom fake);
  2. 同键重提 → 200 + 同 task_id(在途幂等);
  3. 轮询进度与轨迹(stage/last_event_at 实时可见);
  4. 读报告(resolved、资源状态);
  5. 检查可取补丁(diff.patch 与冻结候选);
  6. 独立任务演示取消;
  7. 门禁拒绝演示(试图改测试文件的回放 → PATCH_REJECTED)。
任一环节失败返回非零退出码。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.app import create_app
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
_DIFF = (
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
PAYLOAD = {
    "repo_path": str(REPO),
    "issue_text": ISSUE,
    "failed_tests": _FAILED,
    "regression_tests": _REGRESSION,
    "allowed_paths": ["src/**"],
    "engine": "graph",
    "model": "fake",
    "replay_script": [
        {"tool": "search_code", "args": {"keyword": "parse_date"}},
        {"tool": "read_file", "args": {"path": "src/dateparse.py"}},
        {"tool": "finish", "args": {"success": True, "summary": "根因:空/空白未返回 None"}},
        {"tool": "apply_patch", "args": {"patch_text": unified_to_block(_DIFF)}},
        {"tool": "run_tests", "args": {"test_set": "failed"}},
        {"tool": "run_tests", "args": {"test_set": "regression"}},
        {"tool": "finish", "args": {"success": True, "summary": "修复完成,回归保持通过"}},
    ],
}


def _wait_terminal(client: TestClient, task_id: str, timeout_s: float = 120) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        body = client.get(f"/api/tasks/{task_id}")
        if body.status_code == 200:
            task = body.json()
            if task["status"] in {
                "FINISHED",
                "INVALID_TASK",
                "BUDGET_EXCEEDED",
                "VERIFY_FAILED",
                "PATCH_REJECTED",
                "NEEDS_REVIEW",
                "CANCELLED",
            }:
                return task
        time.sleep(0.2)
    raise AssertionError(f"task {task_id} did not finish in {timeout_s}s")


def _step(title: str, ok: bool, detail: str = "") -> None:
    mark = "OK  " if ok else "FAIL"
    print(f"[{mark}] {title}" + (f" — {detail}" if detail else ""))
    if not ok:
        raise AssertionError(f"demo step failed: {title}: {detail}")


def run_golden_path(client: TestClient) -> dict:
    """1-5:成功工单的完整链路;返回核对结果摘要。"""
    first = client.post("/api/tasks", json=PAYLOAD)
    _step("POST 创建任务", first.status_code == 201, str(first.json()))
    task_id = first.json()["task_id"]

    second = client.post("/api/tasks", json=PAYLOAD)
    _step(
        "同键重提幂等",
        second.status_code == 200 and second.json()["task_id"] == task_id,
        f"status={second.status_code}",
    )

    final = _wait_terminal(client, task_id)
    _step(
        "任务到终态且 resolved",
        final["status"] == "FINISHED" and final["verdict"] == "resolved",
        f"{final['status']}/{final['verdict']}",
    )

    traj = client.get(f"/api/tasks/{task_id}/trajectory?limit=200").json()
    tools = {e["tool"] for e in traj["events"]}
    _step(
        "轨迹含 apply_patch/run_tests/finish",
        {"apply_patch", "run_tests", "finish"} <= tools,
        f"{len(traj['events'])} events",
    )

    report_resp = client.get(f"/api/tasks/{task_id}/report")
    report = report_resp.json()
    _step(
        "报告 resolved 且资源状态在案",
        report.get("verdict") == "resolved"
        and report.get("resource_status") in {"within_budget", None},
        f"validation={report.get('validation_status')}",
    )

    run_dir = Path(final["run_dir"])
    diff_text = (run_dir / "diff.patch").read_text(encoding="utf-8")
    candidates = list((run_dir / "candidates").glob("*/diff.patch"))
    _step(
        "补丁与冻结候选可读",
        "diff --git" in diff_text and bool(candidates),
        f"{len(candidates)} candidate(s)",
    )
    return {"task_id": task_id, "status": final["status"], "report": report}


def run_cancel_demo(client: TestClient) -> str:
    """6:独立任务的取消演示(与成功任务分开,不混装)。"""
    payload = dict(PAYLOAD)
    payload["issue_text"] = ISSUE + "\n(取消演示:提交后立刻请求取消)"
    first = client.post("/api/tasks", json=payload)
    assert first.status_code == 201, first.text
    task_id = first.json()["task_id"]
    client.post(f"/api/tasks/{task_id}/cancel")
    final = _wait_terminal(client, task_id)
    _step(
        "取消演示到 CANCELLED 终态",
        final["status"] == "CANCELLED",
        f"status={final['status']}",
    )
    return task_id


def run_gate_rejection_demo(client: TestClient) -> str:
    """7:门禁拒绝演示——试图改测试文件的回放,图级门禁必须拦下。"""
    cheat_diff = (
        "--- a/tests/test_dateparse.py\n"
        "+++ b/tests/test_dateparse.py\n"
        "@@ -1,3 +1,4 @@\n"
        " import pytest\n"
        "+\n"
        " from src.dateparse import parse_date\n"
    )
    payload = dict(PAYLOAD)
    # plain 引擎的判定面直接产出 PATCH_REJECTED 终态(graph 的拒绝是过渡态,
    # 会经 rollback 走轮次耗尽——语义不同,演示选 plain 才是门禁拒绝本义)
    payload["engine"] = "plain"
    payload["issue_text"] = ISSUE + "\n(门禁演示:回放试图修改测试文件)"
    payload["replay_script"] = [
        {"tool": "apply_patch", "args": {"patch_text": unified_to_block(cheat_diff)}},
        {"tool": "finish", "args": {"success": True, "summary": "试图改测试作弊"}},
    ]
    resp = client.post("/api/tasks", json=payload)
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["task_id"]
    final = _wait_terminal(client, task_id)
    _step(
        "门禁拒绝演示 PATCH_REJECTED",
        final["status"] == "PATCH_REJECTED",
        f"status={final['status']}",
    )
    return task_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PatchPilot API golden path 演示(离线)")
    parser.add_argument(
        "--out",
        default=str(ROOT / "runs" / f"demo-api-{datetime.now().strftime('%Y%m%d-%H%M%S')}"),
        help="本次演示的输出目录(db 与 runs 落在这里)",
    )
    args = parser.parse_args(argv)
    out_root = Path(args.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    app = create_app(db_path=out_root / "api.sqlite3", runs_root=out_root / "runs")
    with TestClient(app) as client:
        run_golden_path(client)
        run_cancel_demo(client)
        run_gate_rejection_demo(client)
    print("\nAPI golden path demo: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
