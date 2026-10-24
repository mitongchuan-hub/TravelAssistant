"""Execution progress is factual, bounded and isolated from business state."""
from copy import deepcopy
import json

import pytest

from app.workflow.task_progress import advance_progress, finish_progress, new_progress


def test_model_lifecycle_does_not_publish_prompt_or_response():
    progress = new_progress(timestamp=1000)
    progress = advance_progress(progress, '模型请求', {
        'agent_role': 'main_execution', 'params': {'messages': 'PRIVATE PROMPT', 'api_key': 'SECRET'},
    }, timestamp=1100)
    assert progress['steps'][0]['status'] == 'running'
    progress = advance_progress(progress, '模型响应', {
        'agent_role': 'main_execution', 'content': 'PRIVATE RESPONSE', 'seconds': 1.5,
        'tool_calls': [{'arguments': 'PRIVATE ARGUMENTS'}],
    }, timestamp=2600)
    step = progress['steps'][0]
    assert step['status'] == 'succeeded'
    assert step['duration_ms'] == 1500
    assert all(word not in json.dumps(progress) for word in ('PRIVATE', 'SECRET'))


@pytest.mark.parametrize('result,error,status', [
    ({'status': 'available', 'forecast': 'PRIVATE'}, None, 'succeeded'),
    ({'status': 'unavailable', 'reason': 'PRIVATE'}, None, 'unavailable'),
    (None, 'TimeoutError', 'failed'),
])
def test_tool_state_uses_actual_outcome(result, error, status):
    progress = advance_progress(new_progress(timestamp=0), '工具开始', {
        'role': 'research', 'name': 'query_weather',
        'arguments': {'city': '北京市', 'start_date': '2026-10-24', 'token': 'SECRET'},
    }, timestamp=100)
    assert progress['steps'][0]['label'] == '查询天气'
    assert '北京市' in progress['steps'][0]['detail']
    progress = advance_progress(progress, '工具执行', {
        'role': 'research', 'name': 'query_weather', 'result': result, 'error_type': error,
    }, timestamp=3100)
    assert progress['steps'][0]['status'] == status
    assert progress['steps'][0]['duration_ms'] == 3000
    assert 'PRIVATE' not in json.dumps(progress) and 'SECRET' not in json.dumps(progress)


def test_unknown_events_are_ignored_and_finished_progress_cannot_restart():
    progress = new_progress(timestamp=1000)
    assert advance_progress(progress, '输入上下文', {'text': 'PRIVATE'}) is None
    progress = advance_progress(progress, '模型请求', {'agent_role': 'main_execution'}, timestamp=1100)
    progress = finish_progress(progress, 'superseded', timestamp=2000)
    assert progress['steps'][0]['status'] == 'stopped'
    assert progress['duration_ms'] == 1000
    assert advance_progress(progress, '模型响应', {'agent_role': 'main_execution'}, timestamp=2500) is None


def test_projection_is_bounded_and_preserves_running_step():
    progress = advance_progress(new_progress(timestamp=0), '模型请求', {'agent_role': 'main_execution'}, timestamp=1)
    for i in range(150):
        progress = advance_progress(progress, '工具开始', {'role': 'research', 'name': 'query_weather'}, timestamp=i + 2)
        progress = advance_progress(progress, '工具执行', {'role': 'research', 'name': 'query_weather', 'result': {'status': 'available'}}, timestamp=i + 3)
    assert len(progress['steps']) <= 80
    assert progress['steps'][0]['status'] == 'running'
    assert progress['omitted'] > 0


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv('TRAVEL_PERSISTENCE', '0')
    from app.store import DemoStore
    return DemoStore(persistence_enabled=False, display_worker_enabled=False)


def running(store):
    task, _ = store.submit_task('trip-hangzhou', 'chat', {'body': '查询天气'}, 'request-one')
    store.tasks[task['id']]['status'] = 'running'
    return task


def test_superseded_request_keeps_history_and_rejects_late_events(store):
    task = running(store)
    store.record_task_progress(task['id'], '模型请求', {'agent_role': 'main_execution'})
    message = next(row for row in store.messages['trip-hangzhou'] if row.id == task['thinking_id'])
    assert message.execution_progress['steps']
    next_task, _ = store.submit_task('trip-hangzhou', 'chat', {'body': '先不查了'}, 'request-two')
    before = deepcopy(store.messages['trip-hangzhou'])
    store.record_task_progress(task['id'], '模型响应', {'agent_role': 'main_execution'})
    assert store.messages['trip-hangzhou'] == before
    assert message.execution_progress['state'] == 'superseded'
    assert store.tasks[next_task['id']]['status'] == 'queued'


def test_progress_write_failure_restores_message_without_affecting_task(store, monkeypatch):
    task = running(store)
    before = deepcopy(store.messages['trip-hangzhou'])
    monkeypatch.setattr(store, '_persist', lambda: (_ for _ in ()).throw(OSError('disk unavailable')))
    store.record_task_progress(task['id'], '模型请求', {'agent_role': 'main_execution'})
    assert store.messages['trip-hangzhou'] == before
    assert store.tasks[task['id']]['status'] == 'running'


def test_committed_reply_and_snapshot_keep_execution_history(store, monkeypatch):
    from app.agents import agent_client
    from app.persistence import _message
    from app.workflow.turn_workspace import TurnWorkspace
    from dataclasses import asdict

    def generate(context, execution, *, cancelled, on_progress):
        on_progress('模型请求', {'agent_role': 'main_execution'})
        on_progress('模型响应', {'agent_role': 'main_execution'})
        workspace = TurnWorkspace(context, workflow_state=execution.trip.workflow_state)
        return workspace.finish('已处理你的消息。', workflow_state=execution.trip.workflow_state)

    monkeypatch.setattr(agent_client, 'generate_task_proposal', generate)
    task, _ = store.submit_task('trip-hangzhou', 'chat', {'body': '你好'}, 'commit-test')
    receipt = store.run_task(task['id'])
    assert receipt.committed
    message = next(row for row in store.messages['trip-hangzhou'] if row.id == task['thinking_id'])
    assert message.status == 'sent'
    assert message.execution_progress['state'] == 'succeeded'
    assert len(message.execution_progress['steps']) == 1
    assert _message(asdict(message)).execution_progress == message.execution_progress


def test_trace_sink_failure_does_not_interrupt_model_logging():
    from app.agents.agent_client import ConversationTrace
    trace = ConversationTrace('r', '2026-10-23T10:00:00', 't', 'hello',
        on_progress=lambda *_: (_ for _ in ()).throw(RuntimeError('observer failed')))
    trace.add('模型请求', {'agent_role': 'main_execution'})
    assert len(trace.records) == 1


def test_real_model_request_adapter_publishes_progress_before_response(store, monkeypatch):
    from types import SimpleNamespace
    from app.agents import agent_client

    monkeypatch.setenv('OPENAI_API_KEY', 'test-only-key')
    monkeypatch.setattr(agent_client, 'is_llm_enabled', lambda: True)
    monkeypatch.setattr(agent_client, 'record_agent_run', lambda *args, **kwargs: None)
    task, _ = store.submit_task('trip-hangzhou', 'chat', {'body': '你好'}, 'adapter-test')

    def completion(*args):
        message = next(row for row in store.messages['trip-hangzhou'] if row.id == task['thinking_id'])
        assert message.execution_progress['steps'][-1]['status'] == 'running'
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='你好！', tool_calls=[]))], usage=None)

    monkeypatch.setattr(agent_client, 'create_completion', completion)
    assert store.run_task(task['id']).committed
    message = next(row for row in store.messages['trip-hangzhou'] if row.id == task['thinking_id'])
    assert message.execution_progress['steps'][-1]['status'] == 'succeeded'


def test_message_context_omits_ui_execution_history():
    from types import SimpleNamespace
    from app.agents.agent_runtime import TravelAgentRuntime
    from app.models import Message, User
    message = Message('m', User('u', 'user', 'u'), 'agent', '正文', '10:00',
                      execution_progress={'steps': ['UI-ONLY']})
    runtime = SimpleNamespace(trip=None, plan=None, snapshot={'messages': [message]})
    result = TravelAgentRuntime.read_context(runtime, {'section': 'messages'})
    assert result['items'][0]['body'] == '正文'
    assert 'execution_progress' not in result['items'][0]


def test_completed_progress_is_folded_and_query_details_are_escaped():
    from types import SimpleNamespace
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    from app.models import Message, User
    from app.views import chat_presentation

    progress = advance_progress(new_progress(timestamp=0), '工具开始', {
        'role': 'research', 'name': 'search_places', 'arguments': {'keywords': '<script>alert(1)</script>'},
    }, timestamp=1)
    progress = finish_progress(progress, 'failed', timestamp=1000)
    env = Environment(loader=FileSystemLoader('app/templates'), autoescape=select_autoescape())
    for name in ('chat_time_break', 'chat_reply_view', 'chat_actions', 'chat_supplemental', 'chat_progress_view', 'chat_duration'):
        env.filters[name] = getattr(chat_presentation, name)
    html = env.get_template('partials/chat_messages.html').render(
        messages=[Message('m', User('agent', '助手', 'AI'), 'agent', '本次查询未完成。', '10:00',
                          'failed', execution_progress=progress)], trip=SimpleNamespace(id='test'))
    assert '&lt;script&gt;' in html and '<script>' not in html
    assert 'data-progress-state="failed"' in html
    assert ' open' not in html
    assert '本次查询未完成。' in html
