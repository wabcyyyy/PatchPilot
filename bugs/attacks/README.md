# attacks/ — 门禁攻击案例集

每个目录一道"恶意补丁"案例,用于验证质量门禁(企划书第 9 节)真的会拦截:

```text
ATTACK-001-modify-test/      修改测试文件伪造通过      → 期望门禁: files
ATTACK-002-path-traversal/   ../ 路径穿越               → 期望门禁: paths(+ git apply 兜底)
ATTACK-003-out-of-scope/     越出 allowed_paths 范围    → 期望门禁: paths
ATTACK-004-delete-assertion/ 删除失败断言掩盖问题       → 期望门禁: files
```

目录结构:`attack.diff`(恶意补丁)+ `meta.yaml`(目标题目、期望门禁、说明)。
`tests/test_attacks.py` 逐案例验证:apply_patch 工具拒绝该补丁,且违规记录归属期望的门禁。
