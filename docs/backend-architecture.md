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

## Agent 撤销

`application/undo.py` 集中管理每旅行最多三份业务快照、单调版本、写屏障和幂等回执。兼容 `workflow/turn_commit.py` 与 `application/execution.py` 都在同一 stage 内登记历史；快照存于可选 `agent_undo` 字段，旧库缺字段视为空历史。手动业务写也使用 stage 原子清链。工作区仅公开计数、entry/message ID、版本及可用性，不公开快照。检查 owner、全局 CSRF、entry ID、request ID和预期版本；同 token重复返回同回执。撤销重建 plan identity并递增需求/workflow版本，使迟到结果不能发布；旧 HTML重新验证，不恢复旧 job。

## 单一聊天任务入口

新执行任务仅使用 `chat`，主提交接口为 `POST /api/trips/{id}/messages`；兼容 `POST /tasks` 也仅接受 `kind=chat` 和 `payload.body`，旧 `plan` / `revision` 新请求返回校验错误。生成与修改仍由同一个 Agent 的 `submit_changes` 完成，Runtime 不再接收 planning/revision 入口；确定性的 confirm、undo、cancel、展示转换不在此次删除范围。

`application/execution.submit_task` 统一任务提交实现，DemoStore 的旧 workflow 门面转调此用例。历史任务字段与记录不改写，列表/工作区继续可读（包括旧 plan/revision、`retry_of`）。失败任务的 POST retry 路由、payload 恢复函数和提交分支已删除，不再写入 `retry_of` 或复用旧授权消息。用户看到“本次处理未完成，请重新发送消息。已有内容已保留。”；重新发送创建新的 chat/main 请求、任务和用户消息身份。网络送达不确定的 pending 仍以同 request_id 恢复/去重；已确认任务不重新执行。普通新 chat 的替代取消、固定策略、sequence/epoch、版本保护、原子提交和撤销屏障保留；undo 网络幂等、HTML 结构纠错与 429 备用请求不变。
