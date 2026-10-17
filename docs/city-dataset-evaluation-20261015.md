# 同城测试集 v1 首轮离线验收（2026-10-15）

## 结论

已新增 `city_outings_v1` 测试集和可重复执行入口，并完成首轮冒烟、28场景全量及项目自动回归。**受控执行全部通过，真实模型内容质量未评估。** 本轮只修改测试与文档，不修改业务代码、排版、模型配置或业务数据库，不重启现有服务，不提交或推送代码。

| 执行 | 场景结果 | 实际 pytest 结果 | pytest 耗时 |
| --- | --- | --- | --- |
| 冒烟 `smoke` | 10/10通过 | 18 passed | 9.56秒 |
| 同城全量 `current` | 28/28通过 | 99 passed | 21.72秒 |
| 项目 `tests/automated` 全量 | 不换算为数据集场景 | 915 passed，0失败、0错误、0跳过 | 74.81秒 |

场景映射会展开参数化测试，故28场景与99项不是两个独立质量样本集；冒烟也属于全量的子集，不能相加。915项包含新测试和既有回归，也不能与99项累加。新增测试文件贡献71项：数据集契约9项、受控业务/展示16项、执行器46项。

## 测试集与执行路径

- [数据与协议](../tests/datasets/city_outings_v1/README.md)：版本 `1.0.0`，固定日期 `2026-10-15`，输入与工具资料均为合成数据。
- `CO-B001..012`：12个业务场景，覆盖最小住处输入、无日期多天、跨月、预算口径、过敏未知、只读查询、仅记录需求、字面编辑、指定站点替换、路线不可用、模型超时与卡片重试。
- `CO-D001..004`：4个展示/归属场景，覆盖无日期、约数、明确0元、未知费用、条件雨备、过敏限定、安全解析和旧正文哈希发布保护。
- `CO-S001..012`：12个系统场景，复用现有可执行回归，覆盖表单与候选、可信地点、路线方向/方式、未知中间站、版本保护、目标元数据、幂等、住宿快照、原排版及文本转义。
- 入口 `tests/run_city_dataset.py` 只支持离线。`execution_mode=live_agent/live_display` 和 `live` profile 仅是未来评测分类，**不会开启真实调用**。
- 业务测试走真实 `DemoStore`、`submit_task/run_task`、`TravelAgentRuntime` 与保存/展示链路；替换的是底层模型请求与外部工具资料，不直接伪造整段 `AgentReply` 跳过业务执行。

## 已核验的保护点

- `CO-B001` 使用实际 `build_route_view` 检查住处→公园→餐厅→住处三段合成路线，定向、方式、12/9/18分钟及导航坐标参数分别绑定，投影不修改 Store。
- `CO-B006` 只读场景检查原方案与需求不变，并验证 research 不能获得需求修改权限。
- `CO-B008` 使用实际字面编辑工具；范围外原文和旧方案依据保留。
- `CO-B009` 使用服务器解析的站点目标，目标上下文与用户原话分离，查询替换后的相邻路线。
- `CO-B010` 工具无路线数据时保留未知，不用反向或其他交通方式补出分钟数。
- `CO-B011` 注入模型超时，预期业务任务失败；测试通过表示故障确实发生且旧方案被保护，不表示该任务成功生成。
- `CO-B012` 注入展示失败后单独重试卡片，完整正文先保存，重试不重新规划。
- `CO-D003/D004` 检查危险/歧义解析拒绝和旧哈希发布保护；它们不适用自由文本内容质量打分。
- 原布局场景检查默认聊天、原底栏、概览/准备事项/日期导航顺序，以及新增样式不覆盖原卡片。本轮未以这些DOM断言冒充浏览器或视觉验收。

## 归档与可追溯性

本机首次结果分别保存在：

```text
E:\TravelAssistant-test-results\city-smoke-20261015-r1\
E:\TravelAssistant-test-results\city-suite-20261015-r1\
E:\TravelAssistant-test-results\city-regression-20261015-r1.log
E:\TravelAssistant-test-results\city-regression-20261015-r1.xml
```

两次数据集执行均为一次去重后的 pytest worker session；未覆写旧目录。归档含 `metadata.json`、`dataset/cases.json`与评审模板、`source/`副本、前后manifest、`pytest.log`、结构化 `pytest-report.json`、JUnit、`results.json`、`summary.json`、`review.json`及逐场景证据。

- 全量28条结果均有实际 nodeid，未匹配选择器0、收集错误0、跳过0。
- 全量16份 B/D 独立 `evidence/<case_id>/result.json` 均存在且执行状态为 passed；12个 S 系统场景通过实际 pytest 报告追溯，不伪造规划正文。
- 全量 `review.json` 保持14条 `not_evaluated`、2条 `not_applicable`，不把脚本响应升级为真实模型质量通过。
- 冒烟与全量使用相同209个文件的限定源码快照，两次运行内前后均 `unchanged`、`results_valid=true`。归档排除 `.env*`、业务 `.cache`、认证数据、旧评测目录及依赖缓存。
- 两次 `source-manifest.json` 文件SHA-256：`0c2580221f3181de63ad7280e28960ebd33d049ddbc2c1e461023403fd050137`。
- 冻结 `cases.json` 的SHA-256：`0728b7f2f5044de85e10e0dc179b0445647d53fe83ca7fec6565002b5f53683e`。
- 全量回归结束后，归档内80个业务源码文件与工作区逐项相同。随后仅补充本报告、文档索引、测试目录说明，并将数据集README的归档路径描述改为实际的 `dataset/cases.json`；已归档原始快照与成绩不改写。

业务库未采集指纹、未读取用户聊天或住处；因此不声称用户在测试期间操作的业务数据完全未变化，只声明测试不使用业务数据、不向8000业务接口写测试记录。

## 首轮开发问题与修正

在冻结批量执行前，专项测试发现并修正了测试辅助代码的问题：

1. 合成正文构造中的键读取错误；修正后业务/展示专项16项通过。
2. 合成路线曾使用 `dest_name`，与生产 `destination_name` 不一致。已统一生产契约，增加字段断言及实际三段路线投影验证，最终专项16 passed（10.55秒），随后才进行上述归档批次。

这两项属于测试夹具修正，不是发现并修复了新的业务缺陷。首轮归档冒烟与全量均通过，没有将失败批次覆盖为成功，也未删除或放宽原业务断言。

## 隔离与结论边界

- 导入应用前屏蔽 dotenv 两个入口，关闭默认持久化与真实LLM，清空密钥并阻断非loopback网络；外部工具只用已声明的合成资料。既有持久化测试只使用测试自己的临时SQLite。
- 未调用真实模型或高德，真实模型样本数为0。本轮没有获得外部调用确认，未以测试请求代替授权。
- 受控响应能验证工具权限、保存归属、异常保护和指定输入下的数据保持；**不能证明真实模型会正确分类、完整安排餐次、合理分配预算、忠实修改或准确提取卡片**。
- `ambiguous_place` 没有虚构第二候选；真实程序歧义保护由 `CO-S002/S003` 的受控多候选测试验证，不宣称验证了真实模型的重名选择。
- `rain_reference` 未声明具体天气摘要，工具保持 unavailable；D002雨备限定来自固定原文，不当作查到了真实或合成天气预报。
- B009原需求中的忌口为空，需求不变断言不能单独证明“非空过敏/忌口在开放修改中保留”；该场景修订后的卡片仍为pending，尚未闭环验证新卡片投影。开放式修改的未选段落忠实度仍需真实输出逐项审阅；只有字面编辑有程序级范围外逐字保护。
- B010的不可用工具回包未包含完整端点名，验证的是工具失败后条件正文保持，不能据此宣称已闭环验证该回包在 `route_view` 中的 unavailable 历史路线展示。
- D001/D002使用固定的合成整日 `other` 卡片，证明给定卡片能验证、保存且不改原文，不代表真实展示模型能完成精细活动提取。逐场景证据记录角色、工具与状态，不含每条完整模型请求及拒绝工具回包；相应权限路径另由pytest断言验证。
- 本轮未运行Playwright、实体手机或外链地图。此前原排版恢复的浏览器成绩单独保留，不并入本轮结果。

## 复跑

从项目根，在 PowerShell 使用现有解释器；输出目录必须是项目和业务 `.cache` 之外的新空目录：

```powershell
& 'E:\anaconda\envs\TravelAssistant\python.exe' -B 'E:\pythonProject\learn-claude-code-main\TravelAssistant\tests\run_city_dataset.py' --profile current --list

& 'E:\anaconda\envs\TravelAssistant\python.exe' -B 'E:\pythonProject\learn-claude-code-main\TravelAssistant\tests\run_city_dataset.py' --profile smoke --mode offline --output 'E:\TravelAssistant-test-results\city-smoke-new-run'

& 'E:\anaconda\envs\TravelAssistant\python.exe' -B 'E:\pythonProject\learn-claude-code-main\TravelAssistant\tests\run_city_dataset.py' --profile current --mode offline --output 'E:\TravelAssistant-test-results\city-full-new-run'
```

可用 `--cases CO-B009 CO-B012` 选择重点项，或选择 `business/display/system/layout` 档案。运行期间不要编辑源码和数据；如果发生变化，执行器将整批标记无效而不是输出混合版本成绩。`--browser` 尚未实现，会明确拒绝，不会静默算作已测。
