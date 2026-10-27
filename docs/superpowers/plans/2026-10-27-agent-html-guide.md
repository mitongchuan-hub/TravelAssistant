# Agent 旅行攻略网页 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将固定行程界面替换为网页 Agent 生成的旅行攻略，并支持聊天预览、修改风格和下载。

**Architecture:** 沿用正文为业务依据的规划协议和独立展示任务队列。网页产物使用 `display` 字段的 2.0 合同，纯展示调整通过 `presentation` 候选原子提交，前端使用隔离 iframe。

**Tech Stack:** Python、FastAPI、Jinja2、SQLite、原生 JavaScript/CSS、现有 OpenAI 兼容模型客户端。

## Global Constraints

- 工作分支：`V2.0.0`；保留已有未跟踪文档。
- Python 固定使用 `E:/anaconda/envs/TravelAssistant/python.exe`。
- 正文、日期、需求快照与查询证据继续作为业务依据；网页不反向写业务事实。
- 不向生成内容开放脚本或同源权限；不新增依赖。
- 纯风格修改保持正文、证据、计划 ID 和确认状态不变。
- 每次发布绑定计划 ID、正文哈希、需求快照、展示偏好及任务 ID。

## Task 1：HTML 合同与网页生成 Agent

**Files:** 新建 `app/services/guide_html.py`、`tests/automated/test_guide_html.py`；修改 `app/services/plan_display.py`、`app/agents/agent_runtime.py`、`app/agents/agent_client.py`、`app/prompts/agents/display.txt`。

**Interfaces:** `build_guide_display(raw: str, dates: list[str] | None) -> dict` 接收模型原始输出，返回含 `schema_version`、`html`、`title` 和 `dates` 的字典；`validate_guide_display(data: dict, dates=None) -> dict` 验证保存和发布合同；`guide_document(data: dict) -> str` 为预览与下载插入 CSP。

- [x] 先写失败用例：完整 HTML 可保存，危险标签/URL/事件及截断文档被拒绝，下载文档具备 CSP。

```python
def test_html_output_is_supported():
    data = build_guide_display('<!doctype html><html><head><title>北京攻略</title></head><body><h1>北京攻略</h1></body></html>', ['2026-10-28'])
    assert data['schema_version'] == '2.0'
    assert data['title'] == '北京攻略'
```

- [x] 运行：`& E:/anaconda/envs/TravelAssistant/python.exe -m pytest tests/automated/test_guide_html.py -q`，确认失败来自缺少新合同。
- [x] 使用 `HTMLParser` 校验文档完整性与允许的标签和属性；限制 UTF-8 体积 512 KiB；拒绝主动内容，输出单个文档。兼容旧展示数据的校验入口，仅新生成结果使用 2.0。
- [x] 将 `convert_display` 改为调用网页 prompt，输入 `source_text/source_dates/style_instruction`，HTML 无结构化输出格式参数。失败后纠错一次，超过预算保留原文。
- [x] 运行 Task 1 用例与既有 `test_plan_context.py`，确认新旧合同均可处理。

## Task 2：展示偏好、候选提交与后台版本

**Files:** 修改 `app/models.py`、`app/persistence.py`、`app/agents/agent_protocol.py`、`app/agents/agent_roles.py`、`app/agents/task_runtime.py`、`app/workflow/turn_workspace.py`、`app/workflow/turn_commit.py`、`app/workflow/display_tasks.py`、`app/store.py`、`app/prompts/agents/main.txt`；新建 `tests/automated/test_guide_workflow.py`。

**Interfaces:** `TripPlan.display_style: str = ''` 持久化展示偏好。工具 `restyle_itinerary(plan_ref: str, instruction: str)` 仅为当前有正文的行程注册 `presentation` 候选，payload 为 `plan_ref/style_instruction`；Commit 后才安排展示任务。

- [x] 先写失败用例：显式采用风格候选后计划 ID、正文、证据和确认状态保持不变，展示任务包含新偏好；未采用无效果。
- [x] 覆盖旧风格结果迟到、正文已变、同一正文多次风格请求，以及生成失败时保留旧网页。
- [x] 扩展 Artifact 合同和能力白名单；同轮新内容与旧计划展示请求同时采用时拒绝旧展示候选，防止静默套用。

```python
# Commit 阶段，确认 payload 指向当前计划后：
stage.retry_display(trip_id, plan.id, source_hash, style_instruction=payload['style_instruction'])
```

- [x] 新字段恢复时默认空字符串；排除在 `plan_basis` 的业务内容指纹外。新正文继承偏好。队列记录偏好并校验当前值，保留旧有效产物用于失败恢复。
- [x] 主 prompt 区分“换网页风格”与“改地点/时间”，并使用 `restyle_itinerary` 候选及 `adopt_refs`。
- [x] 运行：`& E:/anaconda/envs/TravelAssistant/python.exe -m pytest tests/automated/test_guide_workflow.py tests/automated/test_plan_confirmation.py tests/automated/test_idea_coverage.py -q`。

## Task 3：聊天攻略预览、鉴权与下载

**Files:** 修改 `app/main.py`、`app/templates/workspace.html`、`app/templates/partials/display_panel.html`；新建 `app/static/guide.js`、`app/static/guide.css`、`tests/automated/test_guide_routes.py`、`tests/automated/test_guide_polling.py`；从 V2 模板移除固定卡片入口，兼容旧 `tab=itinerary` 链接。

**Interfaces:** `GET /workspace/{trip_id}/guide?plan_id=...&source_hash=...&version=...&download=1` 在鉴权和版本检查后返回隔离 HTML。`download=1` 使用附件响应，其他情况使用 CSP sandbox 响应。

- [x] 先写失败用例：所属用户预览/下载成功，未登录和非所有者被拒绝，旧计划/正文/展示版本返回 409，恶意 HTML 无法通过响应入口。
- [x] 聊天页加入攻略区域与入口按钮、原文后备、确认表单和状态；旧行程 tab 渲染聊天并打开攻略。已有业务链接保持兼容。
- [x] iframe `sandbox="allow-popups allow-popups-to-escape-sandbox"`，禁止脚本与同源；宿主提供下载、打开、关闭和重试。生成 HTML 永不插入宿主 `innerHTML`。
- [x] JavaScript 轮询沿用 plan-state，计划 ID 变化时更新版本绑定，重新请求受鉴权的片段；有风格重生成任务时也更新状态。隐藏页暂停轮询，重新可见后恢复。
- [x] 桌面聊天与攻略并排；手机攻略作为可关闭的独立面板，键盘焦点回到打开按钮；避免双重横向滚动。
- [x] 运行全套：`$env:TRAVEL_PERSISTENCE='0'; & E:/anaconda/envs/TravelAssistant/python.exe -m pytest tests/automated -q`，并运行 `git diff --check`。
- [x] 对桌面和手机做一轮浏览器检查；检查预览、下载、旧链接和关闭后的聊天恢复。记录真实模型验证是否完成及局限。

## 完成记录

2026-10-27 在 `V2.0.0` 完成实现，保留用户已有未跟踪文档。README、PRODUCT、DESIGN 与环境配置示例已同步更新。

- 自动化：`TRAVEL_PERSISTENCE=0`，指定 conda 解释器运行 `pytest tests/automated -q -p no:cacheprovider`，194 项通过。覆盖 HTML 结构和主动内容检查、预览鉴权与版本、风格候选采用、确认状态保留、旧任务拒绝、失败恢复与旧行程读取。
- 前端回归：Node 执行实际 `guide.js`，验证状态请求仍为忙碌但片段已空闲时重试按钮正常恢复；`node --check app/static/guide.js` 通过。
- 浏览器：独立 Chrome 与合成数据服务验证 1440×1000 桌面、390×844 手机。预览、下载、旧行程链接、两列底栏、手机焦点恢复正常，页面与攻略无横向溢出，没有 JavaScript 运行异常。确认行程后保持同一 iframe、文档上下文和 240 px 滚动位置。
- 真实模型：一条合成行程通过当前配置的模型生成成功，耗时 47.7 秒，HTML 为 7366 UTF-8 字节。浏览器检查日期、待核实项、未提供预算及示例说明仍可读。此项验证网页生成链路，未覆盖完整真实规划，也不构成通用语义或现实可执行性评测。
- 审查：独立代码审查发现的 HTML 头部顺序、旧来源绑定及按钮状态竞态已修复；回归通过。`git diff --check` 通过。

本地合成示例、浏览器报告及截图保留在被 Git 忽略的 `.cache/v2-guide-check/`，不提交模型凭证、正式用户数据或运行数据库。
