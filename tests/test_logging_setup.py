"""P3-9 日志装配测试:TaskContextFilter 注入、Settings.log_level、装配不越权。"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.app import _setup_logging, create_app
from app.config import Settings, get_settings
from app.logctx import TaskContextFilter, reset_task_context, set_task_context


def test_task_context_filter_injects_ids() -> None:
    """Filter 把 contextvar 的 task_id/request_id 注入 LogRecord;未设置时空串。"""
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    logger = logging.getLogger("patchpilot.logctx-test")
    handler = _Capture()
    handler.addFilter(TaskContextFilter())
    handler.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        logger.info("without context")
        tokens = set_task_context("T-42", "R-7")
        try:
            logger.info("with context")
        finally:
            reset_task_context(tokens)
    finally:
        logger.removeHandler(handler)

    assert captured[0].task_id == "" and captured[0].request_id == ""
    assert captured[1].task_id == "T-42" and captured[1].request_id == "R-7"


def test_set_task_context_reset_restores_previous() -> None:
    """线程复用防泄漏:reset 后上下文回到空串,不残留上一个任务的 id。"""
    tokens = set_task_context("T-1")
    try:
        assert get_task_id() == "T-1"
    finally:
        reset_task_context(tokens)
    assert get_task_id() == ""


def get_task_id() -> str:
    from app.logctx import task_id_var

    return task_id_var.get()


def test_settings_log_level_default_and_validation() -> None:
    """log_level 第 26 键:默认 WARNING,非法值拒绝(与 llm_thinking 同风格)。"""
    settings = Settings(llm_base_url="", llm_api_key="")
    assert settings.log_level == "WARNING"
    with pytest.raises(ValidationError, match="log_level"):
        Settings(llm_base_url="", llm_api_key="", log_level="loud")


def test_setup_logging_is_noop_when_handlers_exist() -> None:
    """pytest/uvicorn 已配好日志时不越权接管(重复 create_app 亦然)。"""
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    _setup_logging(get_settings())
    assert root.handlers == handlers_before


def test_setup_logging_wires_level_and_context_filter() -> None:
    """干净 root 下:级别接线 + 每个 handler 挂 TaskContextFilter + 端到端注入。"""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    root.handlers = []
    try:
        settings = get_settings().model_copy(update={"log_level": "INFO"})
        _setup_logging(settings)
        assert root.level == logging.INFO
        assert root.handlers, "basicConfig 未接线"
        assert all(
            any(isinstance(f, TaskContextFilter) for f in h.filters) for h in root.handlers
        )

        captured: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record)

        capture = _Capture()
        root.addHandler(capture)
        tokens = set_task_context("T-E2E")
        try:
            root.info("wired")
        finally:
            reset_task_context(tokens)
        assert captured and captured[0].task_id == "T-E2E"
    finally:
        root.handlers = saved_handlers
        root.level = saved_level


def test_lifespan_warns_on_empty_api_token(tmp_path, caplog) -> None:
    """空 token 启动告警(R3-Q8 可立即实现项):lifespan 进入时 WARNING。"""
    app = create_app(db_path=tmp_path / "db.sqlite3", runs_root=tmp_path / "runs")
    with caplog.at_level(logging.WARNING, logger="app.api.app"), TestClient(app):
        pass
    assert any("PATCHPILOT_API_TOKEN is empty" in r.message for r in caplog.records)
