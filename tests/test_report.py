"""T10.1/T10.4 批次溯源、复现口径与多批次对比测试。"""

from __future__ import annotations

import json
from pathlib import Path

from app.evals.bugset import load_bug, load_replay_script
from app.evals.driver import run_task
from app.evals.metrics import annotate, collect_runs, latest_per_bug
from app.evals.report import render, render_multi
from app.graph.runner import run_task_graph
from app.llm.fake import FakeLLM

BUG_ROOT = Path("bugs")


def _write_report(run_dir: Path, provenance: dict | None = None, **fields) -> Path:
    run_dir.mkdir(parents=True)
    payload = {
        "task_id": run_dir.name,
        "bug_id": "BUG-001",
        "verdict": "resolved",
        "status": "FINISHED",
        "model_provider": "fake-replay",
        "engine": "graph",
        "rounds": 1,
        "tokens_used": 800,
        "duration_ms": 7000,
        "changed_files": ["src/dateparse.py"],
        "gate_violations": [],
        "verify_failed_ok": True,
        "verify_regression_ok": True,
    }
    payload.update(fields)
    if provenance is not None:
        payload["provenance"] = provenance
    (run_dir / "report.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return run_dir


def _per_bug(runs: Path) -> list:
    rows = [annotate(r, BUG_ROOT) for r in collect_runs(runs)]
    return latest_per_bug(rows)


def test_repro_commands_follow_batch_provenance(tmp_path: Path) -> None:
    runs = tmp_path / "runs" / "real"
    _write_report(
        runs / "BUG-001-x",
        provenance={"model_provider": "openai", "engine": "graph"},
        model_provider="openai",
    )
    _write_report(
        runs / "BUG-002-x",
        provenance={"model_provider": "fake-replay", "engine": "plain"},
        bug_id="BUG-002",
        model_provider="fake-replay",
        engine="plain",
    )
    text = render(runs, BUG_ROOT, report_out="docs/eval-real.md")

    assert f"--model openai --engine graph --out {runs.as_posix()}" in text
    assert f"--model fake --engine plain --out {runs.as_posix()}" in text
    assert f"python -m app.evals.report --runs {runs.as_posix()} --out docs/eval-real.md" in text
    # 旧版手写口径不允许再出现
    assert "--runs runs/m9" not in text


def test_repro_falls_back_to_row_fields_for_legacy_batches(tmp_path: Path) -> None:
    """无 provenance 的历史批次(旧 report.json)按行内 provider/engine 归并。"""
    runs = tmp_path / "runs" / "m9"
    _write_report(runs / "BUG-001-x")  # 默认 fake-replay + graph,无 provenance
    text = render(runs, BUG_ROOT)

    assert f"--model fake --engine graph --out {runs.as_posix()}" in text


def test_driver_records_provenance(tmp_path: Path) -> None:
    bug = load_bug("BUG-001", BUG_ROOT)
    result = run_task(bug, FakeLLM(load_replay_script(bug)), runs_root=tmp_path / "runs")
    report = json.loads((Path(result.run_dir) / "report.json").read_text(encoding="utf-8"))

    prov = report["provenance"]
    assert prov["model_provider"] == "fake-replay"
    assert prov["engine"] == "plain"
    assert prov["execution_backend"] in {"local", "docker"}
    assert prov["generated_at"]
    assert isinstance(prov["git_commit"], str)


def test_graph_engine_records_provenance(tmp_path: Path) -> None:
    bug = load_bug("BUG-001", BUG_ROOT)
    model = FakeLLM(load_replay_script(bug, kind="graph"))
    result = run_task_graph(bug, model, runs_root=tmp_path / "runs")
    report = json.loads((Path(result.run_dir) / "report.json").read_text(encoding="utf-8"))

    assert report["provenance"]["engine"] == "graph"
    assert report["provenance"]["model_provider"] == "fake-replay"


def test_report_shows_provenance_note(tmp_path: Path) -> None:
    runs = tmp_path / "runs" / "real"
    _write_report(
        runs / "BUG-001-x",
        provenance={
            "model_provider": "openai",
            "engine": "graph",
            "model_name": "gpt-4o-mini",
            "execution_backend": "docker",
            "git_commit": "abcdef1234567890",
        },
        model_provider="openai",
    )
    text = render(runs, BUG_ROOT)
    assert "批次溯源:模型 gpt-4o-mini · 执行后端 docker · 代码 abcdef123456" in text

    legacy = tmp_path / "runs" / "m9"
    _write_report(legacy / "BUG-001-x")  # 无 provenance 的旧批次
    assert "批次溯源" not in render(legacy, BUG_ROOT)


def test_unknown_provider_not_claimed_as_real(tmp_path: Path) -> None:
    """provider 缺失(unknown)的批次不得被宣称为'真实模型成绩'。"""
    runs = tmp_path / "runs" / "odd"
    _write_report(runs / "BUG-001-x", model_provider="unknown")
    text = render(runs, BUG_ROOT)
    assert "真实模型在线调用成绩" not in text


def test_multi_batch_report_lists_each_batch(tmp_path: Path) -> None:
    fake_runs = tmp_path / "runs" / "m9"
    real_runs = tmp_path / "runs" / "real"
    _write_report(fake_runs / "BUG-001-x")
    _write_report(
        real_runs / "BUG-001-y",
        provenance={"model_provider": "openai", "engine": "graph"},
        model_provider="openai",
    )
    text = render_multi([fake_runs, real_runs], BUG_ROOT, report_out="docs/cmp.md")

    assert "多批次对比" in text
    # 每个批次前各一条分隔线(区分于表格里的 |---| 分隔)
    assert text.count("\n---\n") == 2
    assert f"`{fake_runs.as_posix()}`" in text and f"`{real_runs.as_posix()}`" in text
    assert f"--model fake --engine graph --out {fake_runs.as_posix()}" in text
    assert f"--model openai --engine graph --out {real_runs.as_posix()}" in text
    assert "--out docs/cmp.md" in text


def test_main_accepts_multiple_runs(tmp_path: Path) -> None:
    from app.evals.report import main

    a, b, out = tmp_path / "a", tmp_path / "b", tmp_path / "cmp.md"
    _write_report(a / "BUG-001-x")
    _write_report(b / "BUG-002-x", bug_id="BUG-002")
    rc = main(["--runs", str(a), "--runs", str(b), "--bugs", "bugs", "--out", str(out)])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert "多批次对比" in text
    assert "BUG-001" in text and "BUG-002" in text
