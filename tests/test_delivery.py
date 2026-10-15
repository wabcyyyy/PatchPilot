"""测试补丁审核与独立 CI 交付 (P16)。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.delivery.bundle import (
    create_delivery_bundle,
    evaluate_delivery_readiness,
    verify_delivery_manifest,
)
from app.errors import InvalidRequestError
from app.storage.repository import Repository


def _setup_task(tmp_path: Path, task_id: str = "T-DELIV") -> tuple[Repository, Path]:
    db_file = tmp_path / "deliv.sqlite3"
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    repo = Repository(db_file)

    t_dir = runs_dir / task_id
    t_dir.mkdir(parents=True, exist_ok=True)
    (t_dir / "diff.patch").write_text("--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n", encoding="utf-8")
    (t_dir / "task_spec.json").write_text('{"schema_version": 1}', encoding="utf-8")
    report_data = {
        "verdict": "resolved",
        "candidate_hash": "cand-hash-12345",
        "source_commit": "git-commit-abcdef",
        "failed_tests_passed": True,
        "regression_tests_passed": True,
        "total_tokens": 1200,
        "total_cost_usd": 0.05,
    }
    (t_dir / "report.json").write_text(json.dumps(report_data), encoding="utf-8")

    repo.create_task(
        task_id=task_id,
        idem_key="cand-hash-12345",
        bug_id="BUG-001",
        repo_path="bugs/BUG-001/repo",
        issue_text="x",
        max_rounds=5,
        engine="graph",
        model_provider="fake",
        run_dir=str(t_dir),
    )
    repo.finalize_task(task_id, "FINISHED", "resolved")
    return repo, t_dir


def test_delivery_bundle_creation_and_tamper_detection(tmp_path: Path) -> None:
    repo, t_dir = _setup_task(tmp_path, "T-BUNDLE")
    task = repo.get_task("T-BUNDLE")
    assert task is not None

    bundle_out = tmp_path / "out_bundle"
    manifest, target_path = create_delivery_bundle(task, t_dir, out_dir=bundle_out)
    assert manifest.task_id == "T-BUNDLE"
    assert manifest.candidate_hash == "cand-hash-12345"
    assert manifest.manifest_hash == manifest.compute_hash()

    # 验证合法包
    is_valid, errors = verify_delivery_manifest(target_path)
    assert is_valid is True
    assert not errors

    # 篡改 patch 内容后校验失败
    (target_path / "diff.patch").write_text("tampered patch", encoding="utf-8")
    is_valid2, errors2 = verify_delivery_manifest(target_path)
    assert is_valid2 is False
    assert any("diff_sha256 mismatch" in e for e in errors2)


def test_model_cannot_self_approve(tmp_path: Path) -> None:
    repo, _ = _setup_task(tmp_path, "T-NOSELF")
    repo.get_or_create_delivery("T-NOSELF", "cand-hash-12345", "HEAD")

    # 模型自审批准必须被拦截
    for forbidden in ("model", "agent", "llm", "patchpilot"):
        with pytest.raises(InvalidRequestError, match="cannot approve"):
            repo.record_review("T-NOSELF", "approved", reviewer=forbidden)

    # 人工审核批准允许
    updated = repo.record_review("T-NOSELF", "approved", reviewer="human-lead", comment="LGTM")
    assert updated["review_status"] == "approved"
    assert updated["reviewed_by"] == "human-lead"


def test_delivery_preconditions_enforcement(tmp_path: Path) -> None:
    repo, _ = _setup_task(tmp_path, "T-PRECOND")
    task = repo.get_task("T-PRECOND")
    assert task is not None
    delivery = repo.get_or_create_delivery("T-PRECOND", "cand-hash-12345", "HEAD")

    # 1. 刚创建时 review_status=pending_review, independent_ci_status=not_started
    ready1, reason1 = evaluate_delivery_readiness(task, delivery)
    assert ready1 is False
    assert "not approved" in reason1

    # 2. 仅 review approved，但 CI 未通过
    delivery = repo.record_review("T-PRECOND", "approved", reviewer="human-reviewer")
    ready2, reason2 = evaluate_delivery_readiness(task, delivery)
    assert ready2 is False
    assert "not passed" in reason2

    # 3. 独立 CI 也通过 -> 满足交付
    delivery = repo.record_independent_ci("T-PRECOND", "passed", ci_run_id="ci-run-888")
    ready3, reason3 = evaluate_delivery_readiness(task, delivery)
    assert ready3 is True
    assert reason3 == "ready_to_deliver"


def test_delivery_api_routes(tmp_path: Path) -> None:
    _setup_task(tmp_path, "T-API")
    db_file = tmp_path / "deliv.sqlite3"
    runs_dir = tmp_path / "runs"

    app = create_app(db_path=db_file, runs_root=runs_dir)
    with TestClient(app) as client:
        # 1. 查询初始交付状态
        r_get = client.get("/api/tasks/T-API/delivery")
        assert r_get.status_code == 200
        d_get = r_get.json()
        assert d_get["review_status"] == "pending_review"
        assert d_get["ready_to_deliver"] is False

        # 2. 尝试交付 (未满足条件返回 409)
        r_fail = client.post("/api/tasks/T-API/deliver")
        assert r_fail.status_code == 409

        # 3. 提交人工审核
        r_rev = client.post(
            "/api/tasks/T-API/review",
            json={"decision": "approved", "comment": "Verified by security lead"},
        )
        assert r_rev.status_code == 200
        assert r_rev.json()["review_status"] == "approved"

        # 4. 上报独立 CI 结果
        r_ci = client.post(
            "/api/tasks/T-API/independent-ci",
            json={"status": "passed", "ci_run_id": "ci-github-actions-101"},
        )
        assert r_ci.status_code == 200
        assert r_ci.json()["independent_ci_status"] == "passed"
        assert r_ci.json()["ready_to_deliver"] is True

        # 5. 执行最终交付
        r_deliv = client.post("/api/tasks/T-API/deliver")
        assert r_deliv.status_code == 200
        d_res = r_deliv.json()
        assert d_res["delivery_status"] == "delivered"
        assert d_res["manifest_hash"] is not None
