"""Wire compatibility for the shared role loop; no provider-side conversation state."""
# 负责在两种模型线协议间转换请求与响应，不依赖服务端会话状态。
from copy import deepcopy
from types import SimpleNamespace

from app.core.config import store_responses, wire_api


# 表示模型回合未完整结束，调用方不得执行其中可能残缺的工具请求。
class IncompleteModelOutput(ValueError):
    """The provider returned a partial turn that must never execute tools."""


# 仅这些结束原因代表 Chat Completions 回合可安全消费。
_COMPLETE_CHAT_REASONS = {None, 'stop', 'tool_calls', 'function_call'}


# 读取并严格校验服务端响应存储开关，保留现有调用接口。
def response_storage_enabled():
    return store_responses()


# 将本地聊天消息转换为 Responses API 输入，并在同一角色内重放推理与函数项。
def responses_input(messages):
    items = []
    for message in messages:
        if message.get('_responses_output') is not None:
            # 在当前角色的本地上下文中成组重放推理项和函数调用项。
            items.extend(deepcopy(message['_responses_output']))
        elif message['role'] == 'tool':
            items.append({'type': 'function_call_output', 'call_id': message['tool_call_id'],
                          'output': message['content']})
        elif message['role'] == 'assistant':
            if message.get('content'):
                items.append({'role': 'assistant', 'content': message['content']})
            for call in message.get('tool_calls', []):
                items.append({'type': 'function_call', 'call_id': call['id'],
                              'name': call['function']['name'], 'arguments': call['function']['arguments']})
        else:
            items.append({'role': message['role'], 'content': message.get('content') or ''})
    return items


# 验证 Chat Completions 只返回一个完整选择，拒绝截断或过滤结果。
def _chat_finish_reason(response):
    choices = list(getattr(response, 'choices', None) or [])
    if len(choices) != 1:
        raise IncompleteModelOutput('Model response did not contain exactly one choice')
    reason = getattr(choices[0], 'finish_reason', None)
    if reason not in _COMPLETE_CHAT_REASONS:
        raise IncompleteModelOutput(
            f'Model response was truncated or filtered; partial output was discarded ({reason or "unknown"})'
        )
    return reason


# 按选定线协议发起模型请求，并归一化两种 API 的回复形状。
def create_completion(client, params, api):
    if api == 'chat_completions':
        payload = {**params, 'store': response_storage_enabled()}
        payload['messages'] = [{k: v for k, v in m.items() if k != '_responses_output'} for m in params['messages']]
        response = client.chat.completions.create(**payload)
        _chat_finish_reason(response)
        return response
    # Responses API 将聊天消息、思考档位、工具和输出格式映射到各自字段。
    payload = {'model': params['model'], 'input': responses_input(params['messages']),
               'store': response_storage_enabled(), 'include': ['reasoning.encrypted_content']}
    if 'timeout' in params:
        payload['timeout'] = params['timeout']
    if 'reasoning_effort' in params:
        payload['reasoning'] = {'effort': params['reasoning_effort']}
    if params.get('tools'):
        payload['tools'] = [{'type': 'function', 'strict': False, **spec['function']} for spec in params['tools']]
    if params.get('response_format'):
        fmt = params['response_format']
        payload['text'] = {'format': ({'type': 'json_schema', **fmt['json_schema']}
                                     if fmt['type'] == 'json_schema' else deepcopy(fmt))}
    response = client.responses.create(**payload)
    if response.status != 'completed':
        reason = getattr(getattr(response, 'incomplete_details', None), 'reason', None)
        raise IncompleteModelOutput(
            f'Model response is {response.status}; incomplete output was discarded ({reason or "unknown"})'
        )
    # 将 Responses 输出拆成文本、函数调用和可供后续重放的原始项。
    output = [item.model_dump(exclude_none=True) for item in response.output]
    text = ''.join(content.get('text', '') for item in output if item['type'] == 'message'
                   for content in item.get('content', []) if content['type'] == 'output_text')
    calls = [SimpleNamespace(id=item['call_id'], function=SimpleNamespace(name=item['name'], arguments=item['arguments']))
             for item in output if item['type'] == 'function_call']
    message = SimpleNamespace(content=text, tool_calls=calls, _responses_output=output)
    usage = response.usage
    details = getattr(usage, 'output_tokens_details', None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=SimpleNamespace(
        prompt_tokens=getattr(usage, 'input_tokens', None), completion_tokens=getattr(usage, 'output_tokens', None),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=getattr(details, 'reasoning_tokens', None))))


# 生成可记录的请求参数副本，去除仅供线协议重放的不透明推理状态。
def visible_params(params):
    # 不透明推理状态在线路请求中必需，但不应写入文本日志。
    return {**params, 'messages': [{k: v for k, v in m.items() if k != '_responses_output'} for m in params['messages']]}
