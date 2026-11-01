"""Explicit reasoning settings must not be silently removed after rejection."""
from types import SimpleNamespace

import httpx
import pytest
from openai import BadRequestError

from app.agents import agent_client
from app.agents.budget import TurnBudget


def test_rejected_reasoning_setting_is_not_removed_or_retried(monkeypatch):
    requests = []
    rejection = BadRequestError(
        'reasoning_effort is not supported',
        response=httpx.Response(400, request=httpx.Request('POST', 'https://model.offline.invalid')),
        body={'error': {'param': 'reasoning_effort', 'code': 'unsupported_parameter'}},
    )

    def reject(client, params, api):
        requests.append(dict(params))
        raise rejection

    monkeypatch.setattr(agent_client, 'create_completion', reject)
    budget = TurnBudget()
    for _ in range(2):
        with pytest.raises(BadRequestError) as error:
            agent_client._create_chat_completion(
                SimpleNamespace(base_url='https://model.offline.invalid'), 'test-model',
                [{'role': 'user', 'content': 'synthetic'}],
                reasoning_effort='medium', budget=budget,
            )
        assert error.value is rejection
    assert len(requests) == 2
    assert all(request['reasoning_effort'] == 'medium' for request in requests)
    assert budget.model_calls == 2
