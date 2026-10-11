"""G10:验收清单提议、红/绿基线校验与 manifest 冻结的安全性测试。

责任边界(SPEC §7 G10):提议只是候选;平台在**未修改的快照副本**上重做红/绿
基线校验;批准之后冻结的 manifest 是 apply_patch 的唯一放行凭据,冻结后不可增删,
磁盘内容与登记 hash 不符即拒(篡改防线)。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from app.diagnosis.proposal import (
    APPROVED_BY_TRUSTED_USER,
    AcceptanceProposal,
    ManifestIntegrityError,
    freeze_manifest,
    load_manifest,
    load_proposal,
    verify_baseline,
    write_proposal,
)
from app.errors import ConflictError, InvalidRequestError
from app.tools.base import ToolContext
from app.tools.patching import apply_patch
from app.tools.tracker import Tracker
from tests.test_service_robustness import _materialize_tiny_repo

FAILED_ONE = "tests/test_dateparse.py::test_empty_string_returns_none"
REGRESSION = [
    "tests/test_dateparse.py::test_iso_format",
    "tests/test_dateparse.py::test_none_returns_none",
]


def _proposal(run_dir: Path, *, verified: bool = False, revision: int = 0) -> AcceptanceProposal:
    proposal = AcceptanceProposal.build(
        task_id="CUSTOM-g10",
        failed_tests=[FAILED_ONE],
        regression_tests=list(REGRESSION),
        rationale="空串未按文档返回 None",
        evidence_ids=["E1"],
        source_snapshot_hash="a" * 64,
        task_spec_hash="b" * 64,
    )
    proposal.revision = revision
    proposal.baseline = {"verified": verified}
    write_proposal(run_dir, proposal)
    return proposal


def test_proposal_build_rejects_empty_overlap_and_bad_identity() -> None:
    """空集 / 重叠 / 非法测试身份在构建期拒绝,不写半截工件。"""
    kwargs = {
        "task_id": "T",
        "rationale": "",
        "evidence_ids": [],
        "source_snapshot_hash": "a" * 64,
        "task_spec_hash": "b" * 64,
    }
    with pytest.raises(InvalidRequestError):
        AcceptanceProposal.build(
            failed_tests=[],
            regression_tests=list(REGRESSION),
            **kwargs,  # type: ignore[arg-type]
        )
    with pytest.raises(InvalidRequestError):
        AcceptanceProposal.build(
            failed_tests=[FAILED_ONE],
            regression_tests=[],
            **kwargs,  # type: ignore[arg-type]
        )
    with pytest.raises(InvalidRequestError):
        AcceptanceProposal.build(
            failed_tests=[FAILED_ONE],
            regression_tests=[FAILED_ONE],
            **kwargs,  # type: ignore[arg-type]
        )
    with pytest.raises(InvalidRequestError):
        AcceptanceProposal.build(
            failed_tests=["tests/test_x.py::test_a; rm -rf"],
            regression_tests=[REGRESSION[0]],
            **kwargs,  # type: ignore[arg-type]
        )


def test_verify_baseline_real_repo_red_green(tmp_path: Path) -> None:
    """真实小仓库:红/绿基线按提议校验通过;选择器不精确时点名无关失败并拒绝。"""
    repo = tmp_path / "repo"
    _materialize_tiny_repo(repo)
    check = verify_baseline(
        python_exe=sys.executable,
        snapshot_dir=repo,
        failed_tests=[FAILED_ONE],
        regression_tests=list(REGRESSION),
        work_dir=tmp_path / "chk",
        timeout_seconds=120,
    )
    assert check.verified
    assert check.red_ok and check.green_ok
    assert len(check.red_failed_ids) == 1
    assert check.red_failed_ids[0].endswith("test_empty_string_returns_none")
    assert check.unrelated_failures == []

    # 文件路径不是具体 node id:红集实际失败落在提议集合之外 ⇒ 无关失败被点名
    loose = verify_baseline(
        python_exe=sys.executable,
        snapshot_dir=repo,
        failed_tests=["tests/test_dateparse.py"],
        regression_tests=[REGRESSION[0]],
        work_dir=tmp_path / "chk-loose",
        timeout_seconds=120,
    )
    assert not loose.verified
    assert len(loose.unrelated_failures) == 1
    assert loose.unrelated_failures[0].endswith("test_empty_string_returns_none")


def test_verify_baseline_collection_error_and_not_green(tmp_path: Path) -> None:
    """收集错误与"回归集本身不绿"是两类事实,都不许被读成"已红/已绿"。"""
    repo = tmp_path / "repo"
    _materialize_tiny_repo(repo)
    missing = verify_baseline(
        python_exe=sys.executable,
        snapshot_dir=repo,
        failed_tests=["tests/test_dateparse.py::test_does_not_exist"],
        regression_tests=[REGRESSION[0]],
        work_dir=tmp_path / "chk-missing",
        timeout_seconds=120,
    )
    assert not missing.verified
    assert missing.collection_error
    assert not missing.red_ok

    not_green = verify_baseline(
        python_exe=sys.executable,
        snapshot_dir=repo,
        failed_tests=[FAILED_ONE],
        regression_tests=[FAILED_ONE, REGRESSION[0]],
        work_dir=tmp_path / "chk-not-green",
        timeout_seconds=120,
    )
    # 该用例同时出现在红集与绿集,提议构建期就会拒;这里直接构造校验读数,
    # 证明"回归集不绿"不会被当成通过
    assert not not_green.verified
    assert not not_green.green_ok


def test_freeze_requires_verified_baseline_and_matching_identity(tmp_path: Path) -> None:
    """未验证的基线不许冻结;hash/revision 不符是冲突;正确批准幂等且可重读。"""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    unverified = _proposal(run_dir, verified=False)
    with pytest.raises(InvalidRequestError):
        freeze_manifest(
            run_dir,
            expected_hash=unverified.content_hash(),
            expected_revision=unverified.revision,
            approved_by=APPROVED_BY_TRUSTED_USER,
        )

    verified = _proposal(run_dir, verified=True, revision=2)
    with pytest.raises(ConflictError):
        freeze_manifest(
            run_dir,
            expected_hash=verified.content_hash(),
            expected_revision=1,  # 过期 revision
            approved_by=APPROVED_BY_TRUSTED_USER,
        )
    with pytest.raises(ConflictError):
        freeze_manifest(
            run_dir,
            expected_hash="0" * 64,  # 与盘上提议不符
            expected_revision=verified.revision,
            approved_by=APPROVED_BY_TRUSTED_USER,
        )

    manifest, already = freeze_manifest(
        run_dir,
        expected_hash=verified.content_hash(),
        expected_revision=verified.revision,
        approved_by=APPROVED_BY_TRUSTED_USER,
    )
    assert already is False
    assert manifest.manifest_hash == verified.content_hash()
    assert manifest.approved_by == APPROVED_BY_TRUSTED_USER
    assert manifest.failed_tests == [FAILED_ONE]

    reloaded = load_manifest(run_dir)
    assert reloaded is not None and reloaded.manifest_hash == manifest.manifest_hash
    # 重复相同批准:幂等成功;不同 hash 的"原地变更"一律冲突
    again, already2 = freeze_manifest(
        run_dir,
        expected_hash=verified.content_hash(),
        expected_revision=verified.revision,
        approved_by=APPROVED_BY_TRUSTED_USER,
    )
    assert already2 is True and again.manifest_hash == manifest.manifest_hash
    with pytest.raises(ConflictError):
        freeze_manifest(
            run_dir,
            expected_hash="1" * 64,
            expected_revision=verified.revision,
            approved_by=APPROVED_BY_TRUSTED_USER,
        )


def test_manifest_and_proposal_tamper_are_refused(tmp_path: Path) -> None:
    """磁盘上的提议/清单与登记 hash 不符 = 不可信,读侧一律拒绝。"""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    verified = _proposal(run_dir, verified=True)
    freeze_manifest(
        run_dir,
        expected_hash=verified.content_hash(),
        expected_revision=verified.revision,
        approved_by=APPROVED_BY_TRUSTED_USER,
    )
    manifest_path = run_dir / "acceptance_manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["failed_tests"] = [REGRESSION[0]]  # 试图把冻结清单换一个集合
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ManifestIntegrityError):
        load_manifest(run_dir)

    proposal_path = run_dir / "acceptance_proposal.json"
    pdata = json.loads(proposal_path.read_text(encoding="utf-8"))
    pdata["regression_tests"] = [FAILED_ONE]
    proposal_path.write_text(json.dumps(pdata), encoding="utf-8")
    with pytest.raises(ManifestIntegrityError):
        load_proposal(run_dir)


def test_apply_patch_blocked_until_manifest_frozen(tmp_path: Path) -> None:
    """批准前不生产补丁:工具层直接拒绝,不依赖图路由(结构保证)。"""
    tracker = Tracker(tmp_path / "trajectory.jsonl", task_id="CUSTOM-g10")
    ctx = ToolContext(
        task_id="CUSTOM-g10",
        workspace=tmp_path / "workspace",
        baseline_commit="0" * 40,
        tracker=tracker,
        mode="diagnose_then_repair",
    )
    blocked = apply_patch(ctx, "")
    assert blocked.ok is False
    assert "not frozen" in (blocked.error or "")

    ctx.acceptance_frozen = True
    allowed = apply_patch(ctx, "")
    assert allowed.ok is False
    assert "not frozen" not in (allowed.error or "")
