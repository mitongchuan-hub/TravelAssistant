# 测试目录

所有测试和评测入口统一放在 `tests/`。从项目根目录执行命令，使用 `E:\anaconda\envs\TravelAssistant\python.exe`。根目录 `pytest.ini` 指定自动测试发现范围；真实模型评测、测速和浏览器脚本不会被普通 pytest 意外运行。

```text
tests/
├── automated/                   自动回归测试，按功能拆文件
├── browser/                     Playwright 脚本及隔离应用/页面夹具
├── performance/                 场景测速、计时与报告工具
│   ├── run_agent_latency.py     26场景的逐阶段耗时与交互报告
│   └── measure_workflow_latency.py  原有固定工作流测速
├── datasets/
│   ├── travel_agent_v1/         历史数据与评审协议
│   └── travel_agent_v2/         当前70条测试集与人工评审模板
├── run_travel_dataset.py        单条真实模型案例
├── run_travel_dataset_batch.py  批量案例与受控系统入口
└── travel_dataset_support.py    夹具、受控资料与数据校验
```

## 自动回归

`automated/` 按功能划分的测试文件仍覆盖有效代码：角色权限、Loop、模型协议与错误恢复、需求校验、页面认证、任务并发、持久化迁移、正文/展示绑定、工具及测试集执行器。旧格式行程的地图、路线、时间线和旧库迁移仍在应用中使用，相关测试继续保留。

```powershell
& E:\anaconda\envs\TravelAssistant\python.exe -m pytest -q
& E:\anaconda\envs\TravelAssistant\python.exe -m pytest tests/automated/test_workflow.py -q
```

目录整理前后收集到的416条用例完全对应，未删减自动覆盖。测试模块之间的现有夹具导入保留在同一目录，数据集的 `pytest_nodes` 同步指向新的文件路径。

## 业务评测与展示专项

详细标准和输出结构见[v2测试集](datasets/travel_agent_v2/README.md)。模型回复默认待人工评审；受控系统回归单独统计。

```powershell
& E:\anaconda\envs\TravelAssistant\python.exe tests/run_travel_dataset_batch.py --profile current --list
& E:\anaconda\envs\TravelAssistant\python.exe tests/run_travel_dataset_batch.py --profile smoke --output "$env:TEMP\travel-smoke-$(Get-Date -Format yyyyMMdd-HHmmss)"
& E:\anaconda\envs\TravelAssistant\python.exe tests/run_travel_dataset_batch.py --profile system --output "$env:TEMP\travel-system-$(Get-Date -Format yyyyMMdd-HHmmss)"
& E:\anaconda\envs\TravelAssistant\python.exe tests/run_travel_dataset_batch.py --profile display --repeats 3 --output "$env:TEMP\travel-display-$(Get-Date -Format yyyyMMdd-HHmmss)"
```

v1保留为历史协议和复测输入，不再扩充；执行时显式传 `--dataset tests/datasets/travel_agent_v1/cases.json`。

## 真实服务测速

高德MCP独立只读验收（协议发现、地点/餐厅详情、路线与缓存，无LLM/业务Store）：

```powershell
& E:\anaconda\envs\TravelAssistant\python.exe tests/performance/check_amap_mcp.py --output "$env:TEMP\amap-mcp-check-$(Get-Date -Format yyyyMMdd-HHmmss)"
& E:\anaconda\envs\TravelAssistant\python.exe -m pytest tests/automated/test_amap_mcp.py -q
```

参考[实施与实际失败记录](../docs/amap-mcp-implementation.md)。原有外部调用替换为MCP适配和实际tools/call计时；受控合成模式继续离线执行，不能用来证明MCP真实性能。

新增[衣物、住宿、天气合并单次提取实验](datasets/display_semantics_v1/combined-experiment.md)，使用 `--strategies combined`，最多1次实际模型请求，沿用原文清单并新增天气事实核对；生产协议保持独立。

新增[独立全局提取对照实验](datasets/display_semantics_v1/split-experiment.md)，同一原文串行比较整篇与两次提取，记录180秒/2请求预算、兼容消耗、逐样本输出和71项人工核对清单。实验不切换生产转换路径。

新增[固定原文语义专项](datasets/display_semantics_v1/README.md)，覆盖分类、衣物字段、近似时间和已知错误时长。运行入口为 `tests/performance/run_display_semantics.py`，连续两次模型连接/超时失败后停止，保留首次失败。离线对照使用 `tests/performance/review_display_semantics.py`；专项检查不代表全面语义通过。

2026-10-13新增边界原文 `datasets/display_boundaries_v1/cases.json` 和连续业务流程 `datasets/optimization_chain_v1/cases.json`，不修改历史v1/v2成绩。边界原文可通过run_display_semantics.py --dataset选择；评审仍区分结构、内容忠实度与完整业务链路。`performance/run_optimization_acceptance.py`串行执行固定原文、边界原文、连续流程、v2业务边界、七日真实工具规划、MCP只读冒烟和上下文配对测量。输出目录须为空，每次失败独立保留；进程退出0不代表语义通过，必须审阅样本内的检查与输出。

`performance/check_quote_transport.py`复现引号在不同模型输出协议中的保留情况；`check_display_completeness_candidate.py`是独立提取格式/思考强度实验，不改生产配置。`compare_context_delivery.py`同窗口交替比较同一分类输入的重复消息与精简消息，不能代表完整业务或浏览器延迟。生产默认YAML块文本输出，本地继续严格验证JSON Schema 1.1；普通pytest同时覆盖安全解析、旧数据读取迁移、精确编辑、组合动作与预算预检。

新增[26场景耗时测试集](datasets/agent_latency_v1/README.md)，逐次记录模型SDK请求、工具/后端调用、缓存、需求校验、保存和页面响应。支持重复采样、受控模式与离线交互报告。

```powershell
& E:\anaconda\envs\TravelAssistant\python.exe tests/performance/run_agent_latency.py --profile smoke --repeats 1 --output "$env:TEMP\agent-latency-$(Get-Date -Format yyyyMMdd-HHmmss)"
```

原有固定工作流脚本会调用当前 `.env` 中的模型与真实天气/地图工具。使用独立SQLite及新的输出目录，不向8000业务服务发送测试请求。内部文字生成完成时间不等于当前同步实现的页面可读时间。

```powershell
& E:\anaconda\envs\TravelAssistant\python.exe tests/performance/measure_workflow_latency.py --output "$env:TEMP\travel-latency-$(Get-Date -Format yyyyMMdd-HHmmss)"
```

## 规划提示词专项

[planning_prompt_v1](datasets/planning_prompt_v1/README.md)六例用真实模型与合成资料检查规划正文和动作授权，后台展示不执行。包括七日、雷雨、晚到早走、完整需求提取、撤销与过敏组合查询；结果中的机器检查与人工语义评审分开，首轮和独立补测保留。运行 `performance/check_planning_prompt.py --output <新空目录>`，可用`--cases`选择。见[改版记录](../docs/planning-prompt-redesign-20261013.md)。

[planning_meals_v1](datasets/planning_meals_v1/README.md)新增四例用餐专项：餐次覆盖、区域衔接、已支付预约、自带餐、早离站和全程餐饮额度。使用同一执行器，加`--dataset tests/datasets/planning_meals_v1/cases.json`，逐餐预算与已有安排需人工对照原文验收。

## 浏览器回归

需要本机Chrome和可供Node加载的Playwright。应用夹具使用内存Store和合成模型输出，临时端口与业务服务独立。

```powershell
npm install playwright
$env:TRAVEL_TEST_PYTHON = 'E:\anaconda\envs\TravelAssistant\python.exe'
node tests/browser/chat_browser.cjs
node tests/browser/chat_ui_browser.cjs
node tests/browser/tasks_browser.cjs
node tests/browser/requirements_browser.cjs
node tests/browser/places_browser.cjs
node tests/browser/text_itinerary_browser.cjs
node tests/browser/async_display_browser.cjs
node tests/browser/display_cards_browser.cjs

$itineraryFixtureDirectory = Join-Path $env:TEMP ('travel-itinerary-ui-' + (Get-Date -Format yyyyMMdd-HHmmss))
& E:\anaconda\envs\TravelAssistant\python.exe tests/browser/render_itinerary.py $itineraryFixtureDirectory
node tests/browser/itinerary_browser.cjs $itineraryFixtureDirectory
```

新增 `chat_ui_browser.cjs` 与内存夹具 `chat_ui_fixture.py`：四宽聊天几何、Markdown安全、长回复、草稿、输入法、等待期间输入、新回复锚点、连续消息及响应丢失后的单次恢复；支持 `TRAVEL_CHAT_ARTIFACTS` 指定截图目录。见[聊天改版验收](../docs/chat-redesign-20261013.md)。

新增 `display_cards_browser.cjs` 和内存夹具 `display_cards_fixture.py`：用固定评测的当天、三日、七日卡片与完全重复/不同限定的专门夹具，检验五种屏宽、三个行前区域同宽竖排、去重后文本保留、原卡片不变、预算键盘展开、住宿直接可见、日期导航、调整操作和天气转义，并生成截图。可通过 `TRAVEL_CARD_ARTIFACTS` 指定截图目录；验收见[卡片改版记录](../docs/itinerary-cards-ui-20261012.md)。

`place_fixture.py`、`text_itinerary_fixture.py`由对应浏览器脚本启动；`render_itinerary.py`为旧格式兼容页面生成HTML夹具，均为有效测试辅助文件。

## 已清理的旧入口

删除5个独立手工脚本：`live_agent_roles_case.py`、`live_text_itinerary_case.py`、`live_workflow_case.py`、`live_routes_case.py`、`live_reasoning_case.py`。它们没有被当前自动测试或数据集调用，重复覆盖旧验收流程，部分仍假设委派事件、旧SDK对象结构或缺失当前五组必填字段。对应覆盖由当前自动回归、v2业务/展示案例与真实服务测速承担。

相关文档已指向当前入口。历史评测报告的原始结果保留；目录调整不把旧成绩重新解释成新模型成绩。业务SQLite、运行证据和模型配置备份均不属于此次清理范围。
