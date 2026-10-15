# TravelAssistant

TravelAssistant 是一个私人旅行规划 Agent。先识别闲聊、查询、规划、修改四类意图，使用无工具 LLM 或业务 Agent Loop 完成请求。生成和修改直接交付完整文字，经需求与计划快照检查后保存；独立后台展示任务随后整理卡片。行程页先读原文，卡片完成后局部补齐，并保留当前查询依据。

项目使用 FastAPI 提供本地 Web 应用，使用 SQLite 保存旅行、对话、想法、当前行程和登录会话。

当前说明、实施验收与历史证据的入口见[文档索引](docs/README.md)。

首版产品范围已确认：中国大陆单城及可当天返回的近郊、1—7天普通自由行，默认地铁优先，完整约定见[首版产品边界](docs/product-boundaries-v1.md)。范围核验、地铁优先策略及移除确认操作等列为后续实施项；文字先读与后台卡片已实施，以下记录当前代码功能。

## 功能

- **旅行需求档案**：在聊天页点击“旅行需求”，维护旅游城市、抵离日期与时刻、可选抵离地点、人数、预算金额与口径、必去地点、避开事项、饮食禁忌和节奏。清空聊天后仍保留，生成和调整时优先使用当前有效需求。
- **旅行想法**：用想法卡片记录地点、预算、节奏和禁忌。所有想法完整保存，白板保留原布局并展示最近 8 条；需求页下方的“全部想法”可查看与删除更早的内容。
- **Agent 聊天**：创建后的欢迎消息确认已保存信息，必填完整时可直接生成，只把必去地点、忌口和节奏作为可选补充。意图识别、闲聊和业务Agent均读取当前需求档案，长对话或清空聊天后仍可读取。通过对话提取旅行约束，模型不可用时保留消息并明确提示失败。
- **创建行程**：只填写城市、起止日期、抵离时间与地点、人数、预算金额/口径/费用范围五组必填信息。浏览器和服务端同时校验，创建后直接保存有效需求。
- **文字与卡片行程**：生成和修改直接保存完整原文并完成主任务，保存时核对当前需求与计划快照。聊天预览原文中的主线、预算判断和关键条件，完整行程可展开。独立后台 LLM 整理按天卡片；失败保留原文并可只重试卡片。原文、展示与依据绑定当前计划，旧卡片不会覆盖新方案。没有额外的模型复核或依据复核意见自动重写步骤；保存成功不保证全部事实与可执行性。
- **按需查询**：Agent 使用天气、地点、美食和路线工具，根据旅行语境选择资料并说明来源与未知事项。工具不可用时明确待核实，不要求用户逐个手动查询。
- **抵离与旅行约束**：当前有效需求完整交给业务 Agent，按当地时间安排首末日、当天往返不住宿，不查询具体火车班次。新文字行程不再做按活动字段执行的日期、时间或路线窗口自动校验。
- **旧行程兼容**：已有时间线、旅行准备、地点地图和路线卡片继续展示；当前结构化行程仍能读取；修改后以新的文字方案替换当前行程。
- **可恢复任务**：聊天、生成和调整保存任务状态，刷新后可查询结果；请求标识去重。思考中可以继续发送，新消息立即替代旧轮并发起独立请求，旧回复和变更作废；不等待旧网络请求结束。失败可重试，服务重启中断不自动重跑。
- **只保留最新版**：每趟旅行保存一份当前行程，生成和修改直接替换。确认只改变当前状态，没有历史列表或恢复入口。
- **模型失败保护**：支持主模型和限流时的备用模型；超时、空回复或需要用户补充时保留当前行程。保存时核对需求与计划快照，拒绝旧结果覆盖新编辑或确认。

## 环境要求

- Python 3.11 或更高版本
- Anaconda 或其他虚拟环境
- Node.js（只在运行浏览器回归测试时需要）
- 可访问模型服务的 API Key

## 安装

创建并激活 Python 环境后安装依赖：

```powershell
python -m pip install -r requirements.txt
```

复制环境变量模板：

```powershell
Copy-Item .env.example .env
```

然后编辑 `.env`：

```env
OPENAI_API_KEY=你的模型服务密钥
OPENAI_BASE_URL=https://mirror.xinshu.ai/v1
OPENAI_MODEL=gpt-5.6-sol
OPENAI_FALLBACK_MODEL=
OPENAI_WIRE_API=responses
OPENAI_STORE_RESPONSES=false
```

`OPENAI_BASE_URL` 和 `OPENAI_MODEL` 可以配置为其他 OpenAI 兼容服务。`OPENAI_WIRE_API` 选择 `responses` 或 `chat_completions`（代码默认）；当前模板采用 Responses。`OPENAI_FALLBACK_MODEL` 留空关闭备用模型，显式填写后在主模型限流时最多切换一次。

`OPENAI_STORE_RESPONSES=false` 为默认值，向服务发送 `store=false`。Responses 工具续轮使用本地消息与必要的加密推理条目，不发送 `previous_response_id`，不依赖服务端会话保存。加密条目只在本轮角色循环中使用，不写入聊天、需求或行程，也不记录到文字日志。未完成的 Responses 输出会被拒绝，避免保存截断方案。

思考强度通过 `reasoning_effort` 配置：意图识别（main）、闲聊（chat）、查询（research）、展示转换（display）默认 `low`，规划与修改默认 `medium`，只读解释默认 `low`。可选的 `OPENAI_REASONING_EFFORT` 全局覆盖与 `OPENAI_REASONING_EFFORT_MAIN/REQUIREMENTS/RESEARCH/PLANNING/REVISION` 单角色覆盖见 `.env.example`；单角色优先。值为 `default` 时不传参数，使用服务端默认行为。显式全局或 REVISION 配置也覆盖解释模式的默认值。

当前镜像 `gpt-5.6-sol` 通过 Responses 发送 `reasoning.effort`，兼容旧服务时通过 Chat Completions 发送 `reasoning_effort`。历史百炼 `kimi-k3` 的低/中档及非法枚举探测保留作为历史证据。不能仅据 HTTP 成功断言内部强度效果；服务明确拒绝思考参数时移除后重试一次，其他错误按原流程处理。请求日志记录角色、协议、实际参数、耗时和思考 token 数，不增加思考正文。证据、完整配置和验证见 [思考强度说明](docs/model-reasoning.md)。

天气工具使用 Open-Meteo，不需要额外 Key。

高德地点、餐厅和路线工具使用官方MCP服务（Streamable HTTP），不再直连高德REST业务接口。配置如下：

```env
AMAP_MAP_KEY=你的高德地图Web服务Key
AMAP_MAPS_API_KEY=
AMAP_MCP_URL=https://mcp.amap.com/mcp
AMAP_MCP_TIMEOUT_SECONDS=20
```

`AMAP_MAPS_API_KEY`非空时优先，否则继续使用原`AMAP_MAP_KEY`。Key只由服务端附加到MCP连接地址，不进入模型参数和日志。无需Node.js服务，Python客户端完成初始化、工具发现和只读工具调用。

地点搜索先通过`maps_text_search`取候选，再通过`maps_search_detail`取坐标与详情；每次最多5个详情。餐厅只对本次有评分的候选排序，缺少人均、电话或营业信息保持未知。路线调用MCP的步行/公交/驾车工具，仍只使用可信POI坐标和原有缓存。未配置Key或服务失败时返回待核实，不回退到直接REST或编造结果。天气仍使用Open-Meteo。

每个应用工具的连接、发现和MCP调用共享20秒截止；额外详情`tools/call`也计入主业务32次外部查询预算。接入与验证见[高德MCP实施记录](docs/amap-mcp-implementation.md)。

SQLite 默认保存在：

```text
.cache/travelassistant.sqlite3
```

可以通过环境变量修改：

```env
TRAVEL_DB_PATH=.cache/travelassistant.sqlite3
TRAVEL_PERSISTENCE=1
```

## 启动

在项目根目录运行：

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

然后打开：

```text
http://127.0.0.1:8000
```

健康检查接口：

```text
http://127.0.0.1:8000/health
```

首次使用时注册或登录账号，再创建一趟旅行。

## Agent 工作流程

第一层由无工具 LLM 识别四类意图。闲聊使用无工具 LLM；查询、规划、修改复用 `TravelAgentRuntime.run_role()` 与同一套 `AgentLoop`，配置各自提示词及工具权限。

```text
用户消息 → 意图识别 LLM
             ├─ 闲聊 → 无工具 LLM → 回复
             ├─ 查询 → 查询 Agent Loop → 带依据的回答
             ├─ 规划 → 需求工具 → 五组必填检查 → 规划 Agent Loop → 完整文字
             │                   └─ 不足 → 提醒补充，记录等待目标
             └─ 修改 → 修改 Agent Loop → 完整修改文字
                                      ↓
                          校验与提交原文 → 主任务完成、文字可读
                                      ↓
                          独立后台展示 → 校验、只补齐当前卡片
                          转换失败 → 保留已交付原文，可重试卡片
```

需求收集属于规划内部。规划和修改通过 `update_requirements` 整理最新整条原话，程序校验候选；不再委派独立需求 Agent。只更新需求不授权重排；等待补充的生成目标独立保存，用户回答后可以承接。页面生成直接读取已保存五组需求，缺失时在查询前停止。

意图识别和闲聊读取当前需求档案及近期聊天，意图识别同时承接等待状态；业务 Agent 获得完整需求，并通过 `read_context` 按需读取当前行程、全部想法、聊天与证据。查询只读，解释不改行程。确认属于修改路径，由程序绑定当前快照执行，不让模型选择版本ID。

原文保存在 `TripPlan.text` 并先交付；独立展示任务按其保存的需求快照提取 JSON，保存到 `display`，共享期限只覆盖主业务。后台展示独立预算默认180秒，最多2次底层请求（包含协议兼容重试）；结构或日期校验失败时，在剩余至少60秒且无前台业务等待的条件下允许一次纠错。通过 `TRAVEL_DISPLAY_ENABLED=0` 可关闭后台转换，文字继续正常交付。程序校验 Schema、日期框架与原文指纹，保存当前行程依据的需求快照和实际工具记录。模型不能提交可信地点、坐标或证据。程序不声称完成逐句事实或逐活动时间核验。

聊天、生成和修改共用 Store 的最终保存边界，检查需求修订号、计划 ID 与完整快照。新消息替代旧轮、清空聊天或并发编辑后，旧回复和写入失效。清空聊天保留需求与行程，同时撤销待补充目标和未完成任务。

行程页提供按天卡片、原文折叠、查询依据、局部修改反馈、确认。旧 `days` 时间线与可信地图保持兼容；旧文字不批量转换。详情见[当前架构](docs/agent-architecture-current.md)与[展示字段](docs/travel-itinerary-display-schema.md)。

提示词位于 `app/prompts/agents/`：main 为意图识别，research/planning/revision 为业务 Agent，display 为展示转换。planning额外加载`planning_examples.txt`中的3个合成少样本；示例只指导方法，不提供本次旅行事实。闲聊提示在 `agent_roles.py` 中配置。

## 工具

工具注册表位于：

```text
app/tools/registry.py
```

天气工具：

```text
app/tools/weather.py
```

美食工具：

```text
app/tools/food.py
```

地点查询工具：

```text
app/tools/places.py
```

地点工具返回真实候选及其来源，Agent 根据旅行语境选择地点；重名无法区分时聊天询问。新文字行程保留 Agent 对来源与未知事项的说明，不提取 POI ID 制作地图卡片。已有地点卡片继续从原计划的可信资料展示。

路线查询工具：

```text
app/tools/routes.py
```

工具仅接受可信地点目录中的 POI ID，不接受模型自报坐标。主业务角色共享最多 16 次模型请求、64 次工具调用、32 次外部查询和 240 秒检查点期限；展示独立预算默认180秒且无工具。同一请求按地点坐标与交通方式复用路线结果。Agent 显式查询需要的路段，新文字交付后不再由程序补查路线或自动触发重生成。

工具调用有轮数限制。天气、美食、地点或路线服务不可用时，工具返回待确认结果，行程可以保留为带未核实事项的草案。不能把部分路线通过检查视为整份行程可执行。

## 项目结构

```text
app/
├── agent_client.py         模型访问、兼容入口、解析与错误回复
├── agent_roles.py          无工具节点与三个业务 Agent 配置
├── agent_runtime.py        意图分流、共用 Loop、需求工具与展示转换
├── plan_display.py         展示 Schema 校验、原文指纹与协议转换
├── agent_loop.py           有状态、有限预算的 Agent Loop 控制器
├── auth.py                  登录、注册和会话
├── main.py                  FastAPI 路由
├── models.py                旅行、需求档案、行程和准备事项模型
├── requirements.py          需求校验、预算口径和本轮聊天更新
├── tasks.py                 任务去重、持久状态、执行与重启恢复
├── plan_validation.py       旧格式行程的日期、时间与抵离校验
├── persistence.py           SQLite 快照和关系表持久化
├── store.py                 旅行状态和业务逻辑
├── prompts/agents/         当前意图、查询、规划、修改和展示提示词
├── schemas/                当前行程展示 JSON Schema
├── tools/
│   ├── registry.py          工具注册和分发
│   ├── weather.py           Open-Meteo 天气工具
│   ├── food.py              高德地图美食工具
│   ├── places.py            高德地图地点工具
│   └── routes.py            高德地图路线工具
├── templates/               Jinja2 页面模板
└── static/                  CSS 和前端脚本

tests/
├── automated/              自动回归测试（Agent、Web、工具、持久化与测试集）
├── browser/                Playwright 浏览器回归及隔离页面夹具
├── performance/            真实模型和外部服务的流程耗时测量
├── datasets/               v1 历史协议与 v2 当前测试集
├── run_travel_dataset.py    单案例评测入口
├── run_travel_dataset_batch.py  批量评测入口
└── travel_dataset_support.py    夹具、合成证据与协议校验

pytest.ini                  自动测试发现范围与项目导入路径
```

## 测试

当前模型业务评测使用[旅行 Agent 测试集 v2](tests/datasets/travel_agent_v2/README.md)：36条业务流程、10条展示转换、16条受控系统回归，另列8条待实现产品目标。支持冒烟、当前全量、展示、系统与目标档案；真实模型结果需人工评审。执行器默认串行，保留各角色、工具、展示耗时和 token 用量。

```powershell
& E:\anaconda\envs\TravelAssistant\python.exe tests/run_travel_dataset_batch.py --profile current --list
& E:\anaconda\envs\TravelAssistant\python.exe tests/run_travel_dataset_batch.py --profile smoke --output "$env:TEMP\travel-v2-smoke-$(Get-Date -Format yyyyMMdd-HHmmss)"
```

另有[Agent 场景耗时测试集](tests/datasets/agent_latency_v1/README.md)，覆盖26种回复、规划、修改、展示与缓存场景，生成逐调用时间线、CSV和离线交互报告，区分文字生成、保存与服务器响应就绪。

运行全部 Python 测试（目录与其他入口见[测试目录说明](tests/README.md)）：

```powershell
python -m pytest -q
```

运行 Python 编译检查：

```powershell
python -m compileall -q app
```

检查 Git diff 空白字符：

```powershell
git diff --check
```

浏览器聊天回归测试需要安装 Playwright，并且本机需要有 Chrome：

```powershell
npm install playwright
$env:TRAVEL_TEST_PYTHON = 'E:\anaconda\envs\TravelAssistant\python.exe'
node tests/browser/chat_browser.cjs
node tests/browser/requirements_browser.cjs
node tests/browser/tasks_browser.cjs
node tests/browser/places_browser.cjs
node tests/browser/text_itinerary_browser.cjs
```

需求浏览器测试会启动独立临时端口，使用内存数据和关闭的模型服务，不连接 8000 服务或业务数据库。

## 数据和安全

- API Key 只从环境变量读取，不写入日志。
- 标准应用日志仅记录异常类型和状态码；当前完整对话调试记录会写入 `.cache/llm_logs`，包含上下文、工具结果和模型回复。它们属于本地运行数据，日志开关和保留周期尚待完善。
- 用户消息在模型失败时仍会保存。
- 无论行程是否已确认，模型失败均不会覆盖当前行程。
- 意图识别 LLM 选择四条业务路径；业务 Agent 在各自权限内使用同一套工具 Loop。路由语义依赖模型和上下文，不使用关键词强制生成。
- 修改 Agent 依据用户原话和定位信息返回完整文字，未指定部分应保持不变；目标不明或快照变化时保留现状。
- 意图节点只读取相关聊天和等待状态；业务 Agent 始终获得完整有效需求，可读取计划、分页聊天与全部想法。清空聊天不删除需求与行程。
- 规划/修改调用需求工具更新候选，列表显式添加/移除，保存时检查需求修订号；展示 LLM 不能更新需求、生成证据或重新规划。
- 手动编辑和模型回复检查需求修订号，旧页面或处理中产生的旧结果不能覆盖较新的需求。
- 需求档案保存在 SQLite 快照和旅行表的 `requirements_json` 中。旧数据迁移保留原有对话、行程和想法，不推断没有提供的人数、日期和偏好。
- `.cache/` 下的 SQLite 数据和运行日志属于本地运行数据，不应提交到远程仓库。

## 任务接口与运行方式

- `POST /api/trips/{trip_id}/tasks`：提交 `kind`（chat/plan/revision）、`payload` 和稳定的 `request_id`；先返回 202 和任务状态。
- `GET /api/trips/{trip_id}/tasks`：返回当前账号旅行的最近 20 个任务，状态为 queued/running/succeeded/failed。
- `POST /api/trips/{trip_id}/tasks/{task_id}/retry`：使用新的 `request_id` 重试失败任务，同一失败任务的重试请求会找回同一个子任务；再次失败需重试最新失败任务。
- `GET /api/trips/{trip_id}/plan-state`：查询当前原文绑定与卡片状态。
- `POST /api/trips/{trip_id}/display`：只重试当前原文的卡片，不重新规划。
- `/api/trips/{trip_id}/messages` 保留兼容，发送时附带 `request_id`，仍返回消息和任务。无 JavaScript 的表单也会提交后台任务并跳转到状态页。

任务与旅行一起保存到 SQLite 快照，后台执行采用 FastAPI BackgroundTasks。同一进程内有去重和执行保护，当前仍要求单进程运行；尚未改造跨进程一致性或引入外部队列。进度仅显示真实的等待、处理中和完成/失败，不展示虚构百分比。单次模型超时取 OPENAI_TIMEOUT_SECONDS 与本轮剩余工作时间的较小值；运行时有 240 秒共享期限，外部查询进行中仍使用工具自身网络超时，返回后会再次检查期限。尚无任务取消或持久化子任务恢复。

## 当前边界

- 景点、酒店等地点由 Agent 查询和匹配，查到 POI 不代表已核实开放、营业或预约。地图链接使用高德坐标；路线耗时来自高德步行、公交或驾车查询，是查询时估算，未来路况、班次及额外候车安检仍需核实。
- 天气预报受 Open-Meteo 可查询范围限制，较远日期只能显示待确认。
- 美食门店信息依赖高德地图 Key 和接口可用性。
- 具体餐厅是否营业、是否排队或是否需要预约，生成后仍建议出发前再次确认。
- 模型服务的响应速度和限流策略由上游服务决定；本项目提供限流时的备用模型，但不能消除上游限流。
