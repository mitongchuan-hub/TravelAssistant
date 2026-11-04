from __future__ import annotations
import json
from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime
from threading import RLock
from contextlib import contextmanager
from uuid import uuid4
from app.domain.models import AgentAction, AgentCard, Message, Trip, TripPlan, User
from app.domain.requirements import update_requirements
from app.storage.state import next_id
'Independent, single-worker display jobs bound to the current saved prose.'
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import logging
import sys
from threading import Condition, Event, Thread
from time import monotonic
from uuid import uuid4
from app.core.config import boolean
from app.services.itinerary.plan_display import text_hash, dates_from_snapshot
from app.services.itinerary.guide_html import validate_guide_display
from app.application.execution import ACTIVE, now
from app.domain.itinerary.freshness import plan_freshness
DISPLAY_ACTIVE = {'queued', 'running', 'waiting_publish'}
logger = logging.getLogger(__name__)

def start_display_worker(self, *, force=False):
    from app.application.guide import _fail_display_queue as _fn__fail_display_queue
    from app.application.guide import display_enabled as _fn_display_enabled
    from app.application.guide import recover_display_jobs as _fn_recover_display_jobs
    from app.application.guide import run_pending_display_jobs as _fn_run_pending_display_jobs
    with self._lock:
        if force:
            self.display_worker_enabled = True
        if not self.display_worker_enabled or not _fn_display_enabled(self):
            return
        self._display_stop.clear()
        if self._display_worker is None or not self._display_worker.is_alive():
            self._display_worker = Thread(target=self._display_worker_loop, name=f'travel-display-{id(self)}', daemon=True)
            self._display_worker.start()
        self._display_wakeup.set()

def _display_worker_loop(self):
    from app.application.guide import _fail_display_queue as _fn__fail_display_queue
    from app.application.guide import display_enabled as _fn_display_enabled
    from app.application.guide import recover_display_jobs as _fn_recover_display_jobs
    from app.application.guide import run_pending_display_jobs as _fn_run_pending_display_jobs
    while not self._display_stop.is_set():
        self._display_wakeup.wait()
        self._display_wakeup.clear()
        if self._display_stop.is_set():
            break
        try:
            _fn_run_pending_display_jobs(self)
        except Exception as exc:
            logger.warning('Display worker failed (%s)', type(exc).__name__)
            _fn__fail_display_queue(self)

def stop_display_worker(self):
    from app.application.guide import _fail_display_queue as _fn__fail_display_queue
    from app.application.guide import display_enabled as _fn_display_enabled
    from app.application.guide import recover_display_jobs as _fn_recover_display_jobs
    from app.application.guide import run_pending_display_jobs as _fn_run_pending_display_jobs
    self._display_stop.set()
    self._display_wakeup.set()
    with self._lock:
        _fn_recover_display_jobs(self)
        worker = self._display_worker
    if worker is not None and worker.is_alive():
        worker.join(timeout=1)
