"""阶段上下文装配(G06):骨架 + 工作记忆 + 规则 → 这一阶段的 system 附加文本,每阶段只装配一次。

要防的缺陷是**叠加**:同一个任务里 SYSTEM_PROMPT 只有一条,但阶段附加上下文
(仓库骨架、记忆块)过去由各节点各自拼字符串。将来再加规则源(G08 的项目说明)就会长成
"每进一个阶段追加一段",模型在第 N 轮看到同一份说明被抄了 N 遍——那是纯成本,不是信息。

`assemble_stage` 因此是**幂等**的:同一 (阶段, 轮次) 第二次装配直接复用第一次的成品,
并把会话身份(能力集合、记忆版本)在同一处记下。可见面 == 执行面这条 G01 立的纪律,
在这里的对应物是"**装配面 == 记忆面**":装配进 system 的记忆版本就是会话登记的那一个。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.agent.session import SessionState
    from app.context.task_memory import TaskMemory


@dataclass(frozen=True)
class StageContext:
    """装配读数:`text` 是给 run_plain_loop 的 extra_system,其余是取证字段。"""

    text: str
    stage: str
    round_no: int
    memory_version: int
    memory_chars: int
    skeleton_chars: int
    reused: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "round_no": self.round_no,
            "memory_version": self.memory_version,
            "memory_chars": self.memory_chars,
            "skeleton_chars": self.skeleton_chars,
            "reused": self.reused,
        }


class ContextAssembler:
    """按 (阶段, 轮次) 缓存装配结果;跨轮次允许重装配(失败轮回 PLAN 修订时记忆确实变了)。"""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, int], StageContext] = {}

    def assemble(
        self,
        *,
        stage: str,
        round_no: int,
        session: SessionState | None,
        memory: TaskMemory | None,
        skeleton: str = "",
        max_chars: int = 3000,
        current_revision: int = 0,
        allowed_tools: list[str] | None = None,
    ) -> StageContext:
        key = (stage, round_no)
        cached = self._cache.get(key)
        if cached is not None:
            # 复用要**说出来**:同一阶段第二次装配没有重渲染记忆,这是"不叠加"的证据,
            # 也是一个调用方需要知道的读数(否则它以为自己拿到了新装配)
            return replace(cached, reused=True)

        memory_block = (
            memory.render(max_chars=max_chars, current_revision=current_revision) if memory else ""
        )
        parts = [block for block in (skeleton, memory_block) if block]
        context = StageContext(
            text="\n\n".join(parts),
            stage=stage,
            round_no=round_no,
            memory_version=memory.version if memory else 0,
            memory_chars=len(memory_block),
            skeleton_chars=len(skeleton),
            reused=False,
        )
        self._cache[key] = context
        if session is not None:
            session.begin_stage(
                stage,
                round_no=round_no,
                allowed_tools=list(allowed_tools or []),
                memory_version=context.memory_version,
            )
        return context

    def invalidate(self, stage: str, *, from_round: int) -> None:
        """失败轮回同一阶段重来时,旧轮次的装配结果必须作废(否则带着旧记忆版本重放)。"""
        for key in [k for k in self._cache if k[0] == stage and k[1] >= from_round]:
            self._cache.pop(key, None)

    def readings(self) -> list[dict[str, object]]:
        return [context.as_dict() for context in self._cache.values()]
