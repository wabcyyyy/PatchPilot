"""P06-2: 原始事实留存与公开视图脱敏测试。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.events import sse_frame
from app.storage.repository import Repository


def test_sse_frame_sanitization() -> None:
    payload = {
        "event_id": 1,
        "tool": "read_file",
        "output_summary": {
            "token": "sk-1234567890abcdef12345678",
            "password": "supersecretpassword",
            "normal": "hello",
        },
    }
    frame = sse_frame(event_id=1, event_type="tool_finished", payload=payload)
    assert "sk-1234567890abcdef12345678" not in frame
    assert "supersecretpassword" not in frame
    assert "[REDACTED_SECRET:TOKEN]" in frame
    assert "[REDACTED_SECRET:PASSWORD]" in frame
    assert "hello" in frame


def test_public_views_redaction(tmp_path: Path) -> None:
    db_path = tmp_path / "patchpilot.sqlite3"
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    repo = Repository(db_path)
    principal = repo.create_principal(principal_id="sec-admin", name="SecUser", role="admin")
    _cred, raw_key = repo.issue_credential(principal_id=principal["id"])
    app = create_app(db_path=db_path, runs_root=runs_dir)

    # 1. 模拟一个任务及其 run_dir 和 report.json
    task_id = "TASK-SECRET-01"
    task_run_dir = runs_dir / task_id
    task_run_dir.mkdir(parents=True, exist_ok=True)

    raw_report = {
        "task_id": task_id,
        "bug_id": "test_bug",
        "verdict": "resolved",
        "status": "FINISHED",
        "engine": "graph",
        "model_provider": "openai",
        "error": "Failed with token sk-9876543210abcdef98765432 and pwd password=mysecretpass123",
        "api_key": "sk-9876543210abcdef98765432",
        "patch_diff": "diff --git a/a.py b/a.py",
    }
    report_file = task_run_dir / "report.json"
    report_file.write_text(json.dumps(raw_report), encoding="utf-8")

    # 创建任务记录与轨迹记录
    repo.create_task(
        task_id=task_id,
        idem_key="IDEM-01",
        bug_id="test_bug",
        repo_path=str(tmp_path),
        issue_text="Issue text",
        max_rounds=5,
        engine="graph",
        model_provider="openai",
        run_dir=str(task_run_dir),
    )
    repo.insert_events(
        task_id,
        [
            {
                "event_id": "EVT-01",
                "round": 1,
                "state": "ACT",
                "tool": "read_file",
                "request_id": "REQ-01",
                "input": {"path": "config.py"},
                "output_summary": {"secret": "sk-secrettoken1234567890"},
                "timestamp": "2026-10-16T00:00:00Z",
            }
        ],
    )

    # 2. 验证原始文件未被污染 (保留原始事实)
    disk_content = report_file.read_text(encoding="utf-8")
    assert "sk-9876543210abcdef98765432" in disk_content
    assert "mysecretpass123" in disk_content

    # 3. 验证通过 API 访问时已脱敏
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {raw_key}"}

        # 3.1 GET /api/tasks/{task_id}/report (JSON 视图) 已脱敏
        res_json = client.get(f"/api/tasks/{task_id}/report", headers=headers)
        assert res_json.status_code == 200
        report_data = res_json.json()
        assert "sk-9876543210abcdef98765432" not in str(report_data)
        assert "mysecretpass123" not in str(report_data)
        assert "[REDACTED_SECRET:TOKEN]" in str(report_data)

        # 3.2 GET /api/tasks/{task_id}/report?format=markdown (Markdown 视图) 已脱敏
        res_md = client.get(f"/api/tasks/{task_id}/report?format=markdown", headers=headers)
        assert res_md.status_code == 200
        assert "sk-9876543210abcdef98765432" not in res_md.text
        assert "mysecretpass123" not in res_md.text
        assert "[REDACTED_SECRET:TOKEN]" in res_md.text

        # 3.3 GET /api/tasks/{task_id}/trajectory 已脱敏
        res_traj = client.get(f"/api/tasks/{task_id}/trajectory", headers=headers)
        assert res_traj.status_code == 200
        traj_data = res_traj.json()
        assert "sk-secrettoken1234567890" not in str(traj_data)
        assert "[REDACTED_SECRET:TOKEN]" in str(traj_data)
