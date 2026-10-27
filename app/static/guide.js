/* Preview documents stay in an opaque sandbox; only app-owned fragments enter the DOM. */
(() => {
  const preview = document.querySelector('[data-guide-preview]');
  const controller = preview?.querySelector('[data-display-controller]');
  const workspace = document.querySelector('[data-travel-tasks-url]');
  if (!preview || !controller || !workspace) return;
  const chat = workspace.querySelector('.chat-screen');
  const opener = workspace.querySelector('[data-guide-open]');
  const closer = preview.querySelector('[data-guide-close]');
  const download = preview.querySelector('[data-guide-download]');
  let panel = controller.querySelector('[data-display-panel]');
  let version = panel.dataset.version, polling = false, retrying = false, stopped = false, timer;
  function show(open, focus = true) {
    preview.dataset.open = String(open);
    opener?.setAttribute('aria-expanded', String(open));
    chat.inert = open;
    if (open) {
      preview.setAttribute('role', 'dialog');
      preview.setAttribute('aria-modal', 'true');
      if (focus) closer.focus();
    } else {
      preview.removeAttribute('role');
      preview.removeAttribute('aria-modal');
      if (focus) opener?.focus();
    }
  }
  opener?.addEventListener('click', () => show(true));
  closer?.addEventListener('click', () => show(false));
  preview.addEventListener('keydown', event => {
    if (event.key === 'Escape') show(false);
    if (event.key !== 'Tab' || preview.dataset.open !== 'true') return;
    const elements = [...preview.querySelectorAll('button:not(:disabled), a[href], summary, iframe')]
      .filter(element => element.getClientRects().length);
    const first = elements[0], last = elements.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  });
  function syncDownload() {
    if (!download) return;
    const url = panel.dataset.downloadUrl;
    download.hidden = !url;
    if (url) download.setAttribute('href', url);
    else download.removeAttribute('href');
  }
  syncDownload();
  show(preview.dataset.initialOpen === 'true', false);
  function schedule() {
    clearTimeout(timer);
    if (!document.hidden && !stopped) timer = setTimeout(poll, panel.dataset.displayStatus === 'pending' ? 1500 : 3500);
  }
  async function update(state) {
    if (state.version === version) { syncRetryButtons(); return; }
    const url = new URL(controller.dataset.partialUrl, location.origin);
    url.searchParams.set('plan_id', state.plan_id);
    url.searchParams.set('source_hash', state.source_hash);
    const response = await fetch(url, {cache: 'no-store', signal: AbortSignal.timeout(10000)});
    if (!response.ok) return;
    const fragment = document.createElement('template');
    fragment.innerHTML = await response.text();
    const next = fragment.content.querySelector('[data-display-panel]');
    if (!next || next.dataset.planId !== state.plan_id || next.dataset.sourceHash !== state.source_hash) return;
    const sourceOpen = panel.querySelector('.guide-source')?.open;
    if (sourceOpen && next.querySelector('.guide-source')) next.querySelector('.guide-source').open = true;
    const focusedRetry = panel.contains(document.activeElement) && document.activeElement.hasAttribute('data-display-retry');
    const previousFrame = panel.querySelector('[data-guide-frame]');
    const nextFrame = next.querySelector('[data-guide-frame]');
    // Keep the live browsing context, its scroll and open details when only
    // app-owned controls change. Moving an iframe would reload its document.
    if (previousFrame && nextFrame && previousFrame.getAttribute('src') === nextFrame.getAttribute('src')) {
      const incoming = [...next.children];
      panel.querySelectorAll(':scope > :not([data-guide-frame])').forEach(node => node.remove());
      let afterFrame = false;
      incoming.forEach(node => {
        if (node === nextFrame) { afterFrame = true; return; }
        if (afterFrame) panel.append(node);
        else panel.insertBefore(node, previousFrame);
      });
      Object.assign(panel.dataset, next.dataset);
    } else {
      panel.replaceWith(next);
      panel = next;
    }
    version = panel.dataset.version;
    workspace.dataset.planId = panel.dataset.planId;
    workspace.dataset.sourceHash = panel.dataset.sourceHash;
    workspace.dataset.requirementsRevision = panel.dataset.requirementsRevision;
    if (focusedRetry) (panel.querySelector('[data-display-retry], .guide-source summary') || closer).focus();
    syncDownload();
    syncRetryButtons();
  }
  function syncRetryButtons() {
    panel.querySelectorAll('[data-display-retry]').forEach(button => {
      button.disabled = panel.dataset.businessBusy === 'true' || retrying;
    });
  }
  async function poll() {
    if (polling || stopped || document.hidden) return;
    polling = true;
    try {
      const response = await fetch(controller.dataset.stateUrl, {cache: 'no-store', signal: AbortSignal.timeout(10000)});
      if (response.ok) await update(await response.json());
      else if ([401, 404].includes(response.status)) stopped = true;
    } catch { /* The current guide and prose remain available. */ }
    finally { polling = false; schedule(); }
  }
  controller.addEventListener('click', async event => {
    const button = event.target.closest('[data-display-retry]');
    if (!button || retrying) return;
    retrying = true;
    button.disabled = true;
    try {
      const response = await fetch(controller.dataset.retryUrl, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({plan_id: panel.dataset.planId, source_hash: panel.dataset.sourceHash}),
        signal: AbortSignal.timeout(15000),
      });
      const state = await response.json();
      if (response.ok) await update(state);
      else notice(state.error || '生成未开始，请稍后重试。');
    } catch { notice('连接暂时中断，请稍后重试。'); }
    finally { retrying = false; syncRetryButtons(); schedule(); }
  });
  function notice(text) {
    let node = panel.querySelector('[data-guide-error]');
    if (!node) { node = document.createElement('p'); node.className = 'guide-notice'; node.dataset.guideError = ''; node.setAttribute('role', 'status'); panel.prepend(node); }
    node.textContent = text;
  }
  document.addEventListener('travel-task-update', poll);
  document.addEventListener('visibilitychange', () => { clearTimeout(timer); if (!document.hidden) poll(); });
  poll();
})();
