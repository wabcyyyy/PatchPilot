"""候选工件:补丁的身份冻结与恢复再验证(S03/F2,ADR-0009 §4)。

缺陷现场(review F2,P0):恢复把 propose/apply/verify/rollback/finish 之后的工作区
一律复位到基线,再直接执行检查点的 `next` 节点。next=finish 时补丁已被复位清掉,
finish 仍按检查点里的旧 verify 布尔值判 resolved——磁盘 diff 为空、报告宣称成功。

修法:在 PROPOSE→APPLY 的交接点(apply 节点入口,含分支合流路径)把**实际工作区
diff** 冻结为 `candidates/<candidate_id>/diff.patch + manifest.json`(tmp+replace
原子写,半写文件读侧不信任);state 只保存引用与哈希。恢复到 apply/verify/finish 时:
验证候选与身份 → reset → 重应用候选 → 清旧 verify/gate 结论 → 从 APPLY 完整重验。
恢复只恢复输入、候选和进度,**不恢复成功结论**。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.errors import TaskError
from app.gitops.differ import working_tree_diff
from app.gitops.patcher import apply_patch
from app.gitops.rollback import reset_workspace

log = logging.getLogger(__name__)

MANIFEST_VERSION = 1


@dataclass
class CandidateManifest:
    """一个候选补丁的身份清单;校验失败即不可信。"""

    version: int
    candidate_id: str
    diff_sha256: str
    task_spec_hash: str
    source_snapshot_hash: str
    baseline_commit: str
    round_no: int
    parent_candidate_id: str | None = None
    generated_call_ids: list[str] = field(default_factory=list)
    created_at: str = ""

    @staticmethod
    def from_dict(data: dict[str, Any]) -> CandidateManifest:
        required = (
            "candidate_id",
            "diff_sha256",
            "task_spec_hash",
            "source_snapshot_hash",
            "baseline_commit",
        )
        missing = [key for key in required if not data.get(key)]
        if missing:
            raise TaskError(f"candidate manifest missing fields: {missing}")
        if int(data.get("version", 0)) != MANIFEST_VERSION:
            raise TaskError(f"unsupported candidate manifest version: {data.get('version')}")
        return CandidateManifest(
            version=MANIFEST_VERSION,
            candidate_id=str(data["candidate_id"]),
            diff_sha256=str(data["diff_sha256"]),
            task_spec_hash=str(data["task_spec_hash"]),
            source_snapshot_hash=str(data["source_snapshot_hash"]),
            baseline_commit=str(data["baseline_commit"]),
            round_no=int(data.get("round_no", 0)),
            parent_candidate_id=data.get("parent_candidate_id"),
            generated_call_ids=list(data.get("generated_call_ids", [])),
            created_at=str(data.get("created_at", "")),
        )


def candidates_root(run_dir: Path | str) -> Path:
    return Path(run_dir) / "candidates"


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def freeze_candidate(
    run_dir: Path | str,
    workspace: Path | str,
    *,
    task_spec_hash: str,
    source_snapshot_hash: str,
    baseline_commit: str,
    round_no: int,
    parent_candidate_id: str | None = None,
    generated_call_ids: list[str] | None = None,
) -> CandidateManifest:
    """把当前工作区 diff 冻结为候选工件;返回清单。

    candidate_id 由 diff 内容派生(同 diff 同 id:重复冻结幂等);清单与补丁
    都走 tmp+replace 原子写,崩溃只能留下"完整旧候选"或"完整新候选"。
    """
    diff_text = working_tree_diff(workspace).diff_text
    if not diff_text.strip():
        raise TaskError("refusing to freeze an empty candidate (no workspace diff)")
    diff_sha = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()
    candidate_id = f"r{round_no}-{diff_sha[:12]}"
    cdir = candidates_root(run_dir) / candidate_id
    cdir.mkdir(parents=True, exist_ok=True)
    _atomic_write(cdir / "diff.patch", diff_text)
    manifest = CandidateManifest(
        version=MANIFEST_VERSION,
        candidate_id=candidate_id,
        diff_sha256=diff_sha,
        task_spec_hash=task_spec_hash,
        source_snapshot_hash=source_snapshot_hash,
        baseline_commit=baseline_commit,
        round_no=round_no,
        parent_candidate_id=parent_candidate_id,
        generated_call_ids=list(generated_call_ids or []),
        created_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
    )
    _atomic_write(cdir / "manifest.json", json.dumps(asdict(manifest), ensure_ascii=False))
    return manifest


def load_candidate(run_dir: Path | str, candidate_id: str) -> tuple[CandidateManifest, str] | None:
    """读候选并校验:清单可解析、补丁字节的 sha256 与清单一致。

    半写/篡改/版本不符一律返回 None——恢复方按"无候选"处理,绝不猜。
    返回 (清单, 补丁全文)。
    """
    cdir = candidates_root(run_dir) / candidate_id
    patch_path = cdir / "diff.patch"
    manifest_path = cdir / "manifest.json"
    if not (patch_path.is_file() and manifest_path.is_file()):
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = CandidateManifest.from_dict(data)
        patch_text = patch_path.read_text(encoding="utf-8")
    except (OSError, ValueError, TaskError) as exc:
        log.warning("candidate %s unreadable: %s", candidate_id, exc)
        return None
    if not str(manifest.candidate_id).startswith(f"r{manifest.round_no}-"):
        return None
    actual_sha = hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
    if actual_sha != manifest.diff_sha256:
        log.warning("candidate %s tampered: sha mismatch", candidate_id)
        return None
    return manifest, patch_text


CHECKPOINT_VERSION = 1


@dataclass
class CandidateCheckpoint:
    """一个候选补丁的验证证据记录(G14)。

    每次候选的 patch/hash 已有 manifest 持久化;这里补**判定事实**:
    gate 是否通过、平台 verify 双测试集的布尔、以及证据所属的验证尝试 id。
    `verification_attempt_id` 是"这份证据是哪一次真实重跑产生的"——旧尝试的
    绿证据不得套到新代码上。
    """

    candidate_id: str
    version: int
    round_no: int
    gate_ok: bool
    verify_failed_ok: bool
    verify_regression_ok: bool
    verification_attempt_id: str
    note: str
    updated_at: str

    @staticmethod
    def from_dict(data: dict[str, Any]) -> CandidateCheckpoint:
        candidate_id = str(data.get("candidate_id", "") or "")
        if not candidate_id:
            raise TaskError("candidate checkpoint missing candidate_id")
        if int(data.get("version", 0)) != CHECKPOINT_VERSION:
            raise TaskError(f"unsupported checkpoint version: {data.get('version')}")
        return CandidateCheckpoint(
            candidate_id=candidate_id,
            version=CHECKPOINT_VERSION,
            round_no=int(data.get("round_no", 0) or 0),
            gate_ok=bool(data.get("gate_ok", False)),
            verify_failed_ok=bool(data.get("verify_failed_ok", False)),
            verify_regression_ok=bool(data.get("verify_regression_ok", False)),
            verification_attempt_id=str(data.get("verification_attempt_id", "") or ""),
            note=str(data.get("note", "") or ""),
            updated_at=str(data.get("updated_at", "") or ""),
        )


def write_candidate_checkpoint(
    run_dir: Path | str,
    candidate_id: str,
    *,
    gate_ok: bool,
    verify_failed_ok: bool,
    verify_regression_ok: bool,
    round_no: int,
    verification_attempt_id: str = "",
    note: str = "",
) -> bool:
    """把候选的验证证据写进 `candidates/<id>/checkpoint.json`(G14)。

    候选目录缺失时静默跳过并返回 False(候选已不是当前态,不据此补证据);
    写入走 tmp+replace 原子写,崩溃只留下完整旧证据或完整新证据。
    """
    cdir = candidates_root(run_dir) / candidate_id
    if not cdir.is_dir():
        return False
    checkpoint = {
        "candidate_id": candidate_id,
        "version": CHECKPOINT_VERSION,
        "round_no": round_no,
        "gate_ok": bool(gate_ok),
        "verify_failed_ok": bool(verify_failed_ok),
        "verify_regression_ok": bool(verify_regression_ok),
        "verification_attempt_id": verification_attempt_id,
        "note": note,
        "updated_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
    }
    _atomic_write(cdir / "checkpoint.json", json.dumps(checkpoint, ensure_ascii=False))
    return True


def read_candidate_checkpoint(run_dir: Path | str, candidate_id: str) -> CandidateCheckpoint | None:
    """读候选验证证据;缺失/损坏/版本不符一律返回 None,调用方按"无证据"处理。"""
    path = candidates_root(run_dir) / candidate_id / "checkpoint.json"
    if not path.is_file():
        return None
    try:
        return CandidateCheckpoint.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TaskError) as exc:
        log.warning("candidate %s checkpoint unreadable: %s", candidate_id, exc)
        return None


def is_candidate_gate_clean(run_dir: Path | str, candidate_id: str) -> bool:
    """候选是否已通过图级质量门禁(G14 v2 续改的准入条件)。

    只有 gate_ok=True 的候选才允许"从具名候选继续修复";门禁拒绝的候选
    checkpoint 记 gate_ok=False,续改不放行、必须整体复位到基线。
    """
    checkpoint = read_candidate_checkpoint(run_dir, candidate_id)
    return checkpoint is not None and bool(checkpoint.gate_ok)


def reapply_candidate(
    workspace: Path | str,
    run_dir: Path | str,
    candidate_id: str,
) -> str:
    """恢复再验证的机械动作:reset 到基线 → 重应用候选补丁。

    复用既有合法通路(reset_workspace + patcher.apply_patch 的落点校验/
    `git apply --check`),候选来自平台目录**不跳过任何门禁**——门禁在 apply
    节点照常对重应用后的工作区 diff 全量执行。返回补丁全文;应用失败抛 TaskError。
    """
    loaded = load_candidate(run_dir, candidate_id)
    if loaded is None:
        raise TaskError(f"candidate {candidate_id} missing or tampered; refusing to reapply")
    manifest, patch_text = loaded
    reset_workspace(workspace, manifest.baseline_commit)
    applied = apply_patch(workspace, patch_text)
    if not applied.applied:
        raise TaskError(f"candidate reapply failed ({applied.rejected_reason}): {applied.detail}")
    return patch_text


def new_verification_attempt_id() -> str:
    return uuid.uuid4().hex


def accepted_contract_hashes(run_dir: Path | str) -> tuple[str, str]:
    """读受理契约的 (task_spec_hash, source_snapshot_hash);缺失/损坏返回 ("", "")。

    契约缺失的候选(旧任务)在恢复校验时按"缺身份证据"拒绝,不放行。
    """
    path = Path(run_dir) / "task_spec.json"
    if not path.is_file():
        return "", ""
    try:
        from app.task_spec import TaskSpec

        spec = TaskSpec.read_file(path)
    except (ValueError, OSError):
        return "", ""
    return spec.task_spec_hash, spec.source_snapshot_hash


def asdict(manifest: CandidateManifest) -> dict[str, Any]:
    return {
        "version": manifest.version,
        "candidate_id": manifest.candidate_id,
        "diff_sha256": manifest.diff_sha256,
        "task_spec_hash": manifest.task_spec_hash,
        "source_snapshot_hash": manifest.source_snapshot_hash,
        "baseline_commit": manifest.baseline_commit,
        "round_no": manifest.round_no,
        "parent_candidate_id": manifest.parent_candidate_id,
        "generated_call_ids": manifest.generated_call_ids,
        "created_at": manifest.created_at,
    }
