# 候选题(BUG-C1xx,待人工审题)

此目录是 **hard 档候选题暂存区**,不属于正式评测集:`app/evals/bugset.list_bug_ids`
只枚举 `bugs/BUG-*` 目录,本目录不会被正式集加载。

## 现状(2026-09-25 E5 审题转正后)

**C101..C107 已转正**为正式集 `bugs/BUG-029..035`(AI 审题记录与逐题证据见
`review-2026-09-25.md`;preflight 三件套在当期 master 复验通过后迁移)。

**C108 未转正**:难度标定不符(fast 版是参考实现的一行变体,分歧即修复点,
预期真实模型 1 轮通过,不满足 hard 的"预期 ≥2 轮"定义;应重标 medium)。
目录保留在本区,`validate_candidate.py` 三件套仍可通过。

## 原始编制背景(T10.2,2026-09-18)

8 道候选(BUG-C101..C108),定位是补正式集的区分度缺口——现有 28 题真实模型
1 轮全过、全部单文件。候选题按类型分三档:

| 类型 | 题目 | 特征 |
|---|---|---|
| 跨文件链(根因在下游) | C101 聚合覆盖、C104 限流单位换算 | 症状文件 ≠ 根因文件,需沿调用链两跳定位 |
| 双文件协同修复 | C102 运费封顶+漏加、C105 阈值对齐、C106 CSV 转义、C107 模板转义 | 只修一处无法 resolved,补丁必须同时改两个文件 |
| 对照实现定位 | C103 URL 解码、C108 校验和分歧 | 仓库内有语义权威的对照实现 |

## 每道题已通过的机器验证

1. `scripts/validate_candidate.py bugs/candidates/BUG-Cxxx`:manifest/replay 完整性
   + 基线断言(failed 逐条红、regression 整体绿);
2. reference.diff 可 `git apply`,应用后 failed+regression 全绿;
3. fake/graph 引擎回放 `resolved`(平台闭环可解)。

## 审题 checklist(转正前人工确认)

- [ ] issue 描述与实际缺陷一致,无泄底(不直接点名根因文件);
- [ ] 修法判定用**操作化标准"无误杀"**(P3-14 整改,取代"修法唯一"):
      逐个列出能通过全部测试的等价修法并核验回归集不误杀,
      checklist 记录每个已核验的等价修法;存在会被误杀的等价修法 → 一票否决。
      "唯一且自然"不再是判据——它曾在 C103/C107 与"等价替换"注记同 row 打勾时
      被静默降级,如实标准就是无误杀;
- [ ] 难度标定为 hard 恰当(预期真实模型 ≥2 轮或多轮失败);
- [ ] `allowed_paths` 覆盖所有自然修法可能触碰的文件。

preflight 复验(P3-14 整改后)必须带 `--log-dir` 落盘逐题原始输出:

```bash
.venv/Scripts/python.exe scripts/validate_candidate.py bugs/candidates/BUG-Cxxx \
    --log-dir runs/preflight/<日期>
```

结论("通过/不通过")与证据(每次 pytest 的命令/退出码/stdout)分离,
审题记录只引用落盘产物,不再以夜志一句话作为指针环的闭合点。

## 转正流程(沿用 ee16b69 先例)

人工审题通过后:目录移入 `bugs/`(编号改为正式 `BUG-0xx` 续号)、
更新 `bugs/README.md` 与正式集计数、跑全量评测刷新报告。
