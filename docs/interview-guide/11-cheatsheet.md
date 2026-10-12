# 11 · 面试速记

## 一句话

PatchPilot 是 Python/pytest 仓库的测试失败诊断与补丁验证原型，重点是平台独立验收和可追溯证据。

## 执行链

TaskSpec/hash → 输入冻结/预检 → 线程池 → graph 阶段（内部共享 plain_loop）→
检索/证据/记忆 → 计划/块补丁 → 门禁/Git → pytest/JUnit → acceptance → report/SQLite。
issue-only 经诊断提议、平台基线复核、可信批准后才能修复。
plain 是单轮驱动，graph 支持多轮候选、交互与恢复；共用验收不等于功能等价。

## 必须会讲

- resolved：非空补丁、失败集全绿、回归全绿、门禁全过、预算内、未取消；双跑启用时一致。graph 另验候选 hash。
- 恢复：checkpoint 定位置，loop/session/memory 带上下文，candidate 带补丁，ledger 带消耗；写阶段重验，无身份不冷启动。
- 资源：已用 + 在途 + 未知持有，回复先结算；费用是估计，不承诺真实账单上限。
- 上下文：AST/字面检索，无向量库；有界回执与原文分页；平台观测不等于模型假设；无跨任务记忆。
- 仓库说明：上下文指导，不进权限面。候选写入口 apply_patch；scratch/记忆/问答另有持久写入。
- 隔离：默认 local，不是沙箱；pytest Docker 有网络/内存/CPU限制，诊断 Docker 另有 PID/no-new-privileges。
- 幂等：完整 TaskSpec hash，只复用在途同契约任务；Redis 只做锁，SQLite 单进程原型，不宣称多实例可靠领取。
- 子任务：默认关闭、有界单层角色委派，独立会话与父预算，不能覆盖父验收。

## 证据与局限

离线 FakeLLM 含参考答案，验证控制链不代表修复率。历史真实 SWE/消融已执行并留有记录，
有选择偏差、抽样回归、早期两臂跨引擎和负结果；当前版本未重新实跑，也无官方成绩。
全绿不证明语义等价，可有过拟合。测试总数、代码行数和 CI 状态不背固定数字，引用具体收据与提交。

## 本轮清理

删除无调用的 _matches_requested wrapper 与 ErrorResponse；修复受理后端/超时/账本/模型身份漂移、VERIFY后终局越时，
Docker custom 预检不再宿主 collect；修正文档。reset_workspace/legacy_v1/fold_output 保留有调用或历史契约用途。
日常 verify.ps1、最多自动选六份测试、90 秒；完整回归待 CI。
