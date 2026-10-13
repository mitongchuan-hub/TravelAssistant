"""Exact v1 controlled system cases; scripted model outputs do not measure model quality."""
from copy import deepcopy
from dataclasses import asdict
import importlib
from threading import Event, Thread

import pytest
from fastapi.testclient import TestClient
from app.agent_client import AgentReply
from app.agent_runtime import TravelAgentRuntime
from app.auth import AuthStore
from app.models import AgentCard
from app.store import DemoStore
from app.tasks import TaskConflict
from test_agent_roles import completion, scripted
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run_travel_dataset import create_fixture, DATA

store_module = importlib.import_module('app.store')
main_module = importlib.import_module('app.main')


def reply(text, **kwargs):
    return AgentReply(text, AgentCard('requirement-summary','测试',text,[],[]),[],**kwargs)


@pytest.mark.parametrize('case_id', ['TA-030','TA-031'])
def test_dataset_supersession(monkeypatch, case_id):
    local, tid = create_fixture('saved')
    original = deepcopy(local.plans[tid])
    entered, release = Event(), Event()
    calls = []
    def model(**kw):
        calls.append(kw['user_body'])
        if len(calls) == 1:
            entered.set()
            assert release.wait(8)
            if case_id == 'TA-031':
                raise TimeoutError('late old failure')
            return reply('不应显示的旧回复',plan_text='不应保存的旧行程',requirements_patch={
                'changes':{'budget_amount':'9999'},'evidence':kw['user_body']})
        return reply('当前两人合计2000元，含住宿、餐饮、市内交通、门票，不含往返大交通。')
    monkeypatch.setattr(store_module,'generate_agent_reply',model)
    case = next(c for c in DATA['cases'] if c['id'] == case_id)
    old,_ = local.submit_task(tid,'chat',{'body':case['steps'][0]['input']},case_id+'-A')
    old_thread = Thread(target=local.run_task,args=(old['id'],))
    old_thread.start()
    try:
        assert entered.wait(2)
        new,_ = local.submit_task(tid,'chat',{'body':case['steps'][1]['input']},case_id+'-B')
        new_thread = Thread(target=local.run_task,args=(new['id'],))
        new_thread.start()
        new_thread.join(2)
        assert not new_thread.is_alive()
        assert local.tasks[new['id']]['status'] == 'succeeded'
        assert local.tasks[old['id']]['status'] == 'superseded'
        assert not any(m.status == 'thinking' for m in local.messages[tid])
    finally:
        release.set()
        old_thread.join(4)
    assert local.plans[tid] == original
    assert local.trips[tid].requirements.budget_amount == '2000'
    assert local.tasks[old['id']]['status'] == 'superseded'
    assert not local.tasks[old['id']]['result_url']
    assert not any(m.status == 'failed' or '不应' in m.body for m in local.messages[tid])


def test_TA032_queued_replacement(monkeypatch):
    local,tid = create_fixture('saved')
    calls=[]
    monkeypatch.setattr(store_module,'generate_agent_reply',lambda **kw:calls.append(kw['user_body']) or reply('你好'))
    old,_=local.submit_task(tid,'chat',{'body':'重新规划。'},'TA032-A')
    new,_=local.submit_task(tid,'chat',{'body':'取消刚才的请求，只说你好。'},'TA032-B')
    assert not calls
    local.run_task(old['id'])
    local.run_task(new['id'])
    assert calls == ['取消刚才的请求，只说你好。']
    assert local.tasks[old['id']]['status']=='superseded'
    assert not hasattr(local, 'plan_versions')


def test_TA033_idempotency(monkeypatch):
    local,tid=create_fixture('saved')
    entered,release=Event(),Event()
    calls=[]
    def model(**kw):
        calls.append(kw['user_body'])
        entered.set()
        assert release.wait(8)
        return reply('当前方案包括西湖、休息和返站。')
    monkeypatch.setattr(store_module,'generate_agent_reply',model)
    old,_=local.submit_task(tid,'chat',{'body':'解释当前方案。'},'TA033-A')
    thread=Thread(target=local.run_task,args=(old['id'],))
    thread.start()
    try:
        assert entered.wait(2)
        duplicate,created=local.submit_task(tid,'chat',{'body':'解释当前方案。'},'TA033-A')
        assert not created and duplicate['id']==old['id'] and duplicate['status']=='running'
        assert len(calls)==1 and sum(m.sender_type=='user' for m in local.messages[tid])==1
    finally:
        release.set()
        thread.join(4)
    assert local.tasks[old['id']]['status']=='succeeded'
    before=deepcopy(local.messages[tid])
    with pytest.raises(TaskConflict):
        local.submit_task(tid,'chat',{'body':'不同的内容。'},'TA033-A')
    assert local.messages[tid] == before


def test_TA034_tool_timing(monkeypatch):
    local,tid=create_fixture('hangzhou')
    events=[]
    clock=[10.0]
    monkeypatch.setattr('app.agent_runtime.monotonic',lambda:clock[0])
    requests=[]
    def request(*a,**kw):
        clock[0]+=2.0
        requests.append(kw)
        if len(requests)==1:
            return completion(calls=[('query_weather',{'city':'杭州'})])
        return completion('已有需求，未知事实待核实。')
    runtime=TravelAgentRuntime(object(),'controlled',{'trip':local.trips[tid],'user_body':'读取并查询，再交给查询Agent。'},request=request)
    runtime.event=lambda title,value:events.append((title,deepcopy(value)))
    original=runtime.read_context
    def read(args):
        clock[0]+=0.125
        return original(args)
    monkeypatch.setattr(runtime,'read_context',read)
    def external(name,*a,**kw):
        clock[0]+=1.25
        if name=='query_route':raise TimeoutError('injected tool timeout')
        return {'status':'unavailable','reason':'unknown'}
    monkeypatch.setattr('app.agent_client.run_tool',external)
    runtime.execute_tool('research','read_context',{'section':'requirements'})
    runtime.execute_tool('research','query_weather',{'city':'杭州'})
    with pytest.raises(TimeoutError):runtime.execute_tool('research','query_route',{})
    runtime.run('research','读取并查询')
    records=[v for t,v in events if t=='工具执行']
    assert len(records)==4
    assert [r['seconds'] for r in records]==[0.125,1.25,1.25,1.25]
    assert records[3]['role']=='research'
    assert records[2]['error_type']=='TimeoutError'
    assert all(r['role'] and r['name'] and 'arguments' in r for r in records)


def test_TA036_long_text_restart_and_page(monkeypatch,tmp_path):
    local,tid=create_fixture('hangzhou')
    from test_agent_roles import ready_for_planning
    ready_for_planning(local,tid)
    local.db_path=str(tmp_path/'dataset.sqlite3')
    local.persistence_enabled=True
    local._persist()
    text='完整中文行程\n'+('上午游览，午后休息，未知待核实。\n'*1000)+'<script>window.__travel_test=1</script>\n【行程结束-TA036】'
    assert len(text)>10000
    scripted(monkeypatch,[completion(text)])
    local.generate_plan(tid)
    assert local.plans[tid].text == text and (not local.plans[tid].days)
    reopened=DemoStore(db_path=local.db_path,persistence_enabled=True)
    assert reopened.plans[tid].text == text
    auth=AuthStore('unused.sqlite3',False)
    auth.create_account('dataset@example.com',local.trips[tid].owner.id,'dataset-password-2026')
    monkeypatch.setattr(main_module,'store',reopened)
    monkeypatch.setattr(main_module,'auth',auth)
    with TestClient(main_module.app) as client:
        client.post('/login',data={'email':'dataset@example.com','password':'dataset-password-2026'})
        for _ in range(2):
            page=client.get(f'/workspace/{tid}?tab=itinerary')
            assert page.status_code==200
            assert '【行程结束-TA036】' in page.text
            assert '<script>window.__travel_test=1</script>' not in page.text
            assert '&lt;script&gt;window.__travel_test=1&lt;/script&gt;' in page.text
    assert reopened.plans[tid].text==text
