"""Planning prerequisites, trusted evidence and the shared result commit boundary."""
from copy import deepcopy
from dataclasses import asdict, replace
import importlib
import json
from threading import Event, Thread

import pytest

from app import agent_client
from app.agent_client import AgentReply
from app.models import AgentCard, TripRequirements
from app.persistence import _plan
from app.planning import planning_brief
from app.store import DemoStore
from test_agent_roles import completion, delegate, scripted, ready_for_planning, requirements_unchanged, update_call, display_data
from test_text_itinerary import web

store_module = importlib.import_module('app.store')


def result(text='完整文字行程', **kw):
    return AgentReply(text, AgentCard('itinerary-draft','行程',text,[],[]),[],**kw)


@pytest.mark.parametrize('start,end,days,nights',[
    ('2026-11-14','2026-11-14',1,0),
    ('2026-11-14','2026-11-15',2,1),
    ('2026-10-31','2026-11-02',3,2),
])
def test_window_is_derived_from_saved_dates(start,end,days,nights):
    local=DemoStore(persistence_enabled=False)
    trip=local.trips['trip-hangzhou']
    trip.requirements=TripRequirements(city='杭州',start_date=start,end_date=end,
                                      arrival_time='10:00',departure_time='18:00')
    brief,question=planning_brief(trip)
    assert not question and brief['day_count']==days and brief['overnight_count']==nights
    assert len(brief['dates'])==days and brief['dates'][0]==start and brief['dates'][-1]==end
    assert brief['arrival']['location'] is None and brief['time_basis']=='旅游城市当地时间'


@pytest.mark.parametrize('changes',[
    {'city':''}, {'start_date':''}, {'end_date':''},
    {'start_date':'2026-11-15','end_date':'2026-11-14'},
    {'start_date':'2026-11-14','end_date':'2026-11-14','arrival_time':'18:00','departure_time':'10:00'},
    {'start_date':'2026-01-01','end_date':'2027-01-02'},
])
def test_missing_or_conflicting_window_stops_before_any_model_query(monkeypatch,changes):
    local=DemoStore(persistence_enabled=False)
    ready_for_planning(local,'trip-hangzhou')
    for key,value in changes.items():setattr(local.trips['trip-hangzhou'].requirements,key,value)
    plan=deepcopy(local.plans['trip-hangzhou'])
    queries=[]
    monkeypatch.setattr(agent_client,'run_tool',lambda *a,**kw:queries.append(a))
    calls=scripted(monkeypatch,[])
    local.generate_plan('trip-hangzhou')
    assert not calls and not queries
    assert local.plans['trip-hangzhou']==plan
    assert local.messages['trip-hangzhou'][-1].body.startswith(('需要补充：','需要确认：'))


def test_main_cannot_skip_requirement_sync_before_planning(monkeypatch):
    local=DemoStore(persistence_enabled=False)
    trip=local.create_trip('未命名旅行','','','','')
    text='2026年11月14日10点杭州东站到，15日18点同站走，两个人合计2000元，西湖必去，不吃辣，请规划。'
    changes={'city':'杭州','start_date':'2026-11-14','end_date':'2026-11-15',
        'arrival_time':'10:00','departure_time':'18:00','arrival_location':'杭州东站','departure_location':'杭州东站',
        'travelers':2,'budget_amount':'2000','budget_scope':'total','budget_coverage':'含住宿餐饮当地交通，不含往返车票',
        'must_visit':{'add':['西湖']},'dietary':{'add':['不吃辣']}}
    requests=scripted(monkeypatch,[delegate('planning',text),update_call(text,changes),
        completion('杭州两日，住宿一晚。交通待核实。'),completion(display_data())])
    local.add_user_message(trip.id,text)
    assert local.plans[trip.id].display_status == 'pending'
    local.run_pending_display_jobs()
    assert [kw['agent_role'] for _,kw in requests]==['main','planning','planning','display']
    assert json.loads(requests[1][0][1]['content'])['user_message']==text
    payload=json.loads(next(m['content'] for m in reversed(requests[2][0]) if m['role']=='tool'))
    assert payload['requirements']['requirements']['city']=='杭州'
    assert payload['travel_window']['overnight_count']==1
    assert local.trips[trip.id].requirements.must_visit==['西湖']
    assert local.trips[trip.id].requirements.dietary==['不吃辣']
    assert local.plans[trip.id].requirements_revision==local.trips[trip.id].requirements.revision==1
    assert not hasattr(local, 'plan_versions')


def test_main_cannot_skip_requirement_sync_by_using_revision(monkeypatch):
    local=DemoStore(persistence_enabled=False)
    tid='trip-hangzhou'
    ready_for_planning(local,tid)
    text='预算改成2500元，按新预算重新规划整份行程。'
    calls=scripted(monkeypatch,[delegate('revision',text,scope='all'),update_call(text,{'budget_amount':'2500'}),
        completion('按2500元完整调整的方案'),completion(display_data())])
    local.add_user_message(tid,text)
    local.run_pending_display_jobs()
    assert [kw['agent_role'] for _,kw in calls]==['main','revision','revision','display']
    assert local.trips[tid].requirements.budget_amount=='2500'
    assert local.trips[tid].requirements.revision==local.plans[tid].requirements_revision==1
    assert local.plans[tid].text=='按2500元完整调整的方案'
    assert not hasattr(local, 'plan_versions')


@pytest.mark.parametrize('ask_in_requirements',[False,True])
def test_chat_missing_date_never_reaches_planner_or_external_tools(monkeypatch,ask_in_requirements):
    local=DemoStore(persistence_enabled=False)
    trip=local.create_trip('杭州','','','','')
    text='帮我排杭州两天轻松游。'
    data=completion('需要补充：哪两天出行？') if ask_in_requirements else update_call(text,{'city':'杭州'})
    responses = [delegate('planning',text), data]
    if not ask_in_requirements:
        responses.append(completion('需要补充：哪两天出行？'))
    calls=scripted(monkeypatch,responses)
    queries=[]
    monkeypatch.setattr(agent_client,'run_tool',lambda *a,**kw:queries.append(a))
    local.add_user_message(trip.id,text)
    assert [kw['agent_role'] for _,kw in calls]==['main']+['planning']*(1 if ask_in_requirements else 2) and not queries
    assert not local.plans[trip.id].has_content
    assert '需要补充' in local.messages[trip.id][-1].body


def test_evidence_is_written_by_tools_and_survives_revision_and_restart(monkeypatch,tmp_path):
    local=DemoStore(db_path=str(tmp_path/'evidence.sqlite3'),persistence_enabled=True)
    trip=local.create_trip('杭州','','','','')
    ready_for_planning(local,trip.id)
    source={'status':'unavailable','reason':'超出预报范围','source':'受控天气服务'}
    monkeypatch.setattr(agent_client,'run_tool',lambda *a,**kw:deepcopy(source))
    scripted(monkeypatch,[completion(calls=[('query_weather',{'city':'杭州','date':'2026-11-14'})]),
        completion('文字草案。天气未知。')])
    local.generate_plan(trip.id)
    original=deepcopy(local.plans[trip.id])
    assert len(original.evidence)==1
    assert original.evidence[0]['result']==source
    assert original.evidence[0]['seconds']>=0 and original.evidence[0]['queried_at']
    assert original.requirements_revision==0
    requests=scripted(monkeypatch,[requirements_unchanged(), completion(calls=[('read_context',{'section':'evidence'})]),completion('文字草案。天气未知。带伞。')])
    local.request_revision(trip.id,feedback='只追加带伞提醒。')
    context=json.loads([m for m,kw in requests if kw['agent_role']=='revision'][-1][-1]['content'])
    assert context['saved_results']==original.evidence
    assert local.plans[trip.id].evidence==original.evidence
    assert not hasattr(local, 'plan_versions')
    reopened=DemoStore(db_path=local.db_path,persistence_enabled=True)
    assert reopened.plans[trip.id].evidence==original.evidence
    assert reopened.plans[trip.id].text=='文字草案。天气未知。带伞。'
    assert not hasattr(reopened, 'plan_versions')


def test_same_commit_boundary_applies_revision_requirements_and_text_once(monkeypatch):
    local=DemoStore(persistence_enabled=False)
    tid='trip-hangzhou'
    original=deepcopy(local.plans[tid])
    feedback='预算改成2000，按新预算调整。'
    observed=[]
    def model(**kw):
        observed.append(kw['user_body'])
        return result('调整后的完整文字',plan_text='调整后的完整文字',requirements_patch={
            'changes':{'budget_amount':'2000'},'evidence':feedback})
    monkeypatch.setattr(store_module,'generate_agent_reply',model)
    local.request_revision(tid,feedback=feedback)
    assert observed==[feedback]
    assert local.trips[tid].requirements.budget_amount=='2000'
    assert local.trips[tid].requirements.revision==1
    assert not hasattr(local, 'plan_versions')
    assert local.plans[tid].requirements_revision==1


def test_mixed_confirmation_and_plan_write_is_rejected_without_partial_commit():
    local=DemoStore(persistence_enabled=False)
    tid='trip-hangzhou'
    before=deepcopy((local.trips[tid],local.plans[tid]))
    reply=result(plan_text='不应保存',requirements_patch={'budget_amount':'9999'},
                 confirm_current=True)
    message=local._agent_reply(tid,'确认',llm_reply=reply)
    assert message.status=='failed'
    assert (local.trips[tid],local.plans[tid])==before


def test_superseded_task_cannot_write_late_evidence(monkeypatch):
    local=DemoStore(persistence_enabled=False)
    tid='trip-hangzhou'
    entered,release=Event(),Event()
    def model(**kw):
        if kw['user_body']=='旧规划':
            entered.set()
            assert release.wait(8)
            return result('旧',plan_text='旧',evidence=[{'tool':'query_weather','result':{'source':'旧证据'}}])
        return result('新',plan_text='新',evidence=[{'tool':'query_weather','result':{'source':'新证据'}}])
    monkeypatch.setattr(store_module,'generate_agent_reply',model)
    old,_=local.submit_task(tid,'chat',{'body':'旧规划'},'old-evidence')
    thread=Thread(target=local.run_task,args=(old['id'],))
    thread.start()
    try:
        assert entered.wait(2)
        new,_=local.submit_task(tid,'chat',{'body':'新规划'},'new-evidence')
        local.run_task(new['id'])
    finally:
        release.set()
        thread.join(4)
    assert local.plans[tid].evidence[0]['result']['source']=='新证据'
    assert not hasattr(local, 'plan_versions')
    assert local.tasks[old['id']]['status']=='superseded'


def test_old_current_plan_loads_without_new_metadata():
    local=DemoStore(persistence_enabled=False);p=asdict(local.plans['trip-hangzhou'])
    p.pop('evidence');p.pop('requirements_revision')
    assert _plan(p).evidence==[] and _plan(p).requirements_revision is None


def test_evidence_page_escapes_sources_and_marks_changed_requirements(web):
    client,local,trip=web
    poison='<script>window.evidencePoison=1</script>'
    ledger=[{'tool':'query_weather','arguments':{'city':'杭州'},'result':{
        'status':'unavailable','source':poison,'reason':'超出预报范围'},'queried_at':'2026-10-08T00:00:00+00:00','seconds':0.1}]
    local._apply_text_plan(trip.id,'完整文字',evidence=ledger)
    local.update_trip_requirements(trip.id,{'budget_amount':'2000'},0)
    page=client.get(f'/workspace/{trip.id}?tab=itinerary')
    assert page.status_code==200
    assert 'data-plan-evidence' in page.text and '超出预报范围' in page.text
    assert '需求已更新，当前方案尚未按新需求重新规划。' in page.text
    assert poison not in page.text and '&lt;script&gt;' in page.text
    assert 'data-task-status' not in page.text
