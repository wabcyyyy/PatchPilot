"""S10a 实验对照比较器:登记差异之外的一切不同都直接报错。

公平对照的前提是"除登记变量外全部相同"。本脚本比较两个批次目录的逐题 provenance:
engine/model/上下文参数/环境必须一致,arm(及其派生策略描述)是允许的差异;
发现未登记差异即非零退出并列出字段——绝不静默放行。

G04 修的两件事(审查 R5 / SPEC §7 G04):

1. **结果不是控制变量**。此前 `verdict/status/tokens_used/rounds/duration_ms` 也逐字段
   参与比较,"两臂结果相同"被当成"配置相同"的证据,而真实对照(结果本来就该不同)
   反而直接报错。现在控制身份与观测结果分开:结果差异永远合法(它就是被测量的量),
   配置漂移一律拒绝(验收矩阵 T07 接受、T08 拒绝)。
2. **每次重复都保留**。旧路径先过 `latest_per_bug`,同一题跑三次只留最新一次,
   重复之间的方差被抹平成"一次成功"。现在按 `(task_identity, arm, repetition)` 配对,
   缺对、重复 task_id、次数与 manifest 不符都单独诊断,不静默剔除、不补跑。

`--manifest` 传入时,允许变量取自 manifest(而不是硬编码集合),结论随 manifest hash
一起落盘,事后能对账"当时比的是哪两份产物"。摘要只描述观测分布,不含机制归因。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.evals.experiment_manifest import ExperimentManifest, read_manifest  # noqa: E402
from app.evals.metrics import RunRow, collect_runs, pair_runs  # noqa: E402

# 无 manifest 时的兼容口径:arm 是登记变量,experiment_policy 由 arm 派生
ALLOWED_DIFFERENCES: frozenset[str] = frozenset({"arm", "experiment_policy"})

# 观测结果:两臂/两次重复**应当**可以不同,它们是被测量的量。
# 拿它们相同去证明"配置相同"是反向推理,这里从结构上禁止这种用法。
OBSERVED_FIELDS: frozenset[str] = frozenset(
    {
        "verdict",
        "status",
        "outcome",
        "rounds",
        "turns",
        "tokens_used",
        "tokens_prompt",
        "tokens_completion",
        "duration_ms",
        "cost_usd",
        "gate_violations",
        "changed_files",
        "error",
        "verify_failed_ok",
        "verify_regression_ok",
        "validation_status",
        "gate_status",
        "resource_status",
        "resources",
        "candidate_id",
        "candidate_hash",
        "verification_attempt_id",
        "baseline_failed",
        "baseline_regression_ok",
        "task_id",
        "delivery_status",
        "review_status",
        "holdout_tests_passed",
        "adversarial_detected",
    }
)

# 控制身份:必须逐字段相同的 provenance 小节。缺节读成 "<absent>",不当"相同"——
# G04 之前生成的批次没有这些节,与带节批次互比时应先被 provenance_schema_version 拦下。
CONTROL_SECTIONS: tuple[str, ...] = (
    "git_commit",
    "dirty_fingerprint",
    "worktree_dirty",
    "engine",
    "model_provider",
    "model_name",
    "max_turns",
    "execution_backend",
    "provenance_schema_version",
    "model_identity",
    "endpoint_identity",
    "pricing_identity",
    "container_identity",
    "budget_identity",
    "config_snapshot",
)

# protocol_identity 里由臂派生的键:只有这几项允许不同
PROTOCOL_ARM_KEYS: frozenset[str] = frozenset(
    {"arm", "experiment_policy", "experiment_policy_sha256"}
)

DISCLAIMER = (
    "本摘要只记录同一控制身份下的观测结果分布,不含机制归因或修复率结论;"
    "要把差异归因到某个改动,必须把该改动登记为变量并另建配对批(SPEC §7 G04/G17)。"
)


def _env_to_settings_key(env_key: str) -> str:
    """manifest 的登记变量写环境变量名;比较时落到 Settings 键名(config_snapshot 的键)。"""
    if env_key.startswith("PATCHPILOT_"):
        return env_key[len("PATCHPILOT_") :].lower()
    return env_key


def _arm_of(row: RunRow) -> str:
    return str(row.provenance.get("arm") or "agent")


def _control_view(row: RunRow) -> dict[str, Any]:
    """一次运行的控制身份视图:provenance 的指定小节 + 任务/源码/测试清单身份。"""
    prov = row.provenance or {}
    view: dict[str, Any] = {key: prov.get(key, "<absent>") for key in CONTROL_SECTIONS}
    protocol = dict(prov.get("protocol_identity") or {})
    view["protocol_identity"] = {
        key: value for key, value in protocol.items() if key not in PROTOCOL_ARM_KEYS
    }
    view["task_spec_hash"] = row.task_spec_hash or "<absent>"
    view["source_snapshot_hash"] = row.source_snapshot_hash or "<absent>"
    view["test_set_hash"] = row.test_set_hash or "<absent>"
    return view


def _observed_view(row: RunRow) -> dict[str, Any]:
    data = json.loads((row.run_dir / "report.json").read_text(encoding="utf-8"))
    return {key: data.get(key) for key in sorted(OBSERVED_FIELDS) if key in data}


def _diff_control(
    key: str, left: dict[str, Any], right: dict[str, Any], allowed_keys: set[str]
) -> list[str]:
    """逐字段比对控制身份;config_snapshot 里只允许 manifest 登记的变量不同。"""
    problems: list[str] = []
    for field in sorted(set(left) | set(right)):
        a, b = left.get(field), right.get(field)
        if a == b or field in allowed_keys:
            continue
        if field == "config_snapshot" and isinstance(a, dict) and isinstance(b, dict):
            drifting = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
            unregistered = [k for k in drifting if f"config_snapshot.{k}" not in allowed_keys]
            if unregistered:
                problems.append(
                    f"{key}: 未登记的配置漂移:{unregistered}"
                    f"(例如 {unregistered[0]}: {a.get(unregistered[0])!r} != {b.get(unregistered[0])!r})"
                )
            continue
        problems.append(f"{key}: {field} differs: {a!r} != {b!r}")
    return problems


def compare_batches(
    batch_a: Path,
    batch_b: Path,
    *,
    manifest: ExperimentManifest | None = None,
    arms: tuple[str, str] = ("agent", "one_shot"),
) -> dict[str, Any]:
    """比较两个批次;返回配对摘要(保留每一次运行)。发现未登记差异抛 SystemExit。"""
    rows_a = collect_runs(batch_a)
    rows_b = collect_runs(batch_b)
    if not rows_a or not rows_b:
        raise SystemExit(
            f"empty batch: a={len(rows_a)} runs, b={len(rows_b)} runs —— 没有产物的批不参与比较"
        )
    expected_reps = manifest.repetitions if manifest is not None else None
    paired_a = pair_runs(rows_a, arms=(arms[0],), expected_repetitions=expected_reps)
    paired_b = pair_runs(rows_b, arms=(arms[1],), expected_repetitions=expected_reps)
    rows_by_task: dict[str, dict[str, RunRow]] = {
        arms[0]: {row.task_id: row for row in rows_a if _arm_of(row) == arms[0]},
        arms[1]: {row.task_id: row for row in rows_b if _arm_of(row) == arms[1]},
    }

    if manifest is None:
        arms_a = {_arm_of(row) for row in rows_a}
        arms_b = {_arm_of(row) for row in rows_b}
        if arms_a != {arms[0]} or arms_b != {arms[1]}:
            raise SystemExit(
                f"batch_a must contain only {arms[0]} runs {sorted(arms_a)},"
                f" batch_b only {arms[1]} runs {sorted(arms_b)}"
            )
    else:
        for name in arms:
            if name not in manifest.arm_names():
                raise SystemExit(
                    f"arm {name!r} 不在 manifest {manifest.manifest_id} 登记的臂里:"
                    f"{manifest.arm_names()}"
                )

    cells_a, cells_b = paired_a["cells"], paired_b["cells"]
    bugs_a = {key.split("@", 1)[0] for key in cells_a}
    bugs_b = {key.split("@", 1)[0] for key in cells_b}
    missing = sorted(bugs_a ^ bugs_b)
    if missing:
        raise SystemExit(
            f"unpaired bugs between batches: {missing}"
            " — 两臂必须预先固定同一题单,缺失/多出的题不进入对照"
        )

    diagnostics = [f"duplicate run id: {d}" for d in paired_a["duplicate_run_ids"]]
    diagnostics += [f"duplicate run id: {d}" for d in paired_b["duplicate_run_ids"]]
    for label, paired in ((arms[0], paired_a), (arms[1], paired_b)):
        diagnostics += [
            f"repetition mismatch in {label}: {note}" for note in paired["repetition_mismatch"]
        ]
    if diagnostics:
        raise SystemExit("pairing diagnostics:\n  " + "\n  ".join(diagnostics))

    if manifest is not None:
        allowed_by_arm = {
            arm.name: {
                f"config_snapshot.{_env_to_settings_key(name)}"
                if name.startswith("PATCHPILOT_")
                else name
                for name in arm.variables
            }
            for arm in manifest.arms
        }
    else:
        allowed_by_arm = {
            arms[0]: set(ALLOWED_DIFFERENCES),
            arms[1]: set(ALLOWED_DIFFERENCES),
        }

    unregistered: list[str] = []
    pairs: list[dict[str, Any]] = []
    for bug_id in sorted(bugs_a):
        cell_a = cells_a[f"{bug_id}@{arms[0]}"]
        cell_b = cells_b[f"{bug_id}@{arms[1]}"]
        views: dict[str, list[dict[str, Any]]] = {}
        for arm_name, cell in ((arms[0], cell_a), (arms[1], cell_b)):
            views[arm_name] = [
                _control_view(rows_by_task[arm_name][entry["task_id"]]) for entry in cell
            ]
            # 同一臂内的重复也必须同一控制身份,否则"重复"其实是另一批实验
            for other in views[arm_name][1:]:
                unregistered += _diff_control(
                    f"{bug_id}@{arm_name}(repetition drift)", views[arm_name][0], other, set()
                )
        allowed = allowed_by_arm[arms[0]] | allowed_by_arm[arms[1]]
        unregistered += _diff_control(bug_id, views[arms[0]][0], views[arms[1]][0], allowed)
        pairs.append(
            {
                "bug_id": bug_id,
                "repetitions": {arms[0]: len(cell_a), arms[1]: len(cell_b)},
                "arms": {
                    arms[0]: [
                        _observed_view(rows_by_task[arms[0]][entry["task_id"]]) for entry in cell_a
                    ],
                    arms[1]: [
                        _observed_view(rows_by_task[arms[1]][entry["task_id"]]) for entry in cell_b
                    ],
                },
                "verdicts": {
                    arms[0]: [entry["verdict"] for entry in cell_a],
                    arms[1]: [entry["verdict"] for entry in cell_b],
                },
                "tokens": {
                    arms[0]: [entry["tokens_used"] for entry in cell_a],
                    arms[1]: [entry["tokens_used"] for entry in cell_b],
                },
            }
        )

    if unregistered:
        raise SystemExit(
            "unregistered control differences between arms/repetitions:\n  "
            + "\n  ".join(unregistered)
        )
    return {
        "pairing": "task_identity+arm+repetition",
        "arms": list(arms),
        "manifest_hash": manifest.manifest_hash if manifest is not None else None,
        "registered_upgrades": list(manifest.registered_upgrades) if manifest else [],
        "pairs": pairs,
        "bugs_paired": len(pairs),
        "runs_kept": sum(len(v) for v in cells_a.values()) + sum(len(v) for v in cells_b.values()),
        "batch_a": str(batch_a),
        "batch_b": str(batch_b),
        "disclaimer": DISCLAIMER,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="同引擎两臂对照比较(未登记差异即报错)")
    parser.add_argument("batch_a", help="臂一(--arm-a)的批次目录")
    parser.add_argument("batch_b", help="臂二(--arm-b)的批次目录")
    parser.add_argument("--out", default="", help="可选:配对摘要 JSON 输出路径")
    parser.add_argument(
        "--manifest",
        default="",
        help="实验 manifest:登记变量与额度以它为准;缺省退回 S10a 的 agent/one_shot 硬编码口径",
    )
    parser.add_argument("--arm-a", default="agent")
    parser.add_argument("--arm-b", default="one_shot")
    args = parser.parse_args(argv)

    manifest = read_manifest(Path(args.manifest)) if args.manifest else None
    summary = compare_batches(
        Path(args.batch_a),
        Path(args.batch_b),
        manifest=manifest,
        arms=(args.arm_a, args.arm_b),
    )
    text = json.dumps(summary, ensure_ascii=False, indent=2, default=str)
    print(text)
    print(f"\npaired bugs: {summary['bugs_paired']} | runs kept: {summary['runs_kept']}")
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
