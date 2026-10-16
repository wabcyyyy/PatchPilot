# PatchPilot 商业模型基线评测操作指南

本文档规定 PatchPilot 接入商业付费大模型（如 DeepSeek-Coder、GPT-4o、Claude 3.5 Sonnet 等）进行测试失败诊断与补丁生成基线评测的标准流程、安全护栏与对账规范。

---

## 1. 核心原则与授权边界

1. **显式预登记（Pre-registration）**：
   - 商业模型的真实调用**只认显式 Experiment Manifest**，严禁通过修改代码常量或临时传参随手起批；
   - 每次实验必须事先定义：题单、双臂变量（如 Agent 循环 vs One-Shot）、重复次数（`repetitions >= 2`）、Token/费用预算包（Micro-USD）、停手条件与审批人信息。
2. **零网络 Dry-Run 先行**：
   - 在真实发起任何付费请求前，必须使用 `--dry-run` 展开矩阵并锁定 `manifest_hash`，核对 `planned_runs` 与总额度；
   - 严禁未经 `--dry-run` 直接执行付费批。
3. **不可逾越的预算与急停护栏**：
   - 批次设置硬性总限额（`batch_cap_tokens` 与 `batch_cap_cost_micro_usd`），单任务超时截断；
   - 触发 400 协议拒绝、花钱不入账（`spend_without_booking`）或系统级紧急停发（`PATCHPILOT_EMERGENCY_STOP=true`）时，驱动器立即原子停手并保全现场。
4. **同一验收判据**：
   - 评测中对商业模型生成的补丁，严格执行与开源/离线基准相同的显式合取判定（`app/graph/acceptance.py`）：
     $$\text{Resolved} \iff \text{Patch} \neq \emptyset \land \text{Gates Passed} \land \text{Target Tests Passed} \land \text{Regression Passed} \land \text{Within Budget} \land \neg\text{Cancelled}$$
   - 绝不放宽门禁或篡改期望行为。

---

## 2. 环境配置与凭据设置

执行商业评测前，操作员需导出对应模型端点的环境变量：

```bash
# 1. 基础端点与密钥 (以 DeepSeek / OpenAI 兼容端点为例)
export OPENAI_API_KEY="sk-your-authorized-commercial-key"
export OPENAI_BASE_URL="https://api.deepseek.com/v1"

# 2. PatchPilot 运行时控制
export PATCHPILOT_LLM_ENABLED="true"
export PATCHPILOT_LLM_PROVIDER="openai"
export PATCHPILOT_MODEL_NAME="deepseek-coder"
export PATCHPILOT_API_TIMEOUT_SECONDS="120"

# 3. 规避非确定性思维链 400 错误 (见 probe-thinking-400 实证)
export PATCHPILOT_LLM_THINKING="disabled"
```

> [!CAUTION]
> 绝不要将真实 API Key 提交到 Git 仓库或明文记录在任何 markdown 文档中。测试套件内运行必须始终使用 `app/llm/fake.py` 的 FakeLLM。

---

## 3. 准备评测清单（Manifest）

参考模板 `docs/eval-commercial-baseline-manifest-template.json` 编制正式评测清单：

```json
{
  "schema_version": 1,
  "manifest_id": "EXP-COMMERCIAL-DEEPSEEK-2026-10",
  "question": "评测 DeepSeek-Coder 在受控门禁下的定位与补丁生成表现",
  "engine": "graph",
  "model": {
    "provider": "openai",
    "name": "deepseek-coder"
  },
  "arms": [
    {
      "name": "agent",
      "variables": {
        "PATCHPILOT_LLM_ENABLED": "true",
        "PATCHPILOT_LLM_THINKING": "disabled",
        "PATCHPILOT_CONTEXT_WINDOW_TOKENS": "16000",
        "PATCHPILOT_TOKEN_ESTIMATE_FACTOR": "1.0"
      }
    },
    {
      "name": "one_shot",
      "variables": {
        "PATCHPILOT_LLM_ENABLED": "true",
        "PATCHPILOT_LLM_THINKING": "disabled",
        "PATCHPILOT_CONTEXT_WINDOW_TOKENS": "0",
        "PATCHPILOT_TOKEN_ESTIMATE_FACTOR": "1.0"
      }
    }
  ],
  "bugs": [
    "BUG-001",
    "BUG-002",
    "BUG-003",
    "BUG-014",
    "BUG-029"
  ],
  "repetitions": 2,
  "budget": {
    "budget_mode": "hybrid",
    "token_budget": 400000,
    "cost_budget_micro_usd": 2000000,
    "max_model_calls": 30,
    "model_context_tokens": 16000,
    "batch_cap_tokens": 8000000,
    "batch_cap_cost_micro_usd": 20000000,
    "max_turns": 24
  },
  "stop_conditions": [
    "protocol_400: 端点明确拒绝/400错误立即停手",
    "spend_without_booking: turns=0 但产生外部计费立即停手",
    "batch_cap: 累计费用触及 batch_cap_cost_micro_usd 立即停手",
    "emergency_stop: 触发系统级紧急停发标志立即停手"
  ],
  "authorization": {
    "granted_by": "OpsLead-ZhangSan (2026-10-16)",
    "scope": "商业模型受限基线评测，严格限制在指定 5 道基准题与 20 USD 上限内",
    "max_cost_micro_usd": 20000000
  },
  "outputs_root": "runs/exp-commercial-deepseek-2026-10",
  "registered_upgrades": [
    "Schema-V3",
    "AtomicQuotaSettlement",
    "FencingLeases",
    "SanitizedPublicView"
  ]
}
```

---

## 4. 离线 Dry-Run 校验

在任何真实调用发生前，执行离线验证：

```bash
python scripts/run_paid_batch.py --manifest path/to/your-manifest.json --dry-run
```

- 预期输出：打印完整的执行矩阵（bug $\times$ arm $\times$ repetition）及预算封包；
- 确认 `network_requests: 0`、`model_requests: 0`；
- 记录输出中的 `manifest_hash`，用于后续台账与结果比对。

---

## 5. 执行评测批次与台账审计

确认授权无误后，启动批次驱动器：

```bash
python scripts/run_paid_batch.py --manifest path/to/your-manifest.json
```

- 运行中会自动竞争 `runs/paid-batch.lock`，防止并发冲突；
- 每一个任务尝试（attempt）完成后，实时将消耗追加写入台账 `runs/paid-batch-ledger-2026-10-09.jsonl`；
- 遇到任何异常退出码：
  - `0`: 批次全部正常完成；
  - `1`: 参数、环境或 manifest 校验不合法；
  - `3`: 触发停手条件（如连续失败或协议报错）；
  - `4`: 累计 Token 或费用触顶预算封包。

---

## 6. 评测结果汇总与双臂归因对比

评测结束后，执行台账汇总分析：

```bash
# 1. 汇总统计 (计算采纳率、单位补丁成本、Token 分位数与失败归因分布)
python scripts/summarize_paid_batch.py

# 2. 严格核对两臂差异 (排除除已登记变量外的任何隐性配置漂移)
python scripts/compare_experiments.py --manifest path/to/your-manifest.json
```

汇总指标包含：
1. **采纳补丁率（Accepted Patch Rate）**：通过所有门禁、原失败测试与回归测试的补丁比例；
2. **每采纳补丁成本（Cost per Accepted Patch）**：总费用 / 采纳补丁数；
3. **消耗分布（Token p50 / p95）**：验证上下文有界压缩与重规划机制在真实商用模型上的节约效果；
4. **全景失败分类**：未通过项明确归类为定位超时、门禁拦截、语法报错或回归失败，不挑跑、不隐瞒。
