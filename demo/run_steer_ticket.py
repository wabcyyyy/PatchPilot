"""演示:用户澄清与途中纠偏——ask_user 等待回答 + steer 作为线索进入会话。

与 run_api_ticket 的分工:那个演示取消与门禁拒绝;本脚本演示 G11 的两条输入面,
并把 G16 报告里的"输入事件"段读给你看。全程离线 FakeLLM。

流程:
  1. 修复任务受理(回放脚本中段调用 ask_user,任务 WAITING_INPUT);
  2. 用户先追加一条纠偏(steer:只作为会话线索,不改测试/权限/预算);
  3. 用户回答澄清问题(answer,绑定 question_id 与输入版本 revision);
  4. 任务继续 → resolved;
  5. 报告 evidence.inputs:一次已回答问题 + 一条已消费纠偏;轨迹留消费事件。
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
# 与 run_api_ticket 同一份已验证可用的补丁(demo 工作区副本上可应用且可解)
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
    "issue_text": ISSUE + "\n(澄清演示:调查中段向用户确认验收口径)",
    "failed_tests": _FAILED,
    "regression_tests": _REGRESSION,
    "allowed_paths": ["src/**"],
    "engine": "graph",
    "model": "fake",
    "replay_script": [
        {"tool": "search_code", "args": {"keyword": "parse_date"}},
        {"tool": "read_file", "args": {"path": "src/dateparse.py"}},
        {
            "tool": "ask_user",
            "args": {"question": "空串应当返回 None 还是抛 ValueError?"},
        },
        {"tool": "finish", "args": {"success": True, "summary": "口径已确认,进入修复"}},
        {"tool": "apply_patch", "args": {"patch_text": unified_to_block(_DIFF)}},
        {"tool": "run_tests", "args": {"test_set": "failed"}},
        {"tool": "run_tests", "args": {"test_set": "regression"}},
        {"tool": "finish", "args": {"success": True, "summary": "按确认的口径修复"}},
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
    parser = argparse.ArgumentParser(description="用户澄清与纠偏演示(离线)")
    parser.add_argument(
        "--out",
        default=str(ROOT / "runs" / f"demo-steer-{datetime.now().strftime('%Y%m%d-%H%M%S')}"),
        help="本次演示的输出目录(db 与 runs 落在这里)",
    )
    args = parser.parse_args(argv)
    out_root = Path(args.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    app = create_app(db_path=out_root / "api.sqlite3", runs_root=out_root / "runs")
    with TestClient(app) as client:
        created = client.post("/api/tasks", json=PAYLOAD)
        _step("受理修复任务(201)", created.status_code == 201, str(created.json()))
        task_id = created.json()["task_id"]

        waiting = _wait(client, task_id, lambda t: t["status"] == "WAITING_INPUT")
        _step("任务等待澄清回答", True, waiting["status"])

        question = client.get(f"/api/tasks/{task_id}/question").json()["question"]
        _step("读出问题与输入版本", bool(question), f"revision={question.get('revision')}")

        # 纠偏:只作为会话线索进入任务,不改测试/权限/预算(等待期间也可追加)
        steer = client.post(
            f"/api/tasks/{task_id}/messages",
            json={"kind": "steer", "text": "优先看 src/dateparse.py 的空白处理"},
        )
        _step("追加途中纠偏(steer)", steer.status_code == 200 and steer.json()["accepted"])

        answer = client.post(
            f"/api/tasks/{task_id}/messages",
            json={
                "kind": "answer",
                "text": "空串按文档返回 None",
                "question_id": question["question_id"],
                "expected_revision": question["revision"],
            },
        )
        _step(
            "回答澄清问题(answer,冻结输入版本)",
            answer.status_code == 200 and answer.json()["woke_waiter"],
            str(answer.json()),
        )

        final = _wait(client, task_id, lambda t: t["status"] in TERMINAL)
        _step(
            "任务继续并 resolved",
            final["status"] == "FINISHED" and final["verdict"] == "resolved",
            f"{final['status']}/{final['verdict']}",
        )

        report = client.get(f"/api/tasks/{task_id}/report").json()
        inputs = (report.get("evidence") or {}).get("inputs") or {}
        # 问题生命周期:open → answered → consumed(被循环消费一次);跑完即为 consumed
        answered = [
            q for q in inputs.get("questions") or [] if q.get("status") in ("answered", "consumed")
        ]
        consumed = [s for s in inputs.get("steers") or [] if s.get("consumed")]
        _step(
            "报告输入事件:1 问已答 + 1 纠偏已消费",
            len(answered) == 1 and len(consumed) == 1,
            f"questions={len(answered)} steers={len(consumed)}",
        )
        traj = client.get(f"/api/tasks/{task_id}/trajectory?limit=300").json()["events"]
        tools = {e["tool"] for e in traj}
        _step(
            "轨迹留消费事件(user_answer_consumed/user_steer_consumed)",
            {"user_answer_consumed", "user_steer_consumed"} <= tools,
        )

    print("\n用户澄清与纠偏 demo: ALL PASS")
    print(f"> 产物目录:{out_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
