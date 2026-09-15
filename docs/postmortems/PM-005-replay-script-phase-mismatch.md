# PM-005 回放脚本与状态机阶段错位,脚本耗尽导致"放弃"

- **日期**:2026-09-16(M5)
- **发生位置**:LOCALIZE 节点 → PROPOSE_PATCH 节点的工具循环
- **失败签名**:`status=VERIFY_FAILED, summary="replay script exhausted"`

## 根因

回放脚本是按 plain 引擎"单循环走完全部步骤"设计的:
`search → read → apply_patch → run_tests → finish`。
graph 引擎把循环拆成两个阶段后,LOCALIZE 阶段(只读工具)尝试执行 `apply_patch`
被阶段限制正确拦截,但该调用**仍从脚本中消费掉了**;到 PROPOSE_PATCH 阶段脚本已耗尽,
FakeLLM 的防御逻辑返回 `finish(success=false)`,任务以"模型放弃"终态收场。

有趣的是这不是框架缺陷:阶段限制、脚本耗尽防御、终止语义**各自都按设计工作**,
是"脚本与执行模型不匹配"这一集成假设错了。

## 改进项

1. 生成器为 graph 引擎输出独立的分阶段脚本
   (`replay/graph-script.json`:定位段只含只读工具 + finish);
2. `load_replay_script(bug, kind=engine)` 按引擎取脚本,CLI 与服务端同源;
3. FakeLLM 保留"耗尽即放弃"防御——它把集成错配转化成了一个**可诊断的终态**,
   而不是死循环烧 token。

## 教训

把单循环拆成多阶段状态机时,测试夹具(包括"脚本化模型")也必须跟着分阶段;
集成错误被防御性设计兜住时,要顺着终态报告把根因修在夹具层,而不是放宽运行时限制。
