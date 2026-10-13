"""Memory-only app with deterministic workflow replies for browser acceptance."""
from pathlib import Path
import json
import os
import sys
from types import SimpleNamespace
from threading import Event

old_started=Event();release_old=Event()
display_started=Event();release_display=Event();hold_display=Event();fail_display=Event()
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
os.environ['TRAVEL_PERSISTENCE']='0';os.environ['TRAVEL_AGENT_DISABLE_LLM']='0';os.environ['OPENAI_API_KEY']='fixture-only'
from app import agent_client
from app.main import app,auth
from app.store import store
import uvicorn

PLAN='杭州一日行程\n\n上午：西湖散步。\n午餐：附近用餐，不吃海鲜。\n下午：原地休息。\n\n开放与交通待核实。\n<script>window.untrusted = true</script>\n'+'很长的地点说明'*80
REVISED=PLAN.replace('原地休息','湖边休息，减少步行')

def response(content='',calls=()):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content,
        tool_calls=[SimpleNamespace(id=f'call-{i}',function=SimpleNamespace(name=n,arguments=json.dumps(a,ensure_ascii=False))) for i,(n,a) in enumerate(calls)]))])

def respond(client,model,messages,**kwargs):
    if kwargs.get('budget'):kwargs['budget'].model()
    role=kwargs['agent_role'];payload=json.loads(messages[1]['content']);text=payload.get('user_message','')
    if role=='main':
        if text=='旧生成':old_started.set();release_old.wait(20)
        intent='chat';action='reply'
        if text=='旧生成':intent='planning';action='generate'
        return response(json.dumps({'intent':intent,'action':action,'scope':'','user_quote':text,'question':None},ensure_ascii=False))
    if role=='chat':return response('最新回复已完成' if text=='最新消息' else '你好，想聊些什么？')
    if role=='display':
        if hold_display.is_set():
            display_started.set()
            if not release_display.wait(30):raise TimeoutError('controlled display gate timeout')
            hold_display.clear()
        if fail_display.is_set():
            fail_display.clear()
            raise TimeoutError('controlled display failure')
        source=payload['source_text'];dates=payload['source_dates'];rest='湖边休息，减少步行' if '湖边休息' in source else '原地休息'
        items=[]
        for time,title,category,location in [('上午','西湖散步','activity','西湖'),('午餐','附近用餐','meal','附近'),('下午',rest,'rest',None)]:
            items.append({'time_text':time,'title':title,'category':category,'location':location,'duration_text':None,
                'description':None,'cost_text':None,'notes':['不吃海鲜'] if category=='meal' else []})
        data={'schema_version':'1.1','weather':[],'title':'杭州一日行程','overview':None,'budget_note':None,
            'days':[{'date':dates[0],'label':'第1天','theme':'轻松游','items':items}],
            'preparation':{'clothing':[],'accommodation':[]},'pending_items':['开放与交通待核实。'],
            'risks':['<script>window.untrusted = true</script>']}
        return response(json.dumps(data,ensure_ascii=False))
    tools=[m for m in messages if m['role']=='tool']
    if role=='planning':
        if not tools:return response(calls=[('query_weather',{'city':'杭州','date':payload['travel_window']['start_date']})])
        return response(PLAN)
    if role=='revision':
        if not tools:return response(calls=[('check_requirements',{'user_quote':text}),('read_context',{'section':'plan'})])
        current=json.loads(tools[-1]['content'])['plan']['text']
        return response(current.replace('原地休息','湖边休息，减少步行'))
    raise AssertionError(role)

agent_client._create_chat_completion=respond
agent_client.run_tool=lambda *a,**kw:{'status':'unavailable','reason':'超出预报范围','source':'受控天气资料'}
agent_client.ConversationTrace.write=lambda *a,**kw:None
user=store.create_user('文字行程浏览器测试')
auth.create_account('text-browser@example.com',user.id,'text-browser-2026')
fields={'city':'杭州','start_date':'2026-11-01','end_date':'2026-11-01','arrival_time':'10:00','departure_time':'18:00',
    'arrival_location':'杭州东站','departure_location':'杭州东站','travelers':2,'budget_amount':'800','budget_scope':'per_person',
    'budget_coverage':'含餐饮当地交通门票，不含往返车票'}
trip=store.create_trip('杭州文字行程','','','','',user,requirements=fields)

@app.get('/_test/old-started')
def old_state():return {'started':old_started.is_set()}

@app.post('/_test/release-old')
def release_request():release_old.set();return {'released':True}

@app.post('/_test/block-display')
def block_display():
    display_started.clear();release_display.clear();hold_display.set();return {'blocked':True}

@app.get('/_test/display-started')
def display_state():return {'started':display_started.is_set()}

@app.post('/_test/release-display')
def release_cards():release_display.set();return {'released':True}

@app.post('/_test/fail-display')
def fail_cards():fail_display.set();return {'will_fail':True}

if __name__=='__main__':uvicorn.run(app,host='127.0.0.1',port=int(sys.argv[1]),log_level='warning')
