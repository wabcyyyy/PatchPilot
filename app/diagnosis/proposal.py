"""issue-only 诊断与验收清单(G10):提议、红/绿基线校验与 manifest 冻结。

责任边界(SPEC §7 G10):
- **提议 ≠ 可信验收清单**。Agent 通过受控命令/检索提出的失败/回归集只是候选;
  平台在**未修改的快照副本**上重新校验精确测试身份与红/绿基线,校验读数进提议工件;
  未知清单必须 trusted_user 批准才冻结成 AcceptanceManifest。
- **manifest 是唯一放行补丁生产的凭据**。冻结后不可增删/换身份;内容与 hash 不符
  即 IntegrityError(篡改防线);重复批准相同 hash 幂等,不同 hash 冲突。
- **失败分类分开记录**:环境故障(超时/junit 缺失)、收集错误(身份歧义)、测试已红
  (红/绿读数)是三类事实;模型对"哪条失败与 issue 相关"的推断只能进 hypothesis
  (TaskMemory),不能进本工件的判定字段。
- 工件落在 run_dir(用户源仓库之外),不进 patch diff;绝对路径不出现在对外字段。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.errors import ConflictError, InvalidRequestError
from app.executor.command_profiles import check_test_ids

log = logging.getLogger(__name__)

PROPOSAL_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1

PROPOSAL_FILENAME = "acceptance_proposal.json"
MANIFEST_FILENAME = "acceptance_manifest.json"

APPROVED_BY_TRUSTED_USER = "trusted_user"

RC_USAGE_ERROR = 4  # pytest: 用法错误(id/路径非法 → 身份歧义,不是"已红")


class ManifestIntegrityError(Exception):
    """落盘 manifest 的内容 hash 与登记值不符:磁盘上的清单不可信,一律待审。"""


def _stable_hash(obj: Any) -> str:
    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class AcceptanceProposal:
    """一条验收清单提议:Agent 提出,平台在冻结快照上校验红/绿基线。"""

    proposal_id: str
    task_id: str
    failed_tests: list[str]
    regression_tests: list[str]
    rationale: str
    evidence_ids: list[str]
    source_snapshot_hash: str
    task_spec_hash: str
    revision: int = 0
    created_at: str = field(default_factory=_now_iso)
    baseline: dict[str, Any] = field(default_factory=dict)  # 平台红/绿读数;模型不可写

    @staticmethod
    def build(
        *,
        task_id: str,
        failed_tests: list[str],
        regression_tests: list[str],
        rationale: str,
        evidence_ids: list[str] | None,
        source_snapshot_hash: str,
        task_spec_hash: str,
    ) -> "AcceptanceProposal":
        """构建并校验提议;测试身份语法/重叠/空集在这里拒绝(不写半截工件)。"""
        failed = dedupe_tests(failed_tests)
        regression = dedupe_tests(regression_tests)
        if not failed:
            raise InvalidRequestError("acceptance proposal needs at least one failed test")
        if not regression:
            raise InvalidRequestError("acceptance proposal needs at least one regression test")
        overlap = sorted(set(failed) & set(regression))
        if overlap:
            raise InvalidRequestError(
                f"ambiguous test identity: {overlap} appear in both failed and regression sets"
            )
        return AcceptanceProposal(
            proposal_id=uuid.uuid4().hex,
            task_id=task_id,
            failed_tests=failed,
            regression_tests=regression,
            rationale=str(rationale or "").strip(),
            evidence_ids=[str(e) for e in (evidence_ids or [])],
            source_snapshot_hash=source_snapshot_hash,
            task_spec_hash=task_spec_hash,
        )

    def content_hash(self) -> str:
        """批准目标的身份:提议与输入身份绑定;baseline/时间戳刻意不在内
        (同一清单在等待窗内重跑基线校验不改变"用户批准了什么")。"""
        return _stable_hash(
            {
                "schema_version": PROPOSAL_SCHEMA_VERSION,
                "task_id": self.task_id,
                "proposal_id": self.proposal_id,
                "failed_tests": self.failed_tests,
                "regression_tests": self.regression_tests,
                "source_snapshot_hash": self.source_snapshot_hash,
                "task_spec_hash": self.task_spec_hash,
                "revision": self.revision,
            }
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PROPOSAL_SCHEMA_VERSION,
            "proposal_id": self.proposal_id,
            "task_id": self.task_id,
            "failed_tests": list(self.failed_tests),
            "regression_tests": list(self.regression_tests),
            "rationale": self.rationale,
            "evidence_ids": list(self.evidence_ids),
            "source_snapshot_hash": self.source_snapshot_hash,
            "task_spec_hash": self.task_spec_hash,
            "revision": self.revision,
            "created_at": self.created_at,
            "baseline": dict(self.baseline),
            "content_hash": self.content_hash(),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "AcceptanceProposal":
        if int(data.get("schema_version", 0)) != PROPOSAL_SCHEMA_VERSION:
            raise ValueError(f"unsupported proposal schema_version: {data.get('schema_version')}")
        proposal = AcceptanceProposal(
            proposal_id=str(data["proposal_id"]),
            task_id=str(data["task_id"]),
            failed_tests=[str(x) for x in data["failed_tests"]],
            regression_tests=[str(x) for x in data["regression_tests"]],
            rationale=str(data.get("rationale") or ""),
            evidence_ids=[str(x) for x in (data.get("evidence_ids") or [])],
            source_snapshot_hash=str(data["source_snapshot_hash"]),
            task_spec_hash=str(data["task_spec_hash"]),
            revision=int(data.get("revision") or 0),
            created_at=str(data.get("created_at") or ""),
            baseline=dict(data.get("baseline") or {}),
        )
        recorded = data.get("content_hash")
        if recorded and recorded != proposal.content_hash():
            raise ManifestIntegrityError(
                f"proposal content hash mismatch: recorded {recorded[:12]}"
            )
        return proposal


def dedupe_tests(ids: list[str]) -> list[str]:
    """语法校验 + 去重(保留首次出现顺序);非法 id 在这里被点名拒绝。"""
    checked = check_test_ids([str(i) for i in (ids or [])])
    seen: set[str] = set()
    out: list[str] = []
    for item in checked:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def write_proposal(run_dir: Path, proposal: AcceptanceProposal) -> str:
    """原子写 run_dir/acceptance_proposal.json,返回 content_hash。"""
    _write_atomic(Path(run_dir) / PROPOSAL_FILENAME, proposal.as_dict())
    return proposal.content_hash()


def load_proposal(run_dir: Path) -> AcceptanceProposal | None:
    path = Path(run_dir) / PROPOSAL_FILENAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ManifestIntegrityError(f"proposal unreadable: {exc}") from exc
    return AcceptanceProposal.from_dict(data)


@dataclass
class BaselineCheck:
    """平台红/绿基线校验的读数:三类失败分开记录,不混装。"""

    red_ok: bool = False
    green_ok: bool = False
    red_exit_code: int | None = None
    green_exit_code: int | None = None
    red_failed_ids: list[str] = field(default_factory=list)
    unrelated_failures: list[str] = field(default_factory=list)
    collection_error: str = ""  # 身份歧义:收集不到提议的测试/收集期报错
    environment_failure: str = ""  # 超时、junit 缺失等环境事实
    checked_at: str = field(default_factory=_now_iso)

    @property
    def verified(self) -> bool:
        return self.red_ok and self.green_ok

    def as_dict(self) -> dict[str, Any]:
        return {
            "red_ok": self.red_ok,
            "green_ok": self.green_ok,
            "red_exit_code": self.red_exit_code,
            "green_exit_code": self.green_exit_code,
            "red_failed_ids": list(self.red_failed_ids),
            "unrelated_failures": list(self.unrelated_failures),
            "collection_error": self.collection_error,
            "environment_failure": self.environment_failure,
            "checked_at": self.checked_at,
            "verified": self.verified,
        }


def verify_baseline(
    *,
    python_exe: str,
    snapshot_dir: Path,
    failed_tests: list[str],
    regression_tests: list[str],
    work_dir: Path,
    timeout_seconds: int,
    env: object = None,
) -> BaselineCheck:
    """在**快照副本**上校验提议清单的红/绿基线(冻结正本零写入)。

    red:提议的失败集必须真红,且失败用例都落在提议集合内(无关失败单独点名);
    green:提议的回归集必须全绿。收集错误/环境故障如实记录为不可批准的原因,
    不冒充"已红"或"已绿"。
    """
    from app.adapters.pytest_adapter import RC_NO_TESTS_COLLECTED, run_pytest

    check = BaselineCheck()
    work_dir.mkdir(parents=True, exist_ok=True)
    copy_dir = work_dir / "baseline-check"
    if copy_dir.exists():
        shutil.rmtree(copy_dir)
    shutil.copytree(snapshot_dir, copy_dir, symlinks=False, ignore=shutil.ignore_patterns(".git"))

    red_report, red_run = run_pytest(
        python_exe,
        copy_dir,
        list(failed_tests),
        work_dir / "baseline-red.xml",
        timeout_seconds=timeout_seconds,
        env=env,  # type: ignore[arg-type]
    )
    green_report, green_run = run_pytest(
        python_exe,
        copy_dir,
        list(regression_tests),
        work_dir / "baseline-green.xml",
        timeout_seconds=timeout_seconds,
        env=env,  # type: ignore[arg-type]
    )
    check.red_exit_code = red_report.exit_code
    check.green_exit_code = green_report.exit_code

    if red_run.timed_out or green_run.timed_out:
        check.environment_failure = "baseline run timed out"
    check.red_failed_ids = [
        c.test_id for c in red_report.failed_cases if c.test_id
    ]
    check.unrelated_failures = sorted(
        set(check.red_failed_ids) - set(failed_tests)
    )
    if red_report.exit_code in (RC_NO_TESTS_COLLECTED, RC_USAGE_ERROR) or red_report.errors:
        check.collection_error = (
            f"failed set not collectable as proposed (exit={red_report.exit_code},"
            f" errors={red_report.errors})"
        )
    check.red_ok = (
        not red_report.all_passed
        and red_report.exit_code not in (RC_NO_TESTS_COLLECTED, RC_USAGE_ERROR)
        and not red_report.errors
        and not check.unrelated_failures
        and not red_run.timed_out
    )
    check.green_ok = (
        green_report.all_passed
        and green_report.exit_code == 0
        and not green_report.errors
        and not green_run.timed_out
    )
    return check


@dataclass
class AcceptanceManifest:
    """冻结后的验收清单:生产补丁的唯一放行凭据。"""

    manifest_hash: str
    task_id: str
    proposal_id: str
    failed_tests: list[str]
    regression_tests: list[str]
    source_snapshot_hash: str
    task_spec_hash: str
    approved_by: str
    approved_at: str
    baseline: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "manifest_hash": self.manifest_hash,
            "task_id": self.task_id,
            "proposal_id": self.proposal_id,
            "failed_tests": list(self.failed_tests),
            "regression_tests": list(self.regression_tests),
            "source_snapshot_hash": self.source_snapshot_hash,
            "task_spec_hash": self.task_spec_hash,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "baseline": dict(self.baseline),
        }


def manifest_hash_for(proposal: AcceptanceProposal) -> str:
    return proposal.content_hash()


def load_manifest(run_dir: Path) -> AcceptanceManifest | None:
    """读回冻结 manifest;内容与登记 hash 不符 = ManifestIntegrityError(磁盘不可信)。"""
    path = Path(run_dir) / MANIFEST_FILENAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ManifestIntegrityError(f"manifest unreadable: {exc}") from exc
    if int(data.get("schema_version", 0)) != MANIFEST_SCHEMA_VERSION:
        raise ManifestIntegrityError(
            f"unsupported manifest schema_version: {data.get('schema_version')}"
        )
    manifest = AcceptanceManifest(
        manifest_hash=str(data["manifest_hash"]),
        task_id=str(data["task_id"]),
        proposal_id=str(data["proposal_id"]),
        failed_tests=[str(x) for x in data["failed_tests"]],
        regression_tests=[str(x) for x in data["regression_tests"]],
        source_snapshot_hash=str(data["source_snapshot_hash"]),
        task_spec_hash=str(data["task_spec_hash"]),
        approved_by=str(data["approved_by"]),
        approved_at=str(data["approved_at"]),
        baseline=dict(data.get("baseline") or {}),
    )
    recomputed = _stable_hash(
        {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "task_id": manifest.task_id,
            "proposal_id": manifest.proposal_id,
            "failed_tests": manifest.failed_tests,
            "regression_tests": manifest.regression_tests,
            "source_snapshot_hash": manifest.source_snapshot_hash,
            "task_spec_hash": manifest.task_spec_hash,
            "approved_by": manifest.approved_by,
        }
    )
    if recomputed != manifest.manifest_hash:
        raise ManifestIntegrityError(
            f"manifest integrity check failed (recorded {manifest.manifest_hash[:12]},"
            f" computed {recomputed[:12]}); the frozen acceptance list is not trustworthy"
        )
    return manifest


def freeze_manifest(
    run_dir: Path,
    *,
    expected_hash: str,
    expected_revision: int,
    approved_by: str,
) -> tuple[AcceptanceManifest, bool]:
    """批准入参:proposal_id+hash+expected_revision 齐备才冻结;重复相同批准幂等。

    返回 (manifest, already_frozen)。冲突语义:
    - 已有 manifest 且 hash 相同 → 幂等成功(already_frozen=True);
    - 已有 manifest 且 hash 不同 → ConflictError(冻结清单不可原地变更,需新任务);
    - hash 与提议内容不符 / revision 不符 / 基线未通过 → 拒绝,不落任何文件。
    """
    run_dir = Path(run_dir)
    existing = load_manifest(run_dir)
    proposal = load_proposal(run_dir)
    if proposal is None:
        raise InvalidRequestError("no acceptance proposal on file for this task")
    if existing is not None:
        if existing.manifest_hash == expected_hash:
            return existing, True
        raise ConflictError(
            "acceptance manifest already frozen with a different hash"
            f" ({existing.manifest_hash[:12]}); create a new task instead of mutating it"
        )
    computed = proposal.content_hash()
    if expected_hash != computed:
        raise ConflictError(
            f"proposal hash mismatch: submitted {str(expected_hash)[:12]},"
            f" on-file proposal hashes to {computed[:12]}"
        )
    if expected_revision != proposal.revision:
        raise ConflictError(
            f"revision conflict: expected {proposal.revision}, submitted {expected_revision}"
        )
    if not proposal.baseline.get("verified"):
        raise InvalidRequestError(
            "baseline red/green verification has not passed for this proposal;"
            " refusing to freeze an unverified acceptance list"
        )
    manifest = AcceptanceManifest(
        manifest_hash=computed,
        task_id=proposal.task_id,
        proposal_id=proposal.proposal_id,
        failed_tests=list(proposal.failed_tests),
        regression_tests=list(proposal.regression_tests),
        source_snapshot_hash=proposal.source_snapshot_hash,
        task_spec_hash=proposal.task_spec_hash,
        approved_by=approved_by,
        approved_at=_now_iso(),
        baseline=dict(proposal.baseline),
    )
    _write_atomic(run_dir / MANIFEST_FILENAME, manifest.as_dict())
    log.info(
        "task %s: acceptance manifest frozen (%s, %d failed / %d regression)",
        proposal.task_id,
        computed[:12],
        len(manifest.failed_tests),
        len(manifest.regression_tests),
    )
    return manifest, False
