"""Four intent routes and one shared business-agent loop."""
from copy import deepcopy
from datetime import date
from dataclasses import asdict
import json
from types import SimpleNamespace

import pytest
from app import agent_client
from app.agent_runtime import TravelAgentRuntime, TurnBudget, AgentBudgetError
from app.store import DemoStore


def completion(content='', calls=()):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
        tool_calls=[SimpleNamespace(id=f'call-{i}', function=SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False))) for i,(name,args) in enumerate(calls)]))])


def plan_text(raw):
    lines=[raw['title'],raw.get('overview','')]
    for day in raw['days']:
        lines.append('日期：'+day.get('date','')+'；主题：'+day.get('theme',''))
        for item in day['items']:
            lines.append(item['time']+' '+item['title'])
            lines.extend(f'{k}：{json.dumps(v,ensure_ascii=False)}' for k,v in item.items() if k not in {'incoming_route','departure_route','place','time','title'})
    lines.extend(raw.get('pending_items',[]))
    return '\n'.join(lines)


def route(intent, text, action='reply', scope='', question=None):
    return completion({'intent':intent,'action':action,'scope':scope,'user_quote':text,'question':question})


def delegate(agent, text, *, finish=True, scope=None):
    """Fixture alias retained for callers; responses now contain intent routing, never delegation tools."""
    intent={'requirements':'planning','research':'query','planning':'planning','revision':'revision'}[agent]
    action={'requirements':'update_requirements','research':'reply','planning':'generate','revision':'explain' if scope=='explain' else 'modify'}[agent]
    return route(intent,text,action,scope or ('item' if agent=='revision' else ''))


def requirements_unchanged():
    def reviewed(messages,kwargs):
        text=json.loads(messages[1]['content'])['user_message']
        return completion(calls=[('check_requirements',{'user_quote':text})])
    return reviewed


def update_call(text,changes,**kw):
    return completion(calls=[('update_requirements',{'user_quote':text,'changes':changes,**kw})])


def display_data(dates=('2026-11-14','2026-11-15')):
    return {'schema_version':'1.1','title':'行程','overview':None,'budget_note':None,
        'days':[{'date':d,'label':f'第{i+1}天','theme':None,'items':[]} for i,d in enumerate(dates)],
        'preparation':{'clothing':[],'accommodation':[]},'weather':[], 'pending_items':[],'risks':[]}


def scripted(monkeypatch,responses):
    requests=[]; iterator=iter(responses)
    def create(client,model,messages,**kwargs):
        if kwargs.get('budget'):kwargs['budget'].model()
        requests.append((deepcopy(messages),kwargs))
        response=next(iterator)
        if callable(response):response=response(messages,kwargs)
        if isinstance(response,Exception):raise response
        return response
    monkeypatch.setattr(agent_client,'_create_chat_completion',create)
    monkeypatch.setattr(agent_client,'is_llm_enabled',lambda:True)
    monkeypatch.setattr(agent_client.ConversationTrace,'write',lambda *a,**kw:None)
    monkeypatch.setattr('openai.OpenAI',lambda **kwargs:object())
    return requests


def ready_for_planning(local,trip_id):
    p=local.trips[trip_id].requirements
    defaults={'city':'杭州','start_date':'2026-11-14','end_date':'2026-11-15','arrival_time':'10:00',
        'departure_time':'18:00','arrival_location':'杭州东站','departure_location':'杭州东站',
        'travelers':2,'budget_amount':'1500','budget_scope':'total','budget_coverage':'含住宿餐饮当地交通，不含往返车票'}
    for key,value in defaults.items():
        if not getattr(p,key) or key=='budget_scope' and p.budget_scope=='unspecified':setattr(p,key,value)
    p.day_count=(date.fromisoformat(p.end_date)-date.fromisoformat(p.start_date)).days+1


def local_trip():
    local=DemoStore(persistence_enabled=False)
    trip=local.create_trip('杭州','','人均1500','轻松','')
    ready_for_planning(local,trip.id)
    return local,trip


def test_casual_chat_uses_classifier_and_plain_llm_without_business_context(monkeypatch):
    local,trip=local_trip()
    local.update_trip_requirements(trip.id,{'dietary':['私有忌口']},0)
    local.plans[trip.id].title='private-plan'
    calls=scripted(monkeypatch,[route('chat','你好'),completion('你好，想聊些什么？')])
    local.add_user_message(trip.id,'你好')
    assert [kw['agent_role'] for _,kw in calls]==['main','chat']
    assert all(kw['tools'] is None for _,kw in calls)
    assert all('private-plan' not in m[1]['content'] and '私有忌口' not in m[1]['content'] for m,_ in calls)
    assert local.messages[trip.id][-1].body == '你好，想聊些什么？'


def test_requirements_are_tools_inside_planning_without_replanning(monkeypatch):
    local,trip=local_trip(); text='预算改成人均2000，不吃海鲜'
    calls=scripted(monkeypatch,[delegate('requirements',text),update_call(text,{'budget_amount':'2000','dietary':{'add':['不吃海鲜']}}),completion('需求已记录。')])
    local.add_user_message(trip.id,text)
    assert [kw['agent_role'] for _,kw in calls]==['main','planning','planning']
    assert local.trips[trip.id].requirements.revision==1
    assert local.trips[trip.id].requirements.dietary==['不吃海鲜']
    assert not hasattr(local, 'plan_versions')
    assert all(not s['function']['name'].startswith('delegate_') for _,kw in calls for s in (kw.get('tools') or []))


@pytest.mark.parametrize('failed',[False,True])
def test_staged_requirements_and_partial_success(monkeypatch,failed):
    local,trip=local_trip();text='预算改为2000，然后生成行程'
    replies=[delegate('planning',text),update_call(text,{'budget_amount':'2000'})]
    replies += [TimeoutError('upstream')] if failed else [completion('完整行程，交通待核实。'),completion(display_data())]
    calls=scripted(monkeypatch,replies)
    local.add_user_message(trip.id,text)
    if not failed:
        assert local.plans[trip.id].display_status == 'pending'
        local.run_pending_display_jobs()
    assert local.trips[trip.id].requirements.budget_amount=='2000'
    assert not hasattr(local, 'plan_versions')
    assert [kw['agent_role'] for _,kw in calls]==(['main','planning','planning'] if failed else ['main','planning','planning','display'])
    if failed:assert '需求变更已保存' in local.messages[trip.id][-1].body
    else:assert local.plans[trip.id].display_status=='ready'


def test_query_can_use_tools_but_cannot_write_requirements_or_plan(monkeypatch):
    local,trip=local_trip();text='查天气'
    calls=scripted(monkeypatch,[delegate('research',text),completion(calls=[('update_requirements',{'user_quote':text,'changes':{'travelers':99}})]),
        completion(calls=[('query_weather',{'city':'杭州'})]),completion('天气待核实。')])
    queries=[]
    monkeypatch.setattr(agent_client,'run_tool',lambda *a,**kw:queries.append(a) or {'status':'unavailable'})
    local.add_user_message(trip.id,text)
    assert len(queries)==1 and local.trips[trip.id].requirements.travelers==2
    assert not hasattr(local, 'plan_versions')
    assert '不允许' in calls[2][0][-1]['content']


def test_nested_delegation_and_unknown_tools_are_denied(monkeypatch):
    local,trip=local_trip();text='查询景点'
    calls=scripted(monkeypatch,[delegate('research',text),completion(calls=[('delegate_planning',{}),('delete_trip',{})]),completion('只查询信息。')])
    local.add_user_message(trip.id,text)
    assert all('不允许' in m['content'] for m in calls[-1][0] if m['role']=='tool')
    assert not hasattr(local, 'plan_versions')


def test_revision_stages_requirements_then_saves_text_and_display(monkeypatch):
    local=DemoStore(persistence_enabled=False);tid='trip-hangzhou';text='预算改为2000，调整备注'
    calls=scripted(monkeypatch,[delegate('revision',text,scope='item'),update_call(text,{'budget_amount':'2000'}),completion('修改后的完整原文'),completion(display_data())])
    local.add_user_message(tid,text)
    local.run_pending_display_jobs()
    assert local.plans[tid].text=='修改后的完整原文' and local.trips[tid].requirements.budget_amount=='2000'
    assert not hasattr(local, 'plan_versions')
    assert [kw['agent_role'] for _,kw in calls]==['main','revision','revision','display']


def test_ambiguous_revision_stops_without_formatting(monkeypatch):
    local=DemoStore(persistence_enabled=False);before=deepcopy(local.plans['trip-hangzhou']);text='晚饭换一家'
    calls=scripted(monkeypatch,[delegate('revision',text,scope='item'),completion('需要补充：哪一天的晚餐？')])
    local.add_user_message('trip-hangzhou',text)
    assert len(calls)==2 and local.plans['trip-hangzhou']==before


def test_explanation_is_read_only(monkeypatch):
    local=DemoStore(persistence_enabled=False);before=deepcopy(local.plans['trip-hangzhou']);text='解释第一天'
    calls=scripted(monkeypatch,[delegate('revision',text,scope='explain'),completion(calls=[('read_context',{'section':'plan'})]),completion('第一天先抵达后休息。')])
    local.add_user_message('trip-hangzhou',text)
    assert {s['function']['name'] for s in calls[1][1]['tools']}=={'read_context'}
    assert local.plans['trip-hangzhou']==before


def test_classifier_cannot_execute_tools(monkeypatch):
    local,trip=local_trip()
    calls=scripted(monkeypatch,[completion(calls=[('delegate_planning',{})])])
    local.add_user_message(trip.id,'你好')
    assert len(calls) == 1 and local.messages[trip.id][-1].status == 'failed'


def test_snapshot_reads_do_not_leak_other_trips_and_paginate():
    local,trip=local_trip()
    for n in range(25):local.add_user_message(trip.id,f'自己的消息{n}',process_agent=False)
    other=local.create_trip('别人旅行','','','','')
    local.add_user_message(other.id,'other-trip-secret',process_agent=False)
    runtime=TravelAgentRuntime(object(),'test',{'trip':trip,'messages':local.messages[trip.id]})
    first=runtime.read_context({'section':'messages','query':'自己的消息','limit':20})
    second=runtime.read_context({'section':'messages','query':'自己的消息','offset':first['next_offset']})
    assert first['total']==25 and len(second['items'])==5
    assert runtime.read_context({'section':'messages','query':'other-trip-secret'})['total']==0


def test_shared_model_budget_includes_classifier(monkeypatch):
    calls=scripted(monkeypatch,[route('chat','你好'),completion('不应执行')])
    runtime=TravelAgentRuntime(object(),'test',{'user_body':'你好'},cache={'_budget':TurnBudget(max_models=1)},request=agent_client._create_chat_completion)
    with pytest.raises(AgentBudgetError):runtime.run()
    assert len(calls)==1


def test_tool_batch_is_rejected_before_external_query(monkeypatch):
    queries=[];monkeypatch.setattr(agent_client,'run_tool',lambda *a,**kw:queries.append(a))
    scripted(monkeypatch,[completion(calls=[('query_weather',{'city':'杭州'}),('search_places',{'city':'杭州','keywords':'西湖'})])])
    runtime=TravelAgentRuntime(object(),'test',{'user_body':'查询'},cache={'_budget':TurnBudget(max_tools=1)},request=agent_client._create_chat_completion)
    with pytest.raises(AgentBudgetError):runtime.run('research')
    assert not queries


def test_chat_confirmation_binds_current_plan_without_target_id(monkeypatch):
    local=DemoStore(persistence_enabled=False);tid='trip-hangzhou'
    local.plans[tid].pending_items=['门票待确认'];before=deepcopy(local.plans[tid])
    calls=scripted(monkeypatch,[route('revision','确认当前行程','confirm')])
    local.add_user_message(tid,'确认当前行程')
    expected=deepcopy(before);expected.status='已确认'
    assert local.plans[tid]==expected and len(calls)==1
    assert calls[0][1]['tools'] is None


def test_same_id_change_during_request_blocks_save(monkeypatch):
    local=DemoStore(persistence_enabled=False);tid='trip-hangzhou';ready_for_planning(local,tid)
    def concurrent(messages,kwargs):
        local.confirm_plan(tid);return completion('新原文')
    scripted(monkeypatch,[concurrent,completion(display_data())])
    local.generate_plan(tid)
    assert local.plans[tid].status == '已确认'
    assert any('已变化' in m.body for m in local.messages[tid])


def test_late_requirement_response_cannot_write(monkeypatch):
    local,trip=local_trip();text='预算改成2000'
    def late(messages,kwargs):
        kwargs['budget'].deadline=0;return update_call(text,{'budget_amount':'2000'})
    scripted(monkeypatch,[delegate('requirements',text),late])
    local.add_user_message(trip.id,text)
    assert local.trips[trip.id].requirements.budget_amount=='1500' and local.messages[trip.id][-1].status=='failed'


def test_fabricated_user_quote_rejected(monkeypatch):
    local,trip=local_trip()
    scripted(monkeypatch,[route('planning','预算改成2000','generate')])
    local.add_user_message(trip.id,'你好')
    assert local.messages[trip.id][-1].status == 'failed'


def test_interest_can_be_idea_without_becoming_must_visit(monkeypatch):
    local,trip=local_trip();text='对西湖有兴趣，先记下来'
    scripted(monkeypatch,[delegate('requirements',text),update_call(text,{},idea_cards=[{'kind':'地点','title':'西湖','body':text}]),completion('已记录想法。')])
    local.add_user_message(trip.id,text)
    assert not local.trips[trip.id].requirements.must_visit and local.idea_cards[trip.id][-1].title=='西湖'
