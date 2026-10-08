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

## 后续(2026-10-08,M11.5/M11.6)

教训那句当时只说对了一半,这次被自己的配置反噬:

1. **改进项 1 的落点选错了**。`pyproject.toml` 把 `--basetemp=.pytest-tmp` 钉在**仓库内**,
   而仓库正好在临时目录的父链上 —— 于是"工作区不是 git 仓库"这类夹具缺陷可以悄悄变绿
   (PROGRESS 的 M10 卡记了一条真实假通过)。M10.5 加了会话级防线
   (`tests/conftest.py:_basetemp_outside_repo`)拦仓库内落点,这条 pin 就与防线直接相冲:
   按 `AGENTS.md`/`README` 写的 `pytest -q` 会先 `rm_rf` 掉 `.pytest-tmp` 再整会话报错。
   现在把落点从共享配置里**删掉**:CI 用系统临时目录,本地显式传仓库外且可写的落点
   (`pytest -q --basetemp=D:/tmp/pt`),理由与本机 ACL 现状写进 `AGENTS.md` 与 `pyproject.toml` 注释。
2. **ACL 损坏至今仍在**(未修):`icacls %TEMP%\pytest-of-wabcy` 直接"拒绝访问",旁边还留着
   当年以 SYSTEM 跑出来的 `pytest-of-SYSTEM`。清理它要管理员权限、且影响本机所有项目,
   属于机器级动作,不在仓库里替谁做。
3. **改进项 2 有第二个坑**:适配器追加的 `--basetemp` 当时按**报告目录**派生,
   于是同一目录里的并发执行共用同一个临时根,而 pytest 在 `--basetemp` 已存在时先整棵删掉再建
   → 后起步那次把先起步那次正在写的 `tmp_path` 删光。实测 18 次并发 9 次伪失败
   (`scripts/measure_basetemp_contention.py`,判据见 TODO M11.5),失败在平台侧的形态正是
   本篇当年那句"子进程内 tmp_path 全部 ERROR"。现改成**每次执行私有并在执行后回收**,
   由 `tests/test_basetemp_isolation.py` 守着(把落键改回旧行为,该用例 12/18 红)。
