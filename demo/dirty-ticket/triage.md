# 归一化工作单：脏报障 → PatchPilot 任务

## 1. 原始材料
- raw-ticket.txt（用户报障 + 开发堆栈）
- 仓库：demo/workspace-copy（日期解析相关代码）

## 2. 逐项翻译

| 原始信息 | 归一结果 | 为什么 |
|---|---|---|
| 「清空日期后 500」「ValueError: unrecognized date format: ''」 | `issue_text` 现象描述 | Agent 需要知道用户看到了什么 |
| 堆栈 `src/dateparse.py:17` | 写入 issue_text，但**不直接当答案** | 定位仍由 Agent 做；堆栈只是线索 |
| 「日期不是必填，不填应该能保存」 | 契约改写：**空/空白输入应返回 None** | 把产品语义翻译成函数契约 |
| 「格式不对也会报错」 | **排除出本任务**（属已知合法行为：非法格式抛 ValueError） | 干扰项，不混进本单判定 |
| 开发/测试补充的复现用例 | `failed_tests` = 空串、纯空白两个用例 | 判定的「该修好」清单 |
| 不能弄坏的既有行为 | `regression_tests` = ISO/斜杠/非法格式/None 四个用例 | 判定的「不能回退」清单 |
| 「只许改业务代码，别动测试」 | `allowed_paths = ["src/**"]` | 门禁范围 |
| 优先级/账号/环境 | **丢弃** | 与补丁验证无关 |

## 3. 产出的任务载荷（将喂给 PatchPilot）

- repo_path: demo/workspace-copy
- issue_text: 见下
- failed_tests / regression_tests / allowed_paths: 见下
- 回放脚本: 模拟「模型按定位→打补丁→自测」的行为（离线演示用）
