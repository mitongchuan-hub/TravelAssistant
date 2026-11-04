# 后端架构（可逆迁移阶段）

当前后端保持单进程、单 Uvicorn worker，并按职责逐步引入 `application/`、`domain/`、`jobs/` 和 `storage/` 模块。迁移只允许新增和修改；旧的 `agents/`、`workflow/`、`services/`、`models.py` 等模块保留，直到所有活动导入完成并有独立删除评审。

## 资源生命周期

`app.main` 的 FastAPI lifespan 负责进程级资源：模型客户端在启动时通过 `client_scope()` 建立，在退出时等待借用请求结束并关闭。业务存储和认证资源应由 lifespan 创建并挂到 `app.state`，路由通过请求上下文读取；本阶段仍保留 `api.dependencies` 的兼容门面，不能再增加新的导入时单例或导入时数据库写入。

## 业务层

- `domain/`：纯业务值对象、需求、行程身份及消息校验。
- `application/`：请求上下文、任务执行、变更提交、工作区和攻略展示的显式用例函数。
- `jobs/`：攻略展示后台 worker 的生命周期入口。
- `storage/`：状态、快照、事务和持久化适配；任务仓储负责历史记录、请求去重和提交回执的按需查询，活动任务字典仅用于进程内执行协调。
- `agents/`：`entrypoints.py` 为唯一请求入口，`travel_runtime.py` 负责业务循环，`display_runtime.py` 负责无工具网页转换；共享 `model_client.py`、`trace.py`、`roles.py` 与 `inputs/`。旧 `agent_client.py/task_runtime.py/main_input.py/agent_roles.py` 已删除，不保留第二套导入入口。模型请求只能借用 lifespan 管理的客户端。
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

## 任务存储与执行边界

任务完整记录由 SQLite 任务仓储保存，包括请求身份、输入、状态与提交回执；历史按任务 ID、旅行内 request ID 或近期列表查询，不回填到活动字典。`self.tasks` 仅保存 `queued/running` 执行状态，普通提交不再复制全部任务历史。工作区仍返回近期历史，不把“移出内存”解释成“记录已删除”。

任务新增或状态变化与业务快照、消息、撤销记录在同一 SQLite 事务内保存；成功后才发布内存状态、移除终态。任务表不参与业务关系投影的全表重建。清空聊天保留历史去重记录，删除旅行则同事务删除所属任务历史。

旧快照任务在升级备份后一次性迁入任务表，迁移完成标记与新快照同事务保存；后续启动不再全量恢复历史。重启中断的任务标为失败，不自动重跑。首次导入旧快照仍有一次性 JSON 解析开销。关闭持久化的隔离测试/演示使用内存仓储，不代表生产历史常驻内存。

聊天执行仍由 FastAPI `BackgroundTasks` 在同一后端进程中启动，这次改动不是可靠任务队列，也没有引入跨进程 Worker。取消检查只读活动内存；同步模型 HTTP 可能仍需等待返回或超时，但迟到结果不得提交。`display_jobs` 保留独立展示 worker 及其现有每旅行替换机制。

本轮只治理任务历史；聊天与其他业务聚合仍使用现有快照，撤销幂等回执的独立增长也不在本轮范围内，不能据此宣称全部 Store 内存增长均已解决。

## 旅行模型与旧存档边界

`domain/summaries.py` 只派生日期摘要，`workflow/counters.py` 只处理执行版本计数，`storage/model_migration.py` 负责版本边界上的旧业务模型清理。`TripRequirements` 仅保存 `start_date/end_date/city/day_count/transport_mode/lodging_address/revision`；`Trip` 不再提供预算、风格和备注字段。前端、API、工具schema、模型投影和关系表同步移除更多设置的结构化契约，不以空值占位或legacy别名继续维护。用户自由要求仍可保留在聊天和通用想法卡片，费用文本、真实路线耗时与执行资源预算不属于这次字段删除。

旅行执行协调状态仅保留 `sequence/epoch/revision`，不再恢复旧 `tasks/focus_task_id/active_question`。取消、替代、手动写和最终提交仍使用单调版本栅栏；历史任务协议中的旧关联字段可只读重放，不把它们重新解释成运行授权。

业务模型升级与任务历史落库分层：任务历史导入阈值固定为 schema 7，schema9不能重新导入已有 SQL 历史或重算 receipt/digest。升级前备份，旧行程快照移走废弃维度但保留原过期／不完整语义，正文与合法HTML不重写。旧撤销条目作为一次性模型变更屏障清空、版本前进，已消费回执保留以防重放；之后的新变更照常支持三步撤销，不再维护旧v7指纹转换链。trips关系镜像物理移除预算／风格／备注列，表迁移、快照、投影与标记同事务；正常启动不重复迁移。

## 单一聊天任务入口

`/trips/new` 的创建表单通过 `POST /api/trips` 保存基础需求并确认地图住宿，成功后进入聊天；该创建操作不启动 Agent，也不创建旧 plan/revision 任务。空白 conversations API 保留兼容，但不是当前新建页面的默认入口。

新执行任务仅使用 `chat`，主提交接口为 `POST /api/trips/{id}/messages`；兼容 `POST /tasks` 也仅接受 `kind=chat` 和 `payload.body`，旧 `plan` / `revision` 新请求返回校验错误。生成与修改仍由同一个 Agent 的 `submit_changes` 完成，Runtime 不再接收 planning/revision 入口；确定性的 confirm、undo、cancel、展示转换不在此次删除范围。

`application/execution.submit_task` 统一任务提交实现，DemoStore 的旧 workflow 门面转调此用例。历史任务字段与记录不改写，列表/工作区继续可读（包括旧 plan/revision、`retry_of`）。失败任务的 POST retry 路由、payload 恢复函数和提交分支已删除，不再写入 `retry_of` 或复用旧授权消息。用户看到“本次处理未完成，请重新发送消息。已有内容已保留。”；重新发送创建新的 chat/main 请求、任务和用户消息身份。网络送达不确定的 pending 仍以同 request_id 恢复/去重；已确认任务不重新执行。普通新 chat 的替代取消、固定策略、sequence/epoch、版本保护、原子提交和撤销屏障保留；undo 网络幂等、HTML 结构纠错与 429 备用请求不变。
