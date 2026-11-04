"""Address-only city outing creation and short-lived, owner-bound POI choices."""
from copy import deepcopy
from datetime import date, timedelta
from threading import Lock
from time import monotonic
from uuid import uuid4

from app.domain.models import Trip, User
from app.domain.requirements import initialize_requirements

CITY_FORM_FIELDS = {'lodging_address', 'start_date', 'day_count', 'revision', 'transport_mode'}


def city_requirements(values, city=''):
    if values.keys() - CITY_FORM_FIELDS:
        raise ValueError('游玩信息字段不正确')
    if not all(isinstance(v, str) for v in values.values()):
        raise ValueError('请填写文字格式的游玩信息')
    address = values.get('lodging_address', '').strip()
    if not address or len(address) > 300:
        raise ValueError('请填写住宿地址或酒店名称，不超过300字')
    transport_mode = values.get('transport_mode', '').strip()
    if transport_mode not in {'driving', 'transit'}:
        raise ValueError('请选择自驾或公交地铁')
    count = values.get('day_count', '').strip()
    if not count.isascii() or not count.isdigit() or not 1 <= int(count) <= 365:
        raise ValueError('游玩天数请填写1到365之间的整数')
    count = int(count)
    start = values.get('start_date', '').strip()
    if not start:
        raise ValueError('请填写出行开始日期')
    end = ''
    if start:
        try:
            end = (date.fromisoformat(start) + timedelta(days=count-1)).isoformat()
        except (ValueError, OverflowError):
            raise ValueError('请填写有效的游玩日期') from None
    changes = {'city': city, 'lodging_address': address, 'start_date': start, 'end_date': end,
               'day_count': count, 'transport_mode': transport_mode}
    probe = Trip('validation', '同城游玩', User('', '', ''), '', '', mode='city')
    initialize_requirements(probe, changes)
    return changes


class LodgingChoices:
    """Coordinates and initial form values stay server-side, never trust hidden fields."""
    def __init__(self, ttl=900, maximum=128):
        self.ttl, self.maximum = ttl, maximum
        self._items = {}
        self._lock = Lock()

    def add(self, owner_id, values, candidates, trip_id=''):
        token = uuid4().hex
        with self._lock:
            now = monotonic()
            self._items = {k: v for k, v in self._items.items() if v['expires'] > now}
            while len(self._items) >= self.maximum:
                self._items.pop(next(iter(self._items)))
            self._items[token] = {'owner_id': owner_id, 'values': deepcopy(values),
                'candidates': deepcopy(candidates), 'trip_id': trip_id, 'expires': now+self.ttl}
        return token

    def get(self, token, owner_id, trip_id=''):
        with self._lock:
            draft = self._items.get(token)
            if (not draft or draft['expires'] <= monotonic() or draft['owner_id'] != owner_id
                    or draft['trip_id'] != trip_id):
                raise ValueError('位置选择已过期，请重新输入住宿地址并查找')
            return deepcopy(draft)

    def discard(self, token):
        with self._lock:
            self._items.pop(token, None)
