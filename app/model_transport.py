"""Wire compatibility for the shared role loop; no provider-side conversation state."""
from copy import deepcopy
import os
from types import SimpleNamespace


class IncompleteModelOutput(ValueError):
    """The provider returned a partial turn that must never execute tools."""


_COMPLETE_CHAT_REASONS = {None, 'stop', 'tool_calls', 'function_call'}


def wire_api():
    value = os.getenv('OPENAI_WIRE_API', 'chat_completions').strip().lower()
    if value not in {'chat_completions', 'responses'}:
        raise ValueError('OPENAI_WIRE_API must be chat_completions or responses')
    return value


def response_storage_enabled():
    value = os.getenv('OPENAI_STORE_RESPONSES', 'false').strip().lower()
    if value not in {'true', 'false'}:
        raise ValueError('OPENAI_STORE_RESPONSES must be true or false')
    return value == 'true'


def responses_input(messages):
    items = []
    for message in messages:
        if message.get('_responses_output') is not None:
            # Replay reasoning and function-call items together, locally, within this role.
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


def create_completion(client, params, api):
    if api == 'chat_completions':
        payload = {**params, 'store': response_storage_enabled()}
        payload['messages'] = [{k: v for k, v in m.items() if k != '_responses_output'} for m in params['messages']]
        response = client.chat.completions.create(**payload)
        _chat_finish_reason(response)
        return response
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


def visible_params(params):
    # Opaque reasoning state is needed on the wire but is not part of text logs.
    return {**params, 'messages': [{k: v for k, v in m.items() if k != '_responses_output'} for m in params['messages']]}
