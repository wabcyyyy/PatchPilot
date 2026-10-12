"""G13:轻量角色委派——三角色定义(SPEC §G13)。

子任务不是"同一个 Agent 的另一个会话":每个角色有一套冻结的工具能力,
与主循环共享注册表里的工具,但装配面不同。

- researcher:只读调查 + 受控诊断(检索/读文件/跑复现与诊断命令/测试集),不改工作区;
- reviewer:只读冻结候选 + 跑测试集,指出风险与补充检查,不写工作区;
- repair_candidate:在自己的隔离工作区修改并验证(有 apply_patch,无 ask_user)。

三个角色都不含 delegate 与 ask_user:子任务不能再发起子任务(递归深度恒为 1),
也不能停下来等真人(等待语义只属于主线,见 G11)。这是"只读角色越权拒绝"的声明面:
工具白名单由这里冻结,子任务循环按它装配 schema 并在执行前挡人。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.tools.registry import FINISH_TOOL

#: researcher:只读 + 受控诊断(与主流程 LOCALIZE 的诊断面同族,但不含 ask_user)
RESEARCHER_TOOLS: tuple[str, ...] = (
    "list_files",
    "search_code",
    "find_symbol",
    "describe_file",
    "read_file",
    "read_evidence",
    "update_memory",
    "git_diff",
    "run_tests",
    "write_repro",
    "run_command",
    FINISH_TOOL,
)

#: reviewer:只读候选 + 测试集,不含任何写工作区/命令面
REVIEWER_TOOLS: tuple[str, ...] = (
    "list_files",
    "search_code",
    "find_symbol",
    "describe_file",
    "read_file",
    "read_evidence",
    "update_memory",
    "git_diff",
    "run_tests",
    FINISH_TOOL,
)

#: repair_candidate:在自己的工作区修改并验证(WRITE_TOOLS 去 ask_user/delegate)
REPAIR_CANDIDATE_TOOLS: tuple[str, ...] = (
    "list_files",
    "search_code",
    "find_symbol",
    "describe_file",
    "read_file",
    "read_evidence",
    "update_memory",
    "git_diff",
    "apply_patch",
    "run_tests",
    "write_repro",
    "run_command",
    FINISH_TOOL,
)


@dataclass(frozen=True)
class RoleSpec:
    """一个委派角色的冻结定义:描述、能力表、是否在隔离工作区运行。"""

    name: str
    description: str
    tools: tuple[str, ...]
    # True = 子任务自己物化仓库、改自己的工作区(repair_candidate);
    # False = 复用父工作区但只有只读/诊断能力。
    isolated_workspace: bool


ROLE_REGISTRY: dict[str, RoleSpec] = {
    "researcher": RoleSpec(
        name="researcher",
        description=(
            "只读调查与受控诊断:检索代码、读文件、跑复现/诊断命令与测试集,"
            "绝不修改工作区。用于替主线确认根因、补证据。"
        ),
        tools=RESEARCHER_TOOLS,
        isolated_workspace=False,
    ),
    "reviewer": RoleSpec(
        name="reviewer",
        description=(
            "评审冻结候选:只读候选差异与证据,跑测试集,指出风险与补充检查。"
            "意见只供主线参考,不能覆盖平台验收。"
        ),
        tools=REVIEWER_TOOLS,
        isolated_workspace=False,
    ),
    "repair_candidate": RoleSpec(
        name="repair_candidate",
        description=(
            "在自己的隔离工作区修改候选并验证:可以 apply_patch 与跑测试,产出候选 diff 与验证结论。"
        ),
        tools=REPAIR_CANDIDATE_TOOLS,
        isolated_workspace=True,
    ),
}


def role_tools(role: str) -> list[str]:
    """取一个角色的工具白名单;未登记角色当场点名(装配前失败,不静默放空)。"""
    spec = ROLE_REGISTRY.get(role)
    if spec is None:
        raise KeyError(f"unknown delegate role: {role}")
    return list(spec.tools)
