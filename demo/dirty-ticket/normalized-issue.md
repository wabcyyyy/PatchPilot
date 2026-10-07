## 现象
表单将「出生日期」清空后保存，服务端 500。
用户期望：日期非必填，不填也能保存。

## 线索（来自堆栈，仅供参考，未必是唯一相关点）
ValueError: unrecognized date format: ''
  at src/dateparse.py parse_date

## 需要满足的契约
`parse_date`（src/dateparse.py）：
- 输入 `None` → 返回 `None`（已支持，请勿破坏）
- 输入空字符串 `""` 或纯空白（如 `"   "`）→ 应返回 `None`，不得抛异常
- 非法格式（如 `"31-01-2026"`）→ 仍应抛 `ValueError`（这是预期行为，不要改成返回 None）
- 合法格式（`YYYY-MM-DD` / `YYYY/MM/DD`）→ 保持现有解析行为

## 约束
- 只修改 src/ 下的实现代码，禁止改动 tests/
- 修复后：下方失败用例须通过，回归用例须全部保持通过

## 失败用例（必须修到通过）
- tests/test_dateparse.py::test_empty_string_returns_none
- tests/test_dateparse.py::test_whitespace_returns_none

## 回归用例（必须保持通过）
- tests/test_dateparse.py::test_iso_format
- tests/test_dateparse.py::test_slash_format
- tests/test_dateparse.py::test_invalid_format_raises
- tests/test_dateparse.py::test_none_returns_none
