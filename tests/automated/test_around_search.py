"""Offline around-search contracts; no model, Store or network."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.agents.tools import registry
from app.agents.inputs.context import project_evidence
from app.services.maps import amap_mcp, amap_rest
from tests.automated.test_readonly_chat_runtime import runtime, Script


CENTER = {'poi_id': 'center', 'name': '中心', 'longitude': 120.1, 'latitude': 30.2, 'city': '杭州'}
DETAIL = {'id': 'center', 'name': '中心', 'location': '120.1,30.2', 'city': '杭州',
          'entr_location': '120.11,30.21'}
ARGS = {'keywords': '餐饮', 'location': '120.1,30.2'}


def test_schema_is_actual_remote_contract():
    tool = next(t for t in registry._NATIVE if t['name'] == 'maps_around_search')
    assert tool['parameters'] == {'type': 'object', 'properties': {
        'keywords': {'type': 'string', 'description': '搜索关键词'},
        'location': {'type': 'string', 'description': '中心点经度纬度'},
        'radius': {'type': 'string', 'description': '搜索半径'},
        'strategy': {'type': 'integer', 'description': '召回策略，0=默认召回策略，1=优先召回扫街榜POI', 'default': 0}},
        'required': ['keywords', 'location']}
    assert len(amap_mcp.ALLOWED_TOOLS) == 6
    assert len(runtime(Script()).registry) == 10


@pytest.mark.parametrize('source', ['invented', 'search', 'entry', 'failed', 'mismatch', 'normalized_only'])
def test_untrusted_centers_never_reach_provider(monkeypatch, source):
    runner = runtime(Script())
    calls = []
    monkeypatch.setattr(amap_mcp, 'call', lambda *a: calls.append(a) or {'pois': []})
    if source == 'search':
        runner.catalog['center'] = deepcopy(CENTER)
    if source == 'entry':
        runner.search_ids.add('center')
        monkeypatch.setattr(amap_rest, 'call', lambda args: deepcopy(DETAIL))
        runner._query('maps_search_detail', {'id': 'center'})
        before = runner.budget.external_calls
    else:
        before = 0
    if source in {'failed', 'mismatch', 'normalized_only'}:
        raw = dict(DETAIL)
        if source == 'failed': raw['status'] = 'unavailable'
        if source == 'mismatch': raw['id'] = 'wrong'
        saved = [{'tool': 'maps_search_detail', 'arguments': {'id': 'center'},
                  'result': raw if source != 'normalized_only' else {},
                  'normalized': {'status': 'available', 'candidates': [CENTER]}}]
        runner.execution_context = replace(runner.execution_context, plan=SimpleNamespace(evidence=saved))
        runner = type(runner)(None, 'offline', runner.request_context, runner.execution_context, Script())
    args = dict(ARGS, location='120.11,30.21') if source == 'entry' else ARGS
    with pytest.raises(ValueError): runner._query('maps_around_search', args)
    assert not calls and runner.budget.external_calls == before


@pytest.mark.parametrize('source', ['lodging', 'current_detail', 'saved_detail'])
def test_trusted_center_search_ids_raw_projection_and_rest_dispatch(monkeypatch, source):
    runner = runtime(Script())
    calls = []
    raw = {'pois': [{'id': 'food', 'name': '餐厅', 'location': '120.2,30.3', 'cityname': '杭州'},
                    {'id': 'missing-coords', 'name': '补给'}, {'id': ''}, {'id': ' '}, {'id': None}]}
    monkeypatch.setattr(amap_mcp, 'call', lambda name, args: calls.append((name, deepcopy(args))) or deepcopy(raw))
    monkeypatch.setattr(amap_rest, 'call', lambda args: calls.append(('REST', deepcopy(args))) or
                        (deepcopy(DETAIL) if args['id'] == 'center' else
                         {'id': args['id'], 'name': '餐厅', 'location': '120.2,30.3', 'city': '杭州'}))
    if source == 'lodging':
        runner.trip.lodging = deepcopy(CENTER)
        runner._merge_places({})
    elif source == 'current_detail':
        runner.search_ids.add('center')
        runner._query('maps_search_detail', {'id': 'center'})
    else:
        runner.execution_context = replace(runner.execution_context, plan=SimpleNamespace(evidence=[{
            'tool': 'maps_search_detail', 'arguments': {'id': 'center'}, 'result': deepcopy(DETAIL)}]))
        runner = type(runner)(None, 'offline', runner.request_context, runner.execution_context, Script())
    before = len(calls)
    assert runner._query('maps_around_search', ARGS) == raw
    assert runner._query('maps_around_search', ARGS) == raw
    assert len(calls) == before + 1
    assert runner.search_ids >= {'food', 'missing-coords'}
    assert not {'', ' ', None} & runner.search_ids
    assert 'missing-coords' not in runner.catalog
    row = runner.evidence[-1]
    assert row['result'] == raw and row['queried_at']
    candidate = project_evidence([row])['items'][0]['facts']['candidates'][0]
    assert candidate['poi_id'] == 'food' and candidate['longitude'] == 120.2
    assert candidate['latitude'] == 30.3
    unlocated = project_evidence([row])['items'][0]['facts']['candidates'][1]
    assert unlocated['poi_id'] == 'missing-coords'
    assert 'longitude' not in unlocated and 'latitude' not in unlocated
    with pytest.raises(ValueError):
        runner._query('maps_around_search', dict(ARGS, location='120.2,30.3'))
    runner._query('maps_search_detail', {'id': 'food'})
    assert calls[-1] == ('REST', {'id': 'food'})


def test_around_failure_not_cached_and_progress_safe(monkeypatch):
    from app.application.progress import advance_progress, new_progress
    runner = runtime(Script())
    runner.trip.lodging = deepcopy(CENTER)
    runner._merge_places({})
    calls = []
    monkeypatch.setattr(amap_mcp, 'call', lambda *a: calls.append(a) or {'status': 'unavailable'})
    for _ in range(2): runner._query('maps_around_search', ARGS)
    assert len(calls) == runner.budget.external_calls == 2
    assert not runner.external_results
    progress = advance_progress(new_progress(), '工具开始', {
        'name': 'maps_around_search', 'role': 'travel_agent',
        'arguments': dict(ARGS, radius='500', secret='do not expose')})
    assert progress['steps'][0]['label'] == '查询周边地点'
    assert '餐饮' in progress['steps'][0]['detail']
    assert '500' in progress['steps'][0]['detail']
    assert '120.1' not in progress['steps'][0]['detail']
    assert 'secret' not in progress['steps'][0]['detail']


@pytest.mark.parametrize('failure', ['timeout', 'budget', 'cancel'])
def test_around_exceptions_are_not_cached(monkeypatch, failure):
    from app.agents.budget import AgentBudgetError
    runner = runtime(Script())
    runner.trip.lodging = deepcopy(CENTER)
    runner._merge_places({})
    error = TimeoutError if failure == 'timeout' else AgentBudgetError
    def fail(*args, **kwargs):
        if failure == 'cancel':
            runner.budget.cancelled = lambda: True
            runner.budget.remaining()
        if failure == 'budget':
            runner.budget.max_external = 1
            runner.budget.external()
        raise error(failure)
    monkeypatch.setattr(amap_mcp, 'call', fail)
    with pytest.raises(error):
        runner._query('maps_around_search', ARGS)
    assert not runner.external_results and not runner.evidence
    assert runner.budget.external_calls == 1


def test_saved_unlocated_search_id_can_fetch_detail(monkeypatch):
    runner = runtime(Script())
    runner.execution_context = replace(runner.execution_context, plan=SimpleNamespace(evidence=[{
        'tool': 'maps_around_search', 'result': {'pois': [{'id': 'food', 'name': '餐厅'}]}}]))
    runner = type(runner)(None, 'offline', runner.request_context, runner.execution_context, Script())
    monkeypatch.setattr(amap_rest, 'call', lambda args: dict(DETAIL, id=args['id']))
    assert runner._query('maps_search_detail', {'id': 'food'})['id'] == 'food'


def test_prompts_include_around_contract():
    from app.agents.roles import travel_role
    prompt = travel_role().prompt
    for text in ('maps_around_search', '半径', '指代', '过敏', '步行'):
        assert text in prompt
