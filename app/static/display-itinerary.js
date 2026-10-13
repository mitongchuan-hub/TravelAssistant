let disposeDisplayNavigation = () => {};
function initializeDisplayNavigation() {
  disposeDisplayNavigation();
  const itinerary = document.querySelector('[data-display-itinerary]');
  const nav = itinerary?.querySelector('[data-display-day-nav]');
  if (!nav) return;
  const links = [...nav.querySelectorAll('[data-display-day-link]')];
  const days = links.map(link => itinerary.querySelector(link.getAttribute('href'))).filter(Boolean);
  const events = new AbortController();
  let frame = 0;
  function select(index) {
    links.forEach((link, i) => {
      if (i === index) link.setAttribute('aria-current', 'location');
      else link.removeAttribute('aria-current');
    });
    const selected = links[index];
    if (!selected) return;
    // Move only the horizontal tab strip; never scroll the reader vertically.
    const rect = selected.getBoundingClientRect(), bounds = nav.getBoundingClientRect();
    if (rect.left < bounds.left || rect.right > bounds.right) {
      nav.scrollTo({left: nav.scrollLeft + rect.left - bounds.left - (bounds.width - rect.width) / 2, behavior: 'instant'});
    }
  }
  function update() {
    frame = 0;
    if (!nav.isConnected || !days.length) return;
    const boundary = nav.getBoundingClientRect().bottom + 28;
    let active = 0;
    days.forEach((day, i) => { if (day.getBoundingClientRect().top <= boundary) active = i; });
    select(active);
  }
  function queue() { if (!frame) frame = requestAnimationFrame(update); }
  const resize = new ResizeObserver(() => {
    itinerary.style.setProperty('--display-nav-height', `${Math.ceil(nav.getBoundingClientRect().height)}px`);
    queue();
  });
  resize.observe(nav);
  window.addEventListener('scroll', queue, {passive: true, signal: events.signal});
  window.addEventListener('resize', queue, {passive: true, signal: events.signal});
  nav.addEventListener('click', event => {
    const link = event.target.closest('[data-display-day-link]');
    if (!link) return;
    const index = links.indexOf(link), day = days[index];
    if (!day) return;
    event.preventDefault();
    select(index);
    const navHeight = nav.getBoundingClientRect().height;
    window.scrollTo({top: window.scrollY + day.getBoundingClientRect().top - navHeight - 16,
      behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'});
    history.replaceState(null, '', link.getAttribute('href'));
  }, {signal: events.signal});
  queue();
  disposeDisplayNavigation = () => {events.abort(); resize.disconnect(); cancelAnimationFrame(frame);};
}
initializeDisplayNavigation();

document.addEventListener('click', (event) => {
  const button = event.target.closest('[data-display-edit]');
  if (!button) return;
  const feedback = document.querySelector('#text-revision-feedback');
  if (!feedback) return;
  feedback.value = `调整${button.dataset.day}的“${button.dataset.title}”，其他安排保留：`;
    feedback.scrollIntoView({ block: 'center', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' });
  feedback.focus({ preventScroll: true });
  feedback.setSelectionRange(feedback.value.length, feedback.value.length);
});

(() => {
  const controller = document.querySelector('[data-display-controller]');
  if (!controller) return;
  const planId = controller.dataset.planId, sourceHash = controller.dataset.sourceHash;
  let panel = controller.querySelector('[data-display-panel]');
  let version = panel?.dataset.version || '', status = panel?.dataset.displayStatus;
  let busy = panel?.dataset.businessBusy === 'true';
  let timer = null, polling = false, retrying = false, stopped = false;
  const current = () => controller.isConnected && controller.dataset.planId === planId
    && controller.dataset.sourceHash === sourceHash;
  function schedule() {
    clearTimeout(timer);
    if (!stopped && current() && !document.hidden && (status === 'pending' || busy)) timer = setTimeout(poll, 2000);
  }
  function notice(text) {
    let element = controller.querySelector('[data-display-notice]');
    if (!element) {
      element = document.createElement('p');
      element.dataset.displayNotice = 'true';
      element.className = 'display-progress';
      element.setAttribute('role', 'status');
      controller.prepend(element);
    }
    let label = element.querySelector('[data-display-notice-text]');
    if (!label) {
      label = document.createElement('span');
      label.dataset.displayNoticeText = 'true';
      element.prepend(label);
    }
    label.textContent = text;
  }
  async function update(state) {
    if (!current()) return;
    if (state.plan_id !== planId || state.source_hash !== sourceHash) {
      stopped = true;
      notice('当前行程已更新，请刷新后查看新方案。');
      return;
    }
    const previousStatus = status;
    status = state.display_status;
    busy = !!state.busy;
    const retry = controller.querySelector('[data-display-retry]');
    if (retry) retry.disabled = !!state.busy || retrying;
    if (state.version === version || (status === 'pending' && previousStatus === 'pending')) return;
    const url = new URL(controller.dataset.partialUrl, location.origin);
    url.searchParams.set('plan_id', planId);
    url.searchParams.set('source_hash', sourceHash);
    const response = await fetch(url, {cache: 'no-store', signal: AbortSignal.timeout(10000)});
    if (!response.ok || !current()) return;
    const html = document.createElement('template');
    html.innerHTML = await response.text();
    const next = html.content.querySelector('[data-display-panel]');
    if (!next || next.dataset.planId !== planId || next.dataset.sourceHash !== sourceHash || !current()) return;
    // Keep both the original prose and editing form in place. Adjust only the
    // viewport anchor when inserting cards above content the user is reading.
    const focused = document.activeElement;
    const prose = document.querySelector('[data-itinerary-text]');
    let anchor = focused instanceof HTMLTextAreaElement ? focused : null;
    if (!anchor && prose?.closest('details')?.open) {
      const rect = prose.getBoundingClientRect();
      if (rect.top < innerHeight && rect.bottom > 0) anchor = prose;
    }
    const before = anchor?.getBoundingClientRect().top;
    panel.replaceWith(next);
    panel = next;
    initializeDisplayNavigation();
    version = next.dataset.version;
    status = next.dataset.displayStatus;
    if (anchor?.isConnected && before !== undefined && before < innerHeight && before > -anchor.getBoundingClientRect().height) {
      // Let native scroll anchoring settle, then correct the remaining delta
      // immediately. Smooth CSS scrolling would keep moving while the user types.
      await new Promise(resolve => requestAnimationFrame(resolve));
      if (anchor.isConnected && current()) window.scrollTo({
        top: window.scrollY + anchor.getBoundingClientRect().top - before, behavior: 'instant',
      });
    }
  }
  async function poll() {
    if (polling || retrying || stopped || !current() || document.hidden) return;
    polling = true;
    try {
      const response = await fetch(controller.dataset.stateUrl, {cache: 'no-store', signal: AbortSignal.timeout(10000)});
      if (response.ok) await update(await response.json());
      else if ([401, 404].includes(response.status)) stopped = true;
    } catch { /* Prose remains readable; a later poll can recover. */ }
    finally { polling = false; schedule(); }
  }
  controller.addEventListener('click', async event => {
    const button = event.target.closest('[data-display-retry]');
    if (!button || retrying || stopped || !current()) return;
    retrying = true;
    button.disabled = true;
    try {
      const response = await fetch(controller.dataset.retryUrl, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({plan_id: planId, source_hash: sourceHash}), signal: AbortSignal.timeout(15000),
      });
      const state = await response.json();
      if (response.ok) await update(state);
      else notice(state.error || '卡片暂未整理完成，完整文字已保留。');
    } catch { notice('卡片暂未整理完成，完整文字已保留。'); }
    finally { retrying = false; if (button.isConnected) button.disabled = false; schedule(); }
  });
  document.addEventListener('visibilitychange', () => {
    clearTimeout(timer);
    if (!document.hidden && !stopped && (status === 'pending' || busy)) poll();
  });
  document.addEventListener('travel-task-update', () => { if (!stopped) poll(); });
  if (status === 'pending' || busy) poll();
})();
