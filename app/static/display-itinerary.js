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

(() => {
  const sheet = document.querySelector('[data-route-adjust]');
  const form = sheet?.querySelector('[data-route-adjust-form]');
  const field = form?.querySelector('[name=feedback]');
  const workspace = document.querySelector('[data-travel-tasks-url]');
  const storageKey = `travelassistant:route-drafts:${location.pathname}:${workspace?.dataset.planId || ''}`;
  let drafts = {}, target = '', returnFocus = null;
  try { drafts = JSON.parse(sessionStorage.getItem(storageKey) || '{}') || {}; } catch { /* Keep a local draft. */ }
  function save() {
    if (!field || !target) return;
    drafts[target] = field.value;
    try { sessionStorage.setItem(storageKey, JSON.stringify(drafts)); } catch { /* The open field stays readable. */ }
  }
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-display-edit], [data-route-quick]');
    if (!button || button.disabled) return;
    if (sheet instanceof HTMLDialogElement && form && field) {
      save();
      const item = button.hasAttribute('data-display-edit');
      target = item ? `${button.dataset.dayIndex}:${button.dataset.itemIndex}` : `all:${button.dataset.routeQuick}`;
      form.elements.day_index.value = item ? button.dataset.dayIndex : '';
      form.elements.item_index.value = item ? button.dataset.itemIndex : '';
      sheet.querySelector('[data-route-adjust-context]').textContent = item
        ? [button.dataset.day, button.dataset.title, button.dataset.time, button.dataset.location].filter(Boolean).join(' · ')
        : '针对当前整条路线调整。提交前可以修改下面的要求。';
      sheet.querySelector('[data-route-adjust-presets]').hidden = !item;
      field.value = drafts[target] || (item ? '' : button.dataset.routeQuick);
      form.querySelector('[data-revision-feedback]').hidden = true;
      returnFocus = button;
      sheet.showModal();
      field.focus({preventScroll: true});
      return;
    }
    const feedback = document.querySelector('#text-revision-feedback');
    if (!feedback) return;
    feedback.value = `调整${button.dataset.day}的“${button.dataset.title}”，其他安排保留：`;
    feedback.scrollIntoView({block: 'center', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'});
    feedback.focus({preventScroll: true});
    feedback.setSelectionRange(feedback.value.length, feedback.value.length);
  });
  if (!sheet || !form || !field) return;
  field.addEventListener('input', save);
  sheet.addEventListener('click', event => {
    const preset = event.target.closest('[data-route-adjust-preset]');
    if (preset) { field.value = preset.dataset.routeAdjustPreset; save(); field.focus(); }
    if (event.target.closest('[data-route-adjust-close]')) sheet.close();
  });
  sheet.addEventListener('close', () => { save(); returnFocus?.focus({preventScroll: true}); });
  sheet.addEventListener('cancel', save);
})();

(() => {
  const controller = document.querySelector('[data-display-controller]');
  if (!controller) return;
  const planId = controller.dataset.planId, sourceHash = controller.dataset.sourceHash;
  const requirementsRevision = controller.dataset.requirementsRevision;
  let panel = controller.querySelector('[data-display-panel]');
  let version = panel?.dataset.version || '', status = panel?.dataset.displayStatus;
  let busy = panel?.dataset.businessBusy === 'true';
  let timer = null, polling = false, retrying = false, stopped = false;
  const current = () => controller.isConnected && controller.dataset.planId === planId
    && controller.dataset.sourceHash === sourceHash;
  function schedule() {
    clearTimeout(timer);
    if (!stopped && current() && !document.hidden) timer = setTimeout(poll, status === 'pending' || busy ? 2000 : 10000);
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
    if (state.plan_id !== planId || state.source_hash !== sourceHash
        || (requirementsRevision !== undefined && state.requirements_revision !== undefined
          && String(state.requirements_revision) !== requirementsRevision)) {
      stopped = true;
      notice('路线或设置已经更新，当前输入已保留。请查看最新路线后再调整。');
      document.dispatchEvent(new CustomEvent('travel-route-outdated'));
      return;
    }
    const previousStatus = status;
    status = state.display_status;
    busy = !!state.busy;
    const retry = controller.querySelector('[data-display-retry]');
    if (retry) retry.disabled = !!state.busy || retrying;
    if (state.version === version || (status === 'pending' && previousStatus === 'pending')) return;
    if (document.querySelector('[data-route-adjust][open]')) return;
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
    if (!document.hidden && !stopped) poll();
  });
  document.addEventListener('travel-task-update', () => { if (!stopped) poll(); });
  if (status === 'pending' || busy) poll();
  else schedule();
})();
