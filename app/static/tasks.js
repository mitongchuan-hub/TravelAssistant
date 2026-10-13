/* Recoverable task execution without a floating status panel. */
(() => {
  const workspace = document.querySelector('[data-travel-tasks-url]');
  if (!workspace) return;
  const endpoint = workspace.dataset.travelTasksUrl;
  const storageKey = `travelassistant:task-submit:${endpoint}`;
  const TASK_POLL_ACTIVE_MS = 1000;
  const TASK_POLL_IDLE_MS = 2500;
  let active = null, polling = false, pollTimer = null;
  function schedulePoll() {
    clearTimeout(pollTimer);
    if (!document.hidden) pollTimer = window.setTimeout(poll,
      active && ['queued', 'running'].includes(active.status) ? TASK_POLL_ACTIVE_MS : TASK_POLL_IDLE_MS);
  }
  function show(task) {
    active = task;
    schedulePoll();
    document.dispatchEvent(new CustomEvent('travel-task-update', {detail: task}));
    const busy = ['queued', 'running'].includes(task.status);
    workspace.querySelectorAll('form').forEach(form => {
      if (!/\/(plan|revision)$/.test(form.action)) return;
      form.querySelectorAll('button[type=submit]').forEach(button => { button.disabled = busy; });
    });
    const feedback = workspace.querySelector('[data-revision-feedback]');
    if (feedback && task.kind === 'revision') {
      feedback.hidden = task.status !== 'failed';
      feedback.textContent = task.status === 'failed' ? task.message + ' 修改意见已保留。' : '';
    }
  }
  document.addEventListener('travel-chat-accepted', event => {
    if (event.detail?.kind === 'chat') show(event.detail);
  });
  function pending() {
    try { return JSON.parse(sessionStorage.getItem(storageKey) || 'null'); } catch { return null; }
  }
  function remember(value) {
    try { if (value) sessionStorage.setItem(storageKey, JSON.stringify(value)); else sessionStorage.removeItem(storageKey); } catch { /* Server tasks remain queryable. */ }
  }
  function resetForm(form) {
    form.removeAttribute('aria-busy');
    form.dataset.submitting = 'false';
    form.querySelectorAll('button[type=submit]').forEach(button => { button.disabled = false; });
  }
  async function submit(kind, payload, form) {
    const old = pending();
    const request = old && old.kind === kind && JSON.stringify(old.payload) === JSON.stringify(payload)
      ? old : {kind, payload, request_id: createTravelRequestId()};
    if (old && old !== request) { resetForm(form); return; }
    remember(request);
    form.setAttribute('aria-busy', 'true');
    form.dataset.submitting = 'true';
    form.querySelectorAll('button[type=submit]').forEach(button => { button.disabled = true; });
    try {
      const response = await fetch(endpoint, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(request), signal: AbortSignal.timeout(15000)});
      const data = await response.json();
      if (!response.ok) { remember(null); return; }
      remember(null);
      show(data.task);
      const url = new URL(location.href);
      url.searchParams.set('task', data.task.id);
      history.replaceState(null, '', url);
    } catch { /* Keep uncertain requests for recovery by the next poll. */ }
    finally {
      resetForm(form);
      if (active && ['queued', 'running'].includes(active.status)) {
        form.querySelectorAll('button[type=submit]').forEach(button => { button.disabled = true; });
      }
    }
  }
  document.addEventListener('submit', event => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    let kind, payload;
    if (form.action.endsWith('/plan')) { kind = 'plan'; payload = {}; }
    else if (form.action.endsWith('/revision')) {
      kind = 'revision';
      const data = new FormData(form);
      payload = {item_id: data.get('item_id') || null, feedback: String(data.get('feedback') || '').trim()};
      if (!payload.feedback) return;
    } else return;
    event.preventDefault();
    if (form.dataset.submitting === 'true') return;
    submit(kind, payload, form);
  }, true);
  async function poll() {
    if (polling || document.hidden) return;
    polling = true;
    try {
      const response = await fetch(endpoint, {cache: 'no-store', signal: AbortSignal.timeout(10000)});
      if (!response.ok) return;
      const data = await response.json();
      const uncertain = pending();
      const tasks = data.tasks;
      const recovered = uncertain && tasks.find(task => task.request_id === uncertain.request_id);
      if (recovered) remember(null);
      if (uncertain && !recovered) {
        const response = await fetch(endpoint, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(uncertain), signal: AbortSignal.timeout(15000)});
        const restored = await response.json();
        if (response.ok) { remember(null); show(restored.task); }
        else {
          remember(null);
          if (uncertain.kind === 'chat') document.dispatchEvent(new CustomEvent('travel-chat-rejected', {detail: uncertain}));
        }
        return;
      }
      const wanted = new URL(location.href).searchParams.get('task');
      const task = recovered || tasks.find(task => ['queued', 'running'].includes(task.status))
        || tasks.find(task => task.id === wanted) || tasks[tasks.length - 1];
      if (!task) return;
      if (!wanted && !active && !recovered && task.status === 'succeeded') return;
      const finishedNow = active?.id === task.id && ['queued', 'running'].includes(active.status) && task.status === 'succeeded';
      show(task);
      if (finishedNow && task.kind !== 'chat') {
        if (task.kind === 'revision' && task.payload.item_id) {
          const key = `travelassistant:revision-drafts:/workspace/${task.trip_id}`;
          try {
            const drafts = JSON.parse(sessionStorage.getItem(key) || '{}');
            delete drafts[task.payload.item_id];
            sessionStorage.setItem(key, JSON.stringify(drafts));
          } catch { /* Saved task remains available. */ }
        }
        const url = new URL(task.result_url, location.origin);
        url.searchParams.set('task', task.id);
        location.assign(url.href);
      }
    } catch { /* Next poll can recover. */ }
    finally { polling = false; schedulePoll(); }
  }
  poll();
  document.addEventListener('visibilitychange', () => {
    clearTimeout(pollTimer);
    if (!document.hidden) poll();
  });
})();
