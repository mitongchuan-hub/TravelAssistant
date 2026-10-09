"""A new message replaces an unfinished task without waiting or saving old output."""
from copy import deepcopy
from threading import Event, Thread
import importlib

import pytest

from app.agent_client import AgentReply
from app.agent_runtime import AgentBudgetError, TravelAgentRuntime
from app.models import AgentCard
from app.store import DemoStore
from app.tasks import TaskConflict
from test_agent_roles import completion

store_module = importlib.import_module('app.store')


def reply(text, **kwargs):
    return AgentReply(text, AgentCard('itinerary-draft', '行程', text, [], []), [], **kwargs)


@pytest.mark.parametrize('kind', ['chat', 'plan', 'revision'])
@pytest.mark.parametrize('old_raises', [False, True])
def test_new_message_runs_before_old_request_returns_and_discards_old_result(monkeypatch, kind, old_raises):
    local = DemoStore(persistence_enabled=False)
    trip = local.create_trip('杭州', '', '', '', '')
    local._apply_text_plan(trip.id, '已保存的行程')
    entered, release = Event(), Event()
    calls = []
    def model(**kwargs):
        calls.append(kwargs)
        if kwargs['user_body'] == '最新消息':
            return reply('最新回复', plan_text='最新行程')
        entered.set()
        assert release.wait(5)
        if old_raises:
            raise TimeoutError('旧请求超时')
        return reply('旧结果', plan_text='旧行程', requirements_patch={
            'changes': {'budget_amount': '9999'}, 'evidence': kwargs['user_body']})
    monkeypatch.setattr(store_module, 'generate_agent_reply', model)
    payload = {'body': '旧消息'} if kind == 'chat' else ({'feedback': '旧修改'} if kind == 'revision' else {})
    old, _ = local.submit_task(trip.id, kind, payload, 'old')
    worker = Thread(target=local.run_task, args=(old['id'],))
    worker.start()
    try:
        assert entered.wait(2)
        latest, _ = local.submit_task(trip.id, 'chat', {'body': '最新消息'}, 'new')
        newest = Thread(target=local.run_task, args=(latest['id'],))
        newest.start()
        newest.join(2)
        assert not newest.is_alive(), 'new task waited for old model request'
        assert local.tasks[old['id']]['status'] == 'superseded'
        assert local.tasks[latest['id']]['status'] == 'succeeded'
        assert local.plans[trip.id].text == '最新行程'
        saved = deepcopy(local.plans[trip.id])
        versions = deepcopy(local.plan_versions[trip.id])
        assert not any(m.status == 'thinking' for m in local.messages[trip.id])
    finally:
        release.set()
        worker.join(5)
    assert local.plans[trip.id] == saved and local.plan_versions[trip.id] == versions
    assert local.trips[trip.id].requirements.budget_amount != '9999'
    assert not any(m.body in ('旧结果', '旧行程') or m.status == 'failed' for m in local.messages[trip.id])
    assert local.tasks[old['id']]['status'] == 'superseded'
    assert not local.tasks[old['id']]['result_url']
    assert len(calls) == 2


def test_queued_task_replaced_before_start_never_calls_model(monkeypatch):
    local = DemoStore(persistence_enabled=False)
    trip = local.create_trip('杭州', '', '', '', '')
    old, _ = local.submit_task(trip.id, 'chat', {'body': '旧消息'}, 'old')
    new, _ = local.submit_task(trip.id, 'chat', {'body': '新消息'}, 'new')
    duplicate, created = local.submit_task(trip.id, 'chat', {'body': '旧消息'}, 'old')
    assert not created and duplicate['status'] == 'superseded'
    assert local.tasks[new['id']]['status'] == 'queued'
    calls = []
    monkeypatch.setattr(store_module, 'generate_agent_reply', lambda **kw: calls.append(kw['user_body']) or reply('新回复'))
    local.run_task(old['id'])
    local.run_task(new['id'])
    assert calls == ['新消息']
    assert sum(m.sender_type == 'user' for m in local.messages[trip.id]) == 2
    with pytest.raises(TaskConflict):
        local.submit_task(trip.id, 'chat', {'body': '旧消息'}, 'retry-old', old['id'])


def test_invalid_new_message_does_not_cancel_current_task():
    local = DemoStore(persistence_enabled=False)
    old, _ = local.submit_task('trip-hangzhou', 'plan', {}, 'old')
    with pytest.raises(ValueError):
        local.submit_task('trip-hangzhou', 'chat', {'body': ''}, 'invalid')
    assert local.tasks[old['id']]['status'] == 'queued'


def test_superseded_state_survives_restart_and_only_latest_is_interrupted(tmp_path):
    path = str(tmp_path / 'supersede.sqlite3')
    local = DemoStore(db_path=path, persistence_enabled=True)
    old, _ = local.submit_task('trip-hangzhou', 'chat', {'body': '旧消息'}, 'old')
    new, _ = local.submit_task('trip-hangzhou', 'chat', {'body': '新消息'}, 'new')
    reopened = DemoStore(db_path=path, persistence_enabled=True)
    assert reopened.tasks[old['id']]['status'] == 'superseded'
    assert reopened.tasks[new['id']]['status'] == 'failed'
    assert not any(m.id == old['thinking_id'] for m in reopened.messages['trip-hangzhou'])


def test_cancelled_response_cannot_execute_tools():
    cancelled = [False]
    def model(*args, **kwargs):
        kwargs['budget'].model()
        cancelled[0] = True
        return completion(calls=[('query_weather', {'city': '杭州'})])
    runtime = TravelAgentRuntime(object(), 'test', {'user_body': '查询', 'cancelled': lambda: cancelled[0]}, request=model)
    with pytest.raises(AgentBudgetError, match='替代'):
        runtime.run('research')
    assert runtime.budget.tool_calls == 0 and runtime.budget.external_calls == 0
