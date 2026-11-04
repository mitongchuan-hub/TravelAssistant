# TravelAssistant · 同城游玩

以已确认的住宿为起终点，生成、阅读和调整同城游玩路线。使用 Vue 3、TypeScript、Vite、FastAPI JSON API 和 SQLite，当前按单进程运行。

## 使用方式

1. 注册或登录，点击“新建旅行”，在原有表单填写住宿地址或酒店名称、出行方式、开始日期与游玩天数；住宿有多个地图候选时，选择具体位置后确认创建。
2. 保存成功后进入聊天，创建资料固定；人数、预算、每日时刻、想去哪里和偏好等统一维护为一份需求正文，可通过聊天或想法页编辑。
3. 发消息说“生成路线”，修改、确认和网页重新生成也通过消息提出。新执行任务只使用 `chat`，主 API 为 `/messages`；旧 `plan/revision` 任务保留历史读取。失败后重新发送新的聊天，不提供失败任务专用重试。
4. 行程正文保存后，网页 Agent 在后台生成 HTML/CSS 攻略；聊天页点击“攻略”预览，也可以打开独立网页或下载 HTML。
5. 通过聊天修改方案；只想换颜色、字号或排版时可直接说“把攻略改成手账风”。纯风格修改保持行程内容与确认状态，内容修改后重新生成网页。设置变化不会自动重排行程。

产品面向手机浏览器，工作区采用最多 430px 的单列布局。工作区保留“想法／聊天”切换，想法墙展示和管理自由要求；点击“攻略”进入预览，顶部左侧返回聊天、右侧下载当前 HTML，网页可用时显示下载入口。宽屏浏览器使用相同的手机交互。旧行程链接仍会打开攻略预览。每日从同一住宿出发并返回，未知地点保持待核实；网页只复用正文已提供的 HTTPS 链接。查询耗时只是查询时估算，不保证未来开放、班次、票价或现实可执行性。

项目只提供从住宿出发并返回的同城游玩。住宿、出行方式、开始日期和天数在创建后固定，聊天不能修改。历史记录的需求、聊天和行程继续保留；基础信息不完整的旧旅行不能生成或重排行程，请通过完整表单新建旅行。每趟旅行保留一份当前行程。撤销入口暂时隐藏，后端仍保留幂等恢复；终止不会自动撤回已提交修改。

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
│   ├── storage/              # 活动状态、SQLite 任务仓储与业务快照
│   ├── views/                # 保留的纯展示/历史兼容处理
│   └── models.py             # 业务数据结构
├── tests/                    # Python 自动化与离线评测
│   ├── browser/server.py     # 浏览器测试专用后端服务
│   └── benchmarks/agent_latency.py # 模型调用耗时评测（默认离线）
├── docs/
└── .env                      # 本地配置，不提交
```

前端只访问相对 `/api` 路径；模型与地图密钥始终留在后端。地图连接和地点解析统一位于 `backend/app/services/maps/`，Agent 与兼容 API 复用；模型直接使用筛选后的高德原生工具，天气保持独立实现。生成的 HTML 攻略不是 Vue 模板：仍从受保护的文档端点加载进无脚本/无同源权限 iframe。

修改规则、数据保存和 Agent 执行分别查看 `services/`、`storage/`、`agents/`；页面结构和浏览器交互查看 `frontend/src/`。迁移合同见 [前后端迁移说明](docs/frontend-backend-migration.md)。

后端公开返回结构集中在 `backend/app/api/schemas.py`，由 serializers 的白名单投影生成；这些 TypedDict 只明确类型，不替代请求运行时校验或自动补值。行程依据是否过期统一由 `services/itinerary/freshness.py` 判断，工作流不依赖展示模块。演示业务数据的纯工厂位于 `storage/demo_data.py`，Store 仍负责状态和持久化协调。

Agent 入口与模型基础设施职责如下（运行器和工具的业务行为不变）：

| 文件（`backend/app/agents/`） | 职责                                                                                                      |
| ------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `entrypoints.py`              | `generate_task_result` / `generate_plan_display`，装配本轮轨迹、预算、客户端借用与备用模型配置        |
| `trace.py`                    | `ConversationTrace`、进度回调和运行记录持久化                                                           |
| `model_client.py`             | `request_completion`，固定参数发送、请求 ID、预算计时、usage 与单请求 429 备用                          |
| `model_transport.py`          | Chat Completions / Responses 线协议适配                                                                   |
| `inputs/execution.py`         | 执行数据装配：定义 `TaskExecutionContext`，从显式业务对象深复制本轮快照、仅保留 sent 消息，不接收 Store |
| `inputs/context.py`           | 模型输入装配：将执行快照投影为主 Agent 与 `read_context` 可见的上下文，不授予写入权限                   |

`workflow/turn_commit.py::execute_turn()` 在锁内解析展示目标并调用执行装配，固定本轮 payload 对应的条目与目标；可信 `RequestContext`、授权与版本冲突检查、取消/进度回调和最终原子保存仍留在 workflow 外层。`inputs/` 不反向依赖 workflow 任务执行器。

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
AMAP_REST_TIMEOUT_SECONDS=10

CITY_DB_PATH=.cache/cityassistant.sqlite3
TRAVEL_PERSISTENCE=1
TRAVEL_DISPLAY_ENABLED=1
TRAVEL_DISPLAY_TIMEOUT_SECONDS=180
TRAVEL_COOKIE_SECURE=0
```

- `OPENAI_WIRE_API` 必须明确填写 `responses` 或 `chat_completions`，不自动选择协议。`OPENAI_STORE_RESPONSES` 必须明确填写 `true` 或 `false`。
- 模型与地图超时必须是有限正数；地图超时不超过 120 秒。开启网页生成时，必须填写展示超时。
- 进程环境变量可代替 `.env` 文件；启动错误只报告变量名称和规则，不回显密钥或连接地址。
- `OPENAI_FALLBACK_MODEL` 不填或留空均关闭备用模型；配置后每个失败的主模型 HTTP 429 请求最多使用备用模型重试一次，并非整轮只能切换一次；仍受共享预算约束，不重放已执行的工具。`MODEL_ID` 不再作为主模型的隐式别名。
- 固定模型参数（包括 `reasoning_effort`、工具和 `response_format`）被端点拒绝时直接报错，不静默删除、替换参数或降低响应格式后重试。
- `OPENAI_MAIN_ENABLE_THINKING` 仅对 `chat_completions` 的主旅行 Agent 生效，通过 `chat_template_kwargs.enable_thinking` 显式控制；`responses` 不发送该开关。未配置时保留服务端默认思考行为。
- `AMAP_MAPS_API_KEY` 非空时优先于 `AMAP_MAP_KEY`；凭证仅由服务端使用，同一 Web 服务 key 需有 MCP 与 REST 地点详情权限及额度。`AMAP_REST_TIMEOUT_SECONDS` 是新增可选 REST HTTP 分阶段超时，未配置默认10秒（显式填写须有限正数、最多120秒，空白无效），并取剩余预算/住宿查询 deadline 的较小值；同步在途请求不能立即取消，不是严格墙钟截止承诺。
- `TRAVEL_DISPLAY_ENABLED=0` 关闭后台网页生成，正文仍可交付。
- `TRAVEL_PERSISTENCE=0` 不持久保存状态，仅适合隔离调试；正常使用保持开启。
- 其他配置项见 `.env.example`；实际参数处理以 `backend/app/agents/roles.py`、`backend/app/agents/model_transport.py` 和 `backend/app/services/maps/amap_mcp.py` 为准。

配置由 `backend/app/core/config.py::load_environment()` 在入口显式加载一次，`.env` 中已定义的配置优先于同名进程环境变量（`override=True`）；`.env` 未定义的配置仍可由进程环境提供；配置模块及模型/地图适配器的导入本身不读取 `.env`。Web 入口先加载并严格验证配置，再创建全局 Store，模型客户端在 FastAPI lifespan 中创建并供聊天与 HTML 生成复用，关闭时等待活跃借用结束后统一释放。每轮消息、预算、工作区和轨迹仍独立。

独立调用 Agent 的脚本也须先执行 `load_environment()` 和对应范围的 `validate_configuration()`，再在 `app.core.resources.client_scope()` 内运行；业务函数不再偷偷创建临时模型客户端。未启动资源、无密钥或显式禁用时明确失败；生产模型配置不识别 pytest。自动测试通过测试夹具禁用真实模型、拦截真实客户端与网络，需验证生命周期时显式注入假客户端；离线评测不启动模型资源。修改连接密钥、地址等配置后需重新启动资源生命周期，不支持运行中切换共享连接。

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

浏览器打开 [http://127.0.0.1:5173](http://127.0.0.1:5173)，不是后端端口。Vite 将 `/api` 请求（含攻略预览/下载）代理到 FastAPI；健康检查 [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health)。本地前后端均使用 `127.0.0.1`，不要在同一会话混用 localhost 和 IP。

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

主 Agent 直接返回 `ExecutionResult`：回复正文、可选的完整变更包、失败状态和原因。`read_context` 与 `save_trip_changes` 的工具定义、参数 Schema 和执行入口分别位于 `backend/app/agents/tools/read_context.py`、`save_trip_changes.py`；注册表统一注册，运行器绑定本轮快照与预算并调度执行。工具不直接操作 Store，读取投影和业务校验继续复用既有模块。`backend/app/workflow/execution.py` 定义轻量请求快照、执行结果和持久化回执；行程内容摘要位于 `backend/app/services/itinerary/plan_basis.py`，供任务和保存时检查业务版本。

已删除 `agent_protocol.py` 和 `turn_workspace.py`，不再注册候选、选择候选 ID、冻结嵌套协议或对进程内提案签名。保存函数在锁内检查当前任务、请求和业务版本，复验变更包的原话、证据、目标及摘要，随后原子持久化。历史任务回执保持可读，执行结果不能改写历史工作流。

### 用户消息校验

前端负责非空与 10,000 字符限制的即时提示，后端消息规则统一定义在 `backend/app/core/message_input.py`。聊天接口和兼容任务入口复用相同规则：消息必须是字符串，原始长度不超过 10,000 字符，去首尾空白后非空；不截断超长内容。任务在去重前规范化消息，避免同一请求因首尾空白产生内容冲突。`RequestContext` 复用公共长度与类型检查，保留内部空原话操作的兼容性，不改写授权原文。

登录、旅行归属与 CSRF 仍由已有公共入口检查；工具参数和保存时的授权、版本校验属于后续执行检查。消息错误返回明确中文原因，不回传用户原文或其他校验上下文。

### 创建表单与后续 Agent 聊天

新建旅行先使用既有表单填写住宿、出行方式、日期和天数，确认地图住宿后保存并进入聊天；打开表单本身不会创建空旅行。创建后基础资料固定，用户通过消息咨询、维护唯一需求正文、生成或修改行程、确认、清空以及调整攻略网页，Agent 任务仍只有 `chat`。想法页保留导航与生成快捷入口，查看和编辑同一份需求正文，不再维护卡片列表。正文手动保存绑定需求版本，冲突保留草稿。攻略预览和下载保留。

Agent 按意图受理旅行咨询与合理辅助（如旅行费用计算、菜单翻译），不提供完整菜谱、独立解题、代码或诗歌等非旅行实质回答；饮食偏好和餐厅查询合法，裸菜名结合上下文澄清，混合请求只处理旅行部分，不能借工具转述或业务写入绕过范围。Agent 拥有 `read_context`、只读的 `read_skill`、`save_trip_changes`、独立的 `query_weather` 和 6 个高德原生工具，共 10 个工具。高德原生工具为地点搜索 `maps_text_search`、周边搜索 `maps_around_search`、详情 `maps_search_detail`、步行 `maps_direction_walking`、驾车 `maps_direction_driving` 和公交 `maps_direction_transit_integrated`；餐厅通过地点搜索与详情查询，不再维护单独餐饮工具。查询不会自动写入业务数据；只有明确授权并通过服务器校验的变更才会保存。住宿在创建时由真实地图查询和候选确认固定，Agent 后续只能读取，不能替换。保存行程或调整网页后通知后台攻略生成。

信息足够且已授权生成时，不逐项追问兴趣；未说明部分暂按首次到访视角、适中节奏，有条件兼顾代表景点、本地美食、特色街区和文化，不硬凑。行程用一句话说明实际假设，不当作用户事实或写入需求正文，不推断人数、预算、健康。安全、范围、同城和固定创建资料优先，明确偏好与禁忌高于默认；路线尽量就近少折返，但预约、开放和指定顺序优先，现实冲突说明后让用户取舍。规则送达的离线合同与 `tests/scope_eval.py` 中规划用例的真实模型语义评审分开，前者通过不证明后者。

模型输入中的 current_date 直接提供当前日期，不要求模型根据页面入口判断用户意图。工具串行执行，不设固定聊天步数或调用次数；保留单次模型配置超时、360 秒整轮期限、取消、账号隔离、参数和真实 POI 校验，重复无效调用会终止。历史任务接口保留兼容。

### 应用内旅行规划 Skill

`backend/app/agents/skills/travel-planning/SKILL.md` 承载同城首次、多日与局部修改的方法；主提示词保留安全、授权、事实和提交契约，仅常驻同源技能简介。Agent 使用 `read_skill(name="travel-planning", resource="main")` 读取主文，按需读取 `evidence-and-routing`、`output-and-revision` 参考资源；同轮已加载内容直接复用。普通咨询、单次查询、只记需求、精确改字、确认和清空无需加载。

资源为固定白名单，不提供任意路径或脚本执行；读取占工具调用、不计外部查询，不进入事实证据或修改保存流程。方法和示例不授予生成权限，也不代表现实查询结果。技能场景定义位于该目录的 `evals/evals.json`，当前标为 `not_run`；离线接入通过不证明真实触发或规划质量提升。设计与评测边界见 `docs/superpowers/specs/2026-11-04-travel-planning-skill-design.md`。

### 地图工具与天气

6 个地图工具名称和参数保留在 `backend/app/agents/tools/amap_tools.json`。搜索继续使用 MCP 并验证远程实时 Schema；步行、驾车、公交固定单次 REST v3 `/direction/walking`、`/direction/driving`、`/direction/transit/integrated`，不走 MCP 或跨版本 fallback；`maps_search_detail({id})` 保留本地参数及可信 ID 校验，底层固定单次 HTTPS GET `/v3/place/detail?extensions=all`，不再调用 MCP detail 或远程详情 Schema。无重定向、自动重试、MCP fallback 或 v5 拼接；失败明确 unavailable。Agent 搜索不自动拉全部详情。周边搜索只接受固定已确认住宿或成功同ID详情的真实中心（可复用服务器保存的详情证据）；搜索候选坐标、入口/出口不授予中心权限。未指定 radius 时省略，不猜半径；候选按需单次REST详情，搜索距离不等于步行路线或时间，重名和指代不明先澄清。创建住宿时 MCP 搜索 session 先结束，再复用同一 REST 详情函数，最多尝试5个去重ID（失败占名额），与搜索共享单调 deadline。

模型收到搜索/路线原始结果，以及详情顶层可见的实际行政区、电话、网站、多图、biz_ext营业/评分/消费参考、入口/出口及导航标识。详情 metadata 标注 `amap_rest`、`v3`、`place_detail` 与查询 `fetched_at`，上游 `timestamp` 独立保留，`raw` 为同次脱敏 REST envelope；响应体最多2 MiB，不抓网站或下载图片。缺项/空值保持未知、真实零值不丢弃；营业描述不保证当天开放或预约，消费参考不是门票价，网站不保证官方。可信 candidate 仅保留既有 PlaceDetails 字段，source 仍为“高德地图 POI”。入口不替换中心坐标，不移动已确认住宿。成功同ID详情可派生绑定该次证据的 `navigation_endpoints`，路线平铺参数 `origin_ref` / `destination_ref` 可引用服务器 ref；出发使用center/exit，到达使用center/entrance，坐标与ref冲突拒绝，入口不授予周边中心权限。同POI不同端点仍实际查路线，证据及导航链接使用实际端点；入口存在不代表开放或可通行。历史详情只读派生ref、不改写原记录。

公交可选 `strategy` 为官方枚举0/1/2/3/5；当前无已验证的日期查询支持窗，提供 `departure_at` 时明确 unavailable，不发送请求、不静默查询今天。不提供时间时仅为查询时参考，不确证旅行日期未来班次。结果分开记录 requested_departure、actual_parameters、查询时刻及query_semantics；同方案duration/segments/fare_yuan保持一致，空票价未知、不拼备选线路。v5公共详情权限与business/navi已探测，但真实children链未验证，详情仍保持v3，不宣称已支持子POI授权。

服务器解析地点中心与路线为业务证据，保留 raw/result 与 normalized，旧 MCP 证据继续可读。路线坐标须属于已查询地点中心或已确认住宿，公交城市须对应真实地点。相同成功查询本轮缓存、失败不缓存；一次详情HTTP只扣一次 external预算，预算/取消异常不转服务失败。REST共享同步HTTP资源在lifespan中创建、借用排空后关闭；独立入口须显式使用 `app.services.maps.amap_rest.client_scope()`，再调用 `detail(poi_id)`，未启动时明确不可用。

天气继续由 `backend/app/agents/tools/weather.py` 的原服务实现查询，不改用高德天气。原生工具减少维护包装，但搜索与详情拆开可能增加模型调用次数，不能据此宣称更快或更准确。

### 事务保存与攻略生成

执行策略固定为 `task_execution`：一个 `travel_agent` 在同一有界循环中读取需求、直接查询地点/天气/交通，并生成或修改行程；不再委派需求、查询、规划、修改子 Agent。工具定义、Schema 和实现由注册表绑定。`save_trip_changes` 用于用户明确记录、增删改需求，或生成、修改、确认、清空行程；仅查询、解释或澄清时不用，固定创建资料不可改。需求提交完整正文并保留未授权更改的旧要求。它是唯一保存终止工具（不注册旧名 `submit_changes` 别名）：一轮一次提交全部授权变更，内部直接返回变更包并结束模型执行，由 `TurnCommit` 复验并原子保存。历史 trace 仅保留旧工具标签的只读展示兼容，不改写历史 payload 或回执。非法子操作导致整包拒绝，不隐式保留部分写入；保存成功文案以实际提交结果为准。历史策略标记仍归一为 `task_execution`，不保留旧框架切换。

正文先保存，HTML 网页独立派生。展示执行器直接接收正文字符串，返回 `display`、`display_source_hash`、`display_status`，失败时附安全错误信息；不再借用包含业务和工作流字段的旧 `AgentReply`。最后一个 `display` Agent 根据正文与日期，使用默认“轻松、愉悦”风格生成完整 HTML/CSS；网页在无脚本、无同源权限的 iframe 中预览。网页生成失败可通过新消息要求重新生成，并可阅读原文；失败时保留来源仍匹配的上一份网页。运行中新消息进入同旅行 follow-up 等待队列，不替代或注入当前轮；激活时读取最新已保存业务状态。当前轮成功且执行真正退出后自动处理下一条；失败、手动终止或服务重启后暂停队列，用户明确继续才恢复，不重跑失败项。有效手动修改会使当前旧结果失效并暂停等待队列。清空聊天保留需求和当前行程，取消全部未完成消息并拒绝旧页面迟到的新提交。

聊天中的执行过程按真实模型与工具事件更新，展示需求整理、天气／地点／交通查询等步骤及耗时。执行时展开，结束后可再次展开回看；失败、暂无结果和被新消息替代分别标注。过程随助手消息持久保存，旧消息没有记录时保持原展示。

Agent 读取固定创建资料、完整需求正文和当前已保存行程。需求正文以完整替换表达新增、修改和删除，空字符串表示清空；不再维护卡片 ID、分类、逐卡处理结果或分页摘要。只读答复或澄清不会自行写业务数据。

生成行程绑定当时的完整需求快照与版本。仅更新需求会保留旧行程并使其依据过期，不自动重排；同轮明确授权更新需求并生成时，两者原子保存。需求是否被完整、正确地落实仍需语义评审，版本绑定不等于事实或路线质量验收。

一轮可以多次查询，但合法 `save_trip_changes` 会立即结束模型执行；首版不支持保存后同轮继续规划。规划未完成时可明确仅提交需求，整包提交失败则不能暗中保存需求子集。新消息、业务版本和持久化检查保护旧状态，但不保证模型内容完全正确；保存成功或网页就绪不代表事实、预算或现实可执行性已核验。

## ID 职责

- `trip_id`：旅行资源归属。
- `request_id`：客户端提交的幂等键；网络不确定时重发复用同一值。
- `execution_task_id`：服务器实际执行作业的身份；重新发送新 chat 创建新的任务身份。
- `message_id`：兼容字段名，指触发执行的用户原始消息。
- `thinking_id`：兼容字段名，指执行期间的助手占位消息，完成后更新为回复。
- `conversation_task_id`：旧工作流关联字段，不是第二个请求 ID；当前仅用于已有会话问题关联。

网络送达不确定时保留 pending，使用相同的 `request_id` 恢复和去重，已确认失败的任务不会重新执行。服务端不提供失败任务重试：提示“本次处理未完成，请重新发送消息。已有内容已保留。”用户重新发送是新的 chat，创建新的 request/task/用户消息 ID，不写入 `retry_of`。历史任务（含 plan/revision 和 `retry_of`）继续可读，不改写历史数据。undo 网络幂等、HTML 结构纠错和 429 备用请求不受影响。

### Follow-up 等待队列

运行中可以继续发送消息；每条消息独立授权、独立任务，同旅行按服务器接受顺序执行。等待消息可取消，不支持原地编辑；每旅行最多50条等待项。取消等待项不打断当前任务，停止当前任务则暂停队列。暂停后新发消息只排队，点击“基于当前保存内容继续”才恢复；在途模型请求未退出时不会启动下一条。等待消息不会提前进入当前 Agent 的上下文。本功能不包含 steer，也不是跨进程的可靠任务系统。

## 任务接口

以下接口要求登录并拥有对应旅行，具体参数校验以 `backend/app/api/` 为准：

| 接口                                                 | 用途                                                                                     |
| ---------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `POST /api/trips`                                  | 新建旅行表单提交；住宿重名时返回候选，再用 choice_token 与 poi_id 确认创建               |
| `PUT /api/trips/{trip_id}/requirements-text`       | 用 `text` 与 `expected_revision` 更新唯一需求正文，不修改固定创建资料                    |
| `POST /api/trips/{trip_id}/messages`               | 发送用户消息，由 Agent 理解并处理意图                                                    |
| `POST /api/trips/{trip_id}/tasks`                  | 兼容提交，仅接受 `kind=chat`、`payload.body` 和稳定的 `request_id`；新任务返回 202 |
| `GET /api/trips/{trip_id}/tasks`                   | 查询近期任务及状态；`?request_id=...` 精确确认历史请求是否已受理                       |
| `POST /api/trips/{trip_id}/tasks/{task_id}/cancel` | 取消指定等待项或终止当前执行；终止当前轮会暂停队列                                     |
| `GET /api/trips/{trip_id}/queue`                  | 完整等待队列、当前执行、暂停原因及队列版本，不受近期20项历史窗口限制                   |
| `POST /api/trips/{trip_id}/queue/resume`          | 按 `expected_revision` 与 `chat_generation` 显式继续，不重跑失败项                     |
| `GET /api/trips/{trip_id}/plan-state`              | 查询当前正文绑定与攻略网页状态                                                           |
| `POST /api/trips/{trip_id}/display`                | 只重试当前正文的攻略网页，不重新规划                                                     |
| `GET /api/trips/{trip_id}/guide`                   | 按计划 ID、正文哈希与网页版本预览；`download=1` 下载 HTML                              |

## 项目导航

| 路径                                                                            | 用途                                 |
| ------------------------------------------------------------------------------- | ------------------------------------ |
| [产品说明](docs/PRODUCT.md)                                                      | 产品目标、当前业务规则、兼容与非目标 |
| [界面规范](docs/DESIGN.md)                                                       | 既有视觉、排版及交互状态保护         |
| `backend/app/main.py`、`backend/app/api/`                                   | 应用装配、JSON API 与共享请求依赖    |
| `backend/app/core/`、`backend/app/auth/`                                    | 运行配置、共享客户端与账号认证       |
| `backend/app/storage/store.py`、`backend/app/storage/persistence.py`        | 业务状态与 SQLite 持久化             |
| `backend/app/agents/prompts/`                                                 | 当前角色提示词及合成示例             |
| `frontend/src/pages/`、`frontend/src/components/`、`frontend/src/assets/` | Vue 页面、组件与样式                 |

文档分类见 [docs/README.md](docs/README.md)：包括当前规范、后端架构、测试与调试证据、设计预览及历史研究和面试资料；历史描述不作为当前实现或测试通过依据。

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

- 登录账号的旅行、消息、想法、需求、当前方案和任务状态保存到 SQLite；备份由使用者管理。任务历史由独立任务表按需查询，内存活动字典仅保留排队/执行中的任务；任务终态与业务变更同事务保存后移出活动字典。旧快照任务升级前自动备份、一次性迁移，重启不全量加载历史或自动重跑中断任务。这仍是单进程后台执行，不是独立可靠任务队列。
- 规范需求仅保留日期、天数、城市、交通方式、住宿地址和修订号，日期摘要统一派生；更多设置及预算／风格／备注字段不再由模型、API或数据库投影维护。schema9升级前备份，清除旧可选字段与旧撤销条目，保留已消费撤销回执；升级后新的业务修改仍支持三步撤销。原行程正文与合法网页保留，旧过期／不完整依据不会因删字段变为有效。业务模型升级与任务历史迁移分开处理，不重写历史任务回执或摘要。
- 标准异常日志避免输出凭证；Agent 运行记录可能包含用户输入、模型输出、工具过程和用量，保存在业务库 `agent_runs`，应视为敏感数据，保留周期尚待完善。
- 模型输入会发送到所配置的模型服务；地点、天气和路线查询会访问相应外部服务。不要把本地运行理解为全部数据离线。
- 地点匹配不证明营业、预约、遮雨或门票；天气受预报可查询范围限制。日期未定时不默认今天或查询当天预报。
- 地图或模型不可用时保留输入和当前方案，明确失败或待核实；不能依靠模型补造事实来绕过服务失败。
