"""Pure validation and staged application of one direct-agent CRUD transaction.

A user quote binds the request, not its intent. Destructive and textual operations
also require positive, current instructions; uncertain commands fail closed.
"""
from copy import deepcopy
from dataclasses import asdict, replace
from hashlib import sha256
import json
import re
from types import SimpleNamespace
from uuid import uuid4

from app.domain.itinerary.identity import plan_basis
from app.workflow.execution import serialize
from app.models import IdeaCard, TripPlan
from app.services.maps.evidence import evidence_kind, evidence_result, place_messages
from app.services.itinerary.planning import planning_brief
from app.services.requirements import chat_changes, update_requirements

_QUOTES = str.maketrans({'“': '"', '”': '"', '‘': "'", '’': "'", '＂': '"', '＇': "'"})
_OPERATIONS = {'requirements_patch', 'lodging', 'ideas', 'itinerary'}


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


def _idea_hash(card):
    return canonical_digest(asdict(card) if not isinstance(card, dict) else card)


def prepare_changes(args, request_context, execution_context, evidence=()):
    """Validate a detached snapshot and return its canonical change package.

    ``evidence`` is supplied only by the server's query executor, never tool args.
    No store, network, clock, ID allocation or business mutation occurs here.
    """
    args = deepcopy(serialize(args))
    _shape(args, {'user_quote', *_OPERATIONS, 'next_question'}, {'user_quote'})
    if args['user_quote'] != request_context.body:
        raise ValueError('user_quote必须逐字引用完整本轮用户消息')
    if not any(args.get(key) for key in _OPERATIONS):
        raise ValueError('submit_changes至少需要一项实际操作')
    if 'next_question' in args:
        _text(args['next_question'], 'next_question', 2000)
    trip, plan = _get(execution_context, 'trip'), _get(execution_context, 'plan')
    cards = list(_get(execution_context, 'idea_cards', ()) or ())
    if (trip.id != request_context.trip_id or trip.owner.id != request_context.owner_id
            or trip.requirements.revision != request_context.requirements_revision
            or plan_basis(plan) != request_context.plan_basis):
        raise ValueError('CRUD快照与本轮业务依据不一致')
    body = request_context.body
    display_target = _get(execution_context, 'display_target')
    if display_target is not None:
        from app.workflow.tasks import validate_display_target_metadata
        validate_display_target_metadata(plan, serialize(display_target))
    changes = {}
    if args.get('requirements_patch'):
        patch = args['requirements_patch']
        if not isinstance(patch, dict) or not patch:
            raise ValueError('requirements_patch必须是稀疏的实际字段')
        if re.search(r'如果|假如|假设|要是|(?:不要|别|暂不|不必|无需)(?:修改|更新|保存|改动|更改)', body):
            raise ValueError('假设或否定请求不能保存正式需求')
        changes = chat_changes({'changes': patch, 'evidence': body}, body, trip)
        # 全句引用只证明来源；每个变化字段仍须有本轮对应内容或明确字段指令。
        field_hints = {
            'travelers': r'人数|人均以外人数|(?:共|一共|我们)?\s*\d+\s*(?:个人|人|位)',
            'day_count': r'天数|\d+\s*天|[一二两三四五六七八九十]+天',
            'start_date': r'日期|出发|开始|\d{4}[-年/]\d|\d+月\d+日',
            'end_date': r'日期|结束|返回|天数|\d+\s*天|[一二两三四五六七八九十]+天|\d{4}[-年/]\d|\d+月\d+日',
            'budget_amount': r'预算|元|费用|花费', 'budget_scope': r'预算|人均|每人|总额|总共',
            'budget_coverage': r'预算|费用|包括|包含|不含',
            'transport_mode': r'交通|自驾|开车|公交|地铁|公共交通',
            'outing_start_time': r'出门|出发|开始|几点|\d{1,2}[:：]\d{2}',
            'outing_end_time': r'返回|回去|结束|几点|\d{1,2}[:：]\d{2}',
            'pace': r'节奏|轻松|紧凑|慢|赶|休闲', 'style': r'风格|节奏|轻松|紧凑|慢|休闲',
        }
        for key, value in changes.items():
            previous = getattr(trip if key in {'destination', 'date_range', 'style', 'note'} else trip.requirements, key)
            if value == previous:
                continue
            if key in {'must_visit', 'avoid', 'dietary'}:
                mentioned = [item for action in ('add', 'remove') for item in patch[key].get(action, [])]
                if any(item not in body for item in mentioned):
                    raise ValueError('需求列表变更必须对应本轮明确提供的内容')
            elif key in field_hints:
                if not re.search(field_hints[key], body):
                    raise ValueError('本轮没有明确涉及需求字段：' + key)
            elif not isinstance(value, str) or not value or value not in body:
                raise ValueError('需求变更必须对应本轮明确提供的内容：' + key)
        trip = update_requirements(trip, changes)
    lodging = None
    if args.get('lodging'):
        from app.services.places import collect_places
        _shape(args['lodging'], {'poi_id'}, {'poi_id'})
        catalog = collect_places(place_messages(evidence))
        lodging = catalog.get(args['lodging']['poi_id'])
        if not lodging or not lodging.get('city'):
            raise ValueError('住宿必须引用本轮地图查询返回的真实地点')
        if (not any(value and value in body for value in (lodging.get('name'), lodging.get('address')))
                or re.search(r'如果|假如|假设|不要|不是|别', body)):
            raise ValueError('请在本轮明确说出要使用的住宿名称或地址；重名时先询问具体分店')
        if (sum(place.get('name') == lodging.get('name') for place in catalog.values()) > 1
                and (not lodging.get('address') or lodging['address'] not in body
                     or sum(place.get('address') == lodging['address'] for place in catalog.values()) > 1)):
            raise ValueError('住宿存在同名候选，请在聊天中说明具体地址后再确认')
        if not re.search(r'(?:我|我们|这次|目前|现在)(?:已经|已)?住|住在|入住|住宿\s*[：:]|住宿(?:是|在|改为|选)|(?:作为|用作|设为|选为).{0,3}住宿', body):
            raise ValueError('请选择该地点作为住宿，不能把普通地点查询当作住宿授权')
        trip = deepcopy(trip)
        trip.requirements.city = lodging['city']
        trip.requirements.lodging_address = lodging.get('address') or lodging['name']
        trip.lodging = deepcopy(lodging)
    existing = {_get(card, 'id'): card for card in cards}
    expected = {}
    operations = args.get('ideas', [])
    if not isinstance(operations, list) or len(operations) > 20:
        raise ValueError('ideas必须为不超过20项的操作列表')
    targets = set()
    for operation in operations:
        if not isinstance(operation, dict):
            raise ValueError('想法操作格式不正确')
        action = operation.get('action')
        if action == 'create':
            _shape(operation, {'action', 'kind', 'title', 'body'}, {'action', 'kind', 'title', 'body'})
            values = operation
            if re.search(r'如果|假如|假设|(?:不要|别|无需|不必)(?:添加|保存|记下|记录|创建)', body):
                raise ValueError('假设或否定请求不能创建想法')
            if not any(isinstance(operation.get(key), str) and operation[key].strip()
                       and operation[key].strip() in body for key in ('title', 'body')):
                raise ValueError('新增想法必须能对应本轮用户实际提供的内容')
        elif action in {'update', 'delete'}:
            _shape(operation, {'action', 'idea_id', 'changes'} if action == 'update' else {'action', 'idea_id'},
                   {'action', 'idea_id', 'changes'} if action == 'update' else {'action', 'idea_id'})
            idea_id = operation['idea_id']
            if not isinstance(idea_id, str) or idea_id not in existing or idea_id in targets:
                raise ValueError('想法必须引用当前快照中的唯一精确ID')
            targets.add(idea_id)
            expected[idea_id] = _idea_hash(existing[idea_id])
            _positive(body, ['修改', '更新', '改成', '改为'] if action == 'update'
                      else ['删除', '删掉', '移除', '去掉'], '想法变更')
            card = existing[idea_id]
            labels = [_get(card, 'title', ''), _get(card, 'body', ''), idea_id]
            # 动作和目标必须处于同一肯定分句，不能把“删除午餐，但保留龙井”拼成删除龙井。
            clauses = re.split(r'[，,。；;！!？?\n]|但是|但|不过', body)
            verbs = r'修改|更新|改成|改为' if action == 'update' else r'删除|删掉|移除|去掉'
            matched = any(re.search(verbs, clause)
                          and not re.search(r'保留|留下|不要|不删|别|无需|不必', clause)
                          and any(label and label in clause for label in labels)
                          for clause in clauses)
            if not matched:
                sole_target = len(existing) == 1 and any(
                    re.search(verbs, clause)
                    and not re.search(r'保留|留下|不要|不删|别|无需|不必', clause)
                    and re.search(r'(?:这条|这个|该条|唯一的|这张).{0,4}(?:想法|卡片)', clause)
                    for clause in clauses)
                if not sole_target:
                    raise ValueError('想法变更必须在同一肯定指令中明确指定该卡片')
            values = operation.get('changes', {})
            if action == 'update':
                _shape(values, {'kind', 'title', 'body'})
                if not values:
                    raise ValueError('想法更新不能为空')
        else:
            raise ValueError('未知想法操作')
        for key in ('kind', 'title', 'body'):
            if key in values:
                _text(values[key], 'idea.' + key, 2000 if key == 'body' else 300)
                if key == 'kind' and values[key] not in {'地点', '预算', '节奏', '禁忌', '待归类'}:
                    raise ValueError('想法分类无效')
    itinerary = args.get('itinerary')
    result_text = None
    if itinerary:
        if not isinstance(itinerary, dict):
            raise ValueError('itinerary格式不正确')
        action = itinerary.get('action')
        if action == 'replace':
            _shape(itinerary, {'action', 'text', 'idea_outcomes'}, {'action', 'text', 'idea_outcomes'})
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
            _, question = planning_brief(trip, require_all=True)
            if question:
                raise ValueError(question)
            outcomes = itinerary['idea_outcomes']
            if not isinstance(outcomes, list):
                raise ValueError('idea_outcomes必须逐项覆盖想法')
            refs = []
            for outcome in outcomes:
                _shape(outcome, {'idea_ref', 'status', 'reason'}, {'idea_ref', 'status', 'reason'})
                refs.append(outcome['idea_ref'])
                reason = _text(outcome['reason'], 'idea_outcomes.reason', 2000)
                if outcome['status'] not in {'scheduled', 'conflict', 'needs_input', 'excluded'}:
                    raise ValueError('想法处理状态无效')
                if outcome['status'] == 'scheduled' and reason not in result_text:
                    raise ValueError('已安排的依据必须逐字引用新行程正文')
            retained = set(existing) - {op['idea_id'] for op in operations if op['action'] == 'delete'}
            if len(refs) != len(set(refs)) or set(refs) != retained:
                raise ValueError('idea_outcomes必须完整覆盖提交后保留的所有既有想法ID，不得遗漏或伪造')
            for op in operations:
                if op['action'] == 'create' and op['body'] not in result_text:
                    raise ValueError('同轮新建想法正文必须在新行程中逐字覆盖；否则请分轮提交')
                if op['action'] == 'update' and op['changes'].get('body'):
                    outcome = next(row for row in outcomes if row['idea_ref'] == op['idea_id'])
                    if outcome['status'] == 'scheduled' and op['changes']['body'] not in result_text:
                        raise ValueError('同轮修改想法正文必须在已安排的新行程中逐字覆盖')
            expected = {key: _idea_hash(value) for key, value in existing.items()}
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
        if action in {'edit_text', 'confirm', 'clear'} and any(args.get(key) for key in {'requirements_patch', 'lodging', 'ideas'}):
            raise ValueError('精确编辑、确认或清空必须独立提交')
    payload = {'args': args, 'normalized_changes': changes, 'expected_idea_hashes': expected,
               'idea_ids': sorted(existing) if itinerary and itinerary['action'] == 'replace' else None,
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
    cards = stage.idea_cards[trip_id]
    current = {card.id: card for card in cards}
    if payload.get('idea_ids') is not None and sorted(current) != payload['idea_ids']:
        raise ValueError('想法墙在生成期间已变化，请重新审阅全部想法')
    for key, digest in payload['expected_idea_hashes'].items():
        if key not in current or _idea_hash(current[key]) != digest:
            raise ValueError('目标想法在处理期间已变化，请刷新后重试')
    task = stage.tasks.get(context.execution_task_id, {})
    target = None
    if task.get('kind') == 'revision':
        from app.workflow.tasks import resolve_display_target
        raw_payload = task.get('payload', {})
        supplied = raw_payload.get('display_target')
        target = (resolve_display_target(stage.plans[trip_id], raw_payload)
                  if not isinstance(supplied, dict) or set(supplied) == {'day_index', 'item_index'}
                  else supplied)
        if isinstance(supplied, dict) and set(supplied) != {'day_index', 'item_index'}:
            target = supplied
    execution = SimpleNamespace(trip=stage.trips[trip_id], plan=stage.plans[trip_id],
                                idea_cards=cards, display_target=target)
    verified = prepare_changes(payload['args'], context, execution, payload['evidence'])
    if verified != payload:
        raise ValueError('CRUD候选与当前原话、快照或页面目标不一致')
    args = payload['args']
    lines, kinds = [], []
    if payload['normalized_changes']:
        stage.trips[trip_id] = update_requirements(stage.trips[trip_id], payload['normalized_changes'])
        lines.append('需求已保存。')
        kinds.append('requirements')
    if args.get('lodging'):
        from app.services.places import collect_places
        catalog = collect_places(place_messages(payload['evidence']))
        lodging = catalog[args['lodging']['poi_id']]
        trip = stage.trips[trip_id]
        stage.trips[trip_id] = update_requirements(trip, {
            'city': lodging['city'], 'lodging_address': lodging.get('address') or lodging['name']})
        stage.trips[trip_id].lodging = deepcopy(lodging)
        lines.append('住宿位置已匹配并保存。')
        kinds.append('lodging')
    for operation in args.get('ideas', []):
        action = operation['action']
        if action == 'create':
            cards.append(IdeaCard(id='idea-' + uuid4().hex, kind=operation['kind'],
                                  title=operation['title'], body=operation['body'], author='对话', status='已整理'))
        elif action == 'update':
            index = next(i for i, card in enumerate(cards) if card.id == operation['idea_id'])
            cards[index] = replace(cards[index], **operation['changes'])
        else:
            cards[:] = [card for card in cards if card.id != operation['idea_id']]
    if args.get('ideas'):
        lines.append('旅行想法已更新。')
        kinds.append('ideas')
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
        lines.append('当前行程已清空，旅行需求、想法和聊天记录已保留。')
        kinds.append('clear')
    return lines, kinds
