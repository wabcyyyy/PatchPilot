"""API 请求/响应模型与统一错误结构。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class TaskCreateIn(BaseModel):
    """创建任务:bug_id 指定题目,或 repo_path+issue 接入任意仓库(恰好一个)。"""

    bug_id: str | None = Field(
        None,
        description="bugs/ 下的题目编号,如 BUG-001",
        pattern=r"^BUG-[A-Za-z0-9_-]+$",
    )
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
    """任务对外表示(R2 整改:路由实际挂载,收敛响应面)。

    刻意不外泄:idem_key(内部幂等键,可探测同键任务)、repo_path/issue_text
    (请求方自有输入回显无意义)、id(与 task_id 冗余的库内主键)。
    run_dir 保留:单机工具定位产物需要;多租户部署前必须一并去掉。
    """

    task_id: str
    bug_id: str
    status: str
    verdict: str | None = None
    engine: str | None = None
    model_provider: str | None = None
    max_rounds: int | None = None
    run_dir: str | None = None
    created_at: str | None = None
    finished_at: str | None = None
    # S07:运行中进度(独立于生命周期 status——LOCALIZE 这类 stage 不是终态);
    # 旧行缺列时为 None
    stage: str | None = None
    last_event_at: str | None = None


class TaskListOut(BaseModel):
    tasks: list[TaskOut]


class ErrorResponse(BaseModel):
    code: str
    message: str
    task_id: str | None = None


class TrajectoryPage(BaseModel):
    task_id: str
    total_returned: int
    offset: int
    events: list[dict[str, Any]]
