"""最小等待/批准契约(G10;G11 在同一机制上扩展任意澄清,不建第二套暂停系统)。

- `ApprovalRegistry` 是服务层持有的一份"任务 → 批准事件"登记:执行线程等待前登记,
  批准端点经 `approve` 唤醒同进程内的等待线程;进程已死(无 waiter)时调用方走
  重新入队路径,不假装有人被唤醒。
- `ControlEvent` 是控制事实的载体:事件 id、来源、内容 hash、expected_revision、
  幂等键。`source` 只能由平台写;模型文字永远不构成 trusted_user 批准(§3.2 的
  低信任输入原则在这里落成类型)。
- `wait_for_approval` 是唯一的等待语义:0 = 不等待(headless),超时返回显式结论,
  不无限占用执行线程。
- G11 在同一机制上扩展**澄清与纠偏**:`InteractionStore` 把"问题 / 回答 / 纠偏"
  落成 run_dir 工件(等待必须活过进程),`QuestionRegistry` 与 `ApprovalRegistry`
  同构地承担同进程唤醒;回答只能经 API/CLI 写入(`source=trusted_user` 由平台赋),
  模型文字永远不构成回答。纠偏只是追加进会话的线索,**不改变测试、权限与预算**。
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from app.errors import ConflictError, InvalidRequestError

SOURCE_PLATFORM = "platform"
SOURCE_TRUSTED_USER = "trusted_user"

KIND_ACCEPTANCE_REQUEST = "acceptance_request"
KIND_CLARIFICATION_REQUEST = "clarification_request"
KIND_ANSWER = "answer"
KIND_STEER = "steer"

WAIT_APPROVED = "approved"
WAIT_ANSWERED = "answered"
WAIT_TIMEOUT = "timeout"
WAIT_CANCELLED = "cancelled"

# 问题生命周期:open(等待回答)→ answered(回答已写入)→ consumed(被循环消费一次)
QUESTION_OPEN = "open"
QUESTION_ANSWERED = "answered"
QUESTION_CONSUMED = "consumed"

INTERACTION_FILENAME = "interaction_state.json"
INTERACTION_SCHEMA_VERSION = 1


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class ControlEvent:
    """一条控制事实:由平台写,模型不能伪造来源。"""

    event_id: str
    task_id: str
    kind: str
    source: str
    payload_hash: str
    expected_revision: int = 0
    question_id: str = ""  # G11 澄清问题的引用位;验收批准留空
    idem_key: str = ""  # 同一问题的重复提交经它幂等
    created_at: str = ""

    @staticmethod
    def create(
        *,
        task_id: str,
        kind: str,
        source: str,
        payload_hash: str,
        expected_revision: int = 0,
        question_id: str = "",
        idem_key: str = "",
    ) -> ControlEvent:
        if source not in (SOURCE_PLATFORM, SOURCE_TRUSTED_USER):
            raise ValueError(f"unsupported control event source: {source!r}")
        return ControlEvent(
            event_id=uuid.uuid4().hex,
            task_id=task_id,
            kind=kind,
            source=source,
            payload_hash=payload_hash,
            expected_revision=expected_revision,
            question_id=question_id,
            idem_key=idem_key,
            created_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "task_id": self.task_id,
            "kind": self.kind,
            "source": self.source,
            "payload_hash": self.payload_hash,
            "expected_revision": self.expected_revision,
            "question_id": self.question_id,
            "idem_key": self.idem_key,
            "created_at": self.created_at,
        }


class ApprovalRegistry:
    """任务级批准事件登记:同进程内唤醒等待线程的唯一通道。线程安全。"""

    def __init__(self) -> None:
        self._events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def register(self, task_id: str) -> threading.Event:
        with self._lock:
            event = self._events.get(task_id)
            if event is None:
                event = threading.Event()
                self._events[task_id] = event
            return event

    def approve(self, task_id: str) -> bool:
        """唤醒等待线程;返回 False = 本进程没有该任务的 waiter(调用方走重排队)。"""
        with self._lock:
            event = self._events.get(task_id)
        if event is None:
            return False
        event.set()
        return True

    def has_waiter(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._events

    def release(self, task_id: str) -> None:
        with self._lock:
            self._events.pop(task_id, None)


def wait_for_approval(
    event: threading.Event | None,
    wait_seconds: int,
    cancel_event: threading.Event | None = None,
) -> str:
    """等待批准:0 = 不等待(headless);超时返回 WAIT_TIMEOUT,不无限占用线程。

    取消经 `cancel_event` 协作式中断(默认 None = 不监听,与引入取消之前逐字相同):
    等待窗可能长达 acceptance_wait_seconds(默认 3600s),不能把取消拖到等待结束——
    每 0.25s 醒一次查取消;取消优先于慢到的批准(批准已经落盘,取消只是停动作,
    清单仍在盘上供人工复核)。
    """
    if event is None:
        return WAIT_TIMEOUT
    if wait_seconds <= 0:
        if cancel_event is not None and cancel_event.is_set():
            return WAIT_CANCELLED
        return WAIT_APPROVED if event.is_set() else WAIT_TIMEOUT
    deadline = time.monotonic() + float(wait_seconds)
    while True:
        if cancel_event is not None and cancel_event.is_set():
            return WAIT_CANCELLED
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return WAIT_TIMEOUT
        if event.wait(timeout=min(0.25, remaining)):
            return WAIT_APPROVED


# ---------- G11:澄清问题的持久状态 ----------


class InteractionIntegrityError(ValueError):
    """interaction_state.json 不可信(损坏/版本不符):读侧拒绝、写侧不覆盖。

    与 acceptance_proposal 的 ManifestIntegrityError 同一条纪律:磁盘上读不动的
    控制工件不许被读成"没有输入",也不许被下一次写入静默覆盖——那会把"用户已经回答过"
    抹成"从未回答"。
    """


class InteractionStore:
    """任务内问题 / 回答 / 纠偏的持久状态(run_dir/interaction_state.json)。

    为什么必须落盘:等待要活过进程重启(G11 的 T17)。内存事件只负责**唤醒**同进程
    的等待线程,而"问题是什么、回答是什么、消费过没有"全部是盘上工件。

    结构(全部平铺可审查):
    ```json
    {"schema_version": 1, "task_id": "...", "revision": 0,
     "questions": [{"question_id","question","choices","reason","created_at","revision",
                    "status","answer_text","answer_source","answer_idem","answered_at","consumed_at"}],
     "steers":    [{"idem_key","text","created_at","consumed_at"}]}
    ```
    - `revision` 是**输入版本**:每消费一次用户输入(回答或一批纠偏)+1;问题在提问时
      记下当时的版本,回答必须带同一个版本(expected_revision),过期回答拒绝。
    - 一次只允许一个未回答问题:第二个 ask 被拒(提问是暂停点,不是收件箱)。
    - 单进程内的读-改-写按**路径共享一把锁**(服务端写入与循环消费在同一进程;
      多进程部署本就未设计,见 design.md §7)。

    语义校验住在这一层,而不是调用方:回答一次性消费、重复幂等、不同答案冲突,
    这些规则只允许有一份实现。
    """

    _locks: ClassVar[dict[str, threading.Lock]] = {}
    _locks_guard = threading.Lock()

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / INTERACTION_FILENAME

    @classmethod
    def _lock_for(cls, path: Path) -> threading.Lock:
        key = str(path)
        with cls._locks_guard:
            lock = cls._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                cls._locks[key] = lock
            return lock

    # ---------- 原始读写 ----------

    @staticmethod
    def _empty(task_id: str = "") -> dict[str, Any]:
        return {
            "schema_version": INTERACTION_SCHEMA_VERSION,
            "task_id": task_id,
            "revision": 0,
            "questions": [],
            "steers": [],
        }

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return self._empty()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise InteractionIntegrityError(f"interaction state unreadable: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != INTERACTION_SCHEMA_VERSION:
            raise InteractionIntegrityError(
                "interaction state schema mismatch; refusing to interpret it"
            )
        for key in ("questions", "steers"):
            if not isinstance(raw.get(key), list):
                raise InteractionIntegrityError(f"interaction state field {key!r} is malformed")
        return raw

    def _write(self, state: dict[str, Any]) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    # ---------- 查询 ----------

    def read(self) -> dict[str, Any]:
        """当前状态副本(调用方只读;损坏即抛 InteractionIntegrityError)。"""
        with self._lock_for(self.path):
            return self._read()

    def pending(self) -> bool:
        """盘上是否有待处理的用户输入(未回答问题 / 未消费回答 / 未消费纠偏)。

        读不动按"没有待处理输入"处理并**不是**这里的职责——调用方(循环)要先自己
        决定损坏怎么办;本方法供"可恢复性"之类的粗查使用,损坏时向上抛由调用方定夺。
        """
        state = self.read()
        return self.pending_in(state)

    @staticmethod
    def pending_in(state: dict[str, Any]) -> bool:
        for question in state.get("questions", []):
            if question.get("status") in (QUESTION_OPEN, QUESTION_ANSWERED):
                return True
        return any(not steer.get("consumed_at") for steer in state.get("steers", []))

    def open_question(self) -> dict[str, Any] | None:
        """当前未回答问题(没有就是 None);损坏时抛 InteractionIntegrityError。"""
        return next(
            (q for q in self.read().get("questions", []) if q.get("status") == QUESTION_OPEN),
            None,
        )

    # ---------- 问题与回答 ----------

    def ask(
        self,
        *,
        task_id: str,
        question: str,
        choices: list[str] | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        """登记一个新问题;已有未回答问题或**已有回答未消费**时拒绝(提问是暂停点)。"""
        text = str(question).strip()
        if not text:
            raise InvalidRequestError("question text is empty")
        with self._lock_for(self.path):
            state = self._read()
            for existing in state["questions"]:
                if existing.get("status") in (QUESTION_OPEN, QUESTION_ANSWERED):
                    raise InvalidRequestError(
                        "a question is already open (or answered and not yet consumed);"
                        " wait for the user's answer before asking another"
                    )
            if state.get("task_id") and state["task_id"] != task_id:
                raise InteractionIntegrityError(
                    f"interaction state belongs to {state['task_id']}, not {task_id}"
                )
            question_row = {
                "question_id": uuid.uuid4().hex[:16],
                "question": text,
                "choices": [str(c) for c in (choices or []) if str(c).strip()],
                "reason": str(reason or ""),
                "created_at": _now_iso(),
                "revision": int(state.get("revision", 0)),
                "status": QUESTION_OPEN,
                "answer_text": "",
                "answer_source": "",
                "answer_idem": "",
                "answered_at": "",
                "consumed_at": "",
            }
            state["task_id"] = state.get("task_id") or task_id
            state["questions"].append(question_row)
            self._write(state)
            return dict(question_row)

    def submit_answer(
        self,
        *,
        question_id: str,
        expected_revision: int,
        text: str,
        idem_key: str = "",
    ) -> tuple[dict[str, Any], bool]:
        """写入一条 trusted_user 回答;返回 (问题行, 是否重复提交的幂等命中)。

        拒绝面(全部有明确冲突,不做猜测):未知/已消费后不同答案、revision 过期、
        重复提交但内容不同。同一 idem_key 或逐字相同内容的重复提交按幂等处理。
        """
        answer = str(text).strip()
        if not answer:
            raise InvalidRequestError("answer text is empty")
        with self._lock_for(self.path):
            state = self._read()
            target = None
            for question in state["questions"]:
                if question.get("question_id") == question_id:
                    target = question
                    break
            if target is None:
                raise ConflictError(
                    f"no question {question_id!r} on file (unknown or already rotated)"
                )
            status = str(target.get("status") or "")
            if status == QUESTION_OPEN:
                if int(expected_revision) != int(target.get("revision", 0)):
                    raise ConflictError(
                        f"stale answer: expected_revision={expected_revision},"
                        f" question revision={target.get('revision')}"
                    )
                target["status"] = QUESTION_ANSWERED
                target["answer_text"] = answer
                target["answer_source"] = SOURCE_TRUSTED_USER
                target["answer_idem"] = str(idem_key or "")
                target["answered_at"] = _now_iso()
                self._write(state)
                return dict(target), False
            # 已回答/已消费:重复提交的处置
            if idem_key and idem_key == target.get("answer_idem"):
                return dict(target), True
            if answer == target.get("answer_text"):
                return dict(target), True
            raise ConflictError(
                f"question {question_id!r} was already answered with different content"
                f" (status={status}); refusing to overwrite"
            )

    def consume_answer(self, question_id: str = "") -> tuple[str, str] | None:
        """消费一条已写入的回答(循环侧,恰好一次);同时把输入版本 +1。

        返回 (question_id, answer_text);只消费 status=answered 的问题。
        已被消费/仍 open 的都返回 None——"唤醒但没有答案可消费"不是正常路径,
        由调用方按保守口径收场。
        """
        with self._lock_for(self.path):
            state = self._read()
            for question in state["questions"]:
                if question_id and question.get("question_id") != question_id:
                    continue
                if question.get("status") != QUESTION_ANSWERED:
                    continue
                question["status"] = QUESTION_CONSUMED
                question["consumed_at"] = _now_iso()
                state["revision"] = int(state.get("revision", 0)) + 1
                self._write(state)
                return str(question.get("question_id") or ""), str(
                    question.get("answer_text") or ""
                )
        return None

    # ---------- 途中纠偏 ----------

    def submit_steer(self, *, text: str, idem_key: str = "") -> bool:
        """追加一条纠偏线索;返回是否幂等命中(同 idem 重复提交)。"""
        steer = str(text).strip()
        if not steer:
            raise InvalidRequestError("steer text is empty")
        with self._lock_for(self.path):
            state = self._read()
            if idem_key and any(s.get("idem_key") == idem_key for s in state["steers"]):
                return True
            state["steers"].append(
                {
                    "idem_key": str(idem_key or ""),
                    "text": steer,
                    "created_at": _now_iso(),
                    "consumed_at": "",
                }
            )
            self._write(state)
            return False

    def consume_steers(self) -> list[str]:
        """在安全边界消费全部未读纠偏(一批一次输入版本 +1;无新线索返回空表)。"""
        with self._lock_for(self.path):
            state = self._read()
            pending = [s for s in state["steers"] if not s.get("consumed_at")]
            if not pending:
                return []
            now = _now_iso()
            for steer in pending:
                steer["consumed_at"] = now
            state["revision"] = int(state.get("revision", 0)) + 1
            self._write(state)
            return [str(s.get("text") or "") for s in pending]


class QuestionRegistry:
    """任务级提问等待登记:同进程内唤醒等待线程的唯一通道(与 ApprovalRegistry 同构)。

    登记时带上"等的是哪个问题":回答若对不上,不许误唤醒(唤醒一个等别的问题的线程
    会让那次等待提前收场并可能把答案错配)。
    """

    def __init__(self) -> None:
        self._waiters: dict[str, tuple[threading.Event, str]] = {}
        self._lock = threading.Lock()

    def register(self, task_id: str, question_id: str) -> threading.Event:
        with self._lock:
            entry = self._waiters.get(task_id)
            event = threading.Event() if entry is None else entry[0]
            self._waiters[task_id] = (event, question_id)
            return event

    def answer(self, task_id: str, question_id: str) -> bool:
        """唤醒等待该问题的线程;返回 False = 没有 waiter 或问题不匹配。"""
        with self._lock:
            entry = self._waiters.get(task_id)
        if entry is None:
            return False
        event, expected = entry
        if question_id and expected and question_id != expected:
            return False
        event.set()
        return True

    def has_waiter(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._waiters

    def release(self, task_id: str) -> None:
        with self._lock:
            self._waiters.pop(task_id, None)


def wait_for_user_input(
    event: threading.Event | None,
    wait_seconds: int,
    cancel_event: threading.Event | None = None,
) -> str:
    """等待用户回答:语义与 wait_for_approval 逐字同构(0 = headless 不等待)。"""
    if event is None:
        return WAIT_TIMEOUT
    if wait_seconds <= 0:
        if cancel_event is not None and cancel_event.is_set():
            return WAIT_CANCELLED
        return WAIT_ANSWERED if event.is_set() else WAIT_TIMEOUT
    deadline = time.monotonic() + float(wait_seconds)
    while True:
        if cancel_event is not None and cancel_event.is_set():
            return WAIT_CANCELLED
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return WAIT_TIMEOUT
        if event.wait(timeout=min(0.25, remaining)):
            return WAIT_ANSWERED


@dataclass
class InteractionChannel:
    """ask_user 的等待通道(服务层构造,随任务执行线程注入 ctx)。

    `None`(未接线:离线驱动器、plain 引擎、测试桩)= **headless**:问题照样落盘,
    但不等回答——`run_plain_loop` 以 `clarification_required` 结构化收场,不无限等。
    通道只携带"怎么等/等多久/等待前通知谁";问题与回答的持久语义全在 InteractionStore。
    """

    registry: QuestionRegistry
    task_id: str
    wait_seconds: int
    on_waiting: Callable[[], None] | None = None

    def wait_for_answer(self, question_id: str, cancel_event: threading.Event | None = None) -> str:
        """登记 waiter → 通知等待态 → 等待 → 释放登记;返回 WAIT_* 结论。

        顺序是语义要求:waiter 先登记再通知等待态,否则"回答已落盘但状态还没置
        WAITING_INPUT"的窗口会让服务的重新入队分支与等待线程两个恢复方都启动。
        """
        event = self.registry.register(self.task_id, question_id)
        if self.on_waiting is not None:
            # 状态展示失败不得杀任务:等回答本身仍可能成功
            with contextlib.suppress(Exception):
                self.on_waiting()
        try:
            return wait_for_user_input(event, self.wait_seconds, cancel_event)
        finally:
            self.registry.release(self.task_id)
