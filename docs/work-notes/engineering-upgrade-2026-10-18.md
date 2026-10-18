# 工程升级任务卡（2026-10-18）

范围：补全现有 ACI、证据生命周期、pytest 反馈、失败记忆与运行产物；不重建引擎。

允许修改：
- app/context/ast_outline.py、task_memory.py、evidence_lifecycle.py、evidence_messages.py
- app/tools/symbols.py、registry.py、execution.py、files.py、output_filter.py、patching.py
- app/gitops/blockpatch.py、patcher.py
- app/adapters/pytest_output.py、app/graph/attempts.py
- app/graph/nodes.py、state.py、plain_loop.py、builder.py
- app/evals/driver.py、app/delivery/run_artifacts.py
- tests/test_engineering_upgrade.py、tests/test_gitops.py
- scripts/sample_run.py、examples/sample_run/*、README.md、本任务卡

约束：保持验收、签名、质量门禁语义；保留 G14 具名候选续改，失败版本先留档，
下一轮收到结构化失败摘要。既有工具输出兼容；find_symbol 通过 include_details 取详情。

验收：语法错误块在编译时拒绝、底层多文件失败整体恢复；文件 hash 驱动观测失效并
折叠旧消息；结构化且有预算的测试反馈；失败尝试持久化并跨重规划传递；同一无效补丁
连续出现两次时停止；两引擎报告出口自动生成 patch.diff / trajectory.json /
evaluation_report.json；FakeLLM + 实际 pytest 生成可重放样例，如实标注 local 后端。

验证：根 verify.ps1，定向检查；完整回归留给 CI。无付费模型调用。

## 完成记录

- ACI：详情检索包含装饰器起始行、结束行、带类型签名、Docstring 与原文片段；
  `include_details` 保持旧简版输出兼容。编辑工具强制启用内存语法预检，纯协议编译调用
  默认不启用；底层语法拒绝会恢复所有触及文件，包含非 Python 文件。
- 证据：复用 MemoryEvidence，公开为 EvidenceItem；旧字段保持兼容，文件观测带完整
  hash，多文件检索通过 file_hashes 跟踪依赖。变更/复位按当前文件身份失效；新旧观测
  和原文引用均可审查。所有已登记回执（含错误）尝试保存原文；配额拒存明确保留不可取回状态。
  State 保存 memory_version 与引用，实体继续在绑定的 memory_state.json 中持久化。
- 上下文：陈旧观测与超过近期窗口的回执折成摘要和引用；失效引用在发送前明确提示。
  窗口复用 context_keep_recent_turns，无新增模型调用。
- 测试反馈：PytestOutputParser 从现有 JUnit 报告提取失败数、身份、错误类型和项目帧，
  对序列化诊断统一限额，明示省略条数；pytest 签名和通过判定不变。
- 失败闭环：AttemptRecord 保存完整 diff/失败诊断；State 和记忆保存可恢复引用，PLAN
  收到最近失败教训。保留具名候选续改与原有资源/轮次限制，增加相同非空无效补丁连续
  两次的停止条件。候选恢复后验证结论依旧重新计算。
- 产物：两引擎共用报告出口导出三件套；导出异常写入主报告 artifact_errors，不能冒充
  导出成功，也不改验收结论。轨迹补全节点耗时、完整工具入参和证据失效记录。
- 示例：`examples/sample_run/` 来自 FakeLLM + local 实际 pytest，首轮漏空白串，次轮修复；
  目标测试 2/2、回归测试 4/4。不是 Docker 实测或真实模型效果指标。

定向验证入口：`verify.ps1 -Tests tests/test_engineering_upgrade.py,tests/test_task_memory.py,tests/test_search_tools.py,tests/test_docs_anchors.py`。
早期扩大选择曾达到 90 秒预算，未完成部分不计通过；完整回归、Docker 与真实模型验证未在本卡执行。
提交：本卡实现、必要回归和示例产物同次提交，未自动 push。
