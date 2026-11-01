from __future__ import annotations
import json
from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime
from threading import RLock
from contextlib import contextmanager
from uuid import uuid4
from app.domain.models import AgentAction, AgentCard, IdeaCard, Message, Trip, TripPlan, User
from app.domain.requirements import initial_requirements, update_requirements
from app.storage.state import next_id, IDEA_WALL_LIMIT, IDEA_ROTATIONS

def _normalize_idea_text(value):
    from app.storage.state import _persist as _fn__persist
    return ''.join(value.lower().split())

def delete_idea(self, trip_id: str, idea_id: str) -> bool:
    from app.storage.state import _persist as _fn__persist
    cards = self.idea_cards[trip_id]
    remaining = [card for card in cards if card.id != idea_id]
    if len(remaining) == len(cards):
        return False
    self.idea_cards[trip_id] = remaining
    self.trips[trip_id].last_activity = '已删除一条旅行想法'
    _fn__persist(self)
    return True

def add_idea(self, trip_id: str, body: str) -> IdeaCard:
    from app.storage.state import _persist as _fn__persist
    rotation = IDEA_ROTATIONS[len(self.idea_cards[trip_id]) % len(IDEA_ROTATIONS)]
    card = IdeaCard(id=f'idea-{next_id(self)}', kind='待归类', title=body.strip(), body=body.strip(), author='你', status='待整理', rotation=rotation)
    self.idea_cards[trip_id].append(card)
    self.trips[trip_id].last_activity = '已添加新的旅行想法'
    _fn__persist(self)
    return card

def _add_agent_idea_cards(self, trip_id: str, raw_cards: list[dict], sender: User | None=None) -> None:
    from app.storage.state import _persist as _fn__persist
    if not raw_cards:
        return
    source = '对话' if sender else 'Agent'
    for raw_card in raw_cards:
        kind = str(raw_card.get('kind') or '待归类')
        title = str(raw_card.get('title') or '新的旅行想法').strip()
        body = str(raw_card.get('body') or title).strip()
        status = str(raw_card.get('status') or '已整理').strip()
        if not title or not body:
            continue
        existing_index = _find_similar_idea_index(self, trip_id, kind, title, body)
        if existing_index is not None:
            previous = self.idea_cards[trip_id][existing_index]
            self.idea_cards[trip_id][existing_index] = IdeaCard(id=previous.id, kind=kind, title=title, body=body, author=previous.author if previous.author == '你' else source, status=status, rotation=previous.rotation)
            continue
        rotation = IDEA_ROTATIONS[len(self.idea_cards[trip_id]) % len(IDEA_ROTATIONS)]
        self.idea_cards[trip_id].append(IdeaCard(id=f'idea-{next_id(self)}', kind=kind, title=title, body=body, author=source, status=status, rotation=rotation))

def _find_similar_idea_index(self, trip_id: str, kind: str, title: str, body: str) -> int | None:
    from app.storage.state import _persist as _fn__persist
    normalized_title = _normalize_idea_text(title)
    normalized_body = _normalize_idea_text(body)
    for index, card in enumerate(self.idea_cards[trip_id]):
        if _normalize_idea_text(card.title) == normalized_title:
            return index
        if _normalize_idea_text(card.body) == normalized_body:
            return index
        if card.kind == kind == '预算':
            return index
    return None
