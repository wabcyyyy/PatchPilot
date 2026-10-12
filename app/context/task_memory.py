"""任务记忆(G06):把"平台观测到的事实"与"模型提交的假设"分开登记。

缺陷出处(SPEC §1 缺陷 2 / 评审 R2):阶段之间只传**一层薄摘要**——`findings` 是 LOCALIZE
收尾时模型自己写的一段话,PROPOSE 拿到的是"上一阶段说了什么",而不是"平台观测到了什么"。
于是读过的行范围、打脸假设的反例、已经排除的方向都落在阶段边界外面,补丁阶段只能重新调查
(付费实跑 `runs/swe-hard-graph2/3`:同一题两次独立运行,PROPOSE 阶段 13 次 search / 10 次 read、
`apply_patch` 0 次,而 LOCALIZE 已经把这些文件读过一遍)。

三条立身规则:

1. **确定性优先**:证据条目由平台从真实工具回执自动登记(`record_receipt`),模型一句都不用写;
   模型只能通过受校验的 `update_memory` 提交**假设/排除方向**,并且永远拿不到
   `verified/confirmed` 这类平台专属状态 —— SPEC 要求"不得把推测写成已验证事实";
2. **事实与推断分开渲染**:给模型看的工作记忆块里,平台观测、平台判定、模型假设三个区分别是
   独立小节,模型的话不会伪装成事实出现在同一个列表里;
3. **失效要看得见**:每条证据带着登记时的 `workspace_revision`,工作区被写过就标 `stale`,
   渲染时明写"这是改动前的内容"——与 G05 的 `read_evidence` 同一个口径。

持久化走 `run_dir/memory_state.json`(原子写 + `MEMORY_SCHEMA_VERSION`),崩溃恢复读回,
旧任务没有这份文件时按"空记忆"继续,不假装迁移过。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MEMORY_SCHEMA_VERSION = 1
MEMORY_STATE_FILENAME = "memory_state.json"

SOURCE_PLATFORM = "platform"
SOURCE_MODEL = "model"

STATUS_HYPOTHESIS = "hypothesis"
STATUS_SUPPORTED = "supported"
STATUS_REFUTED = "refuted"
#: 模型允许提交的状态集合。**没有** verified/resolved:那些只能由验收链得出
MODEL_STATUSES = (STATUS_HYPOTHESIS, STATUS_SUPPORTED, STATUS_REFUTED)
#: 模型一旦写这些词就当场拒收(而不是"接受但降级",静默改写模型的话是不诚实的)
PLATFORM_ONLY_STATUSES = ("verified", "confirmed", "resolved", "proven")

MAX_TEXT_CHARS = 400  # 单条假设/排除项的字符上限:记忆块是给模型的,不是储物间
MAX_ITEMS_PER_KIND = 60  # 每个区的条数上限(渲染时再按总字符预算裁)


def _clip(text: str) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) > MAX_TEXT_CHARS:
        return cleaned[:MAX_TEXT_CHARS] + f"…({len(cleaned)} chars)"
    return cleaned


@dataclass
class MemoryEvidence:
    """一条**平台观测**:谁在哪个阶段用什么参数拿到了什么,原文在哪一代工作区。"""

    ref: str
    source: str
    stage: str
    round_no: int
    detail: str
    path: str = ""
    line_range: str = ""
    workspace_revision: int = 0
    evidence_id: str = ""
    sha256: str = ""
    retrievable: bool = False
    omitted_lines: int = 0
    verdict: str = ""  # 平台判定(仅 run_tests 等结构化结果),模型永远写不了这个字段
    stale: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MemoryHypothesis:
    hid: str
    text: str
    status: str
    support: list[str] = field(default_factory=list)
    counter: list[str] = field(default_factory=list)
    author: str = SOURCE_MODEL

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MemoryExclusion:
    xid: str
    text: str
    reason: str
    refs: list[str] = field(default_factory=list)
    author: str = SOURCE_MODEL

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class TaskMemory:
    """一个任务的工作记忆。线程安全(自适应分支的候选会并发写同一份记忆)。"""

    def __init__(self, task_id: str, session_id: str = "", *, enabled: bool = True) -> None:
        self.task_id = task_id
        self.session_id = session_id
        self.enabled = enabled
        self.version = 0
        self.goal = ""
        self.constraints: list[str] = []
        self.acceptance: list[str] = []
        self.evidence: list[MemoryEvidence] = []
        self.hypotheses: list[MemoryHypothesis] = []
        self.excluded: list[MemoryExclusion] = []
        self.plans: list[str] = []
        self.candidate: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._refs: set[str] = set()

    # ---------- 事实登记(平台侧) ----------

    def _new_ref(self, prefix: str) -> str:
        """引用号按区各自递增(E1/H1/X1 互不占号),但全局唯一——`update_memory` 要靠它核对引用。"""
        counters = {"E": len(self.evidence), "H": len(self.hypotheses), "X": len(self.excluded)}
        number = counters.get(prefix, len(self._refs)) + 1
        while f"{prefix}{number}" in self._refs:
            number += 1
        ref = f"{prefix}{number}"
        self._refs.add(ref)
        return ref

    def set_context(
        self,
        *,
        goal: str = "",
        constraints: list[str] | None = None,
        acceptance: list[str] | None = None,
    ) -> None:
        """受理时冻结的任务上下文:目标、平台硬约束、验收清单引用。

        **真的变了才升版本**。恢复路径会再调一次这个方法(同一份冻结输入),如果它无条件
        `version += 1`,读回来的记忆就和崩溃前那一份 hash 不同 ⇒ 绑定校验把自己的恢复判成
        mismatch,任务被错误地收敛成待审(实测 2 条 resume 用例当场红)。
        """
        with self._lock:
            changed = False
            if goal and self.goal != _clip(goal):
                self.goal = _clip(goal)
                changed = True
            if constraints is not None and self.constraints != [_clip(c) for c in constraints]:
                self.constraints = [_clip(c) for c in constraints]
                changed = True
            if acceptance is not None and self.acceptance != [_clip(a) for a in acceptance]:
                self.acceptance = [_clip(a) for a in acceptance]
                changed = True
            if changed:
                self.version += 1

    def record_evidence(
        self,
        *,
        source: str,
        stage: str,
        round_no: int,
        detail: str,
        path: str = "",
        line_range: str = "",
        workspace_revision: int = 0,
        evidence_id: str = "",
        sha256: str = "",
        retrievable: bool = False,
        omitted_lines: int = 0,
        verdict: str = "",
    ) -> MemoryEvidence:
        with self._lock:
            entry = MemoryEvidence(
                ref=self._new_ref("E"),
                source=_clip(source),
                stage=_clip(stage),
                round_no=round_no,
                detail=_clip(detail),
                path=_clip(path),
                line_range=_clip(line_range),
                workspace_revision=workspace_revision,
                evidence_id=evidence_id,
                sha256=sha256[:16],
                retrievable=retrievable,
                omitted_lines=omitted_lines,
                verdict=_clip(verdict),
            )
            self.evidence.append(entry)
            self.version += 1
            return entry

    def mark_stale(self, *, from_revision: int) -> int:
        """工作区从 `from_revision` 往前过的观测全部标失效(仍可引用,不可当现状)。"""
        marked = 0
        with self._lock:
            for entry in self.evidence:
                if not entry.stale and 0 <= entry.workspace_revision < from_revision:
                    entry.stale = True
                    marked += 1
            if marked:
                self.version += 1
        return marked

    def record_candidate(
        self, *, candidate_id: str, candidate_hash: str, workspace_revision: int
    ) -> None:
        with self._lock:
            self.candidate = {
                "candidate_id": candidate_id,
                "candidate_hash": candidate_hash[:16],
                "workspace_revision": workspace_revision,
            }
            self.version += 1

    def record_plan(self, plan_text: str, *, degraded: bool = False) -> int:
        """计划版本进记忆(计划是工件,不是聊天)。返回 1-based 版本号。"""
        with self._lock:
            self.plans.append(("degraded:" if degraded else "") + _clip(plan_text))
            self.version += 1
            return len(self.plans)

    # ---------- 模型通道(受校验) ----------

    def model_update(self, payload: dict[str, Any]) -> tuple[list[str], list[str]]:
        """`update_memory` 的落点。返回 (被接受的条目引用, 被拒绝的原因)。

        校验三条:文本非空且带理由;状态只许 `hypothesis|supported|refuted`;引用的 ref 必须是
        记忆里真实存在的观测。**平台专属状态直接点名拒绝** —— 静默降级等于替模型改写它的说法。
        """
        applied: list[str] = []
        rejected: list[str] = []
        if not isinstance(payload, dict):
            return [], ["payload must be an object"]
        with self._lock:
            for item in payload.get("hypotheses") or []:
                if not isinstance(item, dict):
                    rejected.append("hypothesis entry is not an object")
                    continue
                text = _clip(item.get("text", ""))
                status = str(item.get("status") or STATUS_HYPOTHESIS).strip().lower()
                if not text:
                    rejected.append("hypothesis without text")
                    continue
                if status in PLATFORM_ONLY_STATUSES:
                    rejected.append(
                        f"status {status!r} is platform-only: 只有验收链能判定修复,模型只能提假设"
                    )
                    continue
                if status not in MODEL_STATUSES:
                    rejected.append(f"unknown hypothesis status {status!r}")
                    continue
                refs = [str(r) for r in (item.get("refs") or [])]
                unknown = [r for r in refs if r not in self._refs]
                if unknown:
                    rejected.append(f"hypothesis {text[:30]!r} 引用了不存在的证据 {unknown}")
                    continue
                if status != STATUS_HYPOTHESIS and not refs:
                    rejected.append(
                        f"status {status!r} 必须带上支撑/反例的证据引用(否则它就是空话)"
                    )
                    continue
                entry = MemoryHypothesis(
                    hid=self._new_ref("H"),
                    text=text,
                    status=status,
                    support=[r for r in refs if status != STATUS_REFUTED],
                    counter=[r for r in refs if status == STATUS_REFUTED],
                )
                self.hypotheses.append(entry)
                applied.append(entry.hid)
            for item in payload.get("excluded") or []:
                if not isinstance(item, dict):
                    rejected.append("excluded entry is not an object")
                    continue
                text = _clip(item.get("text", ""))
                reason = _clip(item.get("reason", ""))
                if not text or not reason:
                    rejected.append(f"excluded {text[:30]!r} 需要 text 与 reason 两项")
                    continue
                entry = MemoryExclusion(
                    xid=self._new_ref("X"),
                    text=text,
                    reason=reason,
                    refs=[str(r) for r in (item.get("refs") or []) if str(r) in self._refs],
                )
                self.excluded.append(entry)
                applied.append(entry.xid)
            if applied:
                self.version += 1
        return applied, rejected

    # ---------- 渲染 ----------

    def render(self, *, max_chars: int, current_revision: int = 0) -> str:
        """给模型看的工作记忆块。三区分开:平台说的和模型说的不混排。

        预算从**最新**条目开始保留(调查越近的内容越有用),被裁掉的条数如实写出;
        `max_chars <= 0` 或记忆关闭 = 不出这块。
        """
        if not self.enabled or max_chars <= 0:
            return ""
        # **还没有任何可带的内容时不出块**:只有约束/验收清单不算"观测"。
        # 这条让"接入记忆"在任务开局时与接入前逐字节相同(阶段装配面的回归钉子
        # 见 tests/test_repo_map.py::test_disabled_is_byte_identical_to_pre_change)。
        if not (self.evidence or self.hypotheses or self.excluded or self.plans):
            return ""
        head = [
            f"### 工作记忆 v{self.version}(会话 {self.session_id or '未绑定'})"
            "『观测/判定』由平台从真实工具回执登记,『假设/已排除』由模型提交",
        ]
        if self.constraints:
            head.append("平台硬约束:" + " | ".join(self.constraints))
        if self.acceptance:
            head.append(
                f"验收清单({len(self.acceptance)} 个测试身份):" + "、".join(self.acceptance[:8])
            )
        if self.candidate:
            head.append(
                f"当前候选:{self.candidate.get('candidate_id', '')} "
                f"hash={self.candidate.get('candidate_hash', '')} "
                f"rev={self.candidate.get('workspace_revision', 0)}"
            )
        if self.plans:
            head.append(f"计划 v{len(self.plans)}:{self.plans[-1]}")
        if current_revision:
            head.append(f"(工作区已写入 {current_revision} 代;标『已失效』的观测不是现状)")

        # (区标题, 行列表, 是否优先保留) —— 判定与假设条数少、价值高,先留;观测最新优先
        sections: list[tuple[str, list[str]]] = []
        verdicts = [
            f"- [{e.ref}] {e.stage} r{e.round_no} 判定 {e.verdict}"
            for e in self.evidence
            if e.verdict
        ]
        if verdicts:
            sections.append(("平台判定:", verdicts[-12:]))
        hypo = [
            f"- [{h.hid}]({h.status}) {h.text}"
            + (f" 支撑 {','.join(h.support)}" if h.support else "")
            + (f" 反例 {','.join(h.counter)}" if h.counter else "")
            for h in self.hypotheses
        ]
        if hypo:
            sections.append(("模型假设(未经平台验证):", hypo[-12:]))
        excl = [f"- [{x.xid}] {x.text} ← {x.reason}" for x in self.excluded]
        if excl:
            sections.append(("已排除方向:", excl[-8:]))
        obs = [
            f"- [{e.ref}] {e.stage} r{e.round_no} {e.detail}"
            + (f" 行 {e.line_range}" if e.line_range else "")
            + (f" [已失效:rev{e.workspace_revision} 改动前]" if e.stale else "")
            + (
                f' 原文可取回 read_evidence("{e.evidence_id}")'
                if e.evidence_id and e.retrievable
                else ""
            )
            for e in self.evidence
            if not e.verdict  # 带判定的那条已在"平台判定"区,不重复占预算
        ]
        if obs:
            sections.append(("平台观测(最近在前):", list(reversed(obs))))

        body: list[str] = []
        used = sum(len(line) + 1 for line in head)
        # 给"另有 N 条未显示"那行留位置:否则预算切刀会把这条自我说明切掉,
        # 记忆块就从"明说少给了"变成"看起来完整"——那正是本卡要堵的假陈述。
        budget = max(0, max_chars - used - 72)
        for title, items in sections:
            if not items:
                continue
            prefix_len = len(title) + 1
            if prefix_len >= budget:
                continue
            kept: list[str] = []
            for line in items:
                if len(line) + 1 > budget - prefix_len:
                    break
                kept.append(line)
                prefix_len += len(line) + 1
            omitted = len(items) - len(kept)
            block = [title, *kept]
            if omitted:
                block.append(f"- …另有 {omitted} 条未显示(记忆块预算 {max_chars} 字符)")
            body.extend(block)
            budget -= sum(len(line) + 1 for line in block)
        return "\n".join([*head, *body])[:max_chars]

    # ---------- 序列化与身份 ----------

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": MEMORY_SCHEMA_VERSION,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "version": self.version,
            "goal": self.goal,
            "constraints": list(self.constraints),
            "acceptance": list(self.acceptance),
            "candidate": dict(self.candidate),
            "plans": list(self.plans),
            "evidence": [e.as_dict() for e in self.evidence[-MAX_ITEMS_PER_KIND:]],
            "hypotheses": [h.as_dict() for h in self.hypotheses[-MAX_ITEMS_PER_KIND:]],
            "excluded": [x.as_dict() for x in self.excluded[-MAX_ITEMS_PER_KIND:]],
        }

    def state_hash(self) -> str:
        """记忆内容的确定性 hash:快照/恢复拿它判断"续跑的这次看到的还是同一份记忆吗"。"""
        text = json.dumps(self.snapshot(), ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @classmethod
    def from_snapshot(cls, raw: dict[str, Any]) -> TaskMemory | None:
        """读回;结构不符即返回 None(由调用方按"没有记忆"继续,绝不猜着升版)。"""
        if not isinstance(raw, dict) or raw.get("schema_version") != MEMORY_SCHEMA_VERSION:
            return None
        memory = cls(str(raw.get("task_id", "")), str(raw.get("session_id", "")))
        memory.version = int(raw.get("version", 0))
        memory.goal = str(raw.get("goal", ""))
        memory.constraints = [str(x) for x in (raw.get("constraints") or [])]
        memory.acceptance = [str(x) for x in (raw.get("acceptance") or [])]
        candidate = raw.get("candidate")
        memory.candidate = dict(candidate) if isinstance(candidate, dict) else {}
        memory.plans = [str(x) for x in (raw.get("plans") or [])]
        for row in raw.get("evidence") or []:
            if isinstance(row, dict):
                with contextlib.suppress(TypeError):  # 少一个字段就丢一条,不整份作废
                    memory.evidence.append(MemoryEvidence(**row))
        for row in raw.get("hypotheses") or []:
            if isinstance(row, dict):
                with contextlib.suppress(TypeError):
                    memory.hypotheses.append(MemoryHypothesis(**row))
        for row in raw.get("excluded") or []:
            if isinstance(row, dict):
                with contextlib.suppress(TypeError):
                    memory.excluded.append(MemoryExclusion(**row))
        memory.rebuild_refs()
        return memory

    def rebuild_refs(self) -> None:
        """从已读回的条目重建引用集合(新进程的索引必须与旧条目一致,否则模型引用会被误拒)。"""
        self._refs = (
            {e.ref for e in self.evidence}
            | {h.hid for h in self.hypotheses}
            | {x.xid for x in self.excluded}
        )


def memory_state_path(run_dir: Path) -> Path:
    return Path(run_dir) / MEMORY_STATE_FILENAME


def save_memory_state(run_dir: Path, memory: TaskMemory) -> None:
    """原子落盘。失败只记日志:记忆是加速器,写失败不得把一次运行搞失败。"""
    path = memory_state_path(run_dir)
    tmp = path.with_name(path.name + ".tmp")
    try:
        text = json.dumps(memory.snapshot(), ensure_ascii=False, default=str)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:
        log.warning("task %s: memory state write failed (%s)", memory.task_id, exc)
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def load_memory_state(run_dir: Path) -> TaskMemory | None:
    path = memory_state_path(run_dir)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("memory state unreadable at %s (%s)", path, exc)
        return None
    memory = TaskMemory.from_snapshot(raw)
    if memory is None:
        log.warning("memory state at %s has an unknown schema version; ignoring", path)
    return memory
