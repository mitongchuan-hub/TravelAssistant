"""Validate v2 execution contracts and newly covered deletion safety."""
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
import importlib
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from app.auth import AuthStore
from app.persistence import load_snapshot
from app.store import DemoStore
from test_agent_roles import completion, display_data

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from travel_dataset_support import load_dataset, resolve_fixture, create_fixture, controlled_tool, validate_dataset
from run_travel_dataset import machine_checks
from run_travel_dataset_batch import select_cases, snapshot_source

DATA = load_dataset()
main_module = importlib.import_module('app.main')


def test_dataset_profiles_and_review_template_cover_every_case():
    assert len(DATA['cases']) == 70
    assert Counter(c['execution_mode'] for c in DATA['cases']) == {
        'live_agent': 36, 'live_display': 10, 'controlled': 16, 'spec_only': 8,
    }
    assert len(select_cases(DATA, 'current')) == 62
    assert all(c['acceptance'] == 'target' for c in select_cases(DATA, 'target'))
    assert not set(DATA['profiles']['current']) & set(DATA['profiles']['target'])
    template = json.loads(Path('tests/datasets/travel_agent_v2/review-template.json').read_text(encoding='utf-8'))
    assert {r['case_id'] for r in template['reviews']} == {
        c['id'] for c in DATA['cases'] if c['execution_mode'] in {'live_agent', 'live_display'}
    }
    assert all(r['grade'] == 'pending_review' for r in template['reviews'])


@pytest.mark.parametrize('fixture', list(DATA['fixtures']))
def test_dataset_fixtures_match_spec_without_hardcoded_dates_or_people(fixture):
    spec = resolve_fixture(DATA, fixture)
    local, tid = create_fixture(DATA, fixture)
    assert local.persistence_enabled is False
    profile = asdict(local.trips[tid].requirements)
    for field, value in spec.get('requirements', {}).items():
        assert profile[field] == value, (fixture, field)
    if spec.get('plan_text'):
        assert local.plans[tid].text == spec['plan_text']
    if fixture == 'cross_month':
        assert profile['day_count'] == 3
    if fixture == 'airport':
        assert profile['end_date'] == '2026-11-16' and profile['arrival_location'] == '萧山机场'
    if fixture == 'display_d009':
        assert len(spec['source_text']) > 10000
        assert spec['source_text'].endswith('成分与交叉污染未确认。')


def test_dataset_validator_rejects_duplicate_ids_missing_nodes_and_cycles():
    bad = deepcopy(DATA)
    bad['cases'].append(deepcopy(bad['cases'][0]))
    with pytest.raises(ValueError, match='Duplicate'):
        validate_dataset(bad)
    bad = deepcopy(DATA)
    bad['fixtures']['empty']['inherits'] = 'empty'
    with pytest.raises(ValueError, match='cycle'):
        validate_dataset(bad)
    bad = deepcopy(DATA)
    next(c for c in bad['cases'] if c['execution_mode'] == 'controlled')['pytest_nodes'] = []
    with pytest.raises(ValueError, match='mapping'):
        validate_dataset(bad)


def test_dataset_lodging_query_returns_price_even_when_station_is_in_keyword():
    for keyword in ('独立双床酒店', '杭州东站附近独立双床酒店'):
        result = controlled_tool(DATA, 'budget_insufficient', 'search_places', {'city': '杭州', 'keywords': keyword})
        assert result['minimum_price_yuan'] == 300 and result['candidates'] == []
        assert result['fetched_at'].startswith(DATA['current_date'])
    assert 'minimum_price_yuan' not in controlled_tool(DATA, 'baseline', 'search_places', {'city': '杭州', 'keywords': '杭州东站'})


def test_dataset_synthetic_pois_enter_current_catalog_and_allow_route_lookup():
    from app.places import collect_places
    results = [controlled_tool(DATA, 'baseline', 'search_places', {'city': '杭州', 'keywords': name})
               for name in ('杭州东站', '西湖湖滨')]
    catalog = collect_places([{'role': 'tool', 'name': 'search_places',
                              'content': json.dumps(result, ensure_ascii=False)} for result in results])
    assert set(catalog) == {'test-station', 'test-lake'}
    route = controlled_tool(DATA, 'baseline', 'query_route', {
        'origin_poi_id': 'test-station', 'destination_poi_id': 'test-lake', 'mode': 'transit'}, place_catalog=catalog)
    assert route['status'] == 'available' and route['duration_minutes'] == 38


def test_dataset_machine_checks_reject_successful_but_broadened_revision():
    local, tid = create_fixture(DATA, 'one_day_saved')
    before = {'trip': asdict(local.trips[tid]), 'plan': asdict(local.plans[tid])}
    after = deepcopy(before)
    step = next(c for c in DATA['cases'] if c['id'] == 'TA2-041')['steps'][0]
    after['plan']['text'] += step['assertions']['text_append']
    assert all(machine_checks(step, before, after, [], [], {}).values())
    after['plan']['text'] = '重写了整份行程' + step['assertions']['text_append']
    assert not machine_checks(step, before, after, [], [], {})['exact_text_append']
    step = next(c for c in DATA['cases'] if c['id'] == 'TA2-029')['steps'][0]
    after['trip']['requirements']['budget_amount'] = '2500'
    assert not machine_checks(step, before, after, [], [], {'after_requirements': False})['injection_triggered']


def test_dataset_source_snapshot_excludes_env_and_backup(monkeypatch, tmp_path):
    batch = importlib.import_module('run_travel_dataset_batch')
    monkeypatch.setattr(batch, 'ROOT', tmp_path)
    (tmp_path / '.env.bak-xinshu').write_text('fixture secret', encoding='utf-8')
    (tmp_path / 'code.py').write_text('pass', encoding='utf-8')
    monkeypatch.setattr(batch.subprocess, 'check_output', lambda *a, **kw: b'.env.bak-xinshu\0code.py\0')
    out = tmp_path / 'output'
    out.mkdir()
    snapshot_source(out)
    assert not (out / 'source/.env.bak-xinshu').exists()
    assert (out / 'source/code.py').read_text(encoding='utf-8') == 'pass'


@pytest.mark.parametrize('case_id', ['TA2-040', 'TA2-D001'])
def test_dataset_runner_records_isolated_results_and_timings(monkeypatch, tmp_path, case_id):
    runner = importlib.import_module('run_travel_dataset')
    source = resolve_fixture(DATA, 'display_d001')['source_text'] if case_id == 'TA2-D001' else '完整单日行程'
    outputs = iter([completion(display_data(('2026-11-14',)))] if case_id == 'TA2-D001' else
                   [completion(source), completion(display_data(('2026-11-14',)))])
    def request(*a, **kw):
        kw['budget'].model()
        response = next(outputs)
        response.usage = SimpleNamespace(prompt_tokens=100, completion_tokens=50,
                                        completion_tokens_details=SimpleNamespace(reasoning_tokens=None))
        return response
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def close(self): pass
    monkeypatch.setattr('openai.OpenAI', Client)
    monkeypatch.setattr(runner.agent_client, '_create_chat_completion', request)
    monkeypatch.setattr(runner.agent_client, 'is_llm_enabled', lambda: True)
    # Register these attributes for restoration after main installs its observers.
    monkeypatch.setattr(runner.agent_client.ConversationTrace, 'add', runner.agent_client.ConversationTrace.add)
    monkeypatch.setattr(runner.agent_client.ConversationTrace, 'write', runner.agent_client.ConversationTrace.write)
    monkeypatch.setattr(runner.TravelAgentRuntime, '__init__', runner.TravelAgentRuntime.__init__)
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'dataset-runner-controlled')
    monkeypatch.setenv('TRAVEL_AGENT_DISABLE_LLM', '1')
    monkeypatch.setenv('PYTHONDONTWRITEBYTECODE', '1')
    monkeypatch.setattr(runner, 'business_fingerprint', lambda: 'unchanged')
    out = tmp_path / 'run'
    monkeypatch.setattr(sys, 'argv', ['run', '--case', case_id, '--output', str(out)])
    assert runner.main() == 0
    result = json.loads((out / 'result.json').read_text(encoding='utf-8'))
    assert result['status'] == 'executed_pending_review'
    assert result['machine_checks_pass'] and result['business_db_unchanged']
    assert not result['isolation']['persistence_enabled']
    assert result['model_calls'][-1]['usage']['completion_tokens'] == 50
    timing = result['steps'][0]['timing']
    assert timing['display_seconds'] > 0 and timing['external_seconds'] == 0
    assert timing['model_seconds'] >= timing['display_seconds']
    if case_id == 'TA2-D001':
        assert result['steps'][0]['display_conversion']['source_text'] == source
        assert not result['steps'][0]['plan_changed']
    else:
        assert result['steps'][0]['after']['plan']['text'] == source


def test_delete_trip_dataset_cascade(tmp_path):
    local, tid = create_fixture(DATA, 'saved')
    other, other_tid = create_fixture(DATA, 'one_day_saved')
    # Use a second independent trip in the same Store to check deletion scope.
    trip = local.create_trip('保留旅行', '', '', '', '')
    local._apply_text_plan(trip.id, other.plans[other_tid].text)
    retained = deepcopy(local.plans[trip.id])
    local.add_user_message(tid, '待删除的消息', process_agent=False)
    local.add_idea(tid, '待删除的想法')
    task, _ = local.submit_task(tid, 'chat', {'body': '终态任务'}, 'delete-completed')
    local.tasks[task['id']]['status'] = 'failed'
    local._agent_locks[tid] = object()
    local.db_path = str(tmp_path / 'delete.sqlite3')
    local.persistence_enabled = True
    local._persist()
    assert local.delete_trip(tid)
    for mapping in (local.trips, local.messages, local.plans, local.idea_cards, local._agent_locks):
        assert tid not in mapping
    assert task['id'] not in local.tasks
    assert local.plans[trip.id] == retained
    reopened = DemoStore(db_path=local.db_path, persistence_enabled=True)
    assert tid not in reopened.trips and reopened.plans[trip.id] == retained
    assert tid not in load_snapshot(local.db_path)['trips']
    with sqlite3.connect(local.db_path) as connection:
        for table in ('messages', 'ideas', 'plans'):
            assert connection.execute(f'SELECT COUNT(*) FROM {table} WHERE trip_id=?', (tid,)).fetchone()[0] == 0
    assert local.delete_trip(tid) is False


def test_delete_trip_dataset_ownership(monkeypatch):
    local, tid = create_fixture(DATA, 'saved')
    user = local.create_user('其他用户')
    other = local.create_trip('他人旅行', '', '', '', '', user)
    auth = AuthStore('unused.sqlite3', False)
    password = 'dataset-v2-password'
    auth.create_account('owner-v2@example.com', local.trips[tid].owner.id, password)
    auth.create_account('other-v2@example.com', user.id, password)
    monkeypatch.setattr(main_module, 'store', local)
    monkeypatch.setattr(main_module, 'auth', auth)
    with TestClient(main_module.app) as client:
        response = client.post(f'/trips/{tid}/delete', follow_redirects=False)
        assert response.status_code == 303 and response.headers['location'] == '/'
        assert tid in local.trips
        client.post('/login', data={'email': 'other-v2@example.com', 'password': password})
        assert client.post(f'/trips/{tid}/delete').status_code == 404
        assert tid in local.trips
        client.post('/login', data={'email': 'owner-v2@example.com', 'password': password})
        assert 'delete-trip-button' in client.get('/trips').text
        response = client.post(f'/trips/{tid}/delete', follow_redirects=False)
        assert response.status_code == 303
        assert '旅行已删除' in client.get(response.headers['location']).text
    assert tid not in local.trips and other.id in local.trips
    assert auth.email_exists('owner-v2@example.com') and auth.email_exists('other-v2@example.com')


@pytest.mark.parametrize('status', ['queued', 'running'])
def test_delete_trip_dataset_active_task(status):
    local, tid = create_fixture(DATA, 'saved')
    task, _ = local.submit_task(tid, 'plan', {}, 'delete-active')
    local.tasks[task['id']]['status'] = status
    before = deepcopy((local.trips, local.messages, local.plans, local.idea_cards, local.tasks))
    with pytest.raises(ValueError, match='任务正在进行'):
        local.delete_trip(tid)
    assert (local.trips, local.messages, local.plans, local.idea_cards, local.tasks) == before
