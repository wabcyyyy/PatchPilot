"""M4 驱动器测试:resolved 判定、INVALID_TASK 与门禁拒绝路径。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.evals.bugset import load_bug, load_replay_script
from app.evals.driver import run_task
from app.llm.fake import FakeLLM

BUG_ROOT = Path("bugs")


def test_replay_resolves_bug001(tmp_path: Path) -> None:
    bug = load_bug("BUG-001", BUG_ROOT)
    model = FakeLLM(load_replay_script(bug))
    result = run_task(bug, model, runs_root=tmp_path / "runs")

    assert result.verdict == "resolved" and result.status == "FINISHED"
    assert result.changed_files == ["src/dateparse.py"]
    assert result.baseline_failed >= 1
    assert result.verify_failed_ok and result.verify_regression_ok

    run_dir = Path(result.run_dir)
    assert (run_dir / "report.json").exists()
    assert (run_dir / "diff.patch").exists()
    assert (run_dir / "trajectory.jsonl").exists()
    events = [
        json.loads(line)
        for line in (run_dir / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert {"create_workspace", "apply_patch", "run_tests", "finish"} <= {e["tool"] for e in events}


def test_replay_resolves_cross_file_bug005(tmp_path: Path) -> None:
    bug = load_bug("BUG-005", BUG_ROOT)
    model = FakeLLM(load_replay_script(bug))
    result = run_task(bug, model, runs_root=tmp_path / "runs")
    assert result.verdict == "resolved"
    assert "src/helpers.py" in result.changed_files  # 根因文件,症状在 pipeline


def test_already_fixed_bug_is_invalid_task(tmp_path: Path) -> None:
    """把 BUG-003 的仓库预置为已修复 → 基线无失败 → INVALID_TASK。"""
    source = BUG_ROOT / "BUG-003"
    bug_dir = tmp_path / "BUG-003-FIXED"
    shutil.copytree(source, bug_dir)
    labels = bug_dir / "repo" / "src" / "labels.py"
    labels.write_text(
        '"""标签拼接工具。"""\n\n\ndef join_labels(labels, sep=None):\n'
        '    separator = "," if sep is None else sep\n'
        '    result = ""\n'
        "    for index, label in enumerate(labels):\n"
        "        if index > 0:\n"
        "            result += separator\n"
        "        result += label\n"
        "    return result\n",
        encoding="utf-8",
        newline="\n",
    )

    bug = load_bug(bug_dir)
    model = FakeLLM(load_replay_script(bug))
    result = run_task(bug, model, runs_root=tmp_path / "runs")
    assert result.status == "INVALID_TASK"
    assert "already pass" in (result.error or "")


def test_test_file_patch_gets_rejected(tmp_path: Path) -> None:
    """Agent 试图修改测试文件 → 门禁拒绝 → 无法 resolved,违规记录在案。"""
    malicious = [
        {
            "tool": "apply_patch",
            "args": {
                "diff_text": (
                    "diff --git a/tests/test_labels.py b/tests/test_labels.py\n"
                    "--- a/tests/test_labels.py\n"
                    "+++ b/tests/test_labels.py\n"
                    "@@ -1,4 +1,5 @@\n"
                    " from src.labels import join_labels\n"
                    "+\n"
                    " def test_default_separator():\n"
                    '     assert join_labels(["a", "b", "c"]) == "a,b,c"\n'
                )
            },
        },
        {"tool": "finish", "args": {"success": True, "summary": "试图改测试作弊"}},
    ]
    bug = load_bug("BUG-003", BUG_ROOT)
    result = run_task(bug, FakeLLM(malicious), runs_root=tmp_path / "runs")
    assert result.verdict == "failed"
    assert result.status in {"PATCH_REJECTED", "VERIFY_FAILED"}
    assert result.verify_failed_ok is False  # 原失败测试没有被修复
    # R2 整改:此前 OR 断言把两种不同结局混为一谈。实际链路:apply_patch 在
    # 工具层被 [files] 门禁拒绝(轨迹留痕)→ 终局 diff 为空 → 终局门禁报 [format]
    events = [
        json.loads(line)
        for line in (Path(result.run_dir) / "trajectory.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    patch_events = [e for e in events if e.get("tool") == "apply_patch"]
    assert patch_events and any("[files]" in (e.get("error") or "") for e in patch_events), events
    assert result.gate_violations == ["[format] diff is empty"]


def test_crash_converges_to_needs_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """驱动器把意外异常收敛为 NEEDS_REVIEW,不向上抛。"""
    bug = load_bug("BUG-004", BUG_ROOT)

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.evals.driver.run_plain_loop", boom)
    model = FakeLLM(load_replay_script(bug))
    result = run_task(bug, model, runs_root=tmp_path / "runs")
    assert result.status == "NEEDS_REVIEW" and result.verdict == "needs_review"
    assert "boom" in (result.error or "")


def test_report_contains_token_detail(tmp_path: Path) -> None:
    """N2a:report.json 落盘 tokens_prompt/tokens_completion 且与 fake 语义一致。"""
    bug = load_bug("BUG-001", BUG_ROOT)
    result = run_task(bug, FakeLLM(load_replay_script(bug)), runs_root=tmp_path / "runs")
    report = json.loads((Path(result.run_dir) / "report.json").read_text(encoding="utf-8"))
    assert report["tokens_prompt"] >= 0 and report["tokens_completion"] >= 0
    assert result.tokens_prompt == 0
    assert result.tokens_completion == result.tokens_used > 0


# ---------- E2:评测溯源强制与批次清单 ----------


def test_provenance_carries_new_traceability_fields() -> None:
    """E2:provenance 自带 git_commit/config_snapshot/started_at 三新字段;
    git_commit 在本仓库(git 工作树)内实测非 unknown。"""
    from datetime import datetime

    from app.evals.provenance import build_provenance

    prov = build_provenance("fake-replay", "", "plain")
    assert prov["git_commit"] not in ("", "unknown")
    assert len(prov["git_commit"]) == 40
    snapshot = prov["config_snapshot"]
    assert set(snapshot) == {
        "llm_model",
        "llm_enabled",
        "execution_backend",
        "test_timeout_seconds",
        "llm_timeout_seconds",
        "token_budget",
        "max_patch_files",
    }
    started = datetime.fromisoformat(prov["started_at"])
    assert started.tzinfo is not None


def test_require_model_name_guard_three_branches() -> None:
    """E2 守卫三分支:回放放行 / 真实有名放行 / 真实空名 raise。"""
    from app.evals.provenance import require_model_name

    require_model_name("", llm_enabled=False)
    require_model_name(None, llm_enabled=False)
    require_model_name("deepseek-flash", llm_enabled=True)
    with pytest.raises(ValueError, match="model_name"):
        require_model_name("", llm_enabled=True)
    with pytest.raises(ValueError, match="model_name"):
        require_model_name(None, llm_enabled=True)
    with pytest.raises(ValueError, match="model_name"):
        require_model_name("   ", llm_enabled=True)


def test_real_run_without_model_name_fails_fast(tmp_path: Path, monkeypatch) -> None:
    """fail-fast 落点:真实模型(llm_enabled=True、provider 非 fake-replay)缺
    model_name 时,run_task 在物化任何仓库之前拒绝——零花费地失败,而不是
    花钱买回无身份的报告。fake-replay 豁免:回放零花费,不受守卫约束。"""
    from types import SimpleNamespace

    from app.config import get_settings

    real_settings = get_settings()
    monkeypatch.setattr(
        "app.evals.driver.get_settings",
        lambda: real_settings.model_copy(update={"llm_enabled": True}),
    )
    bug = load_bug("BUG-001", BUG_ROOT)
    real_stub = SimpleNamespace(provider="openai")  # 守卫先 raise,该模型不参与执行
    with pytest.raises(ValueError, match="model_name"):
        run_task(bug, real_stub, runs_root=tmp_path / "runs")
    # 守卫先于任何落盘:runs 目录根本不会被创建
    assert not (tmp_path / "runs").exists()


def test_fake_batch_writes_manifest(tmp_path: Path) -> None:
    """E2:fake 批次收尾写 batch_manifest.json,字段齐全、计数正确、自带溯源。"""
    from app.evals.driver import run_batch

    bugs = [load_bug(b, BUG_ROOT) for b in ("BUG-001", "BUG-005")]
    results = run_batch(
        bugs, lambda bug: FakeLLM(load_replay_script(bug)), runs_root=tmp_path / "fake-batch"
    )
    assert [r.verdict for r in results] == ["resolved", "resolved"]

    manifest = json.loads(
        (tmp_path / "fake-batch" / "batch_manifest.json").read_text(encoding="utf-8")
    )
    assert set(manifest) == {
        "git_commit",
        "started_at",
        "ended_at",
        "config_snapshot",
        "model",
        "bug_ids",
        "verdict_counts",
    }
    assert manifest["bug_ids"] == ["BUG-001", "BUG-005"]
    assert manifest["verdict_counts"] == {"resolved": 2}
    assert manifest["git_commit"] not in ("", "unknown")
    assert manifest["model"] == ""
    assert manifest["config_snapshot"]["execution_backend"] in {"local", "docker"}
