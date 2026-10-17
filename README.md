# TravelAssistant · 同城游玩

以已确认的住宿为起终点，生成、阅读和调整同城游玩路线。使用 FastAPI、Jinja2、原生 JavaScript/CSS 和 SQLite，当前按单进程运行。

## 使用方式

1. 注册或登录，填写住宿地址或酒店名称，选择实际地图候选。
2. 可选填写日期、天数、每日出门/返回时间、人数、预算与偏好；默认一天，未知信息不补成事实。
3. 在聊天中说“生成路线”，或使用聊天菜单、想法页、行程页的生成入口。
4. 先阅读完整文字，后台再整理按天卡片；卡片失败可单独重试，不丢失正文。
5. 通过聊天或卡片“调整此项”修改当前方案。设置变化不会自动重排行程。

默认聊天，底栏保持“想法／聊天／行程”；功能优化不重新排列既有页面。每日从同一住宿出发并返回，可信地点可打开高德导航，未知地点保持待核实。查询耗时只是查询时估算，不保证未来开放、班次、票价或现实可执行性。

旧完整旅行仍保留原需求、聊天和行程，不自动转换为同城模式。每趟旅行只保留一份当前方案，没有方案历史恢复入口。

## 环境与安装

- Python 3.11 或更高版本，建议使用虚拟环境。
- 可访问的 OpenAI 兼容模型服务及 API Key。
- 高德地图 Web 服务 Key，用于住宿匹配、地点和路线查询。天气使用 Open-Meteo，无需额外 Key。

在项目根目录运行：

```powershell
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

编辑 `.env`，至少填写有效的模型和地图凭证。以下是配置形状；服务地址与模型名按实际供应商修改，不表示所有供应商支持相同协议：

```env
OPENAI_API_KEY=你的模型服务密钥
OPENAI_BASE_URL=https://mirror.xinshu.ai/v1
OPENAI_MODEL=供应商支持的模型名
OPENAI_FALLBACK_MODEL=
OPENAI_WIRE_API=responses
OPENAI_STORE_RESPONSES=false

AMAP_MAP_KEY=你的高德地图Web服务Key
AMAP_MAPS_API_KEY=
AMAP_MCP_URL=https://mcp.amap.com/mcp
AMAP_MCP_TIMEOUT_SECONDS=20

CITY_DB_PATH=.cache/cityassistant.sqlite3
TRAVEL_PERSISTENCE=1
TRAVEL_DISPLAY_ENABLED=1
TRAVEL_DISPLAY_OUTPUT_FORMAT=yaml
```

- `OPENAI_WIRE_API` 支持 `responses` 和 `chat_completions`；代码默认后者，环境模板选择前者。
- `OPENAI_FALLBACK_MODEL` 留空关闭备用模型；配置后在主模型限流时最多切换一次。
- `AMAP_MAPS_API_KEY` 非空时优先于 `AMAP_MAP_KEY`；凭证仅由服务端使用。
- `TRAVEL_DISPLAY_ENABLED=0` 关闭后台卡片整理，正文仍可交付。
- `TRAVEL_PERSISTENCE=0` 不持久保存状态，仅适合隔离调试；正常使用保持开启。
- 其他配置项见 `.env.example`；实际参数处理以 `app/agent_roles.py`、`app/model_transport.py` 和 `app/tools/amap_mcp.py` 为准。

不要提交 `.env`、SQLite 数据库或包含用户对话的运行记录。

## 启动

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000>，健康检查为 <http://127.0.0.1:8000/health>。当前不支持多 worker 或多进程部署，不要增加 `--workers`。

通过 HTTPS 代理部署时，应正确传递原始协议；需要强制安全 Cookie 时配置 `TRAVEL_COOKIE_SECURE=1`。这不替代 HTTPS、访问控制或数据库备份。

`main` 使用 `CITY_DB_PATH`，默认 `.cache/cityassistant.sqlite3`。改造前完整版源码保留在 `travel-full`，该分支使用自己的 `TRAVEL_DB_PATH` 和数据库。切换前停止服务，核对对应分支配置；Git 只保存源码，不备份或同步业务数据库。

## 执行与恢复

执行策略固定为 `task_execution`：`main_execution` 在有界工具循环中组合需求、查询、生成、修改四个专业能力，候选经 `TurnWorkspace` 汇总后由 `TurnCommit` 一次性原子提交；旧意图分流框架已移除。策略在任务提交时固定，历史 `legacy` 任务在恢复时自动归一为 `task_execution`。

正文先保存，卡片独立派生。新消息会替代正在执行的旧轮；版本变化、清空聊天或新编辑使旧结果失效。失败保留已有方案和输入，可重试；服务重启不自动重跑写入任务。清空聊天保留需求和当前行程，但撤销待补充目标与未完成任务。

这些保护防止旧结果覆盖新状态，不保证生成内容完全正确。没有独立的第二模型复核步骤，保存成功或卡片就绪均不等于事实、预算或路线质量通过验收。

## 任务接口

以下接口要求登录并拥有对应旅行，具体参数校验以 `app/main.py` 为准：

| 接口 | 用途 |
| --- | --- |
| `POST /api/trips/{trip_id}/tasks` | 提交 `kind`（chat/plan/revision）、`payload` 和稳定的 `request_id`；新任务返回 202 |
| `GET /api/trips/{trip_id}/tasks` | 查询近期任务及状态 |
| `POST /api/trips/{trip_id}/tasks/{task_id}/retry` | 重试失败任务，使用新的 `request_id` |
| `GET /api/trips/{trip_id}/plan-state` | 查询当前正文绑定与卡片状态 |
| `POST /api/trips/{trip_id}/display` | 只重试当前正文的卡片，不重新规划 |

## 项目导航

| 路径 | 用途 |
| --- | --- |
| [PRODUCT.md](PRODUCT.md) | 产品目标、当前业务规则、兼容与非目标 |
| [DESIGN.md](DESIGN.md) | 既有视觉、排版及交互状态保护 |
| `app/main.py` | Web 路由与任务接口 |
| `app/store.py`、`app/persistence.py` | 业务状态与 SQLite 持久化 |
| `app/prompts/agents/` | 当前角色提示词及合成示例 |
| `app/templates/`、`app/static/` | 页面模板与前端资源 |

历史实验、验收流水、旧方案和演示产物已退出当前文档目录，需要追溯时查 Git 历史，不沿用历史测试成绩作为当前验证。

## 当前测试入口

当前保留的离线专项测试：

```powershell
python -m pytest -q tests/automated/test_main_input_projection.py tests/automated/test_main_observations.py
```

覆盖 main 输入投影及工具观察合同，不是完整 Web、地图或路线质量回归。`python -m pytest -q` 只运行当前实际存在且符合 `pytest.ini` 发现规则的测试。旧浏览器脚本、数据集及批量 runner 已不在当前工作区，不再列为可用命令。

文档及代码变更可检查：

```powershell
git diff --check
```

## 数据与安全边界

- 登录账号的旅行、消息、想法、需求、当前方案和任务状态保存到 SQLite；备份由使用者管理。
- 标准异常日志避免输出凭证；Agent 运行记录可能包含用户输入、模型输出、工具过程和用量，保存在业务库 `agent_runs`，应视为敏感数据，保留周期尚待完善。
- 模型输入会发送到所配置的模型服务；地点、天气和路线查询会访问相应外部服务。不要把本地运行理解为全部数据离线。
- 地点匹配不证明营业、预约、遮雨或门票；天气受预报可查询范围限制。日期未定时不默认今天或查询当天预报。
- 地图或模型不可用时保留输入和当前方案，明确失败或待核实；不能依靠模型补造事实来绕过服务失败。
