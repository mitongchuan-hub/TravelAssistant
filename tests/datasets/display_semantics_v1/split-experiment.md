# 整篇与独立全局提取实验

实验入口：`tests/performance/run_display_split_experiment.py`。这是隔离评测工具；应用的生产展示入口继续使用现有整篇转换。

同一份原文比较两种方案：

- whole：生产`generate_plan_display()`，正常1次请求，结构/日期失败时按现有条件允许1次纠错。
- split：先由LLM提取overview、budget_note、preparation、pending_items、risks，再由LLM提取schema_version、title、days；两次都读取同一份完整原文，程序合并并校验。程序不会补写衣物、酒店或费用。

每种方案共享180秒、最多2次实际SDK请求的生产预算（可由现有env覆盖，运行元数据记录实际值）。split没有额外纠错额度；协议兼容若消耗第2次请求，后续阶段因预算耗尽回退。任一阶段失败均不返回半份卡片；原文与分阶段诊断保留。取消、总预算与前台优先callback在实验函数中有边界测试，尚未接入正式后台任务。

四份原文，每方案各3次，共24次转换，串行运行。第1/3次先whole后split，第2次先split后whole。数据集不变，避免分别重新规划导致输入不同。跨时间窗口的历史耗时仅作为参考，性能比较主要用本轮配对结果；这些样本不能证明稳定P95或生产可用率。

```powershell
& E:\anaconda\envs\TravelAssistant\python.exe tests/performance/run_display_split_experiment.py --output "$env:TEMP\display-split-$(Get-Date -Format yyyyMMdd-HHmmss)"
```

支持`--cases LAT-012`、`--repeats 1`和`--strategies whole split`；输出目录必须为空。连续2次传输失败停止，失败记录不可覆盖。补测使用新目录，保留首次失败，不能仅选成功计算通过率。

`review-checklist.json`冻结71项原文引用与预期含义。这是评审依据，不是发给LLM的答案；执行前核对引用是否存在。`review-template.json`逐样本生成pending_review清单；程序辅助字段检查不自动给语义通过。人工结果另存semantic-review.json，避免报告刷新覆盖评审。

输出：

- manifest.json、source_snapshot：模型/协议配置、预算、原文与源码/提示词/Schema指纹，不含密钥或.env。
- results.json、逐样本result.json与脱敏调用日志：源文字、结果、实际SDK请求、usage；服务未报告的token保持null。SDK耗时包含网络/排队/预填充/生成。
- progress.json、status.json：正在执行的样本、完成/阻塞状态。执行工具中断单独记录，不当作模型连接失败。
- report.md、summary.json、review-template.json：成功与失败等待、请求数、衣物字段观察及71项人工清单。

模型调用发生在隔离进程，不创建Store、不写业务SQLite、不向8000业务服务提交业务。长实验应独立启动；不要与有较短执行超时的测试命令放在同一进程树中，以免工具超时连带终止实验。完整实验结束后核对业务库，并逐项评审决定是否进一步试验或接入。
