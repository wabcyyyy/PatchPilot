"""Fold stale or aged tool replies while preserving tool-call pairing and references."""

from __future__ import annotations

import json
import re

from app.context.task_memory import TaskMemory

_STATUS_SUFFIX = re.compile(
    r"\n\n\[evidence_status\]\n以下引用已失效，历史记忆仅代表改动前的观测："
    r"E\d+(?:,E\d+)*。查看当前代码必须重新调用检索工具。$"
)


def compact_evidence_messages(
    messages: list[dict[str, object]], memory: TaskMemory | None, *, keep_recent_turns: int
) -> list[dict[str, object]]:
    if memory is None or not memory.enabled:
        return messages
    entries = {item.ref: item for item in memory.evidence}
    total = sum(msg.get("role") == "assistant" for msg in messages)
    turn = 0
    out: list[dict[str, object]] = []
    for msg in messages:
        if msg.get("role") == "system" and isinstance(msg.get("content"), str):
            marker = "\n\n[evidence_status]"
            # Only replace our generated suffix; repository text may contain the marker.
            base = _STATUS_SUFFIX.sub("", str(msg["content"]))
            stale_refs = [item.ref for item in memory.evidence if item.stale]
            if stale_refs:
                base += (
                    marker + "\n以下引用已失效，历史记忆仅代表改动前的观测：" + ",".join(stale_refs)
                )
                base += "。查看当前代码必须重新调用检索工具。"
            out.append({**msg, "content": base})
            continue
        if msg.get("role") == "assistant":
            turn += 1
        if msg.get("role") != "tool" or not isinstance(msg.get("content"), str):
            out.append(msg)
            continue
        try:
            payload = json.loads(str(msg["content"]))
        except (ValueError, TypeError):
            out.append(msg)
            continue
        refs = payload.get("_memory_refs", []) if isinstance(payload, dict) else []
        observations = [entries[ref] for ref in refs if ref in entries]
        stale = any(item.stale for item in observations)
        aged = total - turn >= max(1, keep_recent_turns)
        if observations and (stale or aged):
            reason = (
                "该文件已被修改，旧观测已失效，如需查看最新代码请重新调用检索工具"
                if stale
                else (
                    "旧回合输出已折叠；原始内容可按 evidence_id 取回"
                    if all(item.retrievable for item in observations)
                    else "旧回合输出已折叠；原始内容未保存，请重新调用工具"
                )
            )
            folded = {
                "_memory_refs": refs,
                "is_stale": stale,
                "summary": "; ".join(f"[{item.ref}] {item.detail}" for item in observations),
                "evidence_ids": [item.evidence_id for item in observations if item.evidence_id],
                "note": reason,
            }
            out.append({**msg, "content": json.dumps(folded, ensure_ascii=False)})
        else:
            out.append(msg)
    return out
