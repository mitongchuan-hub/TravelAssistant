# 主任务执行 Agent 实施记录

- 日期：2026-10-16
- 对应设计：[从意图分流到任务执行](agent-task-execution-design-20261016.md)
- 当前状态：**A–C 已实现并通过当前离线回归；D 的评测与策略切换基础设施已建立，真实模型评测和正式启用未执行。**
- 页面范围：本阶段不改 UI、页面排版、底栏、卡片或行程信息顺序。

## 1. 状态与启用边界

任务执行重构按设计的 A→B→C→D 推进：

- A：可信请求、能力结果、候选工作区、终止提案和提交回执协议已实现；
- B：`main` 有界工具循环、专业能力封装和共享预算已实现；
- C：跨轮会话目标、活动追问、workflow revision 和集中提交已实现；
- D：50 条合成场景、Schema 校验、离线评分、结果来源哈希和真实隔离 runner 已新增；本轮只运行离线校验和 mock 模型/冻结工具的 adapter 测试，未运行 live。

服务端策略名统一为：

```text
TRAVEL_AGENT_EXECUTION_STRATEGY=legacy|task_execution
```

默认值必须保持 `legacy`。策略在提交 execution task 时固定，同一执行中不能从新循环回退到旧循环或反向切换。A–C 确定性保护、真实模型质量验收和回滚演练通过前，不能默认启用 `task_execution`。

本文不宣称新架构已上线，不宣称具备真实语义质量，也不把 JSON 合法或 validate-only 成功当作自然语言理解通过。

## 2. 阶段 D 已新增内容

### 2.1 数据集

`tests/datasets/main_execution_v1/cases.json` 固定：

- 50 个合成场景；
- 每场景 3 次独立 repeat，共 150 次计划执行；
- `city` / `travel`、有无方案、单/多诉求；
- 条件三态、否定范围、假设与正式采纳；
- hotel 每晚 `500` 跨轮绑定、明确纠正、歧义；
- 超过最近 8 条消息的恢复；
- 取消、插问、多个未完成目标；
- 外查权限、跨 trip、伪造引用和注入式外部内容；
- 页面 `planning / revision / confirm` fast path；
- 幂等、版本冲突、预算耗尽和保守失败提交。

天气、酒店、地点、票务等结果是冻结夹具，不是现实事实。数据集说明见[主任务执行合成评测集 v1](../tests/datasets/main_execution_v1/README.md)。

### 2.2 校验与评分

`tests/evaluation/main_execution_support.py` 负责：

- 严格 JSON 读取、重复键拒绝、case/fixture/期望结构及覆盖数量检查；
- 将 fixture 与 case override 合成隔离输入；
- 按 `before_state → after_state`、required effects、forbidden effects 和必要回复事实评分；
- 不要求唯一固定 trace；trace 只用于失败诊断；
- 枚举全部失败，不只返回首个错误；
- 汇总模型/工具/外部调用、输入/输出/总 token 及端到端 p50/p95；
- 计算当前工作树源码、prompt、数据集和脱敏配置 SHA-256；
- 创建唯一 run 目录，并以独占写入避免覆盖。

实际执行结果必须包含终止状态、提交前后状态、可观察效果、reply facts、trace、usage 与 latency。离线评分要求选中 case 的全部 repeat，缺失、重复或多余结果均失败。

### 2.3 Runner 默认安全行为

`tests/evaluation/run_main_execution.py` 默认等同 `--validate-only`：

```powershell
python tests/evaluation/run_main_execution.py --validate-only
```

该路径只使用标准库和评测支持模块：

- 不导入应用 runtime/store；
- 不调用 `load_dotenv`，不读取 `.env`；
- 不初始化模块级默认 Store；
- 不访问生产 SQLite；
- 不启动展示 worker；
- 不调用模型、地图、天气或其他网络服务。

输出默认写到 `.cache/evaluations/main_execution/<UTC时间>-validate-only-<随机后缀>/`。每次运行创建新目录，不覆盖旧报告。

已有隔离结果可用 `--score-results` 离线评分，同样不导入业务运行时。

## 3. 真实隔离 runner 对接合同

真实入口冻结为：

```python
app.agent_client.generate_task_proposal(
    request_context,
    execution_context,
    cancelled=None,
) -> TerminalProposal
```

`execution_context` 使用 `app.task_runtime.TaskExecutionContext`，携带 trip、messages、idea_cards、plan、metrics、current_date、scope、target_item_id 和 display_target；entry 来自可信 request context。Store 侧由 `capture_request_context(...)` 建立不可变请求，再由 `commit_turn(context, proposal)` 统一提交。

live adapter 已按以下边界实现：

1. 每 case/repeat 创建显式 `DemoStore(db_path=<临时目录>/case.sqlite3, persistence_enabled=True, display_worker_enabled=False)`；不读取模块级默认 Store。
2. 用合成 fixture 替换 trips/messages/plans/idea_cards/tasks/display_jobs；不读取真实业务数据。
3. 固定 `task_execution` 策略；不在同轮回退 legacy。
4. 主 Agent 使用真实 `TaskExecutionRuntime` 模型循环；规划、需求、查询和修改调用真实 `TravelAgentRuntime.run_capability` 专业循环。天气、地图、餐饮和路线只使用冻结 `unknown/unavailable` tool adapter，不连接网络。
5. proposal 经临时 Store 的真实 `commit_turn`；评分 effects 来自提交前后状态、CommitReceipt、proposal evidence 和实际冻结工具观察，而非只看模型 trace。
6. `TurnBudget` 在单轮强制模型请求尝试次数；runner 的全局 ledger 在 case 间扣减。`max-total-tokens` 是 provider usage 返回后的累计停止阈值，不是严格费用硬上限：最后一个已发请求可能越过阈值。
7. 记录实际 input/output/total token、模型/工具/外部调用和端到端 latency；usage 缺失不能当作 0 成功。
8. import 前阻止 dotenv，设置 `TRAVEL_PERSISTENCE=0` 和 `TRAVEL_DISPLAY_ENABLED=0`，并把任何兜底数据库路径指向临时目录；导入若创建该文件即失败。
9. `app/turn_commit.py` 先在私有 staging 聚合中应用需求、行程、消息、workflow、execution task 和 display job，`save_snapshot` 成功后才发布到内存；失败不暴露半提交。评测走同一提交路径，不运行展示 worker。

离线 mock 模型集成测试已覆盖 `main → research capability → 冻结 query_weather → commit_turn`，证明 adapter 不以预置最终结果冒充成功。真实模型 live 未运行；运行时任何 fixture/协议适配错误都作为 `live_error` 失败并保留已产生的结果，不计作通过。

## 4. Live 门禁

真实模型不会由默认命令触发。调用者必须同时显式提供：

```powershell
python tests/evaluation/run_main_execution.py --live `
  --max-model-calls <正整数> `
  --max-total-tokens <正整数>
```

只给 `--live`、只给一个上限、或在非 live 模式提供预算参数都会直接失败。模型请求次数是严格尝试上限；总 token 是累计 usage 停止阈值，批准预算时必须明确最后一个请求可能超出阈值。参数齐全后如 fixture、proposal、专业能力、commit 或 usage 任一环节失败，runner 返回非零状态并报告 `live_error`；不能生成空壳“通过”结果。

本次实施**没有运行 live**，没有网络调用，没有模型费用，也没有真实语义验收数据。

## 5. 报告与可追溯性

每个 run 保存 `report.json`；评分或 live 另保存 JSONL 明细。报告包含：

- 全部失败（case、repeat、每项失败原因）；
- 调用数和 token 合计；
- p50/p95 端到端时延；
- 源码/prompt/数据集/脱敏配置哈希及源码、prompt manifest；
- 模式、case 筛选、repeat、策略与预算；
- `semantic_acceptance_performed` 和 live 错误状态。

配置脱敏按 credential、secret、password、authorization、cookie、API key、auth token 字段名过滤。报告不读取 `.env`，不复制凭据值、业务数据库或真实用户数据。源码哈希基于当前工作树文件，不以 `HEAD` 代替含未提交修改的实际来源。

## 6. 验证与剩余工作

2026-10-16 本轮最终验证（显式 `E:\anaconda\envs\TravelAssistant\python.exe`）：

- 当前可收集的离线回归 **134 passed in 6.14s**，其中旧需求角色36项继续作为legacy基线；用户已删除的历史测试没有恢复，不计作历史全量通过。
- Store完整接线覆盖travel同轮需求后规划、city纯需求、页面生成、精确文字修改、酒店短答500只用于当前查询、显式采用取消后查询超时仍保留取消。
- 单次最终提交、持久化失败不发布半状态、workflow-only冲突、未选任务候选拒绝、确认、清空、回滚挂起、旧展示job拒绝、条件写入和伪造消息引用均有离线覆盖。
- Python语法检查及`git diff --check`通过；Git仅提示现有LF→CRLF转换警告。
- 50场景评测集`--validate-only`通过，计划150次，不代表执行了150次真实模型任务；定版代码指纹与校验报告位于 `.cache/evaluations/main_execution/20261016T102838.040699Z-validate-only-235ed6a8be/report.json`。
- 未读取实际业务数据库或`.env`，未运行live模型/地图/浏览器，未重启服务、部署或提交Git；代码中的schema6迁移不表示运行中数据库已迁移。

离线专项测试 `tests/automated/test_task_execution_evaluation.py` 覆盖：

- 50×3 和关键类别；
- 聚合结构错误；
- 非唯一 trace 的状态/effect 评分；
- 全失败输出、调用/token 与 p50/p95；
- repeat 缺失拒绝；
- provenance 哈希与凭据脱敏；
- 唯一目录不覆盖；
- 默认 validate-only 不导入业务 Store；
- live 双预算门禁和全局预算失败关闭；
- mock 模型驱动真实 main、真实 research capability、冻结外部工具和真实临时 Store 提交。

仍需后续验收：

- 无网络冻结工具下的真实模型全量 50×3 执行；
- 各类 effects/reply facts 的人工语义复核与必要映射校准；
- 经单独批准后的少量真实工具连接测试；
- legacy 对照、质量/误写/时延发布门槛冻结与回滚演练。

已知条件边界：天气冻结/真实工具合同提供降水概率等结构化字段，数值阈值和日期/城市适用范围可以程序校验；任意自然语言或主观条件仍不能由字段存在自证。条件语义或阈值不明确时必须保守澄清，`unknown` 不能当作 `not_satisfied`，也不能授权写入。

在这些工作完成前，validate-only 只说明**数据和评分基础设施自洽**，不说明新主 Agent 会正确理解条件、否定、指代、取消或跨轮短回答。
