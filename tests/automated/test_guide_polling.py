"""Polling DTO coverage migrated from the retired Jinja fragment controller.

Coverage map: newer-idle fragment -> authoritative latest workspace/state version;
responsive preview/focus/Escape/download DOM -> frontend Playwright regression.
Backend retains guide availability/version bindings for all viewport sizes.
"""
from fastapi.testclient import TestClient
from app.services.itinerary.guide_html import build_guide_display
from app.services.itinerary.plan_display import text_hash


def test_newer_idle_snapshot_replaces_busy_state_without_stale_task(monkeypatch):
    from app.main import app
    from app.api import dependencies as deps
    from app.storage.store import DemoStore
    store = DemoStore(persistence_enabled=False, display_worker_enabled=False)
    trip = 'trip-hangzhou'
    monkeypatch.setattr(deps, 'store', store)
    monkeypatch.setattr(deps, 'current_user', lambda request: store.trips[trip].owner)
    task, _ = store.submit_task(trip, 'chat', {'body': '你好'}, 'poll-race')
    with TestClient(app) as client:
        busy = client.get(f'/api/trips/{trip}/plan-state').json()
        assert busy['busy']
        store.tasks[task['id']]['status'] = 'succeeded'
        idle = client.get(f'/api/trips/{trip}/workspace').json()['display_state']
        assert not idle['busy'] and idle['version'] != busy['version']
        assert client.get(f'/api/trips/{trip}/plan-state').json() == idle


def test_guide_content_versions_update_and_missing_source_revokes_availability(monkeypatch):
    from app.main import app
    from app.api import dependencies as deps
    from app.storage.store import DemoStore
    store = DemoStore(persistence_enabled=False, display_worker_enabled=False)
    trip = 'trip-hangzhou'
    store._apply_text_plan(trip, '09:00 西湖。')
    plan = store.plans[trip]
    html = '<!doctype html><html><head><title>攻略</title></head><body><p>西湖。</p></body></html>'
    plan.display = build_guide_display(html)
    plan.display_source_hash = text_hash(plan.text)
    plan.display_status = 'ready'
    monkeypatch.setattr(deps, 'store', store)
    monkeypatch.setattr(deps, 'current_user', lambda request: store.trips[trip].owner)
    with TestClient(app) as client:
        old = client.get(f'/api/trips/{trip}/plan-state').json()
        assert old['guide_available']
        plan.display = build_guide_display(html.replace('西湖。', '西湖新攻略。'))
        current = client.get(f'/api/trips/{trip}/plan-state').json()
        assert current['guide_available'] and current['guide_version'] != old['guide_version']
        plan.text += '18:00 返回。'
        assert not client.get(f'/api/trips/{trip}/plan-state').json()['guide_available']
