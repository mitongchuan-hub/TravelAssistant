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

    monkeypatch.setattr(scope_eval, 'request_completion', request)
    result = scope_eval.run_case(case, None, 'offline')
    assert not result['errors']
    assert result['unchanged']
    if context != 'waiting':
        assert not result['failed']


def test_ambiguous_short_answer_checks_no_guessed_business_changes():
    result = dict(failed=False, unchanged=True, selected=[], calls=[])
    checks = mechanical_checks(dict(id='waiter', preserve_question=True), result)
    assert checks['execution_completed']
    assert checks['no_unrequested_changes']
    result['selected'] = ['unauthorized-change']
    assert not mechanical_checks(dict(id='waiter', preserve_question=True), result)['no_unrequested_changes']


def test_short_answer_check_requires_only_the_requested_field():
    result = dict(failed=False, unchanged=True, selected=['crud'], calls=[], submitted=[
        {'requirements_patch': {'travelers': 2}}])
    assert mechanical_checks({'id': 'Q04'}, result)['short_answer_only_updates_travelers']
    result['submitted'][0]['requirements_patch']['budget_amount'] = '800'
    assert not mechanical_checks({'id': 'Q04'}, result)['short_answer_only_updates_travelers']


@pytest.mark.parametrize('case', load_cases(), ids=lambda case: case['id'])
def test_every_dataset_context_has_valid_runtime_contract(monkeypatch, case):
    def request(*args, **kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='Hello', tool_calls=[]))])

    monkeypatch.setattr(scope_eval, 'request_completion', request)
    result = scope_eval.run_case(case, None, 'offline')
    assert not result['errors'], result['errors']
    assert result['unchanged']
