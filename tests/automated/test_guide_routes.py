"""Guide previews and downloads are owned, version-bound, isolated documents."""
from copy import deepcopy
from html import unescape
import re

import pytest
from fastapi.testclient import TestClient

from app.services.itinerary.guide_html import build_guide_display
from app.services.itinerary.plan_display import text_hash


TRIP = 'trip-hangzhou'
HTML = '<!doctype html><html><head><title>旅行攻略</title></head><body><h1>西湖</h1><p>费用待核实。</p></body></html>'


@pytest.fixture
def guide(monkeypatch):
    monkeypatch.setenv('TRAVEL_PERSISTENCE', '0')
    from app import main
    from app.api import dependencies as deps
    from app.storage.store import DemoStore

    store = DemoStore(persistence_enabled=False, display_worker_enabled=False)
    store._apply_text_plan(TRIP, '09:00 西湖，费用待核实。')
    plan = store.plans[TRIP]
    plan.display = build_guide_display(HTML)
    plan.display_source_hash = text_hash(plan.text)
    plan.display_status = 'ready'
    monkeypatch.setattr(deps, 'store', store)
    monkeypatch.setattr(deps, 'current_user', lambda request: store.trips[TRIP].owner)
    with TestClient(main.app, follow_redirects=False) as client:
        yield store, client, deps


def params(store):
    state = store.get_display_state(TRIP)
    return {key: state[key] for key in ('plan_id', 'source_hash')} | {'version': state['guide_version']}


def test_preview_and_download_are_isolated_owned_documents(guide):
    store, client, _ = guide
    for download in (0, 1):
        response = client.get(f'/api/trips/{TRIP}/guide', params=params(store) | {'download': bool(download)})
        assert response.status_code == 200
        assert '西湖' in response.text and '费用待核实' in response.text
        assert "script-src 'none'" in response.headers['content-security-policy']
        assert 'sandbox' in response.headers['content-security-policy']
        assert 'Content-Security-Policy' in response.text
        assert response.headers['cache-control'] == 'no-store'
        if download:
            assert response.headers['content-disposition'].startswith('attachment;')


@pytest.mark.parametrize('field', ['plan_id', 'source_hash', 'version'])
def test_outdated_preview_binding_is_rejected(guide, field):
    store, client, _ = guide
    old = params(store) | {field: 'old'}
    response = client.get(f'/api/trips/{TRIP}/guide', params=old)
    assert response.status_code == 409


def test_replaced_html_invalidates_previous_download_url(guide):
    store, client, _ = guide
    old = params(store)
    store.plans[TRIP].display = build_guide_display(HTML.replace('西湖', '西湖游玩攻略'))
    response = client.get(f'/api/trips/{TRIP}/guide', params=old | {'download': 1})
    assert response.status_code == 409


def test_unauthenticated_and_other_owner_cannot_read_guide(guide, monkeypatch):
    store, client, deps = guide
    from app.domain.models import User

    binding = params(store)
    monkeypatch.setattr(deps, 'current_user', lambda request: None)
    assert client.get(f'/api/trips/{TRIP}/guide', params=binding).status_code == 401
    monkeypatch.setattr(deps, 'current_user', lambda request: User('other', '其他用户', '其'))
    assert client.get(f'/api/trips/{TRIP}/guide', params=binding).status_code == 404


def test_corrupted_saved_html_cannot_bypass_validation(guide):
    store, client, _ = guide
    store.plans[TRIP].display['html'] = HTML.replace('</body>', '<script>alert(1)</script></body>')
    response = client.get(f'/api/trips/{TRIP}/guide', params=params(store))
    assert response.status_code == 409
    assert '<script>' not in response.text


def test_workspace_exposes_source_and_guide_state_without_inline_html(guide):
    store, client, _ = guide
    response = client.get(f'/api/trips/{TRIP}/workspace')
    assert response.status_code == 200
    data = response.json()
    assert data['plan']['text'] == store.plans[TRIP].text
    assert data['display_state']['guide_available']
    assert '<h1>西湖</h1>' not in response.text


def test_latest_guide_remains_readable_during_failed_style_refresh(guide):
    store, client, _ = guide
    store.plans[TRIP].display_status = 'fallback'
    response = client.get(f'/api/trips/{TRIP}/guide', params=params(store))
    assert response.status_code == 200


@pytest.mark.parametrize('status', ['ready', 'pending', 'fallback'])
def test_plan_state_supplies_current_download_binding(guide, status):
    store, client, _ = guide
    store.plans[TRIP].display_status = status
    response = client.get(f'/api/trips/{TRIP}/plan-state')
    assert response.status_code == 200
    state = response.json()
    assert state['guide_available']
    binding = {key: state[key] for key in ('plan_id', 'source_hash')}
    download = client.get(f'/api/trips/{TRIP}/guide', params=binding | {
        'version': state['guide_version'], 'download': True})
    assert download.status_code == 200
    assert download.headers['content-disposition'].startswith('attachment;')


def test_new_source_disables_guide_until_matching_display_available(guide):
    store, client, _ = guide
    store.plans[TRIP].text += ' 18:00 返回住处。'
    response = client.get(f'/api/trips/{TRIP}/workspace')
    assert response.status_code == 200
    assert response.json()['display_state']['guide_available'] is False


def test_legacy_structured_plan_remains_readable_without_replanning(guide):
    store, client, _ = guide
    from app.storage.store import DemoStore
    store.plans[TRIP] = DemoStore(persistence_enabled=False, display_worker_enabled=False).plans[TRIP]
    plan = store.plans[TRIP]
    assert plan.days and not plan.text
    response = client.get(f'/api/trips/{TRIP}/workspace')
    assert response.status_code == 200
    days = response.json()['plan']['days']
    assert days[0]['items'][0]['title'] == plan.days[0].items[0].title
    assert days[-1]['items'][-1]['title'] == plan.days[-1].items[-1].title
