"""G16-C5:迁移与兼容验收——从旧 SQLite/loop 快照/报告形态出发,不伪造迁移成功。

SPEC G16:"迁移从旧 SQLite/loop/report fixture 出发;旧任务恢复不可自动使用新提示/
新工具/新价格。缺完整旧身份时待审,不伪造迁移成功。"三条钉:

1. 旧 SQLite 行(没有受理契约)→ recover_stale 判**待审**(NEEDS_REVIEW),绝不
   重新入队拿新配置跑一遍——那等于把"不知道当时怎么受理的"伪装成"接着跑";
2. 旧 loop 快照(没有会话/记忆/profile 绑定字段)→ 按既有语义继续参与恢复,
   不把"没绑定"判成 mismatch(把升级前所有快照一律待审 = 伪造迁移失败面);
3. custom fake 任务崩在补丁阶段、进程死亡 → 新服务实例 recover_stale 重新入队,
   回放脚本从**受理契约**复原(不读题目目录,改了也不跟随)→ 续跑 resolved。

配套接线修复:service._execute 恢复路径此前拿不到 custom 任务的回放脚本
(bugs/ 目录不存在该文件,load_replay_script 抛 TaskError 判死),现从冻结契约复原。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from app.api.service import TaskService
from app.graph.loop_state import (
    SNAPSHOT_VERSION,
    LoopSnapshot,
    load_loop_snapshot,
)
from app.graph.resume import binding_mismatch
from app.storage.repository import Repository

REPO_ROOT = Path(__file__).resolve().parent.parent
BUGS_ROOT = Path("bugs")
TERMINAL = {
    "FINISHED",
    "INVALID_TASK",
    "BUDGET_EXCEEDED",
    "VERIFY_FAILED",
    "PATCH_REJECTED",
    "NEEDS_REVIEW",
    "CANCELLED",
}

# 崩溃方子进程:建 custom fake 任务 → 轮询到补丁阶段 → os._exit(7),进程死、
# 契约与现场都在盘上。恢复方(测试进程)必须从契约复原回放脚本续跑。
# 契约脚本带标记串:续跑如果用的不是这份(修复缺失 = 判死;换了脚本来源 = 标记缺失),
# 断言会当场点名。FakeLLM 无状态,续跑从第 0 步重放整份脚本,因此脚本必须
# "从头再跑一遍也能收敛"(read/finish 幂等,apply 在复位后的工作区上重打)。
_CHILD = """
import os, sys, time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from app.api.service import TaskService            # noqa: E402
from app.storage.repository import Repository      # noqa: E402
from tests.conftest import block                   # noqa: E402
from tests.test_service_robustness import (        # noqa: E402
    _TINY_FIX_DIFF,
    _materialize_tiny_repo,
)

repo_dir = Path(sys.argv[2])
_materialize_tiny_repo(repo_dir)
service = TaskService(
    repo=Repository(sys.argv[3]),
    runs_root=Path(sys.argv[4]),
    bugs_root=Path(sys.argv[5]),
)
# 标记串打进补丁正文(一行注释):最终工作区里有它 = 续跑应用的就是契约这份补丁
patch_text = block(
    _TINY_FIX_DIFF.replace(
        "+        return None\\n",
        "+        return None\\n+    # CONTRACT-RESTORED-FIX\\n",
    )
)
script = [
    {"tool": "read_file", "args": {"path": "src/dateparse.py"}},
    {"tool": "finish", "args": {"success": True, "summary": "根因:空串未处理"}},
    {"tool": "apply_patch", "args": {"patch_text": patch_text}},
    {"tool": "run_tests", "args": {"test_set": "failed"}},
    {"tool": "run_tests", "args": {"test_set": "regression"}},
    {
        "tool": "finish",
        "args": {"success": True, "summary": "CONTRACT-RESTORED-FIX 空串按文档返回 None"},
    },
]
task, _ = service.create_task(
    repo_path=str(repo_dir),
    issue_text="空串未按文档返回 None(迁移恢复演示)",
    failed_tests=["tests/test_dateparse.py::test_empty_string_returns_none"],
    regression_tests=[
        "tests/test_dateparse.py::test_iso_format",
        "tests/test_dateparse.py::test_none_returns_none",
    ],
    replay_script=script,
    engine="graph",
    model="fake",
)
task_id = task["task_id"]
from app.graph.loop_state import SNAPSHOT_FILENAME   # noqa: E402

deadline = time.time() + 180
while time.time() < deadline:
    row = service.get_task(task_id)
    # 等到 PROPOSE 首轮快照落盘再崩:stage 翻转与首轮快照之间有个小窗口,
    # 死在那个窗口里(无快照无候选)会被恢复面如实判待审 —— 那是设计行为,
    # 本测试要的是确定性的"可恢复现场"。
    if (
        row
        and row.get("stage") == "PROPOSE_PATCH"
        and (Path(sys.argv[4]) / task_id / SNAPSHOT_FILENAME).is_file()
    ):
        print("TASK", task_id, flush=True)
        os._exit(7)  # 崩在补丁阶段:进程死,契约与现场都在盘上
    time.sleep(0.2)
os._exit(9)  # 没等到 PROPOSE_PATCH:现场不符合预期,判失败
"""


def _poll(service: TaskService, task_id: str, predicate, timeout_s: float = 180.0):
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        last = service.get_task(task_id) or {}
        if predicate(last):
            return last
        time.sleep(0.2)
    raise AssertionError(f"condition not met in {timeout_s}s (last={last})")


def test_old_sqlite_row_without_spec_converges_needs_review(tmp_path: Path) -> None:
    """旧 SQLite 行(无受理契约)→ 待审,不重新入队、不拿新配置跑一遍。"""
    repo = Repository(tmp_path / "old.sqlite3")
    repo.create_task(
        task_id="OLD-NO-SPEC",
        idem_key="old-idem",
        bug_id="BUG-001",
        repo_path=str(tmp_path / "nowhere"),
        issue_text="升级前的老任务",
        max_rounds=3,
        engine="graph",
        model_provider="fake",
        run_dir=str(tmp_path / "old-run"),
    )
    assert repo.set_status_unless_terminal("OLD-NO-SPEC", "RUNNING")
    (tmp_path / "old-run").mkdir()  # 没有 task_spec.json = 旧身份不完整

    service = TaskService(repo=repo, runs_root=tmp_path / "runs", bugs_root=BUGS_ROOT)
    # 返回值 = 处理掉的僵尸行数(两条路都计):这一行走的是"判待审"路,不是重新入队
    assert service.recover_stale() == 1

    row = repo.get_task("OLD-NO-SPEC")
    # 待审的标记是状态本身;旧路径收敛不写 verdict(留下"为什么待审"给人工看轨迹)
    assert row["status"] == "NEEDS_REVIEW"
    # 判待审不是判执行:没有轨迹、没有报告,不伪造"接着跑过"
    assert not (tmp_path / "old-run" / "trajectory.jsonl").exists()
    assert not (tmp_path / "old-run" / "report.json").exists()


def test_old_loop_snapshot_without_binding_still_resumes(tmp_path: Path) -> None:
    """旧快照(缺会话/记忆/profile 绑定字段)→ 按既有语义继续,不判 mismatch。"""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    raw = {
        "version": SNAPSHOT_VERSION,
        "stage": "LOCALIZE",
        "round_no": 1,
        "turn_no": 2,
        "messages": [{"role": "user", "content": "issue"}],
        "task_id": "T-OLD",
        # 刻意没有:session_id/session_hash/memory_version/memory_hash/
        # runtime_profile/runtime_policy_hash —— 升级前落盘的形态
    }
    from app.graph.loop_state import snapshot_path

    snapshot_path(run_dir).write_text(json.dumps(raw), encoding="utf-8")
    loaded = load_loop_snapshot(run_dir, stage="LOCALIZE", round_no=1, task_id="T-OLD")
    assert loaded is not None and loaded.session_id == "" and loaded.runtime_profile == ""

    # 恢复方按既有语义处理:两侧不都有绑定 ⇒ 空 mismatch(续跑,不是待审)
    session = type("S", (), {"session_id": "s-current"})()
    memory = type("M", (), {"version": 9, "state_hash": lambda self: "m-current"})()
    runtime = type("R", (), {"profile_name": "repair_v2", "policy_hash": "x" * 64})()
    mismatched, _ = binding_mismatch(loaded, session, memory, runtime)
    assert mismatched == []


def test_new_snapshot_binding_fields_are_detectable(tmp_path: Path) -> None:
    """对照组:新快照带绑定字段时,同形 loads 后 mismatch 面照常工作(不是永远放行)。"""
    snapshot = LoopSnapshot(
        stage="LOCALIZE",
        round_no=1,
        turn_no=1,
        task_id="T-NEW",
        session_id="s1",
        runtime_profile="repair_v2",
        runtime_policy_hash="A" * 64,
    )
    session = type("S", (), {"session_id": "s1"})()
    memory = type("M", (), {"version": 1, "state_hash": lambda self: "m1"})()
    runtime = type("R", (), {"profile_name": "legacy_v1", "policy_hash": "B" * 64})()
    mismatched, _ = binding_mismatch(snapshot, session, memory, runtime)
    assert "runtime_profile" in mismatched and "runtime_policy_hash" in mismatched


def test_custom_fake_task_resume_restores_replay_from_contract(tmp_path: Path) -> None:
    """崩在等待回答 → 新实例 recover_stale 重入队 → 契约复原回放 → 回答后 resolved。"""
    db = tmp_path / "svc.sqlite3"
    runs = tmp_path / "runs"
    runs.mkdir()
    child_script = tmp_path / "waiting_child.py"
    child_script.write_text(_CHILD, encoding="utf-8")
    env = {
        **os.environ,
        "PATCHPILOT_LLM_ENABLED": "false",
        # P12 起僵尸认定要求"job 租约已过期":给子进程 1 秒短租约,
        # 崩溃后稍等租约到期,恢复语义才与生产一致(其余断言不变)
        "PATCHPILOT_JOB_LEASE_DURATION_SECONDS": "1",
        "PYTHONIOENCODING": "utf-8",
    }

    proc = subprocess.run(
        [
            sys.executable,
            str(child_script),
            str(REPO_ROOT),
            str(tmp_path / "repo"),
            str(db),
            str(runs),
            str(BUGS_ROOT),
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert proc.returncode == 7, (proc.returncode, proc.stdout[-400:], proc.stderr[-400:])
    task_id = next(line.split()[1] for line in proc.stdout.splitlines() if line.startswith("TASK "))

    # 新服务实例(同一 db/runs):等子进程的短租约过期后,恢复重新入队,回放脚本从受理契约复原
    service = TaskService(repo=Repository(db), runs_root=runs, bugs_root=BUGS_ROOT)
    deadline = time.monotonic() + 30
    recovered = 0
    while time.monotonic() < deadline:
        recovered = service.recover_stale()
        if recovered:
            break
        time.sleep(1)
    assert recovered == 1

    final = _poll(service, task_id, lambda t: t["status"] in TERMINAL)
    assert final["status"] == "FINISHED" and final["verdict"] == "resolved", final

    # 迁移纪律的正面证据:续跑由**契约里冻结的脚本**驱动 —— 标记串只存在于契约脚本;
    # 复原缺失(修复前的行为)时恢复直接判死,换了脚本来源时标记串不会出现。
    run_dir = Path(final["run_dir"])
    events = [
        json.loads(line)
        for line in (run_dir / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    tools = {e["tool"] for e in events}
    assert {"apply_patch", "run_tests", "finish"} <= tools
    # 标记串只存在于契约脚本的补丁正文里:终局补丁含它 = 续跑应用的就是契约那份
    # (workspace 会被可弃集回收,diff.patch 是保留的取证产物)
    diff_text = (run_dir / "diff.patch").read_text(encoding="utf-8")
    assert "CONTRACT-RESTORED-FIX" in diff_text
