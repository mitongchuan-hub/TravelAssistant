# TravelAssistant

TravelAssistant 是一个以景点路线为主线的私人旅行规划 Agent。它把旅行拆成“游览计划、旅行准备、每日行程”三部分：先确定想去哪里和怎么游览，再补充衣物、住宿、餐食和交通安排。

项目使用 FastAPI 提供本地 Web 应用，使用 SQLite 保存旅行、对话、想法、行程版本和登录会话。

## 功能

- **旅行想法**：用想法卡片记录地点、预算、节奏和禁忌。
- **Agent 聊天**：通过对话提取旅行约束，模型不可用时保留消息并明确提示失败。
- **生成第一版行程**：以景点和路线为主线，生成按天排列的游览计划。
- **旅行准备**：单独展示衣物、天气装备和住宿建议。
- **每日时间线**：把游览、用餐、交通、住宿和休息放在同一条时间线上。
- **具体饮食推荐**：餐食项目包含推荐菜品或当地食物、菜系、区域、预算和预约状态。
- **天气查询**：使用 Open-Meteo 查询温度、降雨和天气状况，并生成穿衣、遮阳、补水和雨具建议。
- **美食门店查询**：可选接入高德地图 POI，查询真实门店、评分、人均消费、地址和营业时间。
- **版本管理**：确认行程后保留版本，后续修改可以生成新草案或恢复旧版本。
- **模型容错**：支持主模型和限流时的备用模型；生成失败时保留当前行程与版本，不用本地模板冒充成功。

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

生成第一版行程时，Agent 分为两个阶段：

```text
基础提示词
    ↓
信息收集 Skill
    ↓
天气和美食等工具
    ↓
最终生成 Skill + JSON Schema
    ↓
后端校验
    ↓
保存行程版本
```

信息收集阶段负责识别目的地、日期、预算、人数、必去地点、饮食禁忌、住宿限制、交通偏好和穿衣需求。没有提供的偏好保持为空或待确认，不会被当成用户已经明确选择。

最终生成阶段以景点路线为主线，再补齐衣食住行：

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
app/prompts/base_system.txt
app/prompts/skills/gather_trip_information.txt
app/prompts/skills/finalize_trip_plan.txt
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

工具调用有轮数限制。天气或美食服务不可用时，工具返回待确认结果，行程仍然可以继续生成。

## 项目结构

```text
app/
├── agent_client.py          Agent 调用、工具循环和模型降级
├── auth.py                  登录、注册和会话
├── main.py                  FastAPI 路由
├── models.py                旅行、行程和准备事项模型
├── persistence.py           SQLite 快照和关系表持久化
├── store.py                 旅行状态和业务逻辑
├── response_schema.py       最终旅行结果 JSON Schema
├── prompts/
│   ├── base_system.txt      基础系统提示词
│   └── skills/              阶段性 Skill
├── tools/
│   ├── registry.py          工具注册和分发
│   ├── weather.py           Open-Meteo 天气工具
│   └── food.py              高德地图美食工具
├── templates/               Jinja2 页面模板
└── static/                  CSS 和前端脚本

test_app.py                 Web、认证和页面测试
test_regressions.py         Agent、持久化和工具回归测试
tests/chat_browser.cjs      浏览器端聊天回归测试
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
```

## 数据和安全

- API Key 只从环境变量读取，不写入日志。
- 日志只记录异常类型和状态码，不记录对话内容、模型完整响应或密钥。
- 用户消息在模型失败时仍会保存。
- 无论行程是否已确认，模型失败均不会覆盖当前行程或创建新版本。
- 普通聊天通过模型的 `next_action` 区分继续聊天、询问确认和用户已同意生成；仅出现“行程”等词不会直接触发规划。
- 单项修改依据原条目 ID 定位，后端只应用该条目的变化；目标不匹配或行程已被其他操作更新时保留现状。
- 当前想法墙全部卡片和完整现有行程都会提供给模型；解析不会按固定天数或每日条目数截断。
- `.cache/` 下的 SQLite 数据和运行日志属于本地运行数据，不应提交到远程仓库。

## 当前边界

- 天气预报受 Open-Meteo 可查询范围限制，较远日期只能显示待确认。
- 美食门店信息依赖高德地图 Key 和接口可用性。
- 具体餐厅是否营业、是否排队或是否需要预约，生成后仍建议出发前再次确认。
- 模型服务的响应速度和限流策略由上游服务决定；本项目提供限流时的备用模型，但不能消除上游限流。
