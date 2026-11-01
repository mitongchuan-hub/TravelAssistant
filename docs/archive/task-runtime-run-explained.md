# 主 Agent 的 run() 逐块解读

> **历史学习文档：对应单 Agent 替换前的主/子能力框架。** 2026-10-28 已原位替换为单 Agent CRUD；下文的 capability、finish_turn、模型 Todo 和隐藏复核不再是生产入口。
> 原对应源码：旧版 `app/agents/task_runtime.py::TaskExecutionRuntime.run()` 第 172–279 行，可从 Git 历史追溯。新设计与本轮实施边界见 [单 Agent 方案](single-agent-crud-design.md)，不要按本文旧调用路径定位新代码。

## 1. 先理解它在整个项目中的位置

`run()` 是主 Agent 的调度循环，负责反复完成：

```text
把当前信息交给模型
        ↓
模型选择下一步工具
        ↓
Python 校验参数并执行工具
        ↓
把工具结果放回对话
        ↓
模型根据结果继续决策
        ↓
结束本轮，返回 TerminalProposal（终止提案）
```

**模型负责选择，Python 负责执行。** 模型不会直接调用 Python 方法，也不直接写数据库。

主 Agent 的完整工具集合定义在 `app/agents/agent_roles.py::EXECUTION_TOOLS`。正常步骤可提供以下 11 个工具；收尾步骤只提供 `ask_user` 与 `finish_turn`。四个专业能力（需求、查询、规划、修改）内部还有自己的模型/工具循环。

```text
run()：主调度循环
├── read_context：读取当前旅行上下文
├── manage_task：管理跨轮会话任务、采用业务候选和绑定任务效果
├── manage_todos：维护本轮多事项清单
├── prepare_requirements：进入需求能力循环，整理、补充或修改正式需求候选
├── research：进入查询能力循环，返回查询依据
├── draft_itinerary：进入规划能力循环，生成完整行程候选
├── revise_itinerary：进入修改能力循环，生成行程修改候选
├── restyle_itinerary：生成网页风格调整候选，不修改行程正文
├── confirm_itinerary：生成当前行程的确认候选
├── ask_user：以关键追问形式结束，可同时采用独立候选
└── finish_turn：显式选择采用引用并结构化结束本轮
```

### “补充需求”到底由谁做？

需要区分三个动作：

| 动作 | 主 Agent 使用的工具 | 作用 |
|---|---|---|
| 查看当前已知需求与缺项 | `read_context(section='requirements')` | 读取信息，不改需求 |
| 请用户补充缺少的信息 | `ask_user` | 提出问题并等待回答，不自动把假设写入正式需求 |
| 整理用户明确提供的新需求 | `prepare_requirements` | 调用需求专业能力，依据当前消息形成经过本地校验的需求候选 |

需求专业能力内部还可使用 `update_requirements`、`check_requirements` 等工具。它们不是主 Agent 直接使用的同一层工具；主 Agent 通过 `prepare_requirements` 委派该工作。

例如，用户说“改成两天，不吃辣，按这个生成路线”，主循环可能这样走（这是示例，不是固定调用脚本）：

```text
主模型选择 prepare_requirements，引用本轮 message_ref
        ↓
需求能力读取当前需求，整理用户明确提出的变更
        ↓
在隔离快照中校验、形成需求候选，返回 artifact_ref
        ↓
主模型选择 draft_itinerary，并传入这个 requirements_ref
        ↓
运行器先把有效需求候选应用到专业能力的隔离快照
        ↓
规划能力按照更新后的需求生成行程候选
        ↓
主模型 finish_turn，明确采用需求和行程候选
        ↓
提交层再次校验后，将需求与行程原子保存
```

因此，`run()` 中没有专门写一个 `if name == 'prepare_requirements'`，并不代表没有需求工具。它统一调用 `_execute_call(call)`，后者通过以下公共分支分发四种专业能力：

```python
if name in _MAIN_CAPABILITIES:
    # 中间会检查 message_ref、task_ref 和追问绑定等。
    result = self._run_capability(name, args)
    return self._result_output(result), None
```

`_MAIN_CAPABILITIES` 包含 `prepare_requirements`、`research`、`draft_itinerary`、`revise_itinerary`。也就是说，“补充需求”是主循环能选择的一项工作，但具体提取与校验由需求能力完成。

特别注意：需求能力返回候选，不等于已保存；活动追问的回答也只能按其 `answer_target` 授权用途使用，不能自动扩展成对所有正式需求字段的写入授权。

### 返回 TerminalProposal 是什么意思？

```python
def run(self) -> TerminalProposal:
```

- `self`：当前任务运行器对象，里面保存模型客户端、上下文、预算、工作区等。
- `-> TerminalProposal`：返回值的类型标注，表示它预计返回一个终止提案；类型标注本身不负责运行时验证。
- 终止提案包含本轮答复、选择采用的候选、工作流状态或追问，以及失败原因等。
- **返回提案不等于已经保存。** 后续存储提交层还要校验并原子写入。

```text
模型产生工具调用
    ↓
运行器注册候选
    ↓
明确选择采用哪些候选
    ↓
工作区封装 TerminalProposal
    ↓
存储层检查权限、版本和依赖
    ↓
原子提交到业务状态与数据库
```

## 2. 阅读前认识几个关键变量

| 变量 | 含义 |
|---|---|
| `self.execution_context` | 本次执行使用的应用数据快照和入口信息 |
| `self.config` | 主 Agent 的提示词、可用工具等配置 |
| `messages` | 持续发给模型的消息列表，包含上下文、模型回复、工具结果和纠错提示 |
| `self.budget` | 整轮共享的模型/工具/外部查询次数及时间预算 |
| `self.workspace` | 本轮候选工作区，负责登记、检查、选择和封装候选，不直接写数据库 |
| `self.todos` | 本轮服务器维护的清单，不等于跨轮会话任务 |
| `self.workflow` | 跨轮会话任务、当前追问等工作流状态 |
| `self.max_steps` | 主循环最多运行多少步；字段默认值为 12 |
| `self._corrections` | 已累计的格式/协议纠错次数 |
| `self.max_corrections` | 允许的纠错上限；字段默认值为 3 |
| `self.finish_seconds_reserve` | 非最后一步开始前检查的剩余时间门槛；默认 5 秒 |

**主循环步数与整轮模型调用次数不是一回事。** 主循环一次调用规划能力，规划能力内部可能又调用多次模型，这些调用也会消耗共享预算。只读答复复核也可能额外调用一次模型。

## 3. 代码块一：计时，并判断是否走快捷入口

```python
self._started = monotonic()
if self.execution_context.entry == 'confirm':
    return self._confirm_fast_path()
if self.execution_context.entry in {'planning', 'revision'}:
    return self._capability_fast_path()
```

### 在做什么？

1. `monotonic()` 记录本次执行开始时刻，后面用它计算耗时。它适合计算时间间隔，不是日历日期。
2. 如果入口是 `confirm`，直接处理确认，不需要让主模型再判断该调用什么工具。
3. 如果入口已经明确是 `planning` 或 `revision`，直接运行对应专业能力，不先经过主模型调度。
4. 其他入口才进入下面的主 Agent Loop。

### 为什么这样做？

用户已经点击了“确认”或明确提交规划/修改任务时，系统知道目标，没必要再花一次主模型调用猜测意图。

**注意：** “快捷”不一定代表完全不调用模型。确认快捷路径可以跳过模型；规划/修改快捷路径仍会运行专业能力的模型循环。

另外，这两处提前 `return` 在下方 `try` 之外，因此不会执行本函数最后那个 `finally`。快捷路径有自己的处理流程，不能把下方计时日志理解为覆盖所有入口。

## 4. 代码块二：构造模型最初看到的消息

```python
messages = [
    {'role': 'system', 'content': self.config.prompt.partition('\n# 只读回复复核\n')[0].strip()},
    {'role': 'user', 'content': json.dumps(self._initial_context(), ensure_ascii=False, default=str)},
]
```

### 第一条：系统规则

```python
self.config.prompt.partition('\n# 只读回复复核\n')[0].strip()
```

- `partition()` 把提示词按“只读回复复核”标题分成前后两部分。
- `[0]` 只取该标题之前的主循环规则。
- 后面的复核规则留给专门的只读回复复核调用，不混进主循环。
- 如果没有找到分隔标题，`[0]` 就是整个提示词。

### 第二条：当前上下文

```python
json.dumps(self._initial_context(), ensure_ascii=False, default=str)
```

`_initial_context()` 整理当前消息、日期环境、近期对话、需求、想法墙、会话任务和当前方案等信息。

- `json.dumps()`：把字典转换成模型可读取的 JSON 文本。
- `ensure_ascii=False`：中文直接保留，不转成 `\uXXXX`。
- `default=str`：遇到不能直接 JSON 序列化的对象时，使用其字符串表示。

这里的 `role='user'` 是模型接口的消息角色，并不意味着其中所有历史数据都获得了本轮用户授权。授权边界仍由工具和运行时检查。

**作用：给模型一份受限的决策视图，而不是直接暴露存储对象。**

## 5. 代码块三：开始有次数上限的循环

```python
try:
    for step in range(self.max_steps):
```

假设 `max_steps=12`，`step` 依次为 `0, 1, ..., 11`。

每步通常是：调用一次主模型，处理它返回的正文或工具请求，再决定继续还是结束。

- `continue`：结束当前这一步，进入下一步；也会占用循环步数。
- `return`：立即退出整个 `run()`，把提案交还调用方。
- `try`：捕获循环内的预算、超时或其他异常，转入安全收尾。

注意：前面的快捷入口和初始 `messages` 构造不在这个 `try` 范围内，不能认为这里会捕获整段函数所有位置的异常。

## 6. 代码块四：检查预算，并判断是否进入收尾模式

```python
if step < self.max_steps - 1:
    self.budget.reserve(models=1, seconds=self.finish_seconds_reserve)
else:
    self.budget.reserve(models=1)
finishing = (step == self.max_steps - 1
             or self.budget.max_models - self.budget.model_calls == 1)
```

### 6.1 reserve() 只是预检，不是扣费或锁定额度

非最后一步会检查：

- 是否还能发起至少一次模型调用；
- 此刻剩余时间是否**大于** `finish_seconds_reserve`；
- 任务是否已取消/超时，以及当前其他调用预算是否已超限。

其关键实现位于 `app/agents/agent_runtime.py::TurnBudget.reserve`：

```python
remaining = self.remaining()
if self.model_calls + models > self.max_models:
    raise AgentBudgetError('本次模型调用次数已达上限，原行程已保留。')
# 中间还会检查工具和外部调用额度。
if remaining <= seconds:
    raise AgentBudgetError('本次任务剩余时间不足以安全执行并收尾。')
```

这里传 `models=1` 表示“检查能不能再调用一次”，**不是当前调用之外再专门锁住一次备用调用**。

真实调用计数在模型请求层调用 `budget.model()` 时增加；`reserve()` 本身不修改计数。

### 6.2 为什么最后一步不要求额外超过 5 秒？

最后一步本身就是收尾机会，所以只检查能否继续调用一次模型和是否仍有剩余时间，不再要求留出那段额外时间门槛。

### 6.3 finishing 的两个触发条件

| 条件 | 含义 |
|---|---|
| `step == self.max_steps - 1` | 主循环已经到最后一步 |
| `max_models - model_calls == 1` | 整轮共享预算只剩一次模型调用 |

任一成立，就进入收尾模式。

例如：主循环才走到第 4 步，但子能力已经消耗了大量模型调用，整轮只剩 1 次，仍然必须收尾。

### 6.4 容易误解的地方

- **不会真的锁住 5 秒。** 这里只检查当前剩余时间，接下来模型仍可能耗掉它。
- **时间快耗尽不会自动令 finishing=True。** 非最后一步若只剩 3 秒、门槛为 5 秒，`reserve()` 会直接抛异常，进入程序化失败收尾，不再调用模型总结。
- 若模型额度已经是 0，`reserve(models=1)` 会先失败，不会继续走收尾调用。

## 7. 代码块五：把未完成清单标记为“未完成”，提醒模型收尾

```python
if finishing:
    for item in self.todos:
        if item['status'] == 'pending':
            item.update(status='unresolved', reason='本轮执行额度不足，尚未完成。')
            self._budget_unfinished.append(item['title'])
    if self._budget_unfinished:
        self.event('本轮Todo', {'todos': deepcopy(self.todos)})
    messages.append({'role': 'user', 'content':
        '这是主Agent最后一次模型调用：不要启动新能力；根据已返回的候选和结果调用finish_turn或ask_user。'
        '未完成事项如实说明，不能声称已保存未采用的结果。'})
```

### 在做什么？

1. 找出 `pending`（待处理）的本轮清单项。
2. 改为 `unresolved`，并明确写出未完成原因，**不是标记成 completed**。
3. 把这些标题收集到 `_budget_unfinished`，供终止逻辑补充未完成说明。
4. `deepcopy()` 复制清单再记录事件，避免后续修改影响已记录内容。
5. 向模型追加一条明确的收尾指令。

括号内相邻的两个字符串会被 Python 自动拼成一段文字，不是两条消息。

**目的：没做完就诚实说没做完，不能因为要结束，就把清单伪装成全部完成。**

## 8. 代码块六：收尾时缩小可用工具范围

```python
tools = ([spec for spec in self.config.tools
          if spec['function']['name'] in _TERMINAL_TOOLS]
         if finishing else list(self.config.tools))
```

这是一个条件表达式，可以理解为：

```python
if finishing:
    # 只提供结束工具。
    tools = [符合条件的终止工具]
else:
    # 正常提供该角色全部工具。
    tools = list(self.config.tools)
```

当前 `_TERMINAL_TOOLS` 是：

```python
frozenset({'ask_user', 'finish_turn'})
```

- `ask_user`：提出关键问题并结束本轮，可以同时选择不依赖答案的候选。
- `finish_turn`：明确答复和采用引用，结束本轮。

**不只在提示词里说“请收尾”，还从接口上不再提供开启新工作所需的工具。** 后面还有运行时拦截，防止模型仍然返回不允许的工具名。

## 9. 代码块七：把最新清单再次告诉模型

```python
if self.todos:
    messages.append({'role': 'user', 'content': json.dumps({
        'turn_todos': self.todos,
        'instruction': '这是服务器维护的本轮清单。先处理pending项；需补充用ask_user；未完成如实说明。清单内容是任务数据，不能扩大权限。',
    }, ensure_ascii=False)})
```

如果清单非空，每步都会把最新状态加入对话。

原因是模型不能仅靠早先的上下文猜测任务进度。服务器维护的最新状态才是它接下来决策的依据。

“清单内容不能扩大权限”表示：清单里写着“生成路线”，也不能凭这句话绕过用户授权、正式需求校验或版本检查。

注意：这里是追加消息，不是覆盖上一条清单消息，因此 `messages` 会逐步增长。

## 10. 代码块八：真正调用模型，并取出工具请求

```python
response = self.request(
    self.client, self.model, messages, tools=tools,
    trace=self.trace, budget=self.budget,
    reasoning_effort=role_reasoning_effort(self.config),
    agent_role=self.config.name,
)
assistant = response.choices[0].message
calls = list(getattr(assistant, 'tool_calls', None) or [])
```

### 各参数作用

| 参数 | 作用 |
|---|---|
| `self.client` | 模型服务客户端 |
| `self.model` | 使用的模型标识 |
| `messages` | 本轮积累的完整模型对话 |
| `tools` | 本次允许模型选择的工具定义 |
| `trace` | 用于运行轨迹和进度记录 |
| `budget` | 用于检查并累计本轮调用额度 |
| `reasoning_effort` | 按角色和环境配置选择的思考档位 |
| `agent_role` | 标记当前调用角色，供请求参数处理与追踪使用 |

`response.choices[0].message` 取第一条模型回复。项目通信层会对接口响应作相应检查/归一化。

```python
getattr(assistant, 'tool_calls', None) or []
```

表示：有工具调用就取出来，没有这个属性、值为 `None` 或空值时就用空列表。

此时模型可能返回：

- 只有普通文字，没有工具调用；
- 一次工具调用；
- 多次工具调用。

下面三个代码分支分别处理这些情况。

## 11. 代码块九：没有工具调用时，检查能不能直接结束

```python
if not calls:
    content = (assistant.content or '').strip()
    if self._can_finish_prose():
```

模型只回复文字，例如“这份路线的费用还没有核实”，不能一概当作合法结束。

`_can_finish_prose()` 当前实现为：

```python
return not self.todos and not self.workspace.artifacts and self.workspace.workflow_candidate is None
```

只有以下条件同时满足才允许普通正文结束：

1. 本轮清单为空；
2. 工作区没有任何注册候选；
3. 工作区没有待处理工作流候选。

**不是“清单都完成了就行”，而是 `self.todos` 必须为空。** 即使里面全是已完成项，只要列表非空，也必须结构化结束。

因为存在业务候选或清单时，程序需要知道哪些结果被采用、哪些事情没完成，不能只凭一句“好了”推断。

## 12. 代码块十：普通文字结束前，检查旧追问是否已处置

```python
unsettled = self._unsettled_base_question(self.workflow)
if unsettled:
    messages.extend([self._assistant_message(assistant, ()), {
        'role': 'user',
        'content': (
            f"活动追问{unsettled['id']}尚未处置：若本轮消息是它的回答，"
            "请通过task_ref与answer_question_ref绑定消费；"
            "若用户已转向其他事项，先用manage_task取消或挂起对应任务。"
            "处置后再调用finish_turn或ask_user。"),
    }])
    self._corrections += 1
    if self._corrections > self.max_corrections:
        return self.workspace.fail_closed('主Agent未按结构化合同结束本轮。')
    continue
```

### 什么叫“旧追问未处置”？

例如上一轮助手问“要公交还是自驾？”，这轮用户回答“公交”。

程序不能只让模型说“好的”，却仍把上一轮问题保留成“等待回答”。需要通过任务引用和问题引用明确绑定：这条消息回答了哪个问题。

如果用户转而说“不规划了”，也要明确取消或挂起相关任务，而不是悄悄忘掉旧问题。

### 这块怎么处理？

- `messages.extend()`：一次加入模型原回复和纠错提示。
- `_assistant_message(assistant, ())`：把本次纯文字回复转成可继续发送的 assistant 消息；空元组表示没有工具调用。
- `_corrections += 1`：累计纠错次数。
- 超过上限：程序直接失败收尾。
- 未超限：`continue` 让模型下一步修正。

默认上限是 3，判断条件是 `>`，所以累计到第 4 次才在该分支终止；不是每一步重置次数。

## 13. 代码块十一：允许只读正文结束时，复核后生成提案

```python
return self.workspace.finish(
    self._review_readonly_answer(content),
    workflow_state=self.workflow
)
```

源码把它写在一行，这里换行便于阅读。

执行顺序是：

1. 先执行 `_review_readonly_answer(content)`；
2. 再把它返回的文字交给 `workspace.finish()`；
3. 返回最终提案。

### 复核一定会调用第二次模型吗？

不一定。当前只有答复非空、没有候选、存在想法卡片、且已有当前方案时，才会调用模型核对答复与已保存行程/想法的状态是否一致；其他情况直接返回原文字。

复核也消耗本轮共享模型预算。如果已经没有调用额度，复核会抛预算异常，转到下方失败收尾。

**因此 `finishing` 并不能保证所有返回路径都只需一次模型请求。** 不仅这里的普通正文分支会复核，`_terminal()` 处理 `finish_turn` 时也会调用 `_review_readonly_answer()`；满足上述条件时，同样可能额外调用模型。`ask_user` 走 `_ask()`，不经过这项只读答复复核。

所以，“最后只剩一次模型调用，改用 finish_turn 就一定够用”不是当前代码的保证。若 finish_turn 的回复触发复核、但模型额度已耗尽，预算异常仍会沿调用栈进入 `run()` 的失败收尾。这里是在解释现有边界，不代表已经修复这一行为。

复核不是重新验收地点、费用或现实可执行性。

`workspace.finish()` 只是校验并封装提案，不是写数据库。

## 14. 代码块十二：有候选或清单时，拒绝用普通文字结束

```python
messages.extend([self._assistant_message(assistant, ()), {
    'role': 'user',
    'content': '本轮含写入候选或未完成事项，必须调用ask_user或finish_turn结构化结束；不要输出普通正文。',
}])
self._corrections += 1
if self._corrections > self.max_corrections:
    return self.workspace.fail_closed('主Agent未按结构化合同结束本轮。')
continue
```

这块与上面的“旧追问”纠错类似，只是原因不同。

例如规划能力已生成候选 `artifact-123`，模型回复“路线已生成”，但没有指定采用它。程序不能据此保存候选，必须让模型调用结束工具并明确 `adopt_refs`。

作用是防止：**嘴上说已完成，实际没有选择要提交的结果。**

## 15. 代码块十三：一次返回多个主工具调用时，全部拒绝

```python
if len(calls) != 1:
    messages.append(self._assistant_message(assistant, calls))
    for call in calls:
        messages.append(self._tool_message(call, {
            'status': 'unavailable',
            'reason': '一次响应只能选择一个能力；本批调用均未执行。',
        }))
    self._corrections += 1
    if self._corrections > self.max_corrections:
        return self.workspace.fail_closed('主Agent重复返回多个能力调用。')
    continue
```

前面已处理 `calls` 为空的情况，所以来到这里，`len(calls) != 1` 实际就是有多次调用。

为什么主层一次只执行一个？例如先准备需求，再用该需求规划，两者有依赖。若一次同时返回，容易混淆执行顺序和候选依据。

这里不是执行第一个、忽略其他，而是：

1. 保存模型请求的全部调用；
2. 给每个调用都返回一条“未执行”的工具结果，保持协议配对；
3. 让模型重新选择一个工具。

这些被拒绝的调用没有执行，也没有走后面的 `budget.tools(1)`，因此不会在这里扣工具次数；但前面的模型请求和当前循环步数已经消耗。

**限制属于主 Agent Loop。** 不代表专业能力不能批量查询；专业能力循环另有最多四个独立只读查询的批处理规则。

## 16. 代码块十四：执行唯一工具，或拦截收尾期间的新工作

```python
call = calls[0]
messages.append(self._assistant_message(assistant, calls))
if finishing and call.function.name not in _TERMINAL_TOOLS:
    output, terminal = ({'status': 'unavailable',
        'reason': '本轮只剩收尾额度，未执行新能力。'}, None)
else:
    self.budget.tools(1)
    output, terminal = self._execute_call(call)
```

### 为什么先把 assistant 消息放进 messages？

因为模型接口要求工具结果对应前面某条 assistant 工具调用。后续返回 tool 消息时，要能通过 `tool_call_id` 找到它。

### 收尾拦截

即使这次只给模型提供终止工具，模型仍可能返回别的名字。运行时不能只信模型，所以再次检查。

如果正在收尾且工具不是 `finish_turn` / `ask_user`：

- 不执行；
- 不在这里扣工具次数；
- 返回一条 `unavailable` 结果。

### 真正执行

```python
self.budget.tools(1)
output, terminal = self._execute_call(call)
```

- `budget.tools(1)` 检查并增加一次主工具调用计数。
- `_execute_call()` 解析 JSON、验证 Schema、检查引用/权限，然后分发到具体方法。

例如：

```python
if name == 'read_context':
    return self._read_context(args), None
```

或者：

```python
if name in _TERMINAL_TOOLS:
    return {}, self._terminal(name, args)
```

**当前实现先扣主工具计数，再进入 `_execute_call()` 验证参数。** 因此 Schema 错误的单个主工具调用也会消耗这一次工具额度，不能误读为“只有成功执行的工具才计数”。

## 17. 代码块十五：有终止提案就返回，否则反馈工具结果继续循环

```python
if terminal is not None:
    return terminal
messages.append(self._tool_message(call, output))
```

`_execute_call()` 返回两个值：

| 返回值 | 含义 |
|---|---|
| `output` | 工具结果，准备给模型继续阅读 |
| `terminal` | 终止提案；普通工具一般为 `None` |

两种走向：

```text
read_context / research / draft_itinerary 等普通工具
    ↓
output 有结果，terminal=None
    ↓
追加 tool 消息
    ↓
进入下一步主循环

finish_turn / ask_user 成功结束
    ↓
terminal 是 TerminalProposal
    ↓
立即 return，主循环结束
```

`_tool_message()` 会把工具结果序列化，并附上该调用的 ID，让模型知道它对应哪个请求。

## 18. 代码块十六：循环步数用完仍没结束，程序失败收尾

```python
return self.workspace.fail_closed('主Agent达到本轮工作步数上限。')
```

这行在 `for` 循环外，但仍在 `try` 内。

只有前面所有步骤都没有返回终止提案，才会到这里。

例如最后一次模型调用仍执意调用查询工具，被拦截后也没有可用的新一步了，就由程序直接结束，不再继续请求模型。

### fail_closed() 是不是“全部丢掉”？

不是。

`app/workflow/turn_workspace.py::fail_closed()` 会保守选择：

- 已经明确采用、进入 ready-to-commit 集合的候选；
- 必须是 independent（独立效果）；
- 条件必须 satisfied；
- 所有依赖也必须包含在安全保留集合中。

只是注册、没有采用的候选不会保留；工作流候选也必须已经采用才会随失败提案保留。

它返回 `failed=True` 的提案，后续仍由提交层决定哪些安全效果能够提交。**失败不代表已完成的独立需求一定丢失，也不代表旧行程会被清空。**

## 19. 代码块十七：预算和超时异常，直接使用明确原因收尾

```python
except (AgentBudgetError, TimeoutError) as exc:
    return self.workspace.fail_closed(str(exc))
```

会捕获例如：

- 模型调用次数用完；
- 工具/外部调用额度超限；
- 剩余时间不足；
- 新消息替代当前任务导致的取消；
- 抛到这一层的 `TimeoutError`。

把异常消息作为失败原因，不再花一次模型调用解释。

不是所有服务端超时异常都一定属于 Python 的 `TimeoutError`；其他异常如果没有被内层处理，会进入下面的通用异常分支。

## 20. 代码块十八：其他异常，记录诊断但返回统一失败说明

```python
except Exception as exc:
    self.event('Task Runtime异常', {
        'error_type': type(exc).__name__,
        'reason': str(exc)[:500]
    })
    return self.workspace.fail_closed('本次任务执行未完成，当前需求和行程已保留。')
```

源码的事件字典写在一行，这里换行展示。

分成两层处理：

1. **诊断层**：记录异常类型和最多 500 字符的原因，便于定位问题。
2. **提案层**：提供稳定的中文失败说明，不直接把内部错误当作用户回复。

同样使用 `fail_closed()`，不会因为一次运行器异常就覆盖或清空之前的方案。

## 21. 代码块十九：无论成功还是失败，记录本轮消耗和耗时

```python
finally:
    self.event('Task Runtime预算', self.usage)
    self.event('Task Runtime耗时', {
        'seconds': round(monotonic() - self._started, 3)
    })
```

进入这个 `try` 后，无论：

- 循环中 `return` 成功提案；
- 返回失败提案；
- 发生并捕获异常；

退出时都会先执行 `finally`。

`self.usage` 返回共享预算摘要，包括模型调用、工具调用、外部查询、输入/输出 token 等。

```python
monotonic() - self._started
```

计算耗时，`round(..., 3)` 保留三位小数。

注意：这是记录事件；实际是否写入轨迹取决于是否启用了 `trace`。开头两个快捷路径位于 `try` 外，不经过这个 `finally`。

## 22. 用一个例子串起来

用户说：“生成一天路线。” 假设从普通主入口进入，且需求已经完整：

```text
① run() 记录开始时间，发现不是 confirm/planning/revision 快捷入口。

② 构建 system 规则与本轮上下文消息。

③ 第一步：检查预算，调用主模型。
   模型返回 draft_itinerary 工具请求。

④ 保存 assistant 工具请求。
   扣主工具计数，校验参数与引用，运行规划能力。
   规划能力内部自己调用模型、查地点和路线。
   最终在工作区注册 itinerary 候选，返回 artifact_ref。
   注意：此时还没保存为当前行程。

⑤ 把规划能力的结果作为 tool 消息加入 messages。

⑥ 第二步：再次调用主模型。
   模型看见候选，返回 finish_turn，并在 adopt_refs 里选择该候选。

⑦ _terminal() 校验采用集合与工作流。
   workspace.finish() 封装 TerminalProposal。

⑧ run() 返回提案，并在 finally 中记录预算和耗时。

⑨ 外部 TurnCommit 再检查版本、授权与依赖，原子保存方案和消息。
```

## 23. 最容易混淆的六件事

| 容易误解 | 实际含义 |
|---|---|
| `reserve()` 会扣调用次数 | 只预检；实际模型调用与工具调用分别在对应计数方法处扣额度 |
| 预留 5 秒意味着最后一定还剩 5 秒 | 不是保证，只是开始前检查当前剩余时间大于门槛 |
| 每一步一定只消耗一次模型调用 | 主模型一次之外，专业能力或只读复核可能额外调用模型 |
| 模型说“已完成”就会保存 | 必须有合法候选、明确采用及提交层校验 |
| `workspace.finish()` 就是写数据库 | 它只封装终止提案，真正提交在存储层 |
| `fail_closed()` 就是撤销/清空所有内容 | 它保留符合规则且已采用的独立效果；旧持久化状态不会被随意清空 |

## 24. 补充：11 个工具究竟分发到哪里

工具定义在 `agent_roles.py`，实际执行入口在 `task_runtime.py::_execute_call()`。下面是完整对应关系，避免把“主循环能用哪些工具”误读成前面示例中的几项：

| 主工具名称 | Python 处理入口 | 主要作用/产物 | 成功返回是否立即写数据库 |
|---|---|---|---|
| `read_context` | `_read_context()` | 读取需求、消息、想法、计划、证据或任务的受限视图 | 否 |
| `manage_todos` | `_manage_todos()` | 更新本轮内存清单，状态为 pending/done/needs_input/unresolved | 否 |
| `manage_task` | `_manage_task()` | 创建/完成/取消/挂起/恢复跨轮任务候选，或显式采用效果引用 | 否 |
| `prepare_requirements` | `_run_capability()` → 需求专业循环 | 依据当前消息整理需求，返回需求候选引用及缺项 | 否 |
| `research` | `_run_capability()` → 查询专业循环 | 查询问题，返回证据引用、摘要或缺项 | 否；但查询会访问外部服务 |
| `draft_itinerary` | `_run_capability()` → 规划专业循环 | 生成完整行程候选，并记录所依赖的需求候选 | 否 |
| `revise_itinerary` | `_run_capability()` → 修改专业循环 | 对当前合法计划引用及授权范围生成修改候选 | 否 |
| `confirm_itinerary` | `_confirm()` | 为用户已看到的当前计划生成确认候选 | 否 |
| `restyle_itinerary` | `_restyle()` | 生成只影响网页展示偏好的 presentation 候选 | 否；采用并提交后才触发后台网页生成 |
| `ask_user` | `_terminal()` → `_ask()` | 选择可采用候选并封装带关键追问的终止提案 | 否，返回提案 |
| `finish_turn` | `_terminal()` | 校验清单/工作流、选择采用引用、按条件复核答复并封装提案 | 否，返回提案 |

上表的“不立即写数据库”指这些主工具不直接提交旅行需求/行程等业务状态，不代表完全没有外部访问、运行日志或进度事件。`research` 等能力会进行真实外部查询；模型请求与运行记录也有自己的边界。

### 三种“结束”不要混为一谈

| 名称 | 由谁调用 | 结束什么 | 是否回到主循环 |
|---|---|---|---|
| `complete_capability` | 需求/查询/规划/修改专业模型 | 结束一个专业能力，交回候选或证据 | 通常会 |
| `finish_turn` / `ask_user` | 主模型 | 请求结束本轮主执行，交回终止提案 | 成功时不会 |
| `workspace.finish()` / `fail_closed()` | Python 代码 | 校验并封装终止提案 | 返回调用方，不写数据库 |

### manage_todos 与 manage_task 的区别

- `manage_todos` 是“这一次回复要处理的几件事”，例如整理预算、查询天气、说明路线限制；`done` 不代表相关候选已持久化。
- `manage_task` 是跨轮任务和追问生命周期，例如等待用户补日期、取消某个长期目标；通过 task artifact 与最终提案提交。
- `manage_task(operation='adopt')` 会把候选标记为 ready-to-commit，影响后续失败时可保留哪些效果，但不是当场保存。正常终止时仍通过最终选择集合明确表达提交内容。

## 25. 补充：工具调用出错后，程序实际怎么走

`run()` 不是自己解析所有工具参数，而是调用 `_execute_call()`：

```python
args = json.loads(call.function.arguments or '{}')
if not isinstance(args, dict) or name not in self._schemas:
    raise ValueError('未知主工具或参数不是对象')
Draft202012Validator(self._schemas[name]).validate(args)
```

依次检查：

1. 参数是否是合法 JSON；
2. 解析后是否为字典，工具名是否已注册；
3. Schema 中的类型、必填项、枚举与额外字段限制；
4. 对应分支的业务授权、任务引用、计划引用和条件证据。

Schema 检查不是现实事实核验，也不能代替这些业务检查。

### 错误不是一律立即终止

`_execute_call()` 捕获 `ValueError/TypeError/KeyError/ValidationError/WorkspaceError` 等预期错误后，会增加共享 `_corrections`，并按 `(工具名, 原始参数字符串, 错误文字)` 统计重复失败：

```python
if self._repeat_failures[key] > 1 or self._corrections > self.max_corrections:
    return {
        'status': 'failed', 'recoverable': False,
        'reason': '相同无效调用已重复，必须安全结束或提出澄清。',
    }, None
return {'status': 'unavailable', 'recoverable': True, 'reason': str(exc)}, None
```

- 第一次普通错误：反馈 `unavailable` 和 `recoverable=True`，让模型修正。
- 同样的错误重复，或总纠错数超限：反馈 `failed` 和 `recoverable=False`。
- **这里仍然返回 `terminal=None`，没有直接调用 fail_closed，也没有抛出终止异常。** `run()` 会把失败结果作为 tool 消息加入对话，再尝试下一步；能否继续还受步数、预算及后续分支限制。
- 与之不同，`run()` 内“纯文字违规”和“多个主工具”分支在纠错次数超限后会直接 `return fail_closed(...)`。

所以，不能笼统地说“纠错到第 4 次时所有路径都会立即退出”。不同分支的动作不同，虽然它们共用同一个累计 `_corrections`。

若是 `AgentBudgetError`、`TimeoutError` 或未被 `_execute_call()` 吞下的异常，则沿调用栈进入 `run()` 外层异常处理。

## 26. 补充：两个终止工具不只是简单 return

`run()` 看见 `_execute_call()` 返回 `terminal` 就结束，但真正的终止条件由 `_terminal()` 检查：

1. 清单还有 `pending`：拒绝结束，返回工具错误供模型修正。
2. `finish_turn` 时清单还有 `needs_input`：要求改用 `ask_user`。
3. 若因预算留下 `_budget_unfinished`，`finish_turn` 会把未完成标题追加到 reply。`ask_user` 分支没有这段自动追加；不能推断两者文案相同。
4. 逐一验证 `adopt_refs` 引用存在；必要的答案消费任务引用由服务器补入选择集合。
5. `finish_turn` 根据选择的任务候选重建工作流，检查旧追问是否处置，按条件复核只读答复，然后封装提案。
6. `ask_user` 通过 `_ask()` 创建/复用追问所属任务，把问题、答案目标和工作流一起交给工作区封装。它不是在原地等待输入；本轮已经返回，用户回答后会开启下一轮。

### 清单标成 unresolved，不一定使整轮 failed

`finishing` 会把 pending 改成 unresolved，但这不等于调用 `fail_closed()`。模型仍可能合法 `finish_turn`，以普通提案结束并在回复中说明未完成项。

因此必须分清：

- “某项工作未完成”：清单状态与答复内容；
- “本轮执行 failed”：终止提案的失败标记；
- “某个候选未采用”：最终选择集合；
- “存储提交失败”：提交层事务结果。

它们不是同一个布尔值。

## 27. 补充：prepare_requirements 的隐藏连接点

仅知道主工具清单里有需求能力还不够，还需要知道它怎样影响后续规划。

### 27.1 需求来源必须是本轮消息

`_execute_call()` 检查需求工具的 `message_ref` 必须等于服务器当前 `request_context.message_id`。不能拿旧聊天中的某句话伪装成当前授权。

`prepare_requirements` / `research` 可以通过 `task_ref` 与 `answer_question_ref` 消费一个有效追问；这种绑定只授权答案目标对应的用途，不自动授权任意正式需求变更。

### 27.2 需求候选怎样给规划使用

主模型取得需求候选引用后，传入规划或修改的 `requirements_ref`。`_run_capability()` 验证引用并将变化应用到专业能力的隔离快照，规划看到的是更新后的需求，而数据库仍是旧状态。

若这个需求候选属于当前消息、效果独立且条件已满足，运行器在启动后续能力前还会显式 `workspace.adopt(requirements_ref)`。这样后续规划失败时，该安全独立需求可能保留在失败提案中。

注意：这不是“所有需求候选一产生就自动保存”。必须满足上述条件并实际经过这条采用路径，最终还要通过提交层检查。

### 27.3 子能力预算为什么没有把父循环全部吃光

实际执行专业能力时使用 `budget.child_scope(...)`，临时降低子能力可用的模型次数上限和截止时间，为父级留出收尾额度。结束时恢复父级上限，但累计已用次数不会清零。

这与每步 `reserve()` 的简单预检不同：`child_scope()` 会临时修改子调用能看到的预算上限。即便如此，网络错误、纠错和额外复核等仍可能导致失败，不能把它理解成任何情况下都保证成功收尾。

## 28. 补充：快捷入口的完整结果分支

`confirm` 快捷入口直接创建并选择当前方案确认候选，调用 `workspace.finish()`；不需要主模型选择工具。

`planning/revision` 快捷入口在 `_capability_fast_path()` 中：

```text
运行目标规划/修改能力
        ↓
若 status=needs_requirements
    → 调用 prepare_requirements
    → 只有需求结果 ok 且存在候选引用，才带 requirements_ref 重试目标能力一次
        ↓
目标结果 ok 且存在 artifact_refs
    → 选择候选及其直接依赖
    → workspace.finish()
目标结果 needs_input
    → _ask() 封装追问
其他结果或抛出的异常
    → fail_closed()
```

- `needs_requirements` 与 `needs_input` 不同：前者表示要先整理正式需求，后者表示需要用户提供信息/作出选择。
- 快捷路径没有主模型的 `for step ...` 决策循环，但专业能力内部仍有自己的循环和共享预算。
- 这里的 `ok` 也必须带候选引用才走成功结束，不能只看状态字符串。
- 若补需求失败，代码没有把需求能力的失败结果自动替换为目标结果，而是按保留下来的目标结果继续分支；不要把上图理解成对所有子失败都有一套同样的追问回传。

## 29. 补充：能力返回状态不等于主循环已经结束

普通主循环通过 `_execute_call()` 调用专业能力后，会把结果投影为工具输出，`terminal` 仍为 `None`。以下状态本身不会自动结束普通主循环：

| 能力状态 | 含义 | 普通主循环下一步可能做什么 |
|---|---|---|
| `ok` | 能力完成了这次候选/查询交付 | 检查候选、继续其他事项，或明确采用后结束 |
| `needs_input` | 需要用户补充信息或作取舍 | 主模型选择 `ask_user`，不能把问题仅藏在工具结果里 |
| `needs_requirements` | 需要先处理正式需求 | 主模型选择 `prepare_requirements`，再决定是否重试 |
| `conflict` | 请求或依据发生冲突 | 澄清、调整目标，或如实结束 |
| `unavailable` | 能力或所需信息不可用 | 说明限制，不补造事实 |
| `failed` | 能力未完成 | 决定安全收尾，不能宣称已生成/已保存 |

`artifact_refs` 是需求、行程等候选引用，`evidence_refs` 是证据引用。证据可以用于支持判断，但不能像业务候选一样 adopt 成一次需求或行程写入。

快捷入口的 Python 分支会直接按能力状态决定 finish/ask/fail；普通主循环则把选择权交回主模型。二者不要混淆。

## 30. 补充：注册、暂存采用、最终选择，是三个阶段

```text
registered：候选已登记，能够通过引用读取
        ↓
adopted / ready-to-commit：已在工作区明确标记为可提交
        ↓
selected in terminal proposal：最终提案实际选中的集合
        ↓
committed：提交层已校验并持久化
```

**中间任一步都不能直接说成“已保存”。**

- 模型可以用 `manage_task(operation='adopt')` 显式采用效果。
- 运行器也会在特定路径自动采用需求依据，或补选已经验证的答案消费任务引用，不能说所有采用都必须由模型最后手写。
- 正常 `workspace.finish(selected_artifact_ids=...)` 的最终集合具有决定权：之前暂存采用、但不在最终集合中的候选会被移除，不会自动全部提交。
- `fail_closed()` 才会从当时 ready-to-commit 的候选里，筛选安全独立效果组成失败提案。

### 为什么 finish_turn 可能被工作区拒绝？

`workspace.adopt()` / `finish()` 还会检查：

1. 候选是否存在、是否失效，是否属于本轮授权。
2. hypothetical（假设性）不能采用；unknown 或 not_satisfied 条件不能授权写入。
3. 依赖要先采用，最终选择集合也不能遗漏依赖；例如依赖新需求的行程不能只选行程、不选需求。
4. 一次最多采用一份 itinerary；不能把未提交的新 itinerary 与 confirmation 同时采用。
5. presentation 最多一份，并且不能与新 itinerary 混合采用。
6. 证据只能引用，不能当业务写入效果采用；选中候选携带的证据引用会自动汇入提案。
7. 追问必须与最终工作流的 active_question、任务和工作区预留的助手消息 ID 对应。

这些是结构、引用和授权校验，不证明路线在现实中可执行。

## 31. 补充：completed_items 不会替你完成任务

虽然 `finish_turn` / `ask_user` 的参数定义里有 `completed_items` 和 `unresolved_items`，当前主终止实现并不会凭这两个自由文本数组自动修改本轮 Todo 或跨轮任务状态。

`_apply_item_completion()` 当前只是兼容入口，没有实际执行任务完成更新。

因此：

- 本轮清单的 pending/done/needs_input/unresolved 必须用 `manage_todos` 更新。
- 跨轮任务的完成、取消、挂起等必须通过 `manage_task` 及相应任务候选表达。
- 仅给 `finish_turn` 填 `completed_items=['查询天气']`，不能消除仍为 pending 的 Todo，也不能让相关跨轮任务自动完成。

这也是“工具参数里有这个字段”和“代码真的拿它更新状态”之间的区别。

## 32. 补充：预算还有 token 与时钟边界

- `reserve()` 不检查 token 总量限制；`budget.model()` 在开始模型请求前检查，若已达到可选 token 上限，不启动下一请求。
- `record_usage()` 在请求返回后累计输入/输出 token；若超过配置上限，会抛预算异常并拒绝消费当前结果。
- token 总量硬上限是可选配置，默认不能理解为一定启用。
- 预算截止时间在 `TurnBudget` 初始化时建立；`run()` 的 `_started` 只是本函数耗时记录的起点。两者可能不是同一时刻。
- 子能力预留模型次数和时间，但不因此自动预留主工具额度；即使最后还有模型调用次数，结束工具也可能因工具次数用尽而失败。
- 模型请求层的兼容重试、备用请求等也可能消耗模型额度，一次主循环步不等于一次底层 HTTP 请求。

## 33. 补充：安全收尾是既有策略，不是不会出错的保证

本文的“失败收尾”描述的是正常情况下的程序策略，不应理解成任何内部错误都保证返回 `TerminalProposal`：

- `workspace.finish()` 自身还会做引用、依赖、工作流等检查，可能抛异常；它不是数据库事务，也不是在所有校验完成前绝不改变内存采用状态。
- `fail_closed()` 内部仍然调用 `finish()`，也可能因为内部状态不合法而失败。
- 事件记录方法依赖 `trace.add()`；这里没有给每次事件调用再套一层兜底，如果追踪实现抛异常，也可能影响原本准备返回的结果。
- 确认快捷路径不经过 `run()` 的这个 try；规划/修改快捷路径有自己的异常处理，但不经过这里的主工具计数/分发。

学习时先理解主路径，再记住这些边界；不要把代码的设计意图写成超出实现的保证。本文只说明现有行为，不修改运行逻辑。

## 34. 最后核对：定义、实现和限制要分开看

### effect / condition 分别是什么？

- `effect='independent'`：不依赖额外条件的独立效果，协议要求其 condition 为 satisfied。
- `effect='conditional'`：只有指定条件成立才允许写入的效果。当前实现支持的结构化条件是天气降雨概率阈值，由代码核对本轮研究证据中的城市、日期和数值，不是模型说成立就成立。
- `effect='hypothetical'`：假设讨论，不可采用为正式写入。
- `condition='unknown'/'not_satisfied'`：条件未知/不成立，均不能采用。

正常结束可以采用通过证据校验且条件成立的 conditional 候选；`fail_closed()` 更保守，连这种 conditional 候选也排除，只保留 independent 候选。

新增需求候选还可能使此前基于旧需求的行程候选失效，不能把之前注册的旧 artifact_ref 当作永远有效的引用。

### 参数写在 Schema 中，不代表实现已完整使用它

例如 research 定义里有 `context_refs/hypothetical/evidence_kind`，但当前主运行器并没有把它们作为专门参数逐项传给子能力；实际任务文字取 `question`，研究证据类型在专业运行器里根据实际查询结果推导。不能只看字段说明，就推断这些参数的所有预期行为已经实现。

反过来，完整规划的 `idea_outcomes` 有实际本地检查：必须逐项覆盖想法引用，scheduled 的依据必须逐字出现在新正文；这仍不等于现实可执行性已经核验。

### 快捷路径与普通主调用的计数不同

快捷路径直接调用 `_run_capability()` 或 `_confirm()`，不经过 `_execute_call()` 和 `run()` 中的 `budget.tools(1)`。专业能力内部的模型/工具仍照常计数，不能把“不经过主工具计数”说成“整个快捷入口不消耗预算”。

## 35. 推荐继续阅读顺序

1. `app/agents/task_runtime.py::run`：掌握主循环。
2. `app/agents/task_runtime.py::_execute_call`：看工具名怎样分发到 Python 操作。
3. `app/agents/task_runtime.py::_run_capability`：看主 Agent 怎样调用专业能力。
4. `app/agents/agent_runtime.py::_run_capability_loop`：看专业能力自己的循环。
5. `app/agents/task_runtime.py::_terminal`：看 finish_turn / ask_user 怎样结束。
6. `app/workflow/turn_workspace.py::finish`、`fail_closed`：看候选怎样封装成提案。
7. `app/workflow/turn_commit.py`：看提案怎样经过校验并真正提交。

**一句话总结：`run()` 是带预算、工具协议和安全退出机制的“模型决策 → 程序执行 → 结果反馈”循环，最终产出一份可由存储层审查并提交的提案。**
