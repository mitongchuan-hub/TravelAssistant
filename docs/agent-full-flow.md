# 当前版本全流程：每层真实输入与输出

版本说明：本文件及配套 SVG、HTML、JSON、PNG 是主/子 Agent 改造前的运行快照，保留用于追溯。最新架构与已知边界见 [角色 Agent 当前实现](agent-role-implementation.md)。

[查看可缩放流程图与逐层完整数据](agent-full-flow.html) · [SVG](agent-full-flow.svg) · [完整证据 JSON](agent-full-flow-evidence.json)

本次真实模型：`kimi-k3`，trace：`8918fd7053de`，记录时间：`2026-10-05T01:28:58+00:00`。

通过 FastAPI TestClient 调用真实 HTTP 路由和后台任务，查询真实模型与工具，并写入独立 SQLite。所有旅行输入为合成案例。浏览器轮询、局部修改、失败恢复等未执行分支按源码说明，没有伪造运行记录。

当前实际结构：外层持久任务 → 模式分派 → 事实收集 Loop → 独立规划 Loop → Store 最终保护和保存 → 任务结果与渲染页面。普通聊天另有 conversation Loop。

上一版概念流程图中“统一 Loop 自主选择规划或等待、信息不足进入 waiting_user”的描述过于宽泛；本版按照当前实现更正：规划动作由代码安排，等待用户的回复没有接入可恢复 waiting_user 状态。

本次最终得到 8 个条目、6 条系统路线记录、4 条待确认事项，保存 v1；业务库前后计数相同：`{'trips': 8, 'messages': 47}`。

## 01 用户建立旅行与有效需求

来源：`app/main.py → create_trip / save_requirements`。本次实测。

**输入**

```text
目的地：杭州；日期：2026-11-01
10:00 抵达杭州东站；20:00 从杭州东站离开
1 人；人均 800 元；节奏轻松
必去：灵隐寺、庆春朴门(灵隐店)
午餐后在餐厅原地休息；不增加茶馆、不安排过夜
往返公交；寺院到餐厅步行；各安排写明确起止时段
```

**处理**

- POST /trips 创建旅行
- POST /workspace/{id}/requirements
- 校验完整候选需求与 revision
- 更新 Trip + TripRequirements
- 保存需求，行程仍为空

**输出**

```text
trip.id = trip-102
requirements.revision = 1
budget_amount = "800"
budget_scope = "per_person"
plan = {id:"empty", status:"未生成", days:[]}
聊天初始欢迎消息 1 条；历史版本 0 个
```

本次使用独立账号和独立 SQLite。日期与时刻是合成案例的用户输入，地点和路线随后真实查询。

[展开本层完整输入输出](agent-full-flow.html#io-01)

## 02 浏览器生成请求 → HTTP 接口

来源：`app/static/tasks.js → app/main.py::api_submit_task`。本次实测。

**输入**

```text
POST /api/trips/trip-102/tasks
{
  "kind": "plan",
  "payload": {},
  "request_id": "svg-full-flow-d63ce00396"
}
登录会话 Cookie（图与数据文件不导出凭证）
```

**处理**

- 检查登录与旅行归属
- 校验 kind / payload / request_id
- plan 请求的 payload 归一为 {}
- 提交任务；注册 BackgroundTasks
- 本次 HTTP 返回：202

**输出**

```text
task.id = bb4e0252bfe64b0187748b839d1cfbbd
request_id = svg-full-flow-d63ce00396
kind = "plan"；status = "queued"
message = "等待处理"
plan_id = "empty"
requirements_revision = 1
result_url = ""
```

本次通过 FastAPI TestClient 调用真实 HTTP 路由；后台任务也真实执行。浏览器 JS 的轮询与跳转行为由源码核对。

[展开本层完整输入输出](agent-full-flow.html#io-02)

## 03 外层任务运行与版本保护

来源：`app/tasks.py::submit_task / run_task`。本次实测。

**输入**

```text
queued task + 最新 Store 状态
基线：plan_id="empty"，需求 revision=1
同一 request_id 对应同一内容
本次没有重试，没有并发任务
```

**处理**

- 相同请求去重；每趟旅行只运行一个任务
- 获取本旅行 _agent_lock
- 非聊天任务启动前核对需求与行程基线
- status：queued → running
- kind=plan → store.generate_plan(trip_id)

**输出**

```text
持久任务 status = "running"
message = "正在处理，请稍候；可以离开页面，回来查看结果。"
新增 Agent 消息 status = "thinking"
内部生成指令：
请根据当前对话和想法墙，生成一版结构化旅行行程。
```

任务状态持久化在 SQLite 快照；Loop 的阶段和历史只存在本次内存与 trace。两者是不同的状态对象。

[展开本层完整输入输出](agent-full-flow.html#io-03)

## 04 组装模型上下文与选择入口

来源：`app/store.py::_generate_reply → app/agent_client.py::_build_context`。本次实测。

**输入**

```text
Trip + 当前有效需求
user_body = 请根据当前对话和想法墙，生成一版结构化旅行行程。
messages / idea_cards / plan / plan_versions / metrics
planning_mode = True
target_item_id = null
```

**处理**

- 深拷贝本次上下文
- 保留最近 12 条 status=sent 的消息
- 全部想法、完整当前行程、最近 3 个版本摘要
- seed_places(plan) 初始化可信目录
- route_cache = {}
- planning_mode=True → 规划入口

**输出**

```text
model = "kimi-k3"
context：中文上下文字符串（完整内容见 HTML）
本次消息 1 条；想法 0 条
place_catalog 初始为空；route_cache 初始为空
需求 revision / expected_plan_id 由 Store 另行保护
它们没有作为 AgentLoopState 字段存入 Loop
```

一个旅行 Agent 的职责由提示词和代码区分；当前没有独立的“理解 Agent / 规划 Agent / 沟通 Agent”。

[展开本层完整输入输出](agent-full-flow.html#io-04)

## 05 事实收集循环：模型选择工具

来源：`app/agent_client.py::_collect_tool_messages → AgentLoop`。本次实测。

**输入**

```text
system = 基础提示词 + 信息收集提示词
user = 上一层完整 context
tools = query_weather / search_food / search_places / query_route
mode="fact_collection"；phase="collecting_facts"
max_steps=3；max_tool_calls=32
```

**处理**

- decide() 调用模型
- 有 tool_calls → execute_tools
- 没有 tool_calls → complete
- Action Gate 检查动作名称、payload 类型
- 允许动作只有 execute_tools / complete
- 本次每批工具按顺序执行

**输出**

```text
第 1 轮模型返回真实 tool_calls：
query_weather({"city":"杭州","date":"2026-11-01"})
search_places({"city":"杭州","keywords":"杭州东站"})
search_places({"city":"杭州","keywords":"灵隐寺"})
search_places({"city":"杭州","keywords":"庆春朴门 灵隐"})
```

LoopAction 是代码根据模型的 tool_calls 构造的；模型没有返回统一的自由动作 JSON。

[展开本层完整输入输出](agent-full-flow.html#io-05)

## 06 工具执行：真实地点与天气结果

来源：`app/tools/registry.py → places.py / weather.py；app/places.py::collect_places`。本次实测。

**输入**

```text
query_weather: {
  "city": "杭州",
  "date": "2026-11-01"
}
search_places: {
  "city": "杭州",
  "keywords": "杭州东站"
}
search_places: {
  "city": "杭州",
  "keywords": "灵隐寺"
}
search_places: {
  "city": "杭州",
  "keywords": "庆春朴门 灵隐"
}
```

**处理**

- 解析参数对象；按工具名称分发
- 高德地点接口 / Open-Meteo 天气接口
- 校验 POI ID 和坐标，过滤无效与重复结果
- 以 role=tool 消息回传模型
- 可信地点累积到 place_catalog
- 查询不到的事实保持未知

**输出**

```text
query_weather：status=unavailable
日期超出当前预报范围，天气待确认。
search_places：status=available
查询词：杭州东站；返回 10 条候选
杭州东站 → B023B08WDR
地址：天城路1号；坐标：120.212605, 30.290846
来源：高德地图 POI；查询：2026-10-05T01:29:08+00:00
search_places：status=available
查询词：灵隐寺；返回 10 条候选
灵隐寺 → B023B02842
地址：法云弄1号；坐标：120.101406, 30.240826
来源：高德地图 POI；查询：2026-10-05T01:29:08+00:00
search_places：status=available
查询词：庆春朴门 灵隐；返回 2 条候选
庆春朴门(灵隐店) → B0GKVZ1O4T
地址：白乐桥181号；坐标：120.109073, 30.244621
来源：高德地图 POI；查询：2026-10-05T01:29:08+00:00
```

图中展示候选摘要；HTML 与 JSON 保留全部原始工具结果。POI 查询成功不代表开放、预约或门票已核实。

[展开本层完整输入输出](agent-full-flow.html#io-06)

## 07 观察事实 → 追加路线及其他必要查询

来源：`app/agent_client.py → app/tools/routes.py::lookup_route`。本次实测。

**输入**

```text
下一轮 messages 包含前轮 assistant + tool 结果
query_route({
  "destination_poi_id": "B023B02842",
  "mode": "transit",
  "origin_poi_id": "B023B08WDR"
})
query_route({
  "destination_poi_id": "B0GKVZ1O4T",
  "mode": "walking",
  "origin_poi_id": "B023B02842"
})
query_route({
  "destination_poi_id": "B023B08WDR",
  "mode": "transit",
  "origin_poi_id": "B0GKVZ1O4T"
})
search_food({
  "city": "杭州",
  "keywords": "庆春朴门",
  "max_results": 3
})
```

**处理**

- 模型根据查到的 POI ID 请求下一批工具
- 路线仅接受可信目录中的起终点 ID
- 系统取得工具坐标并查询高德
- 秒数向上取整为分钟
- 按 ID + 坐标 + 方式缓存成功与失败
- 同一请求最多 32 个不同路线缓存键

**输出**

```text
杭州东站 → 灵隐寺
mode=transit；status=available
68 分钟；15235 米
source=高德路线规划；fetched_at=2026-10-05T01:29:13+00:00
灵隐寺 → 庆春朴门(灵隐店)
mode=walking；status=available
17 分钟；1206 米
source=高德路线规划；fetched_at=2026-10-05T01:29:13+00:00
庆春朴门(灵隐店) → 杭州东站
mode=transit；status=available
56 分钟；14500 米
source=高德路线规划；fetched_at=2026-10-05T01:29:13+00:00
search_food：status=available
本次模型还查询了美食门店，完整结果见数据区。
```

这里的路线是查询时估算。额外候车、安检、入口内部步行和未来班次没有得到保证。

[展开本层完整输入输出](agent-full-flow.html#io-07)

## 08 事实循环停止 → 构造规划请求

来源：`app/agent_client.py::_collect_tool_messages → _request_planning_model`。本次实测。

**输入**

```text
累积的 assistant tool_calls + tool messages
最新 place_catalog 和 route_cache
最后一轮模型响应 / 步数上限
```

**处理**

- 完成或耗尽查询步数后返回已收集消息
- 切换到最终行程提示词
- 追加明确的完整 JSON 生成指令
- 新建独立 planning Loop
- 事实 Loop 与 planning Loop 共享目录/缓存
- 它们不共享同一个 AgentLoopState

**输出**

```text
fact status = budget_exhausted
steps = 3；tool_calls = 8
#1 execute_tools → collecting_facts
#2 execute_tools → collecting_facts
#3 execute_tools → collecting_facts
可信地点总数 = 22
planning：phase=drafting_plan；max_steps=2
```

达到事实查询预算也会进入生成阶段；并不自动等同于“所有事实已经查齐”。

[展开本层完整输入输出](agent-full-flow.html#io-08)

## 09 规划循环：生成结构化草案

来源：`app/agent_client.py::_request_planning_model / _parse_planning_reply`。本次实测。

**输入**

```text
system = 基础提示词 + 最终生成提示词
user=context + 事实阶段工具消息 + 完整生成指令
response_format：json_schema / travel_plan / strict=true
当前动作：draft_plan；失败后才是 repair_plan
```

**处理**

- 动作顺序由代码确定
- 模型组合地点、时间、节奏与理由
- 解析 body / card / idea_cards / plan
- 检查必需字段和非空 plan
- 模型输出 POI 引用；系统后续绑定事实
- 格式不支持时：json_schema → json_object → 普通输出

**输出**

```text
title = 杭州灵隐一日慢游
status = 草案
1 天；8 个条目
模型最终草案的 time / category / title / duration：
10:00-10:20 | activity | 抵达杭州东站，前往公交站 | 20分钟
10:20-11:28 | transport | 公交前往灵隐寺 | 68分钟
11:28-14:30 | activity | 游览灵隐寺 | 约3小时
14:30-14:47 | transport | 步行前往庆春朴门 | 17分钟
14:47-16:00 | meal | 庆春朴门午餐 | 约1小时13分钟
16:00-17:15 | rest | 餐厅原地休息 | 约1小时15分钟
17:15-18:11 | transport | 公交返回杭州东站 | 56分钟
18:11-20:00 | rest | 东站候车，20:00 离开 | 约1小时49分钟
```

本层展示模型字段，下一层展示系统补充字段。实际是否使用格式兼容回退，可在 HTML 的全部模型请求中核对。

[展开本层完整输入输出](agent-full-flow.html#io-09)

## 10 系统绑定可信 POI 引用与补充路线

来源：`app/routes.py::merge_target / prepare_routes / _endpoint`。本次实测。

**输入**

```text
解析后的 raw_plan + 有效需求
place_catalog：22 个候选
route_cache：前面查询过的路线
existing_plan=empty；target_item_id=null
```

**处理**

- 本次整份生成，merge_target 原样返回
- 匹配 raw_item.poi_id；未可信的引用清空
- transport 是移动，不是额外停留地点
- 首末日抵离地点唯一精确匹配
- 本次还自动查一次杭州东站 POI
- 复用路线缓存；同一地点生成 0 分钟记录
- 未知地点/时间追加 pending 与风险

**输出**

```text
保留 8 个条目；丢弃模型自报路线资料
共 6 条系统路线记录（含同地点零移动）
杭州东站 → 杭州东站 | transit | available | 0 分钟
杭州东站 → 灵隐寺 | transit | available | 68 分钟
灵隐寺 → 庆春朴门(灵隐店) | walking | available | 17 分钟
庆春朴门(灵隐店) → 庆春朴门(灵隐店) | walking | available | 0 分钟
庆春朴门(灵隐店) → 杭州东站 | transit | available | 56 分钟
杭州东站 → 杭州东站 | transit | available | 0 分钟
incoming_route / departure_route 由工具结果生成
pending_items = 4 条
```

PlaceDetails 对象及 selected_at 在保存层生成。本层只核对 POI 引用并补充 RouteDetails 字段。自动补查不计入事实 Loop 的 tool_call_count。

[展开本层完整输入输出](agent-full-flow.html#io-10)

## 11 确定性校验 → 通过或一次修正

来源：`app/plan_validation.py::validate_plan → app/routes.py::route_time_errors`。本次实测。

**输入**

```text
系统补充后的完整 plan + TripRequirements
日期覆盖、天数、明确时刻与起止窗口
相邻路线耗时、交通安排、抵达与离开边界
```

**处理**

- 检查日期非法/重复/乱序/缺漏
- 检查明确时间倒序/重叠/抵离越界
- 检查路线起终点和可用交通窗口
- 未知结束时间保留待核实，不能靠 duration 猜
- 第 1 次失败：附错误与缓存路线反馈模型
- 第 2 次仍失败：failed，不保存新版本

**输出**

```text
第 1 次校验：return None，无异常
planning status=completed；steps=1
本次 repair_plan 次数=0
pending_items 不为空也可以完成并保存草案
```

校验函数真实返回 None 或抛异常；“通过”表示没有检测到已知规则冲突，不表示开放、预约、预算等全部核实。

[展开本层完整输入输出](agent-full-flow.html#io-11)

## 12 生成 AgentReply，返回 Store

来源：`app/agent_client.py::generate_agent_reply → app/store.py::_generate_reply`。本次实测。

**输入**

```text
最终模型 body/card/idea_cards
route_cache["final_plan"]：系统补充后的 plan
可信地点目录 verified_places
需求基线 revision=1；行程基线 id=empty
```

**处理**

- 再次解析最终结构化回复
- 采用缓存中的系统补充计划
- 存在路线待核实项时补充正文说明
- Store 再检查 plan 并加上需求/行程基线
- 模型原始 JSON 没有直接写数据库

**输出**

```text
AgentReply.failed = False
AgentReply.next_action = ask_plan_confirmation
plan = hydrated_plan
verified_places = 22 个候选
requirements_patch = {}
Store 增补 requirements_revision=1
Store 增补 expected_plan_id="empty"
body 摘要：第一版杭州一日行程好了！全程按你的要求：东站公交往返、灵隐寺步行到庆春朴门午餐、下午就在餐厅原地休息，不加茶馆、不住宿。11月1日的天气暂时查不到（超出预报范围），穿衣建议先按杭州深秋常规给出，出发前一周我再帮你确认。
```

本案例是显式 plan 任务；无需先用聊天模型判断生成意图。普通聊天触发生成的额外调用在分支说明中单独列出。

[展开本层完整输入输出](agent-full-flow.html#io-12)

## 13 Store 最终保护 → 生成业务对象与版本

来源：`app/store.py::_agent_reply / _apply_agent_plan / _save_plan_version`。本次实测。

**输入**

```text
AgentReply + 最新 Trip/Plan
写入前需求 revision=1
当前 plan.id="empty"
validated raw_plan + verified_places
```

**处理**

- 核对需求 revision 与 expected_plan_id
- 保存前再次 validate_plan
- 创建 TripPlan / TripDay / TripItem
- 以真实目录生成 PlaceDetails + selected_at
- 以系统结果创建 RouteDetails
- 整份生成使用系统新 ID
- 替换 thinking 消息并生成 v1

**输出**

```text
new plan.id = plan-117
plan.status = 草案
version = v1；id=version-118
8 个条目；5 个条目绑定地点
模型 id → 保存 id：
 → item-108
 → item-109
 → item-110
 → item-111
 → item-112
 → item-113
 → item-114
 → item-115
样例 place.selected_at = 2026-10-05T01:30:02+00:00
所有坐标取自地点目录；没有写入模型坐标
```

整份生成后的 item ID 与模型 ID 不同。单项修改保留目标业务 ID，并只更新授权条目及受影响的路线资料。

[展开本层完整输入输出](agent-full-flow.html#io-13)

## 14 SQLite 持久化与回读结果

来源：`app/store.py::_persist → app/persistence.py::save_snapshot`。本次实测。

**输入**

```text
Store 状态：users/trips/messages/idea_cards/plans/plan_versions/tasks
schema_version=3
本次专用数据库：.cache/agent-flow-demo/d63ce00396/demo.sqlite3
```

**处理**

- serialize_state 生成全量快照
- app_snapshots：key=demo_store_v1
- 同一事务同步旅行、消息、想法、计划、版本表
- tasks 只在快照内，没有独立任务表
- 回读独立数据库并与内存对象比较

**输出**

```text
案例旅行行数 = 1
案例计划行数 = 1
案例消息行数 = 2
案例版本行数 = 1
plan_roundtrip_equal = True
task_roundtrip_equal = True
业务库前后计数：{'trips': 8, 'messages': 47} → {'trips': 8, 'messages': 47}
```

仍是单进程全量快照方案；这次回读验证不代表已经实现跨进程一致性或持久 Loop 恢复。

[展开本层完整输入输出](agent-full-flow.html#io-14)

## 15 任务结果 → 页面显示 → 用户下一步

来源：`app/tasks.py::run_task → tasks.js → workspace.html`。本次实测。

**输入**

```text
任务执行结束：新消息 status=sent
版本数量 0 → 1
GET /api/trips/trip-102/tasks
GET /workspace/trip-102?tab=itinerary
```

**处理**

- run_task 用消息成功与新增版本判断结果
- 持久保存 succeeded / failed
- 浏览器可见时每 2.5 秒轮询任务
- 生成/修改完成后跳转 result_url
- Jinja 渲染按天时间线、地点、路线和待核实项

**输出**

```text
task.status = succeeded
task.message = 已完成
result_url = /workspace/trip-102?tab=itinerary
页面 HTTP=200；全部条目标题匹配=True
8 个时间线条目；待确认 4 条
用户可查看详情、修改条目、确认方案、恢复版本
确认方案不消除 pending，也不提升工具证据覆盖
```

本次真实拿到任务 JSON 与渲染 HTML；本次没有使用真实浏览器点击“确认”或执行修改。JS 轮询、跳转及后续分支按当前源码说明。

[展开本层完整输入输出](agent-full-flow.html#io-15)

## 16 最终时间线：实际保存结果

来源：`输出摘自独立数据库中的 TripPlan，保留原始时刻与时长`。本次实测。

**输入**

```text
最终 saved_plan.days
标题：杭州灵隐一日慢游
内容状态：草案
日期：2026-11-01
用户窗口：10:00—20:00
```

**处理**

- 表中 time 与 duration 均逐字取自保存对象
- 路线字段由系统从可信工具取得
- 停留理由、准备项、风险保留在完整数据区
- 保持待确认信息，不替模型补齐未知值

**输出**

```text
10:00-10:20 | 抵达杭州东站，前往公交站 | 20分钟
10:20-11:28 | 公交前往灵隐寺 | 68分钟
11:28-14:30 | 游览灵隐寺 | 约3小时
14:30-14:47 | 步行前往庆春朴门 | 17分钟
14:47-16:00 | 庆春朴门午餐 | 约1小时13分钟
16:00-17:15 | 餐厅原地休息 | 约1小时15分钟
17:15-18:11 | 公交返回杭州东站 | 56分钟
18:11-20:00 | 东站候车，20:00 离开 | 约1小时49分钟
主要真实移动：
杭州东站 → 灵隐寺：68 分钟（transit）
灵隐寺 → 庆春朴门(灵隐店)：17 分钟（walking）
庆春朴门(灵隐店) → 杭州东站：56 分钟（transit）
```



[展开本层完整输入输出](agent-full-flow.html#io-16)

## 17 最终不确定性：实际 pending 与风险

来源：`saved_plan.pending_items / risks / preparation`。本次实测。

**输入**

```text
所有真实工具结果 + 用户约束 + 模型建议
未知的天气、营业、门票、预约及额外预留
```

**处理**

- 工具查询与建议分别保留来源
- 没有依据的事实继续待核实
- 本次所有已检测规则冲突消除后仍是草案
- 不会将“succeeded”解释为所有事实已核实

**输出**

```text
pending_items（全部）：
2026-11-01 杭州天气暂不可查（超出预报范围），温度、降雨与穿衣建议待出发前确认
灵隐寺（飞来峰景区）门票价格与是否需预约待确认
庆春朴门热门时段可能排队，建议出发前电话 0571-87975777 确认等位情况
公交候车与换乘步行未计入工具耗时，实际耗时可能略长
risks（全部）：
11月1日天气未知：杭州深秋一般10-18°C，偶有阴雨，存在降雨风险
周末灵隐区域客流较大，公交可能拥堵导致耗时增加
餐厅午市高峰可能等位，影响后续休息与返程时间安排
公交班次与路况为未来估算，不保证与实际一致
```

这里按保存数据原样显示，不把模型的“constraints_met”列表当作系统对全部需求的形式证明。另有两处模型正文事实归因未核实，见第 21 层。

[展开本层完整输入输出](agent-full-flow.html#io-17)

## 18 普通聊天分支：当前代码协议

来源：`app/agent_client.py::_request_chat_model → app/store.py::_generate_reply`。源码核对，本次未执行。

**输入**

```text
kind="chat"
payload={body: 用户原文}
requirements + recent messages + current plan
planning_mode=False；max_steps=4
```

**处理**

- 独立 conversation Loop：execute_tools / complete
- 返回正文或结构化聊天回复
- 普通聊天自带的 plan 被忽略
- requirements_patch 须含本轮消息的原文依据
- 列表修改用 add/remove
- next_action=generate_plan 时另调规划入口

**输出**

```text
continue_chat：保存消息与需求更新
ask_plan_confirmation：显示生成/继续补充入口
generate_plan：再执行事实 Loop + planning Loop
show_plan：显示已有计划入口
提问是回复消息；外层 chat task 可为 succeeded
没有进入可持久恢复的 waiting_user 状态
```

分支按代码核对，本次案例没有执行；这里给出协议字段，不伪造聊天运行结果。

[展开本层完整输入输出](agent-full-flow.html#io-18)

## 19 局部修改、失败与恢复分支

来源：`app/tasks.py + app/store.py::request_revision / restore_plan_version`。源码核对，本次未执行。

**输入**

```text
revision payload={item_id, feedback}
最新当前行程、需求、版本和目标条目
或失败任务 ID / 用户选择的历史版本 ID
```

**处理**

- revision → 事实 Loop + planning Loop
- merge_target 合并目标与原邻居后检查
- 模型不能修改邻居规避冲突
- 失败最多修正一次；仍失败保留当前版本
- 重试生成子任务，聊天原文不重复添加
- 重启 queued/running → failed，中断后手动重试

**输出**

```text
成功：只改目标，更新受影响路线，新增草案版本
失败：failed 消息 / task，原行程保持
恢复：旧快照生成新 plan.id 和新版本
过期任务结果因 revision/plan_id 不一致被拒绝
没有任务取消、任务总时限或跨进程队列
```

分支按代码核对；本次没有执行局部修改、重试或恢复。扩大修改范围需用户授权。

[展开本层完整输入输出](agent-full-flow.html#io-19)

## 20 当前 Loop 能力与预算的实际边界

来源：`app/agent_loop.py + 三个业务 Loop 的调用处`。源码核对，本次未执行。

**输入**

```text
AgentLoopState：mode/phase/goal/counters/evidence/history
LoopAction：name/payload/reason
LoopObservation：status/data/next_phase/tool_calls/done/error
```

**处理**

- 步数与工具计数在每个 Loop 内独立
- 批次执行完才累计工具调用并检查超额
- fact Loop 3 步；chat Loop 4 步；planning Loop 2 步
- 自动地点/路线补查在 Loop 计数外
- 路线缓存另有最多 32 个不同路段的限制

**输出**

```text
控制器定义 running / completed / waiting_user / failed / budget_exhausted
业务动作白名单未开放 wait_user / fail
没有统一任务级的模型/工具/总时长预算
Loop evidence 主要保存观察摘要；完整 POI/路线存在共享字典
规划修正阶段不给模型 tools；后端可补查新地点关联路线
热门景点榜单没有独立注册工具；美食查询不是全城榜单
```

这版图明确区分现有运行行为与控制器可表达的能力；不能将通用终态或分层设计视为已接入业务。

[展开本层完整输入输出](agent-full-flow.html#io-20)

## 21 本次输出暴露的正文证据边界

来源：`对比实际模型输出与 query_weather / search_food 工具结果`。本次实测。

**输入**

```text
query_weather：unavailable，日期超出预报范围
search_food：source=高德地图 POI，rating=4.8
最终计划中的风险文本与 meal.recommendation
```

**处理**

- 保留原始模型与保存数据，不事后改写案例
- 将文本声明和实际工具证据逐一对照
- 确定性校验主要覆盖结构、日期、时间和路线
- 没有对所有自然语言声明逐句做来源校验

**输出**

```text
观察 1：模型写“杭州深秋一般10-18°C”
天气工具没有返回该温度，气候数字尚未核实
观察 2：模型写“大众点评/高德评分4.8”
4.8 有高德依据；“大众点评”归因无工具依据
因此：任务成功、版本已保存，但正文事实覆盖仍有限
图中这些文本保留为实际输出，不视为已核实事实
```

本轮只制作流程与证据图，没有修改应用或替模型修正上述文本。后续若继续改造，应收紧正文中事实声明与来源的绑定。

[展开本层完整输入输出](agent-full-flow.html#io-21)

## 证据与检查

- 原始 trace：`.cache/llm_logs/2026-10-05T092858_8918fd7053de.txt`。
- 独立库：`.cache/agent-flow-demo/d63ce00396/demo.sqlite3`。
- 页面 HTML：`.cache/agent-flow-demo/d63ce00396/workspace.html`。
- SVG 已按 XML 解析；完整字段均由本次捕获结果生成。
- 下面是本次运行检查的实际结果。

```json
{
  "plan_roundtrip_equal": true,
  "task_roundtrip_equal": true,
  "workspace_status": 200,
  "task_succeeded": true,
  "business_counts_unchanged": true,
  "workspace_contains_all_item_titles": true,
  "workspace_contains_all_item_ids": true
}
```
