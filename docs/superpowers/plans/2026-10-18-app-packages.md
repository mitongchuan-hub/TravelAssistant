# app 目录整理

目标：按已确认的职责分组整理 Python 模块，保持业务行为及 app.main:app 启动入口。

- agents：agent_client、agent_roles、agent_runtime、agent_protocol、model_transport、main_input、task_runtime。
- workflow：tasks、task_context、turn_workspace、turn_commit、display_tasks。
- services：requirements、planning、planning_evidence、city_outings、places、plan_display。
- views：presentation、chat_presentation、display_presentation、route_view、trip_welcome。
- 根目录保留 main、models、store、persistence、auth 及包初始化文件；tools、prompts、templates、static、schemas 原位保留。

步骤：

1. 建立四个包，将文件迁入；将项目内部导入同步改为绝对导入，包括函数内延迟导入。
2. 修正模型客户端的 .env 路径和展示 schema 路径，更新当前维护文档中的模块路径。
3. 校验全部 Python 语法、内部导入目标、实际模块加载和模板语法；通过隔离存储的 HTTP 冒烟验证创建、登录及行程页面。
4. 重启既有服务并检查健康状态。当前 tests/automated 不存在，不以历史测试结果代表本次验证。

不创建兼容转发文件；不合并模块、不改变执行流程、不修改现有数据库。
