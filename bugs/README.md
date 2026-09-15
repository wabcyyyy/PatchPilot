# bugs/ — 自建 Bug 任务集

每道题一个目录,命名 `BUG-<三位编号>`;攻击案例在 `attacks/`。

## 目录规范(企划书 11.1)

```text
bugs/BUG-001/
├── repo/              # 题目仓库(纯工作树;运行时由驱动器物化为 git 仓库并固定基线 commit)
│   ├── conftest.py    # 空文件,使 from src... 导入生效
│   ├── src/...        # 含脆弱实现的源码
│   └── tests/...      # 失败测试 + 回归测试
├── issue.md           # 面向 Agent 的问题描述(只描述症状,不透露修法)
├── manifest.yaml      # 判定所需信息(见下)
├── replay/script.json # FakeLLM 回放脚本(流水线验证用,不是标准答案)
└── expected/reference.diff # 参考修复 diff(评测"定位成功率"的期望修改范围依据)
```

## manifest.yaml 字段

| 字段 | 说明 |
|---|---|
| `id` / `category` / `difficulty` | 编号;缺陷分类(异常处理/边界条件/类型错误/数据访问/跨文件定位);难度(simple/medium) |
| `failed_tests` | 基线中**必须失败**的测试 nodeid 列表(修复后必须全部转通过) |
| `regression_tests` | 基线中**必须通过**的测试 nodeid 列表(修复后必须保持通过,即 PASS_TO_PASS) |
| `allowed_paths` | 允许修改的路径 glob;超出即被路径门禁拒绝 |
| `max_rounds` | 最大修复轮数 |

## 约定

- 题目由 `scripts/gen_bugs.py` 从 spec 生成,`--validate` 校验基线:failed 必须失败、regression 必须绿;
- `replay/script.json` 是平台闭环的离线验证手段(provider=fake-replay),**评测报告中的真实成绩必须来自真实模型运行**;
- repo 目录不包含 `.git`(保持主仓库可跟踪),基线 commit 在每次任务运行时固定并记入轨迹。
