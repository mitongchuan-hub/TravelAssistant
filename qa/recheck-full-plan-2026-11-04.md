# 未完行程：生产 Agent 完整循环复查（2026-11-04）

## 结论

**本次有效完整运行没有复现原69字、结尾“符合”的未完正文。不能据此宣称问题已修复。** 当前生产 Agent 自然执行5轮主模型请求、5次工具，最后 `submit_changes` 成功提交805字符正文；独立展示模型再执行1轮，HTML ready。正文包含三餐、午后回住宿休息、往返、花生提醒及少步行建议，最后一句完整。没有追加第二次有效内容复测。

同时，正文仍有可明确指出的质量问题：把2026-11-05写成周三（实际周四）、无依据称饭店两餐含房费、未独立查询返程就写成与去程相同的确定值。**正文语义完整不等于整份攻略事实可靠或可直接交付。** HTML保留这些问题，没有修正事实。

## 范围、隔离与输入差异

- 已读取原 `qa/acceptance-2026-11-04.md`、activation-snapshots、api-evidence及trace-summary中目标 `live-simple-plan-1793772863801` / trip-103 的证据。不修改旧报告、旧附件或产品文件。
- 本机日期2026-11-04，北京时间。保留原出行日2026-11-05；本次不是跨日期的历史当天重放，日期与旧测同日、出行日仍为次日。
- 固定解释器 `E:/anaconda/envs/TravelAssistant/python.exe`。真实当前 FastAPI，仅监听 `127.0.0.1:8767`，独立临时SQLite、全新合成账号，启动前断言空Store（无trips/plans）。未访问8000/5173，未对原服务发请求或停进程。
- `.env` 仅读入本次进程内存；随后覆盖 `CITY_DB_PATH`、`TRAVEL_DB_PATH` 及环境中的 DB_PATH/DATABASE 项，在 `config._environment_lock` 内锁定 `_environment_loaded=True`，防止 `load_environment(override=True)` 覆盖隔离路径。另加本次进程内 `sqlite3.connect` 防护，拒绝任何不等于临时库的连接。没有改 `.env` 或配置文件。
- 通过真实公开注册API、创建API及 `PUT /requirements-text` 重建。真实地图返回候选，确认原POI **B000A10FBB 北京饭店，116.409385,39.908798**；未直接填业务模型或伪造地图结果。搜索“北京饭店”首轮候选有时不含目标，使用相同原词再次公开查询后选中目标，无替代饭店。
- 固定资料：北京饭店、2026-11-05、1天、transit；保存的唯一需求正文恰为 `不吃花生\n尽量少走路`。有效测试创建trip-101，初始需求revision2；旧样本trip-103激活时revision3。ID/revision数字不同不是业务内容差异。
- **未复建旧样本全部前序聊天。** 本次激活conversation仅有创建欢迎消息；旧样本另有先前规划失败、B/C需求消息、取消咨询及复述偏好的消息。因此这是相同原请求和已保存业务状态的公开API复查，不是完全相同转录的精确重放。没有后续偷偷补齐history，也没有另跑独立runtime绕过commit。
- 实际首轮HTTP中的 `requirements.known.text` 已含两项禁忌/偏好，固定POI/日期/transit也均传入。模型本次没有调用 `read_context`，但不能据此说它没收到最新需求。完整投影见 `full-recheck/activation-context.json`。

### 逐字复用的用户请求

> 请生成一个极简一日路线：早餐后从北京饭店步行到王府井街区，午餐后回住宿休息，晚餐在住宿解决。不再安排其他景点。交通仍以创建资料的公交为准，近距离可说明无需乘车。不吃花生，尽量少走路。请先读取最新需求，查真实王府井地点，给必要条件和估算说明。请保存可用正文并生成攻略网页。

API回读的task.body与原附件message.text逐字相等。

## 测量方法及无效准备尝试（不隐瞒额外调用）

测试启动器仅做内存hook：拦截模型host上的最终 SDK HTTP send，原样调用真实send、读取真实response bytes后原样交还SDK；不fake响应、不替换工具、不改发送JSON。SDK解析后的关键字段另存。只捕获模型host，不记录地图/天气凭证URL或请求headers。

- 当前环境OpenAI SDK实际继承 **httpx2.Client**，并非通常的httpx.Client。第一次启动器预检因把 `database_path()` 的str当Path而停止，未导入Store或调用模型；第一次实际实例的候选不含原POI，停止于创建前，无模型。
- 随后一次原任务测量只hook了httpx.Client：模型真实返回后，验收层读取未设置的thread-local轮次，引发 `AttributeError`，任务failed且未执行工具。**这次是验收hook故障，不是正文缺陷复现。** 有1次未捕获的真实SDK模型请求；无raw/usage，不能编造其响应。证据独立保留在 `full-recheck/setup-attempt-150524/`（其中嵌套更早的纯准备证据）。
- 修正临时启动器为同时覆盖实际httpx2 send后，重新全新空库重建相同输入，只完成下述 **1个有效完整任务**。没有改变用户文本、工具选择或模型参数；不是挑选多个有效结果。
- 因而本次工作整体是 **7次真实SDK模型请求：1次无效测量 + 有效运行6次（5主+1展示）**。此前单HTTP诊断不计入本次工作，也未重复其强制tool_choice方案。
- 未发送诊断user消息、`tool_choice`、temperature、max_tokens或自定义思考开关。最终实际发送参数为 `model=qwen3.8:27b`、`reasoning_effort=low`、`store=false`；low来自原生产role，不是测试新设。`OPENAI_MAIN_ENABLE_THINKING`未配置。SDK重试0，主请求超时240秒、整轮生产预算360秒，展示180秒；没有备用切换。
- 沿用生产工具注册表和自动循环；测试外部仅设置20轮/390秒安全取消边界，本次5轮即自然终止，未触发取消或改变runtime预算。

### 原始证据的隐私与完整性边界

`http-01.json`—`http-06.json` 保留最终请求JSON、直接从raw bytes解析的响应JSON、HTTP状态、时间、原始字节长度/SHA-256、finish_reason、usage、普通content与工具参数全文。**raw bytes不原样落盘**：响应JSON中的reasoning_content等内部思考正文只保留字符数和SHA-256，凭证去除。故脱敏JSON不是字节级原件，哈希对应捕获时的原始响应，不对应美化后的脱敏文件。没有把供应商思考链作为可见论据，也不引用其中草稿。

`sdk-01.json`—`sdk-06.json` 另保留SDK归一化响应（同样去思考）。每轮finish_reason、普通content、tool-call ID/name/arguments及实际返回usage标量字段一致。SDK会补null默认字段，不能用整对象不相等推出内容被改写。

## 有效运行结果与逐轮HTTP

有效实例启动北京时间 **15:05:27**，任务提交 **15:05:29**，commit **15:06:05**，展示ready及实例退出 **15:06:33**。主runtime36.198秒，持久化agent_run36.402秒；展示agent_run28.150秒。没有打满360秒。

- request_id：`full-recheck-5cbb0cd0e1d563d2`
- task_id：`aeb66632643541239fc342f01102da4f`
- trip：`trip-101`；保存plan：`plan-103`
- task：succeeded；最终队列空、不暂停、不draining。

| HTTP轮 | 角色/工具 | HTTP耗时 | finish_reason | input / output tokens |
|---|---|---:|---|---:|
| 1 | 主 / maps_text_search | 3.841s | tool_calls | 4987 / 359 |
| 2 | 主 / maps_search_detail | 2.907s | tool_calls | 7147 / 199 |
| 3 | 主 / maps_direction_walking | 3.507s | tool_calls | 8554 / 338 |
| 4 | 主 / query_weather | 8.262s | tool_calls | 8984 / 741 |
| 5 | 主 / submit_changes | 14.307s | tool_calls | 9146 / 1304 |
| 6 | 展示 / 普通HTML回复 | 27.693s | stop | 1932 / 3222 |

全部HTTP200；每个主响应恰有一个工具调用、content空字符串，最后submit结束即不再调用主模型。主累计input38818/output2941；含展示累计input40750/output6163。这里output为供应商实际completion_tokens（可能含内部思考），**不能等同805字符正文的独立token数**；供应商没有提供可据以拆分的明确正文token计数。

### 真实执行的查询

1. `maps_text_search({keywords:"王府井",city:"北京",citylimit:true})`，0.431s，得到王府井步行街 B000A48169 等候选。
2. `maps_search_detail({id:"B000A48169"})`，0.115s，metadata明确amap_rest/v3/place_detail，地址王府井大街256号，location 116.412007,39.913479；biz_ext返回24小时营业/评分4.8。这是POI资料，不证明特定出行日所有商铺开放或免预约。
3. `maps_direction_walking({origin:"116.409385,39.908798",destination:"116.412007,39.913479"})`，0.253s，**仅去程**912米/730秒（约12.17分钟），含具体步行steps。没有反向步行或公交查询；用户允许近距离无需乘车，因此不把未查公交本身算传输失败。
4. `query_weather({city:"北京",date:"2026-11-05"})`，2.287s，真实Open-Meteo返回雾、4.7–16.2°C、降水概率0。概率0不等于确定“无降水”。
5. `submit_changes`，0.014s，replace正文，成功；没有校验失败后反复修正文或手工补写。

完整args和工具返回见 `tool-evidence.json`、`trace.json`；这些来自实际执行，不是诊断脚本假造工具对话。

## 最终submit正文、存储及内容检查

- `submit_changes.itinerary.text` **805字符**；普通正文全文保留于 `analysis.json` 的plan_text及submit_arguments、`http-05.json`的arguments、API workspace和实际工具证据。
- 正文开头：`## 概览`；末句：**“花生过敏请在点餐时明确告知服务员。”**，不是旧样本的“符合”。
- submit参数正文、API plan.text及隔离SQLite `plans.plan_json.text`逐字一致。SQLite仅在本次临时库以mode=ro读取验证，原库从未打开。
- 正文SHA-256：`8270aed9803e6a393c0e5319308a2effcf8d1d715429bb5a6d2322eeffa19412`。
- 首轮输入有禁忌，最终正文也有花生提醒；早餐北京饭店、午餐王府井附近、下午回住宿休息、晚餐北京饭店均有。无新增景点。通过“无需乘车”“不必走完全程”“避免就餐走远”等表达少走路，但仍约1.8公里往返，不代表满足任何未提供的具体步行上限。
- 本次最后一轮finish_reason明确为 **tool_calls**，JSON完整，805字末句完整，**没有length截断证据**。原69字样本没有保留原始finish_reason，不能用本次的tool_calls倒推旧样本原因。更不能以一次未复现宣布修好。
- 模型在同次submit中把本轮路线约束追加到requirements_text，需求revision2→3；最初两项保留。旧样本需求仍仅两项，这也是本次自然模型行为差异，未做人为校正。

### 仍存在的质量风险（与“未完句”分开）

1. **星期错误**：正文写“11月5日（周三）”，Python标准日历计算2026-11-05为周四；HTML照抄。
2. **费用虚构前提**：“两顿在住宿解决（含房费）”未见任何住宿套餐/已付款信息或工具证据；“全天直接支出主要午餐”基于此未经确认前提，不可接受为可靠预算。
3. **返程估算没有标边界**：仅查了去程，正文写“原路……同样约912米/12分钟”，没有说明返程未独立查询；往返约1.8公里不含街区闲逛和饭店内部步行。正文虽用“约”，仍缺查询覆盖范围及出发前导航核实说明。
4. **条件不够审慎**：POI24小时资料直接写“24小时开放”，另称免费、商铺可随意进出休息，缺具体日期临时管制/商铺营业时间等核实边界；地图详情没有给出酒店餐费、当日免票/预约或食物成分。
5. **饮食语义扩大**：用户只说“不吃花生”，末句使用“花生过敏”；不能据输入确证过敏诊断。虽有配料提醒，不能因此替其餐饮安全背书。
6. **天气概率被确定化**：工具返回降水概率0，正文/HTML写“无降水”，未明确预报可能变化。餐饮40–120元也只是模型估算，非菜单报价。

因此可确认的是“本次正文完整与保存链路成功”，不是全量行程质量验收通过。

## HTML对照

真实display Agent生成ready，公开版本绑定guide API返回200；最终HTML保留在 `generated-guide.html`，提取文字见 `analysis.json.html_visible_text`。没有另跑浏览器截图或响应式验收。

HTML保留三餐、午后返程休息、花生提醒、往返约1.8公里；未发现把805字正文裁成旧69字问题，也没有另增目的地。错误星期、饭店含餐前提、返程同值、确定无降水等均源自主正文并被保留，不能归因于HTML生成才新增。HTML的“全天”标题/呈现润色不构成对营业或安全事实的验证。

## 交付、清理及边界

证据目录：`qa/acceptance-2026-11-04/full-recheck/`。主要文件为environment、activation-context、api-evidence、http-01至06、sdk-01至06、trace、tool-evidence、analysis、summary、verification、shutdown及generated-guide.html；准备/无效测量独立归档，不覆盖先前验收附件。

仅关闭本次实例；shutdown确认线程结束，socket connect_ex=10061，8767不再监听。临时脚本、所有本次SQLite、原始进程输出日志在提取脱敏证据后删除；清理验证记录见cleanup.json。产品代码、真实.env、模型配置、旧报告/附件未改，未commit。原dirty工作树保持。

一次有效样本且history不同，无法证明旧问题发生概率、根因或已消失。本次不提出已实施修复，不以新增质量问题替代原缺陷，也不追加有效样本追求复现。
