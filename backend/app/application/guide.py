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


# Compatibility export for tests and callers still importing the former
# workflow mixin. The implementation remains preserved in workflow/.
from app.workflow.display_tasks import DisplayTasks

def display_enabled(self):
    from app.jobs.guide_worker import start_display_worker as _fn_start_display_worker
    from app.storage.state import _persist as _fn__persist
    return boolean('TRAVEL_DISPLAY_ENABLED')

def _foreground_active(self, trip_id=None):
    from app.jobs.guide_worker import start_display_worker as _fn_start_display_worker
    from app.storage.state import _persist as _fn__persist
    for task in self.tasks.values():
        if task['status'] in ACTIVE and (trip_id is None or task['trip_id'] == trip_id):
            return True
    for tid, operations in self._foreground_operations.items():
        if trip_id is not None and tid != trip_id:
            continue
        if any((task_id is None or self.tasks.get(task_id, {}).get('status') in ACTIVE for task_id in operations.values())):
            return True
    return False

@contextmanager
def _foreground_operation(self, trip_id):
    from app.jobs.guide_worker import start_display_worker as _fn_start_display_worker
    from app.storage.state import _persist as _fn__persist
    token = uuid4().hex
    with self._lock:
        self._foreground_operations.setdefault(trip_id, {})[token] = getattr(self._task_context, 'task_id', None)
    try:
        yield
    finally:
        with self._lock:
            operations = self._foreground_operations.get(trip_id, {})
            operations.pop(token, None)
            if not operations:
                self._foreground_operations.pop(trip_id, None)
        _notify_display_worker(self)

def _job_matches_plan(self, job):
    from app.jobs.guide_worker import start_display_worker as _fn_start_display_worker
    from app.storage.state import _persist as _fn__persist
    plan = self.plans.get(job['trip_id'])
    return bool(job['trip_id'] in self.trips and plan and (plan.id == job['plan_id']) and (text_hash(plan.text) == job['source_hash']) and (plan.display_style == job.get('style_instruction', '')) and (plan.requirements_snapshot == job['requirements_snapshot']))

def _discard_display_jobs(self, trip_id):
    from app.jobs.guide_worker import start_display_worker as _fn_start_display_worker
    from app.storage.state import _persist as _fn__persist
    with self._lock:
        self.display_jobs = {jid: job for jid, job in self.display_jobs.items() if job['trip_id'] != trip_id}
        self._display_changed.notify_all()

def _queue_display(self, trip_id):
    from app.jobs.guide_worker import start_display_worker as _fn_start_display_worker
    from app.storage.state import _persist as _fn__persist
    plan = self.plans[trip_id]
    _discard_display_jobs(self, trip_id)
    source_hash = text_hash(plan.text)
    if not display_enabled(self) or self._display_stop.is_set():
        self.plans[trip_id] = replace(plan, display=None, display_source_hash=source_hash, display_status='fallback')
        return None
    jid = uuid4().hex
    job = {'id': jid, 'trip_id': trip_id, 'plan_id': plan.id, 'source_hash': source_hash, 'style_instruction': plan.display_style, 'requirements_snapshot': deepcopy(plan.requirements_snapshot), 'status': 'queued', 'created_at': now(), 'updated_at': now(), 'started_at': None, 'finished_at': None, 'seconds': None, 'error_type': None, 'reason': None}
    self.display_jobs[jid] = job
    self._display_changed.notify_all()
    return deepcopy(job)

def get_display_state(self, trip_id):
    from app.jobs.guide_worker import start_display_worker as _fn_start_display_worker
    from app.storage.state import _persist as _fn__persist
    with self._lock:
        plan = self.plans[trip_id]
        source_hash = text_hash(plan.text)
        job = next((j for j in self.display_jobs.values() if j['trip_id'] == trip_id and j['plan_id'] == plan.id and (j['source_hash'] == source_hash)), None)
        trip = self.trips[trip_id]
        basis = plan_freshness(trip, plan)
        guide = plan.display if isinstance(plan.display, dict) and plan.display.get('schema_version') == '2.0' else None
        guide_available = bool(guide and isinstance(guide.get('html'), str) and guide['html'] and (plan.display_source_hash == source_hash))
        guide_version = text_hash(guide['html']) if guide_available else ''
        busy = _foreground_active(self, trip_id)
        version = ':'.join([plan.id, source_hash, plan.display_status, job['id'] if job else '', job['updated_at'] if job else '', str(trip.requirements.revision), basis['basis_status'], plan.status, str(busy), guide_version])
        return {'plan_id': plan.id, 'source_hash': source_hash, 'display_status': plan.display_status, 'version': version, 'enabled': display_enabled(self), 'has_text': bool(plan.text), 'requirements_revision': trip.requirements.revision, **basis, 'guide_available': guide_available, 'guide_version': guide_version, 'busy': busy}

def retry_display(self, trip_id, plan_id, source_hash, *, style_instruction=None, _committing=False):
    from app.jobs.guide_worker import start_display_worker as _fn_start_display_worker
    from app.storage.state import _persist as _fn__persist
    with self._lock:
        plan = self.plans[trip_id]
        if plan.id != plan_id or text_hash(plan.text) != source_hash:
            raise ValueError('行程已更新，请刷新后再生成攻略网页')
        if not display_enabled(self) or not plan.text:
            raise ValueError('当前无法生成攻略网页，完整文字仍可阅读')
        style = '轻松、愉悦'
        existing = next((j for j in self.display_jobs.values() if j['trip_id'] == trip_id and j['plan_id'] == plan.id and (j['source_hash'] == source_hash)), None)
        if existing and existing['status'] in DISPLAY_ACTIVE and (existing.get('style_instruction', '') == style):
            return deepcopy(existing)
        if _foreground_active(self, trip_id) and (not _committing):
            raise ValueError('当前请求正在处理，请稍后生成攻略网页')
        previous = plan.display if plan.display_source_hash == source_hash else None
        self.plans[trip_id] = replace(plan, display=previous, display_source_hash=source_hash, display_status='pending', display_style=style)
        job = _queue_display(self, trip_id)
        _fn__persist(self)
    if not _committing:
        _notify_display_worker(self)
    return job

def cancel_display_jobs(self, trip_id, reason='攻略网页生成已取消，完整文字已保留'):
    from app.jobs.guide_worker import start_display_worker as _fn_start_display_worker
    from app.storage.state import _persist as _fn__persist
    with self._lock:
        plan = self.plans.get(trip_id)
        changed = False
        for job in self.display_jobs.values():
            if job['trip_id'] == trip_id and job['status'] in DISPLAY_ACTIVE:
                job.update(status='superseded', updated_at=now(), reason=reason)
                job.pop('result', None)
                changed = True
        if plan and plan.display_status == 'pending':
            self.plans[trip_id] = replace(plan, display_status='fallback')
            changed = True
        self._display_changed.notify_all()
        if changed:
            _fn__persist(self)

def recover_display_jobs(self):
    from app.jobs.guide_worker import start_display_worker as _fn_start_display_worker
    from app.storage.state import _persist as _fn__persist
    with self._lock:
        changed = False
        for job in self.display_jobs.values():
            if job['status'] in DISPLAY_ACTIVE:
                job.update(status='interrupted', updated_at=now(), reason='服务重启使攻略网页生成中断，完整文字已保留')
                job.pop('result', None)
                changed = True
        for tid, plan in self.plans.items():
            if plan.display_status == 'pending' and (not _foreground_active(self, tid)):
                self.plans[tid] = replace(plan, display_status='fallback')
                changed = True
        if changed:
            _fn__persist(self)
        self._display_changed.notify_all()

def _display_job_current(self, job_id):
    from app.jobs.guide_worker import start_display_worker as _fn_start_display_worker
    from app.storage.state import _persist as _fn__persist
    with self._lock:
        job = self.display_jobs.get(job_id)
        return bool(not self._display_stop.is_set() and display_enabled(self) and job and (job['status'] == 'running') and _job_matches_plan(self, job))

def _publish_display(self, job):
    from app.jobs.guide_worker import start_display_worker as _fn_start_display_worker
    from app.storage.state import _persist as _fn__persist
    if not _job_matches_plan(self, job):
        job.update(status='superseded', updated_at=now())
        job.pop('result', None)
        return True
    if _foreground_active(self, job['trip_id']):
        return False
    plan = self.plans[job['trip_id']]
    result = job.pop('result')
    display = result.get('display')
    status = result.get('display_status', 'fallback')
    try:
        if not display_enabled(self):
            raise ValueError('攻略网页生成已关闭')
        if result.get('display_source_hash') != job['source_hash']:
            raise ValueError('展示与当前原文不一致')
        if status == 'ready':
            display = validate_guide_display(display, dates_from_snapshot(job['requirements_snapshot']))
        else:
            display = None
    except Exception as exc:
        display, status = (None, 'fallback')
        result.update(error_type=type(exc).__name__, reason=str(exc)[:300])
    if status != 'ready' and plan.display_source_hash == job['source_hash']:
        display = plan.display
    self.plans[job['trip_id']] = replace(plan, display=deepcopy(display), display_source_hash=job['source_hash'], display_status=status)
    job.update(status='succeeded' if status == 'ready' else 'failed', updated_at=now(), error_type=result.get('error_type'), reason=result.get('reason'))
    return True

def _next_display_job(self):
    from app.jobs.guide_worker import start_display_worker as _fn_start_display_worker
    from app.storage.state import _persist as _fn__persist
    with self._lock:
        changed = False
        for job in self.display_jobs.values():
            if job['status'] == 'waiting_publish':
                changed = _publish_display(self, job) or changed
        if changed:
            _fn__persist(self)
            self._display_changed.notify_all()
        if _foreground_active(self) or self._display_stop.is_set():
            return None
        for job in self.display_jobs.values():
            if job['status'] != 'queued':
                continue
            if not _job_matches_plan(self, job) or not display_enabled(self):
                job.update(status='superseded', updated_at=now())
                plan = self.plans.get(job['trip_id'])
                if plan and plan.id == job['plan_id'] and (plan.display_status == 'pending'):
                    self.plans[job['trip_id']] = replace(plan, display_status='fallback')
                _fn__persist(self)
                self._display_changed.notify_all()
                continue
            job.update(status='running', started_at=now(), updated_at=now())
            _fn__persist(self)
            self._display_changed.notify_all()
            self._display_executing[job['id']] = job['trip_id']
            return job['id']
    return None

def run_display_job(self, job_id):
    from app.jobs.guide_worker import start_display_worker as _fn_start_display_worker
    from app.storage.state import _persist as _fn__persist
    from app.agents import entrypoints
    with self._lock:
        job = self.display_jobs.get(job_id)
        if not job or job['status'] != 'running' or (not _job_matches_plan(self, job)):
            return
        if _foreground_active(self):
            job.update(status='queued', updated_at=now())
            _fn__persist(self)
            return
        source = self.plans[job['trip_id']].text
        basis = deepcopy(job['requirements_snapshot'])
        trip_id = job['trip_id']

    def retry_allowed():
        with self._lock:
            return _display_job_current(self, job_id) and (not _foreground_active(self))
    started = monotonic()
    try:
        dates = dates_from_snapshot(basis)
        result = entrypoints.generate_plan_display(source, dates, trip_id=trip_id, cancelled=lambda: not _display_job_current(self, job_id), retry_allowed=retry_allowed)
    except Exception as exc:
        result = {'display': None, 'display_status': 'fallback', 'display_source_hash': text_hash(source), 'error_type': type(exc).__name__, 'reason': '攻略网页未生成，完整文字已保留'}
    with self._lock:
        job = self.display_jobs.get(job_id)
        if not job or job['status'] != 'running':
            return
        job.update(seconds=monotonic() - started, finished_at=now(), updated_at=now(), status='waiting_publish', result=result)
        _publish_display(self, job)
        _fn__persist(self)
        self._display_changed.notify_all()

def run_pending_display_jobs(self):
    """Deterministic drain for isolated tests/runners; production uses the worker."""
    from app.jobs.guide_worker import start_display_worker as _fn_start_display_worker
    from app.storage.state import _persist as _fn__persist
    count = 0
    while (job_id := _next_display_job(self)) is not None:
        try:
            run_display_job(self, job_id)
            count += 1
        finally:
            with self._display_changed:
                self._display_executing.pop(job_id, None)
                self._display_changed.notify_all()
    return count

def _fail_display_queue(self):
    from app.jobs.guide_worker import start_display_worker as _fn_start_display_worker
    from app.storage.state import _persist as _fn__persist
    with self._lock:
        for job in self.display_jobs.values():
            if job['status'] in DISPLAY_ACTIVE:
                job.update(status='waiting_publish', updated_at=now(), result={'display': None, 'display_status': 'fallback', 'display_source_hash': job['source_hash'], 'error_type': 'DisplayQueueError', 'reason': '攻略网页生成暂不可用，完整文字已保留'})
                _publish_display(self, job)
        self._display_changed.notify_all()
    try:
        _fn__persist(self)
    except Exception:
        logger.warning('Could not persist display queue failure')

def _notify_display_worker(self):
    from app.jobs.guide_worker import start_display_worker as _fn_start_display_worker
    from app.storage.state import _persist as _fn__persist
    try:
        if self._display_stop.is_set():
            recover_display_jobs(self)
        elif self.display_worker_enabled:
            _fn_start_display_worker(self)
            self._display_wakeup.set()
    except Exception as exc:
        logger.warning('Display queue unavailable (%s)', type(exc).__name__)
        try:
            _fail_display_queue(self)
        except Exception:
            logger.warning('Could not record display queue failure')

def wait_for_display_jobs(self, trip_id=None, timeout=185):
    from app.jobs.guide_worker import start_display_worker as _fn_start_display_worker
    from app.storage.state import _persist as _fn__persist
    deadline = monotonic() + timeout
    with self._display_changed:
        while any((job['status'] in DISPLAY_ACTIVE and (trip_id is None or job['trip_id'] == trip_id) for job in self.display_jobs.values())) or any((trip_id is None or tid == trip_id for tid in self._display_executing.values())):
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            self._display_changed.wait(remaining)
    return True
