"""Offline evaluator contracts; no test may reach an external fact provider."""
import json
from types import SimpleNamespace

import pytest

from tests import scope_eval


def response(content=None, name=None, arguments=None):
    calls = [] if name is None else [SimpleNamespace(id='call', type='function',
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments or {})))]
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls))])


@pytest.mark.parametrize('structured', [False, True])
def test_scope_eval_exercises_real_readonly_terminal_without_business_changes(monkeypatch, structured):
    monkeypatch.setattr(scope_eval, '_create_chat_completion', lambda *a, **kw: response('Hello'))
    result = scope_eval.run_case(('greeting', 'Hello', 'Greeting'), None, 'offline', structured)
    assert not result['failed'], result['errors']
    assert result['answer'] == 'Hello'
    assert result['unchanged']
    assert result['selected'] == [] and result['calls'] == []


@pytest.mark.parametrize('name,arguments', [
    ('query_weather', {'city': '杭州', 'date': '2030-05-01'}),
    ('search_food', {'city': '杭州', 'keywords': '西湖附近清淡餐厅'}),
    ('search_places', {'city': '杭州', 'keywords': '西湖'}),
    ('query_route', {'origin_poi_id': 'unknown', 'destination_poi_id': 'other', 'mode': 'transit'}),
])
def test_all_fact_tools_are_stubbed_before_live_providers(monkeypatch, name, arguments):
    from app.agents.tools import registry

    def forbidden(*args, **kwargs):
        pytest.fail('scope evaluation attempted a real fact request')

    monkeypatch.setattr(registry, 'run_tool', forbidden)
    seen = []

    def request(client, model, messages, **kwargs):
        seen.append(True)
        if len(seen) == 1:
            return response(name=name, arguments=arguments)
        result = json.loads(messages[-1]['content'])
        assert result['status'] == 'unavailable'
        assert '未取得新事实' in result['reason']
        return response('外部查询不可用，相关事实仍未知。')

    monkeypatch.setattr(scope_eval, '_create_chat_completion', request)
    result = scope_eval.run_case(('facts', '查询游玩信息', 'Unknown facts stay unknown'), None, 'offline')
    assert not result['failed'], result['errors']
    assert result['unchanged'] and result['selected'] == []
    assert len(seen) == 2


def test_direct_requirements_submission_is_captured_but_not_committed(monkeypatch):
    def request(*args, **kwargs):
        return response(name='submit_changes', arguments={
            'user_quote': '人数改成2人', 'requirements_patch': {'travelers': 2}})

    monkeypatch.setattr(scope_eval, '_create_chat_completion', request)
    result = scope_eval.run_case(('requirements', '人数改成2人', 'Only travelers'), None, 'offline')
    assert not result['failed'], result['errors']
    assert result['unchanged']
    assert result['submitted'] == [{'user_quote': '人数改成2人', 'requirements_patch': {'travelers': 2}}]
    assert len(result['selected']) == 1
