"""执行数据装配：复制显式业务对象，不读取 Store、不获取锁或解析 HTTP 目标。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class TaskExecutionContext:
    """Web 层传入的独立可信快照；不授予模型存储权限。"""
    trip: Any
    messages: list[Any] | tuple[Any, ...]
    idea_cards: list[Any] | tuple[Any, ...]
    plan: Any
    metrics: Mapping[str, Any]
    current_date: str
    entry: str = 'main'
    scope: str = ''
    target_item_id: str | None = None
    display_target: Mapping[str, Any] | None = None

    def __post_init__(self):
        if self.entry not in {'main', 'confirm'}:
            raise ValueError('未知任务执行入口')
        if self.scope not in {'', 'item', 'all'}:
            raise ValueError('任务执行范围无效')
        if self.display_target is not None and self.entry != 'main':
            raise ValueError('展示目标只能用于聊天入口')


def build_execution_context(*, trip: Any, plan: Any, messages: list[Any] | tuple[Any, ...],
                            idea_cards: list[Any] | tuple[Any, ...], metrics: Mapping[str, Any],
                            current_date: str, entry: str, payload: Mapping[str, Any],
                            display_target: Mapping[str, Any] | None = None) -> TaskExecutionContext:
    """Detach a turn's data at the caller's lock-bound consistent read point.

    The caller resolves display_target against the same plan before assembly;
    payload only supplies legacy item metadata, not model instructions or authority.
    """
    snapshot = deepcopy({
        'trip': trip, 'plan': plan,
        'messages': [row for row in messages if row.status == 'sent'],
        'idea_cards': idea_cards, 'metrics': metrics,
        'target_item_id': payload.get('item_id'), 'display_target': display_target,
    })
    return TaskExecutionContext(**snapshot, current_date=current_date, entry=entry,
        scope='item' if snapshot['target_item_id'] or snapshot['display_target'] else '')
