# TravelAssistant · 同城游玩

以已确认的住宿为起终点，生成、阅读和调整同城游玩路线。使用 Vue 3、TypeScript、Vite、FastAPI JSON API 和 SQLite，当前按单进程运行。

## 使用方式

1. 注册或登录，开始新的聊天，直接告诉助手住宿名称、所在城市和游玩想法；地图重名时在聊天中说明具体分店或地址。
2. 在聊天中提供出行开始日期、游玩天数与交通方式；每日出门/返回时间、人数、预算与偏好可选，未知信息不补成事实。
3. 发消息说“生成路线”，修改、确认和网页重试也通过消息提出。
4. 行程正文保存后，网页 Agent 在后台生成 HTML/CSS 攻略；聊天页点击“攻略”预览，也可以打开独立网页或下载 HTML。
5. 通过聊天修改方案；只想换颜色、字号或排版时可直接说“把攻略改成手账风”。纯风格修改保持行程内容与确认状态，内容修改后重新生成网页。设置变化不会自动重排行程。

产品面向手机浏览器，工作区采用最多 430px 的单列布局。工作区保留“想法／聊天”切换，想法墙展示和管理自由要求；点击“攻略”进入预览，顶部左侧返回聊天、右侧下载当前 HTML，网页可用时显示下载入口。宽屏浏览器使用相同的手机交互。旧行程链接仍会打开攻略预览。每日从同一住宿出发并返回，未知地点保持待核实；网页只复用正文已提供的 HTTPS 链接。查询耗时只是查询时估算，不保证未来开放、班次、票价或现实可执行性。

项目只提供从住宿出发并返回的同城游玩。历史记录的需求、聊天和行程可查看；继续生成或调整前，在聊天中完成住宿地图匹配并保存。每趟行程只保留一份当前方案，没有方案历史恢复入口。

## 代码目录

```text
TravelAssistant/
├── frontend/                 # Vue 3 + TypeScript + Vite
│   ├── src/
│   │   ├── api/              # 类型化 fetch、CSRF、错误处理
│   │   ├── pages/            # 登录、旅行、设置、工作区
│   │   ├── components/       # 聊天、想法、进度、攻略组件
│   │   ├── stores/           # 会话与工作区状态
│   │   ├── composables/      # 焦点等浏览器交互
│   │   ├── router/           # 页面路由
│   │   └── assets/           # 沿用既有样式
│   └── tests/                # 组件与真实浏览器验证
├── backend/app/              # 唯一 Python app 包
│   ├── main.py               # 配置校验、API 装配与生命周期
│   ├── api/                  # JSON API、公开DTO、页面专属 tools/
│   ├── auth/                 # 账号、会话、CSRF
│   ├── core/                 # 配置与共享客户端
│   ├── agents/               # 单 Agent、prompts/ 与专属 tools/
│   ├── services/             # 业务规则及 itinerary/ 行程服务
│   ├── workflow/             # 任务、版本检查、原子提交
│   ├── storage/              # 内存状态与 SQLite 保存恢复
│   ├── views/                # 保留的纯展示/历史兼容处理
│   └── models.py             # 业务数据结构
├── tests/                    # Python 自动化与离线评测
│   ├── browser/server.py     # 浏览器测试专用后端服务
│   └── benchmarks/agent_latency.py # 模型调用耗时评测（默认离线）
├── docs/
└── .env                      # 本地配置，不提交
```

前端只访问相对 `/api` 路径；模型与地图密钥始终留在后端。页面专属地图实现 `backend/app/api/tools/` 与 `backend/app/agents/tools/` 独立维护，不相互调用。生成的 HTML 攻略不是 Vue 模板：仍从受保护的文档端点加载进无脚本/无同源权限 iframe。

修改规则、数据保存和 Agent 执行分别查看 `services/`、`storage/`、`agents/`；页面结构和浏览器交互查看 `frontend/src/`。迁移合同见 [前后端迁移说明](docs/frontend-backend-migration.md)。

后端公开返回结构集中在 `backend/app/api/schemas.py`，由 serializers 的白名单投影生成；这些 TypedDict 只明确类型，不替代请求运行时校验或自动补值。行程依据是否过期统一由 `services/itinerary/freshness.py` 判断，工作流不依赖展示模块。演示消息、卡片和行程的纯工厂位于 `storage/demo_data.py`，Store 仍负责状态和持久化协调。

## 环境与安装

- Python 3.11 或更高版本，建议使用虚拟环境。
- 可访问的 OpenAI 兼容模型服务及 API Key。
- 高德地图 Web 服务 Key，用于住宿匹配、地点和路线查询。天气使用 Open-Meteo，无需额外 Key。

本机开发固定使用 `E:/anaconda/envs/TravelAssistant/python.exe`。PATH 中的默认 `python` 指向无效的空文件，`py` 也未指向项目环境；安装依赖和启动服务均使用下面的完整路径。其他机器请替换为实际的项目 Python 解释器路径。

在项目根目录运行：

```powershell
& E:/anaconda/envs/TravelAssistant/python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

编辑 `.env`，填写有效的模型服务地址、模型名和模型/地图凭证，并明确填写开关、超时和数据库路径。以下是配置形状；服务地址与模型名按实际供应商填写，不表示所有供应商支持相同协议。必需项缺失、空白、仍是模板占位符或格式错误时，启动会一次列出问题并停止，不会替你选择模型、地址或数据库：

```env
OPENAI_API_KEY=你的模型服务密钥
OPENAI_BASE_URL=你的模型服务HTTP或HTTPS地址
OPENAI_MODEL=供应商支持的模型名
OPENAI_FALLBACK_MODEL=
OPENAI_WIRE_API=responses
OPENAI_STORE_RESPONSES=false
OPENAI_TIMEOUT_SECONDS=240

AMAP_MAP_KEY=你的高德地图Web服务Key
AMAP_MAPS_API_KEY=
AMAP_MCP_URL=https://mcp.amap.com/mcp
AMAP_MCP_TIMEOUT_SECONDS=20

CITY_DB_PATH=.cache/cityassistant.sqlite3
TRAVEL_PERSISTENCE=1
TRAVEL_DISPLAY_ENABLED=1
TRAVEL_DISPLAY_TIMEOUT_SECONDS=180
TRAVEL_COOKIE_SECURE=0
```

- `OPENAI_WIRE_API` 必须明确填写 `responses` 或 `chat_completions`，不自动选择协议。`OPENAI_STORE_RESPONSES` 必须明确填写 `true` 或 `false`。
- 模型与地图超时必须是有限正数；地图超时不超过 120 秒。开启网页生成时，必须填写展示超时。
- 进程环境变量可代替 `.env` 文件；启动错误只报告变量名称和规则，不回显密钥或连接地址。
- `OPENAI_FALLBACK_MODEL` 不填或留空均关闭备用模型；配置后在主模型限流时最多切换一次。`MODEL_ID` 不再作为主模型的隐式别名。
- `AMAP_MAPS_API_KEY` 非空时优先于 `AMAP_MAP_KEY`；凭证仅由服务端使用。
- `TRAVEL_DISPLAY_ENABLED=0` 关闭后台网页生成，正文仍可交付。
- `TRAVEL_PERSISTENCE=0` 不持久保存状态，仅适合隔离调试；正常使用保持开启。
- 其他配置项见 `.env.example`；实际参数处理以 `backend/app/agents/agent_roles.py`、`backend/app/agents/model_transport.py` 和 `backend/app/agents/tools/amap_mcp.py`、`backend/app/api/tools/amap_mcp.py` 为准。

配置由 `backend/app/core/config.py::load_environment()` 在入口显式加载一次，已有进程环境变量优先（`override=False`）；配置模块及模型/地图适配器的导入本身不读取 `.env`。Web 入口先加载并严格验证配置，再创建全局 Store，模型客户端在 FastAPI lifespan 中创建并供聊天与 HTML 生成复用，关闭时等待活跃借用结束后统一释放。每轮消息、预算、工作区和轨迹仍独立。

独立调用 Agent 的脚本也须先执行 `load_environment()` 和对应范围的 `validate_configuration()`，再在 `app.core.resources.client_scope()` 内运行；业务函数不再偷偷创建临时模型客户端。未启动资源、无密钥或显式禁用时明确失败；默认 pytest 与离线评测不会创建真实客户端。修改连接密钥、地址等配置后需重新启动资源生命周期，不支持运行中切换共享连接。

不要提交 `.env`、SQLite 数据库或包含用户对话的运行记录。

## 启动

### 开发（两个终端，仓库根目录）

后端：

```powershell
& E:/anaconda/envs/TravelAssistant/python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

前端：

```powershell
cd frontend
npm ci
npm run dev
```

浏览器打开 <http://127.0.0.1:5173>，不是后端端口。Vite 将 `/api` 请求（含攻略预览/下载）代理到 FastAPI；健康检查 <http://127.0.0.1:8000/health>。本地前后端均使用 `127.0.0.1`，不要在同一会话混用 localhost 和 IP。

### 构建与部署

```powershell
cd frontend
npm run typecheck
npm test
npm run build
```

本地浏览器端到端验证（使用临时数据库和伪模型/地图，不调用付费服务；需要已安装 Chrome）：

```powershell
# 仓库根目录，先完成 frontend 构建
npm --prefix frontend exec -- playwright test --config frontend/playwright.config.ts
```

部署 `frontend/dist/`，同源反向代理 `/api/` 到 FastAPI；示例见 `docs/deployment/nginx.conf`。开发/预览服务器不是生产服务。所有写 API 验证同源 Origin 和 `X-CSRF-Token`；客户端先 GET `/api/auth/session`，Token 仅放内存，会话 Cookie 保持 HttpOnly。不要配置允许任意来源的 credentialed CORS，不把 `VITE_*` 当秘密存储。

HTTPS 部署显式设 `TRAVEL_COOKIE_SECURE=1`，代理传递可信主机/协议。未知 `/api` 和缺失静态资源不能走 SPA 首页回退；深层页面刷新由前端静态服务处理。

仍只支持单个后端进程，不要增加 `--workers`。`CITY_DB_PATH` 必填，相对路径按仓库根定位，沿用原数据库；不会复制数据库到 backend。Git 只保存源码，不备份业务数据。

## 执行与恢复

### Agent 执行结果与保存

主 Agent 直接返回 `ExecutionResult`：回复正文、可选的完整变更包、失败状态和原因。`backend/app/workflow/execution.py` 定义轻量请求快照、执行结果和持久化回执；行程内容摘要位于 `backend/app/services/itinerary/plan_basis.py`，供任务和保存时检查业务版本。

已删除 `agent_protocol.py` 和 `turn_workspace.py`，不再注册候选、选择候选 ID、冻结嵌套协议或对进程内提案签名。保存函数在锁内检查当前任务、请求和业务版本，复验变更包的原话、证据、目标及摘要，随后原子持久化。历史任务回执保持可读，执行结果不能改写历史工作流。

### 用户消息校验

前端负责非空与 10,000 字符限制的即时提示，后端消息规则统一定义在 `backend/app/core/message_input.py`。聊天接口和兼容任务入口复用相同规则：消息必须是字符串，原始长度不超过 10,000 字符，去首尾空白后非空；不截断超长内容。任务在去重前规范化消息，避免同一请求因首尾空白产生内容冲突。`RequestContext` 复用公共长度与类型检查，保留内部空原话操作的兼容性，不改写授权原文。

登录、旅行归属与 CSRF 仍由已有公共入口检查；工具参数和保存时的授权、版本校验属于后续执行检查。消息错误返回明确中文原因，不回传用户原文或其他校验上下文。

### 当前聊天：唯一业务操作入口

用户只需发消息，即可咨询、补充需求、匹配住宿、管理想法、生成或修改行程、确认、清空以及调整攻略网页。新建对话直接进入聊天；不再要求填写设置表单或选择业务入口。前端想法墙保留，自由要求条目与结构化需求统一作为 Agent 的 requirements 输入；想法页生成快捷操作发送普通聊天消息。攻略预览和下载保留。

Agent 可自由回答非旅行问题，并拥有 `read_context`、天气、地点、餐饮、路线和 `submit_changes` 六个工具。查询不会自动写入业务数据；只有明确授权并通过服务器校验的变更才会保存。住宿只能引用本轮真实地图查询地点，重名在聊天中澄清。保存行程或调整网页后通知后台攻略生成。

模型输入中的 current_date 直接提供当前日期，不要求模型根据页面入口判断用户意图。工具串行执行，不设固定聊天步数或调用次数；保留单次模型配置超时、360 秒整轮期限、取消、账号隔离、参数和真实 POI 校验，重复无效调用会终止。历史任务接口保留兼容。

### 事务保存与攻略生成

执行策略固定为 `task_execution`：一个 `travel_agent` 在同一有界循环中读取需求、直接查询地点/天气/交通，并生成或修改行程；不再委派需求、查询、规划、修改子 Agent。工具定义、Schema 和实现由注册表绑定。`submit_changes` 是终止工具：一轮最多提交一个完整业务变更包，内部直接返回变更包，由 `TurnCommit` 复验并原子保存。非法子操作导致整包拒绝，不隐式保留部分写入；保存成功文案以实际提交结果为准。历史策略标记仍归一为 `task_execution`，不保留旧框架切换。

正文先保存，HTML 网页独立派生。最后一个 `display` Agent 根据正文、日期与展示偏好生成完整 HTML/CSS；网页在无脚本、无同源权限的 iframe 中预览。生成失败可重试并阅读原文；风格重生失败时保留来源仍匹配的上一份网页。新消息会替代正在执行的旧轮；版本变化、清空聊天或新编辑使旧结果失效。失败保留已有方案和输入，可重试；服务重启不自动重跑写入任务。清空聊天保留需求和当前行程，但撤销待补充目标与未完成任务。

聊天中的执行过程按真实模型与工具事件更新，展示需求整理、天气／地点／交通查询等步骤及耗时。执行时展开，结束后可再次展开回看；失败、暂无结果和被新消息替代分别标注。过程随助手消息持久保存，旧消息没有记录时保持原展示。

Agent 读取当前已保存行程完整正文和有界想法摘要，更多内容可分页读取；整理状态不代表行程已采用。已移除隐藏的第二次回复复核调用，以及模型管理的 todo/跨轮任务编排工具。只读答复或澄清不会自行写业务数据。

想法墙也是旅行输入。完整生成提交时，Agent 须逐项返回想法处理结果：已安排、存在冲突、需补充信息或不适用，并说明依据；已安排项须摘录新行程的对应文字。程序检查卡片引用覆盖与正文摘录，卡片不是已核实事实，也不扩大局部修改、预订或支付授权。

一轮可以多次查询，但合法 `submit_changes` 会立即结束模型执行；首版不支持保存后同轮继续规划。规划未完成时可明确仅提交需求，整包提交失败则不能暗中保存需求子集。新消息、业务版本和持久化检查保护旧状态，但不保证模型内容完全正确；保存成功或网页就绪不代表事实、预算或现实可执行性已核验。

## 任务接口

以下接口要求登录并拥有对应旅行，具体参数校验以 `backend/app/api/` 为准：

| 接口 | 用途 |
| --- | --- |
| `POST /api/trips/conversations` | 创建无需预填住宿或日期的新对话 |
| `POST /api/trips/{trip_id}/messages` | 发送用户消息，由 Agent 理解并处理意图 |
| `POST /api/trips/{trip_id}/tasks` | 提交 `kind`（chat/plan/revision）、`payload` 和稳定的 `request_id`；新任务返回 202 |
| `GET /api/trips/{trip_id}/tasks` | 查询近期任务及状态 |
| `POST /api/trips/{trip_id}/tasks/{task_id}/retry` | 重试失败任务，使用新的 `request_id` |
| `GET /api/trips/{trip_id}/plan-state` | 查询当前正文绑定与攻略网页状态 |
| `POST /api/trips/{trip_id}/display` | 只重试当前正文的攻略网页，不重新规划 |
| `GET /api/trips/{trip_id}/guide` | 按计划 ID、正文哈希与网页版本预览；`download=1` 下载 HTML |

## 项目导航

| 路径 | 用途 |
| --- | --- |
| [产品说明](docs/PRODUCT.md) | 产品目标、当前业务规则、兼容与非目标 |
| [界面规范](docs/DESIGN.md) | 既有视觉、排版及交互状态保护 |
| `backend/app/main.py`、`backend/app/api/` | 应用装配、JSON API 与共享请求依赖 |
| `backend/app/core/`、`backend/app/auth/` | 运行配置、共享客户端与账号认证 |
| `backend/app/storage/store.py`、`backend/app/storage/persistence.py` | 业务状态与 SQLite 持久化 |
| `backend/app/agents/prompts/` | 当前角色提示词及合成示例 |
| `frontend/src/pages/`、`frontend/src/components/`、`frontend/src/assets/` | Vue 页面、组件与样式 |

文档分类见 [docs/README.md](docs/README.md)：旧架构提案保存在 archive，学习与求职资料分别在 learning、career；历史描述不作为当前实现或测试通过依据。

## 当前测试入口

使用项目固定的 conda 解释器，在项目根目录执行：

```bash
# 离线自动化：伪模型、工具和临时数据库，不调用真实模型。
E:/anaconda/envs/TravelAssistant/python.exe -m pytest tests/automated -q

# 范围评测入口：不带 --live 时只检查/输出数据，不发付费模型请求。
E:/anaconda/envs/TravelAssistant/python.exe -m tests.scope_eval
E:/anaconda/envs/TravelAssistant/python.exe -m tests.scope_suite
```

范围评测源码为 `tests/scope_eval.py` 与 `tests/scope_suite.py`，具体参数以 `--help` 为准。真实模型评测须显式传 `--live`，会把输入发送到配置的服务并可能计费，不属于默认离线验证。评测使用隔离数据和不可用的外部事实替身，不证明真实地图、SQLite 提交或网页链路通过；语义结果仍需人工审阅。

当前仓库不包含旧 README 所列的 `tests.main_agent_eval.runner`，不要使用该历史命令。离线测试通过不等于真实模型路线质量或现实可执行性通过。

文档及代码变更可检查：

```powershell
git diff --check
```

## 数据与安全边界

- 登录账号的旅行、消息、想法、需求、当前方案和任务状态保存到 SQLite；备份由使用者管理。
- 标准异常日志避免输出凭证；Agent 运行记录可能包含用户输入、模型输出、工具过程和用量，保存在业务库 `agent_runs`，应视为敏感数据，保留周期尚待完善。
- 模型输入会发送到所配置的模型服务；地点、天气和路线查询会访问相应外部服务。不要把本地运行理解为全部数据离线。
- 地点匹配不证明营业、预约、遮雨或门票；天气受预报可查询范围限制。日期未定时不默认今天或查询当天预报。
- 地图或模型不可用时保留输入和当前方案，明确失败或待核实；不能依靠模型补造事实来绕过服务失败。
