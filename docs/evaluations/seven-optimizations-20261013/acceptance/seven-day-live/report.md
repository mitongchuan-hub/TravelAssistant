# Agent 场景耗时报告

模型模式：live；工具模式：live；模型：qwen3.8:27b。
记录1个处理步骤；进程错误0个。业务数据快照未变化：True。

文字生成完成与保存/服务器响应就绪分别记录；这里没有测量实际浏览器可见时间和首token。

| 场景 / 步骤 | 样本 | 文字首次响应中位数 | 含后台完成中位数 | P90 | 最大 | 状态 | 主要耗时类别 |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| LAT-015 / 1 七日六晚规划 | 1 | 41.014s | 41.016s | 41.016s | 41.016s | {"failed": 1} | 规划模型 34.937s |

## 逐场景调用

### LAT-015 第1次 / 步骤1：七日六晚规划

状态 `failed`，总耗时 41.016s；SDK请求 3 次，工具 15 次，后端获取 28 次，缓存命中 6 次。

| 开始 | 步骤 | 总耗时（含子调用） | 自身耗时 | 状态 |
| ---: | --- | ---: | ---: | --- |
| 0.001s | SQLite全量快照保存 | 0.010s | 0.000s | completed |
| 0.001s | SQLite事务写入 | 0.010s | 0.010s | completed |
| 0.011s | SQLite全量快照保存 | 0.010s | 0.000s | completed |
| 0.011s | SQLite事务写入 | 0.010s | 0.010s | completed |
| 0.021s | SQLite全量快照保存 | 0.011s | 0.000s | completed |
| 0.021s | SQLite事务写入 | 0.010s | 0.010s | completed |
| 0.210s | SDK请求：planning | 8.168s | 8.168s | completed |
| 8.378s | 工具：check_requirements | 0.000s | 0.000s | completed |
| 8.379s | 工具：read_context | 0.000s | 0.000s | completed |
| 8.379s | 工具：read_context | 0.000s | 0.000s | completed |
| 8.379s | SDK请求：planning | 12.059s | 12.059s | completed |
| 20.438s | 工具：query_weather | 1.500s | 0.000s | completed |
| 20.438s | 服务获取与解析：weather | 0.722s | 0.722s | completed |
| 21.161s | 服务获取与解析：weather | 0.777s | 0.777s | completed |
| 21.938s | 工具：query_weather | 0.000s | 0.000s | completed |
| 21.939s | 工具：query_weather | 0.000s | 0.000s | completed |
| 21.939s | 工具：query_weather | 0.000s | 0.000s | completed |
| 21.939s | 工具：query_weather | 0.000s | 0.000s | completed |
| 21.939s | 工具：query_weather | 0.000s | 0.000s | completed |
| 21.939s | 工具：query_weather | 0.000s | 0.000s | completed |
| 21.940s | 工具：search_places | 1.093s | 0.000s | completed |
| 21.940s | 服务获取与解析：places | 1.093s | 0.325s | completed |
| 23.033s | 工具：search_places | 0.961s | 0.000s | completed |
| 23.033s | 服务获取与解析：places | 0.960s | 0.277s | completed |
| 23.994s | 工具：search_places | 0.875s | 0.000s | completed |
| 23.994s | 服务获取与解析：places | 0.874s | 0.224s | completed |
| 24.868s | 工具：search_places | 0.882s | 0.000s | completed |
| 24.868s | 服务获取与解析：places | 0.882s | 0.217s | completed |
| 25.751s | SDK请求：planning | 14.710s | 14.710s | completed |
| 40.461s | 工具：search_places | 0.445s | 0.000s | error |
| 40.461s | 服务获取与解析：places | 0.445s | 0.261s | error |
| 40.908s | SQLite全量快照保存 | 0.011s | 0.000s | completed |
| 40.908s | SQLite事务写入 | 0.011s | 0.011s | completed |
| 40.919s | SQLite全量快照保存 | 0.011s | 0.000s | completed |
| 40.919s | SQLite事务写入 | 0.011s | 0.011s | completed |
| 40.936s | 消息JSON响应 | 0.001s | 0.001s | completed |
| 40.937s | 聊天片段HTML渲染 | 0.012s | 0.012s | completed |
| 40.949s | 行程页HTML渲染 | 0.065s | 0.065s | completed |

就绪时刻：`{"request_accepted_seconds": 0.01074109999990469, "processing_started_seconds": 0.010742499999651045, "prose_generated_seconds": null, "display_conversion_finished_seconds": null, "new_plan_persisted_seconds": null, "reply_store_ready_seconds": 40.91870840000047, "task_terminal_seconds": 40.930244299999686, "reply_json_ready_seconds": 40.9370342000002, "chat_html_ready_seconds": 40.94866980000006, "itinerary_html_ready_seconds": 41.01391289999992, "cards_published_seconds": null, "cards_html_ready_seconds": 41.016485199999806, "first_token_seconds": null, "browser_visible_seconds": null, "prose_to_reply_store_seconds": null, "display_queue_and_execution_seconds": null, "task_status": "failed", "scope": "Local Store + isolated ASGI response. Browser transport/render/poll wait not measured.", "poll_intervals_seconds": {"app": 3.0, "app_active": 1.0, "tasks": 2.5, "tasks_active": 1.0}}`。
