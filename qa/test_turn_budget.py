"""Regression tests for app.agents.budget.TurnBudget (methods not unit-tested).

Locks in the accounting semantics: strict int tool counts, external/model
exhaustion, and the token-limit gate that distinguishes bool from int, ignores
negative and fractional token counts, and raises only when the limit is strictly
exceeded.
"""
import pytest

from app.agents.budget import AgentBudgetError, TurnBudget


def test_initial_usage_is_zero():
    usage = TurnBudget().usage()
    assert usage == {'model_calls': 0, 'tool_calls': 0, 'external_calls': 0,
                     'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}


@pytest.mark.parametrize('count', [True, -1, 1.5, 'x', None, [1]])
def test_tools_rejects_non_int_counts(count):
    with pytest.raises(AgentBudgetError):
        TurnBudget().tools(count)


def test_tools_accumulates_and_enforces_cap():
    budget = TurnBudget(max_tools=2)
    budget.tools(1)
    assert budget.tool_calls == 1
    budget.tools(1)
    assert budget.tool_calls == 2
    with pytest.raises(AgentBudgetError, match='工具调用次数已达上限'):
        budget.tools(1)


def test_external_exhaustion():
    budget = TurnBudget(max_external=1)
    budget.external()
    with pytest.raises(AgentBudgetError, match='外部查询次数已达上限'):
        budget.external()


def test_model_exhaustion():
    budget = TurnBudget(max_models=1)
    budget.model()
    with pytest.raises(AgentBudgetError, match='模型调用次数已达上限'):
        budget.model()


def test_token_usage_ignores_bool_negative_and_fractional():
    budget = TurnBudget()
    budget.record_usage(input_tokens=5, output_tokens=True)   # bool is not an int count
    assert budget.input_tokens == 5 and budget.output_tokens == 0
    budget.record_usage(input_tokens=-3)                        # negative ignored
    assert budget.input_tokens == 5
    budget.record_usage(input_tokens=6.0)                       # float ignored
    assert budget.input_tokens == 5


def test_token_limit_raises_only_when_exceeded():
    budget = TurnBudget(total_token_limit=10)
    budget.record_usage(input_tokens=5)
    budget.record_usage(input_tokens=5)     # == 10, not yet over
    with pytest.raises(AgentBudgetError, match='令牌预算已超出'):
        budget.record_usage(input_tokens=1)  # 11 > 10


def test_model_refused_when_token_limit_already_met():
    budget = TurnBudget(total_token_limit=0)
    with pytest.raises(AgentBudgetError, match='令牌预算已用尽'):
        budget.model()


def test_cancellation_aborts_remaining_and_reserve():
    budget = TurnBudget(cancelled=lambda: True)
    with pytest.raises(AgentBudgetError, match='新消息替代'):
        budget.remaining()
    with pytest.raises(AgentBudgetError):
        budget.reserve(models=1)


def test_deadline_timeout_detected_without_sleeping():
    # A negative seconds budget puts the deadline in the past immediately.
    budget = TurnBudget(seconds=-5)
    with pytest.raises(AgentBudgetError, match='时间上限'):
        budget.remaining()


def test_reserve_finish_reserve_gate():
    budget = TurnBudget(seconds=60)
    # Asking for a finishing reserve larger than the remaining time fails.
    with pytest.raises(AgentBudgetError, match='剩余时间不足'):
        budget.reserve(seconds=61)


def test_total_tokens_property():
    budget = TurnBudget()
    budget.record_usage(input_tokens=4, output_tokens=6)
    assert budget.total_tokens == 10
