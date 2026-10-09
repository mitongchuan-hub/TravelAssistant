"""Responses SDK wire tests: stateless tool replay, storage, formats and failures."""
from copy import deepcopy
import json

import httpx
import openai
import pytest

from app.agent_client import _create_chat_completion, ConversationTrace
from app.agent_runtime import TravelAgentRuntime, TurnBudget
from app.store import DemoStore


@pytest.fixture(autouse=True)
def responses_env(monkeypatch):
    monkeypatch.setenv('OPENAI_WIRE_API', 'responses')
    monkeypatch.setenv('OPENAI_STORE_RESPONSES', 'false')


def sdk(handler):
    return openai.OpenAI(api_key='test-only', base_url='https://model.invalid/v1', max_retries=0,
                         http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def response(output=None, status='completed'):
    output = output if output is not None else [{'type':'message','id':'msg_final','role':'assistant','status':'completed',
        'content':[{'type':'output_text','text':'资料待核实。','annotations':[]}]}]
    return httpx.Response(200,json={'id':'resp_test','object':'response','created_at':1,'model':'gpt-5.6-sol',
        'status':status,'output':output,'incomplete_details':{'reason':'max_output_tokens'} if status=='incomplete' else None,
        'usage':{'input_tokens':12,'output_tokens':8,'total_tokens':20,'output_tokens_details':{'reasoning_tokens':3}}})


def test_role_loop_replays_reasoning_call_and_tool_result_without_server_state():
    requests=[]
    def handler(request):
        payload=json.loads(request.content)
        requests.append(payload)
        assert request.url.path=='/v1/responses'
        if len(requests)==1:
            return response([
                {'type':'reasoning','id':'rs_1','summary':[], 'encrypted_content':'opaque-private-state'},
                {'type':'function_call','id':'fc_1','call_id':'call_1','name':'read_context',
                 'arguments':'{"section":"requirements"}','status':'completed'}])
        return response()
    local=DemoStore(persistence_enabled=False)
    trace=ConversationTrace('responses','now','synthetic','查需求')
    with sdk(handler) as client:
        runtime=TravelAgentRuntime(client,'gpt-5.6-sol',{'trip':local.trips['trip-hangzhou'],'user_body':'查需求'},
            trace=trace,request=_create_chat_completion)
        runtime.run('research')
    assert len(requests)==2
    assert all(p['store'] is False and 'previous_response_id' not in p for p in requests)
    assert all(p['include']==['reasoning.encrypted_content'] for p in requests)
    assert requests[0]['reasoning']=={'effort':'low'}
    assert all(t['type']=='function' and 'function' not in t and t['strict'] is False for t in requests[0]['tools'])
    replay=requests[1]['input']
    assert sum(item.get('type')=='reasoning' for item in replay)==1
    assert sum(item.get('type')=='function_call' for item in replay)==1
    output=next(item for item in replay if item.get('type')=='function_call_output')
    assert output['call_id']=='call_1' and json.loads(output['output'])['requirements']
    assert runtime.cache['final_reply'].body=='资料待核实。'
    log=''.join(trace.events)
    assert 'opaque-private-state' not in log
    assert '"reasoning_tokens": 3' in log and '"wire_api": "responses"' in log
    assert runtime.budget.model_calls==2


@pytest.mark.parametrize('fmt,expected',[
    ({'type':'json_object'},{'type':'json_object'}),
    ({'type':'json_schema','json_schema':{'name':'req','strict':True,'schema':{'type':'object','properties':{},'additionalProperties':False}}},
     {'type':'json_schema','name':'req','strict':True,'schema':{'type':'object','properties':{},'additionalProperties':False}}),
])
def test_format_conversion_and_default_effort(fmt,expected):
    requests=[]
    with sdk(lambda request:requests.append(json.loads(request.content)) or response()) as client:
        result=_create_chat_completion(client,'gpt-5.6-sol',[{'role':'user','content':'test'}],response_format=fmt)
    assert requests[0]['text']=={'format':expected}
    assert 'response_format' not in requests[0] and 'reasoning' not in requests[0]
    assert result.choices[0].message.content=='资料待核实。'


@pytest.mark.parametrize('status',['incomplete','failed','cancelled'])
def test_non_completed_response_is_discarded(status):
    with sdk(lambda request:response(status=status)) as client:
        with pytest.raises(ValueError,match='incomplete output was discarded'):
            _create_chat_completion(client,'gpt-5.6-sol',[{'role':'user','content':'test'}])


def test_explicit_reasoning_effort_rejection_retries_once_with_same_input():
    requests=[]
    def handler(request):
        payload=json.loads(request.content)
        requests.append(payload)
        if len(requests)==1:
            return httpx.Response(400,json={'error':{'param':'reasoning.effort','message':'Unsupported value for reasoning.effort',
                                                    'code':'unsupported_value'}})
        return response()
    budget=TurnBudget()
    with sdk(handler) as client:
        _create_chat_completion(client,'gpt-5.6-sol',[{'role':'user','content':'test'}],reasoning_effort='medium',budget=budget)
    assert len(requests)==budget.model_calls==2
    first=deepcopy(requests[0])
    del first['reasoning']
    assert first==requests[1] and first['store'] is False


def test_auth_error_never_falls_back_to_another_protocol():
    requests=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(401,json={'error':{'message':'Unauthorized','type':'authentication_error'}})
    with sdk(handler) as client,pytest.raises(openai.AuthenticationError):
        _create_chat_completion(client,'gpt-5.6-sol',[],reasoning_effort='low')
    assert len(requests)==1 and requests[0].url.path=='/v1/responses'


def test_unknown_protocol_fails_before_request(monkeypatch):
    monkeypatch.setenv('OPENAI_WIRE_API','invalid')
    requests=[]
    with sdk(lambda request:requests.append(request) or response()) as client,pytest.raises(ValueError,match='OPENAI_WIRE_API'):
        _create_chat_completion(client,'gpt-5.6-sol',[])
    assert not requests
