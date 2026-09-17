"""API 请求/响应模型与统一错误结构。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class TaskCreateIn(BaseModel):
    """创建任务:bug_id 指定题目,或 repo_path+issue 接入任意仓库(恰好一个)。"""

    bug_id: str | None = Field(None, description="bugs/ 下的题目编号,如 BUG-001")
    repo_path: str | None = Field(None, description="自定义 Git 仓库路径;与 bug_id 恰好一个")
    issue_text: str | None = Field(None, description="缺陷描述(repo_path 时必填)")
    failed_tests: list[str] | None = Field(None, description="基线应失败的测试(repo_path 时必填)")
    regression_tests: list[str] | None = Field(
        None, description="基线应通过的回归测试(repo_path 时必填)"
    )
    allowed_paths: list[str] | None = Field(
        None, description="补丁白名单;None = 不设白名单(禁改测试文件仍由门禁无条件兜底)"
    )
    replay_script: list[dict[str, Any]] | None = Field(
        None, description="model=fake 时的回放脚本(与 bugs/ 回放脚本同构);自定义任务必给"
    )
    engine: Literal["graph", "plain"] = "graph"
    model: Literal["fake", "openai"] = "fake"
    max_rounds: int | None = Field(None, ge=1, le=20)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> TaskCreateIn:
        if (self.bug_id is None) == (self.repo_path is None):
            raise ValueError("exactly one of bug_id or repo_path is required")
        if self.repo_path is not None:
            missing = [
                name
                for name, value in (
                    ("issue_text", self.issue_text),
                    ("failed_tests", self.failed_tests),
                    ("regression_tests", self.regression_tests),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"repo_path task requires: {', '.join(missing)}")
        return self


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
