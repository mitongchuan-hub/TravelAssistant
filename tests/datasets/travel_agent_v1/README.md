# 旅行Agent测试集

本目录保留 v1.1 数据与历史验收协议。当前重建版本见[v2 测试集](../travel_agent_v2/README.md)，运行器默认使用 v2；复测 v1 须显式传入 `--dataset tests/datasets/travel_agent_v1/cases.json`，旧结果不按新协议倒改。

`cases.json`当前为v1.1.0，共36条，固定评测日期2026-10-08，使用合成旅行和受控工具。执行器为`tests/run_travel_dataset.py`及`tests/run_travel_dataset_batch.py`，6条并发/去重/耗时/长文字案例走`tests/automated/test_travel_dataset_system.py`。

v1.1适配只保留最新版行程：删除历史fixture，预期由`expected_plan_versions_delta`改为`expected_plan_changed`。TA-026检查确认当前状态，TA-027检查请求恢复旧版本时解释不支持且当前数据不变。TA-034检查共用Loop的工具耗时，TA-036允许原文后的展示转换，完整原文、转义与重启一致仍必须满足。此前v1.0输入已随原验收证据冻结，历史结果不按新协议重新计分。

## 数据和判定

- `setup`引用隔离旅行fixture，各例重新创建，不能共享变更后的状态。
- `tool_fixture`指定工具成功、未知、歧义或失败情境。工具返回是合成证据，不能对外当成实际旅行事实。
- `steps`按顺序执行；并发案例按when设置阻塞与释放。`checks`逐项评审，`forbidden`任一出现则失败。
- `expected_plan_changed`为true/false/null，比较本步骤前后当前计划全文、状态、展示与依据；null表示不锁定变化。需求变化与任务状态仍独立检查。
- `priority`为P0硬约束、权限、事实与保存安全，或P1业务质量。`layer`区分模型业务与系统集成。

真实模型案例记录原文、回复、工具、需求/当前行程前后快照、任务状态、原文指纹和展示校验。新展示JSON检查Schema、日期、原文绑定，再人工评审未知、估算、预算与条件是否忠实；结构正确不证明语义正确。

业务通过、业务失败、题面歧义、环境阻塞、受控系统测试分别统计。HTTP200、任务succeeded或回复“已保存”不代表方案质量合格。原文与展示、版本协议与程序schema_version不是同一概念；产品不维护历史行程。

质量按约束遵守、可执行性、证据可靠性、修改忠实度、表达完整性分别0～2分，不适用记N/A。单次受控模型结果不证明稳定性或真实旅行事实；重复或补测单独留存，不能覆盖首次失败。

## 覆盖与执行

| 用例 | 关注点 |
| --- | --- |
| TA-001～006 | 意图、需求增量、预算口径、撤销、缺失/冲突 |
| TA-007～012 | 抵离窗口、当天往返、跨月、当地时间、老人、预算冲突 |
| TA-013～019 | 地点歧义、开放预约、天气、路线方向、工具失败、过敏、来源注入 |
| TA-020～025 | 只读解释、精确替换/追加、歧义目标、局部/全局修改 |
| TA-026～029 | 确认当前、旧版本请求不支持、清空聊天、需求部分成功 |
| TA-030～033 | 新消息作废旧轮、异常迟到、排队取消、请求去重 |
| TA-034～036 | 工具耗时、原文交付、长文字与安全展示 |

每次使用新的C盘输出目录，记录模型/端点（无密钥）、源码HEAD和未提交状态、数据集快照、源码manifest及评审。只有内存Store或独立SQLite，不向8000业务服务发送测试业务请求。工具耗时与模型等待分别记录。

```powershell
& E:\anaconda\envs\TravelAssistant\python.exe tests/run_travel_dataset_batch.py --dataset tests/datasets/travel_agent_v1/cases.json --output "$env:TEMP\travel-agent-new-run" --workers 2
& E:\anaconda\envs\TravelAssistant\python.exe -m pytest tests/automated/test_travel_dataset_system.py -q
```

历史v1.0报告：[首次执行](../../../docs/travel-agent-evaluation-20261008.md)、[四意图全量验收](../../../docs/travel-agent-evaluation-workflow-20261010.md)。当前最新版存储的回归、浏览器与迁移检查见[简化验收](../../../docs/latest-itinerary-only-20261010.md)。已完成v1.1完整首轮：30条真实模型24通过、1未满足全部检查、2题面/夹具问题、3上游502阻塞；6条受控系统通过。3例各补测一次，2业务通过、1仍超时，原始首轮保留。详细判定、展示问题与证据见[最新版工作流测试集报告](../../../docs/travel-agent-evaluation-latest-only-20261010.md)。
