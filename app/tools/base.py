"""工具层公共类型:ToolContext 与 ToolResult。"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.adapters.pytest_adapter import BugEnv
from app.agent.session import SessionState
from app.context.evidence import EvidenceRef, EvidenceStore
from app.context.task_memory import TaskMemory
from app.executor.whitelist import DEFAULT_WHITELIST
from app.tools.tracker import Tracker


@dataclass
class ToolContext:
    """一次任务执行期内所有工具共享的上下文。

    workspace 内的一切操作都被限制在该目录;测试执行只允许
    manifest 预定义的 test_sets,不允许 Agent 任意指定用例。
    """

    task_id: str
    workspace: Path
    baseline_commit: str
    tracker: Tracker
    report_dir: Path = field(
        default_factory=lambda: Path("runs")
    )  # junit 等报告的落盘目录(工作区外)
    python_exe: str = field(default_factory=lambda: sys.executable)
    # 题目自带执行环境(外部数据集)。None = 沿用 python_exe 与全局镜像配置,
    # 存量题目行为不变。
    env: BugEnv | None = None
    test_sets: dict[str, list[str]] = field(default_factory=dict)
    allowed_paths: list[str] | None = None
    whitelist: tuple[str, ...] = DEFAULT_WHITELIST
    max_read_lines: int = 400
    max_search_results: int = 50
    max_patch_files: int = 5
    test_timeout_seconds: int = 120
    forbid_test_files: bool = True
    # 自适应分支触发信号(卡5):连续"补丁应用失败"的次数(门禁/协议拒绝不计——
    # 那是可修正的单线反馈,只有真应用失败才说明当前思路在原地打转)
    patch_fail_streak: int = 0
    # 工作区写入代数(G05):0 = 受理时冻结的输入快照,每次成功的写操作(apply_patch /
    # reset_workspace)在 registry 里 +1。证据引用靠它判断"我看到的还是不是当前内容",
    # 而不是让模型拿一份改前的源码当现状用。
    workspace_revision: int = 0
    # 原始回执仓(G05)。None = 尚未建;registry 在第一次需要落盘时按 report_dir 建,
    # 所以所有既有构造点(含 23 处测试夹具)一行未改就能工作。
    evidence: EvidenceStore | None = None
    # 任务记忆与会话身份(G06)。两者默认 None ⇒ 未接线时的每一次运行与今天逐字节相同
    # (候选 ctx、离线脚本与 23 处测试夹具都不必构造记忆)。registry 只在句柄存在时登记观测。
    memory: TaskMemory | None = None
    session: SessionState | None = None
    # ---------- G09:受控命令与复现工件 ----------
    # 任务执行后端(冻结口径):"" = 运行时读 Settings.execution_backend;
    # 节点侧构造 ctx 时按受理时冻结的值传入,运行中改环境变量不改变已受理任务
    execution_backend: str = ""
    # 取消事件(协作式):run_command 在等待子进程/容器退出期间轮询它
    cancel_event: threading.Event | None = None
    # write_repro 的登记表:名字 → 内容 sha256(python_repro 只运行登记过的脚本)
    repro_files: dict[str, str] = field(default_factory=dict)
    # 受控命令的执行序号(scratch 原始输出文件与诊断副本的编号)
    command_seq: int = 0
    # ---------- G10:issue-only 诊断与验收清单 ----------
    # 任务模式:"repair"(缺省)| "diagnose_then_repair"。repair 行为与引入前逐字相同
    mode: str = "repair"
    # 验收清单是否已冻结:diagnose 模式下 apply_patch 的放行凭据(平台写,模型不可见地读)
    acceptance_frozen: bool = False
    # Agent 已提交、尚未被 diagnose 节点取走的验收提议(diagnosis.proposal.AcceptanceProposal)
    pending_acceptance: Any | None = None
    # 受理身份引用:proposal/manifest 与冻结 TaskSpec 绑定用(空 = 未接线,旧行为不变)
    source_snapshot_hash: str = ""
    task_spec_hash: str = ""
    # ---------- G11:用户交互通道 ----------
    # ask_user 的等待通道(InteractionChannel)。None = 未接线(headless):问题照落盘,
    # 但不等回答——run_plain_loop 以 clarification_required 结构化收场。
    # 问题与回答的持久语义在 run_dir/interaction_state.json(InteractionStore),不在这里。
    interaction: Any | None = None
    # 任务级累计的用户等待时长(G11):循环在每次等待结束后累加,节点按增量把任务
    # deadline 顺延出去(恰好一次,见 TaskNodes._sync_wait_pause_deadline)。
    interaction_paused_seconds: float = 0.0


@dataclass
class ToolResult:
    ok: bool
    output: Any = None
    error: str | None = None
    # 这条回执的原始文本落在证据仓的位置(超大且被裁过时才有值)。它不是给模型的
    # 字段,而是给上层/G06 任务记忆建引用用的身份。
    evidence: EvidenceRef | None = None
    # **发给模型**的那一份:超过可见上限时由 registry 生成(有界视图 + `_bounded` +
    # `evidence` 元数据)。None = 没裁过,发 `output` 原样。与 output 分两个字段是刻意的:
    # 取证摘要读 output(未裁),消息组装读 visible(已裁),两边都不必靠猜。
    visible: Any = None

    @classmethod
    def fail(cls, error: str) -> ToolResult:
        return cls(ok=False, output=None, error=error)
