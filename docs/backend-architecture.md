# 后端架构（可逆迁移阶段）

当前后端保持单进程、单 Uvicorn worker，并按职责逐步引入 `application/`、`domain/`、`jobs/` 和 `storage/` 模块。迁移只允许新增和修改；旧的 `agents/`、`workflow/`、`services/`、`models.py` 等模块保留，直到所有活动导入完成并有独立删除评审。

## 资源生命周期

`app.main` 的 FastAPI lifespan 负责进程级资源：模型客户端在启动时通过 `client_scope()` 建立，在退出时等待借用请求结束并关闭。业务存储和认证资源应由 lifespan 创建并挂到 `app.state`，路由通过请求上下文读取；本阶段仍保留 `api.dependencies` 的兼容门面，不能再增加新的导入时单例或导入时数据库写入。

## 业务层

- `domain/`：纯业务值对象、需求、行程身份及消息校验。
- `application/`：请求上下文、任务执行、变更提交、工作区和攻略展示的显式用例函数。
- `jobs/`：攻略展示后台 worker 的生命周期入口。
- `storage/`：状态、快照、事务和持久化适配。
- `agents/`：模型客户端、输入快照、trace 和运行时；模型请求只能借用 lifespan 管理的客户端。
- `workflow/`：保留的兼容实现；任何替换必须逐条迁移导入并保留可回滚路径。

## 数据和演示内容

生产启动不得自动写入或暴露演示旅行、消息、计划或想法。`demo_data.py` 只允许作为显式测试/演示工厂使用；真实数据库和 `.env` 不应被架构验证修改。数据库 schema 迁移必须避免无条件删除业务表，破坏性迁移需要单独审批和备份策略。

## 验证边界

使用固定解释器运行受影响测试和全量测试：

```text
E:/anaconda/envs/TravelAssistant/python.exe -m pytest tests/automated -q
```

不使用真实模型、地图、网络、`.env` 或生产数据库。任何旧文件删除候选应单独报告，不在可逆阶段删除。
