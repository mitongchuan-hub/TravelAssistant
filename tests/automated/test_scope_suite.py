from types import SimpleNamespace

import pytest

from tests import scope_eval
from tests.scope_suite import load_cases, mechanical_checks


def test_comprehensive_dataset_has_unique_cases_and_expected_coverage():
    cases = load_cases()
    assert len(cases) >= 45
    assert len({case['category'] for case in cases}) >= 8
    assert {'simple', 'medium', 'complex'} == {case['level'] for case in cases}


@pytest.mark.parametrize('context', ['empty', 'missing_dates', 'waiting'])
def test_special_contexts_run_without_fixture_contract_errors(monkeypatch, context):
    case = dict(id='fixture', input='Hello', rubric='Fixture', context=context)
    if context == 'waiting':
        case['active_question'] = dict(text='When?', answer_target=dict(namespace='requirements', field='start_date'))

    def request(*args, **kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='Hello', tool_calls=[]))])

    monkeypatch.setattr(scope_eval, '_create_chat_completion', request)
    result = scope_eval.run_case(case, None, 'offline')
    assert not result['errors']
    assert result['unchanged']
    if context != 'waiting':
        assert not result['failed']


def test_unfinished_waiter_is_not_scored_as_preserved_success():
    result = dict(failed=True, unchanged=True, selected=[], calls=[], initial_workflow={}, proposed_workflow=None)
    checks = mechanical_checks(dict(id='waiter', preserve_question=True), result)
    assert not checks['execution_completed']
    assert not checks['waiting_question_preserved']


@pytest.mark.parametrize('case', load_cases(), ids=lambda case: case['id'])
def test_every_dataset_context_has_valid_runtime_contract(monkeypatch, case):
    def request(*args, **kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='Hello', tool_calls=[]))])

    monkeypatch.setattr(scope_eval, '_create_chat_completion', request)
    result = scope_eval.run_case(case, None, 'offline')
    assert not result['errors'], result['errors']
    assert result['unchanged']
