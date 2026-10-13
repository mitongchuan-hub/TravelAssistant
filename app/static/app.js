function createTravelRequestId() {
  return typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
}

document.addEventListener('submit', (event) => {
  const form = event.target;
  if (event.defaultPrevented || !(form instanceof HTMLFormElement)) {
    return;
  }

  if (form.dataset.submitting === 'true') {
    event.preventDefault();
    return;
  }

  if (form.classList.contains('composer')) {
    event.preventDefault();
    form.dataset.submitting = 'true';
    submitComposerMessage(form);
    return;
  }

  const button = form.querySelector('button[type="submit"]');
  form.dataset.submitting = 'true';
  form.setAttribute('aria-busy', 'true');
  if (button) {
    button.disabled = true;
    button.dataset.originalText = button.textContent || '';
  }
});

async function submitComposerMessage(form) {
  const input = form.querySelector('[name="body"]');
  const button = form.querySelector('button[type="submit"]');
  const chatList = document.querySelector('[data-chat-list]');
  if (!isChatInput(input) || !(chatList instanceof HTMLElement)) {
    form.submit();
    return;
  }

  const body = input.value.trim();
  if (!body) {
    form.dataset.submitting = 'false';
    updateChatComposer();
    return;
  }
  const taskStorageKey = `travelassistant:task-submit:${(form.dataset.apiUrl || '').replace(/\/messages$/, '/tasks')}`;
  let uncertain = false;
  try { uncertain = Boolean(sessionStorage.getItem(taskStorageKey)); } catch { /* Recovery remains available through task polling. */ }
  if (uncertain) {
    form.dataset.submitting = 'false';
    setChatSendFeedback('上一条消息正在确认是否送达，请稍候。');
    updateChatComposer();
    return;
  }

  const requestId = form.dataset.retryRequestId || createTravelRequestId();
  delete form.dataset.retryRequestId;
  const user = currentUser(form);
  const pendingMessage = appendUserMessage(body, user);
  pendingMessage.dataset.requestId = requestId;
  input.value = '';
  updateChatComposer();
  saveChatDraft();
  form.setAttribute('aria-busy', 'true');
  if (button) {
    button.disabled = true;
  }
  await waitForNextPaint();
  const thinkingMessage = appendAgentThinkingMessage();
  scrollChatToLatest();

  pendingMessage.dataset.optimistic = 'true';
  if (thinkingMessage) thinkingMessage.dataset.requestId = requestId;
  try { sessionStorage.setItem(taskStorageKey, JSON.stringify({kind: 'chat', payload: {body}, request_id: requestId})); } catch { /* Keep same ID on the message for retry. */ }
  try {
    const response = await fetch(form.dataset.apiUrl || form.action, {
      method: 'post',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' },
      body: JSON.stringify({ body, request_id: requestId }),
      signal: AbortSignal.timeout(15000),
    });
    if (!response.ok) {
      try { sessionStorage.removeItem(taskStorageKey); } catch { /* Storage unavailable. */ }
      pendingMessage.dataset.rejected = 'true';
      throw new Error('send failed');
    }
    const payload = await response.json();
    try { sessionStorage.removeItem(taskStorageKey); } catch { /* Storage unavailable. */ }
    const accepted = payload.messages[0];
    if (!accepted) throw new Error('missing message');
    const existing = Array.from(chatList.querySelectorAll('[data-message-id]')).find(node => node !== pendingMessage && node.dataset.messageId === accepted.id);
    if (existing) pendingMessage.remove();
    else pendingMessage.dataset.messageId = accepted.id;
    pendingMessage.dataset.status = 'sent';
    delete pendingMessage.dataset.optimistic;
    pendingMessage.querySelector('[data-message-state]')?.replaceChildren();
    pendingMessage.classList.remove('pending-message');
    setChatSendFeedback('');
    if (payload.task) document.dispatchEvent(new CustomEvent('travel-chat-accepted', {detail: payload.task}));
    if (thinkingMessage && payload.messages[1]) {
      thinkingMessage.dataset.messageId = payload.messages[1].id;
    } else thinkingMessage?.remove();
    await refreshChatMessages(chatList, { force: true });
  } catch (error) {
    if (pendingMessage.dataset.rejected === 'true' || !document.querySelector('[data-travel-tasks-url]')) {
      markMessageFailed(pendingMessage, body);
      thinkingMessage?.remove();
    } else if (pendingMessage.dataset.optimistic === 'true') {
      pendingMessage.querySelector('[data-message-state]')?.replaceChildren('正在确认是否送达…');
      setChatSendFeedback('连接中断，正在确认是否送达。消息会保留。');
    } else setChatSendFeedback('');
  } finally {
    form.dataset.submitting = 'false';
    form.removeAttribute('aria-busy');
    updateChatComposer();
    if (document.activeElement === input) input.focus({preventScroll: true});
  }
}

function waitForNextPaint() {
  return new Promise((resolve) => {
    window.requestAnimationFrame(() => window.requestAnimationFrame(resolve));
  });
}

document.addEventListener('click', (event) => {
  const target = event.target;
  if (!(target instanceof Element)) {
    return;
  }

  const ideaTrigger = target.closest('[data-idea-detail-trigger]');
  if (ideaTrigger instanceof HTMLElement) {
    openIdeaDetail(ideaTrigger);
    return;
  }

  if (target.closest('[data-idea-detail-close]')) {
    closeIdeaDetail();
    return;
  }

  const retryButton = target.closest('[data-retry-message]');
  if (retryButton instanceof HTMLButtonElement) {
    retryFailedMessage(retryButton);
  }

});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    closeIdeaDetail();
  }
  if (event.key === 'Tab') {
    const sheet = document.querySelector('[data-idea-detail-sheet].open');
    if (!sheet) return;
    const buttons = Array.from(sheet.querySelectorAll('button:not(:disabled)'));
    const first = buttons[0], last = buttons[buttons.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last?.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first?.focus();
    }
  }
});

let ideaDetailReturnFocus = null;

function openIdeaDetail(trigger) {
  const sheet = document.querySelector('[data-idea-detail-sheet]');
  if (!(sheet instanceof HTMLElement)) {
    return;
  }

  ideaDetailReturnFocus = trigger;
  setDetailText('[data-idea-detail-title]', trigger.dataset.title);
  setDetailText('[data-idea-detail-kind]', trigger.dataset.kind);
  setDetailText('[data-idea-detail-body]', trigger.dataset.body);
  setDetailText('[data-idea-detail-author]', trigger.dataset.author);
  setDetailText('[data-idea-detail-status]', trigger.dataset.status);
  const deleteId = sheet.querySelector('[data-idea-delete-id]');
  if (deleteId instanceof HTMLInputElement) {
    deleteId.value = trigger.dataset.ideaId || '';
  }
  sheet.removeAttribute('aria-hidden');
  sheet.classList.add('open');
  document.body.classList.add('sheet-open');
  const closeButton = sheet.querySelector('[data-idea-detail-close]');
  if (closeButton instanceof HTMLElement) {
    closeButton.focus();
  }
}

function closeIdeaDetail() {
  const sheet = document.querySelector('[data-idea-detail-sheet]');
  if (!(sheet instanceof HTMLElement)) {
    return;
  }

  const wasOpen = sheet.classList.contains('open');
  sheet.setAttribute('aria-hidden', 'true');
  sheet.classList.remove('open');
  document.body.classList.remove('sheet-open');
  if (wasOpen) ideaDetailReturnFocus?.focus({ preventScroll: true });
}

function setDetailText(selector, value) {
  document.querySelectorAll(selector).forEach((element) => {
    element.textContent = value || '';
  });
}

function appendAgentThinkingMessage() {
  const chatList = document.querySelector('[data-chat-list]');
  if (!(chatList instanceof HTMLElement)) {
    return;
  }

  const article = document.createElement('article');
  article.className = 'message agent thinking-message';
  article.dataset.status = 'thinking';
  article.dataset.agentThinkingMessage = 'true';
  article.setAttribute('aria-live', 'polite');
  article.innerHTML = `
    <span class="avatar" aria-hidden="true">✦</span>
    <div class="message-body">
      <div class="chat-bubble chat-thinking" role="status">正在整理回复 <span class="typing-dots" aria-hidden="true"><i></i><i></i><i></i></span></div>
    </div>
  `;
  chatList.append(article);
  return article;
}

function appendUserMessage(body, user = { initials: '我', name: '我' }) {
  const chatList = document.querySelector('.chat-list');
  if (!(chatList instanceof HTMLElement)) {
    return;
  }

  const article = document.createElement('article');
  article.className = 'message user pending-message';
  article.dataset.status = 'sending';
  article.setAttribute('aria-label', '你的消息');
  article.innerHTML = `
    <div class="message-body">
      <div class="chat-bubble chat-user-text"></div>
      <span class="chat-message-state" data-message-state>发送中…</span>
    </div>
  `;
  const bubble = article.querySelector('.chat-user-text');
  if (bubble) {
    bubble.textContent = body;
  }
  chatList.querySelector('[data-chat-empty]')?.remove();
  chatList.append(article);
  return article;
}

function markMessageFailed(messageElement, body) {
  if (!(messageElement instanceof HTMLElement)) {
    return;
  }
  messageElement.classList.remove('pending-message');
  messageElement.classList.add('failed-message');
  messageElement.dataset.status = 'failed';
  messageElement.dataset.failedBody = body;
  messageElement.querySelector('[data-message-state]')?.replaceChildren();
  const bodyElement = messageElement.querySelector('.message-body');
  if (!bodyElement || bodyElement.querySelector('[data-retry-message]')) {
    return;
  }
  const retry = document.createElement('button');
  retry.type = 'button';
  retry.className = 'retry-message';
  retry.dataset.retryMessage = 'true';
  retry.textContent = '发送失败，点此重试';
  bodyElement.append(retry);
}

function retryFailedMessage(button) {
  const messageElement = button.closest('.failed-message');
  const form = document.querySelector('.composer');
  const input = form?.querySelector('[name="body"]');
  if (!(messageElement instanceof HTMLElement) || !(form instanceof HTMLFormElement) || !isChatInput(input)) {
    return;
  }
  if (form.dataset.submitting === 'true') {
    return;
  }
  form.dataset.retryRequestId = messageElement.dataset.requestId || createTravelRequestId();
  const draft = input.value;
  input.value = messageElement.dataset.failedBody || '';
  updateChatComposer();
  messageElement.remove();
  form.requestSubmit();
  input.value = draft;
  updateChatComposer();
  saveChatDraft();
}

function currentUser(form) {
  return {
    initials: form.dataset.currentInitials || '我',
    name: form.dataset.currentName || '我',
  };
}

function drawIdeaRoutes() {
  document.querySelectorAll('[data-route-board]').forEach((board) => {
    const svg = board.querySelector('.travel-route');
    const path = board.querySelector('[data-route-path]');
    const cards = Array.from(board.querySelectorAll('[data-route-index]')).sort((a, b) => {
      return Number(a.dataset.routeIndex) - Number(b.dataset.routeIndex);
    });

    if (!(svg instanceof SVGSVGElement) || !(path instanceof SVGPathElement) || cards.length === 0) {
      return;
    }

    const boardRect = board.getBoundingClientRect();
    layoutIdeaCards(board, cards, boardRect);
    svg.setAttribute('viewBox', `0 0 ${boardRect.width} ${boardRect.height}`);

    if (cards.length < 2) {
      path.removeAttribute('d');
      return;
    }

    const points = cards.map((card) => {
      const rect = card.getBoundingClientRect();
      return {
        x: rect.left - boardRect.left + rect.width / 2,
        y: rect.top - boardRect.top + rect.height / 2,
      };
    });

    const commands = [`M ${points[0].x.toFixed(1)} ${points[0].y.toFixed(1)}`];
    for (let index = 1; index < points.length; index += 1) {
      const previous = points[index - 1];
      const current = points[index];
      const midX = (previous.x + current.x) / 2;
      commands.push(`C ${midX.toFixed(1)} ${previous.y.toFixed(1)}, ${midX.toFixed(1)} ${current.y.toFixed(1)}, ${current.x.toFixed(1)} ${current.y.toFixed(1)}`);
    }

    path.setAttribute('d', commands.join(' '));
    path.classList.remove('route-draw');
    window.requestAnimationFrame(() => path.classList.add('route-draw'));
  });
}

function layoutIdeaCards(board, cards, boardRect) {
  const routePoints = routePointsForCount(cards.length);
  const width = Math.max(94, Math.min(136, boardRect.width * (cards.length > 6 ? 0.32 : 0.38)));
  const titleSize = cards.length > 6 ? 13 : 15;

  cards.forEach((card, index) => {
    const point = routePoints[index];
    const cardHeight = estimateCardHeight(card, width);
    const left = clamp(point.x * boardRect.width - width / 2, 18, boardRect.width - width - 18);
    const top = clamp(point.y * boardRect.height - cardHeight / 2, 18, boardRect.height - cardHeight - 18);
    card.style.setProperty('--idea-width', `${width.toFixed(1)}px`);
    card.style.setProperty('--idea-left', `${left.toFixed(1)}px`);
    card.style.setProperty('--idea-top', `${top.toFixed(1)}px`);
    card.style.setProperty('--idea-title-size', `${titleSize}px`);
  });

  board.dataset.cardCount = String(cards.length);
}

function routePointsForCount(count) {
  const layouts = {
    1: [{ x: 0.50, y: 0.48 }],
    2: [{ x: 0.25, y: 0.25 }, { x: 0.66, y: 0.66 }],
    3: [{ x: 0.23, y: 0.18 }, { x: 0.66, y: 0.42 }, { x: 0.35, y: 0.72 }],
    4: [{ x: 0.22, y: 0.16 }, { x: 0.66, y: 0.28 }, { x: 0.30, y: 0.56 }, { x: 0.70, y: 0.76 }],
    5: [{ x: 0.22, y: 0.16 }, { x: 0.64, y: 0.24 }, { x: 0.30, y: 0.46 }, { x: 0.68, y: 0.58 }, { x: 0.48, y: 0.82 }],
    6: [{ x: 0.22, y: 0.14 }, { x: 0.64, y: 0.23 }, { x: 0.28, y: 0.40 }, { x: 0.68, y: 0.53 }, { x: 0.32, y: 0.72 }, { x: 0.70, y: 0.84 }],
    7: [{ x: 0.22, y: 0.13 }, { x: 0.64, y: 0.22 }, { x: 0.28, y: 0.38 }, { x: 0.68, y: 0.50 }, { x: 0.30, y: 0.66 }, { x: 0.70, y: 0.78 }, { x: 0.42, y: 0.90 }],
    8: [{ x: 0.22, y: 0.12 }, { x: 0.66, y: 0.21 }, { x: 0.26, y: 0.34 }, { x: 0.70, y: 0.45 }, { x: 0.30, y: 0.58 }, { x: 0.72, y: 0.69 }, { x: 0.28, y: 0.82 }, { x: 0.70, y: 0.91 }],
  };

  return layouts[Math.min(count, 8)];
}

function estimateCardHeight(card, width) {
  const title = card.querySelector('strong')?.textContent || '';
  const lines = Math.max(1, Math.ceil(title.length / Math.max(5, Math.floor(width / 18))));
  return 39 + lines * 18;
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(value, max));
}

window.addEventListener('load', drawIdeaRoutes);
window.addEventListener('load', scrollChatToLatest);
window.addEventListener('resize', () => window.requestAnimationFrame(drawIdeaRoutes));

function scrollChatToLatest() {
  const chatList = document.querySelector('.chat-list');
  if (chatList) {
    if (isChatScrollContainer(chatList)) chatList.scrollTop = chatList.scrollHeight;
    else window.scrollTo({ top: document.documentElement.scrollHeight, behavior: 'auto' });
    const notice = document.querySelector('[data-chat-new]');
    if (notice) notice.hidden = true;
  }
}

const CHAT_POLL_ACTIVE_MS = 1000;
const CHAT_POLL_IDLE_MS = 3000;
let chatPollingTimer = null;
let chatPollingInFlight = false;

function startChatPolling() {
  const chatList = document.querySelector('[data-chat-list]');
  if (!(chatList instanceof HTMLElement) || !chatList.dataset.messagesUrl) {
    return;
  }

  const poll = async () => {
    await refreshChatMessages(chatList);
    if (!document.hidden) chatPollingTimer = window.setTimeout(poll,
      chatList.querySelector('[data-agent-thinking-message]') ? CHAT_POLL_ACTIVE_MS : CHAT_POLL_IDLE_MS);
  };
  chatPollingTimer = window.setTimeout(poll, CHAT_POLL_IDLE_MS);
  document.addEventListener('visibilitychange', () => {
    clearTimeout(chatPollingTimer);
    if (!document.hidden) poll();
  });
}

async function refreshChatMessages(chatList, options = {}) {
  const composer = document.querySelector('.composer');
  const force = options.force === true;
  if (chatPollingInFlight || (!force && document.hidden)) {
    return;
  }
  if (!force && composer instanceof HTMLFormElement && composer.dataset.submitting === 'true') {
    return;
  }
  chatPollingInFlight = true;
  try {
    const response = await fetch(chatList.dataset.messagesUrl, {
      headers: { 'X-Requested-With': 'fetch' },
      cache: 'no-store',
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok || response.redirected) {
      return;
    }
    const html = await response.text();
    // A poll started before Send must not wipe the optimistic outgoing message.
    if (!force && composer?.dataset.submitting === 'true') {
      return;
    }
    if (html.trim()) {
      const view = captureChatView(chatList);
      const wasNearBottom = view.nearBottom;
      const fragment = document.createElement('template');
      fragment.innerHTML = html;
      const incoming = Array.from(fragment.content.querySelectorAll('[data-message-id]'));
      const existing = new Map(Array.from(chatList.querySelectorAll('[data-message-id]'))
        .map((node) => [node.dataset.messageId, node]));
      const messages = incoming.map((node) => {
        const previous = existing.get(node.dataset.messageId);
        if (previous?.querySelector('details[open]')) {
          node.querySelector('details')?.setAttribute('open', '');
        }
        return previous?.outerHTML === node.outerHTML ? previous : node;
      });
      // Unsent messages must remain available for the existing retry action.
      const local = Array.from(chatList.querySelectorAll('.failed-message, .pending-message'));
      const extras = local.length || messages.length ? [] : Array.from(fragment.content.querySelectorAll('[data-chat-empty]'));
      if (messages.some((node, index) => chatList.children[index] !== node)
          || chatList.children.length !== messages.length + local.length + extras.length) {
        chatList.replaceChildren(...messages, ...local, ...extras);
        if (wasNearBottom) scrollChatToLatest();
        else {
          restoreChatView(chatList, view);
          const notice = document.querySelector('[data-chat-new]');
          if (notice) notice.hidden = false;
        }
        document.querySelector('[data-chat-announcement]')?.replaceChildren('收到新回复');
        updateChatComposer();
      }
    }
  } catch (error) {
    // Keep the current messages if polling fails.
  } finally {
    chatPollingInFlight = false;
  }
}

window.addEventListener('load', startChatPolling);
