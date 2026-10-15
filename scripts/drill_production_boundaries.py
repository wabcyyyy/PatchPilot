"""生产边界与故障恢复演练工具 (P19).

用法:
  python scripts/drill_production_boundaries.py --all
  python scripts/drill_production_boundaries.py --drill admission
  python scripts/drill_production_boundaries.py --drill fencing
  python scripts/drill_production_boundaries.py --drill quota
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app.api.admission import (  # noqa: E402
    AdmissionController,
    AdmissionQueueFullError,
    EmergencyStopError,
)
from app.config import Settings  # noqa: E402
from app.errors import QuotaExceededError  # noqa: E402
from app.observability.health import check_readiness  # noqa: E402
from app.storage.repository import Repository  # noqa: E402


def drill_admission() -> bool:
    print("[drill:admission] 正在演练有界背压与紧急停发...")
    ctrl_stop = AdmissionController(concurrency_limit=1, queue_limit=1, emergency_stop=True)
    stop_intercepted = False
    try:
        with ctrl_stop.acquire_permit():
            pass
    except EmergencyStopError:
        stop_intercepted = True

    if not stop_intercepted:
        print("  [FAIL] 紧急停发未按预期拦截")
        return False

    ctrl = AdmissionController(concurrency_limit=1, queue_limit=0)
    queue_full_intercepted = False
    with ctrl.acquire_permit():
        try:
            with ctrl.acquire_permit():
                pass
        except AdmissionQueueFullError:
            queue_full_intercepted = True

    if not queue_full_intercepted:
        print("  [FAIL] 队列饱和背压拦截失败")
        return False

    print("  [PASS] 准入背压与紧急停发演练成功。")
    return True


def drill_fencing() -> bool:
    print("[drill:fencing] 正在演练 Fencing Token 租约防篡改...")
    with tempfile.TemporaryDirectory() as td:
        db_file = Path(td) / "fence.sqlite3"
        with Repository(db_file) as repo:
            repo.create_task(
                task_id="DRILL-T1",
                idem_key="idem-drill-t1",
                bug_id="BUG-001",
                repo_path=str(Path(td) / "repo"),
                issue_text="drill fence",
                max_rounds=5,
                engine="graph",
                model_provider="fake",
                run_dir=str(Path(td) / "runs" / "DRILL-T1"),
                owner="p1",
                principal_id="p1",
            )
            repo.enqueue_job(task_id="DRILL-T1")

            c1 = repo.claim_next_job(worker_id="w1", lease_duration_seconds=1.0)
            assert c1 is not None
            tok1 = c1["fencing_token"]

            with repo._lock, repo._conn:
                repo._conn.execute(
                    "UPDATE jobs SET lease_expires_at = datetime('now', '-10 seconds')"
                )

            c2 = repo.claim_next_job(worker_id="w2", lease_duration_seconds=60.0)
            assert c2 is not None
            tok2 = c2["fencing_token"]

            # w1 迟到写
            w1_ok = repo.complete_job(
                task_id="DRILL-T1",
                worker_id="w1",
                status="completed",
                fencing_token=tok1,
            )
            # w2 正常写
            w2_ok = repo.complete_job(
                task_id="DRILL-T1",
                worker_id="w2",
                status="completed",
                fencing_token=tok2,
            )
            if w1_ok or not w2_ok:
                print(f"  [FAIL] Fencing 校验异常: w1_ok={w1_ok}, w2_ok={w2_ok}")
                return False
    print("  [PASS] Fencing Token 校验演练成功。")
    return True


def drill_quota() -> bool:
    print("[drill:quota] 正在演练原子配额限制...")
    with tempfile.TemporaryDirectory() as td:
        db_file = Path(td) / "quota.sqlite3"
        with Repository(db_file) as repo:
            settings = Settings(principal_daily_budget_usd=5.0, task_budget_cap_usd=5.0)

            try:
                repo.reserve_quota(
                    task_id="T1",
                    principal_id="u1",
                    amount_usd=4.0,
                    tokens=1000,
                    daily_budget_usd=settings.principal_daily_budget_usd,
                    daily_token_budget=settings.principal_daily_token_budget,
                )
                r1 = True
            except QuotaExceededError:
                r1 = False

            try:
                repo.reserve_quota(
                    task_id="T2",
                    principal_id="u1",
                    amount_usd=2.0,
                    tokens=1000,
                    daily_budget_usd=settings.principal_daily_budget_usd,
                    daily_token_budget=settings.principal_daily_token_budget,
                )
                r2 = True
            except QuotaExceededError:
                r2 = False

            if not r1 or r2:
                print(f"  [FAIL] 配额超限拦截异常: r1={r1}, r2={r2}")
                return False
    print("  [PASS] 原子配额演练成功。")
    return True


def drill_readiness() -> bool:
    print("[drill:readiness] 正在演练存储探针与异常状态映射...")
    with tempfile.TemporaryDirectory() as td:
        db_file = Path(td) / "ready.sqlite3"
        with Repository(db_file) as repo:
            ok, d = check_readiness(repo, runs_root=Path(td))
            if not ok or d.get("status") != "ready":
                print(f"  [FAIL] 就绪探针正常状态异常: ok={ok}, d={d}")
                return False
    print("  [PASS] 就绪探针演练成功。")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PatchPilot 生产边界故障演练")
    parser.add_argument("--all", action="store_true", help="执行全部演练场景")
    parser.add_argument(
        "--drill",
        choices=["admission", "fencing", "quota", "readiness"],
        help="执行指定演练场景",
    )
    args = parser.parse_args(argv)

    if not args.all and not args.drill:
        args.all = True

    drills = {
        "admission": drill_admission,
        "fencing": drill_fencing,
        "quota": drill_quota,
        "readiness": drill_readiness,
    }

    selected = list(drills.keys()) if args.all else [args.drill]
    all_ok = True
    for name in selected:
        fn = drills[name]
        if not fn():
            all_ok = False

    if all_ok:
        print("\n== 全部生产边界演练通过 (exit 0) ==")
        return 0
    print("\n[!] 存在失败演练项 (exit 1)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
