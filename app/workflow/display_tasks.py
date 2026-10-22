"""Independent, single-worker display jobs bound to the current saved prose."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import logging
import os
import sys
from threading import Condition, Event, Thread
from time import monotonic
from uuid import uuid4

from app.services.plan_display import text_hash, validate_display, dates_from_snapshot
from app.workflow.tasks import ACTIVE, now
from app.views.route_view import plan_basis

DISPLAY_ACTIVE = {'queued', 'running', 'waiting_publish'}
logger = logging.getLogger(__name__)


class DisplayTasks:
    def _init_display_tasks(self, worker_enabled=None):
        self.display_jobs = {}
        self._foreground_operations = {}
        self._display_changed = Condition(self._lock)
        self._display_wakeup = Event()
        self._display_stop = Event()
        self._display_worker = None
        self._display_executing = {}
        self.display_worker_enabled = ('pytest' not in sys.modules) if worker_enabled is None else worker_enabled

    @staticmethod
    def display_enabled():
        return os.getenv('TRAVEL_DISPLAY_ENABLED', '1').strip().lower() not in {'0', 'false', 'no', 'off'}

    def _foreground_active(self, trip_id=None):
        for task in self.tasks.values():
            if task['status'] in ACTIVE and (trip_id is None or task['trip_id'] == trip_id):
                return True
        for tid, operations in self._foreground_operations.items():
            if trip_id is not None and tid != trip_id:
                continue
            if any(task_id is None or self.tasks.get(task_id, {}).get('status') in ACTIVE
                   for task_id in operations.values()):
                return True
        return False

    @contextmanager
    def _foreground_operation(self, trip_id):
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
            self._notify_display_worker()

    def _job_matches_plan(self, job):
        plan = self.plans.get(job['trip_id'])
        return bool(job['trip_id'] in self.trips and plan and plan.id == job['plan_id']
                    and text_hash(plan.text) == job['source_hash']
                    and plan.requirements_snapshot == job['requirements_snapshot'])

    def _discard_display_jobs(self, trip_id):
        # Keep only the latest plan, including its derivative. In-flight callbacks
        # will find their job missing and cannot resurrect removed data.
        with self._lock:
            self.display_jobs = {jid: job for jid, job in self.display_jobs.items() if job['trip_id'] != trip_id}
            self._display_changed.notify_all()

    def _queue_display(self, trip_id):
        plan = self.plans[trip_id]
        self._discard_display_jobs(trip_id)
        source_hash = text_hash(plan.text)
        if not self.display_enabled() or self._display_stop.is_set():
            self.plans[trip_id] = replace(plan, display=None, display_source_hash=source_hash, display_status='fallback')
            return None
        jid = uuid4().hex
        job = {'id': jid, 'trip_id': trip_id, 'plan_id': plan.id, 'source_hash': source_hash,
               'requirements_snapshot': deepcopy(plan.requirements_snapshot), 'status': 'queued',
               'created_at': now(), 'updated_at': now(), 'started_at': None, 'finished_at': None,
               'seconds': None, 'error_type': None, 'reason': None}
        self.display_jobs[jid] = job
        self._display_changed.notify_all()
        return deepcopy(job)

    def get_display_state(self, trip_id):
        with self._lock:
            plan = self.plans[trip_id]
            source_hash = text_hash(plan.text)
            job = next((j for j in self.display_jobs.values() if j['trip_id'] == trip_id
                        and j['plan_id'] == plan.id and j['source_hash'] == source_hash), None)
            trip = self.trips[trip_id]
            basis = plan_basis(trip, plan)
            version = ':'.join([plan.id, source_hash, plan.display_status,
                                job['id'] if job else '', job['updated_at'] if job else '',
                                str(trip.requirements.revision), basis['basis_status']])
            return {'plan_id': plan.id, 'source_hash': source_hash, 'display_status': plan.display_status,
                    'version': version, 'enabled': self.display_enabled(), 'has_text': bool(plan.text),
                    'requirements_revision': trip.requirements.revision, **basis,
                    'busy': self._foreground_active(trip_id)}

    def retry_display(self, trip_id, plan_id, source_hash):
        with self._lock:
            plan = self.plans[trip_id]
            if plan.id != plan_id or text_hash(plan.text) != source_hash:
                raise ValueError('行程已更新，请刷新后再整理卡片')
            if not self.display_enabled() or not plan.text:
                raise ValueError('当前无法整理卡片，完整文字仍可阅读')
            existing = next((j for j in self.display_jobs.values() if j['trip_id'] == trip_id
                             and j['plan_id'] == plan.id and j['source_hash'] == source_hash), None)
            if existing and existing['status'] in DISPLAY_ACTIVE:
                return deepcopy(existing)
            if self._foreground_active(trip_id):
                raise ValueError('当前请求正在处理，请稍后整理卡片')
            self.plans[trip_id] = replace(plan, display=None, display_source_hash=source_hash, display_status='pending')
            job = self._queue_display(trip_id)
            self._persist()
        self._notify_display_worker()
        return job

    def cancel_display_jobs(self, trip_id, reason='卡片整理已取消，完整文字已保留'):
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
                self._persist()

    def recover_display_jobs(self):
        with self._lock:
            changed = False
            for job in self.display_jobs.values():
                if job['status'] in DISPLAY_ACTIVE:
                    job.update(status='interrupted', updated_at=now(), reason='服务重启使卡片整理中断，完整文字已保留')
                    job.pop('result', None)
                    changed = True
            for tid, plan in self.plans.items():
                if plan.display_status == 'pending' and not self._foreground_active(tid):
                    self.plans[tid] = replace(plan, display_status='fallback')
                    changed = True
            if changed:
                self._persist()
            self._display_changed.notify_all()

    def _display_job_current(self, job_id):
        with self._lock:
            job = self.display_jobs.get(job_id)
            return bool(not self._display_stop.is_set() and self.display_enabled()
                        and job and job['status'] == 'running' and self._job_matches_plan(job))

    def _publish_display(self, job):
        if not self._job_matches_plan(job):
            job.update(status='superseded', updated_at=now())
            job.pop('result', None)
            return True
        if self._foreground_active(job['trip_id']):
            return False
        plan = self.plans[job['trip_id']]
        result = job.pop('result')
        display = result.get('display')
        status = result.get('display_status', 'fallback')
        try:
            if not self.display_enabled():
                raise ValueError('卡片整理已关闭')
            if result.get('display_source_hash') != job['source_hash']:
                raise ValueError('展示与当前原文不一致')
            if status == 'ready':
                display = validate_display(display, dates_from_snapshot(job['requirements_snapshot']))
            else:
                display = None
        except Exception as exc:
            display, status = None, 'fallback'
            result.update(error_type=type(exc).__name__, reason=str(exc)[:300])
        # Replace only the derivative; preserve the current confirmation state,
        # prose, evidence, requirements basis and plan identity.
        self.plans[job['trip_id']] = replace(plan, display=deepcopy(display),
            display_source_hash=job['source_hash'], display_status=status)
        job.update(status='succeeded' if status == 'ready' else 'failed', updated_at=now(),
                   error_type=result.get('error_type'), reason=result.get('reason'))
        return True

    def _next_display_job(self):
        with self._lock:
            changed = False
            for job in self.display_jobs.values():
                if job['status'] == 'waiting_publish':
                    changed = self._publish_display(job) or changed
            if changed:
                self._persist()
                self._display_changed.notify_all()
            if self._foreground_active() or self._display_stop.is_set():
                return None
            for job in self.display_jobs.values():
                if job['status'] != 'queued':
                    continue
                if not self._job_matches_plan(job) or not self.display_enabled():
                    job.update(status='superseded', updated_at=now())
                    plan = self.plans.get(job['trip_id'])
                    if plan and plan.id == job['plan_id'] and plan.display_status == 'pending':
                        self.plans[job['trip_id']] = replace(plan, display_status='fallback')
                    self._persist()
                    self._display_changed.notify_all()
                    continue
                job.update(status='running', started_at=now(), updated_at=now())
                self._persist()
                self._display_changed.notify_all()
                self._display_executing[job['id']] = job['trip_id']
                return job['id']
        return None

    def run_display_job(self, job_id):
        from app.agents import agent_client
        with self._lock:
            job = self.display_jobs.get(job_id)
            if not job or job['status'] != 'running' or not self._job_matches_plan(job):
                return
            if self._foreground_active():
                job.update(status='queued', updated_at=now())
                self._persist()
                return
            source = self.plans[job['trip_id']].text
            basis = deepcopy(job['requirements_snapshot'])
            trip_id = job['trip_id']
        def retry_allowed():
            with self._lock:
                return self._display_job_current(job_id) and not self._foreground_active()

        started = monotonic()
        try:
            dates = dates_from_snapshot(basis)
            result = agent_client.generate_plan_display(source, dates, trip_id=trip_id,
                cancelled=lambda: not self._display_job_current(job_id), retry_allowed=retry_allowed)
        except Exception as exc:
            result = {'display': None, 'display_status': 'fallback', 'display_source_hash': text_hash(source),
                      'error_type': type(exc).__name__, 'reason': '卡片整理未完成，完整文字已保留'}
        with self._lock:
            job = self.display_jobs.get(job_id)
            if not job or job['status'] != 'running':
                return
            job.update(seconds=monotonic()-started, finished_at=now(), updated_at=now(),
                       status='waiting_publish', result=result)
            self._publish_display(job)
            self._persist()
            self._display_changed.notify_all()

    def run_pending_display_jobs(self):
        """Deterministic drain for isolated tests/runners; production uses the worker."""
        count = 0
        while (job_id := self._next_display_job()) is not None:
            try:
                self.run_display_job(job_id)
                count += 1
            finally:
                with self._display_changed:
                    self._display_executing.pop(job_id, None)
                    self._display_changed.notify_all()
        return count

    def start_display_worker(self, *, force=False):
        with self._lock:
            if force:
                self.display_worker_enabled = True
            if not self.display_worker_enabled or not self.display_enabled():
                return
            self._display_stop.clear()
            if self._display_worker is None or not self._display_worker.is_alive():
                self._display_worker = Thread(target=self._display_worker_loop,
                    name=f'travel-display-{id(self)}', daemon=True)
                self._display_worker.start()
            self._display_wakeup.set()

    def _fail_display_queue(self):
        with self._lock:
            for job in self.display_jobs.values():
                if job['status'] in DISPLAY_ACTIVE:
                    job.update(status='waiting_publish', updated_at=now(), result={
                        'display': None, 'display_status': 'fallback', 'display_source_hash': job['source_hash'],
                        'error_type': 'DisplayQueueError', 'reason': '卡片整理暂不可用，完整文字已保留'})
                    self._publish_display(job)
            self._display_changed.notify_all()
        try:
            self._persist()
        except Exception:
            logger.warning('Could not persist display queue failure')

    def _notify_display_worker(self):
        try:
            if self._display_stop.is_set():
                self.recover_display_jobs()
            elif self.display_worker_enabled:
                self.start_display_worker()
                self._display_wakeup.set()
        except Exception as exc:
            logger.warning('Display queue unavailable (%s)', type(exc).__name__)
            try:
                self._fail_display_queue()
            except Exception:
                logger.warning('Could not record display queue failure')

    def _display_worker_loop(self):
        while not self._display_stop.is_set():
            self._display_wakeup.wait()
            self._display_wakeup.clear()
            if self._display_stop.is_set():
                break
            try:
                self.run_pending_display_jobs()
            except Exception as exc:
                logger.warning('Display worker failed (%s)', type(exc).__name__)
                self._fail_display_queue()

    def wait_for_display_jobs(self, trip_id=None, timeout=185):
        deadline = monotonic()+timeout
        with self._display_changed:
            while any(job['status'] in DISPLAY_ACTIVE and (trip_id is None or job['trip_id'] == trip_id)
                      for job in self.display_jobs.values()) or any(trip_id is None or tid == trip_id for tid in self._display_executing.values()):
                remaining = deadline-monotonic()
                if remaining <= 0:
                    return False
                self._display_changed.wait(remaining)
        return True

    def stop_display_worker(self):
        self._display_stop.set()
        self._display_wakeup.set()
        with self._lock:
            self.recover_display_jobs()
            worker = self._display_worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=1)
