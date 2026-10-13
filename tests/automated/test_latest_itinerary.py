"""Only the current itinerary is retained; old databases migrate without changing live travel data."""
from copy import deepcopy
from dataclasses import asdict
import json
import sqlite3

import pytest
from app.agent_runtime import TravelAgentRuntime
from app.agent_roles import roles
from app.plan_display import text_hash
from app.persistence import CURRENT_SCHEMA_VERSION, load_snapshot, serialize_state
from app.store import DemoStore, USERS
from test_agent_roles import completion, route, scripted, display_data, ready_for_planning
from test_text_itinerary import web


def test_old_database_discards_history_and_keeps_current_data(tmp_path):
    path=tmp_path/'old.sqlite3'
    local=DemoStore(db_path=str(path),persistence_enabled=True)
    tid='trip-hangzhou';ready_for_planning(local,tid)
    local._apply_text_plan(tid,'最新版行程',evidence=[{'tool':'query_weather','result':{'status':'unavailable'}}],
        display=display_data(),display_source_hash=text_hash('最新版行程'),display_status='ready')
    local.confirm_plan(tid)
    current=deepcopy(serialize_state(local,USERS));legacy=deepcopy(current)
    legacy['schema_version']=3
    legacy['plan_versions']={tid:[{'id':'version-999','text':'不应恢复的旧行程'}]}
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE plan_versions (id TEXT PRIMARY KEY, version_json TEXT)')
        c.execute('INSERT INTO plan_versions VALUES (?,?)',('version-999',json.dumps({'text':'旧行程'})))
        c.execute("UPDATE app_snapshots SET payload=? WHERE key='demo_store_v1'",(json.dumps(legacy,ensure_ascii=False),))
    reopened=DemoStore(db_path=str(path),persistence_enabled=True)
    migrated=load_snapshot(path)
    assert migrated['schema_version']==CURRENT_SCHEMA_VERSION
    assert 'plan_versions' not in migrated and not hasattr(reopened,'plan_versions')
    for key in ('trips','plans','messages','idea_cards','users','tasks'):
        assert migrated[key]==current[key]
    with sqlite3.connect(path) as c:
        assert not c.execute("SELECT name FROM sqlite_master WHERE name='plan_versions'").fetchone()
        assert c.execute('SELECT COUNT(*) FROM plans').fetchone()[0]==len(reopened.trips)


def test_repeated_writes_store_only_latest_original_display_and_evidence(tmp_path):
    path=tmp_path/'latest.sqlite3';local=DemoStore(db_path=str(path),persistence_enabled=True)
    tid='trip-hangzhou';ready_for_planning(local,tid)
    for i in range(3):
        text=f'最新行程{i}'
        local._apply_text_plan(tid,text,evidence=[{'source':f'依据{i}'}],display=display_data(),
            display_source_hash=text_hash(text),display_status='ready')
        local._persist()
    reopened=DemoStore(db_path=str(path),persistence_enabled=True)
    assert reopened.plans[tid].text=='最新行程2'
    assert reopened.plans[tid].display_source_hash==text_hash('最新行程2')
    assert reopened.plans[tid].evidence==[{'source':'依据2'}]
    snapshot=load_snapshot(path)
    assert '最新行程0' not in json.dumps(snapshot,ensure_ascii=False)
    assert '最新行程1' not in json.dumps(snapshot,ensure_ascii=False)


def test_history_tools_and_context_are_removed():
    local=DemoStore(persistence_enabled=False)
    rt=TravelAgentRuntime(object(),'fixture',{'trip':local.trips['trip-hangzhou']})
    for config in roles().values():
        for tool in config.tools:
            assert tool['function']['name'] not in {'read_versions','change_version'}
    with pytest.raises(ValueError,match='未知上下文'):rt.read_context({'section':'versions'})
    assert not hasattr(local,'restore_plan_version')


def test_request_for_old_version_clarifies_without_replacing_current(monkeypatch):
    local=DemoStore(persistence_enabled=False);tid='trip-hangzhou';before=deepcopy(local.plans[tid])
    text='恢复V1版本'
    calls=scripted(monkeypatch,[route('revision',text,'explain','explain','只保留当前行程，没有历史版本可恢复。')])
    local.add_user_message(tid,text)
    assert local.plans[tid]==before and len(calls)==1
    assert '没有历史版本' in local.messages[tid][-1].body


def test_confirmation_still_rejects_a_concurrent_plan_change(monkeypatch):
    local=DemoStore(persistence_enabled=False);tid='trip-hangzhou';text='确认当前方案'
    def concurrent(messages,kw):
        local._apply_text_plan(tid,'并发写入的新方案')
        return route('revision',text,'confirm')
    scripted(monkeypatch,[concurrent]);local.add_user_message(tid,text)
    assert local.plans[tid].text=='并发写入的新方案'
    assert local.plans[tid].status=='草案' and local.messages[tid][-1].status=='failed'


def test_confirmation_chat_task_succeeds_and_navigates_to_current_itinerary(monkeypatch):
    local=DemoStore(persistence_enabled=False);tid='trip-hangzhou';before=deepcopy(local.plans[tid])
    scripted(monkeypatch,[route('revision','确认当前方案','confirm')])
    task,_=local.submit_task(tid,'chat',{'body':'确认当前方案'},'confirm-current')
    local.run_task(task['id'])
    assert local.tasks[task['id']]['status']=='succeeded'
    assert local.tasks[task['id']]['result_url'].endswith('tab=itinerary')
    before.status='已确认'
    assert local.plans[tid]==before
