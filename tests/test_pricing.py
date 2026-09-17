"""N2b:成本核算(价目表/覆盖文件/fake 恒 None)+ 旧库 migration + 报告成本行。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.api.report import render_markdown
from app.config import get_settings
from app.evals.pricing import PRICES, estimate_cost
from app.storage.repository import Repository

REQUIRED_MODELS = ("deepseek-chat", "deepseek-reasoner", "gpt-4o-mini", "gpt-4o")


def test_builtin_prices_cover_required_models() -> None:
    for model in REQUIRED_MODELS:
        prompt_usd, completion_usd = PRICES[model]
        assert prompt_usd > 0 and completion_usd > prompt_usd


def test_estimate_cost_exact_match() -> None:
    # 1M 输入 + 1M 输出
    assert estimate_cost("gpt-4o-mini", 1_000_000, 1_000_000) == pytest.approx(0.75)
    assert estimate_cost("deepseek-chat", 2_000_000, 0) == pytest.approx(0.54)


def test_estimate_cost_unknown_model_is_none() -> None:
    assert estimate_cost("not-a-real-model", 1000, 1000) is None


def test_estimate_cost_fake_provider_always_none() -> None:
    assert estimate_cost("fake", 1000, 1000) is None
    assert estimate_cost("fake-replay", 1000, 1000) is None


def test_price_overrides_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """覆盖文件优先于内置:可加新模型,也可改内置价格。"""
    overrides = tmp_path / "prices.json"
    overrides.write_text('{"my-model": [1.0, 2.0], "gpt-4o": [3.0, 4.0]}', encoding="utf-8")
    monkeypatch.setenv("PATCHPILOT_PRICE_OVERRIDES", str(overrides))
    get_settings.cache_clear()
    try:
        assert estimate_cost("my-model", 1_000_000, 0) == pytest.approx(1.0)
        assert estimate_cost("gpt-4o", 1_000_000, 0) == pytest.approx(3.0)  # 覆盖生效
    finally:
        get_settings.cache_clear()


def test_price_overrides_malformed_file_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overrides = tmp_path / "broken.json"
    overrides.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("PATCHPILOT_PRICE_OVERRIDES", str(overrides))
    get_settings.cache_clear()
    try:
        assert estimate_cost("gpt-4o", 0, 0) == 0.0  # 回落内置,不抛异常
    finally:
        get_settings.cache_clear()


def _old_schema_db(path: Path) -> None:
    """手工建一个缺新列的旧 schema 库,并预置一条旧数据。"""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE evaluations ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " task_id TEXT UNIQUE, bug_id TEXT, localized INTEGER, patch_applied INTEGER,"
        " final_resolved INTEGER, regression_introduced INTEGER, security_blocked INTEGER,"
        " rounds INTEGER, tokens INTEGER, duration_ms INTEGER)"
    )
    conn.execute(
        "INSERT INTO evaluations (task_id, bug_id, final_resolved, tokens)"
        " VALUES ('T-OLD', 'BUG-001', 1, 160)"
    )
    conn.commit()
    conn.close()


def test_migration_adds_columns_and_keeps_old_rows(tmp_path: Path) -> None:
    db = tmp_path / "old.sqlite3"
    _old_schema_db(db)

    repo = Repository(db)  # connect 触发 migration
    columns = {
        row["name"] for row in repo._conn.execute("PRAGMA table_info(evaluations)").fetchall()
    }
    assert {"cost_usd", "tokens_prompt", "tokens_completion"} <= columns

    rows = repo.list_evaluations()
    assert len(rows) == 1  # 旧数据完好
    assert rows[0]["task_id"] == "T-OLD" and rows[0]["tokens"] == 160

    # 新列可写
    repo.upsert_evaluation(
        task_id="T-NEW", bug_id="BUG-002", cost_usd=0.0123, tokens_prompt=10, tokens_completion=20
    )
    new_row = next(r for r in repo.list_evaluations() if r["task_id"] == "T-NEW")
    assert new_row["cost_usd"] == pytest.approx(0.0123)
    assert new_row["tokens_prompt"] == 10 and new_row["tokens_completion"] == 20


def test_markdown_renders_cost_line() -> None:
    md = render_markdown({"task_id": "T-1", "bug_id": "B", "cost_usd": 0.01234})
    assert "成本(约值):$0.0123" in md

    md_none = render_markdown({"task_id": "T-2", "bug_id": "B", "cost_usd": None})
    assert "成本(约值):n/a" in md_none
