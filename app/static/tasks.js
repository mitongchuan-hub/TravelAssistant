/* Recoverable tasks with honest route states and version-bound actions. */
(() => {
  const workspace = document.querySelector('[data-travel-tasks-url]');
  if (!workspace) return;
  const endpoint = workspace.dataset.travelTasksUrl;
  const storageKey = `travelassistant:task-submit:${endpoint}`;
  const status = workspace.querySelector('[data-route-task-status]');
  const TASK_POLL_ACTIVE_MS = 1000;
  const TASK_POLL_IDLE_MS = 2500;
  let active = null, polling = false, pollTimer = null, submitting = false, rejection = false;
  const busyTask = task => task && ['queued', 'running'].includes(task.status);
  function schedulePoll() {
    clearTimeout(pollTimer);
    if (!document.hidden) pollTimer = setTimeout(poll, busyTask(active) || pending() ? TASK_POLL_ACTIVE_MS : TASK_POLL_IDLE_MS);
  }
  function pending() {
    try { return JSON.parse(sessionStorage.getItem(storageKey) || 'null'); } catch { return null; }
  }
  function remember(value) {
    try {
      if (value) sessionStorage.setItem(storageKey, JSON.stringify(value));
      else sessionStorage.removeItem(storageKey);
    } catch { /* Server tasks remain queryable. */ }
  }
  function routeNotice(heading, message, {retry = false, refresh = false, chat = false} = {}) {
    if (!status) return;
    status.hidden = false;
    status.querySelector('[data-route-task-heading]').textContent = heading;
    status.querySelector('[data-route-task-message]').textContent = message;
    status.querySelector('[data-route-task-retry]').hidden = !retry;
    status.querySelector('[data-route-task-refresh]').hidden = !refresh;
    status.querySelector('[data-route-task-chat]').hidden = !chat;
  }
  function formNotice(form, message) {
    let feedback = form?.querySelector('[data-revision-feedback], [data-task-feedback]');
    if (!feedback && form) {
      feedback = document.createElement('p');
      feedback.dataset.taskFeedback = 'true';
      feedback.setAttribute('role', 'status');
      form.append(feedback);
    }
    if (feedback) { feedback.textContent = message; feedback.hidden = !message; }
  }
  function isCurrent(task) {
    const payload = task?.payload || {};
    return (!payload.expected_plan_id || payload.expected_plan_id === workspace.dataset.planId)
      && (!payload.source_hash || payload.source_hash === workspace.dataset.sourceHash)
      && (payload.expected_requirements_revision === undefined
        || String(payload.expected_requirements_revision) === workspace.dataset.requirementsRevision);
  }
  function show(task) {
    const changed = !active || active.id !== task.id || active.status !== task.status || active.updated_at !== task.updated_at;
    active = task;
    rejection = false;
    schedulePoll();
    if (changed) document.dispatchEvent(new CustomEvent('travel-task-update', {detail: task}));
    const busy = busyTask(task);
    workspace.querySelectorAll('form').forEach(form => {
      if (!/\/(plan|revision)$/.test(form.action)) return;
      form.querySelectorAll('button[type=submit]').forEach(button => {
        button.disabled = busy || workspace.dataset.routeOutdated === 'true';
      });
    });
    workspace.querySelectorAll('[data-route-quick], [data-display-edit]').forEach(button => {
      button.disabled = busy || workspace.dataset.routeOutdated === 'true';
    });
    if (busy) {
      routeNotice(task.kind === 'plan' ? '正在安排你的路线' : task.kind === 'revision' ? '正在调整路线，原方案仍可查看' : '助手正在处理你的消息',
        task.kind === 'chat' ? '可以到聊天页查看回复，已有路线不会在失败时丢失。' : '正在结合已保存的设置和可用资料处理。可以离开页面，回来后继续查看；无需重复提交。', {chat: task.kind === 'chat'});
    } else if (task.status === 'failed') {
      const current = isCurrent(task) && workspace.dataset.routeOutdated !== 'true';
      routeNotice('这次没有完成，已有内容已保留', task.message || '请稍后重试。',
        {retry: current, refresh: !current, chat: task.kind === 'chat'});
    } else if (task.needs_input) {
      routeNotice('还需要你确认一件事', task.message, {chat: true});
    } else if (task.status === 'succeeded' && status) status.hidden = true;
    workspace.querySelectorAll('[data-revision-feedback]').forEach(feedback => {
      if (task.kind !== 'revision') return;
      feedback.hidden = !busy && task.status !== 'failed';
      feedback.textContent = busy ? '正在调整，可以关闭面板。修改意见已保留。'
        : task.status === 'failed' ? `${task.message} 修改意见已保留。` : '';
    });
  }
  function failure(message, form, refresh = false) {
    rejection = true;
    routeNotice('暂时无法提交', message, {refresh});
    formNotice(form, message + ' 输入已保留。');
  }
  function resetForm(form) {
    if (!form) return;
    form.removeAttribute('aria-busy');
    form.dataset.submitting = 'false';
    form.querySelectorAll('button[type=submit]').forEach(button => {
      button.disabled = busyTask(active) || workspace.dataset.routeOutdated === 'true';
    });
  }
  function navigate(task) {
    if (task.status !== 'succeeded' || task.kind === 'chat' || !task.result_url) return;
    const url = new URL(task.result_url, location.origin);
    if (url.origin !== location.origin) return;
    url.searchParams.set('task', task.id);
    location.assign(url.href);
  }
  async function postRequest(request, form) {
    const url = request.retry_of ? `${endpoint}/${encodeURIComponent(request.retry_of)}/retry` : endpoint;
    const body = request.retry_of ? {request_id: request.request_id} : request;
    const response = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body), signal: AbortSignal.timeout(15000)});
    const data = await response.json();
    if (!response.ok) {
      remember(null);
      failure(data.error || (response.status === 401 ? '登录已失效，请重新登录。' : '提交未成功，请稍后重试。'), form, response.status === 409);
      if (request.kind === 'chat') document.dispatchEvent(new CustomEvent('travel-chat-rejected', {detail: request}));
      return;
    }
    remember(null);
    formNotice(form, '');
    show(data.task);
    const address = new URL(location.href);
    address.searchParams.set('task', data.task.id);
    history.replaceState(null, '', address);
    navigate(data.task);
  }
  async function submit(kind, payload, form, retryOf = '') {
    if (submitting || busyTask(active)) return;
    const old = pending();
    const request = old && old.kind === kind && old.retry_of === (retryOf || undefined)
      && JSON.stringify(old.payload) === JSON.stringify(payload) ? old
      : {kind, payload, request_id: createTravelRequestId(), ...(retryOf ? {retry_of: retryOf} : {})};
    if (old && old !== request) {
      failure('上一条请求正在确认是否送达，请稍候。', form);
      return;
    }
    submitting = true;
    remember(request);
    form?.setAttribute('aria-busy', 'true');
    if (form) form.dataset.submitting = 'true';
    form?.querySelectorAll('button[type=submit]').forEach(button => { button.disabled = true; });
    if (status) routeNotice('正在提交，请稍候', '输入已保留，正在确认请求。无需重复点击。');
    else formNotice(form, '正在提交，请稍候。');
    try { await postRequest(request, form); }
    catch {
      routeNotice('正在确认请求是否送达', '连接中断，输入已保留。正在使用同一请求标识确认，不会重复生成。');
      formNotice(form, '正在确认是否送达，输入已保留。');
    } finally { submitting = false; resetForm(form); schedulePoll(); }
  }
  function binding(data) {
    if (!data.has('expected_plan_id')) return {};
    return {expected_plan_id: String(data.get('expected_plan_id')), source_hash: String(data.get('source_hash') || ''),
      expected_requirements_revision: Number(data.get('expected_requirements_revision'))};
  }
  document.addEventListener('submit', event => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    const data = new FormData(form);
    let kind, payload;
    if (form.action.endsWith('/plan')) { kind = 'plan'; payload = binding(data); }
    else if (form.action.endsWith('/revision')) {
      kind = 'revision';
      payload = {feedback: String(data.get('feedback') || '').trim(), ...binding(data)};
      if (!payload.feedback) return;
      if (data.get('day_index') !== null && data.get('day_index') !== '') {
        payload.display_target = {day_index: Number(data.get('day_index')), item_index: Number(data.get('item_index'))};
      } else payload.item_id = data.get('item_id') || null;
    } else return;
    event.preventDefault();
    if (form.dataset.submitting === 'true') return;
    if (workspace.dataset.routeOutdated === 'true') { failure('路线或设置已经变化，请先查看最新路线再提交。', form, true); return; }
    submit(kind, payload, form);
  }, true);
  status?.querySelector('[data-route-task-retry]').addEventListener('click', () => {
    if (active?.status === 'failed' && isCurrent(active)) submit(active.kind, active.payload, null, active.id);
  });
  document.addEventListener('travel-chat-accepted', event => { if (event.detail?.kind === 'chat') show(event.detail); });
  document.addEventListener('travel-route-outdated', () => {
    workspace.dataset.routeOutdated = 'true';
    workspace.querySelectorAll('form[action$="/plan"] button[type=submit], form[action$="/revision"] button[type=submit], [data-route-quick], [data-display-edit]').forEach(button => { button.disabled = true; });
    routeNotice('路线或设置已在其他页面更新', '当前页面仍保留你看到的内容和输入。请查看最新路线后再调整。', {refresh: true});
  });
  async function poll() {
    if (polling || submitting || document.hidden) return;
    polling = true;
    try {
      const response = await fetch(endpoint, {cache: 'no-store', signal: AbortSignal.timeout(10000)});
      if (!response.ok) return;
      const {tasks} = await response.json();
      const uncertain = pending();
      const recovered = uncertain && tasks.find(task => task.request_id === uncertain.request_id);
      if (recovered) remember(null);
      if (uncertain && !recovered) { await postRequest(uncertain, null); return; }
      const wanted = new URL(location.href).searchParams.get('task');
      const task = recovered || tasks.find(task => busyTask(task))
        || tasks.find(task => task.id === wanted) || tasks[tasks.length - 1];
      if (!task || (!wanted && !active && !recovered && task.status === 'succeeded')) return;
      if (task.kind === 'chat' && !busyTask(task) && !wanted && !recovered) return;
      if (rejection && !busyTask(task) && !recovered) return;
      const finishedNow = active?.id === task.id && busyTask(active) && task.status === 'succeeded';
      show(task);
      const completedFromThisPage = wanted === task.id && task.status === 'succeeded'
        && task.kind !== 'chat' && task.plan_id === workspace.dataset.planId;
      if (finishedNow || completedFromThisPage || (recovered && task.status === 'succeeded')) navigate(task);
    } catch { /* The saved route and drafts remain available; a later poll recovers. */ }
    finally { polling = false; schedulePoll(); }
  }
  poll();
  document.addEventListener('visibilitychange', () => { clearTimeout(pollTimer); if (!document.hidden) poll(); });
})();
