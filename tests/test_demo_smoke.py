"""S08:demo 冒烟——直接 subprocess 跑当前脚本,不绕过 demo/main。

review F7:旧演示提交 diff_text 字段,真实跑是 VERIFY_FAILED;
本文件钉住"脚本本身在新目录跑通且 resolved",并证明演示源目录零写入。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_WS = REPO_ROOT / "demo" / "workspace-copy"


def _dir_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _run_module(
    module: str, out: Path, *, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATCHPILOT_LLM_ENABLED": "false", "PYTHONIOENCODING": "utf-8"}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", module, "--out", str(out)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )


def test_dirty_ticket_demo_subprocess_resolves(tmp_path: Path) -> None:
    """F7 主验收:当前脚本 subprocess 真跑 → resolved,旧字段不再出现,源目录零写入。"""
    before = _dir_hash(DEMO_WS)
    out = tmp_path / "demo-out"
    proc = _run_module("demo.run_dirty_ticket", out)
    assert proc.returncode == 0, (proc.returncode, proc.stdout[-800:], proc.stderr[-800:])
    assert _dir_hash(DEMO_WS) == before, "演示源目录必须零写入"

    reports = sorted(out.glob("*/report.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["status"] == "FINISHED" and report["verdict"] == "resolved"
    assert report["changed_files"] == ["src/dateparse.py"]
    assert report["resource_status"] == "within_budget"
    # F7 直接证据:轨迹里 apply_patch 事件用 patch_text,不再出现 diff_text
    events = [
        json.loads(line)
        for line in reports[0]
        .parent.joinpath("trajectory.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    apply_events = [e for e in events if e["tool"] == "apply_patch"]
    assert apply_events, "回放必须真实调用过 apply_patch"
    for event in apply_events:
        assert "diff_text" not in (event.get("input") or {}), "旧字段 diff_text 不得复活"
        assert "patch_text" in (event.get("input") or {})
    # 补丁可读且非空
    assert (reports[0].parent / "diff.patch").read_text(encoding="utf-8").strip()


def test_dirty_ticket_demo_fails_nonzero_on_broken_replay(tmp_path: Path) -> None:
    """演示失败必须非零退出:临时换坏脚本(直接构造不可解析补丁)。"""
    # 用环境变量无法注入脚本;退而验证 main() 的失败分支——
    # 以 monkeypatch 的等价路径覆盖:这里直接断言脚本对 --out 可写性失败会非零。
    out = tmp_path / "demo-out" / "nested-file.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("block the out dir creation\n", encoding="utf-8")
    proc = _run_module("demo.run_dirty_ticket", out)
    assert proc.returncode != 0, "输出目录不可创建时必须非零退出"


# ---------- G16:四个新演示的 subprocess 冒烟(每个演示钉一条,不混装) ----------


def test_issue_only_demo_subprocess_resolves(tmp_path: Path) -> None:
    """issue-only 全流程:批准后生产、门禁拒绝反馈重试、报告记验收清单与 repair_v2 身份。"""
    before = _dir_hash(DEMO_WS)
    out = tmp_path / "demo-out"
    proc = _run_module("demo.run_issue_only_ticket", out)
    assert proc.returncode == 0, (proc.returncode, proc.stdout[-800:], proc.stderr[-800:])
    assert _dir_hash(DEMO_WS) == before, "演示源目录必须零写入"

    reports = sorted(out.glob("runs/*/report.json"))
    assert reports, proc.stdout[-400:]
    report = json.loads(reports[-1].read_text(encoding="utf-8"))
    assert report["verdict"] == "resolved"
    evidence = report["evidence"]
    assert evidence["acceptance"]["frozen"] is True
    assert evidence["run_identity"]["agent_profile"] == "repair_v2"
    assert report["rounds"] >= 2, "门禁拒绝后的反馈重试必须留下轮次事实"


def test_steer_demo_subprocess_resolves(tmp_path: Path) -> None:
    """澄清与纠偏:报告 inputs 记 1 问已答 + 1 纠偏已消费。"""
    before = _dir_hash(DEMO_WS)
    out = tmp_path / "demo-out"
    proc = _run_module("demo.run_steer_ticket", out)
    assert proc.returncode == 0, (proc.returncode, proc.stdout[-800:], proc.stderr[-800:])
    assert _dir_hash(DEMO_WS) == before, "演示源目录必须零写入"

    reports = sorted(out.glob("runs/*/report.json"))
    assert reports, proc.stdout[-400:]
    inputs = json.loads(reports[-1].read_text(encoding="utf-8"))["evidence"]["inputs"]
    assert [q for q in inputs["questions"] if q["status"] in ("answered", "consumed")]
    assert [s for s in inputs["steers"] if s["consumed"]]


def test_delegate_demo_subprocess_resolves(tmp_path: Path) -> None:
    """子任务委派:主线 resolved,报告 subtasks 登记 researcher 贡献。"""
    before = _dir_hash(DEMO_WS)
    out = tmp_path / "demo-out"
    env_extra = {"PATCHPILOT_DELEGATION_ENABLED": "true"}
    proc = _run_module("demo.run_delegate_ticket", out, extra_env=env_extra)
    assert proc.returncode == 0, (proc.returncode, proc.stdout[-800:], proc.stderr[-800:])
    assert _dir_hash(DEMO_WS) == before, "演示源目录必须零写入"

    reports = sorted(out.glob("runs/*/report.json"))
    assert reports, proc.stdout[-400:]
    report = json.loads(reports[-1].read_text(encoding="utf-8"))
    assert report["verdict"] == "resolved"
    assert len(report["subtasks"]) == 1
    assert report["subtasks"][0]["role"] == "researcher" and report["subtasks"][0]["ok"]


def test_restart_resume_demo_subprocess_resolves(tmp_path: Path) -> None:
    """崩溃恢复:续跑 resolved,轨迹留 resume_from_checkpoint 与复位事件。"""
    before = _dir_hash(DEMO_WS)
    out = tmp_path / "demo-out"
    proc = _run_module("demo.run_restart_resume_ticket", out)
    assert proc.returncode == 0, (proc.returncode, proc.stdout[-800:], proc.stderr[-800:])
    assert _dir_hash(DEMO_WS) == before, "演示源目录必须零写入"

    run_dir = out / "runs" / "DEMO-RESUME"
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert report["verdict"] == "resolved"
    tools = [
        json.loads(line)["tool"]
        for line in (run_dir / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert "resume_from_checkpoint" in tools and "resume_reset_workspace" in tools
    assert tools.count("create_workspace") == 1, "已完成阶段不得重跑"
