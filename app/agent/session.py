"""会话状态(G06):一个任务从受理到终局的那一条连续会话的事实记录。

为什么需要这一层:引擎把 LOCALIZE / PLAN / PROPOSE 当**独立循环**跑,每个循环自己建
`messages`、自己回填工具结果。结果是"会话"这个概念在代码里没有落点——
阶段之间只靠一段模型自述的 `findings` 文本衔接(见 `app/context/task_memory.py` 的缺陷记录),
而"这条 assistant 有没有配对的 tool 回执"这种一致性只散在 `plain_loop` 的四个分支里靠自觉。

`SessionState` 承担两件事,而且只做这两件:

1. **连续身份**:session_id / 协议版本 / 阶段顺序 / 每阶段的能力集合 / 记忆版本。
   快照与报告据此判断"续跑的这一次和崩溃前是不是同一条会话"(SPEC 契约 6);
2. **消息配对规则的唯一实现处**:`orphan_tool_calls()` 找出"assistant 声明了但没人回执"的
   tool_call,`repair_orphans()` 用显式标注的占位回执补齐。严格端点(DeepSeek 等)对孤儿消息
   直接 400,而"花了钱还 400"这条已经在付费批上崩穿过一次整题(见 `4a4093e` 的教训)。

刻意**不做**的事:把上一阶段的完整 messages 原样灌进下一阶段。那会让 PROPOSE 的请求体积
随 LOCALIZE 的轮数线性膨胀(实测那种 16-19 轮只读调查就是几十屏),而 G06 的可验证要求是
"阶段切换不丢证据"——丢不丢由 TaskMemory 的证据索引决定,跟"搬不搬原始对话"无关。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

SESSION_PROTOCOL = "repair_v2.session.1"
ORPHAN_REPLY = "[unanswered] this tool call had no recorded result before the process stopped"

# G12:工具调用状态机。planned(已声明)→ running(执行中)→ completed(有回执)。
# 崩溃把 planned/running 卡在中间 ⇒ 恢复时标记 unknown:"跑没跑过"这件事已经不可知,
# 恢复策略(registry.RECOVERY_POLICY)决定措辞:读类可安全重放、写类先核对状态、
# 命令类不许盲目重跑——而不是让模型自己猜。
CALL_PLANNED = "planned"
CALL_RUNNING = "running"
CALL_COMPLETED = "completed"
CALL_UNKNOWN = "unknown"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _call_name(call: Any) -> str:
    """从 OpenAI 线格式的 tool_call 里取函数名(取不到返回空串)。"""
    if not isinstance(call, dict):
        return ""
    function = call.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "")
    return ""


def _recovery_hint(name: str, recovery: Callable[[str], str] | None) -> str:
    """按恢复策略补一句"下一步怎么判断"(G12;recovery=None 或无名 = 不补)。"""
    if recovery is None or not name:
        return ""
    policy = recovery(name)
    if policy == "state_check":
        return (
            "; this call may have modified the workspace — check git_diff / the frozen"
            " candidate state before resubmitting, and only resubmit if the change is absent"
        )
    if policy == "no_rerun":
        return (
            "; its side effects are unknown — do not blindly rerun it;"
            " inspect the recorded evidence or choose a different, side-effect-free check"
        )
    return "; this call is safe to replay if you still need its result"


@dataclass
class StageRecord:
    """一次阶段进入(同一阶段可能因失败轮重复进入)。"""

    stage: str
    round_no: int
    allowed_tools: list[str] = field(default_factory=list)
    memory_version: int = 0
    turns: int = 0
    entered_at: str = field(default_factory=_now_iso)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ToolCallRef:
    call_id: str
    name: str
    stage: str
    round_no: int
    turn_no: int
    answered: bool = False
    # G12 状态机(见模块顶部常量);answered 保留给既有读方,两者同源更新
    status: str = CALL_PLANNED

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class SessionState:
    """一条连续会话的身份与配对事实。纯内存对象,落盘由 `app/graph/loop_state.py` 绑定。"""

    def __init__(self, task_id: str, session_id: str = "", *, protocol: str = SESSION_PROTOCOL):
        self.task_id = task_id
        self.session_id = session_id or f"s-{uuid4().hex[:12]}"
        self.protocol = protocol
        self.stages: list[StageRecord] = []
        self.calls: list[ToolCallRef] = []
        self.memory_version = 0
        self.evidence_refs: list[str] = []
        self.started_at = _now_iso()

    # ---------- 阶段与能力 ----------

    def begin_stage(
        self, stage: str, *, round_no: int, allowed_tools: list[str], memory_version: int
    ) -> StageRecord:
        record = StageRecord(
            stage=stage,
            round_no=round_no,
            allowed_tools=list(allowed_tools),
            memory_version=memory_version,
        )
        self.stages.append(record)
        self.memory_version = memory_version
        return record

    @property
    def current_stage(self) -> str:
        return self.stages[-1].stage if self.stages else ""

    def note_turn(self, turn_no: int) -> None:
        if self.stages:
            self.stages[-1].turns = max(self.stages[-1].turns, turn_no)

    def note_evidence(self, ref: str) -> None:
        if ref and ref not in self.evidence_refs:
            self.evidence_refs.append(ref)

    # ---------- 工具调用配对 ----------

    def declare_call(
        self, call_id: str, name: str, *, stage: str, round_no: int, turn_no: int
    ) -> None:
        self.calls.append(
            ToolCallRef(call_id=call_id, name=name, stage=stage, round_no=round_no, turn_no=turn_no)
        )

    def mark_running(self, call_id: str) -> None:
        """执行前把声明标成 running(G12):崩溃落在这之后 ⇒ 恢复标 unknown。"""
        for call in self.calls:
            if call.call_id == call_id and call.status == CALL_PLANNED:
                call.status = CALL_RUNNING
                return

    def answer_call(self, call_id: str) -> None:
        for call in self.calls:
            if call.call_id == call_id:
                call.answered = True
                call.status = CALL_COMPLETED
                return

    def mark_open_calls_unknown(self) -> list[str]:
        """把 planned/running 的调用标成 unknown(恢复入口调用);返回被标的 call_id。

        "没有回执"不等于"没执行过"——这条状态就是那个不可知区间的显式表达,
        交给恢复策略(RECOVERY_POLICY)决定下一步措辞,而不是当成没跑过。
        """
        marked: list[str] = []
        for call in self.calls:
            if call.status in (CALL_PLANNED, CALL_RUNNING):
                call.status = CALL_UNKNOWN
                marked.append(call.call_id)
        return marked

    def restore_calls(self, rows: list[dict[str, Any]]) -> int:
        """从快照恢复工具调用账(G12):崩溃前那本账带回内存,状态原样保留。"""
        added = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            self.calls.append(
                ToolCallRef(
                    call_id=str(row.get("call_id", "")),
                    name=str(row.get("name", "")),
                    stage=str(row.get("stage", "")),
                    round_no=int(row.get("round_no", 0) or 0),
                    turn_no=int(row.get("turn_no", 0) or 0),
                    answered=bool(row.get("answered", False)),
                    status=str(row.get("status") or CALL_PLANNED),
                )
            )
            added += 1
        return added

    def unanswered(self) -> list[ToolCallRef]:
        return [call for call in self.calls if not call.answered]

    @staticmethod
    def orphan_tool_calls(messages: list[dict[str, Any]]) -> list[str]:
        """assistant 声明了、但后面没有配对 tool 回执的 call id 列表。

        这是对**线格式**的检查,不看内存里的账:恢复/续跑时唯一能信的就是即将发出去的那份消息。
        """
        declared: dict[str, str] = {}
        answered: set[str] = set()
        for message in messages:
            role = message.get("role")
            if role == "assistant":
                for call in message.get("tool_calls") or []:
                    if isinstance(call, dict) and call.get("id"):
                        declared[str(call["id"])] = "yes"
            elif role == "tool" and message.get("tool_call_id"):
                answered.add(str(message["tool_call_id"]))
        return [call_id for call_id in declared if call_id not in answered]

    @staticmethod
    def repair_orphans(
        messages: list[dict[str, Any]],
        recovery: Callable[[str], str] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """给孤儿 tool_call 补一条显式标注的占位回执(顺序:补齐在该 assistant 之后)。

        绝不静默伪造成功结果——回执内容就是"这一次没有记录到结果",模型看得见这件事。
        recovery(G12)给出这条工具的恢复策略:占位回执按策略补一句"接下来怎么判断"
        (读类可安全重放 / 写类先看状态 / 命令类不许盲目重跑)。默认 None = 与接入前
        逐字相同的占位文本(既有断言不受影响)。
        """
        repaired = [dict(message) for message in messages]
        added = 0
        index = 0
        while index < len(repaired):
            message = repaired[index]
            if message.get("role") != "assistant":
                index += 1
                continue
            answered = {
                str(item.get("tool_call_id"))
                for item in repaired[index + 1 :]
                if isinstance(item, dict) and item.get("role") == "tool"
            }
            inserts: list[dict[str, Any]] = []
            for call in message.get("tool_calls") or []:
                call_id = str(call.get("id") or "") if isinstance(call, dict) else ""
                if call_id and call_id not in answered:
                    inserts.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": ORPHAN_REPLY + _recovery_hint(_call_name(call), recovery),
                        }
                    )
            if inserts:
                repaired[index + 1 : index + 1] = inserts
                added += len(inserts)
                index += len(inserts)
            index += 1
        return repaired, added

    # ---------- 身份与序列化 ----------

    def snapshot(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "task_id": self.task_id,
            "protocol_version": self.protocol,
            "started_at": self.started_at,
            "memory_version": self.memory_version,
            "evidence_refs": list(self.evidence_refs),
            "stages": [s.as_dict() for s in self.stages],
            "calls": [c.as_dict() for c in self.calls][-200:],
        }

    def identity_hash(self) -> str:
        """会话身份 hash:阶段序列 + 每阶段能力集合 + 记忆版本。

        恢复比的就是它——同一 session_id 但阶段序列或能力集合变了,就不是那条会话。
        """
        payload = {
            "session_id": self.session_id,
            "protocol_version": self.protocol,
            "stages": [
                (s.stage, s.round_no, tuple(s.allowed_tools), s.memory_version) for s in self.stages
            ],
        }
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @classmethod
    def from_snapshot(cls, raw: dict[str, Any]) -> SessionState | None:
        if not isinstance(raw, dict) or raw.get("protocol_version") != SESSION_PROTOCOL:
            return None
        session = cls(
            str(raw.get("task_id", "")),
            str(raw.get("session_id", "")),
            protocol=SESSION_PROTOCOL,
        )
        session.memory_version = int(raw.get("memory_version", 0))
        session.evidence_refs = [str(x) for x in (raw.get("evidence_refs") or [])]
        session.started_at = str(raw.get("started_at", session.started_at))
        for row in raw.get("stages") or []:
            if isinstance(row, dict):
                session.stages.append(
                    StageRecord(
                        stage=str(row.get("stage", "")),
                        round_no=int(row.get("round_no", 0)),
                        allowed_tools=[str(x) for x in (row.get("allowed_tools") or [])],
                        memory_version=int(row.get("memory_version", 0)),
                        turns=int(row.get("turns", 0)),
                        entered_at=str(row.get("entered_at", "")),
                    )
                )
        for row in raw.get("calls") or []:
            if isinstance(row, dict):
                session.calls.append(
                    ToolCallRef(
                        call_id=str(row.get("call_id", "")),
                        name=str(row.get("name", "")),
                        stage=str(row.get("stage", "")),
                        round_no=int(row.get("round_no", 0)),
                        turn_no=int(row.get("turn_no", 0)),
                        answered=bool(row.get("answered", False)),
                        # G12 新增字段:旧快照缺 status ⇒ 按 answered 推导(有回执=completed,
                        # 无回执=planned),语义与加字段前逐字相同
                        status=str(
                            row.get("status")
                            or (
                                CALL_COMPLETED if bool(row.get("answered", False)) else CALL_PLANNED
                            )
                        ),
                    )
                )
        return session
