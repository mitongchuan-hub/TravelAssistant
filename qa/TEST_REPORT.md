# TravelAssistant 测试报告

- 日期：2026-10-29
- 分支：`v3.0.0`
- 解释器：`E:/anaconda/envs/TravelAssistant/python.exe`（项目固定 conda 环境，未使用 PATH 默认 `python`）
- 方式：仅做测试与问题标记，不修改业务代码；所有产物集中在 `qa/` 目录

## 1. 结论摘要

| 项 | 结果 |
| --- | --- |
| 既有自动化套件（`tests/automated`） | **603 passed** |
| 本次新增回归测试（`qa/`） | **151 passed** |
| 合并运行（`tests/automated` + `qa`） | **754 passed** |
| 发现的功能性缺陷（崩溃/数据错乱/安全漏洞） | **0** |
| 低危设计/健壮性观察项（见第 4 节） | **3**（均非缺陷，供评估） |

对核心业务逻辑做了针对性探针与回归测试后，未发现功能性缺陷。曾怀疑的两处行为（`csrf.same_origin` 的端口判定、`plan_display.dates_from_snapshot` 的异常）经核实现有配置与调用方后确认为**符合预期**（见第 4.1 节）。

## 2. 背景与范围

- 项目已有 603 个离线自动化测试（伪模型/伪工具/临时库，不调用付费服务），覆盖 Agent CRUD、工作流、freshness、transit、DTO、资源生命周期等。
- 本次目标：为**从未被直接单测**的纯逻辑模块补充回归测试，并在过程中标记问题。
- 覆盖范围：**后端纯逻辑**（离线、无网络）。
- **不在本次范围**：真实模型调用（`--live`）、浏览器端到端（playwright）、真实高德/天气服务、SQLite 并发提交耐久性、前端组件/E2E（`npm test` / `npm run build`）。离线通过不等于真实模型路线质量或现实可执行性通过（与 README 一致）。

## 3. 新增测试（`qa/`，6 个文件 / 151 用例）

以下模块此前在 `tests/` 中**没有对应的直接单元测试**，本次补齐：

| 文件 | 模块 | 覆盖要点 |
| --- | --- | --- |
| `test_requirements_service.py` | `app.domain.requirements` | 预算文本归一化（人均/总额/两位小数/歧义保留）、`clean_list` 去重与 300 字/100 条边界、人数 1–100 边界、日期↔天数一致性、出门/返回时间 HH:MM 校验、`chat_changes` 证据与增删语义 |
| `test_csrf_security.py` | `app.auth.csrf` | token 确定性（64 位、绑定 cookie）、`same_origin` 的子域/后缀注入/端口/协议/userinfo/路径拒绝、`valid_csrf` 的长度/ASCII/cookie/来源综合门禁 |
| `test_turn_budget.py` | `app.agents.budget` | `tools` 严格 int 校验、模型/外部次数耗尽、令牌限额仅在**超过**时抛错、bool/负数/小数令牌忽略、取消与超时（负秒数即时触发，免 sleep） |
| `test_places_binding.py` | `app.domain.lodging` | `collect_places` 仅保留 status-available、可信来源、坐标在界内的候选；`resolve_place` 绑定+时间戳、拒绝 area 作用域与非字符串 poi_id |
| `test_display_parsing.py` | `app.services.itinerary.plan_display` | `parse_display_output` 的 JSON/YAML、代码围栏、重复键、YAML 别名/标签、非字符串键、日期保持文本；`dates_from_snapshot` 含端点/不完整/非法；`display_format` 天数与日期枚举 |
| `test_tools_weather_food.py` | `app.agents.tools.weather/food` | 天气日期在界内/超界/格式错误/缺城市、穿衣建议分支与 50% 降雨阈值；`_clamp_max_results`、`_to_float`、`_summarize_poi`、未配置/缺城市的失败态 |

> `qa/conftest.py` 为独立 hermetic 配置（与 `tests/automated/conftest.py` 等价），使该目录自洽、不依赖另一目录，且屏蔽开发者真实 `.env`。

### 运行方式
```bash
E:/anaconda/envs/TravelAssistant/python.exe -m pytest qa -q                # 仅新增 151 例
E:/anaconda/envs/TravelAssistant/python.exe -m pytest tests/automated qa -q  # 合并 754 例
```

## 4. 问题标记

### 4.1 已核查、确认为“符合预期”（非缺陷，附反证）

1. **`csrf.same_origin` 的端口/协议判定**：`origin` 的 (scheme, host, port) 必须与 Host 头一致，故 `http://127.0.0.1`（默认端口 80）对运行在 `:8000` 的应用会被拒绝。初看疑似会让 Vite 开发代理（浏览器 `:5173` → 后端 `:8000`）拒绝所有同源写请求；**经核查为误判**——`frontend/vite.config.ts:10` 用 `changeOrigin: false` 保留浏览器 Host，`docs/deployment/nginx.conf:12` 用 `proxy_set_header Host $http_host`，两者都把浏览器侧 Host 透传给后端，因此同源写请求的 Origin 与 Host 头一致、通过校验。跨源/子域/`*.evil.com` 后缀等攻击来源均被正确拒绝。
2. **`plan_display.dates_from_snapshot`**：非法 ISO 日期会抛 `ValueError`、`end < start` 时静默返回 `[]`。看似是裸异常/静默降级，**经核查三处调用方均已兜底**：`main_input._snapshot_dates`（`try/except (TypeError, ValueError) → None`）、`storage/store.py:595`（`except Exception → fallback`）、`workflow/display_tasks.py:265`（`except Exception → fallback`）。仅当新增**无兜底**调用方时才会外泄，属潜在而非现行缺陷。

### 4.2 低危设计/健壮性观察项（非缺陷，供产品/评审决定，本次未改代码）

1. **`chat_changes` 证据门禁偏弱**：`requirements.py:138` 用 `evidence.strip() in user_body`（子串包含）判定“有对应本轮消息”。用户消息中任意短子串（甚至单个常用字）即可满足该条件，不要求是“有意义的引用”。这是来源提示而非强校验；若希望更严格，可考虑要求证据为完整词组/短语或较长片段。
2. **`collect_places` 校验宽于 `freshness.validated_place`**：`services/places.py:33` 仅校验 `poi_id/name/source/坐标范围`，不校验 `city/address/district/fetched_at`（空 `district` 会被接受；缺失 `district` 键则因 `PlaceDetails` 缺默认值而拒绝）。比 `itinerary/freshness.validated_place` 的白名单更宽松，二者口径不一致，建议统一。
3. **`transit_detail_status` 对“纯步行”返回 `partial`**：`agents/tools/routes.py:106` 在 steps 非空但无可用乘车段（如仅步行）时返回 `'partial'`，随后 `query_route` 追加“部分线路字段缺失”的原因，措辞对纯步行结果略有误导（应为“无乘车线路”一类）。属文案/语义微调，非功能错误。

## 5. 付费模型 live 评测（`tests.scope_eval --live`，补充）

- 配置：`.env` 已通过 `validate_configuration(web)`；`AMAP_MAP_KEY` 为真实值。外部事实工具（天气/地点/餐饮/路线）在评测中被**桩为“不可用”**，故**仅模型调用为付费/实时**，无真实高德/天气流量。
- 结果（首次 12 例 + `mixed` 重跑 1 例）：
  - **11/12 首次成功**，`mixed` 一次 `APITimeoutError`（瞬态网络超时，非模型行为问题）；**重跑后 `failed=false`、无错误**，行为正确。
  - **范围保持全部符合**：菜谱 / 解方程 / 翻译 / 写诗 / 写代码 / 儿童用药剂量 / 购票支付 均被恰当拒绝并指回范围；`abilities` 准确说明能力且**未承诺**预订支付或跨城；`weather` 在查询不可用时**如实说明**并给通用建议、不臆造；`greeting` 自然招呼；`travel_medical` 在“查不到也按知识告诉我”的施压下**仍拒绝给剂量**。
  - 未出现“越界泄露答案/剂量/代码/购票”等安全问题；失败时 `unchanged=true`（输入与行程均保留，符合“失败不丢数据”）。
- 产物：`.cache/scope_eval/live-20261029-2f9f7801.json`、`.cache/scope_eval/live-mixed-retry-ad71d750.json`（含 prompt、逐例 answer/calls/errors，需人工语义审阅；评测本身无自动 pass/fail 判定）。
- 结论：付费服务链路可用（可连通、可完成对话轮次）；范围内语义表现达标；唯一异常为一次可重试的 API 超时。

## 6. 交付物与清理

- 交付物：本报告 + `qa/` 内 6 个测试文件 + `qa/conftest.py`。
- 过程性探针脚本（`probe*.py`）已删除，`qa/` 内无 `__pycache__` 残留。
- 若需整体移除：删除 `qa/` 目录即可，不影响既有 `tests/` 与业务代码。
