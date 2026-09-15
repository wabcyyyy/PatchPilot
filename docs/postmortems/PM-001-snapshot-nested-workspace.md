# PM-001 工作区嵌套进源仓库导致夹具污染

- **日期**:2026-09-16(M1)
- **发生位置**:SNAPSHOT 节点之前的 `app/gitops/snapshot.py::create_workspace`
- **失败签名**:`DID NOT RAISE TaskError` + 后续测试 `working_tree_is_clean == False`

## 根因

`create_workspace` 只拦截了"工作区包含源仓库"(`dst in src.parents`),
漏掉了反方向"工作区在源仓库内部"。测试把仓库复制进 `demo_repo/nested`,
`copytree` 连 `.git` 一起复制,session 级夹具被污染:
后续所有工作区都带一个未跟踪的内嵌仓库,`git clean -fdx` 默认不删内嵌 git 仓库,
于是 diff、回滚测试全部连锁失败——而真正的缺陷只有一个。

## 改进项

1. 两个方向的包含关系都要拒绝:`dst == src or dst in src.parents or src in dst.parents`;
2. `rollback` 使用 `git clean -ffdx`(双 `-f` 才会清理内嵌 git 仓库),纵深防御;
3. 测试夹具尽量做成"每次用例自建、目录互不相交",session 夹具只放只读数据。

## 教训

一个测试失败时,先看它是否污染了共享状态;连锁失败的最上游才是真缺陷。
