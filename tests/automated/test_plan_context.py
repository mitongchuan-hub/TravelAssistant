"""Main can verify existing activities against the saved itinerary."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app.application.contracts import RequestContext
from app.domain.itinerary.identity import plan_basis
from app.agents.inputs.context import project_main_context, project_main_read_context, project_plan, project_requirements
from app.agents.inputs.execution import TaskExecutionContext
from app.agents.travel_runtime import TaskExecutionRuntime
from app.domain.models import IdeaCard, Message, Trip, TripPlan, User
from app.services.itinerary.plan_display import text_hash
from app.storage.legacy.workflow import empty_workflow


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


@pytest.mark.parametrize('end,day_count,expected', [
    ('2026-11-02', 2, ['2026-11-01', '2026-11-02']),
    ('2026-11-02', 1, None),
    ('2026-10-31', 1, None),
    ('', 2, None),
])
def test_requirements_include_derived_dates_and_trusted_lodging(end, day_count, expected):
    trip = Trip('trip', '杭州', '', '', '', '', User('user', '旅客', '旅'), '', '')
    trip.requirements.start_date = '2026-11-01'
    trip.requirements.end_date = end
    trip.requirements.day_count = day_count
    trip.requirements.city = '杭州'
    trip.requirements.lodging_address = '测试住宿地址'
    trip.requirements.outing_start_time = '09:00'
    trip.lodging = {'poi_id': 'hotel', 'name': '测试酒店', 'longitude': 120.1,
                    'latitude': 30.2, 'source': '高德地图 POI'}
    before = deepcopy(trip)
    view = project_requirements(trip)
    assert view['known']['dates'] == expected
    assert view['known']['lodging']['poi_id'] == 'hotel'
    assert view['known']['lodging']['longitude'] == 120.1
    assert view['known']['outing_start_time'] == '09:00'
    assert view['known']['outing_end_time'] == ''
    assert trip == before
    read = project_main_read_context('requirements', {'items': []}, trip=trip)
    assert read['known'] == view['known']


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


@pytest.mark.parametrize('include_ideas', [False, True])
def test_single_agent_reads_current_plan_before_readonly_reply(plan, include_ideas):
    before = deepcopy(plan)
    seen = []

    def request(client, model, messages, **kwargs):
        seen.append(kwargs['agent_role'])
        payload = json.loads(messages[1]['content'])
        assert payload['current_plan']['text'] == plan.text
        assert '若临时管制则改为步行' in payload['current_plan']['text']
        if include_ideas:
            assert payload['requirements']['items']['items'][0]['body'] == '晚上骑共享单车'
        return response('晚上已安排骑行；若临时管制则改为步行。')

    runtime = reply_runtime(plan, request, include_ideas=include_ideas)
    proposal = runtime.run()

    assert proposal.answer == '晚上已安排骑行；若临时管制则改为步行。'
    assert len(seen) == 1  # Correct context replaces the old second-model reviewer.
    assert not proposal.failed and not proposal.changes
    assert plan == before


@pytest.mark.parametrize('failure', ['timeout', 'empty'])
def test_unavailable_model_does_not_publish_success_or_change_plan(plan, failure):
    before = deepcopy(plan)

    def request(client, model, messages, **kwargs):
        if failure == 'timeout':
            raise TimeoutError('模型超时')
        return response('')

    runtime = reply_runtime(plan, request)
    proposal = runtime.run()

    assert proposal.failed
    assert not proposal.changes
    assert plan == before
