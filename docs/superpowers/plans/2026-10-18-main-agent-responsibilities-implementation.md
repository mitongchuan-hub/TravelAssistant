# MainAgent 职责边界实施方案

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 MainAgent 收敛为同城游玩业务主控：理解请求与授权、选择专业能力、决定业务结果的取舍、协调追问并解释结果；由程序负责候选依赖、工作流落地、校验和提交状态。

**Architecture:** 沿用 `task_execution`、现有九个主工具、专业能力及 `TurnWorkspace → TurnCommit` 提交流程。在主工具和内部候选协议之间增加本轮业务结果映射，使主控选择业务成果，运行时展开合法依赖。分别修复插话保留追问、取消操作的失败保留和保存状态表达，不引入额外分类模型。

**Tech Stack:** Python、现有 Agent Runtime、JSON Schema / jsonschema、pytest、现有模型传输层。

## Global Constraints

- 本方案是开发交接材料；本次交付仅新增 Markdown，业务实现与验证由后续开发 Agent 执行。
- 当前产品只服务于已确认住宿所在城市的同城游玩，按日返回住宿；生成、修改继续遵守现有业务前置条件。
- 住宿、开始日期和天数是规划必需信息；人数、预算未知不能单独阻断生成。
- 沿用 `task_execution`；不恢复旧意图组合路由，不新增职责分类 Agent，不增加每轮额外模型审核。
- 保留真实用户消息、授权、追问用途、条件证据、候选失效、依赖、版本、请求代次和原子提交检查。
- 用户授权的理解和业务成果取舍属于 MainAgent；决定能否实际写入属于程序。不能将“能力返回成功”视为用户已授权采用全部结果。
- Markdown 行程正文是权威交付物，展示卡片是派生结果；主控不另写第二份行程正文。
- 不调用真实模型、地图或天气服务的测试必须保持离线；真实模型评测单独运行、保留原始结果并人工审查。
- Windows 命令统一使用 `E:/anaconda/envs/TravelAssistant/python.exe`；不要使用 PATH 上的 `python` 或 `py`。
- 工作树已有大量未提交的目录调整及其他开发工作。使用当前 `app/agents`、`app/services`、`app/workflow` 路径，保留已有修改；每任务只提交自身差异，不能使用 `git add .` 收入其他工作。

---

## 1. 依据、现状与实施范围

执行前阅读 [职责设计](../specs/2026-10-18-main-agent-responsibilities-design.md)、[CLAUDE.md](../../../CLAUDE.md)、[PRODUCT.md](../../../PRODUCT.md) 和 [README.md](../../../README.md)。验收场景 A01–A26 以职责设计第 10 节为准。

2026-10-18 检查到的工作树现状：

| 已有实现或记录 | 本方案如何处理 |
| --- | --- |
| main、requirements、research、planning、revision 提示词已补业务范围；已有 [范围实施记录](2026-10-18-main-agent-scope.md) | 保留并补回归，不重新建立另一套受理体系 |
| [范围验证记录](../../known-issues/2026-10-18-main-agent-scope-verification.md)、`tests/scope_eval.py`、`tests/automated/test_scope_eval_contract.py` 已存在 | 复用运行器，保持直接文本和结构化结束两种测试可运行；历史评测不等于本方案验收 |
| `manage_task` 暴露 `adopt`、`effect_refs`；终止工具暴露 `adopt_refs` | 主控改为选择业务结果引用，运行时封装候选依赖及采用步骤 |
| 主提示词、`run` 和 `_terminal` 要求旧追问必须消费或处置所属任务 | 放行合法插话保留，继续校验真正答案的绑定与消费 |
| 提示词仍要求某些路径预先说“需求已保存” | 删除冲突要求；实际保存状态沿用提交程序的真实结果 |
| `TurnCommit.commit_turn` 已允许旧活动追问原样保留，已按实际效果生成状态文案 | 优先复用；不能为了运行时改动放宽提交校验 |

当前问题是主控还承担过多提交协议细节，以及部分对话合同相互冲突。项目已经具备主控、专业能力和提交程序的分层，实施应在这一基础上收敛职责。

职责设计中有关“尚无测试文件”的历史描述已落后于当前工作树，交付时应更新，不能据此删除现有范围评测。

## 2. 实施后的责任合同

| 对象 | 负责决定或完成的事项 |
| --- | --- |
| MainAgent | 逐项判断受理范围；理解完整原话、条件与授权；区分答案、插话和任务切换；选择专业能力；决定采用哪些业务成果；解释结果、限制及未完成事项 |
| requirements | 从绑定的真实完整消息提取明确授权的业务内需求变化，保留完整 `user_quote` |
| research | 查询具体业务问题，交付证据和限制；查询成功不自动授权修改需求或行程 |
| planning / revision | 生成或修改完整行程正文；可继续执行任务内部必要的地点、路线和天气查询 |
| display | 从权威正文派生展示，保持内容一致 |
| Runtime / Workspace | 绑定来源与追问；将业务选择转换为合法候选集合；排序依赖、管理任务候选、检查条件与引用，生成可提交提案 |
| Commit | 按最新状态再次校验，实际应用、持久化、发布回执和保存状态文案 |

业务边界按目的和上下文判断，不能依赖单个关键词。当前行程相关的天气可以查询；没有上下文的“天气怎么样”先澄清游玩城市和日期。数学、菜谱、无关翻译、编程、医疗剂量、投资建议不因无需工具或夹带旅行用语而获得受理。

直接回答限于招呼、能力说明、已知需求或行程解释、适用证据和结果汇总、必要澄清、范围或能力限制说明。该规则同时覆盖 prose、`finish_turn.reply`、追问文字和转述的专业摘要。

## 3. 文件与接口安排

| 文件 | 本次职责 |
| --- | --- |
| 新建 `app/agents/main_results.py` | 仅管理本轮业务结果引用及候选依赖展开，不做语义分类、持久化或自动批准 |
| 修改 [agent_roles.py](../../../app/agents/agent_roles.py) | 主工具合同收敛，保持九个工具及本地参数校验 |
| 修改 [task_runtime.py](../../../app/agents/task_runtime.py) | 接入结果映射、追问保留、任务操作封装和失败结束 |
| 修改 [main_input.py](../../../app/agents/main_input.py) | 主控结果投影；保留判断任务、授权、追问和条件所需信息 |
| 按需修改 [agent_runtime.py](../../../app/agents/agent_runtime.py) | 让绑定答案产生的业务候选携带服务器生成的答案消费依赖 |
| 修改 [main.txt](../../../app/prompts/agents/main.txt) | 分工、工具用法、插话规则及提交状态表达；保留已实施的业务范围 |
| 按实测调整 [requirements.txt](../../../app/prompts/agents/requirements.txt)、[research.txt](../../../app/prompts/agents/research.txt)、[planning.txt](../../../app/prompts/agents/planning.txt)、[revision.txt](../../../app/prompts/agents/revision.txt) | 补专业能力边界，维持能力内部查询与完整原话合同 |
| 审核 [task_context.py](../../../app/workflow/task_context.py)、[turn_workspace.py](../../../app/workflow/turn_workspace.py)、[turn_commit.py](../../../app/workflow/turn_commit.py) | 复用状态转换、采用和提交校验；不要求机械修改每个文件 |
| 新建 `tests/automated/main_agent_fixtures.py` | 离线模型响应、带原追问的运行时、隔离提交环境 |
| 新建 `tests/automated/test_main_results.py`、`test_main_agent_responsibilities.py`、`test_main_agent_commit_truth.py` | 分别覆盖选择合同、会话行为、真实提交结果 |
| 修改 [scope_eval.py](../../../tests/scope_eval.py)，新建 `tests/main_agent_eval.py`、`tests/datasets/main_agent_responsibilities_v1.json` | 保持原范围评测；补成功能力、无上下文、多轮和失败场景 |
| 更新职责设计与验证记录 | 同步实际实现、命令、结果及未验证项 |

### 主工具的目标接口

九个工具名称保持不变：`read_context`、`manage_task`、`prepare_requirements`、`research`、`draft_itinerary`、`revise_itinerary`、`confirm_itinerary`、`ask_user`、`finish_turn`。

| 接口 | 目标变化 |
| --- | --- |
| 专业结果、任务候选、确认候选 | 主控可见 `status`、`summary`、`result_ref`、必要证据、缺项、限制；不向主控暴露整组 `artifact_refs` 的采用协议 |
| `finish_turn` / `ask_user` | `adopt_refs` 改为 `accept_result_refs: list[str]`，表达主控决定采用哪些业务成果；默认空列表 |
| `finish_turn` | 增加 `preserve_question_ref: str | null`，用于显式保留原追问；默认 `null` |
| `manage_task` | 操作为 create / complete / suspend / resume / cancel，删除模型可调用的 adopt；`effect_refs` 改为 `result_refs`，用于完成任务时指定业务成果 |
| `manage_task.effect_kind` | 创建任务时必须提供；后续操作从服务器任务读取，若模型提供冲突值则拒绝 |
| `requirements_ref`、`plan_ref`、`answer_question_ref`、`answer_target` | 保留有明确业务用途的依据与答案引用；其有效性由程序校验 |
| `effect`、`condition`、`condition_predicate`、`condition_evidence_refs` | 保留当前授权和条件语义；程序继续核对真实证据，不以主控自报满足为依据 |

`result_ref` 只在本次运行时有效，由服务器产生，映射一份能力结果或一个任务操作的候选集合。选择行程结果可以包含它明确声明的需求依赖；不得据此加入其他并列结果、其他行程、未选择的需求或无关任务。

## 4. Task 1：建立离线行为测试入口

**文件：** 新建 `tests/automated/main_agent_fixtures.py`、`tests/automated/test_main_agent_responsibilities.py`；保留现有 `test_scope_eval_contract.py`。

**接口：** 提供 `prose_reply(text)`、`tool_reply(name, **arguments)` 和 `make_runtime(body, *, responses, pending_field=None, overrides=None, entry='main') -> TaskExecutionRuntime`。`responses` 中的可调用项接收当前 runtime，便于使用服务器实际返回的引用。

- [ ] 建立以下离线响应及运行时工厂；未指定的专业能力调用直接令测试失败，不能意外访问网络。

```python
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4
import json

from app.agents.agent_protocol import RequestContext, plan_basis
from app.agents.task_runtime import TaskExecutionContext, TaskExecutionRuntime
from app.models import Message, Trip, TripPlan, TripRequirements, User
from app.workflow.task_context import add_task, empty_workflow, set_active_question


def prose_reply(text):
    return SimpleNamespace(content=text, tool_calls=[])


def tool_reply(name, **arguments):
    call = SimpleNamespace(id=uuid4().hex, type='function', function=SimpleNamespace(
        name=name, arguments=json.dumps(arguments, ensure_ascii=False)))
    return SimpleNamespace(content='', tool_calls=[call])


def make_runtime(body, *, responses, pending_field=None, overrides=None, entry='main'):
    owner = User('owner', 'Tester', 'T')
    trip = Trip('trip', '杭州', '', '', '', '', owner, '', '',
        requirements=TripRequirements(city='杭州', lodging_address='已确认住宿',
            start_date='2030-05-01', end_date='2030-05-01', day_count=1),
        lodging={'poi_id': 'hotel', 'name': '已确认住宿', 'city': '杭州'})
    plan = TripPlan('empty', '', '未生成')
    messages = []
    workflow = empty_workflow()
    if pending_field:
        if pending_field == 'start_date':
            trip.requirements.start_date = trip.requirements.end_date = ''
        origin = Message('origin', owner, 'user', '请帮我安排同城游玩', 'now')
        question = Message('question-message', owner, 'agent', '请补充出行信息', 'now')
        messages.extend([origin, question])
        workflow, task_id = add_task(workflow, goal='补充后安排同城游玩',
            source_message_ids=[origin.id], authorization_refs=[origin.id],
            effect_kind='itinerary', task_id='pending-task')
        workflow, _ = set_active_question(workflow, task_id=task_id,
            text='哪天出发？' if pending_field == 'start_date' else '几人同行？',
            answer_target={'namespace': 'requirements', 'field': pending_field},
            assistant_message_id=question.id, source_message_ids=[origin.id],
            question_id='pending-question')
    trip.workflow_state = workflow
    message = Message('current-message', owner, 'user', body, 'now')
    messages.append(message)
    context = RequestContext(trip.id, owner.id, 'execution', None, uuid4().hex,
        message.id, body, entry, 'task_execution', trip.requirements.revision,
        plan_basis(plan), workflow['revision'], workflow['sequence'], workflow['epoch'])
    execution = TaskExecutionContext(trip, messages, [], plan, {}, '2030-04-29', entry=entry)
    scripted = iter(responses)

    def request(client, model, messages, **kwargs):
        answer = next(scripted)
        answer = answer(runtime) if callable(answer) else answer
        return SimpleNamespace(choices=[SimpleNamespace(message=answer)])

    def unexpected(args, workspace, runtime):
        raise AssertionError('Unexpected capability call in an offline test')

    capabilities = {name: unexpected for name in (
        'prepare_requirements', 'research', 'draft_itinerary', 'revise_itinerary')}
    capabilities.update(overrides or {})
    runtime = TaskExecutionRuntime(None, 'offline', context, execution,
        request=request, capability_overrides=capabilities)
    return runtime
```

- [ ] 写入下列第一组行为测试。它们校验运行时能否保留追问，不声称证明模型能正确判断语义。

```python
from copy import deepcopy
import pytest
from app.agents.agent_protocol import thaw
from tests.automated.main_agent_fixtures import make_runtime, prose_reply, tool_reply


@pytest.mark.parametrize('structured', [False, True])
def test_capability_interruption_preserves_pending_date_question(structured):
    response = (tool_reply('finish_turn', reply='我可以帮你规划同城游玩。',
        accept_result_refs=[], preserve_question_ref='pending-question')
        if structured else prose_reply('我可以帮你规划同城游玩。'))
    runtime = make_runtime('你能做什么？', responses=[response], pending_field='start_date')
    before = deepcopy(runtime.workflow)
    proposal = runtime.run()
    assert not proposal.failed, proposal.reason
    assert not proposal.artifacts
    assert proposal.question is None
    assert thaw(proposal.workflow_state)['active_question'] == before['active_question']
    assert thaw(proposal.workflow_state)['tasks'] == before['tasks']
```

`proposal.question is None` 表示没有新建正式追问，旧追问仍位于工作流中。

- [ ] 运行现有范围合同测试并记录实际基线，再运行新增用例确认当前运行时尚未满足目标。

```powershell
& 'E:/anaconda/envs/TravelAssistant/python.exe' -m pytest tests/automated/test_scope_eval_contract.py -q
& 'E:/anaconda/envs/TravelAssistant/python.exe' -m pytest tests/automated/test_main_agent_responsibilities.py -q
```

新增用例在旧实现上应失败；必须查看失败原因，不能把导入错误、响应序列耗尽等测试装配错误当成已复现业务问题。后续任务全部完成后，两组测试都应通过。

## 5. Task 2：封装业务结果与候选依赖

**文件：** 新建 `app/agents/main_results.py`、`tests/automated/test_main_results.py`。

**接口：** `MainResultRegistry(workspace)`；`register(*, artifact_refs=(), evidence_refs=()) -> str | None`；`resolve(result_refs) -> tuple[tuple[str, ...], tuple[str, ...]]`。第一项是按依赖排序的业务候选，第二项是证据。模块只引用当前 Workspace，不持有 Store。

- [ ] 先建立需求候选与依赖它的行程候选，验证只选择行程结果时能获得合法依赖；未选择的其他结果不能被加入。

```python
from app.agents.main_results import MainResultRegistry
from tests.automated.main_agent_fixtures import make_runtime


def test_selecting_itinerary_expands_its_declared_requirement_dependency():
    runtime = make_runtime('2人同行', responses=[])
    workspace = runtime.workspace
    requirements = workspace.register_artifact('requirements', {
        'raw_patch': {'user_quote': '2人同行', 'changes': {'travelers': 2}},
        'normalized_changes': {'travelers': 2}}, source_capability='prepare_requirements')
    itinerary = workspace.register_artifact('itinerary', {
        'text': '同城游玩安排', 'evidence': {}, 'preserve_plan_basis': False},
        source_capability='draft_itinerary', depends_on=[requirements.id])
    registry = MainResultRegistry(workspace)
    result_ref = registry.register(artifact_refs=[itinerary.id])
    selected, evidence = registry.resolve([result_ref])
    assert selected == (requirements.id, itinerary.id)
    assert evidence == ()
    assert workspace.ready_to_commit == ()
```

这里使用候选图验证引用与依赖，不提交示例行程正文，不以该正文替代行程质量测试。

- [ ] 运行新增测试，确认尚无结果映射时失败；随后实现以下核心逻辑。

```python
from dataclasses import dataclass
from uuid import uuid4
from app.workflow.turn_workspace import WorkspaceError


@dataclass(frozen=True)
class MainResult:
    artifact_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]


class MainResultRegistry:
    def __init__(self, workspace):
        self.workspace = workspace
        self._results = {}

    def _valid(self, ref):
        artifact = self.workspace.get_artifact(ref)
        if not artifact.belongs_to(self.workspace.context):
            raise WorkspaceError('结果不属于本次请求')
        if self.workspace.invalidation_reason(ref):
            raise WorkspaceError('结果依赖的候选已失效')
        return artifact

    def register(self, *, artifact_refs=(), evidence_refs=()):
        if isinstance(artifact_refs, (str, bytes)) or isinstance(evidence_refs, (str, bytes)):
            raise WorkspaceError('结果引用必须为引用列表')
        artifacts = tuple(dict.fromkeys(artifact_refs))
        evidence = tuple(dict.fromkeys(evidence_refs))
        for ref in artifacts:
            if self._valid(ref).kind == 'evidence':
                raise WorkspaceError('证据不能登记为业务写入结果')
        for ref in evidence:
            if self._valid(ref).kind != 'evidence':
                raise WorkspaceError('证据引用种类错误')
        if not artifacts and not evidence:
            return None
        ref = 'result-' + uuid4().hex
        self._results[ref] = MainResult(artifacts, evidence)
        return ref

    def resolve(self, result_refs):
        if isinstance(result_refs, (str, bytes)):
            raise WorkspaceError('业务结果选择必须为引用列表')
        selected, evidence, visiting = [], [], set()

        def add_evidence(ref):
            if self._valid(ref).kind != 'evidence':
                raise WorkspaceError('结果包含非法证据引用')
            if ref not in evidence:
                evidence.append(ref)

        def visit(ref):
            if ref in selected:
                return
            if ref in visiting:
                raise WorkspaceError('候选依赖成环')
            artifact = self._valid(ref)
            if artifact.kind == 'evidence':
                raise WorkspaceError('证据不能作为业务效果采用')
            visiting.add(ref)
            for dependency in artifact.depends_on:
                visit(dependency)
            for proof in artifact.evidence_refs:
                add_evidence(proof)
            visiting.remove(ref)
            selected.append(ref)

        for ref in result_refs:
            if not isinstance(ref, str) or ref not in self._results:
                raise WorkspaceError('业务结果引用不存在或不属于本轮')
            result = self._results[ref]
            for artifact_ref in result.artifact_refs:
                visit(artifact_ref)
            for evidence_ref in result.evidence_refs:
                add_evidence(evidence_ref)
        return tuple(selected), tuple(evidence)
```

入口参数为字符串而非引用列表时也必须明确拒绝。`resolve` 不调用 `adopt`，不把假设、条件未知或证据当成可写事实；最终是否可采用继续由 Workspace 校验。

- [ ] 补齐下列行为用例：空选择；重复选择去重；纯证据结果；伪造或跨运行时引用；需求变化使旧行程失效；两个行程同时选择被 Workspace 拒绝；假设、条件未知和条件不满足被拒绝；选择 A 不加入并列 B。
- [ ] 运行 `-m pytest tests/automated/test_main_results.py -q`，查看实际失败并修正，直到所有上述合同通过；检查本任务差异后单独提交。

## 6. Task 3：接入主工具，移走手工采用步骤

**文件：** 修改 `agent_roles.py`、`task_runtime.py`、`main_input.py`、按需修改 `agent_runtime.py`；扩展 `test_main_agent_responsibilities.py`、`test_main_results.py`。

**接口：** 保持内部 `CapabilityResult`、`Artifact`、`TerminalProposal`、`CommitReceipt` 的权威合同。Runtime 新增 `self.main_results: MainResultRegistry`、`_fail_closed(reason: str) -> TerminalProposal`、`_build_failure_workflow(safe_artifact_ids) -> dict`。

- [ ] 先测试主控只提交 `accept_result_refs` 即可合法结束，不再要求复制依赖引用或调用 `manage_task adopt`；保留旧范围评测的两个离线用例。
- [ ] 按第 3 节调整主工具 Schema，继续使用 `strict_tool` 和本地 JSON Schema 校验。模型可见接口不同时保留两套终止字段；内部提交协议继续使用原有 `selected_artifact_ids`。

```python
# 合并到两个终止工具的 properties；默认值由 Runtime 用 get 读取。
terminal_properties = {
    'accept_result_refs': {'type': 'array', 'items': {'type': 'string'}},
}
# 只合并到 finish_turn。
finish_properties = {
    'preserve_question_ref': {'type': ['string', 'null']},
}
# 合并到 manage_task；删除旧 adopt 操作和 effect_refs 字段。
task_properties = {
    'operation': {'type': 'string',
        'enum': ['create', 'complete', 'suspend', 'resume', 'cancel']},
    'result_refs': {'type': 'array', 'items': {'type': 'string'}},
}
```

- [ ] 专业结果先经过现有 `workspace.observe` 验证，再登记结果引用。主控投影保留状态、摘要、证据、缺项、限制；新增 `result_ref`，去掉原始 `artifact_refs`。若结果含唯一需求候选，额外提供已有业务用途的 `requirements_ref`。

```python
output = project_capability_observation(result)
output.pop('artifact_refs', None)
result_ref = self.main_results.register(
    artifact_refs=result.artifact_refs, evidence_refs=result.evidence_refs)
if result_ref is not None:
    output['result_ref'] = result_ref
requirements = [ref for ref in result.artifact_refs
                if self.workspace.get_artifact(ref).kind == 'requirements']
if len(requirements) == 1:
    output['requirements_ref'] = requirements[0]
```

失败或 unavailable 且无真实候选、证据时，不伪造一个“可采用”的结果。任务和确认候选也要经过相同映射，不能继续漏出旧终止用法。保留内部 `_confirm` 原始输出供快捷确认入口使用，在模型工具派发处包装其结果。

- [ ] 终止时展开选中的结果，并加入服务器验证过的答案消费前置候选，再交给原有工作流选择和 Workspace。将 `resolve` 的证据传给 `finish(evidence_refs=...)`；`ask_user` 的路径也需传递。

```python
selected, evidence = self.main_results.resolve(args.get('accept_result_refs') or ())
selected = list(selected)
for ref in reversed(self._answer_binding_refs):
    if ref not in selected:
        selected.insert(0, ref)
workflow = self._selected_workflow(selected)
# finish_turn 的追问保留校验见 Task 4；ask_user 继续构造真实新问题。
proposal = self.workspace.finish(args['reply'].strip(),
    selected_artifact_ids=selected, evidence_refs=evidence, workflow_state=workflow)
```

该片段描述 `finish_turn` 主路径，实际实现还必须合并下述已明确接受的取消、挂起候选。`_selected_workflow` 关于遗漏任务变化的检查必须保留，不能把累计快照中的无关变化全部当成依赖自动采用。

- [ ] `manage_task complete` 将 `result_refs` 展开为真实业务效果，再复用 `_validate_task_effects`。read 任务不得借完成操作采用写入结果；写入任务必须具备匹配的业务效果；自由文本 `completed_items` 不能改变任务状态。
- [ ] create / resume / complete 返回可选择的任务结果，终止时明确接受才提交。complete 的 `result_refs` 是候选依赖，不能在建立完成候选时提前采用未选择的业务成果；当前 `_manage_task` 中直接 `workspace.adopt(effect_ref)` 的行为需要据此收敛。
- [ ] 对已有任务的 cancel / suspend：主控发出合法业务操作就表示选择这个任务变化；运行时登记并准备保留，不再让模型额外执行 adopt。操作不得夹带 `result_refs` 写入需求或行程；真实用户授权的识别仍须由主控完成并接受评测。
- [ ] 正常终止自动合并上述明确接受的取消、挂起候选。取消本轮刚创建且尚未提交的任务仍需合法候选依赖，不将它当作已存在的跨轮任务处理。
- [ ] 将 `run`、预算耗尽、纠错耗尽、异常和快捷能力失败等所有安全结束入口统一接入 `_fail_closed`。继续只保留已接受、独立、条件满足且依赖齐全的效果。
- [ ] `_build_failure_workflow` 从 `_base_workflow` 重放最终安全集合所支持的任务操作，不能直接提交最后一个累计快照。重放使用现有 `add_task`、`transition_task`、`consume_active_question`，并读取服务器保存的操作参数；任务完成的业务效果必须也在安全集合内。

```python
# 已存在任务的独立取消/挂起重放核心。state 初值为 deepcopy(_base_workflow)。
payload = thaw(artifact.payload)
if payload['operation'] in {'cancel', 'suspend'}:
    state = transition_task(state, payload['task_ref'],
        'cancelled' if payload['operation'] == 'cancel' else 'suspended',
        reason=payload.get('item', ''),
        unresolved_items=payload.get('unresolved_items') or None)
elif payload['operation'] == 'consume_answer':
    state, _ = consume_active_question(state,
        question_id=payload['question_ref'],
        answer_message_id=payload['answer_message_ref'])
```

create / resume / complete 按其既有状态转换重放，只有安全集合实际包含该任务候选及其前置时才执行。服务器保存重放参数时必须同时保存操作顺序；不能由模型自由构造工作流 JSON。

- [ ] 绑定短答产生的正式候选必须包含对应答案消费前置。检查 `agent_runtime.py` 注册 requirements / itinerary 的位置，向 `depends_on` 合并服务器 `_bound_answer.task_effect_ref`。正常结束、独立需求失败保留均不能出现“保存了答案产生的需求，原问题却还未消费”的状态。
- [ ] 保留“明确独立的需求变化作为后续规划依据，规划失败仍可保存”的现有能力。只有主控确实选择该有效需求作为后续输入、且它来自独立授权时，才准备保留；不能将所有需求能力返回值一律提前采用。若有答案消费前置，先一起准备对应任务效果并在失败工作流中一致重放。
- [ ] 增加以下集成回归并运行本任务全部测试：

| 用例 | 必须观察到的结果 |
| --- | --- |
| 用户取消已有规划任务，随后查询失败或预算耗尽 | 取消效果仍可合法提交；原需求、原行程保留；没有额外 adopt 模型调用 |
| 前面产生了未选择的行程或其他任务，随后独立取消原任务并失败 | 只保留安全取消及已经合法接受的独立效果，不带入未选结果或无依据任务变化 |
| 条件性完成候选不满足，另一个任务独立取消，随后失败 | 不提交条件性完成；独立取消与工作流一致 |
| 人数答案绑定后形成独立需求，后续规划失败 | 若保存该需求，必须同时合法消费人数追问；否则两者一起保持未提交 |
| 两个专业能力返回候选，只接受其中一个结果 | 不自动接受另一结果；必要依赖除外 |
| 只读查询完成 | 不完成仍等待日期的规划任务，不写入正式需求或行程 |

确认 Workspace 的未知条件、失效、重复行程、确认与新草稿互斥检查仍生效。检查本任务差异后单独提交。

## 7. Task 4：允许插话保留，维持答案绑定

**文件：** 修改 `task_runtime.py` 的 `run`、`_unsettled_base_question`、`_terminal`、`_can_finish_prose`、`_ask`、`_bind_active_answer`，以及 `main.txt`；扩展 `test_main_agent_responsibilities.py`。

**接口：** 新增 `_validate_preserved_question(final_workflow, preserve_question_ref) -> None`。这个方法验证结构和前置，不负责自然语言分类。

- [ ] 将 Task 1 两种结束路径的失败用例作为回归起点，追加业务外拒绝、含义不明短答、只读查询后结束三个场景。
- [ ] 对完全没有候选、工作流操作和答案绑定的 prose 回复，允许保留原追问。使用原工作流结束，不能因“本轮已回复”就消费问题、取消或挂起任务。
- [ ] 对结构化结束，旧追问仍存在时要求 `preserve_question_ref` 精确匹配原 ID，且旧追问和所属任务的业务状态保持一致；没有原追问却传入引用也必须拒绝。

```python
def _validate_preserved_question(self, final_workflow, preserve_question_ref):
    base = self._base_workflow.get('active_question')
    current = final_workflow.get('active_question')
    if preserve_question_ref is None:
        if base and current and current.get('id') == base['id']:
            raise ValueError('保留原追问时须明确给出 preserve_question_ref')
        return
    if not base or preserve_question_ref != base['id'] or current != base:
        raise ValueError('保留引用必须对应未改写的原活动追问')
    before = next(row for row in self._base_workflow['tasks'] if row['id'] == base['task_id'])
    after = next(row for row in final_workflow['tasks'] if row['id'] == base['task_id'])
    if before != after or self._answer_binding_refs:
        raise ValueError('已变更或已消费的原追问不能声明保留')
```

对原任务有正式需求或行程依据变更时，不能仅靠问题 ID 相同声称保留成功，仍须检查任务依据与版本。如果新事项确实要建立新的正式追问，继续走 `_ask` 和唯一活动追问规则；被替换任务按现有合同暂存，不能同时存在两个活动问题。

- [ ] 将提示词中的强制“消费或取消/挂起”改为以下规则，并同步运行时纠错文案：

```text
先判断本轮消息与原追问的关系。
明确答案：通过所属 task_ref 与 answer_question_ref 调用相应能力，按 answer_target 使用。
招呼、能力说明、范围外问题、临时只读插问：回复后保留原任务及原活动追问。
含义不明短答：澄清含义，保留原问题，不猜测写入其他字段。
明确取消、暂停或业务切换：按用户授权操作任务。
保留原问题并使用 finish_turn 时，填写 preserve_question_ref；没有业务候选时可以直接自然回复。
```

- [ ] 保留错误 question_ref、错误 task_ref、错误请求绑定及任务局部答案不得写正式需求的检查，不删除 `_bind_active_answer` 以换取结束路径通过。
- [ ] 增加并运行以下确定性测试：A13 两种结束路径；A14 查询成功与 unavailable；A15 合法人数答案与错误问题引用；A16 日期追问下的模糊“800”；A17 明确取消；新正式追问替换时仍唯一。
- [ ] 对每个插话用例比较正式需求及版本、行程正文和依据及确认状态、旧任务语义、原问题 ID / task / text / target / 绑定资格。正常聊天记录和请求计数变化不作为失败。
- [ ] 增加实际提交后的跨轮恢复测试：提交能力说明插话 → 新建真实日期消息 → 绑定原问题并消费。仅检查单轮提案不足以证明追问生命周期正确。

这里存在明确的语义边界：prose 放行和 `preserve_question_ref` 都无法独立证明用户没有回答原问题。真实模型必须评测“2人”“800”“后天”等短答；模型自报保留不算语义正确的证据。检查本任务差异后单独提交。

## 8. Task 5：保存状态以真实提交为准

**文件：** 修改 `main.txt`；审核 `turn_commit.py`、`turn_workspace.py`；新建 `test_main_agent_commit_truth.py`，扩展 `main_agent_fixtures.py`。

**接口：** 保持 `commit_turn(context, proposal) -> CommitReceipt`。不新增模型提交确认轮，不用成功关键词替换或删除来模拟真实回执。

- [ ] 在 fixture 文件新增 `make_commit_case(body)`。使用关闭持久化与展示后台线程的 `DemoStore`，明确追加当前用户消息，再创建与真实版本一致的 RequestContext。

```python
from app.store import DemoStore
from app.workflow.task_context import normalize_workflow
from app.workflow.turn_workspace import TurnWorkspace


def make_commit_case(body):
    store = DemoStore(persistence_enabled=False, display_worker_enabled=False)
    trip_id = next(iter(store.trips))
    trip = store.trips[trip_id]
    trip.requirements.travelers = None
    message = Message('commit-user-' + uuid4().hex, trip.owner, 'user', body, 'now')
    store.messages[trip_id].append(message)
    workflow = normalize_workflow(trip.workflow_state, trip=trip,
        messages=store.messages[trip_id], plan=store.plans[trip_id])
    context = RequestContext(trip_id, trip.owner.id, 'execution-' + uuid4().hex,
        None, uuid4().hex, message.id, body, 'main', 'task_execution',
        trip.requirements.revision, plan_basis(store.plans[trip_id]),
        workflow['revision'], workflow['sequence'], workflow['epoch'])
    workspace = TurnWorkspace(context, workflow_state=workflow,
        authorized_message_ids=[row.id for row in store.messages[trip_id]],
        visible_task_ids=[row['id'] for row in workflow['tasks']])
    return store, context, workspace
```

- [ ] 先测试真实效果与最终消息一致，包含需求独立成功、行程被拒绝的部分成功。提交测试应调用真正的 `commit_turn`，不能把回执直接 mock 成成功。

```python
from app.services.requirements import chat_changes
from tests.automated.main_agent_fixtures import make_commit_case


def test_requirements_saved_but_invalid_itinerary_not_reported_as_updated():
    body = '人数改为2人。'
    store, context, workspace = make_commit_case(body)
    raw = {'user_quote': body, 'changes': {'travelers': 2}}
    requirements = workspace.register_artifact('requirements', {
        'raw_patch': raw,
        'normalized_changes': chat_changes(raw, body, store.trips[context.trip_id])},
        source_capability='prepare_requirements')
    itinerary = workspace.register_artifact('itinerary', {
        'text': '', 'evidence': {}, 'preserve_plan_basis': False},
        source_capability='draft_itinerary', depends_on=[requirements.id])
    proposal = workspace.finish('行程处理尚未完成。',
        selected_artifact_ids=[requirements.id, itinerary.id])
    receipt = store.commit_turn(context, proposal)
    assert receipt.committed
    assert requirements.id in receipt.accepted_artifact_ids
    assert itinerary.id not in receipt.accepted_artifact_ids
    assert store.trips[context.trip_id].requirements.travelers == 2
    message = next(row for row in store.messages[context.trip_id]
                   if row.id == receipt.message_id)
    assert '需求已保存' in message.body
    assert '行程已更新' not in message.body
```

确认空行程确实由当前行程验证器拒绝；如该拒绝路径变化，使用当前验证器明确拒绝的正文并记录原因，不能改为绕过行程校验。

- [ ] 删除主提示词中让主控提前宣告“需求已保存”的要求，替换为“说明行程未完成等业务限制；保存结果由提交程序补充”。确认、取消同样不得在候选阶段宣告提交成功。
- [ ] 复用 `commit_turn` 现有 `effect_lines`，以实际发生的需求变化、行程采用、确认和取消生成状态。遇到版本冲突、候选拒绝或持久化失败，不能发布该效果的成功消息。
- [ ] 补齐：候选生成但未选择；需求没有实际变化；版本冲突；独立需求成功而规划运行失败；行程候选提交被拒绝；确认失败；取消失败；相同 request 重复提交；持久化异常不发布任何暂存业务状态或成功消息。
- [ ] 持久化失败用例使用临时路径和 monkeypatch `app.workflow.turn_commit.save_snapshot` 抛出异常；断言 `TurnPersistenceError`、发布前后的业务状态一致，不连接实际数据库。
- [ ] 同时检查 `receipt.accepted_artifact_ids`、`rejected_artifacts`、真实业务状态和最终用户消息。必要时最小修改提交逻辑；如果现有程序已满足，保留实现并补回归。运行 `-m pytest tests/automated/test_main_agent_commit_truth.py -q`，检查差异后单独提交。

## 9. Task 6：保持业务范围，完成跨入口语义验收

**文件：** 复用 `tests/scope_eval.py`、`test_scope_eval_contract.py`；新建 `tests/main_agent_eval.py`、`tests/datasets/main_agent_responsibilities_v1.json`；按结果调整现有五份业务提示词。

**接口：** 新运行器支持 `--live`、`--ids`、`--repeat`、`--structured`、`--label`；不加 `--live` 时仅列出场景。默认离线，不自动调用外部服务。

- [ ] 更新现有范围合同测试中手工构造的 `finish_turn` 参数，使用 `accept_result_refs=[]`；测试仍须分别验证 prose 与结构化结束实际通过 Runtime，不能删除其中一路来绕过失败。
- [ ] 数据集逐项包含 `id`、`initial_state`、`turns`、`capability_fixture`、`expected_calls`、`expected_state`、`semantic_rubric`。覆盖职责设计全部 A01–A26，不能只给已有 12 个范围案例换编号。

```json
{
  "id": "A13",
  "initial_state": "pending_start_date",
  "turns": ["你能帮我做什么？", "2030年5月1日出发"],
  "capability_fixture": "requirements_success",
  "expected_calls": [[], ["prepare_requirements"]],
  "expected_state": {
    "after_turn_1": "original_question_preserved",
    "after_turn_2": "original_question_consumed_by_bound_date_answer"
  },
  "semantic_rubric": "第一轮只介绍同城游玩能力，第二轮正确使用日期，不取消原任务、不扩大授权。"
}
```

`expected_calls` 表示该场景需要的专业能力，不把 `finish_turn` 等终止工具混在其中。测试场景的成功需求 fixture 可以只完成日期整理，避免额外规划调用影响这个针对追问恢复的案例。

- [ ] `main_agent_eval.py` 实现并校验上述状态、fixture 和期望标识；遇到未知值直接报错。复用 Task 1 / Task 5 的离线基础设施，不建立第二套主控执行算法。
- [ ] 实现 fixture：无专业调用；需求成功；查询成功与带证据；unavailable；查询中需要补充；规划成功；规划失败；行程拒绝；条件未知；越界专业摘要。成功 fixture 通过 Workspace 注册候选和证据，不能只返回伪造引用。
- [ ] 多轮用例每轮通过真实 `commit_turn` 写入隔离内存 Store，再从实际状态建立下一轮上下文；不得仅在内存里手工删改 `active_question` 伪造恢复成功。
- [ ] A05 使用确定日期、城市及真实结构的天气 fixture；A06 使用无可用住宿、城市和日期的初始状态，避免沿用现有范围运行器默认杭州上下文而使场景失真。
- [ ] A08 / A09 对比必填缺项与可选字段未知；A07 验证普通查询不索取全部规划字段；保留完整原话与混合请求的业务内提取检查。
- [ ] A20 在专业摘要中注入无关代码、菜谱或剂量内容，人工检查主控没有透传；不能只检查调用过正确工具。
- [ ] A25 分别覆盖 planning、revision、confirm 快捷入口，验证用户授权、精确修改范围、方案依据、条件及版本。快捷入口不必额外调用 MainAgent，但不能执行附带的无关任务或越权写入。
- [ ] 对接真实模型时，仅替换模型请求函数；默认继续以 fixture 隔离专业工具。另行报告哪些专业能力使用真实模型与真实外部服务，不能把模拟成功说成业务链路已全部验证。
- [ ] 每次真实运行记录模型名称、参数、提示词全文或哈希、工具 Schema 哈希、代码版本或工作树差异标识、完整逐轮输入、调用参数、结果、提交回执及最终文本；输出到 `.cache/main_agent_eval/`，交付报告保留可定位的结果索引。
- [ ] 记录实际使用的终止方式；标记为 structured 的案例必须实际调用 `finish_turn` 或合法 `ask_user`。只在评测参数里写了 structured、实际仍直接输出 prose 的运行，不计入结构化路径覆盖。
- [ ] 人工逐条评审：范围外实质内容、合法业务误拒、无依据动态事实、错误答案绑定、错误完成或保存承诺。结构检查和语义结果分别统计，不以关键词检测或模型自报“业务内”替代语义验收。

执行顺序与命令：

```powershell
# 离线回归；必须先通过。
& 'E:/anaconda/envs/TravelAssistant/python.exe' -m pytest tests/automated -q

# 现有范围评测，验证本次接口调整后两种终止路径仍有效。
& 'E:/anaconda/envs/TravelAssistant/python.exe' -m tests.scope_eval --live --label responsibilities --repeat 3
& 'E:/anaconda/envs/TravelAssistant/python.exe' -m tests.scope_eval --live --label responsibilities-structured --structured --repeat 3

# 新运行器先检查场景，再执行 A01–A26。
& 'E:/anaconda/envs/TravelAssistant/python.exe' -m tests.main_agent_eval
& 'E:/anaconda/envs/TravelAssistant/python.exe' -m tests.main_agent_eval --live --label responsibilities --repeat 3
& 'E:/anaconda/envs/TravelAssistant/python.exe' -m tests.main_agent_eval --live --label responsibilities-structured --structured --repeat 3
```

真实评测沿用项目既有模型连接配置，不在文档、日志或命令中写密钥。三次运行用于暴露波动，不代表对任意输入的保证。出现失败时保留样例、针对对应职责修复，再重跑受影响集及必要回归。

## 10. Task 7：更新文档并完成交接

**文件：** 更新职责设计、范围验证记录；新建 `docs/known-issues/2026-10-18-main-agent-responsibilities-verification.md`。

- [ ] 更新职责设计第 2、8、9、11 节，反映当前范围提示词、业务结果引用、追问保留合同、现有测试与实际命令，保持职责定义一致。
- [ ] 验证记录分别列出自动化合同、真实主模型语义、真实专业能力和外部服务的实际覆盖，不把历史范围验证当成本次全部验收结果。
- [ ] 提供四组真实可复查的用户可见记录：范围外拒绝、临时插话后恢复原追问、合法查询成功、提交失败或部分成功。示例来自实际运行，不手工编造“预期通过”作为证据。
- [ ] 检查所有模型可见工具说明、主提示词、错误纠正消息和运行器，确认没有遗留要求模型执行 `manage_task adopt` 或手工排序 `adopt_refs` 的旧用法。内部 Workspace 的 `adopt` 保留。
- [ ] 运行最终必要回归、查看实际输出并检查差异；仅提交本任务文件和已确认属于本方案的修改，不收录其他开发中的目录重构、缓存和模型日志。
- [ ] 交付时列出实际修改文件、测试命令与结果、语义评审结果、失败或未验证项。固定场景有失败时保持问题状态开放，不写“全部完成”。

## 11. 验收映射与完成条件

| 验收项 | 实施位置 | 关键证据 |
| --- | --- | --- |
| A01–A04：招呼、能力说明、范围外、两种终止路径 | Task 1、3、6 | 两条合同均通过；真实文本未提供范围外实质内容 |
| A05–A09：天气上下文、普通查询、规划必填与可选信息 | Task 6 | 查询证据、缺项追问、合法业务未被误拒 |
| A10–A12：授权需求、混合请求、旅行夹带医疗 | Task 2、3、5、6 | 完整原话、正确提取、合法子项可独立完成 |
| A13–A16：插话、只读插问、人数短答、模糊短答 | Task 1、4、6 | 实际提交后的原问题保留与跨轮正确消费 |
| A17：取消任务 | Task 3、4、5 | 无手工 adopt；失败后合法保留；既有业务按授权保留 |
| A18–A20：不支持执行、查询失败、专业摘要越界 | Task 3、6 | 未模拟成功、未编造事实、未透传越界内容 |
| A21–A22：假设与条件未知 | Task 2、3、6 | Workspace 和 Commit 继续阻止未授权写入 |
| A23–A24：未采用、拒绝、部分成功 | Task 3、5 | 回执、真实数据和最终状态文案一致 |
| A25–A26：快捷入口与已有行程解释 | Task 3、4、6 | 快捷入口保持合同；主控不重复生成或擅自修改 |
| 业务结果选择与依赖 | Task 2、3 | 不加入无关结果、不遗漏必要依赖、伪造与失效引用被拒绝 |
| 安全失败时工作流一致 | Task 3、5 | 只保留合法独立效果，任务完成与真实提交效果一致 |

完成标准：九个主工具合同一致；A01–A26 均有实际覆盖与评审结论；所有确定性回归通过；原追问可跨越插话继续使用；采用与提交保护持续有效；主控不承担依赖排序和额外 adopt 操作；最终保存状态与真实提交相符。交付报告必须说明仍依赖模型语义判断的部分及有限样例验证的范围。
