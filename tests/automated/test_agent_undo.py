"""Offline undo regression tests against the production compatibility store."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.storage.store import DemoStore
from app.application.undo import capture, record_change, undo, metadata, manual_barrier


def store(tmp_path, persistent=False):
    return DemoStore(db_path=str(tmp_path / 'undo.sqlite3'), persistence_enabled=persistent,
                     display_worker_enabled=False)


def change(s, value):
    trip_id = 'trip-hangzhou'
    with s._lock:
        stage = s._stage_turn(trip_id)
        before = capture(stage, trip_id)
        stage.trips[trip_id].requirements.text = value
        record_change(stage, trip_id, before, 'reply-' + value)
        s._persist_staged_turn(stage, trip_id)


def step(s, request):
    m = metadata(s, 'trip-hangzhou')
    return undo(s, 'trip-hangzhou', m['latest_entry_id'], request, m['version'])


def test_three_steps_drop_fourth_and_idempotent(tmp_path):
    s = store(tmp_path)
    for value in ('one', 'two', 'three', 'four'):
        change(s, value)
    assert metadata(s, 'trip-hangzhou')['available_count'] == 3
    m = metadata(s, 'trip-hangzhou')
    first = step(s, 'first')
    assert s.trips['trip-hangzhou'].requirements.text == 'three'
    assert undo(s, 'trip-hangzhou', m['latest_entry_id'], 'first', m['version']) == first
    step(s, 'second')
    step(s, 'third')
    assert s.trips['trip-hangzhou'].requirements.text == 'one'
    assert metadata(s, 'trip-hangzhou')['available_count'] == 0
    assert len(s.messages['trip-hangzhou']) >= 3


def test_manual_barrier_even_same_content(tmp_path):
    s = store(tmp_path)
    change(s, 'one')
    s.save_requirements_text('trip-hangzhou', '手动新正文', s.trips['trip-hangzhou'].requirements.revision)
    assert not metadata(s, 'trip-hangzhou')['can_undo']


def test_restart_and_failure_atomicity(tmp_path, monkeypatch):
    s = store(tmp_path, True)
    change(s, 'one')
    before = deepcopy(s.trips['trip-hangzhou'])
    import app.application.execution as execution
    with monkeypatch.context() as patch:
        patch.setattr(execution, 'save_snapshot', lambda *a, **kw: (_ for _ in ()).throw(OSError('disk')))
        with pytest.raises(Exception):
            step(s, 'fail')
    assert s.trips['trip-hangzhou'] == before
    assert metadata(s, 'trip-hangzhou')['available_count'] == 1
    s = store(tmp_path, True)
    m = metadata(s, 'trip-hangzhou')
    result = step(s, 'ok')
    s = store(tmp_path, True)
    assert undo(s, 'trip-hangzhou', m['latest_entry_id'], 'ok', m['version']) == result
    s.delete_trip('trip-hangzhou')
    assert 'trip-hangzhou' not in s.agent_undo


def test_production_commit_records_only_business_and_confirm_is_barrier(tmp_path):
    from app.domain.changes import prepare_changes
    from app.application.contracts import ExecutionResult
    s = store(tmp_path)
    tid = 'trip-hangzhou'
    from tests.automated.task_fixtures import running_context
    context = running_context(s, tid, '添加想法：晚上散步')
    execution = SimpleNamespace(trip=deepcopy(s.trips[tid]), plan=deepcopy(s.plans[tid]), display_target=None)
    changes = prepare_changes({'user_quote': context.body, 'requirements_text': '晚上散步'}, context, execution)
    s.commit_turn(context, ExecutionResult(changes=changes))
    assert metadata(s, tid)['available_count'] == 1
    context = running_context(s, tid, '你好')
    s.commit_turn(context, ExecutionResult(answer='你好'))
    assert metadata(s, tid)['available_count'] == 1
    s.confirm_plan(tid)
    assert not metadata(s, tid)['can_undo']


def test_fresh_html_restore_and_monotonic_versions(tmp_path):
    from app.services.itinerary.guide_html import build_guide_display
    from app.services.itinerary.freshness import plan_freshness
    from dataclasses import asdict
    s = store(tmp_path)
    tid = 'trip-hangzhou'
    trip, plan = s.trips[tid], s.plans[tid]
    plan.text = '沿湖散步'
    plan.requirements_revision = trip.requirements.revision
    plan.requirements_snapshot = asdict(trip.requirements)
    # Missing city lodging is deliberately unknown, not fresh.
    plan.display = build_guide_display('<!doctype html><html><head><title>散步攻略</title></head><body><main><h1>散步攻略</h1><p>沿湖散步，出发前核实开放时间。</p></main></body></html>', [])
    plan.display_source_hash = __import__('app.services.itinerary.plan_display', fromlist=['text_hash']).text_hash(plan.text)
    plan.display_status = 'ready'
    status = plan_freshness(trip, plan)['basis_status']
    old_id = plan.id
    old_revision = trip.requirements.revision
    change(s, 'new')
    step(s, 'restore-html')
    restored = s.plans[tid]
    assert restored.id != old_id
    assert restored.display and restored.display_status == 'ready'
    assert s.trips[tid].requirements.revision > old_revision
    assert plan_freshness(s.trips[tid], restored)['basis_status'] == status


def test_conflict_active_and_changed_payload(tmp_path):
    from app.workflow.tasks import TaskConflict
    s = store(tmp_path)
    change(s, 'one')
    m = metadata(s, 'trip-hangzhou')
    s.tasks['active'] = {'trip_id': 'trip-hangzhou', 'status': 'running'}
    with pytest.raises(TaskConflict):
        undo(s, 'trip-hangzhou', m['latest_entry_id'], 'token', m['version'])
    s.tasks.clear()
    step(s, 'token')
    with pytest.raises(TaskConflict):
        undo(s, 'trip-hangzhou', 'other', 'token', m['version'])


def test_undo_api_owner_csrf_idempotency_and_safe_metadata(tmp_path, monkeypatch):
    from app.main import app
    from app.api import dependencies as deps
    from app.auth import AuthStore, SESSION_COOKIE_NAME
    from fastapi.testclient import TestClient
    s = store(tmp_path, True)
    auth = AuthStore(s.db_path, persistence_enabled=True)
    monkeypatch.setattr(deps, 'store', s)
    monkeypatch.setattr(deps, 'auth', auth)
    change(s, 'one')
    change(s, 'two')
    tid = 'trip-hangzhou'
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, auth.create_session(s.trips[tid].owner.id))
        session = client.get('/api/auth/session').json()
        headers = {'Origin': 'http://testserver', 'X-CSRF-Token': session['csrf_token']}
        public = client.get('/api/trips/' + tid + '/workspace').json()['undo']
        assert set(public) == {'available_count', 'latest_entry_id', 'message_id', 'version', 'can_undo', 'reason'}
        payload = {'entry_id': public['latest_entry_id'], 'expected_version': public['version'], 'request_id': 'api-token'}
        assert client.post('/api/trips/' + tid + '/undo', json=payload).status_code == 403
        first = client.post('/api/trips/' + tid + '/undo', json=payload, headers=headers)
        assert first.status_code == 200
        again = client.post('/api/trips/' + tid + '/undo', json=payload, headers=headers)
        assert again.json() == first.json()
        assert s.trips[tid].requirements.text == 'one'
        payload['request_id'] = 'other-tab'
        assert client.post('/api/trips/' + tid + '/undo', json=payload, headers=headers).status_code == 409
        client.cookies.set(SESSION_COOKIE_NAME, auth.create_session('other-user'))
        # Unknown account is unauthenticated, never authorized to restore snapshots.
        assert client.post('/api/trips/' + tid + '/undo', json=payload, headers=headers).status_code in {401, 403, 404}


def test_manual_ideas_invalidate_late_agent_even_if_value_restored(tmp_path):
    from app.application.contracts import ExecutionResult
    s = store(tmp_path)
    tid = 'trip-hangzhou'
    change(s, 'one')
    context = s.capture_request_context(tid, body='旧消息')
    original = s.trips[tid].requirements.text
    s.save_requirements_text(tid, '临时想法', s.trips[tid].requirements.revision)
    s.save_requirements_text(tid, original, s.trips[tid].requirements.revision)
    assert not s.commit_turn(context, ExecutionResult(answer='迟到回复')).committed
    assert not metadata(s, tid)['can_undo']


def test_manual_barrier_rolls_back_with_sqlite_failure(tmp_path, monkeypatch):
    s = store(tmp_path, True)
    change(s, 'one')
    before = deepcopy(s.trips['trip-hangzhou'].requirements.text)
    import app.application.execution as execution
    monkeypatch.setattr(execution, 'save_snapshot', lambda *a, **kw: (_ for _ in ()).throw(OSError('disk')))
    with pytest.raises(Exception):
        s.save_requirements_text('trip-hangzhou', '失败写入', s.trips['trip-hangzhou'].requirements.revision)
    assert s.trips['trip-hangzhou'].requirements.text == before
    assert metadata(s, 'trip-hangzhou')['available_count'] == 1


@pytest.mark.parametrize('freshness', ['fresh', 'stale', 'unknown'])
def test_restored_basis_keeps_original_semantics(tmp_path, freshness):
    from app.services.itinerary.freshness import plan_freshness
    from dataclasses import asdict
    s = store(tmp_path)
    tid = 'trip-hangzhou'
    trip, plan = s.trips[tid], s.plans[tid]
    # A legacy persisted non-city trip allows testing complete requirements
    # independently from the already-covered missing-lodging unknown case.
    trip.mode = 'legacy'
    trip.requirements.revision = 5
    plan.requirements_revision = 5
    plan.requirements_snapshot = asdict(trip.requirements)
    if freshness == 'stale':
        plan.requirements_revision = 4
        plan.requirements_snapshot['revision'] = 4
    elif freshness == 'unknown':
        plan.requirements_snapshot = {}
    assert plan_freshness(trip, plan)['basis_status'] == freshness
    change(s, 'new')
    step(s, 'restore')
    assert plan_freshness(s.trips[tid], s.plans[tid])['basis_status'] == freshness


def test_new_agent_branch_and_noop_do_not_recover_consumed_history(tmp_path):
    s = store(tmp_path)
    for value in ('one', 'two', 'three'):
        change(s, value)
    step(s, 'back')
    change(s, 'branch')
    assert metadata(s, 'trip-hangzhou')['available_count'] == 3
    change(s, 'branch')
    assert metadata(s, 'trip-hangzhou')['available_count'] == 3
    step(s, 'back-branch')
    assert s.trips['trip-hangzhou'].requirements.text == 'two'


def test_invalid_html_is_not_retained_or_restored(tmp_path):
    s = store(tmp_path)
    tid = 'trip-hangzhou'
    plan = s.plans[tid]
    plan.text = '正文保留'
    plan.display_status = 'ready'
    plan.display_source_hash = __import__('app.services.itinerary.plan_display', fromlist=['text_hash']).text_hash(plan.text)
    plan.display = {'schema_version': '2.0', 'dates': [], 'html': '<script>alert(1)</script>'}
    change(s, 'one')
    assert s.agent_undo[tid]['entries'][-1]['before']['plan']['display'] is None
    step(s, 'invalid-html')
    assert s.plans[tid].text == '正文保留'
    assert s.plans[tid].display is None


def test_old_snapshot_without_undo_is_compatible(tmp_path):
    from app.storage.persistence import serialize_state, restore_state
    from app.storage.store import USERS
    s = store(tmp_path)
    snapshot = serialize_state(s, USERS)
    snapshot.pop('agent_undo')
    restore_state(s, snapshot, dict(USERS))
    assert metadata(s, 'trip-hangzhou')['available_count'] == 0
