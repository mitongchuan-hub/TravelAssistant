"""Workflow prerequisites, display conversion, creation form and version isolation."""
from copy import deepcopy
from dataclasses import asdict
import importlib
import json
from pathlib import Path

import pytest
from app import agent_client
from app.agent_runtime import AgentBudgetError
from app.plan_display import text_hash, validate_display
from app.store import DemoStore
from test_agent_roles import completion, route, update_call, scripted, ready_for_planning, display_data, requirements_unchanged
from test_text_itinerary import web


def creation_fields(city='杭州'):
    """Full-travel fixture retained for legacy lifecycle and display tests."""
    return {'city': city, 'start_date': '2026-11-14', 'end_date': '2026-11-15',
        'arrival_time': '10:00', 'departure_time': '18:00', 'arrival_location': city+'站',
        'departure_location': city+'站', 'travelers': '2', 'budget_amount': '2000',
        'budget_scope': 'total', 'budget_coverage': '含住宿餐饮当地交通和门票，不含往返车票'}


def city_creation_fields(city='杭州'):
    return {'lodging_address': city+'湖滨酒店', 'city_hint': city}


def test_creation_requires_lodging_address(web):
    client, local, _ = web
    before = len(local.trips)
    page = client.post('/trips', data={'city_hint': '杭州'}, follow_redirects=False)
    assert page.status_code == 400 and len(local.trips) == before
    assert '住宿地址' in page.text


@pytest.mark.parametrize('changes', [
    {'day_count': '0'}, {'travelers': '0'}, {'travelers': '1.5'},
    {'budget_amount': 'NaN'}, {'budget_amount': '10', 'budget_scope': 'unspecified'},
    {'outing_start_time': '18:00', 'outing_end_time': '10:00'},
])
def test_creation_rejects_invalid_values_without_creating_trip(web, changes):
    client, local, _ = web
    values = {**city_creation_fields(), **changes}
    before = len(local.trips)
    page = client.post('/trips', data=values, follow_redirects=False)
    assert page.status_code == 400 and len(local.trips) == before
    assert '杭州湖滨酒店' in page.text


def test_creation_saves_durable_lodging_and_optional_settings(web, monkeypatch):
    client, local, _ = web
    from test_city_outings import LODGING
    monkeypatch.setattr('app.main.search_places', lambda args: {
        'status': 'available', 'total_matched': 1, 'candidates': [LODGING]})
    page = client.get('/trips/new')
    assert 'name="lodging_address"' in page.text
    assert 'name="arrival_location"' not in page.text
    response = client.post('/trips', data=city_creation_fields(), follow_redirects=False)
    assert response.status_code == 303
    tid = response.headers['location'].split('/')[2].split('?')[0]
    p = local.trips[tid].requirements
    assert p.city == '杭州市' and p.travelers is None and p.day_count == 1
    assert p.budget_amount == '' and p.budget_scope == 'unspecified'
    welcome = local.messages[tid][0]
    assert welcome.agent_card.kind == 'requirement-summary' and not welcome.agent_card.bullets
    chat = client.get(response.headers['location'])
    assert chat.status_code == 200 and '湖滨住处' in chat.text
    assert '可以直接说' in chat.text and '生成路线' in chat.text
    local.clear_chat(tid)
    assert local.trips[tid].requirements == p


@pytest.mark.parametrize('entry', ['page', 'chat', 'replan'])
def test_generated_text_is_saved_without_a_second_model_verdict(monkeypatch, entry):
    local=DemoStore(persistence_enabled=False)
    trip=local.create_trip('杭州','','','','')
    ready_for_planning(local,trip.id)
    source='推荐主线：湖滨散步，午后休息。\n\n---\n\n完整逐日安排，预算与待确认条件。'
    text='生成行程吧' if entry!='replan' else '整体重新安排，节奏轻松'
    replies=[]
    if entry=='chat':
        replies=[route('planning',text,'generate'),requirements_unchanged()]
    elif entry=='replan':
        local._apply_text_plan(trip.id,'原有行程')
        replies=[route('revision',text,'modify','all'),requirements_unchanged()]
    calls=scripted(monkeypatch,[*replies,completion(source)])
    if entry=='page':
        local.generate_plan(trip.id)
    else:
        local.add_user_message(trip.id,text)
    expected=['planning'] if entry=='page' else ['main']+['revision' if entry=='replan' else 'planning']*2
    assert [kwargs['agent_role'] for _,kwargs in calls]==expected
    assert local.plans[trip.id].text==local.messages[trip.id][-1].body==source
    assert local.messages[trip.id][-1].status=='sent'
    assert local.plans[trip.id].display_status=='pending'


@pytest.mark.parametrize('key',['city','start_date','end_date','arrival_time','departure_time','arrival_location',
    'departure_location','travelers','budget_amount','budget_coverage'])
def test_missing_required_information_stops_page_generation_before_models(monkeypatch,key):
    local=DemoStore(persistence_enabled=False);tid='trip-hangzhou';ready_for_planning(local,tid)
    setattr(local.trips[tid].requirements,key,None if key=='travelers' else '')
    before=deepcopy(local.plans[tid]);calls=scripted(monkeypatch,[])
    local.generate_plan(tid)
    assert not calls and local.plans[tid]==before
    assert local.messages[tid][-1].body.startswith('需要补充')
    assert local.trips[tid].workflow_state['awaiting_generation']


def test_waiting_goal_survives_restart_and_is_visible_to_intent_model(monkeypatch,tmp_path):
    local=DemoStore(db_path=str(tmp_path/'waiting.sqlite3'),persistence_enabled=True)
    trip=local.create_trip('杭州','','','','');ready_for_planning(local,trip.id)
    trip.requirements.travelers=None;trip.requirements.budget_amount=''
    scripted(monkeypatch,[]);local.generate_plan(trip.id)
    restored=DemoStore(db_path=local.db_path,persistence_enabled=True)
    text='两人，预算合计2000元'
    calls=scripted(monkeypatch,[route('planning',text,'generate'),update_call(text,{'travelers':2,'budget_amount':'2000'}),
        completion('补齐信息后的完整行程'),completion(display_data())])
    restored.add_user_message(trip.id,text)
    context = json.loads(calls[0][0][1]['content'])
    assert len(calls[0][0]) == 2 and context['user_message'] == text
    assert context['workflow_state']['awaiting_generation']
    assert json.loads(calls[1][0][1]['content'])['action']=='generate'
    assert restored.plans[trip.id].text=='补齐信息后的完整行程'
    assert not restored.trips[trip.id].workflow_state


def test_incomplete_requirement_update_stops_rest_of_tool_batch(monkeypatch):
    local=DemoStore(persistence_enabled=False);trip=local.create_trip('杭州','','','','');text='去杭州，请生成'
    calls=scripted(monkeypatch,[route('planning',text,'generate'),completion(calls=[
        ('update_requirements',{'user_quote':text,'changes':{'city':'杭州'}}),('query_weather',{'city':'杭州'})])])
    queries=[];monkeypatch.setattr(agent_client,'run_tool',lambda *a,**kw:queries.append(a))
    local.add_user_message(trip.id,text)
    assert not queries and len(calls)==2
    assert local.trips[trip.id].requirements.city=='杭州' and not local.plans[trip.id].has_content


def test_generation_cannot_skip_original_message_review(monkeypatch):
    local=DemoStore(persistence_enabled=False);tid='trip-hangzhou';ready_for_planning(local,tid);text='按当前需求生成'
    calls=scripted(monkeypatch,[route('planning',text,'generate'),completion('试图跳过检查的方案'),
        requirements_unchanged(),completion('经过检查的完整方案'),completion(display_data())])
    local.add_user_message(tid,text)
    local.run_pending_display_jobs()
    assert local.plans[tid].text=='经过检查的完整方案'
    assert [kw['agent_role'] for _,kw in calls]==['main','planning','planning','planning','display']


@pytest.mark.parametrize('failure',[ValueError('invalid JSON'),TimeoutError('conversion timeout'),completion('not-json')])
def test_display_failure_preserves_prose_and_successful_task(monkeypatch,failure):
    local=DemoStore(persistence_enabled=False);tid='trip-hangzhou';ready_for_planning(local,tid)
    responses = [completion('完整原文，预约待核实。'), failure]
    if not isinstance(failure, Exception): responses.append(completion('still-not-json'))
    calls=scripted(monkeypatch,responses)
    task,_=local.submit_task(tid,'plan',{},'display-fallback');local.run_task(task['id'])
    assert local.tasks[task['id']]['status']=='succeeded' and local.plans[tid].display_status=='pending'
    local.run_pending_display_jobs()
    assert local.tasks[task['id']]['status']=='succeeded'
    assert local.plans[tid].text=='完整原文，预约待核实。'
    assert local.plans[tid].display is None and local.plans[tid].display_status=='fallback'
    assert len(calls)==(2 if isinstance(failure, Exception) else 3)
    assert all(kw['tools'] is None for _, kw in calls[1:])


def test_display_expiring_budget_does_not_discard_completed_prose(monkeypatch):
    local=DemoStore(persistence_enabled=False);tid='trip-hangzhou';ready_for_planning(local,tid)
    def expire(messages,kw):
        kw['budget'].deadline=0;return completion(display_data())
    scripted(monkeypatch,[completion('业务阶段已完成'),expire]);local.generate_plan(tid)
    local.run_pending_display_jobs()
    assert local.plans[tid].text=='业务阶段已完成' and local.plans[tid].display_status=='fallback'


def test_supersession_before_commit_discards_prose_display_and_evidence(monkeypatch):
    local=DemoStore(persistence_enabled=False);tid='trip-hangzhou';ready_for_planning(local,tid)
    original=deepcopy(local.plans[tid])
    def replaced(messages,kw):
        local.submit_task(tid,'chat',{'body':'新消息，取消旧方案'},'new-before-commit')
        return completion('旧轮原文')
    scripted(monkeypatch,[replaced])
    task,_=local.submit_task(tid,'plan',{},'old-before-commit');local.run_task(task['id'])
    assert local.tasks[task['id']]['status']=='superseded'
    assert local.plans[tid] == original and not local.display_jobs


def test_latest_text_display_and_basis_survive_confirmation_and_restart(monkeypatch,tmp_path):
    local=DemoStore(db_path=str(tmp_path/'display.sqlite3'),persistence_enabled=True)
    trip=local.create_trip('杭州','','','','');ready_for_planning(local,trip.id)
    expected=display_data();scripted(monkeypatch,[completion('原文1'),completion(expected)])
    local.generate_plan(trip.id)
    local.run_pending_display_jobs()
    assert local.plans[trip.id].display==expected
    local.confirm_plan(trip.id)
    local.update_trip_requirements(trip.id,{'budget_amount':'3000'},0)
    scripted(monkeypatch,[requirements_unchanged(),completion('原文2'),completion(expected)])
    local.request_revision(trip.id,feedback='只增加备注')
    local.run_pending_display_jobs()
    local.clear_chat(trip.id)
    reopened=DemoStore(db_path=local.db_path,persistence_enabled=True)
    assert reopened.plans[trip.id].text=='原文2'
    assert reopened.plans[trip.id].display==expected
    assert reopened.plans[trip.id].display_source_hash==text_hash('原文2')
    assert reopened.plans[trip.id].requirements_snapshot['budget_amount']=='3000'
    assert reopened.trips[trip.id].requirements.budget_amount=='3000'
    assert not hasattr(reopened,'plan_versions')


def test_wrong_source_hash_cannot_bind_json_to_different_text():
    local=DemoStore(persistence_enabled=False)
    local._apply_text_plan('trip-hangzhou','原文B',display=display_data(),display_source_hash=text_hash('原文A'),display_status='ready')
    assert local.plans['trip-hangzhou'].display is None and local.plans['trip-hangzhou'].display_status=='fallback'


def test_display_cards_escape_all_model_fields_and_keep_original(web):
    client,local,trip=web;data=display_data();poison='<script>window.injected=1</script>'
    data['days'][0]['items']=[{'time_text':'午后','title':poison,'category':'meal','location':None,
        'duration_text':None,'description':poison,'cost_text':'预算预留，非报价','notes':[poison]}]
    local._apply_text_plan(trip.id,'完整原文',display=data,display_source_hash=text_hash('完整原文'),display_status='ready')
    page=client.get(f'/workspace/{trip.id}?tab=itinerary')
    assert page.status_code==200 and 'data-display-itinerary' in page.text
    assert poison not in page.text and '&lt;script&gt;' in page.text
    assert '午后' in page.text and '非报价' in page.text and '查看完整行程原文' in page.text
    assert 'data-display-edit' in page.text
    local.update_trip_requirements(trip.id,{'start_date':'2026-11-16','end_date':'2026-11-17'},0)
    refreshed=client.get(f'/workspace/{trip.id}?tab=itinerary')
    assert '2026-11-14 — 2026-11-15' in refreshed.text
    assert '当前方案尚未按新需求重新规划' in refreshed.text
    assert local.trip_metrics(trip.id)['plan_item_count']==1


def test_example_json_schema_keeps_vague_times_and_unknowns():
    data=json.loads(Path('docs/travel-itinerary-display-example.json').read_text(encoding='utf-8'))
    result=validate_display(data)
    assert result['days'][1]['items'][0]['time_text']=='午后'
    assert result['days'][0]['items'][0]['duration_text'] is None
    assert '非餐厅报价' in result['days'][0]['items'][2]['cost_text']
    data['days'][0]['items'][0]['longitude']=120
    with pytest.raises(Exception):validate_display(data)


def test_provider_schema_is_expanded_and_still_validates_example():
    from jsonschema import Draft202012Validator, FormatChecker
    from app.plan_display import display_format
    schema=display_format()['json_schema']['schema']
    assert '$ref' not in json.dumps(schema) and '$defs' not in schema
    example=json.loads(Path('docs/travel-itinerary-display-example.json').read_text(encoding='utf-8'))
    Draft202012Validator(schema,format_checker=FormatChecker()).validate(example)


def test_invalid_schema_is_not_retried_as_unconstrained_json(monkeypatch):
    import httpx,openai
    from app.plan_display import display_format
    monkeypatch.setenv('OPENAI_WIRE_API','responses')
    attempts=[]
    def reject(request):
        attempts.append(json.loads(request.content))
        return httpx.Response(400,json={'error':{'code':'invalid_json_schema','message':'Invalid schema for response_format',
            'param':'text.format.schema','type':'invalid_request_error'}})
    with openai.OpenAI(api_key='fixture-only',base_url='https://fixture.invalid/v1',max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(reject))) as client:
        with pytest.raises(openai.BadRequestError):
            agent_client._create_chat_completion(client,'fixture',[],response_format=display_format())
    assert len(attempts)==1 and attempts[0]['text']['format']['type']=='json_schema'


def test_clear_chat_cancels_active_conversion_and_pending_goal(monkeypatch):
    local=DemoStore(persistence_enabled=False);tid='trip-hangzhou';ready_for_planning(local,tid)
    original=deepcopy(local.plans[tid]);local.trips[tid].workflow_state={'awaiting_generation':True}
    def clear(messages,kw):
        local.clear_chat(tid);return completion(display_data())
    scripted(monkeypatch,[completion('不能回写的旧原文'),clear])
    task,_=local.submit_task(tid,'plan',{},'clear-during-display');local.run_task(task['id'])
    assert local.plans[tid].text=='不能回写的旧原文'
    local.run_pending_display_jobs()
    assert not local.messages[tid] and not local.trips[tid].workflow_state
    assert local.tasks[task['id']]['status']=='succeeded'
    assert local.plans[tid].text=='不能回写的旧原文' and local.plans[tid].display_status=='fallback'


def test_page_generation_has_no_requirement_update_tool(monkeypatch):
    local=DemoStore(persistence_enabled=False);tid='trip-hangzhou';ready_for_planning(local,tid)
    calls=scripted(monkeypatch,[completion('原文'),completion(display_data())])
    local.generate_plan(tid)
    names={s['function']['name'] for s in calls[0][1]['tools']}
    assert 'update_requirements' not in names


def test_intent_clarification_does_not_authorize_future_generation(monkeypatch):
    local,trip=DemoStore(persistence_enabled=False),None
    trip=local.create_trip('杭州','','','','');ready_for_planning(local,trip.id)
    text='我还在考虑是否规划'
    calls=scripted(monkeypatch,[route('planning',text,'generate',question='需要现在生成行程吗？')])
    local.add_user_message(trip.id,text)
    assert len(calls)==1 and not local.trips[trip.id].workflow_state
    assert not hasattr(local, 'plan_versions')
