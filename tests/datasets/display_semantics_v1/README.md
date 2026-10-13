# 固定原文展示语义专项

四份原文冻结自2026-10-11优化1的真实规划，分别为聊天当天、页面当天、三日与七日。只测试展示转换，不执行旅行业务、不访问业务SQLite。日期保持原文一致，不随运行日变动。

`cases.json`含完整原文及对应计划需求快照，另列衣物词组、近似时间与已知错误时长作为专项观察。检查通过只证明指定字段/表达仍出现，不证明预约、过敏、条件、事实与所有预算口径完整，也不代表模型语义质量通过。

运行（默认每份3次、串行、连续2次连接/超时错误停止）：

```powershell
& E:\anaconda\envs\TravelAssistant\python.exe tests/performance/run_display_semantics.py --output "$env:TEMP\display-semantics-$(Get-Date -Format yyyyMMdd-HHmmss)"
```

可传`--before`指定修改前results.json生成相同原文的专项对照；原文不同则拒绝比较。可用`--cases LAT-012`单独验证场景。输出目录必须为空，首次失败不能覆盖。

输出包含逐样本结果、冻结源码/Schema/提示词及指纹、脱敏调用日志、完成/阻塞状态、comparison/checks.json与report.md。模型超时无卡片时质量标记not_evaluated，不算内容失败或内容通过；中断请求不算完成样本。结构失败仍保留原文及诊断。

离线复核既有结果：

```powershell
& E:\anaconda\envs\TravelAssistant\python.exe tests/performance/review_display_semantics.py --before 修改前results.json --after 修改后results.json --output "$env:TEMP\display-semantic-review"
```

[衣物、住宿、天气合并单次提取实验](combined-experiment.md)使用`combined`策略，新增天气独立字段，一次响应包含每日安排及所有全局信息，最多1次实际请求；18项天气核对与71项原文清单一起人工评审。生产协议与展示路径继续保持现状。

衣物、住宿、预算、忌口与核实条件的[整篇/独立提取实验](split-experiment.md)使用同一份固定原文，并新增71项人工核对清单；正常两次提取仍由LLM负责，程序仅合并与校验。运行入口为 `tests/performance/run_display_split_experiment.py`，生产路径不随实验切换。

人工继续对照：活动顺序、近似时间、原文时长、分类、装备/住宿字段、预算人数范围与非报价措辞、忌口/过敏、固定预约、雨天备选、二选一与未知。词语和标题检查不能替代这一步。
