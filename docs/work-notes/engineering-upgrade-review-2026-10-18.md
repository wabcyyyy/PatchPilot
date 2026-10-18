# 工程升级审查收尾（2026-10-18）

审查对象：1b20fa1。保留工作区已有 README、配置示例和注释整理，不混入本次修复提交。

范围：app/context/evidence_lifecycle.py、app/tools/registry.py、app/graph/resume.py、
app/delivery/run_artifacts.py、tests/test_upgrade_review.py、本说明。

修复：恢复时复位/重应用后按文件身份失效旧观测；短回执落盘错误返回明确不可取回
元数据，保留工具本身的执行结果；验收报告区分实际失败与缺少身份确认的全绿 XML。
分页取回自身再次被折叠时，为本次回执保存独立身份，避免原文 hash 错绑到源证据。

验证：根 verify.ps1 定向检查，不运行本地全量；完整回归以 push 后最终提交 CI 为准。
不将 CI 读数写入仓库。提交只包含本卡修复和必要回归。
