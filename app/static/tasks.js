/* Recoverable task state, outside the idea wall's original layout. */
(() => {
  const workspace = document.querySelector('[data-travel-tasks-url]');
  if (!workspace) return;
  const endpoint = workspace.dataset.travelTasksUrl;
  const panel = workspace.querySelector('[data-task-status]');
  const message = panel.querySelector('[data-task-message]');
  const retry = panel.querySelector('[data-task-retry]');
  const result = panel.querySelector('[data-task-result]');
  const storageKey = `travelassistant:task-submit:${endpoint}`;
  let active = null, polling = false, retryRequest = null;
  const id = () => createTravelRequestId();
  function show(task) {
    active = task;
    panel.hidden = false;
    message.textContent = task.message;
    const busy = ['queued', 'running'].includes(task.status);
    workspace.querySelectorAll('form').forEach(form => {
      if (!/\/(plan|revision|messages)$/.test(form.action)) return;
      form.querySelectorAll('button[type=submit]').forEach(button => { button.disabled = busy; });
    });
    const sheetFeedback = workspace.querySelector('[data-revision-feedback]');
    if (sheetFeedback && task.kind === 'revision') {
      sheetFeedback.hidden = false;
      sheetFeedback.textContent = task.message + (task.status === 'failed' ? ' 修改意见已保留。' : '');
    }
    retry.hidden = task.status !== 'failed';
    result.hidden = task.status !== 'succeeded' || !task.result_url;
    if (task.result_url) result.href = task.result_url;
  }
  function pending() {
    try { return JSON.parse(sessionStorage.getItem(storageKey) || 'null'); } catch { return null; }
  }
  function remember(value) {
    try { if (value) sessionStorage.setItem(storageKey, JSON.stringify(value)); else sessionStorage.removeItem(storageKey); } catch { /* Server-side task remains queryable. */ }
  }
  function resetForm(form) {
    form.removeAttribute('aria-busy');
    form.dataset.submitting = 'false';
    form.querySelectorAll('button[type=submit]').forEach(button => { button.disabled = false; });
  }
  async function submit(kind, payload, form) {
    const old = pending();
    const request = old && old.kind === kind && JSON.stringify(old.payload) === JSON.stringify(payload)
      ? old : {kind, payload, request_id: id()};
    if (old && old !== request) {
      panel.hidden = false;
      message.textContent = '上一条请求的结果尚未确认，请先刷新查看任务。';
      resetForm(form);
      return;
    }
    remember(request);
    form.setAttribute('aria-busy', 'true');
    form.dataset.submitting = 'true';
    form.querySelectorAll('button[type=submit]').forEach(button => { button.disabled = true; });
    panel.hidden = false;
    message.textContent = '正在提交…';
    try {
      const response = await fetch(endpoint, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(request), signal: AbortSignal.timeout(15000)});
      const data = await response.json();
      if (!response.ok) {
        remember(null);
        throw new Error(data.error || '提交未完成');
      }
      remember(null);
      show(data.task);
      const url = new URL(window.location.href);
      url.searchParams.set('task', data.task.id);
      window.history.replaceState(null, '', url);
    } catch (error) {
      message.textContent = pending() ? '未能确认提交结果。请求已保留，刷新后会查询或按同一标识重发。' : error.message;
    } finally { resetForm(form); if (active) show(active); }
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
  retry.addEventListener('click', async () => {
    if (!active || active.status !== 'failed' || retry.disabled) return;
    retry.disabled = true;
    const original = active;
    retryRequest ||= {task_id: original.id, request_id: id()};
    panel.hidden = false;
    try {
      const response = await fetch(`${endpoint}/${original.id}/retry`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({request_id: retryRequest.request_id}), signal: AbortSignal.timeout(15000)});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '重试未完成');
      retryRequest = null;
      show(data.task);
      const url = new URL(location.href);
      url.searchParams.set('task', data.task.id);
      history.replaceState(null, '', url);
    } catch (error) { message.textContent = error.message + '。请先刷新查看最新状态。'; }
    finally { retry.disabled = false; }
  });
  async function poll() {
    if (polling || document.hidden) return;
    polling = true;
    try {
      const response = await fetch(endpoint, {cache: 'no-store', signal: AbortSignal.timeout(10000)});
      if (!response.ok) {
        if (response.status === 401) { panel.hidden = false; message.textContent = '登录已失效，请重新登录后查看任务。'; }
        return;
      }
      const data = await response.json();
      const uncertain = pending();
      const tasks = data.tasks;
      const recovered = uncertain && tasks.find(task => task.request_id === uncertain.request_id);
      if (recovered) remember(null);
      if (uncertain && !recovered) {
        // Same ID and same payload: safe even if the server accepted the lost request.
        const response = await fetch(endpoint, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(uncertain), signal: AbortSignal.timeout(15000)});
        const restored = await response.json();
        if (response.ok) { remember(null); show(restored.task); }
        else { remember(null); panel.hidden = false; message.textContent = restored.error || '请求未完成'; }
        return;
      }
      const wanted = new URL(location.href).searchParams.get('task');
      const task = tasks.find(task => task.status === 'queued' || task.status === 'running')
        || tasks.find(task => task.id === wanted) || tasks[tasks.length - 1];
      if (!task) return;
      if (!wanted && !active && task.status === 'succeeded') return;
      const finishedNow = active?.id === task.id && ['queued', 'running'].includes(active.status) && task.status === 'succeeded';
      show(task);
      if (finishedNow && task.kind !== 'chat') {
        if (task.kind === 'revision' && task.payload.item_id) {
          const key = `travelassistant:revision-drafts:/workspace/${task.trip_id}`;
          try {
            const drafts = JSON.parse(sessionStorage.getItem(key) || '{}');
            delete drafts[task.payload.item_id];
            sessionStorage.setItem(key, JSON.stringify(drafts));
          } catch { /* Saved task is still available. */ }
        }
        const url = new URL(task.result_url, location.origin);
        url.searchParams.set('task', task.id);
        location.assign(url.href);
      }
    } catch { /* Keep known status; next poll can recover. */ }
    finally { polling = false; }
  }
  poll();
  window.setInterval(poll, 2500);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
})();
