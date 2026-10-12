# PatchPilot 演示入口(S08 / G16)

全部离线演示都不联网、不调用真实模型(FakeLLM 回放),失败返回非零退出码。
演示源目录 `demo/workspace-copy` 在任何一次运行前后保持字节不变(脚本自证)。

各演示分工(G16:不把所有情况挤进一个成功脚本):

| 脚本 | 演示什么 |
|---|---|
| `run_dirty_ticket` | 引擎直连:工单 → 受理 → 修复 → 报告的最小闭环 |
| `run_api_ticket` | API golden path:幂等/进度/报告/补丁 + **取消** + 门禁拒绝 |
| `run_issue_only_ticket` | issue-only 全流程:诊断 → **用户批准验收清单** → 门禁拒绝反馈重试 → 独立验收 → 报告(repair_v2 显式入口) |
| `run_steer_ticket` | **用户澄清与纠偏**:ask_user 等待回答 + steer 作为线索 + 报告输入事件 |
| `run_delegate_ticket` | **子任务委派**:researcher 只读子任务、独立会话/账本、报告 subtasks 段 |
| `run_restart_resume_ticket` | **崩溃恢复**:工具边界进程死亡 → 检查点续跑 → 复位 + 门禁/验证重跑 |

## 0. 能力查询面(G16)

提交前可先查"什么能受理、什么被显式拒绝"(engine/profile/arm/role/model/预算,
读数与受理层同源):

```bash
curl -s http://127.0.0.1:8001/api/capabilities
```

CLI 建任务入口(`submit`,新任务默认 profile=repair_v2,显式随请求发送):

```bash
python -m app.agent.cli submit --bug-id BUG-001
python -m app.agent.cli submit --repo-path <path> --issue "..." --profile legacy_v1
```

## 1. 工单直连(graph 引擎)

```bash
python -m demo.run_dirty_ticket --out runs/demo-dirty
```

输出:任务 ID、基线失败数、变更文件、门禁结果、双测试集、
validation/gate/resource 三态、报告与补丁路径;resolved 才退出 0。

## 2. API golden path(完整 API 面,进程内 TestClient)

```bash
python -m demo.run_api_ticket --out runs/demo-api
```

依次验证:POST custom fake → 同键重提幂等(200+同 task_id)→ 终态 resolved →
轨迹含 apply_patch/run_tests/finish → 报告与资源状态 → diff.patch 与冻结候选可读 →
独立任务取消(CANCELLED)→ 门禁拒绝(PATCH_REJECTED,plain 引擎)。

## 2a. issue-only 全流程(诊断 → 用户批准 → 反馈重试 → 报告)

```bash
python -m demo.run_issue_only_ticket --out runs/demo-issue-only
```

issue-only 受理(无失败/回归集)→ 诊断提议验收清单(WAITING_INPUT)→
批准冻结 manifest → 第 1 轮试图改测试被门禁拒绝 → 第 2 轮正确补丁 →
双集验证 resolved → 报告读出:验收清单已冻结、rounds≥2、运行身份 profile=repair_v2、
Markdown 三态呈现。

## 2b. 用户澄清与纠偏

```bash
python -m demo.run_steer_ticket --out runs/demo-steer
```

ask_user 使任务 WAITING_INPUT → steer 追加纠偏(只作线索)→ answer 回答
(绑定 question_id 与输入版本)→ resolved → 报告 inputs:1 问已答 + 1 纠偏已消费。

## 2c. 子任务委派(引擎直连)

```bash
PATCHPILOT_DELEGATION_ENABLED=true python -m demo.run_delegate_ticket --out runs/demo-delegate
```

临时题目目录装配 `replay/subtasks.json`(真实装载路径)→ 主线 delegate(researcher)
→ 子任务独立会话/轮次/额度 → 主线吸收摘要后 resolved → 报告 subtasks 逐条登记。

## 2d. 崩溃恢复(引擎直连)

```bash
python -m demo.run_restart_resume_ticket --out runs/demo-resume
```

第一次运行死在 run_tests 工具边界(无终态收敛)→ 崩溃现场保全 →
resume=True 续跑:复位工作区 → 重打补丁 → 门禁/双集验证真实重跑 → resolved。
(崩在模型调用在途时,续跑验证全绿也拒判 resolved——G03 语义,见测试。)

## 3. 本地 HTTP 人工演示(文档路径,未进自动化)

上两节的 TestClient 与真实 HTTP 服务共享同一 app 工厂;要走真实 HTTP:
(2026-10-09 已按本节命令人工实跑一次并留证:docs/evidence/2026-10-09-uvicorn-http-demo.txt,
golden path 全链路 resolved;仍未进 pytest,自动化边界见节末说明。)


```bash
# 终端 1:启动服务(离线回放模式;真实模型需先配 .env 并 PATCHPILOT_LLM_ENABLED=true)
PATCHPILOT_LLM_ENABLED=false python -m uvicorn app.api.app:create_app --factory \
  --host 127.0.0.1 --port 8001

# 终端 2:同一工单经 HTTP 提交(POST 体与 demo/run_api_ticket.py 的 PAYLOAD 相同;
# repo_path 用本机绝对路径)。 health → POST → GET 轮询 → GET report:
curl -s http://127.0.0.1:8001/api/health
curl -s -X POST http://127.0.0.1:8001/api/tasks -H "Content-Type: application/json" \
  -d '{"bug_id":"BUG-001","engine":"graph","model":"fake"}'
curl -s http://127.0.0.1:8001/api/tasks/<task_id>
curl -s http://127.0.0.1:8001/api/tasks/<task_id>/report
```

配置了 `PATCHPILOT_API_TOKEN` 时,以上 curl 均需加
`-H "Authorization: Bearer <token>"`(health 豁免)。
说明:HTTP 路径的命令是给人工演示的入口;自动化验证边界只有
`tests/test_demo_smoke.py`(subprocess 真跑脚本)与
`tests/test_api_golden_path.py`(TestClient 全链路),两者不互相替代。
