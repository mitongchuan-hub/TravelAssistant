"""Measure current workflow latency with live services and isolated SQLite data.

Runs serially: chat/query/confirmation three times and plan/revision twice by
 default. Captures failed waits too. Does not grade full itinerary quality.
"""
from __future__ import annotations
import argparse
from collections import Counter,defaultdict
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime,date,timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import statistics
import subprocess
import sys
from time import monotonic

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
os.environ['TRAVEL_PERSISTENCE']='0'  # Prevent imports from loading the business Store.
os.environ['TRAVEL_AGENT_DISABLE_LLM']='0'
os.environ['PYTHONDONTWRITEBYTECODE']='1'
os.environ.pop('PYTEST_CURRENT_TEST',None)
from app import agent_client
from app.store import DemoStore
from app.plan_display import text_hash

BASE_FIELDS={'city':'杭州','start_date':'2026-11-14','end_date':'2026-11-14',
 'arrival_time':'10:00','departure_time':'18:00','arrival_location':'杭州东站',
 'departure_location':'杭州东站','travelers':'2','budget_amount':'800','budget_scope':'total',
 'budget_coverage':'含当地交通、餐饮和门票，不含往返车票'}
SAVED_TEXT='''杭州当天往返草案
2026年11月14日，2成人，10:00到杭州东站，18:00从杭州东站离开。
到站后前往西湖湖滨，接驳方式及实际耗时待查询，另预留30分钟衔接缓冲（估算）。
12:00午餐，不吃辣。13:00—15:00西湖湖滨散步，中间坐下休息。
15:00后休息并准备返站，目标17:00前到杭州东站，额外候车时间为人为预留。
当天不住宿。两人总预算800元，含当地交通、餐饮和门票，不含往返车票。
待核实：餐厅营业、实际接驳耗时、门票及预算可行性。'''


def business_state():
    with sqlite3.connect((ROOT/'.cache/travelassistant.sqlite3').resolve().as_uri()+'?mode=ro',uri=True) as c:
        raw=c.execute("SELECT payload FROM app_snapshots WHERE key='demo_store_v1'").fetchone()[0]
        data=json.loads(raw)
    return {'payload_sha256':hashlib.sha256(raw.encode()).hexdigest(),
        'trip_count':len(data['trips']),'message_count':sum(map(len,data['messages'].values())),
        'active_tasks':sum(t.get('status') in ('queued','running') for t in data.get('tasks',{}).values())}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',required=True)
    parser.add_argument('--light-repeats',type=int,default=3)
    parser.add_argument('--heavy-repeats',type=int,default=2)
    parser.add_argument('--resume',action='store_true',help='Keep completed samples and continue the unfinished run.')
    args=parser.parse_args()
    if min(args.light_repeats,args.heavy_repeats)<1:parser.error('repeat counts must be positive')
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    before_business=business_state()
    records=json.loads((out/'results.json').read_text(encoding='utf-8')) if args.resume and (out/'results.json').exists() else []
    completed={r['case'] for r in records}
    if args.resume and (out/'business-before.json').exists():
        before_business=json.loads((out/'business-before.json').read_text(encoding='utf-8'))
    active={};model_calls=[];events=[]
    batch_started=monotonic()
    metadata={'started_at':datetime.now().isoformat(),'model':os.getenv('OPENAI_MODEL'),
        'base_url':os.getenv('OPENAI_BASE_URL'),'wire_api':os.getenv('OPENAI_WIRE_API'),
        'store_responses':os.getenv('OPENAI_STORE_RESPONSES'),'fallback':os.getenv('OPENAI_FALLBACK_MODEL'),
        'live_external_tools':True,'execution':'serial','light_repeats':args.light_repeats,
        'heavy_repeats':args.heavy_repeats,'planning_entry':'page task, no intent classifier',
        'revision_entry':'page task, no intent classifier','confirmation_entry':'chat task',
        'fields':BASE_FIELDS,'saved_fixture':SAVED_TEXT,
        'planning_note':'只安排西湖湖滨散步、附近午餐和原地休息。当天往返，不需要具体门店或过夜，完整说明顺序、时间、交通、休息与待核实。',
        'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'scope':'Current code, before implementing the newly approved product boundaries.',
        'excludes':'Browser rendering/polling and concurrent server load; no full quality pass implied.'}
    if args.resume and (out/'metadata.json').exists():
        earlier=json.loads((out/'metadata.json').read_text(encoding='utf-8'))
        earlier.setdefault('resumed_at',[]).append(datetime.now().isoformat())
        metadata=earlier
    (out/'metadata.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'business-before.json').write_text(json.dumps(before_business,indent=2),encoding='utf-8')
    secrets=sorted({v for k,v in os.environ.items() if v and any(t in k for t in ('KEY','TOKEN','PASSWORD')) and len(v)>12},key=len,reverse=True)
    def redact(text):
        for value in secrets:text=text.replace(value,'[REDACTED]')
        return text
    def dump(path,value):
        path.write_text(redact(json.dumps(value,ensure_ascii=False,indent=2,default=str)),encoding='utf-8')
    original_add=agent_client.ConversationTrace.add
    original_request=agent_client._create_chat_completion
    def observe(trace,title,value):
        clean=json.loads(redact(json.dumps(value,ensure_ascii=False,default=str)))
        original_add(trace,title,clean)
        events.append({'request_id':trace.request_id,'title':title,'value':clean,
            'offset_seconds':round(monotonic()-active['started'],6) if active else None})
        if active and title in ('模型请求','模型响应','工具执行','文字行程','展示转换'):
            dump(out/'activity.json',{'case':active['case'],'event':title,
                'role':clean.get('agent_role',clean.get('role')) if isinstance(clean,dict) else None,
                'offset_seconds':round(monotonic()-active['started'],6),'updated_at':datetime.now().isoformat()})
    def write_trace(trace,error=None):
        if error:trace.add('调用异常',type(error).__name__+': '+str(error))
        directory=active['directory'] if active else out
        (directory/(trace.request_id+'.txt')).write_text(redact(''.join(trace.events)),encoding='utf-8')
    def timed_request(*a,**kw):
        start=monotonic();error=None
        row={'role':kw.get('agent_role'),'started_offset':round(start-active['started'],6)}
        try:return original_request(*a,**kw)
        except Exception as exc:
            error=type(exc).__name__;raise
        finally:
            row.update(seconds=round(monotonic()-start,6),error_type=error)
            model_calls.append(row)
    agent_client.ConversationTrace.add=observe
    agent_client.ConversationTrace.write=write_trace
    agent_client._create_chat_completion=timed_request

    def make_store(label,*,text=None,note=''):
        directory=out/label;directory.mkdir(exist_ok=True)
        local=DemoStore(db_path=str(directory/'isolated.sqlite3'),persistence_enabled=True,display_worker_enabled=False)
        trip=local.create_trip('杭州','','','轻松',note,requirements=BASE_FIELDS)
        local.update_trip_requirements(trip.id,{'must_visit':['西湖湖滨'],'dietary':['不吃辣'],'pace':'轻松'},trip.requirements.revision)
        local.messages[trip.id]=[]
        if text:local._apply_text_plan(trip.id,text)
        local._persist()
        return local,trip.id,directory

    def run(kind,index,local,tid,directory,payload):
        label=f'{kind}-{index}';target=directory if directory.name==label else directory/label
        target.mkdir(exist_ok=True)
        plan_before=deepcopy(local.plans[tid]);req_before=deepcopy(local.trips[tid].requirements)
        active.clear();active.update(started=monotonic(),directory=target,case=label)
        event_offset,model_offset=len(events),len(model_calls)
        print(json.dumps({'event':'start','case':label},ensure_ascii=False),flush=True)
        task_kind={'planning':'plan','revision':'revision'}.get(kind,'chat')
        task=None;run_error=None;primary_elapsed=None
        try:
            task,_=local.submit_task(tid,task_kind,payload,'latency-'+label)
            accepted=monotonic()-active['started']
            local.run_task(task['id'])
            primary_elapsed=monotonic()-active['started']
            local.run_pending_display_jobs()
        except Exception as exc:
            accepted=monotonic()-active['started'];run_error=type(exc).__name__
        total=monotonic()-active['started'];selected=deepcopy(events[event_offset:])
        calls=deepcopy(model_calls[model_offset:]);plan=local.plans[tid]
        current=local.tasks.get(task['id'],{}) if task else {}
        message=local.messages[tid][-1] if local.messages[tid] else None
        outputs=[e for e in selected if e['title']=='文字行程']
        tools=[e['value'] for e in selected if e['title']=='工具执行']
        external=[t for t in tools if t['name'] in ('query_weather','query_route','search_places','search_food')]
        by_role=defaultdict(float)
        for call in calls:by_role[call['role']]+=call['seconds']
        success=current.get('status')=='succeeded' and bool(message and message.status=='sent')
        checks={}
        if kind in ('chat','query'):
            checks['plan_and_requirements_preserved']=plan==plan_before and local.trips[tid].requirements==req_before
        elif kind=='confirm':
            expected=deepcopy(plan_before);expected.status='已确认'
            checks['confirmed_original_preserved']=plan==expected and local.trips[tid].requirements==req_before
        elif kind=='planning':
            checks['complete_text_saved']=bool(plan.text) and any(e['value']['text']==plan.text for e in outputs)
            checks['day_frame']=not plan.display or [d['date'] for d in plan.display['days']]==['2026-11-14']
        elif kind=='revision':
            checks['exact_append']=plan.text==plan_before.text+'\n出发前再次核对交通安排。'
        display_check=None
        if kind in ('planning','revision') and plan.display:
            display_check=plan.display_source_hash==text_hash(plan.text)
            checks['display_original_hash']=display_check
        measured_success=success and all(checks.values())
        body_errors=[str(e['value']) for e in selected if e['title']=='调用异常']
        stage_errors=[e['value'] for e in selected if e['title']=='展示转换' and e['value'].get('status')=='fallback']
        status='completed' if measured_success else 'completed_check_failed' if success else 'failed'
        row={'case':label,'kind':kind,'repeat':index,'status':status,'task_status':current.get('status'),
            'seconds':round(total,6),'accepted_seconds':round(accepted,6),
            'primary_seconds':round(primary_elapsed,6) if primary_elapsed is not None else None,
            'text_ready_seconds':outputs[-1]['offset_seconds'] if outputs else None,
            'display_seconds':round(by_role.get('display',0),6),
            'model_seconds':round(sum(c['seconds'] for c in calls),6),
            'model_seconds_by_role':{k:round(v,6) for k,v in by_role.items()},
            'external_seconds':round(sum(t['seconds'] for t in external),6),
            'internal_tool_seconds':round(sum(t['seconds'] for t in tools if t not in external),6),
            'model_requests':sum(e['title']=='模型请求' for e in selected),
            'external_calls':len(external),'external_statuses':dict(Counter(t.get('result',{}).get('status','error') if isinstance(t.get('result'),dict) else 'error' for t in external)),
            'display_status':plan.display_status if kind in ('planning','revision') else None,
            'characters_before':len(plan_before.text),'characters_after':len(plan.text),
            'cards':sum(len(d['items']) for d in plan.display['days']) if plan.display else 0,
            'checks':checks,'error_type':run_error,'errors':body_errors,'display_errors':stage_errors,
            'model_calls':calls,'reply':message.body if message else None,'plan':asdict(plan),
            'events':selected,'payload':payload,'task':current}
        row['other_seconds']=round(total-row['model_seconds']-row['external_seconds']-row['internal_tool_seconds'],6)
        dump(target/'result.json',row)
        records.append({k:v for k,v in row.items() if k not in ('plan','events','reply','task','model_calls','payload')})
        dump(out/'results.json',records)
        print(json.dumps({k:row[k] for k in ('case','status','seconds','text_ready_seconds','display_seconds','model_seconds','external_seconds','display_status')},ensure_ascii=False),flush=True)
        return row

    for kind in ('chat','query','confirm'):
        for index in range(1,args.light_repeats+1):
            if f'{kind}-{index}' in completed:continue
            local,tid,directory=make_store(f'{kind}-{index}',text=SAVED_TEXT)
            weather_date=(date.today()+timedelta(days=3)).isoformat()
            text={'chat':'你好，简单介绍一下你能怎么帮助国内旅行。',
                'query':f'只查询杭州{weather_date}的天气，说明结果，不生成或修改行程和需求。',
                'confirm':'确认当前方案。'}[kind]
            run(kind,index,local,tid,directory,{'body':text})
    for index in range(1,args.heavy_repeats+1):
        plan_label,revision_label=f'planning-{index}',f'revision-{index}'
        if plan_label in completed and revision_label in completed:continue
        if plan_label in completed:
            directory=out/plan_label
            saved=json.loads((directory/'result.json').read_text(encoding='utf-8'))
            local=DemoStore(db_path=str(directory/'isolated.sqlite3'),persistence_enabled=True,display_worker_enabled=False)
            tid=saved['task']['trip_id']
        else:
            local,tid,directory=make_store(plan_label,note=metadata['planning_note'])
            run('planning',index,local,tid,directory,{})
        if revision_label in completed:continue
        revision_directory=out/revision_label;revision_directory.mkdir(exist_ok=True)
        # Reuse the just-generated content length/evidence when available, avoiding an artificially tiny edit fixture.
        if not local.plans[tid].text:
            local._apply_text_plan(tid,SAVED_TEXT);local._persist()
            revision_directory.joinpath('fixture-note.txt').write_text('Planning failed; revision uses the documented saved fixture.',encoding='utf-8')
        run('revision',index,local,tid,revision_directory,{'feedback':'只在行程原文末尾换行追加“出发前再次核对交通安排。”，其余原文逐字保持；不重新查询资料。'})

    summary={'metadata':metadata,'wall_seconds':round((datetime.now()-datetime.fromisoformat(metadata['started_at'])).total_seconds(),3),
        'active_sample_seconds':round(sum(r['seconds'] for r in records),3),'groups':{},'records':records,
        'measurement_interruptions':json.loads((out/'interruptions.json').read_text(encoding='utf-8')) if (out/'interruptions.json').exists() else [],
        'business_before':before_business,'business_after':business_state()}
    summary['business_data_unchanged']=summary['business_before']==summary['business_after']
    for kind in ('chat','query','confirm','planning','revision'):
        values=[r for r in records if r['kind']==kind]
        good=[r for r in values if r['status']=='completed'];samples=[r['seconds'] for r in good]
        summary['groups'][kind]={'attempts':len(values),'completed':len(good),'failed':len(values)-len(good),
            'successful_seconds':samples,'median_seconds':round(statistics.median(samples),3) if samples else None,
            'min_seconds':round(min(samples),3) if samples else None,'max_seconds':round(max(samples),3) if samples else None,
            'display_fallbacks':sum(r['display_status']=='fallback' for r in values),
            'observed_failed_seconds':[r['seconds'] for r in values if r['status']!='completed']}
    dump(out/'summary.json',summary)
    dump(out/'business-after.json',summary['business_after'])
    out.joinpath('done').write_text('complete',encoding='utf-8')
    assert summary['business_data_unchanged'],'Business payload changed during timing run; inspect before attributing.'
    print(json.dumps({'finished':True,'business_data_unchanged':True,'groups':summary['groups']},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
