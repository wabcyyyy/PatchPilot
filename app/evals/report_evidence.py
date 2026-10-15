"""report.json v2 补全段(G16):从 run_dir 工件收集验收清单/候选/命令/输入事件/运行身份。

SPEC G16:"`report.json` v2 记录验收清单、候选、验证、资源来源、模型能力、命令、
子任务、输入事件和运行身份"。验证/资源/模型能力/子任务在执行侧已进 TaskResult;
这里补齐只存在于 run_dir 工件里的四类事实,并把运行身份链汇总成一段:

- acceptance:`acceptance_proposal.json` / `acceptance_manifest.json`(G10;repair
  模式两件都没有 ⇒ proposed/frozen=False,如实呈现"该模式无清单",不冒充已批准);
- candidates:`candidates/<id>/manifest.json` + `checkpoint.json`(G14;gate 拒绝的
  候选同样列出,gate_ok=False 如实呈现,不以"最终候选"掩盖落选者);
- commands:轨迹里 tool=run_command 的执行事件(G09 受控命令);
- inputs:`interaction_state.json` 的问题/回答/纠偏(G11;文本截断展示,原文在工件);
- run_identity:task_spec 身份 + 运行 profile 指纹 + served_model 观测汇总。

全部容错:工件缺失(旧任务/提前终止/取消)落空结构;收集失败只告警,报告照写
——证据补全是附加物,不得让收尾失败把已完成任务吞成 NEEDS_REVIEW。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PROPOSAL_FILENAME = "acceptance_proposal.json"
MANIFEST_FILENAME = "acceptance_manifest.json"
INTERACTION_FILENAME = "interaction_state.json"
SPEC_FILENAME = "task_spec.json"
TRAJECTORY_FILENAME = "trajectory.jsonl"
ADVERSARIAL_EVAL_FILENAME = "adversarial_eval.json"

# 轨迹级清单的截断上限:报告是摘要,明细永远在工件里
COMMAND_EVENT_CAP = 50
TEXT_EXCERPT_CHARS = 160


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _excerpt(text: Any) -> str:
    return str(text or "")[:TEXT_EXCERPT_CHARS]


def _acceptance(run_dir: Path) -> dict[str, Any]:
    proposal = _read_json(run_dir / PROPOSAL_FILENAME) or {}
    manifest = _read_json(run_dir / MANIFEST_FILENAME) or {}
    return {
        "proposed": bool(proposal),
        "frozen": bool(manifest),
        "proposal_id": str(proposal.get("proposal_id", "") or ""),
        "proposal_hash": str(proposal.get("content_hash", "") or ""),
        "manifest_hash": str(manifest.get("manifest_hash", "") or ""),
        "revision": manifest.get("revision", proposal.get("revision", 0)) or 0,
        "failed_tests": list(manifest.get("failed_tests") or proposal.get("failed_tests") or []),
        "regression_tests": list(
            manifest.get("regression_tests") or proposal.get("regression_tests") or []
        ),
        "approved_by": str(manifest.get("approved_by", "") or ""),
        "approved_at": str(manifest.get("approved_at", "") or ""),
    }


def _candidates(run_dir: Path) -> list[dict[str, Any]]:
    root = run_dir / "candidates"
    if not root.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for manifest_path in sorted(root.glob("*/manifest.json")):
        manifest = _read_json(manifest_path) or {}
        checkpoint = _read_json(manifest_path.parent / "checkpoint.json") or {}
        items.append(
            {
                "candidate_id": str(manifest.get("candidate_id", "") or ""),
                "diff_sha256": str(manifest.get("diff_sha256", "") or ""),
                "round_no": int(manifest.get("round_no", 0) or 0),
                "parent_candidate_id": manifest.get("parent_candidate_id"),
                "created_at": str(manifest.get("created_at", "") or ""),
                "gate_ok": checkpoint.get("gate_ok"),
                "verify_failed_ok": checkpoint.get("verify_failed_ok"),
                "verify_regression_ok": checkpoint.get("verify_regression_ok"),
                "verification_attempt_id": str(checkpoint.get("verification_attempt_id", "") or ""),
            }
        )
    items.sort(key=lambda item: (item["round_no"], item["candidate_id"]))
    return items


def _commands(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / TRAJECTORY_FILENAME
    if not path.is_file():
        return []
    items: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("tool") != "run_command":
            continue
        payload = event.get("input_payload") or {}
        items.append(
            {
                "round_no": event.get("round_no"),
                "state": event.get("state"),
                "profile_id": str(
                    payload.get("profile_id")
                    or (payload.get("call") or {}).get("profile_id", "")
                    or ""
                ),
                "duration_ms": event.get("duration_ms"),
                "error": str(event.get("error") or ""),
            }
        )
        if len(items) >= COMMAND_EVENT_CAP:
            break
    return items


def _inputs(run_dir: Path) -> dict[str, Any]:
    state = _read_json(run_dir / INTERACTION_FILENAME) or {}
    questions = []
    for q in state.get("questions") or []:
        if not isinstance(q, dict):
            continue
        questions.append(
            {
                "question_id": str(q.get("question_id", "") or ""),
                "status": str(q.get("status", "") or ""),
                "question": _excerpt(q.get("question")),
                "answer_source": str(q.get("answer_source", "") or ""),
                "answer": _excerpt(q.get("answer_text")),
            }
        )
    steers = [
        {"text": _excerpt(s.get("text")), "consumed": bool(s.get("consumed_at"))}
        for s in (state.get("steers") or [])
        if isinstance(s, dict)
    ]
    return {
        "revision": int(state.get("revision", 0) or 0),
        "questions": questions,
        "steers": steers,
    }


def _run_identity(run_dir: Path) -> dict[str, Any]:
    spec = _read_json(run_dir / SPEC_FILENAME) or {}
    policy = spec.get("effective_policy") or {}
    runtime_profile = policy.get("runtime_profile") or {}
    served: list[str] = []
    trajectory = run_dir / TRAJECTORY_FILENAME
    if trajectory.is_file():
        try:
            for line in trajectory.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict) or event.get("tool") != "llm":
                    continue
                model = str((event.get("output_summary") or {}).get("served_model", "") or "")
                if model and model not in served:
                    served.append(model)
        except OSError:
            served = []
    return {
        "task_spec_hash": str(spec.get("task_spec_hash", "") or ""),
        "platform_commit": str(spec.get("platform_commit", "") or ""),
        "prompts_hash": str(spec.get("prompts_hash", "") or ""),
        "agent_profile": str(policy.get("agent_runtime_profile", "") or ""),
        "profile_name": str(runtime_profile.get("profile_name", "") or ""),
        "runtime_policy_hash": _runtime_policy_hash(policy),
        "served_models": served,
    }


def _runtime_policy_hash(policy: dict[str, Any]) -> str:
    """冻结运行 profile 的指纹(与绑定校验同一种算法);读不出为空串。"""
    from app.agent.runtime import TaskRuntime

    try:
        runtime = TaskRuntime.from_frozen(policy)
    except Exception:  # 非法内容不拦报告:身份段留空比失败收尾好
        return ""
    return runtime.policy_hash if runtime is not None else ""


def _adversarial_eval(run_dir: Path) -> dict[str, Any]:
    data = _read_json(run_dir / ADVERSARIAL_EVAL_FILENAME) or {}
    return {
        "evaluated": bool(data),
        "verdict": str(data.get("verdict", "") or ""),
        "declared_tests_passed": data.get("declared_tests_passed"),
        "holdout_tests_passed": data.get("holdout_tests_passed"),
        "adversarial_audit": data.get("adversarial_audit") or {},
        "manual_review_status": str(data.get("manual_review_status", "") or ""),
        "proof_limits_acknowledged": bool(data.get("proof_limits_acknowledged", False)),
    }


def collect_run_evidence(run_dir: Path | str) -> dict[str, Any]:
    """汇总报告 v2 补全段;任何一类收集失败落空结构,不抛。"""
    run_dir = Path(run_dir)
    evidence: dict[str, Any] = {}
    for name, collector in (
        ("acceptance", _acceptance),
        ("candidates", _candidates),
        ("commands", _commands),
        ("inputs", _inputs),
        ("run_identity", _run_identity),
        ("adversarial_eval", _adversarial_eval),
    ):
        try:
            evidence[name] = collector(run_dir)
        except Exception as exc:  # 防御:单个收集器坏掉不吞掉整份报告
            log.warning("task evidence %s collection failed: %s", name, exc)
            evidence[name] = {} if name != "candidates" else []
    return evidence
