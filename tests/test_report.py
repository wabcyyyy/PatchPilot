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


def test_repro_engine_fallback_is_unknown_not_graph(tmp_path: Path) -> None:
    """P3-16 钉死:engine 与 provenance 全缺的历史行,复现命令兜底 --engine unknown
    (不可执行的诚实),绝不编造可执行的 --engine graph 假命令。"""
    runs = tmp_path / "runs" / "legacy"
    _write_report(runs / "BUG-001-x", engine="")
    text = render(runs, BUG_ROOT)

    assert f"--engine unknown --out {runs.as_posix()}" in text
    assert "--engine graph" not in text


def test_repro_empty_batch_example_uses_plain(tmp_path: Path) -> None:
    """P3-16 钉死:空批次的示例命令用 plain(驱动器真实支持的引擎),
    不再硬编码 graph。"""
    from app.evals.report import repro_commands

    cmds = repro_commands(tmp_path / "empty", [], "docs/eval-report.md")
    assert any("--engine plain" in c and "--bug BUG-001" in c for c in cmds)
    assert not any("--engine graph" in c for c in cmds)


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


def test_provenance_new_fields_keep_repro_merge_stable(tmp_path: Path) -> None:
    """E2:provenance 新增 config_snapshot/started_at 后,复现命令归并与溯源
    说明行为不变(既有字段是唯一输入,新字段只增不改)。"""
    from app.evals.provenance import build_provenance

    runs = tmp_path / "runs" / "b1"
    _write_report(runs / "BUG-001-x", provenance=build_provenance("fake-replay", "", "plain"))
    rows = _per_bug(runs.parent)

    from app.evals.report import repro_commands

    cmds = repro_commands(runs, rows, "docs/eval-report.md")
    assert any("--model fake" in c and "--bug BUG-001" in c for c in cmds)
    text = render(runs, BUG_ROOT)
    assert "批次溯源:" in text  # git_commit 非空,溯源行照常归并


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


# ---------- E7:报告分布字段与判型计数 ----------


def test_distribution_rows_counts_and_percentiles(tmp_path: Path) -> None:
    """3 份不同耗时/token/轮数/判型的 report → 分布值与计数逐一断言。

    P3-13:门禁拦截按 gate_violations 计数(跨引擎通用口径),
    不再按 status == PATCH_REJECTED 计(graph 批永不为该终态)。
    """
    from app.evals.report import distribution_rows

    runs = tmp_path / "runs" / "dist"
    _write_report(
        runs / "BUG-001-a",
        duration_ms=1000,
        tokens_used=100,
        rounds=1,
    )
    _write_report(
        runs / "BUG-002-b",
        bug_id="BUG-002",
        duration_ms=2000,
        tokens_used=200,
        rounds=2,
        verdict="failed",
        status="PATCH_REJECTED",
        gate_violations=["[files] modifying test file is forbidden: tests/test_x.py"],
    )
    _write_report(
        runs / "BUG-003-c",
        bug_id="BUG-003",
        duration_ms=9000,
        tokens_used=300,
        rounds=3,
        verdict="needs_review",
        status="NEEDS_REVIEW",
    )

    rows = distribution_rows(_per_bug(runs))
    table = dict(rows)
    assert table["耗时 min/p50/p95/max"] == "1000 / 2000 / 9000 / 9000 ms"
    assert table["Token min/max"] == "100 / 300"
    assert table["轮数分布(1 / 2 / 3+)"] == "1 / 1 / 1"
    assert table["判型计数"] == "resolved 1 · 门禁拦截 1 · needs_review 1 · 其他 0"


def test_distribution_counts_gate_rejections_across_engines(tmp_path: Path) -> None:
    """P3-13 钉死:graph 批门禁拒绝的终态是 BUDGET_EXCEEDED(轮尽回滚),
    PATCH_REJECTED 对 graph 恒不出现——判型计数按 gate_violations 口径,
    graph 批的门禁拦截不得被记 0 或误入「其他」。"""
    from app.evals.report import distribution_rows

    runs = tmp_path / "runs" / "graph-reject"
    _write_report(
        runs / "BUG-004-d",
        bug_id="BUG-004",
        verdict="failed",
        status="BUDGET_EXCEEDED",  # graph 引擎轮尽回滚后的真实终态
        gate_violations=["[paths] path escapes workspace: ../escape.txt"],
    )
    _write_report(
        runs / "BUG-005-e",
        bug_id="BUG-005",
        verdict="failed",
        status="VERIFY_FAILED",  # 无门禁违规的失败:入「其他」
    )
    table = dict(distribution_rows(_per_bug(runs)))
    assert table["判型计数"] == "resolved 0 · 门禁拦截 1 · needs_review 0 · 其他 1"


def test_distribution_rows_empty_batch_and_missing_fields(tmp_path: Path) -> None:
    """空批次记 n/a 不抛异常;渲染端到端含分布表(缺字段行容忍)。"""
    from app.evals.report import distribution_rows, render

    empty = dict(distribution_rows([]))
    assert set(empty) == {
        "耗时 min/p50/p95/max",
        "Token min/max",
        "轮数分布(1 / 2 / 3+)",
        "判型计数",
    }
    assert all(v == "n/a" for v in empty.values())

    runs = tmp_path / "runs" / "sparse"
    _write_report(runs / "BUG-001-x")  # 默认字段即可渲染
    text = render(runs, BUG_ROOT)
    assert "耗时 min/p50/p95/max" in text and "判型计数" in text


def test_all_green_single_round_real_batch_warns(tmp_path: Path) -> None:
    """P3-18 钉死:真实模型批 ≥5 题全绿且全部 1 轮 → 报告级软集形态警报;
    fake 回放批同形态不触发(脚本构造使然)。"""
    real_runs = tmp_path / "runs" / "real-soft"
    for i in range(1, 6):
        _write_report(
            real_runs / f"BUG-00{i}-x",
            bug_id=f"BUG-00{i}",
            model_provider="openai",
            provenance={"model_provider": "openai", "engine": "plain"},
        )
    text = render(real_runs, BUG_ROOT)
    assert "软集形态警报" in text

    fake_runs = tmp_path / "runs" / "fake-soft"
    for i in range(1, 6):
        _write_report(fake_runs / f"BUG-00{i}-y", bug_id=f"BUG-00{i}")
    assert "软集形态警报" not in render(fake_runs, BUG_ROOT)

    # 分散形态(有 2 轮题)不触发:警报只对"全 1 轮"的软集形态负责
    mixed_runs = tmp_path / "runs" / "real-mixed"
    for i in range(1, 5):
        _write_report(
            mixed_runs / f"BUG-00{i}-z",
            bug_id=f"BUG-00{i}",
            model_provider="openai",
            provenance={"model_provider": "openai", "engine": "plain"},
        )
    _write_report(
        mixed_runs / "BUG-005-w",
        bug_id="BUG-005",
        model_provider="openai",
        rounds=2,
        provenance={"model_provider": "openai", "engine": "plain"},
    )
    assert "软集形态警报" not in render(mixed_runs, BUG_ROOT)
