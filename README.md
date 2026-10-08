# TravelAssistant

TravelAssistant 是一个以景点路线为主线的私人旅行规划 Agent。它把旅行拆成“游览计划、旅行准备、每日行程”三部分：先确定想去哪里和怎么游览，再补充衣物、住宿、餐食和交通安排。

项目使用 FastAPI 提供本地 Web 应用，使用 SQLite 保存旅行、对话、想法、行程版本和登录会话。

## 功能

- **旅行需求档案**：在聊天页点击“旅行需求”，维护旅游城市、抵离日期与时刻、可选抵离地点、人数、预算金额与口径、必去地点、避开事项、饮食禁忌和节奏。清空聊天后仍保留，生成和调整时优先使用当前有效需求。
- **旅行想法**：用想法卡片记录地点、预算、节奏和禁忌。所有想法完整保存，白板保留原布局并展示最近 8 条；需求页下方的“全部想法”可查看与删除更早的内容。
- **Agent 聊天**：通过对话提取旅行约束，模型不可用时保留消息并明确提示失败。
- **抵离时间规划**：用户填写到达、离开旅游城市的日期与当地时刻，助手据此安排首末日，不查询火车票。时刻未定可留空，接驳与预留时间作为估算待核实。保存前检查明确日期、明确时刻和抵离越界。
- **行程业务校验**：按需求档案中的具体日期或天数检查覆盖，识别非法/重复/乱序日期、明确时间重叠与抵离越界。模型最多复用工具结果修正一次；仍失败则保留原行程并提示具体问题。单项修改检查实际合并后的整份行程。模糊时段不猜测，交通时间使用真实路线查询，开放时间尚待接入数据验证。
- **生成第一版行程**：以景点和路线为主线，生成按天排列的游览计划。
- **路线耗时与排程检查**：Agent 查询已匹配地点之间的步行、公交或驾车预计耗时，并检查交通条目、活动间隔和首末日接驳。发现冲突最多修正一次，仍失败时保留原行程；未知地点和时间明确待核实。
- **旅行准备**：单独展示衣物、天气装备和住宿建议。
- **每日时间线**：把游览、用餐、交通、住宿和休息放在同一条时间线上。
- **具体饮食推荐**：餐食项目包含推荐菜品或当地食物、菜系、区域、预算和预约状态。
- **天气查询**：使用 Open-Meteo 查询温度、降雨和天气状况，并生成穿衣、遮阳、补水和雨具建议。
- **美食门店查询**：可选接入高德地图 POI，查询真实门店、评分、人均消费、地址和营业时间。
- **Agent 地点查询与匹配**：生成或调整行程时，Agent 调用高德地点工具并根据旅行语境匹配地点，后端只按真实工具返回的 POI ID 保存地址与坐标。行程详情展示结果、地图入口和地址复制；没有用户逐条查询或选择候选的页面。重名无法区分时通过聊天询问，查不到则保持待核实。
- **可恢复任务**：聊天、生成和调整统一保存任务状态，页面可离开、刷新后查询结果；客户端请求标识用于去重，同一旅行同时只处理一个任务。失败可重试，聊天重试不重复添加用户消息。服务重启后未完成任务明确标为中断，用户检查行程后再重试，不自动重跑。
- **版本管理**：确认行程后保留版本，后续修改可以生成新草案或恢复旧版本。
- **模型容错**：支持主模型和限流时的备用模型；行程格式校验失败时，复用工具结果最多重生成一次。仍失败则明确提示并保留当前行程与版本，不用本地模板冒充成功。

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
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
OPENAI_MODEL=qwen-plus
OPENAI_FALLBACK_MODEL=qwen-plus
```

`OPENAI_BASE_URL` 和 `OPENAI_MODEL` 也可以配置为其他 OpenAI 兼容服务。主模型返回限流错误时，Agent 最多切换一次备用模型。

思考强度通过 `reasoning_effort` 按角色配置：主 Agent、需求与查询默认 `low`，规划与修改默认 `medium`，只读解释默认 `low`。可选的 `OPENAI_REASONING_EFFORT` 全局覆盖与 `OPENAI_REASONING_EFFORT_MAIN/REQUIREMENTS/RESEARCH/PLANNING/REVISION` 单角色覆盖见 `.env.example`；单角色优先。值为 `default` 时不传参数，使用服务端默认行为。显式全局或 REVISION 配置也覆盖解释模式的默认值。

当前百炼 `kimi-k3` 已实测接受低/中档参数并拒绝非法枚举；官方文档与实际端点对部分思考参数的描述存在差异，不能仅据 HTTP 成功断言内部强度效果。服务明确拒绝参数时移除后重试一次，其他错误按原流程处理。请求日志记录角色、实际参数、耗时和思考 token 数，不增加思考正文。证据、完整配置和验证见 [思考强度说明](docs/model-reasoning.md)。

天气工具使用 Open-Meteo，不需要额外 Key。

如果需要真实美食门店推荐，配置高德地图 Key：

```env
AMAP_MAP_KEY=你的高德地图 Web 服务 Key
```

没有配置 `AMAP_MAP_KEY` 时，美食工具会返回待确认状态，Agent 仍可以根据通用知识推荐菜品和用餐区域，但不会伪造门店信息。

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

一个主 Agent 根据聊天直接回答或委派需求、查询、规划、调整四个子 Agent。每个角色只有提示词和工具集合，共用 `TravelAgentRuntime.run_role()` 与 `AgentLoop`：模型返回工具调用就执行并回传结果，没有工具调用就解析结果并结束。子 Agent 自行读取执行上下文，不再固定先跑信息收集阶段、再切换规划阶段。

```text
用户聊天 → 主 Agent
             ├─ 直接回答 / 询问
             ├─ 需求 Agent → 需求变更候选
             ├─ 查询 Agent → 带来源的查询结果
             ├─ 规划 Agent → 新行程草案
             └─ 调整 Agent → 只读解释 / 局部或全局修改
                     ↓
          角色按需读取上下文、查询事实
                     ↓
          系统校验、版本检查和最终保存
```

主 Agent 初始只接收最近 8 条已完成聊天、用户最新消息与当前日期。子 Agent 获得完整有效需求，并用 `read_context` 读取聊天、想法、当前计划、版本和本轮证据。主 Agent 的委派必须引用本轮用户原话；子 Agent 不递归委派，工具权限由程序限制。

需求变更先在本次私有快照中校验，后续规划立即使用更新后的要求；整轮返回 Store 后统一检查版本并应用。后续规划失败时可以保存已经有效确认的需求变更，回复分别说明结果。普通聊天和只读解释不能通过返回 `plan` 直接修改行程。聊天局部修改与页面修改共用合并与保存检查。

模型初始请求允许自由选择工具；结构化结果无效时最多修正一次，修正请求使用严格 Schema，并保留已有工具消息和路线缓存。工具与规则校验在模型外执行。详细实现与当前边界见 `docs/agent-role-implementation.md`。

最终生成的结果仍以景点路线为主线，再补齐衣食住行：

- 衣物和住宿进入“旅行准备”。
- 早餐、午餐、晚餐和小吃进入每日行程。
- 景点之间的移动进入每日交通项目。
- 温度、降雨、穿衣和雨具建议写入准备区或当天备注。
- 没有可靠门店数据时只推荐菜品和区域，不编造已预约餐厅。

行程项目使用以下分类：

```text
activity       游览
meal           用餐
transport      交通
accommodation  住宿
rest           休息
```

完整结果协议位于：

```text
app/response_schema.py
```

提示词位于：

```text
app/agent_roles.py
app/prompts/agents/main.txt
app/prompts/agents/requirements.txt
app/prompts/agents/research.txt
app/prompts/agents/planning.txt
app/prompts/agents/revision.txt
```

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

Agent 返回匹配的 `poi_id`，后端仅从本轮 `search_places` 工具结果取得地点资料；模型自报坐标和未查询的 ID 不会绑定。已匹配地点随行程与历史版本保存。单项调整地点或标题改变时清除旧坐标，原地点未变时保留。地点资料不会自动改变交通或游览时间，模糊候选通过聊天处理。

路线查询工具：

```text
app/tools/routes.py
```

仅接受可信地点目录中的 POI ID，不接受模型自报坐标。所有角色共享最多 16 次模型请求、64 次工具调用、32 次外部查询和 240 秒工作期限；批量工具执行前检查额度，自动补查也消耗外部查询额度。每个角色另有有限步数，同一请求按地点坐标与交通方式复用路线结果，最多缓存 32 个不同路段。生成后的检查也查询缺失路线，并将冲突与真实耗时反馈给 Agent 修正；单项调整检查实际合并后的整份行程，允许更新相关路线资料但不改动邻居安排。路线随行程与版本保存。

工具调用有轮数限制。天气、美食、地点或路线服务不可用时，工具返回待确认结果，行程可以保留为带未核实事项的草案。不能把部分路线通过检查视为整份行程可执行。

## 项目结构

```text
app/
├── agent_client.py         模型访问、兼容入口、解析与错误回复
├── agent_roles.py          一个主 Agent、四个子 Agent 的提示词与工具配置
├── agent_runtime.py        共用角色循环、委派、快照读取与共享预算
├── agent_loop.py           有状态、有限预算的 Agent Loop 控制器
├── auth.py                  登录、注册和会话
├── main.py                  FastAPI 路由
├── models.py                旅行、需求档案、行程和准备事项模型
├── requirements.py          需求校验、预算口径和本轮聊天更新
├── tasks.py                 任务去重、持久状态、执行与重启恢复
├── plan_validation.py       日期覆盖、时间冲突和抵离边界校验
├── persistence.py           SQLite 快照和关系表持久化
├── store.py                 旅行状态和业务逻辑
├── response_schema.py       最终旅行结果 JSON Schema
├── prompts/
│   ├── base_system.txt      基础系统提示词
│   └── skills/              阶段性 Skill
├── tools/
│   ├── registry.py          工具注册和分发
│   ├── weather.py           Open-Meteo 天气工具
│   ├── food.py              高德地图美食工具
│   ├── places.py            高德地图地点工具
│   └── routes.py            高德地图路线工具
├── templates/               Jinja2 页面模板
└── static/                  CSS 和前端脚本

test_agent_loop.py            Agent Loop 状态、动作和停止条件测试
test_agent_roles.py           委派、权限、上下文、组合需求、局部修改和预算测试
test_app.py                  Web、认证和页面测试
test_regressions.py          Agent、持久化和工具回归测试
test_requirements.py         需求编辑、迁移、长对话和并发保护测试
tests/chat_browser.cjs       浏览器端聊天回归测试
tests/requirements_browser.cjs  隔离应用的需求编辑与完整想法回归
```

## 测试

运行全部 Python 测试：

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
node tests/chat_browser.cjs
$env:TRAVEL_TEST_PYTHON = (Get-Command python).Source
node tests/requirements_browser.cjs
node tests/tasks_browser.cjs
node tests/places_browser.cjs
```

需求浏览器测试会启动独立临时端口，使用内存数据和关闭的模型服务，不连接 8000 服务或业务数据库。

## 数据和安全

- API Key 只从环境变量读取，不写入日志。
- 标准应用日志仅记录异常类型和状态码；当前完整对话调试记录会写入 `.cache/llm_logs`，包含上下文、工具结果和模型回复。它们属于本地运行数据，日志开关和保留周期尚待完善。
- 用户消息在模型失败时仍会保存。
- 无论行程是否已确认，模型失败均不会覆盖当前行程或创建新版本。
- 主 Agent 通过工具调用选择业务子 Agent；需求、查询、规划和调整具有不同权限。意图语义依赖模型与提示词，不使用关键词规则强行触发生成。
- 单项修改依据原条目 ID 定位，后端只应用该条目的变化；目标不匹配或行程已被其他操作更新时保留现状。
- 主 Agent 只读取最近聊天；子 Agent 始终获得完整有效需求，并可按需读取完整计划、分页检索历史聊天和全部想法。解析不会按固定天数或每日条目数截断。清空聊天后仍保留有效需求，明确撤销的要求不从旧历史自动恢复。
- 需求子 Agent 采用 `requirements_patch` 和本轮消息原文依据；列表使用显式添加/移除，未提及的字段保持原值。角色提示词见 `app/prompts/agents/requirements.txt`。
- 手动编辑和模型回复检查需求修订号，旧页面或处理中产生的旧结果不能覆盖较新的需求。
- 需求档案保存在 SQLite 快照和旅行表的 `requirements_json` 中。旧数据迁移保留原有对话、行程和想法，不推断没有提供的人数、日期和偏好。
- `.cache/` 下的 SQLite 数据和运行日志属于本地运行数据，不应提交到远程仓库。

## 任务接口与运行方式

- `POST /api/trips/{trip_id}/tasks`：提交 `kind`（chat/plan/revision）、`payload` 和稳定的 `request_id`；先返回 202 和任务状态。
- `GET /api/trips/{trip_id}/tasks`：返回当前账号旅行的最近 20 个任务，状态为 queued/running/succeeded/failed。
- `POST /api/trips/{trip_id}/tasks/{task_id}/retry`：使用新的 `request_id` 重试失败任务，同一失败任务的重试请求会找回同一个子任务；再次失败需重试最新失败任务。
- `/api/trips/{trip_id}/messages` 保留兼容，发送时附带 `request_id`，仍返回消息和任务。无 JavaScript 的表单也会提交后台任务并跳转到状态页。

任务与旅行一起保存到 SQLite 快照，后台执行采用 FastAPI BackgroundTasks。同一进程内有去重和执行保护，当前仍要求单进程运行；尚未改造跨进程一致性或引入外部队列。进度仅显示真实的等待、处理中和完成/失败，不展示虚构百分比。单次模型超时取 OPENAI_TIMEOUT_SECONDS 与本轮剩余工作时间的较小值；运行时有 240 秒共享期限，外部查询进行中仍使用工具自身网络超时，返回后会再次检查期限。尚无任务取消或持久化子任务恢复。

## 当前边界

- 景点、酒店等地点由 Agent 查询和匹配，查到 POI 不代表已核实开放、营业或预约。地图链接使用高德坐标；路线耗时来自高德步行、公交或驾车查询，是查询时估算，未来路况、班次及额外候车安检仍需核实。
- 天气预报受 Open-Meteo 可查询范围限制，较远日期只能显示待确认。
- 美食门店信息依赖高德地图 Key 和接口可用性。
- 具体餐厅是否营业、是否排队或是否需要预约，生成后仍建议出发前再次确认。
- 模型服务的响应速度和限流策略由上游服务决定；本项目提供限流时的备用模型，但不能消除上游限流。
