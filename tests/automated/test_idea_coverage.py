"""Full planning must account for every idea instead of silently ignoring it."""
from types import SimpleNamespace

import pytest

from app.agents.agent_protocol import CapabilityStatus
from app.agents.agent_runtime import TravelAgentRuntime


def runtime():
    value = object.__new__(TravelAgentRuntime)
    value.snapshot = {'idea_cards': [
        {'id': 'night', 'title': 'Night ride', 'body': 'Night ride'},
        {'id': 'breakfast', 'title': 'Buns', 'body': 'Buns'},
    ]}
    value.trace = None
    value.evidence = []
    return value


def outcome(ref, status='scheduled', reason='Night ride at 19:30'):
    return {'idea_ref': ref, 'status': status, 'reason': reason}


def test_missing_or_unknown_ideas_cannot_pass_coverage():
    value = runtime()
    for outcomes in ([], [outcome('night')],
                     [outcome('night'), outcome('unknown')],
                     [outcome('night'), outcome('night'), outcome('breakfast')]):
        with pytest.raises(ValueError, match='idea'):
            value._validate_idea_outcomes({'idea_outcomes': outcomes, 'artifact_text': 'Night ride at 19:30; Buns'})


def test_scheduled_outcome_must_point_to_actual_itinerary_text():
    with pytest.raises(ValueError, match='idea'):
        runtime()._validate_idea_outcomes({'idea_outcomes': [
            outcome('night'), outcome('breakfast', reason='Buns')], 'artifact_text': 'Buns'})


def test_conflicts_and_cancelled_wishes_remain_explicit():
    outcomes = [outcome('night', 'conflict', 'Return deadline conflicts with ride'),
                outcome('breakfast', 'excluded', 'User cancelled breakfast')]
    runtime()._validate_idea_outcomes({'idea_outcomes': outcomes, 'artifact_text': 'Day plan'})
    with pytest.raises(ValueError, match='idea'):
        runtime()._validate_idea_outcomes({'idea_outcomes': [
            outcome('night', 'conflict', ' '), outcomes[1]], 'artifact_text': 'Day plan'})


def test_completion_rejects_missing_coverage_before_creating_candidate():
    value = runtime()
    value.evidence = []
    workspace = SimpleNamespace(register_artifact=lambda *a, **kw: pytest.fail('candidate must not be registered'))
    with pytest.raises(ValueError, match='idea'):
        value._complete_capability('draft_itinerary', {
            'status': CapabilityStatus.OK.value, 'summary': 'Day plan', 'artifact_text': 'Day plan',
            'completed_items': [], 'unresolved_items': [],
        }, workspace)


def test_success_preserves_outcomes_and_surfaces_unresolved_wishes_to_main():
    value = runtime()
    captured = {}

    def register(kind, payload, **kwargs):
        captured.update(payload)
        return SimpleNamespace(id='candidate')

    workspace = SimpleNamespace(artifacts=[], register_artifact=register, observe=lambda result: result)
    outcomes = [outcome('night'), outcome('breakfast', 'conflict', 'Breakfast window conflicts')]
    result = value._complete_capability('draft_itinerary', {
        'status': 'ok', 'summary': 'Partial plan', 'artifact_text': 'Night ride at 19:30',
        'idea_outcomes': outcomes, 'completed_items': [], 'unresolved_items': [],
    }, workspace)
    assert result.artifact_refs == ('candidate',)
    assert captured['idea_outcomes'] == outcomes
    assert any('Breakfast window conflicts' in item for item in result.unresolved_items)


def test_empty_wall_keeps_existing_completion_contract():
    value = runtime()
    value.snapshot['idea_cards'] = None
    value._validate_idea_outcomes({'artifact_text': 'Day plan'})
