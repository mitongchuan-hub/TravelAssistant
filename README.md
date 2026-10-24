# TravelAssistant · 同城游玩

以已确认的住宿为起终点，生成、阅读和调整同城游玩路线。使用 FastAPI、Jinja2、原生 JavaScript/CSS 和 SQLite，当前按单进程运行。

## 使用方式

1. 注册或登录，填写住宿地址或酒店名称，选择实际地图候选。
2. 必填出行开始日期和游玩天数，自动计算结束日期；每日出门/返回时间、人数、预算与偏好可选，未知信息不补成事实。
3. 在聊天中说“生成路线”，或使用聊天菜单、想法页、行程页的生成入口。
4. 先阅读完整文字，后台再整理按天卡片；卡片失败可单独重试，不丢失正文。
5. 通过聊天或卡片“调整此项”修改当前方案。设置变化不会自动重排行程。

默认聊天，底栏保持“想法／聊天／行程”；功能优化不重新排列既有页面。每日从同一住宿出发并返回，可信地点可打开高德导航，未知地点保持待核实。查询耗时只是查询时估算，不保证未来开放、班次、票价或现实可执行性。

项目只提供从住宿出发并返回的同城游玩。历史记录的需求、聊天和行程可查看；继续生成或调整前，须在游玩设置中确认住宿并保存。每趟行程只保留一份当前方案，没有方案历史恢复入口。

## 代码目录

`app/main.py` 是网站入口。`app/agents/` 管理模型调用与 Agent 执行，`app/workflow/` 管理任务状态与提交，`app/services/` 处理需求和行程业务，`app/views/` 生成展示数据。模型、存储、持久化及认证保留在 `app` 根目录；地图工具、提示词、模板与静态资源分别位于 `tools/`、`prompts/`、`templates/`、`static/`。

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
- 其他配置项见 `.env.example`；实际参数处理以 `app/agents/agent_roles.py`、`app/agents/model_transport.py` 和 `app/tools/amap_mcp.py` 为准。

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

聊天中的执行过程按真实模型与工具事件更新，展示需求整理、天气／地点／交通查询等步骤及耗时。执行时展开，结束后可再次展开回看；失败、暂无结果和被新消息替代分别标注。过程随助手消息持久保存，旧消息没有记录时保持原展示。

主 Agent 读取当前已保存行程的完整正文和活动概要；想法墙默认摘要只携带想法内容，整理状态不代表行程采用状态。已有行程与想法墙、且本轮没有产生候选时，只读答复会在提交前由同一模型额外核对一次，依据当前正文修正安排状态和重复加入的建议。复核使用本轮调用预算，失败时不发布未经核对的草稿；该场景会增加一次模型调用。

想法墙也是旅行需求输入。完整生成时，规划能力须逐项返回想法处理结果：已安排、存在冲突、需补充信息或不适用，并说明依据；已安排项须摘录新行程中的对应文字。程序检查全部卡片引用是否覆盖，遗漏时不接受成功候选；摘要截断时按需分页读取原卡。主 Agent 结合处理结果维护本轮 todo，关键取舍需要追问。卡片不是已核实事实，也不扩大局部修改、预订或支付授权。

这些保护防止旧结果覆盖新状态，并减少回复与当前行程不一致的问题，不保证生成内容完全正确。只读答复复核不验收路线事实、预算或现实可执行性，保存成功或卡片就绪也不代表这些内容已经核实。

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

主 Agent 能力测试集及字段、评审说明见 [main_agent_v1](tests/datasets/main_agent_v1/README.md)。使用项目指定的 conda 解释器，在项目根目录执行：

```bash
# 离线验证数据和评测基础设施，不调用模型
E:/anaconda/envs/TravelAssistant/python.exe -m tests.main_agent_eval.runner --validate-only
E:/anaconda/envs/TravelAssistant/python.exe -m pytest tests/automated -q

# 显式调用真实主模型：输入会发送到配置的模型服务，可能产生费用
E:/anaconda/envs/TravelAssistant/python.exe -m tests.main_agent_eval.runner --live-main --profile smoke --repeat 1
# 完整集或按标签筛选
E:/anaconda/envs/TravelAssistant/python.exe -m tests.main_agent_eval.runner --live-main --profile full
E:/anaconda/envs/TravelAssistant/python.exe -m tests.main_agent_eval.runner --live-main --profile full --tag weather
```

`--live-main` 保留生产主循环、提示词及工具校验，专业能力和外部事实使用固定资料，用于隔离回归。

全真实协作评测见 [main_agent_system_v1](tests/datasets/main_agent_system_v1/README.md)：

```bash
# 只验证全真实模式的数据，不发请求
E:/anaconda/envs/TravelAssistant/python.exe -m tests.main_agent_eval.runner --validate-only --dataset tests/datasets/main_agent_system_v1/cases.json
# 主 Agent、实际调用的子 Agent、天气/地点/路线查询全部真实，可能产生费用
E:/anaconda/envs/TravelAssistant/python.exe -m tests.main_agent_eval.runner --live-system --profile smoke --repeat 1
```

`--live-system` 不预设子 Agent 返回内容或调用顺序，使用运行日相对日期和真实查询证据判断结果；缺服务或网络异常单独报告，不把天气未知当作条件不满足。正常协作不设置 token 总额硬上限，但保留循环、调用次数和超时保护。

两种模式均不接正式数据库。报告写入 `.cache/main_agent_eval/` 的独立运行目录；机器检查、服务状态与待人工语义评审分开，离线测试通过不代表模型能力通过。结果验收到候选采用，不证明数据库提交或 Web 展示成功，多轮用例测的是给定合法历史状态下的理解和衔接。

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
