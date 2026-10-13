"""Monotonic spans with per-lane accounting for foreground/background overlap."""
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock, current_thread, get_ident
from time import perf_counter


def interval_union(intervals):
    result = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start, end))
    return sum(end-start for start, end in result)


class Timeline:
    def __init__(self, *, clock=perf_counter, on_start=None):
        self.clock, self.origin = clock, clock()
        self.spans, self.events = [], []
        self.stack = ContextVar('latency_span_stack', default=())
        self.role = ContextVar('latency_model_role', default=None)
        self.on_start = on_start
        self.owner_thread = get_ident()
        self.lock = Lock()

    def offset(self):
        return self.clock()-self.origin

    @contextmanager
    def span(self, name, category, *, lane=None, **metadata):
        stack = self.stack.get()
        with self.lock:
            parent = self.spans[stack[-1]-1] if stack else None
            selected_lane = lane or (parent['lane'] if parent else
                'foreground' if get_ident()==self.owner_thread else
                'display' if current_thread().name.startswith('travel-display-') else 'thread-'+str(get_ident()))
            if parent and parent['lane'] != selected_lane:
                parent, stack = None, ()
            row = {'id': len(self.spans)+1, 'parent_id': parent['id'] if parent else None, 'lane': selected_lane,
                   'name': name, 'category': category, 'start_seconds': self.offset(), 'metadata': metadata,
                   'status': 'completed'}
            self.spans.append(row)
        token = self.stack.set(stack+(row['id'],))
        try:
            if self.on_start:
                self.on_start(row)
            yield row
        except BaseException as exc:
            row.update(status='error', error_type=type(exc).__name__, error=str(exc)[:500])
            raise
        finally:
            row['end_seconds'] = self.offset()
            row['seconds'] = row['end_seconds']-row['start_seconds']
            self.stack.reset(token)

    def event(self, name, **value):
        with self.lock:
            self.events.append({'name': name, 'offset_seconds': self.offset(), **value})

    def finalize(self):
        children = defaultdict(list)
        for span in self.spans:
            if 'end_seconds' not in span:
                raise ValueError('Unclosed span: '+span['name'])
            children[span['parent_id']].append(span)
        categories, lane_categories = defaultdict(float), defaultdict(lambda: defaultdict(float))
        for span in self.spans:
            covered = interval_union([(max(span['start_seconds'], c['start_seconds']), min(span['end_seconds'], c['end_seconds']))
                                      for c in children[span['id']]])
            span['self_seconds'] = max(0.0, span['seconds']-covered)
            categories[span['category']] += span['self_seconds']
            lane_categories[span['lane']][span['category']] += span['self_seconds']
        roots = children[None]
        lane_roots = defaultdict(list)
        for root in roots:
            lane_roots[root['lane']].append(root)
        lane_seconds = {lane: interval_union([(s['start_seconds'], s['end_seconds']) for s in values])
                        for lane, values in lane_roots.items()}
        errors = {lane: sum(lane_categories[lane].values())-duration for lane, duration in lane_seconds.items()}
        union = interval_union([(s['start_seconds'], s['end_seconds']) for s in roots])
        shift = min((s['start_seconds'] for s in roots), default=0.0)
        end = max((s['end_seconds'] for s in roots), default=shift)
        overlap = sum(lane_seconds.values())-union
        if shift:
            for span in self.spans:
                span['start_seconds'] -= shift
                span['end_seconds'] -= shift
            for event in self.events:
                event['offset_seconds'] -= shift
        return {'spans': self.spans, 'events': self.events,
                'exclusive_seconds_by_category': dict(categories),
                'exclusive_seconds_by_lane': {lane: dict(values) for lane, values in lane_categories.items()},
                'lane_seconds': lane_seconds, 'measured_seconds': end-shift, 'covered_seconds': union,
                'concurrent': overlap>1e-6, 'overlap_seconds': max(0,overlap),
                'accounting_error_seconds': sum(errors.values()), 'lane_accounting_errors': errors,
                'accounting_valid': all(abs(error)<1e-6 for error in errors.values()),
                'note': 'Exclusive times add within a lane. Overlapping lanes and observer wait do not add to user latency.'}

    def chrome_trace(self):
        lanes = {lane: index+1 for index, lane in enumerate(dict.fromkeys(s['lane'] for s in self.spans))}
        metadata = [{'name':'thread_name','ph':'M','pid':1,'tid':tid,'args':{'name':lane}} for lane,tid in lanes.items()]
        return {'traceEvents': metadata+[{'name':s['name'],'cat':s['category'],'ph':'X',
            'ts':s['start_seconds']*1e6,'dur':s['seconds']*1e6,'pid':1,'tid':lanes[s['lane']],
            'args':{**s['metadata'],'status':s['status'],'self_seconds':s['self_seconds']}} for s in self.spans],
            'displayTimeUnit':'ms'}
