# 2026-11-04 真实应用隔离验收

## 结论

**链路可运行，整体验收不通过。** 注册登录、真实住宿候选确认、唯一需求正文增删、FIFO 最新状态读取、真实在途停止及显式恢复、真实 HTML 生成/浏览器预览/打开/下载均实际执行。无偏好完整规划被授权校验误拒；另一次新建的极简规划虽成功保存并生成网页，正文只有 **69 个字符且以“符合”结束**，遗漏饮食禁忌和条件说明，不具备可交付的一日攻略质量。不能把 API succeeded / HTML ready 当作内容验收通过。

本报告所有账号、对话、截图及附件均来自本次新建的合成测试数据，没有使用既有业务数据。

## 基线、环境和隔离

- 分支 `v3.0.0`，HEAD `294f4f10e2021d1ab2cb24851ed6acc73bb3a8a9`（follow-up 已在此前提交中）。
- 开始时工作树已有 15 个修改文件、2 个未跟踪文件，为当前 REST 详情实现及配套改动；测试的是 **HEAD + 当时 dirty REST 实现**，不是纯提交版本。包括 config/main、地图 MCP/places/新增 amap_rest、工具注册/Schema、提示词、README/配置示例及测试。本次不修改产品代码、模型参数、真实 `.env`，不 commit/push。
- 固定解释器 `E:/anaconda/envs/TravelAssistant/python.exe`；当前前端 `npm run build`（包含 vue-tsc）成功，然后用当前 FastAPI 和刚构建的 dist 同源运行于 `127.0.0.1:8767`。启动前 netstat 确认该端口空闲。没有使用 tests/browser 的 ScriptedModel、fake 地图或 seed_demo。
- 使用独立临时目录及全新 SQLite；核实所有业务存储均使用 CITY_DB_PATH，另覆盖 TRAVEL_DB_PATH 及环境中任何 DB_PATH/DATABASE 项；导入 app 前已完成隔离，断言空 Store。仅从本次临时库只读提取 trace。
- **发现配置文档不一致**：`backend/app/core/config.py:33` 实际 `load_dotenv(..., override=True)`，README 声称 override=False。因此启动器定向内存读取 `.env` 后覆盖数据库路径，再标记环境已加载，阻止 app 导入时重新覆盖隔离值。未改产品配置；这是一项验收启动保护，不是 fake 关键流程。
- 非敏感模型配置：`qwen3.8:27b`、`chat_completions`、主请求超时 240 秒、备用模型空、主 thinking 开关未配置（服务端默认）；展示开启、展示超时 180 秒；整轮产品预算 360 秒。高德 MCP 20 秒，REST 未显式配置（产品默认 10 秒）。持久化开启，localhost cookie secure=0。未保留密钥或供应商凭证 URL。
- 浏览器 Chrome / Playwright，430×932 与 320×932。
- 应用交互时间：北京时间 **14:08:16–14:15:12**（UTC 06:08:16–06:15:12），约 6 分 55 秒；证据审阅、报告与清理随后完成。初次驱动在 A 失败后仍等待 B，人工停止该驱动并重新登录继续；这是验收脚本的失败分支处理调整，未重跑 A 或改变产品。

## 执行结果矩阵

| 场景 | 实际结果 | 判定 |
|---|---|---|
| 注册、注销再登录 | 新合成账号浏览器注册，API 注销/登录成功 | 通过 |
| 四字段创建、候选选择 | 浏览器填写北京饭店/公交/2026-11-05/1天，真实地图返回多候选，radio 选择“北京国际饭店”并确认；第二趟真实查询选定“北京饭店” | 通过 |
| REST 详情 | 住宿确认实际使用当前 REST 接缝；Agent 查询故宫、景山、王府井详情的 metadata 明确 amap_rest/v3/place_detail，无 fake | 通过；不代表营业事实完全核实 |
| 记录香山、饮食、步行偏好 | trip-101 正文由空变为“想去香山；不吃花生；尽量少走路。”，revision 1→2 | 通过 |
| 删除香山保留禁忌 | 正文变为“不吃花生；尽量少走路。”，revision 2→3，固定资料未变 | 通过 |
| 无偏好默认规划 | trip-103 初始需求空，A 真实查询天气、故宫/景山、步行和首末公交，生成了含默认说明的候选文字，但 submit_changes 两次遭误拒，正文未保存 | **失败 P1** |
| 运行中 B/C FIFO、A 不替代 | A 保持相同 task_id running，B/C waiting 顺序正确；A 失败后队列暂停；显式 resume 后 B 成功，再 C 成功，C 激活输入为 revision2“ 不吃花生 ”，最终revision3含两项 | 通过（失败暂停+恢复分支）；未验证 A 成功后自动接 B 的分支 |
| 真实在途手动 stop | UI progress 的 model running 已观察到；取消后 paused=true/cancelled，waiting 保留，draining=true；等模型真实返回才 draining=false | 通过；停止通过公开 API 执行，不声称点击停止按钮路径已测 |
| 显式 resume、不重跑失败/取消项 | resume 后仅 waiting B 执行；原 A 仍 failed，stop-A 仍 cancelled，最终 queue 空、不暂停、不 draining | 通过 |
| 新极简路线保存 | 新独立 request/task 真实查王府井及往返步行并成功保存 plan-105，需求两项仍在 | 事务链路通过，**内容失败 P1** |
| HTML 生成 | 真 display Agent 16.7秒生成，ready，绑定 plan-105/正文哈希 | 链路通过，内容质量不通过 |
| 浏览器预览/打开/下载 | 实际 iframe、另开浏览器页、API download200（3536字节）及点击下载链接的 browser download event，failure=null | 通过 |
| 320/430 响应式 | 宿主与 iframe 横向溢出均0；body15px，标题层级可辨；已实际看4张聊天/攻略截图 | 布局通过；内容保留失败，见下 |

### 需求/队列证据说明

C 未额外调用 read_context 并不意味着没读最新状态：其首个模型输入 requirements.known.text 为“不吃花生”、revision=2，说明激活时读取了 B 最新提交；本轮输入已带完整正文。A 的首个输入没有等待中的 B/C，因此没有提前注入。B→C 是成功后自动串行推进；A→B 是失败后显式恢复，不混淆两个路径。

## 真实调用与保存统计

- **8 个聊天任务：6 succeeded、1 failed、1 cancelled。** 另有 1 次展示运行，共 **9 条 agent_runs**。
- **22 次真实模型请求**（21主 Agent、1展示），均记录到模型请求结束；比约10–20的目标略高，原因是首轮规划10次后仍进行一次窄范围新规划5次以覆盖HTML。无无限重试、无追加请求。
- **19 次实际工具执行**：7次 submit_changes（2次被拒）、3次地点搜索、3次 REST 详情、2次步行、2次公交、2次天气。完整每请求/每工具耗时在 `acceptance-2026-11-04/trace-summary.json`。
- 模型请求耗时合计 **202.415秒**；工具耗时合计 **9.493秒**；agent_runs累计时长217.318秒（不是墙钟时间，含排队/人工审阅间隔的总场景墙钟另计）。
- trace 聚合 token：input 211132、output 17968；这是服务端 trace usage 字段，不是账单结算。
- **空或非法 JSON 参数 0，单模型响应返回多个工具调用 0**；没有因这两类问题失败。
- 注意：agent_runs.ok=1 表示 trace 没抛外层异常，不等于任务成功。本次 A 的 trace ok=1，但 Task Execution结果.failed=true，公开任务明确 failed；stop-A 也必须按任务 cancelled 判断。

| 请求 | task_id | 状态 | 模型/工具数 | 本轮耗时 |
|---|---|---|---:|---:|
| live-requirements-1793772499369 | 5eb00c75fa7b4ecf8a6d354fe7ebbb09 | succeeded | 1/1 | 4.572s |
| live-delete-xiangshan-1793772504268 | 878eb21dcbb04df3a925fbe1667408e1 | succeeded | 1/1 | 2.397s |
| live-plan-A-1793772507591 | 35f3a339a6284509a3e34535b5566caf | failed | 10/10 | 148.686s |
| live-followup-B-1793772507617 | ae04daa9fc814a069c9594e657161ebd | succeeded | 1/1 | 6.754s |
| live-followup-C-1793772507670 | 3d67637129674c9bafd5110507252933 | succeeded | 1/1 | 3.599s |
| live-stop-A-1793772858453 | bd71f52a30dd4c549dfcfa0554285912 | cancelled | 1/0 | 2.317s |
| live-resume-B-1793772858604 | 826bf9a68c6c4b1bb4c616a2739906a0 | succeeded | 1/0 | 2.938s |
| live-simple-plan-1793772863801 | 380ff8d4d5454cf295cb3ff741e1c816 | succeeded | 5/5 | 29.364s |
| display 3ac03ab3a4b1 | plan-105 派生展示 | ready | 1/0 | 16.691s |

request/task/message/thinking ID 完整关联见 `api-evidence.json`；每次 model_request_id、每模型毫秒耗时、工具参数与秒耗时见 `trace-summary.json`。

## 缺陷与复现

### P1：提及“默认假设”的明确生成请求被误判无授权

- 请求：“请直接生成这一天的同城游玩路线，我没有其他偏好。……说明未提供信息采用的默认假设，但不要把默认假设写入我的需求。”
- task `35f3a339a6284509a3e34535b5566caf`，10次模型/148.7秒真实外部查询后，submit_changes 第一次返回 `invalid_arguments`：“行程生成需要本轮明确的肯定指令，不能凭原话引用推断授权”。模型修正正文后重交仍被拒，任务failed，整包未保存且队列暂停。
- 代码原因：`backend/app/services/agent_crud.py:43–52` 的 `_positive()` 在整条消息任意出现“假设”即拒绝，不区分假设性授权与要求说明默认规划假设。输入开头有明确“请直接生成”，仍误伤。
- 证据：`validation-rejection.json`，trace 两次 submit_changes ValueError，`api-evidence.json` 失败与暂停状态。
- 没有修产品，也没有把下一次新请求称为此场景通过。

### P1：语义未完的 69 字正文仍被保存并制作成“就绪”攻略，禁忌丢失

- 新请求 `live-simple-plan-1793772863801` 明确含“不吃花生、尽量少走路、必要条件和估算说明”，要求一天早餐/午餐/休息/晚餐及返回。
- 保存的完整正文仅69字符：

> 极简一日，仅逛王府井步行街，从北京饭店步行约900米即达，无需乘车。午餐后回房休息，晚餐在住宿解决，不再安排其他景点。全天步行量很小，符合

- `submit_changes.itinerary.text` 本身就以“符合”结束；API/数据库保存与聊天回复一致，不是浏览器显示截断。需求正文仍有“不吃花生\n尽量少走路”，但行程没写饮食禁忌、用餐注意事项、必要条件、估算可靠性，也没有清晰时间安排。不能因为“极简”合理化未完句和安全约束遗漏。
- **finish_reason 限制**：当前 trace 不保存该字段具体值，无法声称确切为 stop 或 tool_calls。当前传输层只接收完整结束原因并拒绝 length/过滤；本轮5次模型均通过完整性门槛、参数JSON完整、无 IncompleteModelOutput。因此已知是“完整性门槛接受的响应中正文语义未完”，**没有证据归因为 token 截断或供应商网络截断**。
- 展示 Agent 把这69字变成正常外观网页，还新增“仅包含……约900米单程步行，节奏轻松”，没有把未完句作为异常呈现。网页没保留饮食禁忌；原文已丢失的条件不会被自动补救。网页显示“回房休息”，但步行量只描述单程900米，返程总量不清晰。
- 证据：`chat-430.png` 能看到尾部“符合”；`generated-guide.html` 和两张guide截图显示网页内容；trace终止工具与API正文一致。

### P2：README 环境优先级与当前实现相反，影响隔离启动安全

- README:118 宣称已有进程环境优先 override=False；实际 config.py:33 override=True。
- 简单按文档仅设置临时CITY_DB_PATH再import app会被真实.env覆盖，可能触及既有业务库。本次已在导入前保护，**没有发生真实业务库访问/迁移**。
- 应统一环境优先级契约并增加启动隔离验收；此处只报告，不修改。

### 待核实的候选路线内容，不算已保存成功方案

首个未保存候选有餐次/休息/预约和默认说明，但也出现“当日正常开放”确定语气，以及地铁换乘/方向等需对照事实的描述；交通查询成功不代表逐句正确。该候选已被拒，本次没有对其做官方票务、全量线路现实可执行性复核，不能对这些陈述背书。

## HTML 和视觉实看

- [320px 攻略](acceptance-2026-11-04/guide-320.png)、[430px 攻略](acceptance-2026-11-04/guide-430.png)：均已打开实际图像审阅。单列雾蓝、深色标题、正文与分段层级可读，标题/路线卡/时间线均不横向裁切；正文实测15px。宿主下载/返回与打开网页入口可见。
- [320px 聊天](acceptance-2026-11-04/chat-320.png)、[430px 聊天](acceptance-2026-11-04/chat-430.png)：布局无横溢出；窄屏长消息会在独立聊天滚动容器中滚动，输入区固定。430图清楚暴露已保存回复的未完句，不是截图尾部遗漏。
- [真实住宿候选](acceptance-2026-11-04/lodging-candidates-430.png)：多候选、地址、单选与确认入口可读。搜索词“北京饭店”第一次返回“北京国际饭店”等候选，脚本实际选择第一项；第二趟返回并选定真正“北京饭店”，不把两者混称。
- iframe sandbox 实测 `allow-popups allow-popups-to-escape-sandbox`，没有 allow-scripts/allow-same-origin；打开独立页标题正确；浏览器下载 failure=null，下载版本与预览绑定相同。
- 视觉/响应式链路可用不等于内容合格，本轮不能给整体UI/无障碍完整认证；未覆盖全部键盘导航、对比度、暗色模式或性能评分。

## 与离线测试的区别和限制

- 本次没有把离线单测计作验收通过：模型、天气、高德MCP、REST真实联网；实际当前FastAPI、SQLite事务、构建前端、Chrome API/页面交互完整运行。没有重放fixtures或内存调用代替应用交互。
- 未修改产品来绕开失败；仅调整验收脚本在A失败后的恢复逻辑、修正读取进度字段（进度在消息execution_progress，不在task.progress）。没有靠调低超时、禁思考、替换模型或fake关键步骤通过。
- 停止及恢复通过公开API，不声称其按钮点击路径通过；新页打开通过真实浏览器导航已测，但不是点击“打开网页”链接事件本身。下载按钮点击已测。
- A成功后自动接B未覆盖；真实覆盖的是A失败暂停、显式恢复及B成功后自动C。没有覆盖重启恢复、50条队列上限、跨账号权限、旧业务库迁移或实际出行日开放/预约。
- 一次真实样本可证明这次行为和具体缺陷，不证明概率稳定性；未再消耗模型调用做统计重复。正文完整度应在修复后针对同样用户语句重新跑真实验收。

## 清理与交付

- 本次后台浏览器驱动已结束；仅停止本次8767服务。socket检查返回10061（连接被拒绝），证明8767不再监听。
- 临时脚本、SQLite（含会话）、原始日志、中间JSON均已删除；未动任何既有8000/5173服务或业务库。
- 保留本报告、5张脱敏合成截图、生成HTML、API/模型工具摘要/激活快照/拒绝证据。未保留认证密码、cookie、choice_token、供应商密钥或完整凭证URL。
- 不新增产品文件变更、不安装依赖、不commit/push。原dirty REST改动保持，新增仅 `qa/` 验收交付物。
