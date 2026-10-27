"""Guide previews and downloads are owned, version-bound, isolated documents."""
from copy import deepcopy
from html import unescape
import re

import pytest
from fastapi.testclient import TestClient

from app.services.guide_html import build_guide_display
from app.services.plan_display import text_hash


TRIP = 'trip-hangzhou'
HTML = '<!doctype html><html><head><title>旅行攻略</title></head><body><h1>西湖</h1><p>费用待核实。</p></body></html>'


@pytest.fixture
def guide(monkeypatch):
    monkeypatch.setenv('TRAVEL_PERSISTENCE', '0')
    from app import main
    from app.store import DemoStore

    store = DemoStore(persistence_enabled=False, display_worker_enabled=False)
    store._apply_text_plan(TRIP, '09:00 西湖，费用待核实。')
    plan = store.plans[TRIP]
    plan.display = build_guide_display(HTML)
    plan.display_source_hash = text_hash(plan.text)
    plan.display_status = 'ready'
    monkeypatch.setattr(main, 'store', store)
    monkeypatch.setattr(main, 'current_user', lambda request: store.trips[TRIP].owner)
    with TestClient(main.app, follow_redirects=False) as client:
        yield store, client, main


def params(store):
    state = store.get_display_state(TRIP)
    return {key: state[key] for key in ('plan_id', 'source_hash')} | {'version': state['guide_version']}


def test_preview_and_download_are_isolated_owned_documents(guide):
    store, client, _ = guide
    for download in (0, 1):
        response = client.get(f'/workspace/{TRIP}/guide', params=params(store) | {'download': download})
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
    response = client.get(f'/workspace/{TRIP}/guide', params=old)
    assert response.status_code == 409


def test_replaced_html_invalidates_previous_download_url(guide):
    store, client, _ = guide
    old = params(store)
    store.plans[TRIP].display = build_guide_display(HTML.replace('西湖', '西湖游玩攻略'))
    response = client.get(f'/workspace/{TRIP}/guide', params=old | {'download': 1})
    assert response.status_code == 409


def test_unauthenticated_and_other_owner_cannot_read_guide(guide, monkeypatch):
    store, client, main = guide
    from app.models import User

    binding = params(store)
    monkeypatch.setattr(main, 'current_user', lambda request: None)
    assert client.get(f'/workspace/{TRIP}/guide', params=binding).status_code == 401
    monkeypatch.setattr(main, 'current_user', lambda request: User('other', '其他用户', '其'))
    assert client.get(f'/workspace/{TRIP}/guide', params=binding).status_code == 404


def test_corrupted_saved_html_cannot_bypass_validation(guide):
    store, client, _ = guide
    store.plans[TRIP].display['html'] = HTML.replace('</body>', '<script>alert(1)</script></body>')
    response = client.get(f'/workspace/{TRIP}/guide', params=params(store))
    assert response.status_code == 409
    assert '<script>' not in response.text


def test_chat_has_preview_and_legacy_itinerary_link_opens_same_workspace(guide):
    store, client, _ = guide
    for tab in ('chat', 'itinerary'):
        response = client.get(f'/workspace/{TRIP}', params={'tab': tab})
        assert response.status_code == 200
        assert 'data-guide-preview' in response.text
        assert 'sandbox="allow-popups allow-popups-to-escape-sandbox"' in response.text
        assert 'data-itinerary-text' in response.text
        assert '<h1>西湖</h1>' not in response.text
        assert 'data-display-itinerary' not in response.text


def test_latest_guide_remains_readable_during_failed_style_refresh(guide):
    store, client, _ = guide
    store.plans[TRIP].display_status = 'fallback'
    response = client.get(f'/workspace/{TRIP}/guide', params=params(store))
    assert response.status_code == 200


@pytest.mark.parametrize('status', ['ready', 'pending', 'fallback'])
def test_preview_fragment_supplies_current_download_binding(guide, status):
    store, client, _ = guide
    store.plans[TRIP].display_status = status
    response = client.get(f'/workspace/{TRIP}/display/partial', params={
        key: params(store)[key] for key in ('plan_id', 'source_hash')
    })
    assert response.status_code == 200
    match = re.search(r'data-download-url="([^"]+)"', response.text)
    assert match is not None
    download = client.get(unescape(match[1]))
    assert download.status_code == 200
    assert download.headers['content-disposition'].startswith('attachment;')


def test_new_source_hides_download_until_its_guide_is_available(guide):
    store, client, _ = guide
    store.plans[TRIP].text += ' 18:00 返回住处。'
    response = client.get(f'/workspace/{TRIP}?tab=chat')
    assert response.status_code == 200
    assert 'data-download-url=""' in response.text
    assert 'data-guide-download download hidden' in response.text


def test_legacy_structured_plan_remains_readable_without_replanning(guide):
    store, client, _ = guide
    from app.store import DemoStore

    store.plans[TRIP] = DemoStore(persistence_enabled=False, display_worker_enabled=False).plans[TRIP]
    plan = store.plans[TRIP]
    assert plan.days and not plan.text
    response = client.get(f'/workspace/{TRIP}?tab=itinerary')
    assert response.status_code == 200
    assert plan.days[0].items[0].title in response.text
    assert plan.days[-1].items[-1].title in response.text
