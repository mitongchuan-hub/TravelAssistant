# 第一层：单工具调用正确性

100 条固定中文合成用例，每例只评测一次模型响应，选择时提供当前完整 8 工具库。不执行工具、不查询真实天气/高德、不写业务数据，不运行调用链，不重试、不预热、不切换备用模型。

| 工具 | 用例数 |
|---|---:|
| read_context | 16 |
| submit_changes | 24 |
| query_weather | 12 |
| maps_text_search | 16 |
| maps_search_detail | 8 |
| maps_direction_walking | 8 |
| maps_direction_driving | 8 |
| maps_direction_transit_integrated | 8 |

## 覆盖与局限

- 全部为应调用一个工具的正例，不包含无工具/澄清场景；这不是全业务意图评测。
- 日期基准固定为 **2026-11-03**，不随运行当天变化。日期未知例要求省略 date；明确日期、相对日期分别覆盖。
- read_context：需求/行程使用用户明确重新读取的指令；历史消息/证据包含 query、offset、limit 默认值和边界。需求和行程在实际初始输入中完整提供，不能把这些例解释为必须补齐缺失信息。
- submit_changes：需求全文新增/保留/修改/删除/清空8例、精确追加/替换8例、确认4例、清空行程4例。仅覆盖闭合、明确的文本操作，**不评测开放式完整行程生成**。原话必须逐字一致；正文不是关键词命中评分。
- 地图详情、路线使用 `prior_calls` 中固定的合成服务器查询结果，ID/坐标不是模型猜测；前置结果只作为只读上下文，不计为本次待测调用。合成 POI 不是现实营业/地理证据。
- keywords 使用用户明确给出的检索词；城市接受数据中预列的有限别名；坐标用 Decimal 数值相等比较，接受尾随零和空白，不接受近似位置或交换方向。
- gold 与模型输入分离。修改用例和允许答案应先于评测冻结，不能看到输出后放宽规则来刷分。

## 指标

1. 工具选择正确：恰好一个预期工具调用。
2. 参数正确：通过当前生产 JSON Schema、参数白名单和每例确定性值约束；未授权操作失败。
3. 格式合法：真实结构化 function call、合法 JSON 对象、无非空正文。拒绝重复键、NaN/Infinity、代码围栏、尾随内容、非对象；不自动修复。
4. 综合通过：三项同时通过。全量 **至少95/100** 达标。

三个分项的分母同样为全部选定用例；网络/API错误、不完整输出仍占分母，但无法观察的分项标记为不可评估，而不是谎称已看到非法格式。当前 transport 会在截断时丢弃部分输出，脚本无法取得这些原始参数及 usage，报告明确记缺失。

95/100 只说明该固定合成集合上的观察结果，**不能证明生产总体准确率≥95%**，也不是工具执行成功率。首次失败不会被后续纠错覆盖。重复措辞和共用夹具用于检查基础调用稳定性，不代表100种相互独立的业务难题。

当前闭合评分对明确给定文字采用严格全文匹配，不评判任意改写的语义等价性。行程编辑中可省略的 context 与显式 null 等价；替换操作额外提供的非空 context 必须在固定原文中唯一匹配且只含一次目标，其他操作字段仍须严格一致；追加 old_text 的 null 与空串按预列答案接受。数据中未允许的附加问题或操作判失败。

## 运行

仓库根目录，使用项目固定 conda Python：

```bash
# 默认只校验数据和请求构造，不创建客户端，不测模型命中率
E:/anaconda/envs/TravelAssistant/python.exe -m tests.tool_calling_eval

# 离线评分器与隔离契约测试
E:/anaconda/envs/TravelAssistant/python.exe -m pytest tests/automated/test_tool_calling_eval.py -q

# 显式真实模型调用：全量100次，会发送合成输入和当前prompt/schema，可能计费
E:/anaconda/envs/TravelAssistant/python.exe -m tests.tool_calling_eval --live

# 仅试跑指定用例，结果明确标为子集，不算全量验收
E:/anaconda/envs/TravelAssistant/python.exe -m tests.tool_calling_eval --live --ids weather-01 walk-01
```

沿用项目 `.env` 的模型、端点、线协议及思考配置，不迁移 provider。`.env` 加载会覆盖进程环境；加载后检查 `OPENAI_STORE_RESPONSES=false`，为 true 时拒绝运行，不修改配置文件。报告不写密钥、带凭据URL、原始HTTP异常正文或推理内容。未配置价格，费用未知，只报告服务端可取得的 token 用量。

输出为 `.cache/tool_calling_eval/<timestamp>-<id>.json` 和 `.md`，逐例保存，包含原始 arguments、判分原因、逐工具统计、数据/prompt/schema及哈希。中断标记 INCOMPLETE，不输出达标结论。全量达标退出0，未达标退出1，CLI输入错误退出2。`--dataset` 可指定同一契约的100条数据，`--output-dir` 可指定报告目录。
