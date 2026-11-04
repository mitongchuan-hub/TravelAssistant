"""Pure validation and staged application of one direct-agent CRUD transaction.

A user quote binds the request, not its intent. Destructive and textual operations
also require positive, current instructions; uncertain commands fail closed.
"""
from copy import deepcopy
from hashlib import sha256
import json
import re
from types import SimpleNamespace

from app.domain.itinerary.identity import plan_basis
from app.workflow.execution import serialize
from app.models import TripPlan
from app.services.maps.evidence import evidence_kind, evidence_result
from app.services.itinerary.planning import planning_brief
from app.services.requirements import update_requirements, MAX_REQUIREMENTS_TEXT

_QUOTES = str.maketrans({'“': '"', '”': '"', '‘': "'", '’': "'", '＂': '"', '＇': "'"})
_OPERATIONS = {'requirements_text', 'itinerary'}


def canonical_digest(value):
    return sha256(json.dumps(serialize(value), ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _get(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _text(value, label, maximum=10000, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError(label + '格式不正确')
    return value


def _shape(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ValueError('提交字段缺失或包含未知字段')


def _positive(body, verbs, label):
    # Do not interpret a quoted command, an old agreement, a denial or a
    # hypothetical as fresh authority. Negative travel preferences remain valid.
    pattern = '|'.join(verbs)
    instruction = re.sub(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"', '', body)
    if (re.search(r'(?:如果|假如|假设|要是|是否|能否|可不可以|之前说|上次说|曾经说)', body)
            or re.search(r'(?:不要|别|不想|不能|无需|暂不|不必|不需要|并非要|不是要|不打算)\s*(?:再|帮我|把|将|立即|现在)?[^，。；\n]{0,10}(?:' + pattern + ')', body)
            or re.search(r'不是.{0,8}(?:' + pattern + ')', body)
            or not re.search(pattern, instruction)):
        raise ValueError(label + '需要本轮明确的肯定指令，不能凭原话引用推断授权')


def _unique(source, fragment):
    reference, normalized = fragment.translate(_QUOTES), source.translate(_QUOTES)
    position = normalized.find(reference)
    if not reference or position < 0 or normalized.find(reference, position + 1) >= 0:
        raise ValueError('原文目标须唯一匹配；请提供足够上下文')
    return source[position:position + len(fragment)]


def exact_edit(text, operation, body, display_target=None):
    """Return a user-sourced exact edit, never mutate its source plan."""
    _shape(operation, {'action', 'operation', 'old_text', 'context', 'new_text'},
           {'action', 'operation', 'old_text', 'new_text'})
    _positive(body, ['替换', '改成', '改为', '修改', '删除', '删掉', '去掉', '追加', '添加', '加一行', '补充'], '精确编辑')
    raw_old = operation['old_text']
    if raw_old is None and operation['operation'] == 'append':
        raw_old = ''
    old = _text(raw_old, 'old_text', 100000, empty=True)
    new = _text(operation['new_text'], 'new_text', 10000, empty=True)
    # Always require unique provenance, even when the exact spelling occurs.
    replacement = _unique(body, new) if new else ''
    if not text:
        raise ValueError('当前没有可精确编辑的行程原文')
    if operation['operation'] == 'append':
        if display_target is not None or old or not replacement or len(replacement.splitlines()) != 1:
            raise ValueError('追加只接受无页面目标的单行原话，且 old_text 必须为空')
        return text + ('' if text.endswith('\n') else '\n') + replacement
    if operation['operation'] != 'replace' or not old:
        raise ValueError('替换操作须提供有效原文目标')
    context = operation.get('context') or old
    _text(context, 'context', 100000)
    target = _unique(text, context)
    original = _unique(target, old)
    if display_target is None and old.translate(_QUOTES) not in body.translate(_QUOTES):
        raise ValueError('聊天精确替换必须在本轮原话中指明原文目标')
    if not replacement:
        _positive(body, ['删除', '删掉', '去掉', '移除'], '删除行程文字')
    if display_target is not None:
        # 展示卡片没有可信原文跨度；仅接受唯一标题所在行内的修改。
        # 不能因为模型给的巨大 context 含标题，就允许修改别处的原文。
        title = display_target.get('title')
        if not isinstance(title, str) or not title.strip():
            raise ValueError('页面目标缺少可定位标题，请明确指定原文后重试')
        title_text = _unique(text, title)
        title_start = text.index(title_text)
        line_start = text.rfind('\n', 0, title_start) + 1
        line_end = text.find('\n', title_start)
        if line_end < 0:
            line_end = len(text)
        edit_start = text.index(target) + target.index(original)
        edit_end = edit_start + len(original)
        if not (line_start <= edit_start < edit_end <= line_end):
            raise ValueError('精确编辑超出当前页面站点的可信原文行，请明确目标后重试')
    return text.replace(target, target.replace(original, replacement, 1), 1)


def prepare_changes(args, request_context, execution_context, evidence=()):
    """Validate a detached snapshot and return its canonical change package.

    ``evidence`` is supplied only by the server's query executor, never tool args.
    No store, network, clock, ID allocation or business mutation occurs here.
    """
    args = deepcopy(serialize(args))
    _shape(args, {'user_quote', *_OPERATIONS, 'next_question'}, {'user_quote'})
    if args['user_quote'] != request_context.body:
        raise ValueError('user_quote必须逐字引用完整本轮用户消息')
    if not any(key in args for key in _OPERATIONS):
        raise ValueError('submit_changes至少需要一项实际操作')
    if 'next_question' in args:
        _text(args['next_question'], 'next_question', 2000)
    trip, plan = _get(execution_context, 'trip'), _get(execution_context, 'plan')
    if (trip.id != request_context.trip_id or trip.owner.id != request_context.owner_id
            or trip.requirements.revision != request_context.requirements_revision
            or plan_basis(plan) != request_context.plan_basis):
        raise ValueError('CRUD快照与本轮业务依据不一致')
    body = request_context.body
    display_target = _get(execution_context, 'display_target')
    if display_target is not None:
        from app.workflow.tasks import validate_display_target_metadata
        validate_display_target_metadata(plan, serialize(display_target))
    if 'requirements_text' in args:
        if re.search(r'如果|假如|假设|要是|(?:不要|别|暂不|不必|无需)(?:修改|更新|保存|改动|更改|记录)', body):
            raise ValueError('假设或否定请求不能保存正式需求')
        text = args['requirements_text']
        if isinstance(text, str) and not text.strip() and trip.requirements.text.strip():
            unchanged = text == trip.requirements.text
            if not unchanged and not re.fullmatch(
                    r'\s*(?:请|帮我|请帮我)?\s*(?:清空|删除|删掉|移除)\s*'
                    r'(?:(?:当前|现有|这份|已有)\s*)?(?:(?:全部|所有|整份|完整)\s*)?'
                    r'(?:的)?\s*(?:需求正文|需求文本|旅行需求正文|旅行需求|全部需求|所有需求)'
                    r'\s*(?:吧|。|！|!)?\s*', body):
                raise ValueError('清空需求正文需要明确针对全文需求的独立肯定指令')
        trip = update_requirements(trip, {'text': args['requirements_text']})
    itinerary = args.get('itinerary')
    result_text = None
    if itinerary:
        if not isinstance(itinerary, dict):
            raise ValueError('itinerary格式不正确')
        action = itinerary.get('action')
        if action == 'replace':
            _shape(itinerary, {'action', 'text'}, {'action', 'text'})
            _positive(body, ['生成', '安排', '规划', '制定', '做一', '来一', '修改', '调整', '重排', '重新', '改成', '改为'], '行程生成')
            result_text = _text(itinerary['text'], 'itinerary.text', 100000)
            # 缺少公交线路证据时，不允许通过终止提交绕过只读回复的明细保护。
            missing_transit = any(
                isinstance(row, dict) and evidence_kind(row) == 'query_route'
                and isinstance(evidence_result(row), dict)
                and evidence_result(row).get('mode') == 'transit'
                and evidence_result(row).get('origin_poi_id') != evidence_result(row).get('destination_poi_id')
                and not evidence_result(row).get('transit_steps')
                for row in evidence)
            if missing_transit and re.search(r'\d+号线|\d+路(?:公交|巴士)?|上车|下车|换乘', result_text):
                raise ValueError('公交线路明细缺少查询依据，不能提交具体线路或上下车安排')
            if len(trip.requirements.text) > MAX_REQUIREMENTS_TEXT:
                raise ValueError('需求正文超过20,000个Unicode字符，请先整理或缩短正文再生成行程')
            _, question = planning_brief(trip, require_all=True)
            if question:
                raise ValueError(question)
        elif action == 'edit_text':
            result_text = exact_edit(plan.text, itinerary, body, _get(execution_context, 'display_target'))
        elif action == 'confirm':
            _shape(itinerary, {'action'}, {'action'})
            _positive(body, ['确认', '就按这个', '就按这版'], '确认行程')
            if not (re.fullmatch(r'\s*(?:请)?(?:确认|确认了|就按这个|就按这版)(?:吧|。|！|!)?\s*', body)
                    or re.search(r'确认.{0,8}(?:行程|计划|方案|攻略)|(?:行程|计划|方案|攻略).{0,8}确认|就按这(?:个|版)', body)):
                raise ValueError('确认必须明确指向当前行程，不能把查询确认当作行程确认')
            if not plan.has_content:
                raise ValueError('没有当前已交付的行程可确认')
        elif action == 'clear':
            _shape(itinerary, {'action'}, {'action'})
            # Clearing is intentionally a narrow standalone full-command grammar.
            if not re.fullmatch(r'\s*(?:请|帮我|请帮我)?\s*(?:清空|删除|删掉|移除)\s*(?:当前|现有|这份|全部|所有|已有)?\s*(?:的)?\s*(?:行程|行程计划|计划|攻略)\s*(?:吧|。|！|!)?\s*', body):
                raise ValueError('清空行程需要独立、完整、肯定的当前指令')
        else:
            raise ValueError('未知行程操作')
        if action in {'edit_text', 'confirm', 'clear'} and 'requirements_text' in args:
            raise ValueError('精确编辑、确认或清空必须独立提交')
    payload = {'args': args,
               'result_text': result_text, 'evidence': deepcopy(serialize(evidence)),
               'display_target': deepcopy(serialize(_get(execution_context, 'display_target'))),
               'request': serialize(request_context)}
    payload['digest'] = canonical_digest(payload)
    return payload


def apply_changes(stage, context, payload):
    """Revalidate the same request against current private stage, then apply all effects.

    Caller must discard ``stage`` on any failure and persist before publishing it.
    Returns deterministic (effect_lines, changed_kinds).
    """
    payload = serialize(payload)
    unsigned = {key: value for key, value in payload.items() if key != 'digest'}
    if payload.get('digest') != canonical_digest(unsigned):
        raise ValueError('CRUD候选摘要不一致')
    trip_id = context.trip_id
    task = stage.tasks.get(context.execution_task_id, {})
    target = None
    if task.get('payload', {}).get('display_target') is not None:
        from app.workflow.tasks import resolve_display_target
        raw_payload = task.get('payload', {})
        supplied = raw_payload.get('display_target')
        target = (resolve_display_target(stage.plans[trip_id], raw_payload)
                  if not isinstance(supplied, dict) or set(supplied) == {'day_index', 'item_index'}
                  else supplied)
        if isinstance(supplied, dict) and set(supplied) != {'day_index', 'item_index'}:
            target = supplied
    execution = SimpleNamespace(trip=stage.trips[trip_id], plan=stage.plans[trip_id],
                                display_target=target)
    verified = prepare_changes(payload['args'], context, execution, payload['evidence'])
    if verified != payload:
        raise ValueError('CRUD候选与当前原话、快照或页面目标不一致')
    args = payload['args']
    lines, kinds = [], []
    if 'requirements_text' in args:
        previous = stage.trips[trip_id]
        updated = update_requirements(previous, {'text': args['requirements_text']})
        stage.trips[trip_id] = updated
        lines.append('需求正文已保存。' if updated.requirements.revision != previous.requirements.revision else '需求正文未变化。')
        if updated.requirements.revision != previous.requirements.revision:
            kinds.append('requirements')
    itinerary = args.get('itinerary', {})
    action = itinerary.get('action')
    if action in {'replace', 'edit_text'}:
        evidence = deepcopy(stage.plans[trip_id].evidence) + payload['evidence']
        stage._apply_text_plan(trip_id, payload['result_text'], evidence,
                               display_status='pending', preserve_plan_basis=action == 'edit_text')
        lines.append('行程已更新。')
        kinds.append('itinerary')
    elif action == 'confirm':
        stage.plans[trip_id].status = stage.trips[trip_id].status = '已确认'
        lines.append('当前行程已确认。')
        kinds.append('confirmation')
    elif action == 'clear':
        stage.plans[trip_id] = TripPlan(id='empty', title='还没有行程计划', status='未生成', days=[])
        stage.trips[trip_id].status = '未生成'
        stage._discard_display_jobs(trip_id)
        lines.append('当前行程已清空，需求正文和聊天记录已保留。')
        kinds.append('clear')
    return lines, kinds
