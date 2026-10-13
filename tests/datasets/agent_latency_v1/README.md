# Agent 场景耗时测试集

独立数据集 `agent_latency_v1`，版本 `1.0.0`，共26个场景。目标是记录不同输入下Agent的响应过程、逐次调用等待，以及结果在服务器就绪的时刻，为延迟优化提供可比较证据。业务质量继续使用[v2评测集](../travel_agent_v2/README.md)；本集不自动给自然语言方案打质量通过。

## 场景

| ID | 场景 | 重点 |
| --- | --- | --- |
| LAT-001 / 002 | 短闲聊 / 长上下文闲聊 | 意图与闲聊模型、上下文字符/token、准备开销 |
| LAT-003 | 只保存预算与忌口 | 需求工具、重复审阅、需求保存，不生成行程 |
| LAT-004 / 005 | 缺信息 / 冲突日期 | 追问在哪个阶段发生，是否多余进入规划查询 |
| LAT-006 | 已授权规划的补充与承接 | 追问轮和完成轮各自计时，排除用户思考时间 |
| LAT-007 / 008 / 009 | 天气 / 地点 / 餐饮查询 | 查询前后模型轮次，实际服务获取与结果解析 |
| LAT-010 | 匹配两处地点后查路线 | 有依赖的串行查询链，可信POI匹配 |
| LAT-011 | 组合查询 | 天气、地点、餐饮叠加等待及工具批次 |
| LAT-012 / 013 | 聊天入口 / 页面入口当天规划 | 是否经过main，完整文字与展示等待对照 |
| LAT-014 / 015 | 三日 / 七日规划 | 输出规模、展示转换、共享预算和回退 |
| LAT-016 | 只读解释 | 读取当前原文，是否误触查询或展示 |
| LAT-017 / 018 / 019 | 精确追加 / 局部修改 / 全局重排 | 小改动却重生成全文的成本，修改与展示分开 |
| LAT-020 | 取消等待生成 | 取消反馈路径，不进入长生成 |
| LAT-021 / 022 | 固定短原文 / 长原文展示转换 | 隔离display，输出token、校验和末尾条件 |
| LAT-023 | 受控外部不可用 | 每次50ms注入等待，失败后是否重复查；不是真实故障耗时 |
| LAT-024 | 受控展示超时 | 50ms注入超时，文字仍保存；不改变正式请求超时参数 |
| LAT-025 / 026 | 天气 / 路线缓存诊断 | 无模型，直接经过工具执行器和真实缓存代码，区分函数与后端获取 |

24个Agent/展示/故障场景，2个无模型缓存诊断。LAT-006有两步，全量每次重复记录27个处理步骤。

档案：`smoke` 4场景；`core` 19场景；`full` 26场景；`planning` 4场景；`revision` 4场景；`display` 2场景；`faults` 2场景；`cache` 2场景。默认 `smoke`、每例3次、串行独立进程。

## 计时内容

使用单调时钟，每个执行轨道内形成父子树，包含开始/结束、总耗时、自身耗时和错误。异步后foreground、display、observer等轨道可能重叠：自身耗时只在同一轨道内加和，不能把全部轨道合计当作用户等待。`primary_seconds`记录文字首次服务器响应，`seconds`记录包含后台结束的观测墙钟；observer_wait只为收集测试数据，不是用户等待。

| 层次 | 保存的内容 |
| --- | --- |
| 请求接受 | submit_task、用户消息/思考气泡与任务入队保存 |
| 上下文 | Store快照、模型客户端准备、聊天序列化、角色输入、read_context |
| 意图 | main模型请求、Schema与动作校验、识别结果 |
| 模型逻辑调用 | 角色、模型、消息数、可见输入字符数、可用工具、请求档位、调用前后预算 |
| 每次SDK请求 | 包括兼容重试与失败；协议、timeout、实际档位、Schema字符数、输出字符数、工具建议、token、服务返回的timings |
| 工具执行 | 每次实际名称、角色、参数、结果状态、结果字符数、错误 |
| 外部后端 | 天气定位与预报_fetch_json；高德MCP适配总跨度与每次tools/call分别计时，含响应解析。连接/初始化/工具发现包含于适配开销，不冒充工具调用或纯服务计算时间。 |
| 缓存 | 每次分发前是否有对应缓存键、是否产生后端获取、实际cache_hit |
| 程序校验 | 需求候选、日期窗口、展示Schema与日期，保存前再次校验 |
| 保存 | 快照冲突边界、原文/展示/依据替换、Store序列化、SQLite事务与全量快照 |
| 日志 | 日志组织与隔离目录落盘；模型思考正文不新增采集 |
| 服务器响应 | 任务/消息JSON、聊天片段HTML、行程页HTML，本地ASGI TestClient |

`model_seconds`为每次SDK等待总和，`display_seconds`已包含在其中。`tool_seconds`包含后端与日志/整理，不能重复加后端耗时。报告色条按所选轨道展示自身耗时，时间线共用墙钟展示重叠；总跨度分别检查每条轨道的加和，不要求跨轨道合计等于用户等待。

SDK耗时包含网络、服务排队、预填充与生成，不能据此精确断言纯GPU推理耗时。服务没有报告的token/timings保留null；不从字符数猜token，不把completion token/整段等待称作解码速度。当前调用非流式，首token时间为null。

## 体验时刻

每轮分别记录：请求接受、开始处理、规划/修改文字生成完成、展示转换结束、新行程SQLite保存、回复在Store就绪、任务终态、消息JSON响应就绪、聊天HTML和行程HTML就绪。

`prose_to_reply_store_seconds`检查文字完成到保存的短延迟；新增卡片发布与最终HTML时刻。主任务结束后测量器继续观测独立后台任务，分别报告原文可读与卡片就绪。实际浏览器可见性由独立浏览器回归/测量验证。

实际浏览器可见时间在本ASGI测试中仍为null。源码活跃聊天/任务轮询1秒，空闲分别3秒/2.5秒；卡片pending轮询2秒，运行时读取并记录。轮询间隔是代码推算，不加入服务器实测总耗时。

## 模式与可比较性

- 默认：真实当前模型 + 真实外部工具。高德未配置、天气超范围或服务异常，如实记录unavailable和后端调用数，不补造查询成功。
- `--tools controlled`：真实模型 + 合成服务payload，仍经过应用工具、校验和缓存代码。后端阶段标为controlled_io，不能代表真实外部延迟。合成POI只保留应用必需的source识别字段，响应显式说明它不是实际证据。
- `--simulate`：脚本模型 + 合成服务，仅验证执行、计时和报告；HTML醒目标注模拟结果，不用于模型速度或质量结论。
- LAT-023/024带受控故障注入，单独标记group、fault_hits和等待；不得与正常服务样本合并后宣称上游实际超时。

默认从本机当天开始，旅行日期在3天后；可用 `--base-date`固定日期。一次批次只解析一次日期，冻结完整夹具和输入到 `dataset-resolved.json`。每场景每次重复使用新进程、新模型客户端、独立SQLite和全新旅行快照；同一案例的多轮共享其隔离Store。场景启动、夹具创建、登录、进程导入、用户思考和报告生成不计入单轮响应耗时。

本集没有构造真实线上队列拥堵，默认接受后立即执行；不能把这段很短的启动间隔解释为生产排队延迟。SQLite规模记录在 `store_size`，本次夹具的保存耗时不能直接外推为用户业务库的保存耗时。远端模型/GPU仍可能有其他负载，重复测试顺序也可能影响缓存或热身，应比较同输入、同配置、同源码的结果。

## 运行

从项目根目录使用conda环境。输出必须是新目录，业务缓存目录不可用作本集输出。真实测速时保持服务模型参数与正式环境一致，后续参数对照分别新建批次。

查看输入与档案，不调用模型：

```powershell
& E:\anaconda\envs\TravelAssistant\python.exe tests/performance/run_agent_latency.py --profile full --list
```

冒烟真实测速，先每例一次确认链路：

```powershell
& E:\anaconda\envs\TravelAssistant\python.exe tests/performance/run_agent_latency.py --profile smoke --repeats 1 --output "$env:TEMP\agent-latency-smoke-$(Get-Date -Format yyyyMMdd-HHmmss)"
```

完整26场景，每例3次；长规划可能达到当前240秒共享预算，失败等待也会记录。该批次会调用真实模型和外部服务：

```powershell
& E:\anaconda\envs\TravelAssistant\python.exe tests/performance/run_agent_latency.py --profile full --repeats 3 --output "$env:TEMP\agent-latency-full-$(Get-Date -Format yyyyMMdd-HHmmss)"
```

定向对照：

```powershell
& E:\anaconda\envs\TravelAssistant\python.exe tests/performance/run_agent_latency.py --cases LAT-012 LAT-013 LAT-017 --repeats 3 --tools controlled --base-date 2026-10-11 --output "$env:TEMP\agent-latency-compare-$(Get-Date -Format yyyyMMdd-HHmmss)"
& E:\anaconda\envs\TravelAssistant\python.exe tests/performance/run_agent_latency.py --profile display --repeats 3 --output "$env:TEMP\agent-latency-display-$(Get-Date -Format yyyyMMdd-HHmmss)"
& E:\anaconda\envs\TravelAssistant\python.exe tests/performance/run_agent_latency.py --profile cache --repeats 3 --output "$env:TEMP\agent-latency-cache-$(Get-Date -Format yyyyMMdd-HHmmss)"
```

离线记录验证：

```powershell
& E:\anaconda\envs\TravelAssistant\python.exe tests/performance/run_agent_latency.py --profile full --repeats 1 --simulate --output "$env:TEMP\agent-latency-demo-$(Get-Date -Format yyyyMMdd-HHmmss)"
& E:\anaconda\envs\TravelAssistant\python.exe -m pytest tests/automated/test_agent_latency.py -q
```

## 输出与定位方式

- `report.html`：离线交互报告，场景/尝试/步骤选择、可加和耗时色条、嵌套时间线、逐调用详情、就绪时刻。点击SDK或工具条查看参数、token、缓存和错误。
- `report.md`：可分享的场景统计与逐次关键调用表。
- `summary.json`：每案例每步骤的全部等待/成功等待统计、中位数/P90/最大值、失败等待、回退和主要自身耗时类别。少于3次不应当作稳定性结论。
- `spans.csv`：每条跨度展开为一行，UTF-8 BOM，方便Excel筛选角色、工具、开始/结束与自身耗时。
- `results.json`、`<case>/attempt-N/evidence/step-N/result.json`：输入、前后快照、回复、任务、完整span/event、指标、客观覆盖检查。失败也保留。
- `chrome-trace.json`：每轮可导入Chrome/Perfetto的X事件时间线。
- `metadata.json`、`dataset-resolved.json`、`source-manifest.json`、`source/`：当前模型、协议、档位、数据日期、源码与配置指纹，无密钥和.env备份。
- `execution.log`：子进程的实时阶段开始和完成事件，执行中可查看当前卡在哪一步。
- `execution-errors.json`：进程异常或硬超时；已落盘步骤不会删除。

先看同场景重复样本的总等待分布，再看主要自身耗时类别；用SDK逻辑调用与实际请求数定位兼容重试，用工具/后端调用数和cache_hit定位重复查询。若文字完成后仍有长等待，再看display与保存阶段；若服务器就绪很快但页面体验慢，需要另测轮询、网络和浏览器渲染。本报告记录客观覆盖检查，不以完成速度替代修改忠实度或展示语义评审。

只读业务快照比较执行前后payload；测量不向8000业务服务提交请求。运行器临时安装计时hook，样本结束恢复，不修改应用源码或正式模型参数。
