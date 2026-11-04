"""Offline batch scheduling: concurrent map I/O, ordered trusted state and barriers."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from copy import deepcopy
import json
from threading import Barrier, Lock, get_ident

import pytest

from test_readonly_chat_runtime import Script, call, runtime
from app.agents.budget import AgentBudgetError, TurnBudget


def test_independent_map_io_overlaps_and_context_is_copied_per_submit(monkeypatch):
    marker = ContextVar('batch_test_marker', default=None)
    marker.set('request')
    barrier = Barrier(3)
    main = get_ident()
    worker_ids = set()
    lock = Lock()
    def query(name, args, **kwargs):
        assert marker.get() == 'request'
        marker.set(args['keywords'])
        with lock:
            worker_ids.add(get_ident())
        barrier.wait(timeout=3)
        return {'pois': [{'id': args['keywords'], 'name': args['keywords']}]}
    monkeypatch.setattr('app.agents.tools.registry.run_tool', query)
    calls = [call('maps_text_search', {'city': '杭州', 'keywords': str(i)}, str(i)) for i in range(6)]
    script = Script(calls, '完成')
    runner = runtime(script)
    state_threads = []
    merge = runner._merge_places
    def merge_on_main(places):
        state_threads.append(get_ident())
        merge(places)
    runner._merge_places = merge_on_main
    assert not runner.run().failed
    assert len(worker_ids) == 3 and main not in worker_ids
    assert state_threads == [main] * 6
    assert marker.get() == 'request'
    assert [row['tool_call_id'] for row in script.transcripts[1] if row['role'] == 'tool'] == [str(i) for i in range(6)]
    assert len([row for row in script.transcripts[1] if row['role'] == 'assistant']) == 1
    assert runner.budget.tool_calls == runner.budget.external_calls == 6


def test_same_batch_search_detail_route_dependency_flushes_before_preparing(monkeypatch):
    seen = []
    def query(name, args, **kwargs):
        seen.append((name, deepcopy(args)))
        if name == 'maps_text_search':
            return {'pois': [{'id': 'a'}, {'id': 'b'}]}
        if name == 'maps_search_detail':
            return {'id': args['id'], 'name': args['id'], 'location': '1,1' if args['id'] == 'a' else '2,2', 'city': '杭州'}
        return {'paths': [{'duration': '60', 'distance': '10'}]}
    monkeypatch.setattr('app.agents.tools.registry.run_tool', query)
    script = Script([
        call('maps_text_search', {'city': '杭州', 'keywords': '景点'}, 's'),
        call('maps_search_detail', {'id': 'a'}, 'a'),
        call('maps_search_detail', {'id': 'b'}, 'b'),
        call('maps_direction_walking', {'origin': '1,1', 'destination': '2,2'}, 'r'),
    ], '完成')
    runner = runtime(script)
    assert not runner.run().failed
    assert [name for name, _ in seen][0] == 'maps_text_search'
    assert [name for name, _ in seen][-1] == 'maps_direction_walking'
    assert len(runner.evidence) == 4
    assert runner.evidence[-1]['normalized']['duration_minutes'] == 1


def test_batch_unknown_id_and_schema_errors_have_ordered_receipts(monkeypatch):
    seen = []
    monkeypatch.setattr('app.agents.tools.registry.run_tool', lambda name, args, **kw: seen.append(args) or {'pois': []})
    script = Script([
        call('maps_text_search', {'city': '杭州', 'keywords': '景点'}, 's'),
        call('maps_search_detail', {'id': 'invented'}, 'bad-id'),
        call('read_context', {'section': 'messages', 'extra': True}, 'bad-schema'),
        call('read_context', {'section': 'messages'}, 'local'),
    ], '未保存')
    runner = runtime(script)
    assert not runner.run().failed
    receipts = [row for row in script.transcripts[1] if row['role'] == 'tool']
    assert [row['tool_call_id'] for row in receipts] == ['s', 'bad-id', 'bad-schema', 'local']
    assert [json.loads(row['content']).get('status') for row in receipts][1:3] == ['invalid_arguments'] * 2
    assert len(seen) == 1


def test_terminal_is_existing_position_barrier_not_new_save_exclusivity(monkeypatch):
    completed = []
    def query(name, args, **kwargs):
        completed.append(args['keywords'])
        return {'pois': []}
    monkeypatch.setattr('app.agents.tools.registry.run_tool', query)
    script = Script([
        call('maps_text_search', {'city': '杭州', 'keywords': 'before'}, 'before'),
        call('save_trip_changes', {'user_quote': '记下夜骑', 'requirements_text': '夜骑'}, 'save'),
        call('maps_text_search', {'city': '杭州', 'keywords': 'after'}, 'after'),
        call('save_trip_changes', {'user_quote': '记下夜骑', 'requirements_text': '不覆盖'}, 'save2'),
    ])
    runner = runtime(script, body='记下夜骑')
    events = []
    runner.trace = type('Trace', (), {'add': lambda self, name, value: events.append((name, value))})()
    result = runner.run()
    assert not result.failed and result.changes['args']['requirements_text'] == '夜骑'
    assert completed == ['before'] and len(script.transcripts) == 1
    skipped = [value for _, value in events if value.get('status') == 'skipped_due_to_terminal']
    assert [row['tool_call_id'] for row in skipped] == ['after', 'save2']


def test_replaced_map_handler_remains_offline_execution_boundary(monkeypatch):
    from dataclasses import replace
    def forbidden(*args, **kwargs):
        raise AssertionError('replaced handler must not reach provider')
    monkeypatch.setattr('app.agents.tools.registry.run_tool', forbidden)
    runner = runtime(Script([call('maps_text_search', {'city': '杭州', 'keywords': '西湖'}, 'map')], '完成'))
    seen = []
    runner.registry['maps_text_search'] = replace(runner.registry['maps_text_search'],
        handler=lambda args: seen.append(args) or {'pois': []})
    assert not runner.run().failed
    assert seen == [{'city': '杭州', 'keywords': '西湖'}]


def test_same_batch_duplicates_reuse_cache_without_duplicate_io(monkeypatch):
    seen = []
    monkeypatch.setattr('app.agents.tools.registry.run_tool',
        lambda name, args, **kwargs: seen.append(args) or {'pois': []})
    args = {'city': '杭州', 'keywords': '西湖'}
    runner = runtime(Script([call('maps_text_search', args, 'a'), call('maps_text_search', args, 'b')], '完成'))
    assert not runner.run().failed
    assert len(seen) == runner.budget.external_calls == 1
    assert runner.budget.tool_calls == 2


def test_error_limit_still_records_all_batch_errors_but_never_saves():
    script = Script([
        call('read_context', {'section': 'invalid'}, 'a'),
        call('read_context', {'section': 'invalid'}, 'b'),
        call('read_context', {'section': 'messages'}, 'c'),
        call('save_trip_changes', {'user_quote': '记下夜骑', 'requirements_text': '夜骑'}, 'save'),
    ])
    runner = runtime(script, body='记下夜骑')
    result = runner.run()
    assert result.failed and not result.changes
    assert [row['tool_call_id'] for row in runner.messages if row['role'] == 'tool'] == ['a', 'b', 'c']


def test_weather_and_local_calls_are_barriers_without_batch_count_limit(monkeypatch):
    seen = []
    def query(name, args, **kwargs):
        seen.append((name, get_ident()))
        return {'pois': []} if name == 'maps_text_search' else {'status': 'available'}
    monkeypatch.setattr('app.agents.tools.registry.run_tool', query)
    calls = [call('maps_text_search', {'city': '杭州', 'keywords': '西湖'}, 'map')]
    calls += [call('query_weather', {'city': '杭州'}, f'w{i}') for i in range(5)]
    calls += [call('read_context', {'section': 'messages'}, f'l{i}') for i in range(5)]
    runner = runtime(Script(calls, '完成'))
    assert not runner.run().failed
    assert seen[0][0] == 'maps_text_search' and seen[0][1] != get_ident()
    assert all(thread == get_ident() for name, thread in seen if name == 'query_weather')
    assert runner.budget.tool_calls == 11


def test_budget_stays_deepcopyable_and_serializable_without_lock_leak():
    from dataclasses import asdict
    budget = TurnBudget(max_external=3)
    budget.external()
    snapshot = deepcopy(budget)
    assert snapshot._lock is not budget._lock
    assert asdict(snapshot)['external_calls'] == 1
    assert '_lock' not in asdict(snapshot)
    json.dumps(asdict(snapshot))


def test_external_budget_check_and_increment_are_atomic():
    budget = TurnBudget(max_external=3)
    def reserve(_):
        try:
            budget.external()
            return True
        except AgentBudgetError:
            return False
    with ThreadPoolExecutor(max_workers=16) as pool:
        accepted = list(pool.map(reserve, range(200)))
    assert sum(accepted) == budget.external_calls == 3
