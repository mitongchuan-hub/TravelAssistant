# 旅行 Agent 测试集 v2

版本 `2.1.0`，固定评测当前日期 `2026-10-10`。按当前代码、Lab Qwen27B 实测问题、旅行删除功能与已确认产品边界重建，共 **70 条**。状态为 `ready_not_live_evaluated`；离线回归通过不代表真实模型质量已通过。

| 类型 | 数量 | 范围 | 执行方式 |
| --- | ---: | --- | --- |
| 当前业务 | 36 | 四意图、需求增量、日期与预算、查询证据、精确修改、当前确认、最新版保护、等待补充、页面生成/修改 | 真实模型 + 合成工具 |
| 展示转换 | 10 | 模糊时间、未知、事实与估算、预算、条件、分类与顺序、日期映射、过敏、长文、引用注入 | 直接调用真实 display 节点 |
| 当前系统 | 16 | 新消息作废、迟到错误、去重、工具计时、长文保存、展示失败/预算/hash/转义、删除级联/权限/活跃任务 | 映射现有或新增 pytest，包含参数化变体 |
| 后续目标 | 8 | 大陆单城1—7天、地铁策略、固定安排、软偏好、移除确认 | 规格待实施，暂不可执行 |

`current` 档案包含前面 **62 条**，其中46条调用真实模型。目标规格不进入当前通过率。当前确认案例 `TA2-026` 与移除确认目标 `TA2-T010` 分别检验各自实现阶段；实现目标时需要升版并替换相关回归，不能同时按两种规则打分。

v2.1将T008文字先读与T009迟到卡片保护升级为当前受控验收。业务执行器在记录主任务就绪后显式收集后台转换结果，以便继续人工评审展示；`primary_seconds`单独记录前台完成时间。

## 相对 v1 的修订

- 新 ID 使用 `TA2-`；保留原始 v1 文件及历史报告，继承用例带 `origin_case`。v1 的数据快照仍应用原始评审协议，不按新标准倒改成绩。
- fixture 直接保存真实字段名：`start_date`、`end_date`、`travelers`、`budget_scope` 等。继承仅合并明确字段，不再把题面日期、人数强制替换为杭州两日默认值。只有测试内部使用空档案，用户创建仍需五组必填。
- 原东京支持场景替换为杭州机场晚到早走、三天两晚。境外请求改为待落地的范围限制目标，不能假称现已支持范围核验。
- 低预算案例允许“未读到报价时保守追问”。实际查到300元/间/晚后才要求与100元预算比较；包含车站名称的住宿查询仍优先返回住宿报价。
- 保留日期参数漏 `end_date`、`user_quote` 位置错误、组合更新后查询、严格追加失败等回归。精确修改按原文逐字比较；需求与任务状态直接检查实际 Store，不只看回复说了什么。
- 新增真实展示转换专项。Schema、日期、原文 hash 与语义忠实度分别检查；字段压缩或推理参数调整必须同时验证预约、未知、估算、饮食条件未丢失。
- 新增旅行删除：删除后重启不能复活、其他旅行与账号保留、他人404、未登录拒绝、queued/running 期间拒绝删除。

## 运行

在项目根目录使用 conda 环境。执行器默认 v2；默认 `workers=1`，避免本地 GPU 并发影响耗时。每次使用新的输出目录，首次和补测不覆盖。

先校验并查看用例，不调用模型：

```powershell
& E:\anaconda\envs\TravelAssistant\python.exe tests/run_travel_dataset_batch.py --profile current --list
& E:\anaconda\envs\TravelAssistant\python.exe tests/run_travel_dataset_batch.py --profile target --list
```

冒烟：8条真实模型用例，覆盖记录需求、歧义查询、精确替换、页面生成/修改以及两条展示转换。

```powershell
& E:\anaconda\envs\TravelAssistant\python.exe tests/run_travel_dataset_batch.py --profile smoke --output "$env:TEMP\travel-v2-smoke-$(Get-Date -Format yyyyMMdd-HHmmss)"
```

当前全量：36条业务 + 10条展示 + 16条系统，系统测试无需外网或模型。

```powershell
& E:\anaconda\envs\TravelAssistant\python.exe tests/run_travel_dataset_batch.py --profile current --output "$env:TEMP\travel-v2-full-$(Get-Date -Format yyyyMMdd-HHmmss)"
```

展示优化对比：同一份源码、源文字、模型配置，每个案例串行重复至少3次，记录首次失败和每次尝试。

```powershell
& E:\anaconda\envs\TravelAssistant\python.exe tests/run_travel_dataset_batch.py --profile display --workers 1 --repeats 3 --output "$env:TEMP\travel-v2-display-$(Get-Date -Format yyyyMMdd-HHmmss)"
```

仅受控系统与数据/执行器检查：

```powershell
& E:\anaconda\envs\TravelAssistant\python.exe tests/run_travel_dataset_batch.py --profile system --output "$env:TEMP\travel-v2-system-$(Get-Date -Format yyyyMMdd-HHmmss)"
& E:\anaconda\envs\TravelAssistant\python.exe -m pytest tests/automated/test_travel_dataset_v2.py tests/automated/test_travel_dataset_system.py -q
```

单例与历史兼容入口：

```powershell
& E:\anaconda\envs\TravelAssistant\python.exe tests/run_travel_dataset.py --case TA2-D002 --output "$env:TEMP\travel-v2-D002-$(Get-Date -Format yyyyMMdd-HHmmss)"
& E:\anaconda\envs\TravelAssistant\python.exe tests/run_travel_dataset_batch.py --dataset tests/datasets/travel_agent_v1/cases.json --cases TA-013 --output "$env:TEMP\travel-v1-TA013-$(Get-Date -Format yyyyMMdd-HHmmss)"
```

支持 `--cases ID ...` 定向执行。`target` 是待实现规格，只支持查看，执行时明确拒绝。`--repeats` 用于真实模型重复；受控系统每批执行一次，参数化变体由 pytest 执行。

## 证据与计时

每次模型调用保存角色、开始偏移、耗时、返回字符数、服务返回的 prompt/completion/reasoning token 数。服务未报告的 token 项为 null，不能凭字符数倒推思考 token。步骤保存总耗时、模型分角色耗时、外部/内部工具耗时、剩余应用开销、文字生成完成时间、展示状态与回退原因。

**display_seconds 已包含于 model_seconds，不能重复相加。** `text_ready_seconds` 是模型交付文字的时间，当前同步实现不代表此时用户已经能在页面读取。耗时比较需要同模型、同配置、同源文字；报告中位数、最大值、失败等待和 fallback，不能只保留成功样本。批次汇总按入口动作聚合，跨案例平均值不能证明单个意图延迟。

本集外部工具为合成资料，工具耗时只说明调度路径，**不能用于评估真实天气/地图服务速度**。真实外部工具测量继续使用 `tests/performance/measure_workflow_latency.py`；它使用不同日期与夹具，其成绩单独保存。浏览器渲染和轮询等待不在这里计时。

输出包括：

- `metadata.json`、`dataset.json`、`source-manifest.json`、`source/`：有效模型环境配置（无密钥）、数据快照、源码HEAD/工作区及源码指纹。`.env` 和备份、业务缓存不进入源码快照。
- `<case>/attempt-N/evidence/result.json`、trace txt、`execution.log`：源输入、回复、工具、需求/行程前后快照、任务状态、展示与hash、机器断言及计时。独立 display 用例只做转换，不改 Store。
- `batch.json`、`summary.json`：执行状态、客观断言与耗时；不自动给自然语言方案打质量通过。
- `review.json`：从评审模板生成的人工评审表，每次尝试独立记录。
- `system-case-mapping.json`、`system-results.xml`、`system.log`：系统案例到 pytest 的映射与实际结果。

只有内存 Store 或测试自己的 SQLite；不向8000业务服务提交测试请求。执行前后只读业务快照比对；若有其他人在此期间改业务数据，标记变化并调查来源，不能据此直接断定执行器写了业务库。

## 判定

真实模型结果默认 `executed_pending_review`。客观断言通过、HTTP200、任务 succeeded、展示 ready 都不能代替人工评审。逐项检查 `checks` 与 `forbidden`；任何禁项成立即失败，不能用平均质量分冲抵 P0。

评审等级为 `pass` / `fail` / `ambiguous` / `environment_blocked` / `not_covered`；未评审保留 `pending_review`。注入故障未命中记 `not_covered`，不要算成功覆盖。已知缺陷仍使用完整验收标准，不因当前模型做不到而放宽。

质量维度为约束遵守、可执行性、证据可靠性、修改忠实度、展示忠实度、表达完整性，各0—2分，不适用为null。语义检查需要拿实际 source_text 与 display 逐项比对，不用关键词命中代替判断。0元、模糊时间、估算缓冲、报价条件、过敏以及原文结尾的信息尤其不能漏。

受控系统回归单独报告，不计入模型质量通过率。新目标未落地，或fixture/题面有歧义时另列，不虚构已通过。所有补测保留首轮结果。
