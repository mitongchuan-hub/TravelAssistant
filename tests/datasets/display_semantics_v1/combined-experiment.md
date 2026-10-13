# 衣物、住宿、天气与行程合并为一次提取

隔离实验入口：`tests/performance/display_combined_experiment.py`，由通用执行器的`combined`策略运行。一次LLM响应同时提取三个独立栏目、每日行程、概览、预算、核实事项和风险。程序定义字段、校验日期与结构；衣物、住宿、天气等内容仍由LLM根据原文提取。

请求Schema将preparation和weather放在长days列表之前，实验同时改变字段组织及提取提示，因此不能将效果单独归因于新增天气字段。天气使用独立列表，包含原文日期表达、天气、气温、降水概率、查询时间和复核提醒；只保留原文已有预报，不额外查询，不把只给两处天气补成七天预报。原文没有天气时允许[]，没有查询时间时为null。

此候选使用实验版本`1.1-weather-experiment`，尚未改变生产1.0协议、模板或后台任务。验证公共行程字段时投影到原1.0校验器，再保留实验版本与weather；不将天气或准备内容清空以兼容旧协议。

本轮限定180秒总预算、最多1次实际SDK请求，无工具、无纠错。结构/日期/网络失败直接保存fallback及原始诊断；协议兼容若需要第二次SDK调用，也会因单请求限制停止，不隐式追加请求。

四份固定原文，每份3次，共12次：

```powershell
& E:\anaconda\envs\TravelAssistant\python.exe tests/performance/run_display_split_experiment.py --strategies combined --output "$env:TEMP\display-combined-$(Get-Date -Format yyyyMMdd-HHmmss)"
```

可用`--cases LAT-015 --repeats 1`冒烟。输出必须是新目录，连续两次连接/超时失败停止。真实执行器不使用业务Store，不向8000提交业务。

评审沿用71项清单，并增加[18项天气核对](weather-review-checklist.json)：路线查询日期不能借作天气查询日期、查询时间未知保留null、约/最高/≤/前后限定、仅部分日期预报、天气复核及衣物栏目分工。这些核对不是模型输入，程序不会据此写入预期答案。

模型输出ready只证明结构与日期；人工评审另存semantic-review.json。既有整篇/两次提取的归档可以对照信息覆盖，跨批次耗时只列参考，不能直接宣称因果提速。要正式选择生产方案仍需同窗口配对、扩展未知/过敏用例及完整链路验收。
