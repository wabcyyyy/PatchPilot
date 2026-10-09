"""实验策略(S10a/F8):同引擎两臂的唯一差异登记面。

缺陷现场(review F8,P1):消融两臂用不同引擎(graph vs plain)——同一个配置值
不能保证两臂消费同一机制;所谓"同一条判定路径"只在 run_task 的执行体插槽内成立,
跨引擎对照混进了 LOCALIZE 份额/PLAN/仓库骨架/verify 双跑等未登记变量。

本模块把"待研究机制"收敛为**显式登记的策略对象**,graph 两臂共用同一条
LOCALIZE→PLAN→PROPOSE→VERIFY 状态机、同一套门禁与最终验收,只差策略字段:

- `agent`(默认):完整执行反馈 + 跨轮重试 + 自适应分支(与既有行为逐字一致);
- `one_shot`:PROPOSE 阶段工具集去掉 run_tests/reset_workspace(模型得不到执行
  反馈),**第一次候选验证失败即终局**(rollback 节点按策略停止,不进入反馈重试),
  分支禁用。one_shot **不是**"只发一次 API 请求":检索与补丁协议自纠仍然允许
  ——区别按工具/状态约束定义,不是按请求次数。

登记纪律:两臂的 manifest/provenance 必须携带策略名与工具白名单;
未在登记差异表里的任何差异(S10b 的 compare_experiments)直接报错。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.graph.nodes import WRITE_TOOLS

# one_shot 的 PROPOSE 工具集 = agent 臂去掉执行反馈通道(run_tests)与
# 反馈重试依赖(reset_workspace);检索/读取/补丁协议自纠保留。
ONE_SHOT_PROPOSE_TOOLS: tuple[str, ...] = tuple(
    tool for tool in WRITE_TOOLS if tool not in {"run_tests", "reset_workspace"}
)


@dataclass(frozen=True)
class ExperimentPolicy:
    """一次实验的两臂差异登记对象(其余一切机制必须相同)。"""

    name: str  # agent | one_shot
    propose_tools: tuple[str, ...]
    allow_retry_after_verify_failure: bool
    allow_branching: bool

    @property
    def is_one_shot(self) -> bool:
        return self.name == "one_shot"

    def describe(self) -> dict[str, object]:
        """进 manifest/provenance 的登记形态(工具白名单全量列出)。"""
        return {
            "policy": self.name,
            "propose_tools": list(self.propose_tools),
            "allow_retry_after_verify_failure": self.allow_retry_after_verify_failure,
            "allow_branching": self.allow_branching,
        }


def agent_policy() -> ExperimentPolicy:
    """默认臂:与引入本模块之前的 graph 行为逐字一致。"""
    return ExperimentPolicy(
        name="agent",
        propose_tools=tuple(WRITE_TOOLS),
        allow_retry_after_verify_failure=True,
        allow_branching=True,
    )


def one_shot_policy() -> ExperimentPolicy:
    """对照臂:同引擎同验收,只去掉执行反馈与跨轮重试(S10a 登记变量)。"""
    return ExperimentPolicy(
        name="one_shot",
        propose_tools=ONE_SHOT_PROPOSE_TOOLS,
        allow_retry_after_verify_failure=False,
        allow_branching=False,
    )


def policy_for(arm: str) -> ExperimentPolicy:
    """arm 字符串 → 策略对象;未知 arm 拒绝(不静默回落默认臂)。"""
    if arm == "agent":
        return agent_policy()
    if arm == "one_shot":
        return one_shot_policy()
    raise ValueError(f"unknown experiment arm: {arm!r}")
