# PM-002 工具函数与底层函数同名导致递归自调用

- **日期**:2026-09-16(M3)
- **发生位置**:PROPOSE_PATCH 节点 → `app/tools/patching.py::apply_patch`
- **失败签名**:`AttributeError: 'WindowsPath' object has no attribute 'allowed_paths'`

## 根因

`patching.py` 顶部 `from app.gitops.patcher import apply_patch`,
随后又定义了同名工具函数 `apply_patch(ctx, diff_text)`。
模块级名字被后者覆盖,工具内部"调用底层补丁应用"实际变成了**递归调用工具自己**,
第一个参数 `ctx.workspace` 被当作了 ToolContext。

## 改进项

1. 从底层模块导入时一律加别名:`from app.gitops.patcher import apply_patch as git_apply_patch`;
2. 工具层与 gitops 层保持命名区分(工具名是协议、函数名是实现);
3. 这类缺陷 unit test 能抓,但更早的信号是"参数类型对不上"——看到
   WindowsPath 拿去找 `allowed_paths` 应立刻怀疑名字遮蔽,而不是类型设计。

## 教训

Python 的"后定义覆盖先定义"在导入与同名函数之间最易踩;别名导入是零成本保险。
