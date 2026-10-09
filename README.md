# TravelAssistant

TravelAssistant 是一个私人旅行规划 Agent。通过聊天整理需求、查询旅行资料，并把完整文字行程保存到独立行程页；修改、确认和历史恢复都围绕这份文字进行。

项目使用 FastAPI 提供本地 Web 应用，使用 SQLite 保存旅行、对话、想法、行程版本和登录会话。

## 功能

- **旅行需求档案**：在聊天页点击“旅行需求”，维护旅游城市、抵离日期与时刻、可选抵离地点、人数、预算金额与口径、必去地点、避开事项、饮食禁忌和节奏。清空聊天后仍保留，生成和调整时优先使用当前有效需求。
- **旅行想法**：用想法卡片记录地点、预算、节奏和禁忌。所有想法完整保存，白板保留原布局并展示最近 8 条；需求页下方的“全部想法”可查看与删除更早的内容。
- **Agent 聊天**：通过对话提取旅行约束，模型不可用时保留消息并明确提示失败。
- **文字行程**：规划与调整直接输出完整文字，保留原文和换行，不生成行程 JSON 或 patch。独立行程页展示当前版本和状态，支持反馈修改、确认与恢复历史版本。
- **按需查询**：Agent 使用天气、地点、美食和路线工具，根据旅行语境选择资料并说明来源与未知事项。工具不可用时明确待核实，不要求用户逐个手动查询。
- **抵离与旅行约束**：当前有效需求完整交给业务 Agent，按当地时间安排首末日、当天往返不住宿，不查询具体火车班次。新文字行程不再做按活动字段执行的日期、时间或路线窗口自动校验。
- **旧行程兼容**：已有时间线、旅行准备、地点地图和路线卡片继续展示；旧/新版本都可恢复。旧行程修改后形成新的文字版本，旧记录保留。
- **可恢复任务**：聊天、生成和调整保存任务状态，刷新后可查询结果；请求标识去重。思考中可以继续发送，新消息立即替代旧轮并发起独立请求，旧回复和变更作废；不等待旧网络请求结束。失败可重试，服务重启中断不自动重跑。
- **版本管理**：确认行程后保留版本，后续修改可以生成新草案或恢复旧版本。
- **模型失败保护**：支持主模型和限流时的备用模型；超时、空回复或需要用户补充时保留原行程与版本。保存时核对需求与计划快照，拒绝旧结果覆盖新编辑或确认。

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

思考强度通过 `reasoning_effort` 按角色配置：主 Agent、需求与查询默认 `low`，规划与修改默认 `medium`，只读解释默认 `low`。可选的 `OPENAI_REASONING_EFFORT` 全局覆盖与 `OPENAI_REASONING_EFFORT_MAIN/REQUIREMENTS/RESEARCH/PLANNING/REVISION` 单角色覆盖见 `.env.example`；单角色优先。值为 `default` 时不传参数，使用服务端默认行为。显式全局或 REVISION 配置也覆盖解释模式的默认值。

当前镜像 `gpt-5.6-sol` 通过 Responses 发送 `reasoning.effort`，兼容旧服务时通过 Chat Completions 发送 `reasoning_effort`。历史百炼 `kimi-k3` 的低/中档及非法枚举探测保留作为历史证据。不能仅据 HTTP 成功断言内部强度效果；服务明确拒绝思考参数时移除后重试一次，其他错误按原流程处理。请求日志记录角色、协议、实际参数、耗时和思考 token 数，不增加思考正文。证据、完整配置和验证见 [思考强度说明](docs/model-reasoning.md)。

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

一个主 Agent 委派需求、查询、规划、调整四个业务子 Agent。五个角色共用 `TravelAgentRuntime.run_role()` 与 `AgentLoop`；角色差异来自提示词和工具权限。

```text
用户聊天 → 主 Agent
             ├─ 直接回答 / 询问
             ├─ 需求 Agent → 需求变更候选
             ├─ 查询 Agent → 带来源的查询结果
             ├─ 规划入口 → 本轮需求同步 → 日期窗口 → 规划 Agent → 完整文字行程
             └─ 调整入口 → 本轮需求同步 → 调整 Agent → 完整新文字
                         └─ 只读解释 → 直接解释，无写入
                                  ↓
                         版本检查、原文保存
                                  ↓
                              行程页
```

主 Agent 初始只接收最近 8 条已完成聊天、用户最新消息与当前日期。业务子 Agent 获得完整有效需求，并用 `read_context` 按需读取聊天、想法、当前行程、版本和本轮证据。委派必须引用本轮用户原话，业务子 Agent 不递归委派。

需求变更先在私有快照中校验，后续业务使用更新后的要求。行程写入入口保证本轮经过一次需求整理，主 Agent 已整理过则复用；页面生成只读已保存需求。新规划缺城市或具体日期时在查询前直接追问，程序按有效日期计算首末日窗口和跨午夜次数。详情见[当前架构](docs/agent-architecture-current.md)。

聊天、生成、修改共用最终保存边界，核对需求修订号、计划 ID 和内容快照后写入。需求仍使用内部数据接口，工具调用参数也继续使用 JSON；本次移除的是行程结构化输出。

新行程与修改原文保存在 `TripPlan.text` 和 `PlanVersion.text`。实际工具查询依据和需求依据修订号随版本保存，备注修改可以复用旧依据；来源和未知原因在行程页展开查看。没有文本转 JSON、文字字段提取、行程格式修正、保存后的自动路线补查。输入窗口由系统计算，输出文字的时间、事实和修改范围仍由业务 Agent 遵守，系统不声称完成逐句事实核验或逐项交通窗口校验。解释与普通聊天不生成版本。

行程页保留，安全展示完整文字，支持反馈修改、确认、刷新与历史恢复。旧的 `days` 数据继续显示原时间线和地点卡片，不批量转换或删除。当前能力与取舍见 [角色 Agent 当前实现](docs/agent-role-implementation.md)。

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

地点工具返回真实候选及其来源，Agent 根据旅行语境选择地点；重名无法区分时聊天询问。新文字行程保留 Agent 对来源与未知事项的说明，不提取 POI ID 制作地图卡片。已有地点卡片继续从原计划的可信资料展示。

路线查询工具：

```text
app/tools/routes.py
```

工具仅接受可信地点目录中的 POI ID，不接受模型自报坐标。所有角色共享最多 16 次模型请求、64 次工具调用、32 次外部查询和 240 秒检查点期限；同一请求按地点坐标与交通方式复用路线结果。Agent 显式查询需要的路段，新文字交付后不再由程序补查路线或自动触发重生成。

工具调用有轮数限制。天气、美食、地点或路线服务不可用时，工具返回待确认结果，行程可以保留为带未核实事项的草案。不能把部分路线通过检查视为整份行程可执行。

## 项目结构

```text
app/
├── agent_client.py         模型访问、兼容入口、解析与错误回复
├── agent_roles.py          主 Agent 与四个业务子 Agent 的角色配置
├── agent_runtime.py        共用角色循环、委派、快照读取与共享预算
├── agent_loop.py           有状态、有限预算的 Agent Loop 控制器
├── auth.py                  登录、注册和会话
├── main.py                  FastAPI 路由
├── models.py                旅行、需求档案、行程和准备事项模型
├── requirements.py          需求校验、预算口径和本轮聊天更新
├── tasks.py                 任务去重、持久状态、执行与重启恢复
├── plan_validation.py       旧格式行程的日期、时间与抵离校验
├── persistence.py           SQLite 快照和关系表持久化
├── store.py                 旅行状态和业务逻辑
├── response_schema.py       历史行程结果协议（新链路不使用）
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
test_text_itinerary.py        文字行程保存、修改、版本、重启与页面保护测试
test_app.py                  Web、认证和页面测试
test_regressions.py          Agent、持久化和工具回归测试
test_requirements.py         需求编辑、迁移、长对话和并发保护测试
tests/chat_browser.cjs       浏览器端聊天回归测试
tests/text_itinerary_browser.cjs  文字行程页生成、修改、确认、恢复和布局验收
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
