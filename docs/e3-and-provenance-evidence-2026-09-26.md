# 零成本补证实验:graph+E3 连通性与溯源硬化(2026-09-26)

> 来源:第二轮审计的遗留项收口——审计 §2.2 R3-Q7 / §3 P3-5(E3 自落地零佐证、
> 「攻击成本翻倍」口径互斥)与 §2.5 R1-Q2-1 / §3 P3-2(fake36 批锚在脏工作树)。
> 两项由用户批准以**零花费**(FakeLLM 回放)执行,替代清单中原列的"真实花费"路径。
> 实验环境:Windows 11 + uv CPython 3.14,`PATCHPILOT_LLM_ENABLED=false`,
> 锚点 commit `ae3db2c`(工作树仅含未跟踪的 `.idea/`、`demo/`)。
> 产物目录 `runs/` 不入库(gitignore);读者可按下列命令原样复跑。

## A. graph+E3 连通性实验(P3-5 后半)

命令(audit R3-Q7 设计的原始口径):

```bash
for n in $(seq 1 35); do id=$(printf "BUG-%03d" "$n"); \
  python -m app.evals.run_single --bug "$id" --model fake --engine graph \
    --out runs/e3-graph-2026-09-26; done
```

结果:35/35 `FINISHED/resolved`。三件套判据逐条命中(全部 35 题):

| 判据 | 结果 |
|---|---|
| ① checkpoint 物证 `run_dir/checkpoints.sqlite` 存在 | 35/35 存在(单题约 68 KB) |
| ② 轨迹含 `verify_double_run` 事件 | 35/35 各 1 条(审计实测:此前**全 runs 树 0 条**) |
| ③ 两次比对 mismatch | 35/35 全为 `null`(无失配);每题的 rerun junit 两份齐全 |

事件体样例(`output_summary`):`{"first":{"failed_ok":true,"regression_ok":true},
"rerun":{"failed_ok":true,"regression_ok":true},"mismatch":null}`。

**证明力边界(如实声明,与审计 R3-Q7 的立场一致)**:本实验证明**机制连通**——
graph 引擎的成功路径确实会拉起第二遍双测试集、比对并留痕;不证明**检出力**——
FakeLLM 为确定性回放,两次运行逐字节一致是构造使然,mismatch 恒 0 不构成
"能发现伪绿"的证据。检出力需要"非自适应偶发伪绿"的真实场景,属真实/扰动批次,
随 §D 留档。E3 的定位仍是"结构性冒烟复核"(config.py 与 design.md §8 已同步)。

## B. 干净树基线批重跑(P3-2 前半)

命令(driver CLI,plain 引擎,写 manifest 与锚点反查):

```bash
python -m app.evals.driver --bugs all --model fake --out runs/fake35-clean-2026-09-26
```

`batch_manifest.json` 实测字段:

| 字段 | 值 | 说明 |
|---|---|---|
| `git_commit` | `ae3db2c…`(40 位) | 锚点 = 产出本批的代码 commit |
| `worktree_dirty` | `false` | **干净树**(tracked 无未提交改动) |
| `dirty_fingerprint` | `""` | 干净 ⇒ 空指纹(有脏树时才有 16 位指纹) |
| `input_anchor_checked` | `true` | ls-tree 反查已执行 |
| `input_anchor_missing` | `[]` | 35 个题目录在锚点 commit 上全部存在 |
| `verdict_counts` | `{"resolved": 35}` | fake 回放,构造性全绿 |

对照历史坏锚(fake36 批,审计 R1-Q2-1):锚点 `367ae36` 上只有 BUG-001..028,
夜报却把它当 E5(35 题)的复现口径引用。

## C. 反向验证:新机制能否抓住历史坏锚?

把新落地的 `missing_inputs_at_commit` 直接指向历史坏锚与当时的 35 题清单:

```python
missing_inputs_at_commit("367ae36", ["BUG-001".."BUG-035"])
# → ['BUG-029', 'BUG-030', 'BUG-031', 'BUG-032', 'BUG-033', 'BUG-034', 'BUG-035']
```

与审计 R1-Q2-1 独立发现的"锚点上不存在该批跑的 7 道新题"**完全一致**(7/7 命中)。
即:该缺陷在生产路径上(manifest 落盘前)已不可能静默通过——缺失即留痕 + WARNING。

## D. 覆盖面与遗留(如实)

- **E3 仍无真实模型批次佐证**:本实验是 fake 回放;真实模型批的 E3 佐证随
  P3-1 的实跑批一并产出(人工触发清单 §1);
- **E3 检出力未证**(见 §A 边界),亦无"攻击成本翻倍"式建模——原宣称作废;
- 脏树指纹的边界:未跟踪文件只进名单不进内容(provenance.py docstring 已声明);
- **脏树 fail-fast 硬闸未做**:现为"记录 + 反查 + 告警",对真实模型批次是否
  升级为"发起即拒"属语义决策,留待用户拍板(人工触发清单 §6);
- 本实验的"干净树"口径 = tracked 无未提交改动;未跟踪目录 `.idea/`、`demo/`
  长期存在且不计入(实测校准,见 provenance.worktree_dirty docstring)。