# 2026-10-12 展示独立提取实验归档

结论：当前split候选不接入生产。正式对照24次均返回结构/日期正确卡片，但仍有准备事项遗漏、错放、无依据状态断言和残句；部分内容改善未达到验收门槛。完整说明见[实验报告](../../display-split-experiment-20261012.md)。

这是固定真实规划原文的展示转换实验，不是四类完整业务规划验收。whole调用现有整篇入口，split由LLM独立提取全局信息和每日安排，再由程序合并；两种方案均为180秒总预算、最多2次实际SDK请求。每原文每方案3次，串行且调换方案顺序。本轮实际36次SDK请求，无连接/超时失败、无兼容/结构纠错。

- [性能报告](report.md)、[性能数据](summary.json)：同窗口成功中位数、最大值、失败等待与请求数。
- [人工评审](semantic-review.md)、[逐项评审数据](semantic-review.json)：71项信息在24份输出下的426项核对，以及补充问题。专项通过不证明穷尽语义正确或事实已核实。
- [全部正式结果](results.json)：原文、日期、卡片、分阶段输出、usage及实际请求记录。
- [清单](review-checklist.json)、[人工决定](manual-decisions.json)：每项原文引用和问题判定，不作为模型输入。
- manifest.json、source_snapshot：运行参数、源码/Schema/提示词与输入快照；逐样本目录保留结果和脱敏日志。
- interrupted-pilot：首批因执行工具60秒超时中断，完成1份whole、下一份split在途；不混入正式成绩，不算模型失败。
- automated-tests.log：489项自动回归。浏览器受控回归结果记录在主实验报告。
- archive-hashes.json：除该指纹文件本身以外的归档文件SHA-256，验证内容没有被后续补测覆盖。

评审报告可仅用Python标准库从人工决定重新生成：

```powershell
& E:\anaconda\envs\TravelAssistant\python.exe docs/evaluations/display-split-20261012/build_manual_report.py
```

review-template.json保持初始pending_review；已完成的人工结论在semantic-review.json，二者分别保存，避免执行器刷新模板覆盖人工评审。补测必须另建目录，不能覆盖本归档。
