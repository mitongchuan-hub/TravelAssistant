"""Chat-only business flow: trusted lodging, atomic writes and display wakeup."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.domain.changes import prepare_changes
from app.storage.store import DemoStore
from app.application.contracts import ExecutionResult
from tests.automated.test_web_contract import csrf_headers


@pytest.fixture
def store():
    return DemoStore(persistence_enabled=False, display_worker_enabled=False)


def commit_chat(store, body, args, evidence=()):
    key = 'trip-hangzhou'
    task, _ = store.submit_task(key, 'chat', {'body': body}, __import__('uuid').uuid4().hex)
    store.tasks[task['id']]['status'] = 'running'
    context = store.capture_request_context(key, task['id'])
    execution = SimpleNamespace(trip=deepcopy(store.trips[key]), plan=deepcopy(store.plans[key]),
                                idea_cards=deepcopy(store.trips[key].requirements.text), display_target=None)
    payload = prepare_changes({'user_quote': body, **args}, context, execution, evidence)
    return store.commit_turn(context, ExecutionResult(changes=payload))


def lodging_evidence():
    return [{'tool': 'search_places', 'arguments': {'city': '杭州', 'keywords': '测试酒店西湖店'},
             'result': {'status': 'available', 'candidates': [{
                 'poi_id': 'hotel-test', 'name': '测试酒店西湖店', 'address': '测试路1号',
                 'longitude': 120.1, 'latitude': 30.2, 'city': '杭州', 'district': '西湖区',
                 'source': '高德地图 POI', 'fetched_at': '2030-01-01'}]}}]


def test_chat_saves_map_matched_lodging(store):
    before = deepcopy(store.trips['trip-hangzhou'])
    with pytest.raises(ValueError):
        commit_chat(store, '我住测试酒店西湖店', {'lodging': {'poi_id': 'hotel-test'}}, lodging_evidence())
    assert store.trips['trip-hangzhou'].requirements == before.requirements
    assert store.trips['trip-hangzhou'].lodging == before.lodging


@pytest.mark.parametrize('body,evidence', [
    ('我住测试酒店西湖店', []),
    ('查询测试酒店西湖店附近景点', lodging_evidence()),
    ('不要把测试酒店西湖店作为住宿', lodging_evidence()),
    ('我住另一家酒店', lodging_evidence()),
])
def test_lodging_needs_actual_map_evidence_and_current_authority(store, body, evidence):
    before = deepcopy(store.trips['trip-hangzhou'])
    with pytest.raises(ValueError):
        commit_chat(store, body, {'lodging': {'poi_id': 'hotel-test'}}, evidence)
    assert store.trips['trip-hangzhou'].requirements == before.requirements
    assert store.trips['trip-hangzhou'].lodging == before.lodging


def test_same_named_lodging_requires_specific_address(store):
    evidence = lodging_evidence()
    candidate = evidence[0]['result']['candidates'][0]
    evidence[0]['result']['candidates'].append({**candidate, 'poi_id': 'other', 'address': '另一条路2号'})
    with pytest.raises(ValueError):
        commit_chat(store, '我住测试酒店西湖店', {'lodging': {'poi_id': 'hotel-test'}}, evidence)


def test_chat_itinerary_wakes_display_and_confirmation_uses_same_chat(store, monkeypatch):
    from tests.automated.test_single_agent_integration import ready_plan
    ready_plan(store)
    notified = []
    monkeypatch.setattr(DemoStore, '_notify_display_worker', lambda self: notified.append(True))
    cards = store.trips['trip-hangzhou'].requirements.text
    receipt = commit_chat(store, '请生成行程', {
        'itinerary': {'action': 'replace', 'text': '# 测试行程\n从住宿出发并返回。'}})
    assert receipt.committed and notified == [True]
    assert store.plans['trip-hangzhou'].text.startswith('# 测试行程')
    receipt = commit_chat(store, '确认当前行程', {'itinerary': {'action': 'confirm'}})
    assert receipt.committed and store.plans['trip-hangzhou'].status == '已确认'
    assert notified == [True]


def test_new_conversation_requires_no_travel_form_and_is_private(store, monkeypatch):
    from app.main import app
    from app.api import dependencies as deps
    owner = store.trips['trip-hangzhou'].owner
    monkeypatch.setattr(deps, 'store', store)
    monkeypatch.setattr(deps, 'current_user', lambda request: owner)
    with TestClient(app) as client:
        response = client.post('/api/trips/conversations', json={}, headers=csrf_headers(client))
        assert response.status_code in {404, 405}
        assert len(store.trips) == 1
