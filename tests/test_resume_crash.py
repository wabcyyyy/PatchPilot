"""真·进程死亡(os._exit)后的续跑证明(M6 的最后一块证据)。

`tests/test_resume.py` 已经覆盖"抛异常的崩溃",但异常路径会走完 `finally`(连接被正常关闭、
清理被执行),那不是真实进程被杀。真崩溃留下两件事必须验:
① SqliteSaver 的连接句柄随进程一起死,检查点库要能被**新进程**重新打开并读出续跑位置;
② turn 边界的快照必须在崩溃前就已落盘(原子写),否则"无缝恢复"只是话说得漂亮。
所以崩溃方跑在**子进程**里、用 `os._exit(7)` 死亡(不触发任何 finally),恢复在测试进程里发起。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from app.evals.bugset import load_bug
from app.graph.loop_state import load_loop_snapshot
from app.graph.runner import run_task_graph
from app.llm.fake import FakeLLM
from tests.conftest import block
from tests.test_graph import BUG_ROOT, _fix_diff
from tests.test_resume import MARKER  # 脏补丁的标记串(子进程自己 import _partial_diff)

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET = "src/dateparse.py"

_CHILD = '''
"""崩溃方子进程:补丁落盘后的下一次模型调用直接 os._exit,不留任何清理机会。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from app.evals.bugset import load_bug          # noqa: E402
from app.graph.runner import run_task_graph     # noqa: E402
from app.llm.fake import FakeLLM                # noqa: E402
from tests.conftest import block                # noqa: E402
from tests.test_graph import (  # noqa: E402
    BUG_ROOT,
    _localize_script,
)
from tests.test_resume import _partial_diff     # noqa: E402


class DieAfterPatch(FakeLLM):
    """看到"本轮之前已经 apply_patch 过"就死:于是快照停在 PROPOSE 的第 1 轮。"""

    def complete(self, messages, tools):
        for message in messages:
            if message.get("role") != "assistant":
                continue
            for call in message.get("tool_calls") or []:
                if (call.get("function") or {}).get("name") == "apply_patch":
                    sys.stdout.flush()
                    os._exit(7)  # 真崩溃:不走 finally、不关连接、不删快照
        return super().complete(messages, tools)


# 崩溃前那份补丁刻意是**半截的脏补丁**(动过锚点行):续跑真能打上正确修复,
# 等价于"工作区确实被复位过",而不是"补丁碰巧可重复应用"
script = _localize_script() + [
    {"tool": "apply_patch", "args": {"patch_text": block(_partial_diff())}}
]
child = run_task_graph(
    load_bug("BUG-001", BUG_ROOT),
    DieAfterPatch(script),
    runs_root=sys.argv[2],
    task_id=sys.argv[3],
    run_dir=Path(sys.argv[2]) / sys.argv[3],
)
print("CHILD-DID-NOT-DIE", child.status)
'''


def _events(run_dir: Path) -> list[dict[str, Any]]:
    lines = (run_dir / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_resume_survives_an_unclean_process_death(tmp_path: Path) -> None:
    task_id = "T-HARD-KILL"
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    child_script = tmp_path / "crash_child.py"
    # 文件名刻意避开 stdlib 与 site-packages 同名(select.py 那种坑),放在仓库外
    child_script.write_text(_CHILD, encoding="utf-8")
    env = {**os.environ, "PATCHPILOT_LLM_ENABLED": "false", "PYTHONIOENCODING": "utf-8"}

    proc = subprocess.run(
        [sys.executable, str(child_script), str(REPO_ROOT), str(runs_root), task_id],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )

    run_dir = runs_root / task_id
    assert proc.returncode == 7, (proc.returncode, proc.stdout[-400:], proc.stderr[-400:])
    assert "CHILD-DID-NOT-DIE" not in proc.stdout
    snapshot = load_loop_snapshot(run_dir, stage="PROPOSE_PATCH", round_no=1, task_id=task_id)
    assert snapshot is not None, "崩溃前那一段工作记忆必须已经在盘上"
    assert snapshot.turn_no == 1 and len(snapshot.messages) > 2
    target = run_dir / "workspace" / TARGET
    assert MARKER in target.read_text(encoding="utf-8"), "脏补丁已落盘才死"

    # 新进程(测试进程)必须能重开那个被"硬死"打断的连接写下的检查点并续跑
    resumed = run_task_graph(
        load_bug("BUG-001", BUG_ROOT),
        FakeLLM(
            [
                {"tool": "apply_patch", "args": {"patch_text": block(_fix_diff())}},
                {"tool": "run_tests", "args": {"test_set": "failed"}},
                {"tool": "run_tests", "args": {"test_set": "regression"}},
                {"tool": "finish", "args": {"success": True, "summary": "复位后按锚点重打"}},
            ]
        ),
        runs_root=runs_root,
        task_id=task_id,
        run_dir=run_dir,
        resume=True,
    )

    assert resumed.status == "FINISHED", (resumed.status, resumed.error)
    assert resumed.verdict == "resolved"
    assert resumed.changed_files == [TARGET]
    assert MARKER not in target.read_text(encoding="utf-8"), "复位必须真的发生"

    events = _events(run_dir)
    names = [e["tool"] for e in events]
    assert "resume_from_checkpoint" in names
    resume_at = names.index("resume_from_checkpoint")
    after = names[resume_at:]
    assert "resume_reset_workspace" in after, "可写阶段续跑前必须复位(抹掉崩溃前的半截现场)"
    assert events[names.index("resume_reset_workspace")]["output_summary"]["rolled_back"] is True
    assert "apply_gate" in after and "run_tests" in after, "门禁与验证必须在续跑这次真重跑"
    assert names[:resume_at].count("create_workspace") == 1
    assert after.count("create_workspace") == 0, "已完成阶段不得重跑"
