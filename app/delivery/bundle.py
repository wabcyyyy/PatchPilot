"""补丁交付审核包与清单签署 (P16)。

规则：
- 导出绑定 source commit、TaskSpec、candidate hash、测试工件、费用账本的审核包；
- 独立计算与校验 manifest hash，防范后台补丁篡改或替换；
- 状态正交解耦：严禁复用 FINISHED 或 RESOLVED 表示已发布，
  必须同时满足 resolved ∧ review_status=approved ∧ independent_ci_status=passed。
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.errors import ConflictError

log = logging.getLogger(__name__)

DELIVERY_MANIFEST_FILENAME = "delivery_manifest.json"


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _canonical_hash(data: dict[str, Any]) -> str:
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class DeliveryManifest:
    task_id: str
    source_commit: str
    candidate_hash: str
    diff_sha256: str
    task_spec_hash: str
    test_summary: dict[str, Any]
    cost_ledger: dict[str, Any]
    created_at: str
    manifest_hash: str = ""

    def compute_hash(self) -> str:
        d = asdict(self)
        d.pop("manifest_hash", None)
        return _canonical_hash(d)


def create_delivery_bundle(
    task: dict[str, Any],
    run_dir: Path | str,
    out_dir: Path | str | None = None,
) -> tuple[DeliveryManifest, Path]:
    """生成并导出自包含的不可变补丁交付审核包 (P16)。"""
    rdir = Path(run_dir)
    diff_path = rdir / "diff.patch"
    spec_path = rdir / "task_spec.json"
    rep_path = rdir / "report.json"

    if not diff_path.is_file():
        raise FileNotFoundError(f"diff.patch not found in {rdir}")
    if not rep_path.is_file():
        raise FileNotFoundError(f"report.json not found in {rdir}")

    report = json.loads(rep_path.read_text(encoding="utf-8"))
    candidate_hash = str(report.get("candidate_hash") or task.get("idem_key") or "")
    source_commit = str(report.get("source_commit") or "HEAD")

    diff_sha = _file_sha256(diff_path)
    spec_sha = _file_sha256(spec_path) if spec_path.is_file() else ""

    test_summary = {
        "verdict": report.get("verdict"),
        "failed_tests_passed": report.get("failed_tests_passed", True),
        "regression_tests_passed": report.get("regression_tests_passed", True),
    }

    cost_ledger = {
        "total_tokens": report.get("total_tokens", 0),
        "total_cost_usd": report.get("total_cost_usd", 0.0),
        "model_calls": report.get("model_calls", 0),
    }

    tid = str(task.get("task_id") or task.get("id"))
    manifest = DeliveryManifest(
        task_id=tid,
        source_commit=source_commit,
        candidate_hash=candidate_hash,
        diff_sha256=diff_sha,
        task_spec_hash=spec_sha,
        test_summary=test_summary,
        cost_ledger=cost_ledger,
        created_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
    )
    manifest.manifest_hash = manifest.compute_hash()

    target_root = Path(out_dir) if out_dir else (rdir / "delivery_bundle")
    target_root.mkdir(parents=True, exist_ok=True)

    # 导出文件
    shutil.copy2(diff_path, target_root / "diff.patch")
    shutil.copy2(rep_path, target_root / "report.json")
    if spec_path.is_file():
        shutil.copy2(spec_path, target_root / "task_spec.json")

    m_path = target_root / DELIVERY_MANIFEST_FILENAME
    m_path.write_text(json.dumps(asdict(manifest), indent=2, ensure_ascii=False), encoding="utf-8")

    return manifest, target_root


def verify_delivery_manifest(bundle_dir: Path | str) -> tuple[bool, list[str]]:
    """校验交付审核包的自洽性与内容哈希 (P16)。"""
    bdir = Path(bundle_dir)
    m_path = bdir / DELIVERY_MANIFEST_FILENAME
    if not m_path.is_file():
        return False, ["delivery_manifest.json missing"]

    try:
        raw_manifest = json.loads(m_path.read_text(encoding="utf-8"))
        manifest = DeliveryManifest(**raw_manifest)
    except Exception as exc:
        return False, [f"failed to parse manifest: {exc}"]

    errors: list[str] = []
    # 1. 校验自身签名哈希
    if manifest.manifest_hash != manifest.compute_hash():
        errors.append("manifest_hash signature mismatch (possible tampering)")

    # 2. 校验 diff.patch
    diff_path = bdir / "diff.patch"
    if not diff_path.is_file():
        errors.append("diff.patch missing from bundle")
    else:
        actual_diff_sha = _file_sha256(diff_path)
        if actual_diff_sha != manifest.diff_sha256:
            errors.append(
                f"diff_sha256 mismatch: expected {manifest.diff_sha256}, got {actual_diff_sha}"
            )

    # 3. 校验 task_spec.json
    spec_path = bdir / "task_spec.json"
    if spec_path.is_file() and manifest.task_spec_hash:
        actual_spec_sha = _file_sha256(spec_path)
        if actual_spec_sha != manifest.task_spec_hash:
            errors.append("task_spec_hash mismatch")

    return len(errors) == 0, errors


def evaluate_delivery_readiness(
    task: dict[str, Any],
    delivery: dict[str, Any] | None,
) -> tuple[bool, str]:
    """不可篡改判定：核对引擎 resolved ∧ 人工审核 approved ∧ 独立 CI passed。"""
    verdict = (task.get("verdict") or "").upper()
    if verdict != "RESOLVED":
        return False, f"task verdict '{verdict}' is not RESOLVED"

    if not delivery:
        return False, "delivery record not initialized"

    review_status = delivery.get("review_status")
    if review_status != "approved":
        return False, f"human review status '{review_status}' is not approved"

    ci_status = delivery.get("independent_ci_status")
    if ci_status != "passed":
        return False, f"independent CI status '{ci_status}' is not passed"

    return True, "ready_to_deliver"


def assert_can_deliver(
    task: dict[str, Any],
    delivery: dict[str, Any] | None,
) -> None:
    ready, reason = evaluate_delivery_readiness(task, delivery)
    if not ready:
        raise ConflictError(f"cannot deliver patch: {reason}")
