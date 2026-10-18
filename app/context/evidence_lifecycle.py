"""File identities for platform observations and mutation-triggered invalidation."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from app.context.evidence import KIND_TOOL_PAYLOAD, EvidenceNotStored, EvidenceRef, build_store
from app.tools.paths import relpath_within

if TYPE_CHECKING:
    from app.tools.base import ToolContext, ToolResult


def file_hash(ctx: ToolContext, path: str) -> str:
    """Hash exact bytes; deletion is an explicit identity, never an empty digest."""
    target = relpath_within(ctx.workspace, path)
    if target is None:
        return "unavailable"
    try:
        return hashlib.sha256(target.read_bytes()).hexdigest()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"


def receipt_hashes(ctx: ToolContext, name: str, output: Any) -> dict[str, str]:
    if not isinstance(output, dict):
        return {}
    paths: set[str] = set()
    if name in {"read_file", "describe_file"} and output.get("path"):
        paths.add(str(output["path"]))
    if name in {"find_symbol", "search_code"}:
        paths.update(str(m["path"]) for m in output.get("matches", []) if m.get("path"))
    if name == "read_evidence" and ctx.memory is not None:
        return {
            path: digest
            for item in ctx.memory.evidence
            if item.evidence_id == output.get("evidence_id")
            for path, digest in item.file_hashes.items()
        }
    return {path: file_hash(ctx, path) for path in sorted(paths)}


def invalidate_evidence(ctx: ToolContext, *, trigger: str, round_no: int = 0) -> list[str]:
    memory = ctx.memory
    if memory is None:
        return []
    paths = {path for item in memory.evidence for path in item.file_hashes}
    hashes = {path: file_hash(ctx, path) for path in sorted(paths)}
    refs = memory.invalidate_files(hashes, revision=ctx.workspace_revision)
    ctx.tracker.record(
        tool="evidence_invalidated",
        round_no=round_no,
        input_payload={"trigger": trigger, "workspace_revision": ctx.workspace_revision},
        output_summary={"refs": refs, "current_file_hashes": hashes},
    )
    return refs


def retain_receipt(ctx: ToolContext, name: str, result: ToolResult) -> None:
    """Every recorded observation is retrievable, including small results and errors."""
    if result.evidence is not None:
        return
    from app.config import get_settings

    if not get_settings().evidence_enabled:
        return
    if ctx.evidence is None:
        ctx.evidence = build_store(ctx.report_dir, ctx.task_id)
    payload = result.output if result.ok else {"error": result.error}
    raw = json.dumps(payload, ensure_ascii=False, default=str)
    try:
        result.evidence = ctx.evidence.save(
            raw, source=name, workspace_revision=ctx.workspace_revision
        )
    except EvidenceNotStored as exc:
        result.evidence = exc.ref
    except OSError as exc:
        data = raw.encode("utf-8", errors="replace")
        result.evidence = EvidenceRef(
            evidence_id="",
            task_id=ctx.task_id,
            source=name,
            kind=KIND_TOOL_PAYLOAD,
            workspace_revision=ctx.workspace_revision,
            sha256=hashlib.sha256(data).hexdigest(),
            raw_bytes=len(data),
            raw_lines=len(data.splitlines()),
            retrievable=False,
            reason=f"evidence persistence failed: {type(exc).__name__}",
        )
