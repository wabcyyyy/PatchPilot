"""API 请求/响应模型与统一错误结构。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class TaskCreateIn(BaseModel):
    """创建任务:首版以 bug_id 指定题目;repo_path+issue 预留给任意仓库。"""

    bug_id: str = Field(..., description="bugs/ 下的题目编号,如 BUG-001")
    engine: Literal["graph", "plain"] = "graph"
    model: Literal["fake", "openai"] = "fake"
    max_rounds: int | None = Field(None, ge=1, le=20)


class TaskOut(BaseModel):
    task_id: str
    bug_id: str
    status: str
    verdict: str | None = None
    engine: str | None = None
    model_provider: str | None = None
    run_dir: str | None = None
    created_at: str | None = None
    finished_at: str | None = None


class ErrorResponse(BaseModel):
    code: str
    message: str
    task_id: str | None = None


class TrajectoryPage(BaseModel):
    task_id: str
    total_returned: int
    offset: int
    events: list[dict[str, Any]]
