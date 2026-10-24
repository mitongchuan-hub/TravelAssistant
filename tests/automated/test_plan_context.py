"""Main can verify existing activities against the saved itinerary."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app.agents.agent_protocol import RequestContext, plan_basis, validate_terminal_proposal
from app.agents.main_input import project_main_context, project_main_read_context, project_plan
from app.agents.task_runtime import TaskExecutionContext, TaskExecutionRuntime
from app.models import IdeaCard, Message, Trip, TripPlan, User
from app.services.plan_display import text_hash
from app.workflow.task_context import empty_workflow


@pytest.fixture
def plan():
    text = '10月24日\n\n19:30 天安门夜骑，骑共享单车。若临时管制则改为步行。'
    return TripPlan(
        'saved-plan', '北京市行程', '已确认', text=text,
        requirements_snapshot={'start_date': '2026-10-24', 'end_date': '2026-10-24'},
        display_status='ready', display_source_hash=text_hash(text),
        display={
            'schema_version': '1.1', 'title': '北京市行程', 'overview': None,
            'budget_note': None, 'weather': [], 'pending_items': [], 'risks': [],
            'preparation': {'clothing': [], 'accommodation': []},
            'days': [{'date': '2026-10-24', 'label': '第1天', 'theme': None, 'items': [{
                'time_text': '19:30', 'title': '天安门夜骑（共享单车）',
                'category': 'activity', 'location': '天安门周边', 'duration_text': None,
                'description': '骑共享单车。', 'cost_text': None,
                'notes': ['若临时管制则改为步行。'],
            }]}],
        },
    )


def test_text_itinerary_exposes_saved_activities_without_legacy_days(plan):
    before = deepcopy(plan)

    view = project_plan(plan, plan_basis(plan))

    assert view['outline'] is not None
    day = view['outline'][0]
    assert day['date'] == '2026-10-24'
    assert day['items'] == [{
        'title': '天安门夜骑（共享单车）', 'time_text': '19:30', 'location': '天安门周边',
    }]
    assert plan == before


def test_initial_context_includes_current_prose_despite_outdated_chat(plan):
    owner = User('user', '旅客', '旅')
    trip = Trip('trip', '北京市', '2026-10-24', '', '', '', owner, '已确认', '')
    history = [Message('old-reply', User('agent', '助手', 'AI'), 'agent',
        '晚上骑行还没有安排行程，需要加入吗？', 'earlier')]
    execution = SimpleNamespace(trip=trip, messages=history, idea_cards=[], current_date='2026-10-23')
    request = SimpleNamespace(message_id='current', body='你好', entry='main',
        plan_basis=plan_basis(plan), requirements_revision=0)

    context = project_main_context(request_context=request, execution_context=execution,
        workflow=empty_workflow(), recent_chat=history, plan=plan)

    assert context['current_plan']['text'] == plan.text
    assert '若临时管制则改为步行' in context['current_plan']['text']
    assert context['conversation'][0]['text'] == history[0].body


@pytest.mark.parametrize('invalid_display', ['old_text', 'wrong_date', 'pending', 'malformed'])
def test_untrusted_display_cannot_supply_current_activities(plan, invalid_display):
    if invalid_display == 'old_text':
        plan.text = '晚上返回住宿休息。'
    elif invalid_display == 'wrong_date':
        plan.display['days'][0]['date'] = '2026-10-25'
    elif invalid_display == 'pending':
        plan.display_status = 'pending'
    else:
        del plan.display['days'][0]['items'][0]['title']

    assert project_plan(plan, plan_basis(plan))['outline'] is None


@pytest.mark.parametrize('display_status', ['ready', 'pending', 'failed'])
def test_plan_read_returns_complete_prose_and_conditions(plan, display_status):
    plan.display_status = display_status
    plan.text = ('白天参观。\n' * 1000) + plan.text + '\n21:30 返回住宿。'
    before = deepcopy(plan)

    result = project_main_read_context('plan', {}, plan=plan, expected_basis=plan_basis(plan))

    assert result['text'] == plan.text
    assert result['text'].endswith('21:30 返回住宿。')
    assert '若临时管制则改为步行' in result['text']
    assert 'evidence' not in result and 'requirements_snapshot' not in result
    assert plan == before


def test_plan_read_rejects_changed_basis(plan):
    basis = plan_basis(plan)
    plan.text = '晚上返回住宿休息。'

    with pytest.raises(ValueError, match='行程'):
        project_main_read_context('plan', {}, plan=plan, expected_basis=basis)


def test_empty_plan_read_does_not_invent_content():
    plan = TripPlan('empty', '', '未生成')
    assert project_main_read_context('plan', {}, plan=plan, expected_basis=plan_basis(plan)) == {'plan': None}


def reply_runtime(plan, request, *, include_ideas=True):
    owner = User('user', '旅客', '旅')
    trip = Trip('trip', '北京市', '2026-10-24', '', '', '', owner, '已确认', '')
    message = Message('current', owner, 'user', '你好', 'now')
    context = RequestContext('trip', owner.id, 'execution', None, 'request', message.id,
        message.body, 'main', 'task_execution', 0, plan_basis(plan), 0, 0, 0)
    ideas = [IdeaCard('idea', '待归类', '晚上骑共享单车', '晚上骑共享单车', '你', '待整理')]
    execution = TaskExecutionContext(trip, [message], ideas if include_ideas else [], plan, {}, '2026-10-23')
    return TaskExecutionRuntime(None, 'test-model', context, execution, request=request)


def response(content, calls=()):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls))])


@pytest.mark.parametrize('structured', [False, True])
def test_readonly_reply_is_reviewed_before_sealing_and_preserves_plan(plan, structured):
    before = deepcopy(plan)
    unsafe = '晚上骑行还没安排，需要加入吗？'

    def request(client, model, messages, **kwargs):
        if kwargs['agent_role'] == 'main_reply_review':
            payload = json.loads(messages[-1]['content'])
            assert payload['current_plan']['text'] == plan.text
            assert payload['draft_reply'] == unsafe
            return response('你好！')
        if structured:
            call = SimpleNamespace(id='call-1', type='function', function=SimpleNamespace(
                name='finish_turn', arguments=json.dumps({'reply': unsafe})))
            return response(None, [call])
        return response(unsafe)

    runtime = reply_runtime(plan, request)
    proposal = runtime.run()

    assert proposal.answer == '你好！'
    assert not proposal.failed and not proposal.selected_artifact_ids
    validate_terminal_proposal(proposal, runtime.request_context)
    assert plan == before


@pytest.mark.parametrize('review_result', ['timeout', 'empty'])
def test_unavailable_review_does_not_publish_unverified_reply(plan, review_result):
    def request(client, model, messages, **kwargs):
        if kwargs['agent_role'] == 'main_reply_review':
            if review_result == 'timeout':
                raise TimeoutError('回复核对超时')
            return response('')
        return response('晚上骑行还没安排，需要加入吗？')

    runtime = reply_runtime(plan, request)
    proposal = runtime.run()

    assert proposal.failed
    assert '需要加入' not in proposal.answer
    assert not proposal.selected_artifact_ids
    validate_terminal_proposal(proposal, runtime.request_context)


def test_reply_without_idea_wall_does_not_require_extra_review(plan):
    def request(client, model, messages, **kwargs):
        assert kwargs['agent_role'] == 'main_execution'
        return response('你好！')

    proposal = reply_runtime(plan, request, include_ideas=False).run()
    assert proposal.answer == '你好！' and not proposal.failed
