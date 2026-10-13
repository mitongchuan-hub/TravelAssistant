# 四类意图工作流验收（2026-10-10）

已实施四类意图识别、无工具闲聊 LLM、查询/规划/修改共用 Agent Loop、规划内部需求工具、五组必填创建页，以及行程原文后的无工具展示转换。新行程按天与活动卡片展示，原文、展示 JSON、需求快照和实际工具依据同版本保存，转换失败回退原文。

## 回归与浏览器

Python 最终回归 **379 passed in 31.61s**。应用源码通过 AST 语法检查；git diff 空白检查通过。依赖增加 jsonschema，展示协议使用独立的 app/schemas/itinerary-display-v1.json。

覆盖四类分流、无工具节点拒绝工具调用、无独立需求委派、五组创建缺项/格式/冲突、需求部分成功、未检查不能查询或写行程、等待目标持久化、含糊授权不创建等待生成目标、原文指纹绑定、日期框架、转换失败/超时、旧轮作废、清空聊天取消未完成任务、确认/恢复/重启、依据快照、转义和旧数据兼容。

六类浏览器检查通过：聊天、任务、创建及需求编辑、新行程卡片与原文、可信地点/路线、旧时间线。新行程在320/390/430/1000宽度下无横向溢出，卡片局部反馈能定位文本；确认恢复和刷新保留原文及依据。连续消息在旧请求结束前发起新轮；页面没有恢复浮动任务状态层。

## 真实模型与受控工具

模型为 gpt-5.6-sol，Responses、store=false，无服务端会话链，SDK自动重试为0。使用合成旅行和内存 Store；外部旅行工具受控返回未知，不查询或写入用户业务数据。

| 场景 | 验收结果 | 单次耗时 |
| --- | --- | --- |
| 闲聊 | 意图识别后无工具 LLM 回复，不生成行程 | 7.643秒 |
| 查询 | 天气工具返回未知，回复保持未知，不修改需求或行程 | 14.095秒 |
| 仅补充需求 | 预算2500元通过规划内部工具更新；“只记录，先不要生成”未生成行程 | 10.347秒 |
| 页面规划 | 完整文字1804字符、两日12张展示卡片，同版本保存；7次受控外部查询 | 93.976秒 |
| 修改 | 原文完整保留，末尾精确追加指定一句；两日12张卡片、0次外部查询 | 197.828秒 |
| 补充续接 | 只回复“两人，预算合计2600元”，识别为planning/generate；保存2人和2600预算并生成两日展示 | 56.458秒 |

确认与历史恢复的程序检查通过，恢复时原文与展示数据保持对应。补充续接的后续验收使用简短行程、禁止外部查询的合成备注，以隔离等待状态、路由和需求参数链路；不与普通规划/修改耗时作因果对比。该例输出两日10张卡片，未执行外部查询。

新增展示调用确实增加等待：本次普通规划的 display 请求约43.944秒，修改约96.748秒。单次运行不能证明稳定时延或提速。JSON Schema、日期框架和原文指纹校验也不证明逐句事实、逐项时间和每次修改范围均已自动核实。本次最初验收未重跑完整36条旧测试集；后续完整执行结果见[新工作流测试集验收](travel-agent-evaluation-workflow-20261010.md)，已发现聊天确认、组合请求遗漏和展示忠实度问题。

## 验收发现与修正

首次展示调用被服务拒绝：$ref旁存在description。服务此前被错误判断为“不支持格式”并降级JSON对象，最终结构无效，程序正确保留原文。修正为请求时展开引用，并把invalid_json_schema从兼容降级条件中排除；随后规划与修改的原生Schema请求和展示校验通过。

首次补充续接误选仅记录需求，需求工具多次把user_quote放在changes内部，最终达到240秒时间预算，需求未被错误写入。修正等待生成目标的意图优先级，将最新原话作为单独消息提供，明确工具参数层级、纯数字金额及具体错误提示。后续同一句补充回答正确承接生成，原话在工具顶层，预算字段为纯数字2600。保留初次失败记录，不将其记作成功。

## 本地启用与数据

本地单进程服务已启动，健康检查status=ok，静态版本为20261010-four-intent-workflow-display-v1。启动后仍为12趟旅行、76条消息；业务SQLite的app_snapshots原始payload与启用前备份逐字相同。未批量转换旧行程，也未重跑中断任务。

代码尚未提交或推送远程；当前远程提交仍为927b947。

## 本机证据位置

- 最终回归与启用记录：C:\Users\16156\AppData\Local\Temp\travelassistant-workflow-accepted-0e15cbc1f15b40e1aed2e13946c21f69，regression.xml与activation.json。
- 最终闲聊/查询/需求：C:\Users\16156\AppData\Local\Temp\travelassistant-workflow-final-routes-8bc2c386babe41bdb0d67c21f3266fb4，review.json与逐请求trace。
- 原生展示规划/修改/恢复：C:\Users\16156\AppData\Local\Temp\travelassistant-workflow-live-final-d40d703e3cef4ef5bcb6591f6293e269。
- 续接通过：C:\Users\16156\AppData\Local\Temp\travelassistant-workflow-resume-final-86621cb5cfa640b6bc6c379e9efd3aac。
- 首次展示失败：C:\Users\16156\AppData\Local\Temp\travelassistant-workflow-live-f3d4fc700db643d787ff4106a735726c。
- 首次续接失败：C:\Users\16156\AppData\Local\Temp\travelassistant-workflow-resume-f00d0c2482ec407eaa036b72690ad20e。
- 新卡片浏览器产物：C:\Users\16156\AppData\Local\Temp\travelassistant-text-ui-mF5Lae。
- 旧时间线产物：C:\Users\16156\AppData\Local\Temp\travelassistant-workflow-legacy-ui-62a6523325b6478680dc7f109ab6827c。
- 改造前源码与SQLite：C:\Users\16156\AppData\Local\Temp\travelassistant-before-workflow-z1w86sgc，包括before-activation.sqlite3。
