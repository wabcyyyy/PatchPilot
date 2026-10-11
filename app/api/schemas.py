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
    failed_tests: list[str] | None = Field(
        None,
        description=(
            "基线应失败的测试;与 regression_tests 成对提供,"
            "双缺 = issue-only 诊断受理(diagnose_then_repair,仅 graph 引擎)"
        ),
    )
    regression_tests: list[str] | None = Field(
        None, description="基线应通过的回归测试;与 failed_tests 成对"
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
            if not self.issue_text:
                raise ValueError("repo_path task requires issue_text")
            if bool(self.failed_tests) != bool(self.regression_tests):
                raise ValueError(
                    "failed_tests and regression_tests must be provided together;"
                    " omit both for issue-only (diagnose_then_repair) intake"
                )
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


class AcceptanceApproveIn(BaseModel):
    """POST /tasks/{id}/acceptance 的批准入参(G10):绑定提议身份与版本。"""

    proposal_id: str = Field(..., description="提议 id(run_dir/acceptance_proposal.json)")
    content_hash: str = Field(..., description="提议内容 hash——这就是被批准的对象")
    expected_revision: int = Field(..., ge=0, description="提议 revision,防过期批准")


class AcceptanceOut(BaseModel):
    """批准结果:冻结的清单身份与任务现状。"""

    task_id: str
    proposal_id: str
    manifest_hash: str
    already_frozen: bool = False
    resumed: bool = False
    status: str


class MessageIn(BaseModel):
    """POST /tasks/{id}/messages(G11):回答澄清问题或追加途中纠偏。

    answer 必须带 question_id 与 expected_revision(防过期回答);idem_key 供重复
    提交幂等。文本永远只作为**会话线索**进入任务,不改变测试、权限与预算契约。
    """

    kind: Literal["answer", "steer"]
    text: str = Field(..., min_length=1, max_length=4000)
    question_id: str = Field("", max_length=64, description="answer 必填(run_dir 工件里的 id)")
    expected_revision: int = Field(0, ge=0, description="answer 必填:提问时的输入版本")
    idem_key: str = Field("", max_length=100, description="重复提交的幂等键(可选)")


class MessageOut(BaseModel):
    """输入受理结果:是否写入、是否幂等命中、唤醒/重排队与任务现状。"""

    task_id: str
    kind: str
    accepted: bool
    already: bool = False
    woke_waiter: bool = False
    resumed: bool = False
    question_id: str = ""
    status: str


class QuestionOut(BaseModel):
    """当前未回答问题与输入版本(CLI/API 展示;无问题时 question 为 null)。"""

    task_id: str
    status: str
    revision: int
    question: dict[str, Any] | None = None
    pending_steers: int = 0


class TrajectoryPage(BaseModel):
    task_id: str
    total_returned: int
    offset: int
    events: list[dict[str, Any]]
