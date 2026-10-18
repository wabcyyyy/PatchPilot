# 可复现运行产物

这是 BUG-001 的脚本化 FakeLLM 回放，测试通过本机 pytest 实际执行。它演示平台闭环，
不代表真实模型修复能力，也不代表 Docker 隔离实验或 SWE-bench 官方结果。

直接查看：

- [evaluation_report.json](evaluation_report.json)：目标/回归测试、门禁、资源状态和最终结论。
- [patch.diff](patch.diff)：最终 Git 补丁；失败运行的同名文件会明确标为未验收现场。
- [trajectory.json](trajectory.json)：节点流转、工具完整入参、耗时、证据失效和失败尝试。
- [attempts/attempt-1.json](attempts/attempt-1.json)：第一轮失败补丁与测试诊断。
- [replay_script.json](replay_script.json)：可复核的模型回放脚本。
- [reports/verify-failed.xml](reports/verify-failed.xml)、
  [reports/verify-regression.xml](reports/verify-regression.xml)：最终实际测试原始报告。

第一轮提交不完整修复，独立验证失败后保存 AttemptRecord。已有具名候选经过基线复位及
重新应用，第二轮基于明确失败反馈修订补丁，再独立执行目标/回归测试并统一验收。
产物中的时间、测试数量和结论来自生成时的真实运行，未手写通过结果。

仓库根目录复现（使用已安装的项目环境，无模型费用）：

```powershell
$env:PATCHPILOT_EXECUTION_BACKEND = 'local'
.venv\Scripts\python.exe -m scripts.sample_run --out runs/reproduced-sample --runs-root runs/reproduced-sample-run
```

完整现场位于生成命令指定的 `runs-root/sample-engineering-upgrade/`，此目录只复制便于审阅的
报告、补丁、轨迹、失败记录、候选及证据。重新生成时应选择新的 `--runs-root`，避免混入旧现场。

质量报告中的 debug_scan 仅扫描新增行，结果是提示项。`unknown` / `not_run` 不能当作通过。
