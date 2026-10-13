# Agent 场景耗时报告

模型模式：live；工具模式：live；模型：qwen3.8:27b。
记录1个处理步骤；进程错误0个。业务数据快照未变化：True。

文字生成完成与保存/服务器响应就绪分别记录；这里没有测量实际浏览器可见时间和首token。

| 场景 / 步骤 | 样本 | 文字首次响应中位数 | 含后台完成中位数 | P90 | 最大 | 状态 | 主要耗时类别 |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| LAT-015 / 1 七日六晚规划 | 1 | 34.532s | 104.327s | 104.327s | 104.327s | {"succeeded": 1} | 展示模型 69.759s |

## 逐场景调用

### LAT-015 第1次 / 步骤1：七日六晚规划

状态 `succeeded`，总耗时 104.327s；SDK请求 3 次，工具 3 次，后端获取 2 次，缓存命中 0 次。

| 开始 | 步骤 | 总耗时（含子调用） | 自身耗时 | 状态 |
| ---: | --- | ---: | ---: | --- |
| 0.000s | SQLite全量快照保存 | 0.010s | 0.000s | completed |
| 0.001s | SQLite事务写入 | 0.010s | 0.010s | completed |
| 0.011s | SQLite全量快照保存 | 0.010s | 0.000s | completed |
| 0.011s | SQLite事务写入 | 0.009s | 0.009s | completed |
| 0.021s | SQLite全量快照保存 | 0.010s | 0.000s | completed |
| 0.021s | SQLite事务写入 | 0.010s | 0.010s | completed |
| 0.152s | SDK请求：planning | 9.677s | 9.677s | completed |
| 9.829s | 工具：read_context | 0.000s | 0.000s | completed |
| 9.829s | 工具：read_context | 0.000s | 0.000s | completed |
| 9.829s | 工具：query_weather | 5.098s | 0.000s | completed |
| 9.830s | 服务获取与解析：weather | 2.723s | 2.723s | completed |
| 12.552s | 服务获取与解析：weather | 2.375s | 2.375s | completed |
| 14.928s | SDK请求：planning | 19.463s | 19.463s | completed |
| 34.394s | SQLite全量快照保存 | 0.011s | 0.000s | completed |
| 34.394s | SQLite事务写入 | 0.011s | 0.011s | completed |
| 34.405s | SQLite全量快照保存 | 0.010s | 0.000s | completed |
| 34.405s | SQLite事务写入 | 0.010s | 0.010s | completed |
| 34.415s | SQLite全量快照保存 | 0.011s | 0.000s | completed |
| 34.416s | SQLite事务写入 | 0.010s | 0.010s | completed |
| 34.429s | 消息JSON响应 | 0.002s | 0.002s | completed |
| 34.430s | 聊天片段HTML渲染 | 0.011s | 0.011s | completed |
| 34.441s | 行程页HTML渲染 | 0.091s | 0.091s | completed |
| 34.443s | SDK请求：display | 69.759s | 69.759s | completed |
| 104.276s | SQLite全量快照保存 | 0.012s | 0.000s | completed |
| 104.277s | SQLite事务写入 | 0.012s | 0.012s | completed |

就绪时刻：`{"request_accepted_seconds": 0.010980699998981436, "processing_started_seconds": 0.010983899999700952, "prose_generated_seconds": 34.3912251999991, "display_conversion_finished_seconds": 104.26878269999906, "new_plan_persisted_seconds": 34.40481170000021, "reply_store_ready_seconds": 34.40486559999954, "task_terminal_seconds": 34.426530299999285, "reply_json_ready_seconds": 34.430265599999984, "chat_html_ready_seconds": 34.44118499999968, "itinerary_html_ready_seconds": 34.53185069999927, "cards_published_seconds": 104.276464999999, "cards_html_ready_seconds": 104.32674459999907, "first_token_seconds": null, "browser_visible_seconds": null, "prose_to_reply_store_seconds": 0.013640400000440422, "display_queue_and_execution_seconds": 69.87159939999947, "task_status": "succeeded", "scope": "Local Store + isolated ASGI response. Browser transport/render/poll wait not measured.", "poll_intervals_seconds": {"app": 3.0, "app_active": 1.0, "tasks": 2.5, "tasks_active": 1.0}}`。
