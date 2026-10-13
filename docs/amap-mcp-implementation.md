# 高德地图切换MCP：实施与验收

2026-10-13。已把地点、餐厅与步行/公交/驾车路线查询从高德REST业务接口切换到官方MCP Streamable HTTP。天气继续Open-Meteo。应用仍向LLM提供既有旅行工具，由Python适配层调用MCP；没有让模型持有Key或直接访问高德全部工具。

## 当前调用链

```mermaid
flowchart LR
    A[业务LLM工具调用] --> B[search_places / search_food / query_route]
    B --> C[参数与可信POI校验 / 路线缓存]
    C --> D[MCP初始化与工具发现]
    D --> E[官方MCP tools/call]
    E --> F[响应解析与现有业务结果适配]
    F --> A
```

官方接入依据：[快速接入](https://lbs.amap.com/api/mcp-server/gettingstarted)、[工具说明](https://lbs.amap.com/api/mcp-server/tools)。真实发现服务名amap-sse-server、版本1.0.0、协议2025-03-26；传输实际使用官方推荐的Streamable HTTP，名称里的sse不代表本项目使用旧SSE传输。

| 应用工具 | MCP调用 | 适配处理 |
| --- | --- | --- |
| search_places | maps_text_search + maps_search_detail | 搜索结果不带坐标，最多5个详情补齐坐标；校验详情ID、有效坐标，不编造地点 |
| search_food | maps_text_search + maps_search_detail | 最多5个详情；MCP评分/人均/营业字段在顶层，适配为原应用字段；缺电话/标签保持null |
| query_route | maps_direction_walking / driving / transit_integrated | 坐标从可信POI目录读取；步行返回route.paths，驾车返回顶层paths，公交返回顶层transits，统一转换；距离缺失时仅使用实际提供的路线整体distance |
| query_weather | 原Open-Meteo | 不调用高德MCP天气 |

LLM可以继续用原工具名和参数。可信候选的字段保持PlaceDetails契约，路线保持RouteDetails契约，原地图链接/存量行程仍可读取；MCP来源元数据只放在不会破坏旧对象构造的工具结果位置。服务返回评分不证明忌口安全，营业和人均仍需现场核实。餐厅排序只针对这次已获取评分的候选，非全城最高排名。

## 配置与依赖

```env
AMAP_MAP_KEY=原高德Web服务Key
AMAP_MAPS_API_KEY=
AMAP_MCP_URL=https://mcp.amap.com/mcp
AMAP_MCP_TIMEOUT_SECONDS=20
```

AMAP_MAPS_API_KEY非空时优先，否则读取现有AMAP_MAP_KEY；当前直接复用已有Key，无需改用户密钥。Key在服务端附加到连接URL，不进入模型/tool arguments/评测元数据；HTTP/MCP日志及异常堆栈均脱敏。URL要求HTTPS，不跟随重定向。没有REST备用回退。

新增Python SDK依赖mcp==1.26.0。约束sse-starlette<3.1和starlette<0.49以匹配现有FastAPI0.118.3；安装过程中发现新版SDK相关依赖会升级到不兼容的Starlette，已恢复兼容版本，pip check通过。Node.js只用于浏览器测试，不是生产MCP连接必需。

每次应用工具创建独立MCP会话并关闭，初始化、分页工具发现和工具查询共享默认20秒总截止，可配置最多120秒，且不超过主业务剩余期限。使用同步桥接到SDK异步客户端，支持在业务线程或已有async loop中调用；无跨线程复用SDK会话。

只允许发现并调用上述5个只读工具，校验发现的inputSchema；导航、打车、自定义地图等工具不开放。工具清单分页最多8页，一个查询最多6次tools/call（一次搜索+5个详情），不会无限扇出。

主业务仍16模型/64应用工具/32外部查询/240秒。现有调度计入首个外部查询，MCP每个额外详情也计数；到达预算或任务取消时不再调用，预算异常不能被SDK异常组吞成普通成功。路线命中缓存、相同POI不新增网络请求。失败按现有规则保留未知与原行程，权限/超时不自动重试，也不退回REST。

## 自动与浏览器验收

- 最终全量529项自动回归通过，50.41秒；新增20项MCP测试使用真实SDK协议与MockTransport，覆盖initialize/通知/tools-list/call、text JSON及structuredContent、权限与格式错误、详情ID错配/坐标无效、外部预算、超时、缺Key、同步/异步桥接、餐厅缺字段、工具白名单与日志/异常堆栈脱敏。
- 后续只调整路线缺失distance判定与日志堆栈脱敏，相关25项/20项定向复查通过，不重复执行无关全量回归。
- 地点/路线浏览器回归通过，旧地图/预计耗时/待核实信息可读；异步展示浏览器回归通过，正文先可读、草稿和阅读位置保留、只重试卡片不重规划。
- 性能测量器同步区分MCP适配开销和每次tools/call，既有26个受控场景回归通过；合成工具资料不能证明真实MCP速度。连接/发现没有被重复算作工具调用，查询与缓存实际跨度分别记录。
- 首次完整回归的3项失败记录保留：可信POI额外字段导致目录过滤/缓存诊断失败、旧路线对象不接受transport字段、健康测试仍假设旧静态版本字符串。前两项已修复，第三项改为检查接口返回有效版本；旧路线测试补齐地点查询替身，避免用假Key访问真实服务。

## 真实查询证据及限制

先用现有Key做官方MCP发现，随后获取地点详情、餐厅详情及三种路线，全部返回有效JSON。

第一轮应用适配冒烟7个步骤成功：

| 步骤 | 状态 | 应用等待 |
| --- | --- | ---: |
| 杭州东站地点搜索 | available，3个有效候选 | 1.279秒 |
| 西湖湖滨公园地点搜索 | available，3个有效候选 | 0.836秒 |
| 杭帮菜餐厅搜索，最多3家 | available，3家 | 0.804秒 |
| 两个候选间步行路线 | available | 0.294秒 |
| 两个候选间公交路线 | available | 0.514秒 |
| 两个候选间驾车路线 | available | 0.302秒 |
| 同一公交路线再次查询 | 命中缓存，无新增MCP请求 | 约0秒 |

实际19次MCP tools/call。第一轮为单次直接适配冒烟，尚未验证长期稳定性；其中来源额外字段随后根据回归结果移出POI/路线对象，可信绑定通过SDK协议测试验证。没有把第一轮直接构造目录的成功替代成最终完整业务链路质量验收。

后续补验存在真实失败：

- 09:52轮首个地点查询20.05秒回退unavailable，随后第二个在途请求被执行工具40秒截止终止；已完成和中断分开记录，不算第二次完成失败。
- 09:54独立补验两个地点分别20.04/20.02秒回退；一个在搜索阶段超时，另一个未进入工具调用，可能在初始化/发现；餐厅随后10.22秒成功，5个实际工具请求记录保存。
- 10:00最终只读冒烟在发现阶段阻塞，0个应用查询/0个工具调用，工具报告blocked。程序不隐式改超时或重试，不据健康接口推断服务已恢复。

因此MCP接入已实现，现有Key曾真实成功，上游可用性在本次窗口有波动；不能承诺稳定成功率或比REST更快。失败返回待核实，原文交付和行程修改能力继续保留。没有运行真实LLM完整规划全量评测，本轮证明协议/适配/保护和部分实际查询，非所有旅行内容质量通过。

复跑独立只读工具验收（不使用LLM/Store/8000业务服务）：

```powershell
& E:\anaconda\envs\TravelAssistant\python.exe tests/performance/check_amap_mcp.py --output "$env:TEMP\amap-mcp-check-$(Get-Date -Format yyyyMMdd-HHmmss)"
```

输出目录必须为空，连续两次地点不可用即阻塞，首次失败不覆盖。真实证据归档见[evaluations/amap-mcp-20261013](evaluations/amap-mcp-20261013/README.md)。

## 本地加载与业务数据

确认无主业务或展示任务，备份后加载新后端，本地服务http://127.0.0.1:8000，最终PID21704（首次加载PID17176），health正常。切换前后14趟旅行、85条消息及全部计划数据指纹（含正文与卡片）一致，数据库schema仍为5，不做结构迁移。

首次备份`.cache/backups/travelassistant-before-schema-20261013-095924-890303.sqlite3`，日志堆栈脱敏补强后再次加载，备份`travelassistant-before-schema-20261013-100434-616515.sqlite3`。没有提交或推送远程。
