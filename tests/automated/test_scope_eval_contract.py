"""Offline checks validate the evaluator's runtime path, not model semantics."""
import json
from types import SimpleNamespace

import pytest

from tests import scope_eval


@pytest.mark.parametrize('structured', [False, True])
def test_scope_eval_exercises_real_terminal_contract_without_business_changes(monkeypatch, structured):
    def request(*args, **kwargs):
        calls = []
        if structured:
            calls = [SimpleNamespace(id='finish', function=SimpleNamespace(
                name='finish_turn', arguments=json.dumps({'reply': 'Hello', 'adopt_refs': []}))) ]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=None if structured else 'Hello', tool_calls=calls))])

    monkeypatch.setattr(scope_eval, '_create_chat_completion', request)
    result = scope_eval.run_case(('greeting', 'Hello', 'Greeting'), None, 'offline', structured)
    assert not result['failed'], result['errors']
    assert result['answer'] == 'Hello'
    assert result['unchanged']
    assert result['selected'] == []
    assert bool(result['calls']) == structured
