"""Second-phase main tool and observation contracts, offline and deterministic."""
from copy import deepcopy
from types import SimpleNamespace

from app.agent_protocol import CapabilityResult, CapabilityStatus, FrozenDict, TerminalQuestion
from app.agent_roles import execution_roles
from app.main_input import project_capability_observation, project_main_read_context
from app.models import Trip, TripPlan, TripRequirements, User
from app.task_context import empty_workflow
from app.task_runtime import TaskExecutionContext, TaskExecutionRuntime


def _runtime():
    owner = User('obs-owner', '观察用户', '观')
    trip = Trip('obs-trip', '杭州', '', '', '', '', owner, '草案', '',
        requirements=TripRequirements(city='杭州', revision=1), workflow_state=empty_workflow())
    context = SimpleNamespace(
        trip=trip, messages=[], idea_cards=[], plan=TripPlan('plan', '标题', '未生成'),
        metrics={}, current_date='2026-10-16', entry='main', scope='', target_item_id=None,
        display_target=None,
    )
    from app.agent_protocol import PlanBasis, RequestContext
    request = RequestContext('obs-trip', owner.id, 'exec', None, 'req', 'message', '查询酒店',
        'main', 'task_execution', 1, PlanBasis('', '', '', '', None, False), 0, 0, 0)
    return TaskExecutionRuntime(object(), 'mock', request, TaskExecutionContext(**context.__dict__),
        request=lambda *args, **kwargs: None)


def test_capability_observation_keeps_refs_question_and_error_omits_empty_fields():
    question = TerminalQuestion('question', 'task', '预算？',
        FrozenDict({'namespace': 'hotel.search', 'field': 'nightly_budget'}), ('message',))
    result = CapabilityResult(CapabilityStatus.NEEDS_INPUT, 'research', '需要预算',
        artifact_refs=('artifact',), evidence_refs=('evidence',), input_request=question)
    projected = project_capability_observation(result)
    assert projected == {
        'status': 'needs_input', 'summary': '需要预算', 'artifact_refs': ['artifact'],
        'evidence_refs': ['evidence'], 'input_request': {
            'ref': 'question', 'task_ref': 'task', 'text': '预算？',
            'answer_target': {'namespace': 'hotel.search', 'field': 'nightly_budget'},
        },
    }


def test_capability_observation_keeps_failed_reason_and_does_not_mutate_result():
    result = CapabilityResult(CapabilityStatus.FAILED, 'research', '查询失败', error='来源不可用')
    before = deepcopy(result)
    assert project_capability_observation(result) == {
        'status': 'failed', 'summary': '查询失败', 'error': '来源不可用',
    }
    assert result == before


def test_execution_tool_schema_contract_remains_nine_strict_tools():
    tools = execution_roles('travel')['main_execution'].tools
    assert [tool['function']['name'] for tool in tools] == [
        'read_context', 'manage_task', 'prepare_requirements', 'research',
        'draft_itinerary', 'revise_itinerary', 'confirm_itinerary', 'ask_user', 'finish_turn',
    ]
    assert all(tool['function']['parameters']['additionalProperties'] is False for tool in tools)
    assert 'condition_predicate' in next(tool for tool in tools if tool['function']['name'] == 'draft_itinerary')['function']['parameters']['properties']


def test_main_read_tasks_hides_basis_and_keeps_open_question():
    runtime = _runtime()
    result = runtime._read_context({'section': 'tasks', 'offset': 0, 'limit': 20})
    assert set(result) == {'focus_task_ref', 'active_question', 'items', 'total'}
    assert 'basis' not in result


def test_main_read_context_hides_shared_internal_contract():
    runtime = _runtime()
    runtime.capability_runtime.read_context = lambda args: {
        'basis': {'requirements_revision': 99, 'plan_id': 'secret'},
        'items': [{'id': 'msg-1', 'sender': {'id': 'owner'}, 'sender_type': 'user',
                   'body': '保留', 'created_at': 'secret', 'status': 'sent',
                   'agent_card': {'secret': True}}],
        'total': 1, 'next_offset': None, 'query': '',
    }
    result = runtime._read_context({'section': 'messages', 'offset': 0, 'limit': 20})
    assert result['items'] == [{'ref': 'msg-1', 'role': 'user', 'text': '保留'}]
    assert 'basis' not in result and 'created_at' not in repr(result)


def test_main_evidence_reads_workspace_artifact_without_adopting_it():
    runtime = _runtime()
    artifact = runtime.workspace.register_evidence({
        'summary': '天气已查询',
        'evidence_kind': 'weather',
        'results': [{'tool': 'query_weather', 'result': {
            'status': 'available', 'city': '杭州', 'requested_date': '2026-10-16',
            'precipitation_probability_max': 70, 'source': 'secret-provider',
        }}],
    }, source_capability='research')
    result = runtime._read_context({'section': 'evidence', 'offset': 0, 'limit': 20})
    assert result['items'][0]['ref'] == artifact.id
    assert result['items'][0]['facts']['results'][0]['facts']['precipitation_probability_max'] == 70
    assert artifact.id not in [item.id for item in runtime.workspace.ready_to_commit]
