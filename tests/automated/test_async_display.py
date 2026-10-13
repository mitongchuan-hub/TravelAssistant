"""Prose-first delivery, immutable display jobs and concurrent publication guards."""
from copy import deepcopy
from dataclasses import asdict
import importlib
import json
from threading import Event, Thread

import pytest
from fastapi.testclient import TestClient

from app.agent_client import AgentReply
from app.auth import AuthStore
from app.models import AgentCard
from app.persistence import CURRENT_SCHEMA_VERSION, load_snapshot
from app.plan_display import text_hash
from app.store import DemoStore
from test_agent_roles import display_data, completion, scripted
from test_workflow import creation_fields

main_module = importlib.import_module('app.main')
store_module = importlib.import_module('app.store')
SOURCE = '完整两日原文\n2026-11-14：午后西湖散步，不吃辣。\n2026-11-15：原地休息，交通、价格和预约待核实。'


def prose(text=SOURCE, **kwargs):
    return AgentReply(text, AgentCard('itinerary-draft', '行程', '', [], []), [], plan_text=text,
        display_status='pending', display_source_hash=text_hash(text), orchestrated=True, **kwargs)


def display_result(source, dates, **kwargs):
    return {'display': display_data(dates), 'display_source_hash': text_hash(source), 'display_status': 'ready'}


def local_store(monkeypatch, *, db_path=None, worker=False):
    local = DemoStore(db_path=str(db_path) if db_path else None, persistence_enabled=bool(db_path), display_worker_enabled=worker)
    trip = local.create_trip('杭州', '', '', '', '', requirements=creation_fields())
    monkeypatch.setattr(store_module, 'generate_agent_reply', lambda **kwargs: prose())
    return local, trip.id


def generate(local, tid, request_id='generate'):
    task, _ = local.submit_task(tid, 'plan', {}, request_id)
    local.run_task(task['id'])
    return task['id']


def test_primary_reply_is_saved_and_readable_while_display_is_blocked(monkeypatch, tmp_path):
    local, tid = local_store(monkeypatch, db_path=tmp_path/'async.sqlite3', worker=True)
    entered, release = Event(), Event()
    def display(source, dates, **kwargs):
        entered.set()
        assert release.wait(8)
        return display_result(source, dates)
    monkeypatch.setattr('app.agent_client.generate_plan_display', display)
    auth = AuthStore('unused.sqlite3', False)
    auth.create_account('async@example.com', local.trips[tid].owner.id, 'async-display-password')
    monkeypatch.setattr(main_module, 'store', local)
    monkeypatch.setattr(main_module, 'auth', auth)
    try:
        with TestClient(main_module.app) as web:
            web.post('/login', data={'email':'async@example.com','password':'async-display-password'})
            response = web.post(f'/api/trips/{tid}/tasks', json={'kind':'plan','payload':{},'request_id':'api-generate'})
            assert response.status_code == 202
            assert entered.wait(2)
            task_id = response.json()['task']['id']
            assert local.tasks[task_id]['status'] == 'succeeded'
            assert local.plans[tid].text == SOURCE and local.plans[tid].display_status == 'pending'
            assert load_snapshot(local.db_path)['plans'][tid]['text'] == SOURCE
            page = web.get(local.tasks[task_id]['result_url'])
            assert '完整两日原文' in page.text and '正在整理卡片' in page.text
            assert 'data-display-itinerary' not in page.text
            assert web.get(f'/api/trips/{tid}/plan-state').json()['display_status'] == 'pending'
            release.set()
            assert local.wait_for_display_jobs(tid, 2)
            state = web.get(f'/api/trips/{tid}/plan-state').json()
            fragment = web.get(f'/workspace/{tid}/display/partial', params={'plan_id':state['plan_id'],'source_hash':state['source_hash']})
            assert state['display_status'] == 'ready' and 'data-display-itinerary' in fragment.text
            assert local.tasks[task_id]['status'] == 'succeeded' and local.plans[tid].text == SOURCE
    finally:
        release.set()
        local.stop_display_worker()


@pytest.mark.parametrize('operation', ['replace', 'clear', 'delete'])
def test_late_display_cannot_overwrite_replaced_cleared_or_deleted_trip(monkeypatch, operation):
    local, tid = local_store(monkeypatch, worker=True)
    entered, release = Event(), Event()
    def display(source, dates, **kwargs):
        entered.set()
        assert release.wait(8)
        return display_result(source, dates)
    monkeypatch.setattr('app.agent_client.generate_plan_display', display)
    try:
        task_id = generate(local, tid)
        assert entered.wait(2)
        if operation == 'replace':
            local._apply_text_plan(tid, '新原文')
            expected = deepcopy(local.plans[tid])
        elif operation == 'clear':
            local.clear_chat(tid)
            expected = deepcopy(local.plans[tid])
        else:
            assert local.delete_trip(tid)
        release.set()
        assert local.wait_for_display_jobs(timeout=2)
        local.stop_display_worker()
        if operation == 'delete':
            assert tid not in local.trips and not local.display_jobs
        else:
            assert local.plans[tid] == expected
            assert local.tasks[task_id]['status'] == 'succeeded'
            if operation == 'clear':
                assert not local.messages[tid] and local.plans[tid].text == SOURCE
    finally:
        release.set()
        local.stop_display_worker()


def test_display_waits_for_readonly_business_to_finish_without_snapshot_conflict(monkeypatch):
    local, tid = local_store(monkeypatch, worker=True)
    card_entered, card_release, foreground_entered, foreground_release = Event(), Event(), Event(), Event()
    def display(source, dates, **kwargs):
        card_entered.set()
        assert card_release.wait(8)
        return display_result(source, dates)
    monkeypatch.setattr('app.agent_client.generate_plan_display', display)
    foreground = None
    try:
        generate(local, tid)
        assert card_entered.wait(2)
        old_plan = deepcopy(local.plans[tid])
        def explain(**kwargs):
            foreground_entered.set()
            assert foreground_release.wait(8)
            return AgentReply('只读解释', AgentCard('requirement-summary','解释','',[],[]),[],orchestrated=True)
        monkeypatch.setattr(store_module, 'generate_agent_reply', explain)
        task, _ = local.submit_task(tid, 'chat', {'body':'只解释当前方案'}, 'explain')
        foreground = Thread(target=local.run_task, args=(task['id'],))
        foreground.start()
        assert foreground_entered.wait(2)
        card_release.set()
        with local._display_changed:
            assert local._display_changed.wait_for(lambda: any(j['status']=='waiting_publish' for j in local.display_jobs.values()),2)
        assert local.plans[tid] == old_plan
        foreground_release.set()
        foreground.join(2)
        assert not foreground.is_alive() and local.tasks[task['id']]['status']=='succeeded'
        assert local.tasks[task['id']]['result_url'].endswith('tab=chat')
        assert local.wait_for_display_jobs(tid,2) and local.plans[tid].display_status=='ready'
        assert local.plans[tid].id==old_plan.id and local.plans[tid].text==SOURCE
    finally:
        card_release.set()
        foreground_release.set()
        if foreground: foreground.join(2)
        local.stop_display_worker()


def test_new_revision_does_not_wait_for_old_cards_and_rejects_old_publication(monkeypatch):
    local, tid = local_store(monkeypatch, worker=True)
    entered, release = Event(), Event()
    sources = []
    def display(source, dates, **kwargs):
        sources.append(source)
        if len(sources)==1:
            entered.set()
            assert release.wait(8)
        return display_result(source,dates)
    monkeypatch.setattr('app.agent_client.generate_plan_display',display)
    try:
        generate(local,tid)
        assert entered.wait(2)
        monkeypatch.setattr(store_module,'generate_agent_reply',lambda **kwargs:prose('修改后的完整新原文'))
        task,_=local.submit_task(tid,'revision',{'feedback':'只追加备注'},'revision')
        local.run_task(task['id'])
        new_id=local.plans[tid].id
        assert local.tasks[task['id']]['status']=='succeeded' and local.plans[tid].text=='修改后的完整新原文'
        release.set()
        assert local.wait_for_display_jobs(tid,2)
        assert local.plans[tid].id==new_id and local.plans[tid].display_source_hash==text_hash('修改后的完整新原文')
        assert sources==[SOURCE,'修改后的完整新原文']
    finally:
        release.set()
        local.stop_display_worker()


def test_card_dates_use_saved_basis_and_preserve_confirmation(monkeypatch):
    local,tid=local_store(monkeypatch)
    generate(local,tid)
    before=deepcopy(local.plans[tid])
    local.update_trip_requirements(tid,{'start_date':'2026-11-16','end_date':'2026-11-17'},local.trips[tid].requirements.revision)
    local.confirm_plan(tid)
    captured=[]
    def display(source,dates,**kwargs):
        captured.append(dates)
        return display_result(source,dates)
    monkeypatch.setattr('app.agent_client.generate_plan_display',display)
    assert local.run_pending_display_jobs()==1
    assert captured==[['2026-11-14','2026-11-15']]
    assert local.plans[tid].requirements_snapshot==before.requirements_snapshot
    assert local.plans[tid].status=='已确认' and local.plans[tid].id==before.id


@pytest.mark.parametrize('failure',['timeout','bad_hash','bad_dates'])
def test_display_failure_never_turns_primary_task_into_failure(monkeypatch,failure):
    local,tid=local_store(monkeypatch)
    task_id=generate(local,tid)
    def display(source,dates,**kwargs):
        if failure=='timeout': raise TimeoutError('injected display timeout')
        result=display_result(source,dates)
        if failure=='bad_hash':result['display_source_hash']=text_hash('different text')
        if failure=='bad_dates':result['display']['days']=result['display']['days'][:1]
        return result
    monkeypatch.setattr('app.agent_client.generate_plan_display',display)
    local.run_pending_display_jobs()
    assert local.tasks[task_id]['status']=='succeeded'
    assert local.plans[tid].text==SOURCE and local.plans[tid].display_status=='fallback'


def test_display_retry_is_bound_to_current_source_and_deduplicates(monkeypatch):
    local,tid=local_store(monkeypatch)
    task_id=generate(local,tid)
    state=local.get_display_state(tid)
    original=deepcopy(local.plans[tid])
    first=local.retry_display(tid,state['plan_id'],state['source_hash'])
    assert local.retry_display(tid,state['plan_id'],state['source_hash'])['id']==first['id']
    assert len(local.display_jobs)==1 and local.plans[tid]==original
    with pytest.raises(ValueError,match='已更新'):
        local.retry_display(tid,'old',state['source_hash'])
    monkeypatch.setattr('app.agent_client.generate_plan_display',display_result)
    local.run_pending_display_jobs()
    assert local.tasks[task_id]['status']=='succeeded' and local.plans[tid].display_status=='ready'


def test_interrupted_display_preserves_primary_success_after_restart(monkeypatch,tmp_path):
    path=tmp_path/'restart.sqlite3'
    local,tid=local_store(monkeypatch,db_path=path)
    task_id=generate(local,tid)
    assert load_snapshot(path)['schema_version']==CURRENT_SCHEMA_VERSION==5
    reopened=DemoStore(db_path=str(path),persistence_enabled=True,display_worker_enabled=False)
    assert reopened.tasks[task_id]['status']=='succeeded' and reopened.plans[tid].text==SOURCE
    assert reopened.plans[tid].display_status=='fallback'
    assert next(iter(reopened.display_jobs.values()))['status']=='interrupted'
    state=reopened.get_display_state(tid)
    reopened.retry_display(tid,state['plan_id'],state['source_hash'])
    monkeypatch.setattr('app.agent_client.generate_plan_display',display_result)
    reopened.run_pending_display_jobs()
    assert reopened.plans[tid].display_status=='ready'


def test_worker_scheduling_failure_preserves_saved_prose(monkeypatch):
    local,tid=local_store(monkeypatch,worker=True)
    monkeypatch.setattr('app.display_tasks.Thread.start',lambda self:(_ for _ in ()).throw(RuntimeError('queue unavailable')))
    task_id=generate(local,tid)
    assert local.tasks[task_id]['status']=='succeeded' and local.plans[tid].text==SOURCE
    assert local.plans[tid].display_status=='fallback'


def test_disabled_display_never_blocks_or_calls_model(monkeypatch):
    monkeypatch.setenv('TRAVEL_DISPLAY_ENABLED','0')
    local,tid=local_store(monkeypatch,worker=True)
    monkeypatch.setattr('app.agent_client.generate_plan_display',lambda *a,**kw:pytest.fail('display called'))
    task_id=generate(local,tid)
    assert local.tasks[task_id]['status']=='succeeded' and local.plans[tid].text==SOURCE
    assert not local.display_jobs and local.plans[tid].display_status=='fallback'


def test_display_interfaces_require_owner_and_reject_stale_source(monkeypatch):
    local,tid=local_store(monkeypatch)
    generate(local,tid)
    user=local.create_user('其他人')
    auth=AuthStore('unused.sqlite3',False)
    auth.create_account('owner-display@example.com',local.trips[tid].owner.id,'display-owner-password')
    auth.create_account('other-display@example.com',user.id,'display-other-password')
    monkeypatch.setattr(main_module,'store',local)
    monkeypatch.setattr(main_module,'auth',auth)
    web=TestClient(main_module.app)
    url=f'/api/trips/{tid}'
    assert web.get(url+'/plan-state').status_code==401
    assert web.post(url+'/display',json={}).status_code==401
    web.post('/login',data={'email':'other-display@example.com','password':'display-other-password'})
    assert web.get(url+'/plan-state').status_code==404
    assert web.post(url+'/display',json={}).status_code==404
    web.post('/login',data={'email':'owner-display@example.com','password':'display-owner-password'})
    assert web.post(url+'/display',json={'plan_id':'old','source_hash':'wrong'}).status_code==409
    assert web.get(f'/workspace/{tid}/display/partial',params={'plan_id':'old','source_hash':'wrong'}).status_code==409


def test_display_budget_is_independent_and_has_no_tools(monkeypatch):
    local,tid=local_store(monkeypatch)
    from app import agent_client
    monkeypatch.setattr(store_module, 'generate_agent_reply', agent_client.generate_agent_reply)
    calls=scripted(monkeypatch,[completion(SOURCE),completion(display_data())])
    task_id=generate(local,tid)
    assert len(calls)==1 and local.tasks[task_id]['status']=='succeeded'
    business_budget=calls[0][1]['budget']
    business_budget.deadline=0
    local.run_pending_display_jobs()
    assert len(calls)==2 and calls[1][1]['agent_role']=='display' and calls[1][1]['tools'] is None
    assert calls[1][1]['budget'] is not business_budget and calls[1][1]['budget'].seconds==180
    assert local.plans[tid].display_status=='ready'
