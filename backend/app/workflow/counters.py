"""Current request fences, independent of archived conversation orchestration."""

def normalize_workflow(raw, **_):
    raw = raw if isinstance(raw, dict) else {}
    return {key: raw[key] if type(raw.get(key)) is int and raw[key] >= 0 else 0
            for key in ('revision', 'sequence', 'epoch')}


def advance_request(raw):
    state = normalize_workflow(raw)
    state['sequence'] += 1
    state['revision'] += 1
    return state


def bump_epoch(raw):
    state = advance_request(raw)
    state['epoch'] += 1
    return state


def clear_workflow(raw, **_):
    return bump_epoch(raw)
