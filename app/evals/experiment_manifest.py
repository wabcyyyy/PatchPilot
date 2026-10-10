"""实验 manifest(SPEC §7 G04):"允许哪些变量、其余必须相同"的可核对登记。

缺陷现场(审查 R5 / 本卡 §2 第 5 条):比较器只比它自己挑的几列,配置漂移(窗口、
工具上限、thinking、价目)看不见;`latest_per_bug` 还会把同一题的多次重复只留一条,
重复之间的方差就被抹平成"一次成功的证据"。付费驱动器又把 Q0 的判据写死在代码里,
新实验不显式换判据就会**继承上一批的停手条件**。

manifest 是这三件事的共同解:它把题单、重复数、臂与登记变量、额度、停手条件、
授权与产物目录写成一份可 hash 的文件;驱动器只认它,比较器拿它核对控制身份。
旧批次与旧预登记文档只读保留,不被本模块改写。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.task_spec import stable_hash

MANIFEST_SCHEMA_VERSION = 1

# 顶层必填字段:少任何一个都无法判定"这批要回答什么、花多少、谁批准的"
REQUIRED_FIELDS: tuple[str, ...] = (
    "manifest_id",
    "question",
    "engine",
    "model",
    "arms",
    "bugs",
    "repetitions",
    "budget",
    "stop_conditions",
    "authorization",
    "outputs_root",
)
OPTIONAL_FIELDS: tuple[str, ...] = (
    "schema_version",
    "notes",
    "registered_upgrades",
    "source_doc",
    "created_at",
)

# 额度与环境类键:它们是**控制身份**,不是登记变量的合法落点
CONTROL_BUDGET_KEYS: tuple[str, ...] = (
    "budget_mode",
    "token_budget",
    "cost_budget_micro_usd",
    "max_model_calls",
    "model_context_tokens",
)


class ManifestError(ValueError):
    """manifest 不合法:拒绝启动,不带病跑一批付费请求。"""


@dataclass(frozen=True)
class Arm:
    """一条臂 = 臂名 + **唯一允许**的登记变量(env 键值对)。

    变量必须写在臂上而不是"跑之前再 export":没登记的差异正是过去作废过一次
    付费实验的原因。
    """

    name: str
    variables: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "variables": dict(self.variables)}


@dataclass(frozen=True)
class ExperimentManifest:
    manifest_id: str
    question: str
    engine: str
    model: dict[str, Any]
    arms: list[Arm]
    bugs: list[str]
    repetitions: int
    budget: dict[str, Any]
    stop_conditions: list[str]
    authorization: dict[str, Any]
    outputs_root: str
    notes: str = ""
    registered_upgrades: list[str] = field(default_factory=list)
    source_doc: str = ""
    created_at: str = ""
    schema_version: int = MANIFEST_SCHEMA_VERSION

    # ---------- 身份 ----------

    def content_dict(self) -> dict[str, Any]:
        """参与 hash 的内容(created_at 不参与:同一份计划重复导出必须同 hash)。"""
        return {
            "schema_version": self.schema_version,
            "manifest_id": self.manifest_id,
            "question": self.question,
            "engine": self.engine,
            "model": self.model,
            "arms": [a.as_dict() for a in self.arms],
            "bugs": list(self.bugs),
            "repetitions": self.repetitions,
            "budget": self.budget,
            "stop_conditions": list(self.stop_conditions),
            "authorization": self.authorization,
            "outputs_root": self.outputs_root,
            "registered_upgrades": list(self.registered_upgrades),
            "source_doc": self.source_doc,
        }

    @property
    def manifest_hash(self) -> str:
        return stable_hash(self.content_dict())

    def arm_names(self) -> list[str]:
        return [arm.name for arm in self.arms]

    def control_identity(self, arm: Arm) -> dict[str, Any]:
        """剔除该臂登记变量之后的控制身份:两臂比较时**必须逐字段相同**。

        协议升级(B0/G01 之类)不是"环境差异",它改变了运行本身 —— 想跨升级比较就必须
        把它登记成变量(registered_upgrades),否则源码差异会被读成"同一条件下的两次结果"。
        """
        identity = dict(self.content_dict())
        identity["arms"] = None  # 臂与变量单独核对,不进控制身份
        identity["registered_upgrades"] = sorted(self.registered_upgrades)
        identity["budget"] = {k: self.budget.get(k) for k in CONTROL_BUDGET_KEYS}
        return {
            "manifest_hash": self.manifest_hash,
            "engine": self.engine,
            "model": self.model,
            "bugs": sorted(self.bugs),
            "repetitions": self.repetitions,
            "budget": identity["budget"],
            "registered_upgrades": identity["registered_upgrades"],
            "arm_variables": dict(arm.variables),
        }

    # ---------- 运行计划(dry-run 与真实启动共用) ----------

    def run_matrix(self) -> list[dict[str, Any]]:
        """按 (bug, arm, repetition) 展开全部运行;顺序即执行顺序,可复核。"""
        return [
            {
                "bug": bug,
                "arm": arm.name,
                "repetition": rep,
                "variables": dict(arm.variables),
                "control_identity": self.control_identity(arm),
            }
            for arm in self.arms
            for bug in self.bugs
            for rep in range(1, self.repetitions + 1)
        ]

    def budget_envelope(self) -> dict[str, Any]:
        """额度包:这批**最多**发多少次调用、花多少 token/钱,以及哈希锚点。"""
        runs = len(self.arms) * len(self.bugs) * self.repetitions
        return {
            "planned_runs": runs,
            "per_run_limits": {k: self.budget.get(k) for k in CONTROL_BUDGET_KEYS},
            "batch_cap_tokens": self.budget.get("batch_cap_tokens"),
            "batch_cap_cost_micro_usd": self.budget.get("batch_cap_cost_micro_usd"),
            "stop_conditions": list(self.stop_conditions),
            "manifest_hash": self.manifest_hash,
            # 比较时要用哪几节控制身份:报告里带着它,事后能对账"当时比的是什么"
            "control_keys": sorted(self.control_identity(self.arms[0])),
        }


# ---------- 读写与校验 ----------


def validate(raw: dict[str, Any]) -> ExperimentManifest:
    """结构 + 语义校验;任何一条不满足都拒绝(宁可不跑,不跑带病的批)。"""
    if not isinstance(raw, dict):
        raise ManifestError("manifest 顶层必须是对象")
    missing = [key for key in REQUIRED_FIELDS if key not in raw]
    if missing:
        raise ManifestError(f"manifest 缺少必填字段:{missing}")
    unknown = sorted(set(raw) - set(REQUIRED_FIELDS) - set(OPTIONAL_FIELDS))
    if unknown:
        raise ManifestError(f"manifest 有未登记字段:{unknown}(新字段要先进 REQUIRED/OPTIONAL)")

    version = int(raw.get("schema_version", MANIFEST_SCHEMA_VERSION))
    if version != MANIFEST_SCHEMA_VERSION:
        raise ManifestError(f"unsupported manifest schema_version={version}")

    arms_raw = raw["arms"]
    if not isinstance(arms_raw, list) or not arms_raw:
        raise ManifestError("manifest.arms 必须是非空列表")
    arms: list[Arm] = []
    for entry in arms_raw:
        if not isinstance(entry, dict) or "name" not in entry:
            raise ManifestError(f"arm 条目缺 name:{entry!r}")
        variables = dict(entry.get("variables") or {})
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in variables.items()):
            raise ManifestError(f"arm {entry['name']} 的登记变量必须是字符串键值")
        arms.append(Arm(name=str(entry["name"]), variables=variables))
    names = [arm.name for arm in arms]
    if len(set(names)) != len(names):
        raise ManifestError(f"臂名重复:{names}")

    bugs = [str(b) for b in raw["bugs"]]
    if not bugs:
        raise ManifestError("manifest.bugs 不能为空(空题单的批不会产出任何证据)")
    if len(set(bugs)) != len(bugs):
        raise ManifestError(f"题单有重复条目:{bugs}")

    repetitions = int(raw["repetitions"])
    if repetitions < 1:
        raise ManifestError(f"repetitions 必须 >= 1,实际 {repetitions}")

    budget = dict(raw["budget"])
    if str(budget.get("budget_mode", "tokens")) not in {"tokens", "cost", "hybrid"}:
        raise ManifestError(f"未知 budget_mode:{budget.get('budget_mode')!r}")

    authorization = dict(raw["authorization"])
    online = str(raw["model"].get("provider", "")) == "openai"
    if online:
        # 真实调用必须写清谁批的、批到什么程度;缺任何一项都拒绝(不猜额度)
        for key in ("granted_by", "scope"):
            if not str(authorization.get(key, "")).strip():
                raise ManifestError(
                    f"在线批的 manifest 必须登记 authorization.{key}"
                    "(SPEC §7 G17:本文件不授权任何真实调用)"
                )
        if int(authorization.get("max_cost_micro_usd") or 0) <= 0:
            raise ManifestError("在线批必须登记 authorization.max_cost_micro_usd(>0)")

    stop_conditions = [str(s) for s in raw["stop_conditions"]]
    if not stop_conditions:
        raise ManifestError("manifest.stop_conditions 不能为空:没有停手条件的批不许启动")

    return ExperimentManifest(
        manifest_id=str(raw["manifest_id"]),
        question=str(raw["question"]),
        engine=str(raw["engine"]),
        model=dict(raw["model"]),
        arms=arms,
        bugs=bugs,
        repetitions=repetitions,
        budget=budget,
        stop_conditions=stop_conditions,
        authorization=authorization,
        outputs_root=str(raw["outputs_root"]),
        notes=str(raw.get("notes", "")),
        registered_upgrades=[str(u) for u in raw.get("registered_upgrades") or []],
        source_doc=str(raw.get("source_doc", "")),
        created_at=str(raw.get("created_at", "")),
        schema_version=version,
    )


def read_manifest(path: Path | str) -> ExperimentManifest:
    text = Path(path).read_text(encoding="utf-8")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest 不是合法 JSON:{exc}") from exc
    return validate(raw)


def write_manifest(path: Path | str, manifest: ExperimentManifest) -> str:
    """原子写 manifest,返回它的 hash(同一份内容重复写 hash 不变 → 幂等)。"""
    payload = {
        **manifest.content_dict(),
        "created_at": manifest.created_at,
        "notes": manifest.notes,
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)
    return manifest.manifest_hash
