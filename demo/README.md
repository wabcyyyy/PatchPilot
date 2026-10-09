# PatchPilot 演示入口(S08)

两个离线演示都不联网、不调用真实模型(FakeLLM 回放),失败返回非零退出码。
演示源目录 `demo/workspace-copy` 在任何一次运行前后保持字节不变(脚本自证)。

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

## 3. 本地 HTTP 人工演示(文档路径,未进自动化)

上两节的 TestClient 与真实 HTTP 服务共享同一 app 工厂;要走真实 HTTP:

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
