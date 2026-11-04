"""Offline scripted model -> real runtime -> atomic commit -> SQLite reload.

All writes use a temporary SQLite database; all model and query output is local.
"""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.agents.budget import TurnBudget
from app.agents.inputs.execution import TaskExecutionContext
from app.agents.travel_runtime import TaskExecutionRuntime
from app.services.itinerary.plan_display import text_hash
from app.storage.store import DemoStore
from app.application.execution import resolve_display_target
from app.application.execution import TurnPersistenceError


TRIP = 'trip-hangzhou'


class ScriptedModel:
    """Only transport is fake; schema validation, runtime and commit are real."""
    def __init__(self, *steps):
        self.steps = iter(steps)
        self.transcripts = []

    def __call__(self, client, model, messages, **kwargs):
        kwargs['budget'].model()
        self.transcripts.append(deepcopy(messages))
        step = next(self.steps)
        if isinstance(step, str):
            message = SimpleNamespace(content=step, tool_calls=[])
        else:
            name, args = step
            call = SimpleNamespace(id='call-' + str(len(self.transcripts)), type='function',
                function=SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False)))
            message = SimpleNamespace(content=None, tool_calls=[call])
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason='stop')])


@pytest.fixture
def store(tmp_path, monkeypatch):
    # Neither the formal database nor a live model/external service is reachable.
    monkeypatch.setenv('TRAVEL_PERSISTENCE', '0')
    monkeypatch.setenv('CITY_DB_PATH', str(tmp_path / 'integration.sqlite3'))
    monkeypatch.setenv('TRAVEL_AGENT_DISABLE_LLM', '1')
    def forbidden(*args, **kwargs):
        raise AssertionError('Unexpected external query in offline integration test')
    monkeypatch.setattr('app.agents.tools.registry.run_tool', forbidden)
    return DemoStore(db_path=str(tmp_path / 'integration.sqlite3'), persistence_enabled=True,
                     display_worker_enabled=False)


def reload(store):
    return DemoStore(db_path=store.db_path, persistence_enabled=True, display_worker_enabled=False)


def runtime_for(store, body, model, *, kind='chat', payload=None, budget=None):
    payload = payload or {'body' if kind == 'chat' else 'feedback': body}
    if kind == 'revision':
        # Historical target-bound rows remain readable; no retry or new revision
        # submission is supported. Exercise their existing authorization guard.
        task, _ = store.submit_task(TRIP, 'chat', {'body': body}, uuid4().hex)
        store.tasks[task['id']].update(kind='revision', payload=deepcopy(payload))
    else:
        task, _ = store.submit_task(TRIP, kind, payload, uuid4().hex)
    store.tasks[task['id']]['status'] = 'running'
    context = store.capture_request_context(TRIP, task['id'])
    execution = TaskExecutionContext(
        trip=deepcopy(store.trips[TRIP]), plan=deepcopy(store.plans[TRIP]),
        messages=deepcopy(store.messages[TRIP]),
        metrics={}, current_date='2030-01-01', entry=context.entry,
        display_target=resolve_display_target(store.plans[TRIP], payload) if kind == 'revision' else None)
    return TaskExecutionRuntime(None, 'offline-script', context, execution, model, budget=budget)


def run_commit(store, body, args, **kwargs):
    model = ScriptedModel(('submit_changes', {'user_quote': body, **args}), '未保存任何修改。')
    runtime = runtime_for(store, body, model, **kwargs)
    proposal = runtime.run()
    receipt = store.commit_turn(runtime.request_context, proposal)
    return runtime, proposal, receipt


def ready_plan(store):
    trip = store.trips[TRIP]
    trip.requirements.city = '杭州'
    trip.requirements.lodging_address = '湖滨路1号'
    trip.requirements.start_date = trip.requirements.end_date = '2030-05-01'
    trip.requirements.day_count = 1
    trip.requirements.transport_mode = 'transit'
    trip.lodging = {'name': '湖滨酒店', 'poi_id': 'hotel-test', 'city': '杭州',
                    'longitude': 120.15, 'latitude': 30.25}


def test_scripted_sparse_requirement_and_idea_survive_reload_and_replay(store):
    body = '记下晚上夜骑这个想法，并游玩天数改为2天'
    runtime, proposal, receipt = run_commit(store, body, {
        'requirements_text': '晚上夜骑'})
    assert receipt.committed and not proposal.failed
    restored = reload(store)
    assert restored.trips[TRIP].requirements.day_count is None
    assert restored.trips[TRIP].requirements.text == '晚上夜骑'
    assert restored.get_task(runtime.request_context.execution_task_id)['committed_effect_kinds'] == ['requirements']
    assert restored.commit_turn(runtime.request_context, proposal) == receipt
    assert len(runtime.request.transcripts) == 1


def test_scripted_invalid_submission_can_correct_without_saving_subset(store):
    body = '记下晚上夜骑这个想法'
    model = ScriptedModel(('submit_changes', {'user_quote': 'wrong', 'ideas': [
        {'action': 'create', 'kind': '地点', 'title': '错误', 'body': '不应保存'}]}),
        ('submit_changes', {'user_quote': body, 'requirements_text': '晚上夜骑'}))
    runtime = runtime_for(store, body, model)
    proposal = runtime.run()
    assert not proposal.failed
    assert json.loads(model.transcripts[1][-1]['content'])['status'] == 'invalid_arguments'
    store.commit_turn(runtime.request_context, proposal)
    text = reload(store).trips[TRIP].requirements.text
    assert text == '晚上夜骑'
    assert '不应保存' not in text


def test_scripted_exact_edit_reload_preserves_requirement_basis(store):
    store._apply_text_plan(TRIP, '09:00 西湖\n10:00 龙井')
    basis = deepcopy(store.plans[TRIP].requirements_snapshot)
    _, proposal, receipt = run_commit(store, '把“09:00 西湖”改为“09:30 西湖”', {
        'itinerary': {'action': 'edit_text', 'operation': 'replace',
                      'old_text': '09:00 西湖', 'new_text': '09:30 西湖'}})
    assert receipt.committed and not proposal.failed
    plan = reload(store).plans[TRIP]
    assert plan.text == '09:30 西湖\n10:00 龙井'
    assert plan.requirements_snapshot == basis


def test_scripted_coverage_omission_rejected_without_requirement_subset(store):
    ready_plan(store)
    before = deepcopy(store.trips[TRIP].requirements)
    _, proposal, _ = run_commit(store, '游玩改为2天，并生成行程', {
        'requirements_patch': {'day_count': 2},
        'itinerary': {'action': 'replace', 'text': '上午西湖'}})
    assert not proposal.changes
    assert reload(store).trips[TRIP].requirements == before


def test_scripted_valid_coverage_and_new_idea_survive_reload(store):
    ready_plan(store)
    text = '上午西湖，晚上夜骑。'
    _, proposal, receipt = run_commit(store, '记录晚上夜骑，并重新规划行程', {
        'requirements_text': '晚上夜骑',
        'itinerary': {'action': 'replace', 'text': text}})
    assert receipt.committed and not proposal.failed
    restored = reload(store)
    assert restored.plans[TRIP].text == text
    assert '晚上夜骑' in restored.trips[TRIP].requirements.text


def test_scripted_stale_idea_write_is_rejected_atomically(store):
    body = '记录慢游龙井'
    runtime = runtime_for(store, body, ScriptedModel(('submit_changes', {'user_quote': body, 'requirements_text': '慢游龙井'})))
    proposal = runtime.run()
    store.save_requirements_text(TRIP, '并发修改', store.trips[TRIP].requirements.revision)
    before = deepcopy(store.trips[TRIP].requirements)
    receipt = store.commit_turn(runtime.request_context, proposal)
    assert not receipt.committed
    assert store.trips[TRIP].requirements == before
    assert reload(store).trips[TRIP].requirements.text == '并发修改'


def test_scripted_persistence_failure_never_publishes_or_reloads_subset(store, monkeypatch):
    body = '记下晚上夜骑这个想法，并游玩天数改为2天'
    args = {'user_quote': body, 'requirements_text': '晚上夜骑'}
    runtime = runtime_for(store, body, ScriptedModel(('submit_changes', args)))
    proposal = runtime.run()
    before = deepcopy((store.trips, store.plans, store.trips, store.messages, store.tasks))
    def fail(*args, **kwargs):
        raise OSError('offline simulated disk full')
    monkeypatch.setattr('app.application.execution.save_snapshot', fail)
    with pytest.raises(TurnPersistenceError):
        store.commit_turn(runtime.request_context, proposal)
    assert (store.trips, store.plans, store.trips, store.messages, store.tasks) == before
    from app.storage.persistence import save_snapshot
    monkeypatch.setattr('app.application.execution.save_snapshot', save_snapshot)
    restored = reload(store)
    assert not '晚上夜骑' in restored.trips[TRIP].requirements.text
    assert restored.trips[TRIP].requirements.day_count is None


def test_scripted_query_budget_exhaustion_cannot_save_next_submission(store, monkeypatch):
    ready_plan(store)
    queried = []
    monkeypatch.setattr('app.agents.tools.registry.run_tool', lambda *a, **kw: queried.append(a) or {'status': 'available'})
    body = '记录夜骑，并查询西湖'
    runtime = runtime_for(store, body, ScriptedModel(
        ('maps_text_search', {'city': '杭州', 'keywords': '西湖'}),
        ('submit_changes', {'user_quote': body, 'ideas': [
            {'action': 'create', 'kind': '地点', 'title': '夜骑', 'body': '夜骑'}]})),
        budget=TurnBudget(max_external=0))
    proposal = runtime.run()
    assert proposal.failed and not proposal.changes and not queried
    store.commit_turn(runtime.request_context, proposal)
    assert '夜骑' not in reload(store).trips[TRIP].requirements.text


def test_selected_display_stop_same_line_exact_edit_survives_reload(store):
    text = '第1天\n09:00 西湖\n10:00 龙井'
    store._apply_text_plan(TRIP, text)
    plan = store.plans[TRIP]
    plan.display = {'days': [{'label': '第1天', 'date': '', 'items': [
        {'title': '西湖', 'time_text': '09:00', 'location': '西湖'},
        {'title': '龙井', 'time_text': '10:00', 'location': '龙井'}]}]}
    plan.display_status = 'ready'
    plan.display_source_hash = text_hash(text)
    body = '请把出发时间改为“11:00”'
    _, proposal, receipt = run_commit(store, body, {'itinerary': {
        'action': 'edit_text', 'operation': 'replace', 'old_text': '09:00',
        'context': '09:00 西湖', 'new_text': '11:00'}}, kind='revision', payload={
            'feedback': body, 'display_target': {'day_index': 0, 'item_index': 0},
            'expected_plan_id': plan.id, 'source_hash': text_hash(text),
            'expected_requirements_revision': store.trips[TRIP].requirements.revision})
    assert receipt.committed and not proposal.failed and proposal.changes
    assert reload(store).plans[TRIP].text == '第1天\n11:00 西湖\n10:00 龙井'


def test_selected_display_stop_cannot_edit_another_stop_through_broad_context(store):
    text = '第1天\n09:00 西湖\n10:00 龙井'
    store._apply_text_plan(TRIP, text)
    plan = store.plans[TRIP]
    plan.display = {'days': [{'label': '第1天', 'date': '', 'items': [
        {'title': '西湖', 'time_text': '09:00', 'location': '西湖'},
        {'title': '龙井', 'time_text': '10:00', 'location': '龙井'}]}]}
    plan.display_status = 'ready'
    plan.display_source_hash = text_hash(text)
    body = '请把出发时间改为“11:00”'
    _, proposal, _ = run_commit(store, body, {'itinerary': {
        'action': 'edit_text', 'operation': 'replace', 'old_text': '10:00',
        'context': text, 'new_text': '11:00'}}, kind='revision', payload={
            'feedback': body, 'display_target': {'day_index': 0, 'item_index': 0},
            'expected_plan_id': plan.id, 'source_hash': text_hash(text),
            'expected_requirements_revision': store.trips[TRIP].requirements.revision})
    assert not proposal.changes
    assert reload(store).plans[TRIP].text == text


def test_scripted_greeting_cannot_create_model_invented_idea(store):
    _, _, _ = run_commit(store, '你好', {'ideas': [
        {'action': 'create', 'kind': '地点', 'title': '私人直升机', 'body': '包私人直升机游览'}]})
    assert '私人直升机' not in reload(store).trips[TRIP].requirements.text


def test_unrelated_delete_instruction_does_not_authorize_card_deletion(store):
    _, _, _ = run_commit(store, '删除午餐安排，但保留龙井茶园想法', {
        'ideas': [{'action': 'delete', 'idea_id': 'idea-place-2'}]})
    assert reload(store).trips[TRIP].requirements.text == store.trips[TRIP].requirements.text


def test_unrelated_requirement_field_cannot_piggyback_on_authorized_addition(store):
    transport = store.trips[TRIP].requirements.transport_mode
    _, proposal, _ = run_commit(store, '游玩天数改为2天', {
        'requirements_patch': {'day_count': 2, 'transport_mode': 'driving'}})
    assert not proposal.changes
    restored = reload(store)
    assert restored.trips[TRIP].requirements.transport_mode == transport
    assert restored.trips[TRIP].requirements.day_count is None


def test_missing_transit_details_cannot_publish_fabricated_terminal_itinerary(store, monkeypatch):
    ready_plan(store)
    hotel = deepcopy(store.trips[TRIP].lodging)
    lake = {'name': '西湖', 'poi_id': 'lake-test', 'city': '杭州',
            'longitude': 120.14, 'latitude': 30.24, 'address': '西湖景区', 'district': '西湖区',
            'source': '高德地图 POI', 'fetched_at': '2030-01-01T00:00:00+00:00'}
    before = store.plans[TRIP].text
    calls = []
    def query(name, arguments, **kwargs):
        calls.append(name)
        if name == 'maps_text_search':
            return {'pois': [{'id': lake['poi_id'], 'name': lake['name']}]}
        if name == 'maps_search_detail':
            return {'id': lake['poi_id'], 'name': lake['name'], 'city': lake['city'],
                    'location': f"{lake['longitude']},{lake['latitude']}"}
        assert name == 'maps_direction_transit_integrated'
        return {'transits': [{'duration': '1500', 'distance': '5000'}]}
    monkeypatch.setattr('app.agents.tools.registry.run_tool', query)
    body = '请查询公交线路并生成行程'
    text = '09:00 在湖滨站乘坐999路公交，在西湖站下车。'
    model = ScriptedModel(('maps_text_search', {'city': '杭州', 'keywords': '西湖'}),
        ('maps_search_detail', {'id': lake['poi_id']}),
        ('maps_direction_transit_integrated', {'origin': '120.15,30.25', 'destination': '120.14,30.24', 'city': '杭州', 'cityd': '杭州'}),
        ('submit_changes', {'user_quote': body, 'itinerary': {'action': 'replace', 'text': text}}),
        '公交线路明细待核实，本轮不保存新行程。')
    runtime = runtime_for(store, body, model)
    proposal = runtime.run()
    assert calls == ['maps_text_search', 'maps_search_detail', 'maps_direction_transit_integrated']
    assert not proposal.changes
    store.commit_turn(runtime.request_context, proposal)
    restored = reload(store)
    assert restored.plans[TRIP].text == before
    assert all('999路' not in row.body for row in restored.messages[TRIP] if row.sender_type == 'agent')


def test_web_message_background_task_runs_real_runtime_and_commit(store, monkeypatch):
    from app import main
    from app.api import dependencies as deps
    from app.agents import entrypoints
    calls = []
    def generate(context, execution, **kwargs):
        model = ScriptedModel(('read_context', {'section': 'requirements'}),
            ('submit_changes', {'user_quote': context.body, 'requirements_text': '晚上夜骑'}))
        runtime = TaskExecutionRuntime(None, 'offline-script', context, execution, model)
        proposal = runtime.run()
        calls.append(runtime)
        return proposal
    monkeypatch.setattr(entrypoints, 'generate_task_result', generate)
    monkeypatch.setattr(deps, 'store', store)
    monkeypatch.setattr(deps, 'current_user', lambda request: store.trips[TRIP].owner)
    from tests.automated.test_web_contract import csrf_headers
    with TestClient(main.app) as client:
        headers = csrf_headers(client)
        response = client.post(f'/api/trips/{TRIP}/messages', json={
            'body': '记下晚上夜骑这个想法', 'request_id': 'offline-web-request'}, headers=headers)
        assert response.status_code == 202
        task_id = response.json()['task']['id']
        assert store.get_task(task_id)['status'] == 'succeeded'
        assert len(calls) == 1 and len(calls[0].request.transcripts) == 2
        messages = client.get(f'/api/trips/{TRIP}/messages').json()['messages']
        assert any('需求正文已保存' in row['body'] for row in messages)
        # Identical HTTP request replays task identity without a second model run.
        duplicate = client.post(f'/api/trips/{TRIP}/messages', json={
            'body': '记下晚上夜骑这个想法', 'request_id': 'offline-web-request'}, headers=headers)
        assert duplicate.json()['task']['id'] == task_id
        assert len(calls) == 1
    restored = reload(store)
    assert restored.trips[TRIP].requirements.text == '晚上夜骑'
    assert restored.get_task(task_id)['committed_effect_kinds'] == ['requirements']
