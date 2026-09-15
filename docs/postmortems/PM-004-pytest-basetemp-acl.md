# PM-004 系统临时目录 ACL 损坏:同一坑摔了两次

- **日期**:2026-09-16(M0 首发,M9 复发)
- **发生位置**:pytest 临时目录解析(`tmp_path` fixture / 子进程中的 pytest)
- **失败签名**:
  - `PermissionError: [WinError 5] 拒绝访问: 'C:\\Users\\25924\\AppData\\Local\\Temp\\pytest-of-wabcy'`
  - BUG-015 基线校验 `regression set unexpected rc=1`(子进程内 tmp_path 全部 ERROR)

## 根因

本机 `%TEMP%\\pytest-of-*` 目录 ACL 损坏(常见于曾以管理员身份运行过 pytest),
主套件设置了 `--basetemp=.pytest-tmp` 后主测试正常,但两处"旁路"仍在用系统临时目录:

1. `--basetemp` 没有传递给被测仓库内执行的 pytest(bug 测试里的 `tmp_path` 依赖它);
2. `scripts/gen_bugs.py` 的基线校验 subprocess 同样没带。

## 改进项

1. 主套件:`pyproject.toml` 固定 `--basetemp=.pytest-tmp` 并加入 `.gitignore`;
2. 适配器:`run_pytest` 统一追加 `--basetemp=<报告目录>/basetemp`,
   目标仓库的 tmp_path 落在受控目录,同时不污染工作区;
3. 校验脚本同样显式传 basetemp。

## 教训

"修好了测试"不等于"修好了所有跑测试的路径"。凡是会**另起 pytest 进程**的地方
(适配器、校验脚本、CI、容器)都要继承同一套环境约束,这类配置应当只有一处定义。
