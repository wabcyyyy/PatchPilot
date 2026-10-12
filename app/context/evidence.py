"""任务内证据存储(G05):工具回执的原始文本落盘 + 有界取回。

缺陷出处(SPEC §1 缺陷 4 / 评审 R4):`plain_loop.py` 把工具结果 `json.dumps` 之后交给
`fold_output`,而 JSON 里的源码换行是转义字符,整个回执通常就是**一行**——头尾折叠立刻
退化成"前 8000 字符硬切"(PROGRESS D.5、TODO M15 已核实记录)。后果有两层:

1. 切在 JSON 中间 ⇒ 模型拿到一段**结构损坏**的文本(缺尾括号/断掉的字符串),
   它只能猜或重查,而重查就是又一轮请求;
2. 被切掉的尾巴**从来没有落过盘** ⇒ 信息永久丢失,取证链里只剩"模型当时看到的前缀"。

本模块只负责第 2 层的一半(原始文本可取回),第 1 层(裁剪发生在序列化之前、
按数据结构裁)在 `app/tools/output_filter.py` 的 `bound_structure`。

边界(全部有对应用例):
- **id 不映射宿主路径**。证据 id 是随机 hex,落盘文件名就是 `<id>.json`,读回必须先
  命中本任务的 `index.jsonl`——所以"别的任务的 id"与"伪造路径"都只会得到 not found;
- **任务内作用域**。store 绑定一个 `root` + `task_id`,索引里每条都带 task_id,查询再比一次
  (同一 run_dir 被复用时也不会串任务);
- **上限明示**。单个工件超体积上限或总量超配额时**拒绝落盘**,并把"这份原始证据取不回"
  如实写进模型可见回执(`retrievable=false`),不做静默丢弃;
- **不含绝对路径**。`EvidenceRef.as_dict()` 只给 id/hash/字节数/行数/来源,不给宿主路径。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

EVIDENCE_SCHEMA_VERSION = 1
INDEX_FILENAME = "index.jsonl"

KIND_TOOL_PAYLOAD = "tool_payload"
KIND_STDOUT = "stdout"
KIND_STDERR = "stderr"
KIND_NOTE = "note"

# 游标 `<字节偏移>:<内容 hash 前缀>`:偏移决定从哪儿读,hash 前缀把游标**钉死在这一个
# 工件**上——把 A 证据的游标拿到 B 证据上用,当场拒绝,而不是从别的偏移读出半截现场。
_CURSOR_SEP = ":"
_HASH_PREFIX = 12


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encode_cursor(offset: int, sha256: str) -> str:
    return f"{max(0, int(offset))}{_CURSOR_SEP}{sha256[:_HASH_PREFIX]}"


def decode_cursor(cursor: str, sha256: str) -> int:
    """解析游标;格式不对或不属于本工件就 ValueError(调用方转成结构化失败,不抛给模型)。"""
    text = (cursor or "").strip()
    if not text:
        return 0
    offset_part, sep, hash_part = text.partition(_CURSOR_SEP)
    if not sep:
        raise ValueError(f"malformed cursor {cursor!r} (expected '<offset>:<hash>')")
    try:
        offset = int(offset_part)
    except ValueError:
        raise ValueError(f"cursor offset is not an integer: {offset_part!r}") from None
    if offset < 0:
        raise ValueError(f"cursor offset must be >= 0, got {offset}")
    if hash_part != sha256[:_HASH_PREFIX]:
        raise ValueError("cursor belongs to a different evidence item")
    return offset


@dataclass(frozen=True)
class EvidenceRef:
    """一次原始回执的**身份**,不是内容。"""

    evidence_id: str
    task_id: str
    source: str
    kind: str
    workspace_revision: int
    sha256: str
    raw_bytes: int
    raw_lines: int
    retrievable: bool
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        """模型可见/轨迹可见的那一份元数据:绝对路径与宿主目录一律不出现在这里。"""
        out: dict[str, object] = {
            "evidence_id": self.evidence_id,
            "source": self.source,
            "kind": self.kind,
            "workspace_revision": self.workspace_revision,
            "sha256": self.sha256[:16],
            "raw_bytes": self.raw_bytes,
            "raw_lines": self.raw_lines,
            "retrievable": self.retrievable,
        }
        if self.reason:
            out["reason"] = self.reason
        return out


@dataclass
class EvidencePage:
    """一次分页取回的读数(工具直接把它当 payload 发出去)。"""

    ref: EvidenceRef
    text: str
    offset: int
    returned_bytes: int
    next_cursor: str | None
    eof: bool
    line_aligned: bool


class EvidenceNotStored(Exception):
    """配额/体积拒绝落盘时携带 ref(retrievable=False),让调用方如实标注而不是假装存下了。"""

    def __init__(self, ref: EvidenceRef) -> None:
        super().__init__(ref.reason)
        self.ref = ref


class EvidenceStore:
    """一个任务(一个 run_dir 子树)的证据仓。线程安全。"""

    def __init__(
        self,
        root: Path,
        task_id: str,
        *,
        max_item_bytes: int = 2_000_000,
        max_total_bytes: int = 64_000_000,
        page_bytes: int = 4000,
    ) -> None:
        self.root = Path(root)
        self.task_id = task_id
        self.max_item_bytes = max(0, int(max_item_bytes))
        self.max_total_bytes = max(0, int(max_total_bytes))
        self.page_bytes = max(256, int(page_bytes))
        self._lock = threading.Lock()
        self._index: dict[str, dict[str, object]] | None = None
        self._used_bytes = 0

    # ---------- 索引 ----------

    @property
    def index_path(self) -> Path:
        return self.root / INDEX_FILENAME

    def _ensure_index(self) -> dict[str, dict[str, object]]:
        """首次使用时读回索引;崩溃恢复/新进程因此仍能取回旧证据。"""
        if self._index is not None:
            return self._index
        index: dict[str, dict[str, object]] = {}
        used = 0
        if self.index_path.is_file():
            for line in self.index_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 残行(写进程被杀在中间)不参与索引,原始文件仍留盘
                if isinstance(row, dict) and isinstance(row.get("evidence_id"), str):
                    index[row["evidence_id"]] = row
                    used += int(row.get("raw_bytes") or 0)
        self._index = index
        self._used_bytes = used
        return index

    # ---------- 写入 ----------

    def save(
        self,
        text: str,
        *,
        source: str,
        kind: str = KIND_TOOL_PAYLOAD,
        workspace_revision: int = 0,
    ) -> EvidenceRef:
        """落一份原始文本,返回身份。超上限时**不落盘**并抛 EvidenceNotStored。"""
        data = text.encode("utf-8", errors="replace")
        evidence_id = uuid.uuid4().hex
        sha256 = _sha256(data)
        lines = len(data.splitlines())
        if self.max_item_bytes and len(data) > self.max_item_bytes:
            raise EvidenceNotStored(
                EvidenceRef(
                    evidence_id="",
                    task_id=self.task_id,
                    source=source,
                    kind=kind,
                    workspace_revision=workspace_revision,
                    sha256=sha256,
                    raw_bytes=len(data),
                    raw_lines=lines,
                    retrievable=False,
                    reason=(
                        f"raw payload exceeds the per-item cap {self.max_item_bytes} bytes"
                        " (kept only the bounded excerpt)"
                    ),
                )
            )
        with self._lock:
            index = self._ensure_index()
            if self.max_total_bytes and self._used_bytes + len(data) > self.max_total_bytes:
                raise EvidenceNotStored(
                    EvidenceRef(
                        evidence_id="",
                        task_id=self.task_id,
                        source=source,
                        kind=kind,
                        workspace_revision=workspace_revision,
                        sha256=sha256,
                        raw_bytes=len(data),
                        raw_lines=lines,
                        retrievable=False,
                        reason=(
                            f"evidence store is full (used {self._used_bytes} of "
                            f"{self.max_total_bytes} bytes); this raw payload was not saved"
                        ),
                    )
                )
            self.root.mkdir(parents=True, exist_ok=True)
            target = self.root / f"{evidence_id}.json"
            tmp = self.root / f"{evidence_id}.json.part"
            # 原子落盘:半截工件比没有工件更糟(read_evidence 会取回一段坏 JSON 当证据)
            with tmp.open("wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
            row: dict[str, object] = {
                "schema_version": EVIDENCE_SCHEMA_VERSION,
                "evidence_id": evidence_id,
                "task_id": self.task_id,
                "source": source,
                "kind": kind,
                "workspace_revision": workspace_revision,
                "sha256": sha256,
                "raw_bytes": len(data),
                "raw_lines": lines,
                "file": target.name,
            }
            index[evidence_id] = row
            self._used_bytes += len(data)
            with self.index_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return EvidenceRef(
            evidence_id=evidence_id,
            task_id=self.task_id,
            source=source,
            kind=kind,
            workspace_revision=workspace_revision,
            sha256=sha256,
            raw_bytes=len(data),
            raw_lines=lines,
            retrievable=True,
        )

    # ---------- 取回 ----------

    def known(self, evidence_id: str) -> bool:
        with self._lock:
            return evidence_id in self._ensure_index()

    def read(self, evidence_id: str, *, cursor: str = "", limit_bytes: int = 0) -> EvidencePage:
        """按字节偏移分页读回。id 不在本任务索引里 = EvidenceLookupError(不外泄他任务内容)。"""
        evidence_id = (evidence_id or "").strip()
        if not evidence_id:
            raise EvidenceLookupError("evidence_id is empty")
        with self._lock:
            row = self._ensure_index().get(evidence_id)
        if row is None or row.get("task_id") != self.task_id:
            # 两种情形同一个读数:未知 id、以及别的任务递过来的 id。不给"存在但拒读"的
            # 旁证,否则任务间存在性本身就是可探测面。
            raise EvidenceLookupError(f"evidence {evidence_id!r} is not registered in this task")
        target = self.root / str(row["file"])
        if not target.is_file():
            raise EvidenceLookupError(f"evidence {evidence_id!r} is indexed but the file is gone")
        data = target.read_bytes()
        sha256 = str(row["sha256"])
        actual = _sha256(data)
        if actual != sha256:
            raise EvidenceLookupError(
                f"evidence {evidence_id!r} content hash mismatch (recorded {sha256[:_HASH_PREFIX]},"
                f" read {actual[:_HASH_PREFIX]})"
            )
        try:
            offset = decode_cursor(str(cursor), sha256)
        except ValueError as exc:
            # 游标问题与"读不到"是同一类失败:统一从 EvidenceLookupError 出,
            # 上层只需要处理一种异常,不会漏掉一条把 500 甩给 API 的路径
            raise EvidenceLookupError(str(exc)) from exc
        if offset > len(data):
            raise EvidenceLookupError(
                f"cursor offset {offset} is past the end of evidence {evidence_id!r} "
                f"({len(data)} bytes)"
            )
        budget = max(256, min(int(limit_bytes or self.page_bytes), self.page_bytes))
        window = data[offset : offset + budget]
        end = offset + len(window)
        line_aligned = True
        if end < len(data):
            nl = data.find(b"\n", end)
            # 只在**预算内**对齐到行尾:预算是给上下文兜底的硬上限,不能为了凑一个换行符
            # 把它悄悄放大(旧 fold_output 的 hard_cap 就是这么被"多留几行"顶穿的)。
            # 装不下的长行就切在中途,并由 line_aligned=false 明说。
            if nl != -1 and nl + 1 - offset <= budget:
                window = data[offset : nl + 1]
                end = nl + 1
            else:
                line_aligned = False
        eof = end >= len(data)
        ref = EvidenceRef(
            evidence_id=evidence_id,
            task_id=self.task_id,
            source=str(row.get("source", "")),
            kind=str(row.get("kind", KIND_TOOL_PAYLOAD)),
            workspace_revision=int(row.get("workspace_revision") or 0),
            sha256=sha256,
            raw_bytes=int(row.get("raw_bytes") or len(data)),
            raw_lines=int(row.get("raw_lines") or 0),
            retrievable=True,
        )
        return EvidencePage(
            ref=ref,
            text=window.decode("utf-8", errors="replace"),
            offset=offset,
            returned_bytes=len(window),
            next_cursor=None if eof else encode_cursor(end, sha256),
            eof=eof,
            line_aligned=line_aligned,
        )


class EvidenceLookupError(Exception):
    """取回失败:未知 id / 文件缺失 / hash 不符 / 游标非法。一律转成结构化失败给模型。"""


EVIDENCE_DIRNAME = "evidence"


def build_store(report_dir: Path, task_id: str) -> EvidenceStore:
    """按 run_dir 约定建仓:证据落在 `<run_dir>/evidence`,与 ledger_state.json 同源
    (`report_dir.parent` 就是 run_dir,见 app/graph/nodes.py 的账本恢复口径)。

    上限每次建仓时读一次 Settings;运行中改环境变量不会挪动已经建好的仓。
    """
    from app.config import get_settings

    settings = get_settings()
    return EvidenceStore(
        Path(report_dir).parent / EVIDENCE_DIRNAME,
        task_id,
        max_item_bytes=settings.evidence_max_item_bytes,
        max_total_bytes=settings.evidence_max_total_bytes,
        page_bytes=settings.evidence_page_bytes,
    )
