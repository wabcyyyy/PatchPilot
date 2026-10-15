"""能力查询面(G16):engine/profile/arm/role/model/预算的真实有效值,一个出口可查。

"真实"= 每一项都从登记处现取(角色表、运行 profile 常量、请求 schema 的 Literal、
预算校验的规则),不在这里手抄一份会漂移的清单。这里的读数与受理层同源:查询面
说"能受理什么",受理层就拒绝什么之外的输入;两者一致由测试钉住
(tests/test_capabilities.py 的同源断言)。

one_shot 在这里是**登记未完成**而不是能力:它没有接线到 API 受理(评测 CLI 的
消融臂),如实标注 accepted_via,不写成 API 可用。
"""

from __future__ import annotations

from typing import Any

from app.agent.roles import ROLE_REGISTRY
from app.agent.runtime import PROFILE_LEGACY_V1, PROFILE_REPAIR_V2, RUNTIME_PROFILES

CAPABILITIES_SCHEMA_VERSION = 1

_PROFILE_NOTES = {
    PROFILE_LEGACY_V1: "旧 API 契约/回放/旧持久任务的既有口径;不做容量比较",
    PROFILE_REPAIR_V2: (
        "新产品入口的口径;要求显式 llm_max_tokens > 0 且声明容量装得下输出预留,"
        "不满足在受理处拒绝(输出上限要按已声明能力配置)"
    ),
}


def capabilities(settings: Any) -> dict[str, Any]:
    """构建能力读数;settings 传调用方的 Settings(与受理同源,不另取全局)。"""
    default_profile = str(getattr(settings, "agent_runtime_profile", "") or "")
    issues = (
        settings.validate_production_readiness()
        if hasattr(settings, "validate_production_readiness")
        else []
    )
    return {
        "schema_version": CAPABILITIES_SCHEMA_VERSION,
        "deployment_mode": getattr(settings, "deployment_mode", "development"),
        "production_readiness": {
            "ready": len(issues) == 0,
            "issues": issues,
        },
        "engines": {
            "graph": {
                "modes": ["repair", "diagnose_then_repair"],
                "notes": "issue-only 受理(diagnose_then_repair)只能走 graph",
            },
            "plain": {
                "modes": ["repair"],
                "notes": "diagnose_then_repair 在受理处显式拒绝(plain 不能等待验收批准)",
            },
        },
        "profiles": {
            name: {
                "default": name == default_profile,
                "notes": _PROFILE_NOTES.get(name, ""),
            }
            for name in RUNTIME_PROFILES
        },
        "arms": {
            "agent": {"accepted_via": ["api", "eval_cli"], "notes": ""},
            "one_shot": {
                "accepted_via": ["eval_cli"],
                "notes": (
                    "未接线到 API 受理(登记未完成):HTTP 面传 arm=one_shot 在 schema"
                    " 校验处 422 显式拒绝,不静默按 agent 跑"
                ),
            },
        },
        "roles": {
            name: {
                "description": spec.description,
                "tools": list(spec.tools),
                "isolated_workspace": spec.isolated_workspace,
            }
            for name, spec in ROLE_REGISTRY.items()
        },
        "models": {
            "fake": {
                "notes": "离线回放;必须提供 replay_script(题目目录或请求内联)",
            },
            "openai": {
                "notes": "真实端点;要求 llm_enabled 且凭据齐备,受理时前置校验",
            },
        },
        "budget": {
            "modes": ["tokens", "cost", "hybrid"],
            "rules": [
                "tokens:按 token_budget 限额",
                "cost/hybrid 在线任务要求 cost_budget_micro_usd > 0 且价目表登记了该模型;"
                "无配置在受理处明确拒绝,不静默变成无限预算(离线 fake 回放豁免)",
            ],
        },
        "defaults": {
            "engine": "graph",
            "model": "fake",
            "agent_profile": default_profile,
        },
    }
