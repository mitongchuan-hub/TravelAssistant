# 单次合并衣物、住宿、天气提取实验归档

2026-10-12。12份固定原文转换均结构/日期通过，实际12次SDK请求。衣物12/12保留，有多晚住宿的6份均保留候选与待确认，天气12/12有原文预报。一次响应也包含每日行程、预算和核实条件，程序不补写内容。

仍有时段补算时长、推导入住时刻、概览数量错误、核实时间范围错配和建议限定丢失。6份在当前专项范围未发现重大问题，5份失败，1份歧义；不是整体自然语言质量通过率。当前实验版本1.1-weather-experiment未接入生产。

- [完整实验报告](../../display-combined-weather-experiment-20261012.md)
- [性能报告](report.md)、[性能数据](summary.json)、[字段覆盖观察](coverage-summary.json)
- [人工评审](semantic-review.md)、[267项核对数据](semantic-review.json)
- [12份原始结果](results.json)：源原文、天气与其他字段、原始模型输出、SDK请求和usage。
- review-checklist.json、weather-review-checklist.json、manual-decisions.json：71项主清单、18项天气清单和人工决定。天气清单为采样后的评审补充，不是模型输入；quote检查均针对相同源原文。
- manifest.json、source_snapshot：运行预算、模型配置、代码/提示词/Schema/原文指纹。weather仅为原文提取，未额外查询。
- 逐样本目录：结果及脱敏调用日志；automated-tests.log：497项完整回归。
- archive-hashes.json：除指纹文件本身之外的归档文件SHA-256。

仅使用Python标准库，根据已保存的人工决定重建评审报告：

```powershell
& E:\anaconda\envs\TravelAssistant\python.exe docs/evaluations/display-combined-weather-20261012/build_manual_report.py
```

旧整篇/两次提取归档继续保留。跨批次性能不能直接当提速比例；补测另建目录，不覆盖本批次。
