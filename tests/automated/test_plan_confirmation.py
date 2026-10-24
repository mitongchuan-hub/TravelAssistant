"""Page confirmation commits directly, without a background execution task."""
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient


TRIP_ID = 'trip-hangzhou'
CONFIRM_URL = f'/workspace/{TRIP_ID}/plan/confirm'


@pytest.fixture
def confirmation(monkeypatch):
    monkeypatch.setenv('TRAVEL_PERSISTENCE', '0')
    from app import main
    from app.store import DemoStore

    store = DemoStore(persistence_enabled=False, display_worker_enabled=False)
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(main, 'current_user', lambda request: store.trips[TRIP_ID].owner)
    with TestClient(main.app, follow_redirects=False) as client:
        yield store, client


def page_binding(store):
    from app.services.plan_display import text_hash

    return {
        'expected_plan_id': store.plans[TRIP_ID].id,
        'source_hash': text_hash(store.plans[TRIP_ID].text),
        'expected_requirements_revision': str(store.trips[TRIP_ID].requirements.revision),
    }


def test_confirm_current_plan_without_execution_task(confirmation):
    store, client = confirmation
    original_plan = deepcopy(store.plans[TRIP_ID])
    message_count = len(store.messages[TRIP_ID])
    assert not store.tasks

    response = client.post(CONFIRM_URL, data=page_binding(store))

    assert response.status_code == 303
    assert response.headers['location'] == f'/workspace/{TRIP_ID}?tab=itinerary'
    assert store.trips[TRIP_ID].status == '已确认'
    original_plan.status = '已确认'
    assert store.plans[TRIP_ID] == original_plan
    assert len(store.messages[TRIP_ID]) == message_count + 1
    assert store.messages[TRIP_ID][-1].body == '当前行程已确认。'
    assert store.messages[TRIP_ID][-1].status == 'sent'
    assert not store.tasks


@pytest.mark.parametrize('outdated_field', [
    'expected_plan_id', 'source_hash', 'expected_requirements_revision', None,
])
def test_confirmation_rejects_outdated_or_missing_page_binding(confirmation, outdated_field):
    store, client = confirmation
    binding = page_binding(store)
    if outdated_field is None:
        binding = {}
    elif outdated_field == 'expected_requirements_revision':
        binding[outdated_field] = str(int(binding[outdated_field]) + 1)
    else:
        binding[outdated_field] = 'outdated'
    before = deepcopy((store.trips, store.plans, store.messages, store.tasks))

    response = client.post(CONFIRM_URL, data=binding)

    assert response.status_code == 409
    assert response.json()['error']
    assert (store.trips, store.plans, store.messages, store.tasks) == before


def test_confirmation_rejects_empty_itinerary(confirmation):
    store, client = confirmation
    plan = store.plans[TRIP_ID]
    plan.text = ''
    plan.days = []
    before = deepcopy((store.trips, store.plans, store.messages, store.tasks))

    response = client.post(CONFIRM_URL, data=page_binding(store))

    assert response.status_code == 409
    assert response.json()['error'] == '当前没有可确认的行程'
    assert (store.trips, store.plans, store.messages, store.tasks) == before
