# 前后端迁移合同

本次将既有手机界面迁移至 Vue 3 + TypeScript + Vite，FastAPI 只处理 JSON 业务接口与隔离的攻略文档。不是页面改版，也不改变 Agent 原子提交或 SQLite 历史数据格式。

## 归属

- `frontend/src`：浏览器页面、组件、路由、状态、样式。
- `backend/app/api`：JSON 输入校验、认证、归属检查、公开数据投影、页面专属地图工具。
- `backend/app/agents`：单业务 Agent、独立展示转换、专属提示词/工具。
- `backend/app/services`、`workflow`、`storage`：继续拥有业务规则、后台任务、安全提交与保存恢复。
- API 工具与 Agent 工具保持独立实现，允许重复；模型和地图凭证不进浏览器。

## 旧入口到新入口

| 旧行为 | 新合同 |
| --- | --- |
| Jinja 登录/注册页面与表单跳转 | Vue AuthPage + `/api/auth/session/login/register/logout`（session 为 GET，其余 POST） |
| 旅行列表/创建/删除 HTML 表单 | Vue TripsPage/CityOutingPage + `/api/trips` JSON |
| 住宿多候选表单 | 创建/更新响应 `choices_required`；只提交服务端 choice_token + poi_id 完成原草稿 |
| 游玩设置 HTML | GET/PUT `/api/trips/{id}/requirements`，原需求 revision 检查 |
| 整个 workspace / HTML 消息片段 | `/api/trips/{id}/workspace` 一致快照；Vue 按稳定 ID 渲染 |
| messages after-ID 增量 | 保留读取兼容；Vue 使用完整快照，能更新同 ID 的思考消息和进度 |
| 生成/修改 | 聊天 `/messages`；新任务仅 chat，`/tasks` 只保留 chat 兼容提交；历史 plan/revision 可读；无失败任务 retry 接口，重新发送是新 chat 身份 |
| 想法表单 | ideas JSON 增删；仍只展示最新八张墙卡 |
| 清空聊天表单 | DELETE messages，复用 clear_chat 的取消/保留行程语义 |
| 确认行程表单 | POST plan/confirm，完整 plan_id/hash/revision |
| HTML 攻略片段 | Vue 根据 display_state 展示，不接收页面片段 |
| 攻略预览/下载文档 | `/api/trips/{id}/guide` 仍返回受限 HTML，核对 plan_id/source_hash/guide version |

## 必须保留的安全与交互

1. HttpOnly 会话 Cookie；CSRF token 从 session bootstrap 获取后仅放内存，所有状态变更使用请求头及同源 Origin 校验。登录/退出轮换绑定。
2. 每条旅行 API 复验所有权；他人旅行与不存在旅行同为404；API错误为JSON，不重定向到HTML。
3. 工作区快照在同一 Store 锁内生成；只公开白名单字段，排除 raw traces、receipt、workflow 内部状态。
4. 未知网络投递结果不等于失败：恢复同一 request_id，不自动生成另一个任务；旧旅行/旧请求响应不得覆盖新状态。
5. 430px 手机外壳、雾蓝颜色、想法/聊天底栏、原信息顺序保持；宽屏不改成桌面仪表盘。
6. 草稿、IME、移动键盘、阅读位置、长回复/进度折叠、失败恢复及焦点行为保留。
7. 生成 HTML 只通过 iframe src 加载；无脚本/同源权限，保留 CSP、no-referrer、nosniff/no-store，下载绑定当前文档版本。不能用 v-html 注入。
8. 不新增用户可见的取消任务入口；现有新聊天替代/清空取消行为保持。
9. 旧结构化方案仍可读，未知费用、交通条件、饮食/预算限定不丢失。

## 测试迁移原则

- 业务、证据、原子提交/回滚、持久化重载、过期提交、取消、资源借用/关闭及工具独立性测试保留。
- 旧路由顺序、Jinja filter/片段、DOM脚本实现断言改为 API/DTO、Vue组件和实际浏览器行为，不以删除断言宣称通过。
- Markdown/HTML转义、交通与费用限定、进度公开投影、攻略可用状态/下载/焦点/iframe稳定性需在新层继续覆盖。
- 零付费浏览器夹具使用临时库、伪模型/地图、显式离线配置；不能读真实.env或碰正式库。

验收结果由实际运行记录给出；此文档不是测试通过声明。
