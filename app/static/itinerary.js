/* Itinerary-only navigation and revision panel. */
(() => {
  const screen = document.querySelector('.itinerary-screen');
  if (!screen) return;

  const nav = screen.querySelector('[data-day-nav]');
  const days = Array.from(screen.querySelectorAll('[data-itinerary-day]'));
  const links = Array.from(screen.querySelectorAll('[data-day-link]'));
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let scrollFrame = null;
  let pendingDay = null;

  function selectDay(day) {
    const index = days.indexOf(day);
    links.forEach((link, i) => {
      if (i === index) link.setAttribute('aria-current', 'location');
      else link.removeAttribute('aria-current');
    });
    const selected = links[index];
    if (selected && nav) {
      const rect = selected.getBoundingClientRect();
      const navRect = nav.getBoundingClientRect();
      if (rect.left < navRect.left || rect.right > navRect.right) {
        nav.scrollTo({left: nav.scrollLeft + rect.left - navRect.left - (nav.clientWidth - rect.width) / 2, behavior: 'instant'});
      }
    }
  }

  function updateDay() {
    scrollFrame = null;
    if (!nav || !days.length) return;
    const threshold = nav.getBoundingClientRect().bottom + 20;
    if (pendingDay) {
      if (Math.abs(pendingDay.getBoundingClientRect().top - threshold) < 8) pendingDay = null;
      else return;
    }
    let current = days[0];
    for (const day of days) {
      if (day.getBoundingClientRect().top <= threshold) current = day;
    }
    if (window.scrollY > 0 && window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 3) {
      current = days[days.length - 1];
    }
    selectDay(current);
  }

  function scheduleDayUpdate() {
    if (scrollFrame === null) scrollFrame = window.requestAnimationFrame(updateDay);
  }

  function jumpTo(target, behavior = 'instant') {
    if (!nav) return;
    const topInset = nav.getBoundingClientRect().height + 16;
    window.scrollTo({top: Math.max(0, window.scrollY + target.getBoundingClientRect().top - topInset), behavior});
  }

  if (nav && days.length) {
    function measureNav() {
      const height = `${Math.ceil(nav.getBoundingClientRect().height)}px`;
      if (screen.style.getPropertyValue('--day-nav-height') !== height) screen.style.setProperty('--day-nav-height', height);
      scheduleDayUpdate();
    }
    links.forEach((link, index) => link.addEventListener('click', event => {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      pendingDay = days[index];
      selectDay(pendingDay);
      window.history.replaceState(null, '', link.hash);
      jumpTo(pendingDay, reduceMotion.matches ? 'instant' : 'smooth');
    }));
    window.addEventListener('scroll', scheduleDayUpdate, {passive: true});
    window.addEventListener('scrollend', () => { pendingDay = null; scheduleDayUpdate(); });
    ['wheel', 'touchstart', 'keydown'].forEach(type => window.addEventListener(type, () => {
      pendingDay = null;
      scheduleDayUpdate();
    }, {passive: true}));
    new ResizeObserver(measureNav).observe(nav);
    new ResizeObserver(scheduleDayUpdate).observe(screen.querySelector('.itinerary-panel'));
    function restoreAnchor() {
      measureNav();
      let id;
      try { id = decodeURIComponent(window.location.hash.slice(1)); } catch { return; }
      if (!id.startsWith('itinerary-day-') && !id.startsWith('itinerary-item-')) return;
      const target = document.getElementById(id);
      if (target) jumpTo(target);
    }
    window.addEventListener('load', restoreAnchor);
    window.addEventListener('hashchange', restoreAnchor);
    measureNav();
  }

  const sheet = screen.querySelector('[data-revision-sheet]');
  const form = sheet?.querySelector('[data-revision-form]');
  if (!(sheet instanceof HTMLDialogElement) || !(form instanceof HTMLFormElement)) return;
  const field = form.querySelector('textarea[name="feedback"]');
  const itemId = form.querySelector('input[name="item_id"]');
  const submit = form.querySelector('[type="submit"]');
  const feedback = form.querySelector('[data-revision-feedback]');
  const notice = screen.querySelector('[data-revision-notice]');
  const triggers = Array.from(screen.querySelectorAll('[data-revision-trigger]'));
  const draftKey = `travelassistant:revision-drafts:${form.dataset.workspaceUrl}`;
  let savedDrafts = [];
  try {
    const value = JSON.parse(sessionStorage.getItem(draftKey) || '{}');
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      savedDrafts = Object.entries(value).filter(([, text]) => typeof text === 'string');
    }
  } catch { /* Storage may be unavailable in private browsing. */ }
  const drafts = new Map(savedDrafts);
  let returnFocus = null;
  let inFlight = false;
  let resultMessage = '';

  function showFeedback(text) {
    feedback.textContent = text;
    feedback.hidden = !text;
    notice.textContent = text;
    notice.hidden = !text;
  }

  function persistDrafts() {
    try { sessionStorage.setItem(draftKey, JSON.stringify(Object.fromEntries(drafts))); } catch { /* Keep the in-memory draft. */ }
  }

  function rememberDraft() {
    if (itemId.value) {
      if (field.value) drafts.set(itemId.value, field.value);
      else drafts.delete(itemId.value);
      persistDrafts();
    }
  }

  function closeSheet() {
    rememberDraft();
    sheet.close();
  }

  triggers.forEach(trigger => trigger.addEventListener('click', () => {
    if (inFlight && itemId.value !== trigger.dataset.itemId) return;
    rememberDraft();
    if (itemId.value !== trigger.dataset.itemId) resultMessage = '';
    itemId.value = trigger.dataset.itemId;
    field.value = drafts.get(itemId.value) || '';
    sheet.querySelector('[data-revision-title]').textContent = trigger.dataset.itemTitle;
    sheet.querySelector('[data-revision-time]').textContent = trigger.dataset.itemTime || '时间待定';
    sheet.querySelector('[data-revision-location]').textContent = trigger.dataset.itemLocation || '地点待定';
    feedback.textContent = resultMessage;
    feedback.hidden = !resultMessage;
    returnFocus = trigger;
    sheet.showModal();
    document.documentElement.classList.add('revision-sheet-open');
    if (!inFlight) field.focus({preventScroll: true});
  }));
  field.addEventListener('input', () => { field.setCustomValidity(''); rememberDraft(); });
  sheet.querySelectorAll('[data-revision-close]').forEach(button => button.addEventListener('click', closeSheet));
  sheet.addEventListener('cancel', rememberDraft);
  sheet.addEventListener('keydown', event => {
    if (event.key !== 'Tab') return;
    const focusable = Array.from(sheet.querySelectorAll('button:not(:disabled), textarea:not(:disabled)'));
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last?.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first?.focus();
    }
  });
  sheet.addEventListener('close', () => {
    rememberDraft();
    document.documentElement.classList.remove('revision-sheet-open');
    returnFocus?.focus({preventScroll: true});
  });
  sheet.addEventListener('click', event => {
    if (event.target !== sheet) return;
    const rect = sheet.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closeSheet();
  });

  form.addEventListener('submit', async event => {
    if (event.defaultPrevented) return;
    event.preventDefault();
    if (inFlight) return;
    if (!field.value.trim()) {
      field.setCustomValidity('请填写想调整的内容');
      field.reportValidity();
      return;
    }
    field.setCustomValidity('');
    rememberDraft();
    inFlight = true;
    form.setAttribute('aria-busy', 'true');
    submit.disabled = true;
    submit.textContent = '正在调整…';
    field.readOnly = true;
    triggers.forEach(trigger => { trigger.disabled = trigger.dataset.itemId !== itemId.value; });
    resultMessage = '正在调整这项安排，完成后会更新行程。';
    showFeedback(resultMessage);
    try {
      const response = await fetch(form.action, {method: 'POST', body: new FormData(form)});
      const destination = new URL(response.url, window.location.href);
      if (!response.ok) throw new Error('request failed');
      if (destination.origin !== window.location.origin || destination.pathname !== form.dataset.workspaceUrl) {
        resultMessage = '登录状态已失效。请复制修改意见，重新登录后再提交。';
      } else if (response.redirected && destination.searchParams.get('tab') === 'itinerary') {
        drafts.delete(itemId.value);
        persistDrafts();
        destination.hash = `itinerary-item-${itemId.value}`;
        window.location.assign(destination.href);
        return;
      } else if (response.redirected && destination.searchParams.get('tab') === 'chat') {
        const html = new DOMParser().parseFromString(await response.text(), 'text/html');
        const replies = html.querySelectorAll('.chat-list .message.agent .message-body > p');
        resultMessage = replies[replies.length - 1]?.textContent?.trim() || '这次调整没有完成，请稍后重试。';
        resultMessage += ' 修改意见已保留。';
      } else {
        throw new Error('unexpected response');
      }
    } catch {
      // A lost connection does not prove whether the server saved a revision.
      resultMessage = '未能确认调整结果，修改意见已保留。请先刷新行程查看结果，再决定是否重试。';
    } finally {
      inFlight = false;
      form.removeAttribute('aria-busy');
      submit.disabled = false;
      submit.textContent = '提交调整';
      field.readOnly = false;
      triggers.forEach(trigger => { trigger.disabled = false; });
    }
    showFeedback(resultMessage);
  });
})();
