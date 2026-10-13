/* Chat viewport, draft and reading state. Shared message transport lives in app.js. */
function isChatInput(input) {
  return input instanceof HTMLInputElement || input instanceof HTMLTextAreaElement;
}
function isChatScrollContainer(list) {
  return ['auto', 'scroll'].includes(getComputedStyle(list).overflowY);
}
function captureChatView(list) {
  if (!isChatScrollContainer(list)) return {
    nearBottom: innerHeight + scrollY >= document.documentElement.scrollHeight - 140,
    scroll: scrollY, container: false,
  };
  const top = list.getBoundingClientRect().top;
  const anchor = Array.from(list.querySelectorAll('[data-message-id]')).find(node => node.getBoundingClientRect().bottom > top + 1);
  return {nearBottom: list.scrollHeight - list.scrollTop - list.clientHeight < 100,
    scroll: list.scrollTop, anchorId: anchor?.dataset.messageId,
    anchorOffset: anchor ? anchor.getBoundingClientRect().top - top : null, container: true};
}
function restoreChatView(list, view) {
  if (!view.container) { window.scrollTo({top: view.scroll, behavior: 'instant'}); return; }
  list.scrollTop = view.scroll;
  const anchor = Array.from(list.querySelectorAll('[data-message-id]')).find(node => node.dataset.messageId === view.anchorId);
  if (anchor && view.anchorOffset !== null) {
    list.scrollTop += anchor.getBoundingClientRect().top - list.getBoundingClientRect().top - view.anchorOffset;
  }
}
function setChatSendFeedback(text) {
  const node = document.querySelector('[data-chat-send-feedback]');
  if (node) { node.textContent = text; node.hidden = !text; }
}
function updateChatComposer() {
  const form = document.querySelector('.composer');
  const input = form?.querySelector('[name="body"]');
  if (!isChatInput(input)) return;
  if (input instanceof HTMLTextAreaElement) {
    input.style.height = '0px';
    const height = Math.max(38, Math.min(input.scrollHeight, 126));
    input.style.height = `${height}px`;
    input.style.overflowY = input.scrollHeight > 126 ? 'auto' : 'hidden';
  }
  const button = form.querySelector('button[type="submit"]');
  let uncertain = false;
  try { uncertain = Boolean(sessionStorage.getItem(`travelassistant:task-submit:${(form.dataset.apiUrl || '').replace(/\/messages$/, '/tasks')}`)); } catch {}
  if (button) button.disabled = form.dataset.submitting === 'true' || uncertain || !input.value.trim();
}
function saveChatDraft() {
  const input = document.querySelector('.composer [name="body"]');
  if (!isChatInput(input)) return;
  try { sessionStorage.setItem(`travelassistant:chat-draft:${location.pathname}`, input.value); } catch {}
}
function initializeChat() {
  const screen = document.querySelector('.chat-screen');
  const input = document.querySelector('.composer [name="body"]');
  if (!screen || !isChatInput(input)) return;
  const form = input.closest('form');
  document.body.classList.add('chat-page');
  try {
    if (!input.value && !new URL(location.href).searchParams.has('draft')) {
      input.value = sessionStorage.getItem(`travelassistant:chat-draft:${location.pathname}`) || '';
    }
  } catch {}
  input.addEventListener('input', () => { updateChatComposer(); saveChatDraft(); });
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229
        && matchMedia('(pointer: fine)').matches && !matchMedia('(pointer: coarse)').matches) {
      event.preventDefault();
      if (!form.querySelector('button[type="submit"]')?.disabled) form.requestSubmit();
    }
  });
  const list = screen.querySelector('[data-chat-list]');
  list?.addEventListener('scroll', () => {
    if (captureChatView(list).nearBottom) {
      const notice = screen.querySelector('[data-chat-new]');
      if (notice) notice.hidden = true;
    }
  }, {passive: true});
  screen.addEventListener('click', event => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    if (target.closest('[data-chat-new]')) scrollChatToLatest();
    const collapse = target.closest('[data-chat-collapse]');
    if (collapse) {
      const details = collapse.closest('details');
      details.open = false;
      details.querySelector('summary').focus({preventScroll: true});
      details.closest('.message').scrollIntoView({block: 'nearest', behavior: 'instant'});
    }
    const draft = target.closest('[data-chat-draft]');
    if (draft) {
      if (input.value.trim()) setChatSendFeedback('输入框已有草稿，请发送或清空后再重试。');
      else { input.value = draft.dataset.chatDraft; updateChatComposer(); saveChatDraft(); }
      input.focus({preventScroll: true});
    }
  });
  const menu = screen.querySelector('[data-chat-menu]');
  document.addEventListener('click', event => { if (menu && !menu.contains(event.target)) menu.open = false; });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && menu?.open) { menu.open = false; menu.querySelector('summary').focus(); }
  });
  let scheduled = false;
  function viewportChanged() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      const nearBottom = list && captureChatView(list).nearBottom;
      const viewport = window.visualViewport;
      const shell = document.querySelector('.phone-shell--chat');
      if (shell && viewport) {
        shell.style.setProperty('--chat-viewport-height', `${viewport.height}px`);
        shell.style.setProperty('--chat-viewport-top', `${viewport.offsetTop}px`);
        shell.classList.toggle('chat-keyboard-open', document.activeElement === input && innerHeight - viewport.height > 120);
      }
      if (nearBottom) scrollChatToLatest();
    });
  }
  window.visualViewport?.addEventListener('resize', viewportChanged);
  window.visualViewport?.addEventListener('scroll', viewportChanged);
  window.addEventListener('resize', viewportChanged);
  input.addEventListener('focus', viewportChanged);
  input.addEventListener('blur', viewportChanged);
  updateChatComposer();
  viewportChanged();
}
document.addEventListener('travel-task-update', event => {
  const task = event.detail;
  const list = document.querySelector('[data-chat-list]');
  if (!task || !list || task.kind !== 'chat') return;
  const pending = Array.from(list.querySelectorAll('[data-optimistic]')).find(node => node.dataset.requestId === task.request_id);
  if (pending && task.message_id) {
    pending.dataset.messageId = task.message_id;
    pending.classList.remove('pending-message');
    pending.dataset.status = 'sent';
    delete pending.dataset.optimistic;
    pending.querySelector('[data-message-state]')?.replaceChildren();
    const thinking = Array.from(list.querySelectorAll('[data-agent-thinking-message]')).find(node => node.dataset.requestId === task.request_id);
    if (thinking && task.thinking_id) thinking.dataset.messageId = task.thinking_id;
    else thinking?.remove();
    setChatSendFeedback('');
  }
  if (pending || ['succeeded', 'failed', 'superseded'].includes(task.status)) refreshChatMessages(list, {force: true});
  updateChatComposer();
});
document.addEventListener('travel-chat-rejected', event => {
  const list = document.querySelector('[data-chat-list]');
  if (!list) return;
  const request = event.detail;
  const pending = Array.from(list.querySelectorAll('[data-optimistic]')).find(node => node.dataset.requestId === request.request_id);
  if (pending) markMessageFailed(pending, request.payload.body);
  Array.from(list.querySelectorAll('[data-agent-thinking-message]')).find(node => node.dataset.requestId === request.request_id)?.remove();
  setChatSendFeedback('');
  updateChatComposer();
});
window.addEventListener('load', initializeChat);
