# PM-003 相对路径报告目录污染了工作区 diff

- **日期**:2026-09-16(M4)
- **发生位置**:BASELINE/VERIFY 的 pytest 执行(`run_pytest` 的 `--junitxml` 参数)
- **失败签名**:`[paths] path outside allowed scope: runs/BUG-001-.../reports/baseline-failed.xml`
  + `[scope] 10 files changed`(本应只有 1 个文件)

## 根因

`run_dir` 由相对路径(`runs/<task>`)拼出,junit 报告路径随之是相对路径;
pytest 的 cwd 是**工作区**,于是 `--junitxml=runs/.../x.xml` 被写进了工作区内部。
同一机理还混入了 `__pycache__/*.pyc`。门禁(正确地)把这些都判为越权路径,
第一次真实闭环以 PATCH_REJECTED 收场——门禁拦住的是自己人的副产物。

## 改进项

1. 驱动器入口统一 `run_dir = (Path(runs_root) / task_id).resolve()`,报告路径绝不相对;
2. 物化仓库时强制写入标准 `.gitignore`(`__pycache__/`、`*.pyc`、`.pytest_cache/`),
   让 `git add -A -N` 天然忽略运行副产物;
3. 门禁违规信息详实,这次正是靠 `[paths]` 的具体路径列表直接定位到污染源。

## 教训

Agent 平台对"工作区纯净度"的要求接近于验收环境的洁净室:任何进程的副产物
只要落在工作区内,就会成为模型视角与门禁视角的一部分。
