"""演示:崩溃恢复——工具边界进程死亡 → 检查点续跑 → 门禁/验证全部重跑。

与 run_api_ticket 的取消演示分工:取消是**终态**(CANCELLED 不可复活);本脚本
演示的是"跑一半崩了"的 M6/S03 语义——崩溃现场保全、续跑前工作区复位、门禁与
双集验证在续跑的这次执行里**真实重跑**,不把崩溃前的判断当结论。全程离线 FakeLLM。

流程:
  1. 第一次运行:LOCALIZE 收敛 → 打补丁 → 在 run_tests 的工具边界"进程死亡"
     (BaseException 绕过 except Exception,与真实崩溃同形——终态收敛没机会跑);
  2. 核对现场:崩溃残留补丁还在工作区、循环快照在 PROPOSE_PATCH 第 1 轮;
  3. 第二次运行(resume=True):续跑决策 → 复位到基线 → 按锚点重打正确补丁 →
     apply 门禁 → 双集验证(含双跑复核)→ resolved;
  4. 轨迹:resume_from_checkpoint / resume_reset_workspace / 验证事件可查,
     create_workspace 只出现一次(已完成阶段不重跑)。
边界说明:崩在模型调用**在途**时账本会留未知持有,续跑验证全绿也拒判 resolved
(G03 语义,test_budget_reservations 单独钉);本演示选工具边界,续跑可以按既有
契约判 resolved——两种现场在测试里都有钉子。
任一环节失败返回非零退出码。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from app.evals.bugset import load_bug
from app.graph.loop_state import load_loop_snapshot
from app.graph.runner import run_task_graph
from app.llm.fake import FakeLLM

ROOT = Path(__file__).resolve().parent.parent
BUG001 = ROOT / "bugs" / "BUG-001"


class _ProcessDied(BaseException):
    """进程死亡的代理:BaseException 绕过 except Exception,终态收敛不会跑。"""


def _die_at_tool_boundary(tool_name: str = "run_tests"):
    """返回一个把 plain_loop.execute 换成"指定工具边界进程死亡"的上下文管理器。

    崩点刻意选在"模型回复**已结算**、工具还没执行"的边界(与
    tests/test_resume.py 的图级用例同形):这一刻账本没有在途模型调用。
    对比:崩在模型调用**在途**时,账本会留下未知持有 token,续跑即使验证全绿,
    终局验收也拒判 resolved(G03"中段崩溃不得认证成功",
    tests/test_budget_reservations.py 单独钉住)——那是另一种现场,本演示不覆盖。
    """

    import app.graph.plain_loop as plain_loop

    real_execute = plain_loop.execute

    class _Context:
        def __enter__(self):  # type: ignore[no-untyped-def]
            def _die(ctx, name, arguments, **kwargs):  # type: ignore[no-untyped-def]
                if name == tool_name:
                    raise _ProcessDied(f"process died at the {tool_name} tool boundary")
                return real_execute(ctx, name, arguments, **kwargs)

            plain_loop.execute = _die
            return self

        def __exit__(self, *exc: object) -> None:
            plain_loop.execute = real_execute

    return _Context()


def _step(title: str, ok: bool, detail: str = "") -> None:
    mark = "OK  " if ok else "FAIL"
    print(f"[{mark}] {title}" + (f" — {detail}" if detail else ""))
    if not ok:
        raise AssertionError(f"demo step failed: {title}: {detail}")


def _good_steps() -> list[dict]:
    """BUG-001 的完整冻结回放(调查→补丁→验证);不手写锚点,不筛选步骤——
    脚本一旦错位,步骤会落进错误的阶段(LOCALIZE/PLAN 的工具白名单不同)。"""
    return json.loads((BUG001 / "replay" / "graph-script.json").read_text(encoding="utf-8"))


def _events(run_dir: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (run_dir / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="崩溃恢复演示(离线)")
    parser.add_argument(
        "--out",
        default=str(ROOT / "runs" / f"demo-resume-{datetime.now().strftime('%Y%m%d-%H%M%S')}"),
        help="本次演示的输出目录",
    )
    args = parser.parse_args(argv)
    out_root = Path(args.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    bug = load_bug("BUG-001", ROOT / "bugs")
    task_id = "DEMO-RESUME"
    run_dir = out_root / "runs" / task_id

    # ① 第一次运行:调查收敛 → 打上正确补丁 → 死在 run_tests 工具边界
    crash_script = _good_steps()
    try:
        with _die_at_tool_boundary("run_tests"):
            run_task_graph(
                bug,
                FakeLLM(crash_script),
                runs_root=out_root / "runs",
                task_id=task_id,
                run_dir=run_dir,
            )
        raise AssertionError("第一次运行应当以进程死亡告终")
    except _ProcessDied:
        _step("第一次运行在 run_tests 工具边界进程死亡(无终态收敛)", True)

    target = run_dir / "workspace" / "src" / "dateparse.py"
    _step(
        "崩溃现场保全:已应用的补丁还在工作区",
        "value.strip()" in target.read_text(encoding="utf-8"),
    )
    snapshot = load_loop_snapshot(run_dir, stage="PROPOSE_PATCH", round_no=1, task_id=task_id)
    _step(
        "循环快照在案(PROPOSE_PATCH 第 1 轮)",
        snapshot is not None and snapshot.turn_no >= 1,
        f"turn_no={snapshot.turn_no if snapshot else None}",
    )

    # ② 续跑:崩溃点在 PROPOSE 中段(候选尚未冻结)→ 快照续接,写阶段先复位到基线,
    #    门禁与双集验证在续跑的这次执行里真实重跑
    resumed = run_task_graph(
        bug,
        FakeLLM(_good_steps()),
        runs_root=out_root / "runs",
        task_id=task_id,
        run_dir=run_dir,
        resume=True,
    )
    _step(
        "续跑到 resolved",
        resumed.status == "FINISHED" and resumed.verdict == "resolved",
        f"{resumed.status}/{resumed.verdict}",
    )

    events = _events(run_dir)
    names = [e["tool"] for e in events]
    _step("轨迹记下续跑决策(resume_from_checkpoint)", "resume_from_checkpoint" in names)
    _step("已完成阶段不重跑(create_workspace 仅一次)", names.count("create_workspace") == 1)
    after = names[names.index("resume_from_checkpoint") :]
    _step(
        "续跑前先复位工作区(resume_reset_workspace)",
        "resume_reset_workspace" in after,
    )
    _step(
        "门禁与双集验证在续跑里真实重跑",
        "apply_gate" in after and "verify" in after,
        f"gate={after.count('apply_gate')} verify={after.count('verify')}",
    )
    _step(
        "崩溃残留被复位覆盖,最终补丁来自续跑",
        "value.strip()" in target.read_text(encoding="utf-8") and names.count("apply_patch") >= 2,
        f"apply_patch×{names.count('apply_patch')}",
    )

    print("\n崩溃恢复 demo: ALL PASS")
    print(f"> 产物目录:{out_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
