"""JSON contracts replacing server-rendered routes (coverage map).

Route signatures/static/Jinja -> JSON routing/error DTOs; auth redirects -> session
and revocation; workspace templates -> DTOs. Frontend tests cover DOM/layout.
"""
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Mount


def csrf_headers(client):
    # Routes may override current_user for ownership isolation, but CSRF still
    # exercises a real opaque session cookie and the public bootstrap endpoint.
    from app.api import dependencies as deps
    from app.auth import SESSION_COOKIE_NAME
    from starlette.requests import Request
    if not client.cookies.get(SESSION_COOKIE_NAME):
        user = deps.current_user(Request({'type': 'http', 'headers': []}))
        if user:
            client.cookies.set(SESSION_COOKIE_NAME, deps.auth.create_session(user.id))
    response = client.get('/api/auth/session')
    assert response.status_code == 200
    return {'Origin': 'http://testserver', 'X-CSRF-Token': response.json()['csrf_token']}


def test_routes_are_api_without_legacy_pages_or_static_mount():
    from app.main import app
    routes = [route for route in app.routes if isinstance(route, APIRoute)]
    assert routes and all(route.path.startswith('/api/') or route.path in {'/health', '/favicon.ico'} for route in routes)
    assert not any(isinstance(route, Mount) for route in app.routes)
    required = {('/api/auth/session', 'GET'), ('/api/auth/register', 'POST'),
                ('/api/auth/login', 'POST'), ('/api/auth/logout', 'POST'),
                ('/api/trips', 'GET'), ('/api/trips', 'POST'),
                ('/api/trips/{trip_id}/workspace', 'GET'),
                ('/api/trips/{trip_id}/guide', 'GET'),
                ('/api/trips/{trip_id}/plan/confirm', 'POST')}
    assert required <= {(r.path, m) for r in routes for m in r.methods}
    with TestClient(app) as client:
        for path in ('/', '/trips', '/workspace/trip-hangzhou', '/static/guide.js'):
            response = client.get(path)
            assert response.status_code == 404 and response.json()['error']['message']


def test_auth_routes_register_login_and_revoke_real_session(monkeypatch, tmp_path):
    from app import main
    from app.auth import AuthStore, SESSION_COOKIE_NAME
    from app.storage import store as store_module
    from app.api import dependencies as deps
    users = dict(store_module.USERS)
    monkeypatch.setattr(store_module, 'USERS', users)
    monkeypatch.setattr(deps, 'USERS', users)
    store = store_module.DemoStore(db_path=str(tmp_path / 'auth.sqlite3'),
        persistence_enabled=True, display_worker_enabled=False)
    auth = AuthStore(store.db_path, persistence_enabled=True)
    monkeypatch.setattr(deps, 'store', store)
    monkeypatch.setattr(deps, 'auth', auth)
    credentials = {'email': 'offline-smoke@example.com', 'password': 'offline-password-123'}
    with TestClient(main.app, follow_redirects=False) as client:
        initial = client.get('/api/auth/session').json()
        assert initial['user'] is None and initial['csrf_token']
        registered = client.post('/api/auth/register', json=credentials, headers=csrf_headers(client))
        assert registered.status_code == 200
        assert registered.json()['user']['id'] in users
        cookie = registered.headers['set-cookie'].lower()
        assert 'httponly' in cookie and 'samesite=lax' in cookie and 'path=/' in cookie
        token = client.cookies.get(SESSION_COOKIE_NAME)
        user_id = auth.user_id_for_session(token)
        assert user_id in users and client.get('/api/trips').status_code == 200
        restored = AuthStore(store.db_path, persistence_enabled=True)
        assert restored.authenticate(**credentials) == user_id
        assert restored.user_id_for_session(token) == user_id
        monkeypatch.setattr(deps, 'auth', restored)
        logged_out = client.post('/api/auth/logout', json={}, headers=csrf_headers(client))
        assert logged_out.status_code == 200 and logged_out.json()['user'] is None
        assert not client.cookies.get(SESSION_COOKIE_NAME)
        assert restored.user_id_for_session(token) is None
        assert client.get('/api/trips').status_code == 401
        wrong = client.post('/api/auth/login', json=credentials | {'password': 'wrong-password'}, headers=csrf_headers(client))
        assert wrong.status_code == 401 and wrong.json()['error']['message']
        assert not client.cookies.get(SESSION_COOKIE_NAME)
        logged_in = client.post('/api/auth/login', json=credentials, headers=csrf_headers(client))
        assert logged_in.status_code == 200
        assert restored.user_id_for_session(client.cookies.get(SESSION_COOKIE_NAME)) == user_id
        assert logged_in.json()['user']['id'] == user_id


def test_mutations_require_same_origin_json_csrf_without_side_effects(monkeypatch):
    from app.main import app
    from app.api import dependencies as deps
    from app.storage.store import DemoStore
    store = DemoStore(persistence_enabled=False, display_worker_enabled=False)
    monkeypatch.setattr(deps, 'store', store)
    monkeypatch.setattr(deps, 'current_user', lambda request: store.trips['trip-hangzhou'].owner)
    with TestClient(app) as client:
        headers = csrf_headers(client)
        payload = {'body': 'must not commit', 'request_id': 'csrf-denied'}
        for invalid in ({}, {'Origin': 'http://testserver'},
                        headers | {'Origin': 'https://evil.example'},
                        headers | {'Origin': 'http://testserver.evil.example'},
                        headers | {'X-CSRF-Token': 'wrong'}):
            response = client.post('/api/trips/trip-hangzhou/messages', json=payload, headers=invalid)
            assert response.status_code == 403 and response.json()['error']['code']
        response = client.post('/api/trips/trip-hangzhou/messages', data=payload, headers=headers)
        assert response.status_code in (403, 415)
        assert not store.tasks


def test_trip_creation_choice_revision_and_cleanup_contract(monkeypatch):
    from app.main import app
    from app.api import dependencies as deps, trips as trip_routes
    from app.storage.store import DemoStore
    from app.services.city_outings import LodgingChoices
    store = DemoStore(persistence_enabled=False, display_worker_enabled=False)
    owner = store.trips['trip-hangzhou'].owner
    monkeypatch.setattr(deps, 'store', store)
    monkeypatch.setattr(deps, 'current_user', lambda request: owner)
    monkeypatch.setattr(deps, 'lodging_choices', LodgingChoices())
    place = {'poi_id': 'hotel-1', 'name': '测试酒店', 'address': '测试路1号',
             'longitude': 120.15, 'latitude': 30.25, 'city': '杭州市', 'district': '西湖区',
             'source': '高德地图 POI', 'fetched_at': '2030-05-01T00:00:00+00:00'}
    monkeypatch.setattr(trip_routes, 'search_places', lambda args: {
        'status': 'available', 'total_matched': 2,
        'candidates': [place, place | {'poi_id': 'hotel-2', 'name': '测试酒店分店'}]})
    values = {'lodging_address': '测试酒店', 'start_date': '2030-05-01',
              'day_count': '2', 'transport_mode': 'transit', 'travelers': '2'}
    with TestClient(app) as client:
        headers = csrf_headers(client)
        before = len(store.trips)
        choices = client.post('/api/trips', json=values, headers=headers)
        assert choices.status_code == 200 and choices.json()['status'] == 'choices_required'
        assert len(store.trips) == before
        token = choices.json()['choice_token']
        changed = client.post('/api/trips', json={'choice_token': token, 'poi_id': 'hotel-1',
                             'day_count': '3'}, headers=headers)
        assert changed.status_code == 409 and len(store.trips) == before
        saved = client.post('/api/trips', json={'choice_token': token, 'poi_id': 'hotel-1'}, headers=headers)
        assert saved.status_code == 200 and saved.json()['status'] == 'saved'
        trip = saved.json()['trip']
        key = trip['id']
        assert trip['lodging']['poi_id'] == 'hotel-1'
        assert trip['requirements']['end_date'] == '2030-05-02'
        replay = client.post('/api/trips', json={'choice_token': token, 'poi_id': 'hotel-1'}, headers=headers)
        assert replay.status_code == 400 and len(store.trips) == before + 1
        form = client.get(f'/api/trips/{key}/requirements').json()['values']
        changed = client.put(f'/api/trips/{key}/requirements', json=form | {'day_count': '3'}, headers=headers)
        assert changed.status_code == 200
        assert client.put(f'/api/trips/{key}/requirements', json=form, headers=headers).status_code == 409
        draft = deps.lodging_choices.add(owner.id, form, [place], key)
        deleted = client.request('DELETE', f'/api/trips/{key}', json={}, headers=headers)
        assert deleted.status_code == 200 and deleted.json() == {'deleted': True}
        assert key not in store.trips and key not in store.messages and key not in store.plans
        assert draft not in deps.lodging_choices._items


def test_task_submission_retry_polling_and_safe_allowlists(monkeypatch):
    from app.main import app
    from app.api import dependencies as deps
    from app.storage.store import DemoStore
    store = DemoStore(persistence_enabled=False, display_worker_enabled=False)
    trip = 'trip-hangzhou'
    monkeypatch.setattr(deps, 'store', store)
    monkeypatch.setattr(deps, 'current_user', lambda request: store.trips[trip].owner)
    ran = []
    monkeypatch.setattr(store, 'run_task', lambda task_id: ran.append(task_id))
    with TestClient(app) as client:
        headers = csrf_headers(client)
        payload = {'kind': 'chat', 'payload': {'body': '你好'}, 'request_id': 'task-api'}
        submitted = client.post(f'/api/trips/{trip}/tasks', json=payload, headers=headers)
        assert submitted.status_code == 202
        task = submitted.json()['task']
        assert ran == [task['id']]
        replay = client.post(f'/api/trips/{trip}/tasks', json=payload, headers=headers)
        assert replay.json()['task']['id'] == task['id'] and len(ran) == 1
        conflict = client.post(f'/api/trips/{trip}/tasks', json=payload | {'payload': {'body': '不同消息'}}, headers=headers)
        assert conflict.status_code == 409
        store.tasks[task['id']].update(status='failed', message='SECRET upstream traceback',
                                      private_prompt='SECRET', evidence=['SECRET'])
        polled = client.get(f'/api/trips/{trip}/tasks')
        assert 'SECRET' not in polled.text and 'payload' not in polled.json()['tasks'][0]
        retry = client.post(f'/api/trips/{trip}/tasks/{task["id"]}/retry',
                            json={'request_id': 'retry-api'}, headers=headers)
        assert retry.status_code == 202 and retry.json()['task']['retry_of'] == task['id']
        assert len(ran) == 2
        denied = client.post(f'/api/trips/{trip}/tasks', json=payload | {
            'request_id': 'injected', 'payload': {'body': '你好', 'owner_id': 'other'}}, headers=headers)
        assert denied.status_code == 400
        after = store.messages[trip][-1].id
        assert client.get(f'/api/trips/{trip}/messages', params={'after': after}).json() == {'messages': []}
        assert client.get(f'/api/trips/{trip}/messages', params={'after': 'unknown'}).json() == {'messages': []}


def test_shared_dependency_seam_and_owner_isolation(monkeypatch):
    from app.main import app
    from app.storage.store import DemoStore
    from app.api import dependencies as deps
    from app.models import User
    first = DemoStore(persistence_enabled=False, display_worker_enabled=False)
    second = DemoStore(persistence_enabled=False, display_worker_enabled=False)
    second.trips.pop('trip-hangzhou')
    second.messages.pop('trip-hangzhou')
    monkeypatch.setattr(deps, 'store', first)
    monkeypatch.setattr(deps, 'current_user', lambda request: first.trips['trip-hangzhou'].owner)
    with TestClient(app) as client:
        response = client.get('/api/trips/trip-hangzhou/workspace')
        assert response.status_code == 200
        assert {'trip', 'plan', 'messages', 'tasks', 'idea_cards', 'wall_cards', 'metrics', 'display_state'} <= response.json().keys()
        monkeypatch.setattr(deps, 'store', second)
        assert client.get('/api/trips/trip-hangzhou/tasks').status_code == 404
        monkeypatch.setattr(deps, 'store', first)
        monkeypatch.setattr(deps, 'current_user', lambda request: User('other', '其他用户', '其'))
        for suffix in ('workspace', 'tasks', 'messages', 'plan-state', 'requirements'):
            assert client.get('/api/trips/trip-hangzhou/' + suffix).status_code == 404
        monkeypatch.setattr(deps, 'current_user', lambda request: None)
        assert client.get('/api/trips/trip-hangzhou/tasks').status_code == 401
