"""Regression tests for app.services.requirements (previously not unit-tested).

Locks in the intended validation of durable traveler requirements: budget
normalization, day-count/date consistency, people/time bounds, and the
evidence-bound chat patch contract. These assert documented behavior; a
failure here indicates a behavior change, not a test bug.
"""
import pytest

from app.models import Trip, TripRequirements, User
from app.services.requirements import (
    chat_changes, clean_list, initial_requirements, update_requirements,
)


def new_trip() -> Trip:
    return Trip('trip', '杭州', '', '', '', '', User('owner', '测试', '测'), '', '',
                mode='city', requirements=TripRequirements(city='杭州', revision=0))


# --- initial_requirements: unambiguous budget text only ----------------------

@pytest.mark.parametrize('budget,amount,scope', [
    ('人均 2600 元', '2600', 'per_person'),
    ('每人2600', '2600', 'per_person'),
    ('总额 5000', '5000', 'total'),
    ('总预算 8000', '8000', 'total'),
    ('2600元', '2600', 'unspecified'),
    ('2600.99', '2600.99', 'unspecified'),
    ('  500  ', '500', 'unspecified'),
])
def test_initial_budget_normalizes_unambiguous_amounts(budget, amount, scope):
    req = initial_requirements(budget, '随意')
    assert req.budget_amount == amount
    assert req.budget_scope == scope


@pytest.mark.parametrize('budget', [
    '2600.999',      # more than two decimals: not normalized, left for Trip
    '2600 元 5',     # trailing junk
    '约 2600 元',    # qualifier word
    '2600元/天',
    '',
])
def test_initial_budget_leaves_ambiguous_text(budget):
    assert initial_requirements(budget, '随意').budget_amount == ''


def test_initial_pace_blank_when_placeholder():
    assert initial_requirements('2600', '风格待定').pace == ''
    assert initial_requirements('2600', ' 慢节奏 ').pace == '慢节奏'


# --- clean_list -------------------------------------------------------------

def test_clean_list_dedupes_and_strips():
    assert clean_list(['a', 'a', ' b ']) == ['a', 'b']


def test_clean_list_keeps_empty_entries_out():
    assert clean_list(['', '  ', 'keep']) == ['keep']


@pytest.mark.parametrize('value', [
    'not-a-list',
    [1],                      # non-string item
    ['ok', None],            # non-string item
    ['x' * 301],             # single item over 300 chars
    ['a'] * 101,             # more than 100 entries
])
def test_clean_list_rejects_invalid(value):
    with pytest.raises(ValueError):
        clean_list(value)


def test_clean_list_accepts_limits():
    assert clean_list(['x' * 300]) == ['x' * 300]          # exactly 300 chars is allowed
    assert len(clean_list(['item-%d' % i for i in range(100)])) == 100  # exactly 100 entries allowed


# --- update_requirements: people bounds -------------------------------------

@pytest.mark.parametrize('value,expected', [
    (1, 1), (100, 100), ('5', 5), (None, None), ('', None),
])
def test_travelers_valid_values(value, expected):
    updated = update_requirements(new_trip(), {'travelers': value})
    assert updated.requirements.travelers == expected


@pytest.mark.parametrize('value', [0, 101, '5.0', True, 'x', -1])
def test_travelers_invalid_values(value):
    with pytest.raises(ValueError):
        update_requirements(new_trip(), {'travelers': value})


# --- update_requirements: date / day-count consistency ----------------------

def test_day_count_matches_date_span():
    updated = update_requirements(new_trip(),
        {'start_date': '2026-10-01', 'end_date': '2026-10-03', 'day_count': 3})
    assert updated.requirements.day_count == 3
    assert updated.date_range == '2026年10月1日 - 2026年10月3日'


def test_single_day_span():
    updated = update_requirements(new_trip(),
        {'start_date': '2026-10-01', 'end_date': '2026-10-01', 'day_count': 1})
    assert updated.requirements.day_count == 1


def test_day_count_mismatch_rejected():
    with pytest.raises(ValueError, match='旅行天数与起止日期不一致'):
        update_requirements(new_trip(),
            {'start_date': '2026-10-01', 'end_date': '2026-10-03', 'day_count': 2})


def test_end_before_start_rejected():
    with pytest.raises(ValueError, match='结束日期不能早于出发日期'):
        update_requirements(new_trip(),
            {'start_date': '2026-10-03', 'end_date': '2026-10-01'})


def test_only_one_of_dates_rejected():
    with pytest.raises(ValueError, match='同时填写'):
        update_requirements(new_trip(), {'start_date': '2026-10-01'})


def test_malformed_date_rejected():
    with pytest.raises(ValueError, match='有效的旅行日期'):
        update_requirements(new_trip(),
            {'start_date': '2026-13-40', 'end_date': '2026-13-41'})


# --- update_requirements: field validation ----------------------------------

def test_unknown_field_rejected():
    with pytest.raises(ValueError, match='需求字段不正确'):
        update_requirements(new_trip(), {'bogus_field': 'x'})


def test_outing_time_format_enforced():
    with pytest.raises(ValueError, match='HH:MM'):
        update_requirements(new_trip(), {'outing_start_time': '24:99'})


def test_outing_end_must_be_after_start():
    with pytest.raises(ValueError, match='返回时间必须晚于出门时间'):
        update_requirements(new_trip(),
            {'outing_start_time': '18:00', 'outing_end_time': '09:00'})


def test_destination_required():
    trip = new_trip()
    trip.destination = ''
    with pytest.raises(ValueError, match='目的地'):
        update_requirements(trip, {'travelers': 2})


def test_non_string_scalar_rejected():
    with pytest.raises(ValueError, match='请填写文本'):
        update_requirements(new_trip(), {'destination': 123})


# --- chat_changes: evidence-bound, explicit add/remove ----------------------

def _trip_with_must_visit():
    trip = new_trip()
    trip.requirements.must_visit = ['西湖', '灵隐寺']
    return trip


def test_chat_changes_adds_with_matching_evidence():
    trip = _trip_with_must_visit()
    body = '我还想去河坊街逛逛'
    patch = {'changes': {'must_visit': {'add': ['河坊街'], 'remove': []}}, 'evidence': '河坊街'}
    result = chat_changes(patch, body, trip)
    assert result['must_visit'] == ['西湖', '灵隐寺', '河坊街']


def test_chat_changes_requires_evidence_present_in_message():
    trip = _trip_with_must_visit()
    patch = {'changes': {'must_visit': {'add': ['x'], 'remove': []}}, 'evidence': '不存在的证据片段'}
    with pytest.raises(ValueError, match='没有对应本轮消息'):
        chat_changes(patch, '这句话里没有这段证据', trip)


def test_chat_changes_removes_existing_item():
    trip = _trip_with_must_visit()
    patch = {'changes': {'must_visit': {'add': [], 'remove': ['灵隐寺']}}, 'evidence': '灵隐寺'}
    result = chat_changes(patch, '把灵隐寺去掉', trip)
    assert result['must_visit'] == ['西湖']


def test_chat_changes_rejects_removing_absent_item():
    trip = _trip_with_must_visit()
    # Evidence must be present in the message to pass the evidence gate and
    # reach the "removing an absent item" check.
    patch = {'changes': {'must_visit': {'add': [], 'remove': ['灵隐寺A']}}, 'evidence': '灵隐寺A'}
    with pytest.raises(ValueError, match='已变化'):
        chat_changes(patch, '把灵隐寺A移除', trip)


def test_chat_changes_rejects_add_and_remove_same_item():
    trip = _trip_with_must_visit()
    patch = {'changes': {'must_visit': {'add': ['西湖'], 'remove': ['西湖']}}, 'evidence': '西湖'}
    with pytest.raises(ValueError, match='不能同时添加和移除'):
        chat_changes(patch, '西湖', trip)


def test_chat_changes_rejects_non_list_operation_shape():
    trip = _trip_with_must_visit()
    patch = {'changes': {'must_visit': ['河坊街']}, 'evidence': '河坊街'}
    with pytest.raises(ValueError, match='添加或移除'):
        chat_changes(patch, '河坊街', trip)
