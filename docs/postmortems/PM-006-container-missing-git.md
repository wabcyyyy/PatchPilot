# PM-006 容器化部署连环坑:缺 git、端口冲突、幂等唯一约束

- **日期**:2026-09-16(M8)
- **发生位置**:docker compose 的 api 容器(本地/容器内 VERIFY 之间)
- **失败签名**(按出现顺序):
  1. `FileNotFoundError: ... 'git'` → 任务 NEEDS_REVIEW;
  2. 健康检查返回 `{"detail":"Not Found"}`(响应来自**别的**服务);
  3. `sqlite3.IntegrityError: UNIQUE constraint failed: tasks.idem_key`。

## 根因

1. `python:3.11-slim` 不含 git,而 gitops 的快照/补丁/回滚全部依赖 git——
   本地跑通时感知不到,容器镜像换基础层后暴露;
2. 宿主 8000 端口已被另一个项目占用,我们的 compose 端口映射绑定失败,
   curl 打到的是那个服务;`docker ps` 里我们的 api 容器停在 `Created` 状态;
3. 幂等设计错位:`idem_key` 加了 UNIQUE 约束,但语义是"仅对在途任务幂等",
   终态任务重跑同键必然撞约束。

## 改进项

1. api 镜像显式安装 git(`apt-get install -y git`),并在镜像注释里写明依赖原因;
2. compose 端口改 8001:8000;健康检查后先 `docker ps` 确认容器真的 Up;
3. 去掉 `idem_key` UNIQUE,幂等 = "查询最近同键任务是否在途 + 任务锁",
   终态任务天然允许重跑;`docker compose down -v` 重置旧 schema 后复测;
4. 把 bugs 目录改为只读挂载进容器,题目更新不再需要重建镜像。

## 教训

容器化的失败模式全是"环境差异",而环境差异**不会在本地测试里出现**。
部署验收必须包含一条真正的容器内端到端(创建任务→轮询→报告),
只验证"容器起来了"是不够的。
