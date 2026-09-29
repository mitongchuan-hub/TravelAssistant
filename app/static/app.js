document.addEventListener('submit', (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) {
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
  const input = form.querySelector('input[name="body"]');
  const button = form.querySelector('button[type="submit"]');
  const chatList = document.querySelector('[data-chat-list]');
  if (!(input instanceof HTMLInputElement) || !(chatList instanceof HTMLElement)) {
    form.submit();
    return;
  }

  const body = input.value.trim();
  if (!body) {
    form.dataset.submitting = 'false';
    return;
  }

  const member = selectedMember(form);
  const isAgentMessage = isAgentMentionValue(body);
  const pendingMessage = appendUserMessage(body, member);
  input.value = '';
  input.disabled = true;
  form.setAttribute('aria-busy', 'true');
  if (button) {
    button.disabled = true;
  }
  if (isAgentMessage) {
    await waitForNextPaint();
    appendAgentThinkingMessage();
  }
  scrollChatToLatest();

  try {
    const response = await fetch(form.dataset.apiUrl || form.action, {
      method: 'post',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' },
      body: JSON.stringify({ body }),
    });
    if (!response.ok) {
      throw new Error('send failed');
    }
    await refreshChatMessages(chatList, { force: true });
  } catch (error) {
    markMessageFailed(pendingMessage, body);
    chatList.querySelectorAll('[data-agent-thinking-message]').forEach((item) => item.remove());
  } finally {
    form.dataset.submitting = 'false';
    form.removeAttribute('aria-busy');
    input.disabled = false;
    if (button) {
      button.disabled = false;
    }
    input.focus();
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

  const copyButton = target.closest('[data-copy-button]');
  if (copyButton instanceof HTMLButtonElement) {
    copyInviteLink(copyButton);
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
});

async function copyInviteLink(button) {
  const source = document.querySelector('[data-copy-source]');
  if (!(source instanceof HTMLInputElement)) {
    return;
  }

  source.select();
  source.setSelectionRange(0, source.value.length);
  const originalText = button.textContent || button.dataset.copyLabel || '复制';
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(source.value);
    } else {
      document.execCommand('copy');
    }
    button.textContent = '已复制';
  } catch (error) {
    button.textContent = '请长按复制';
  }
  window.setTimeout(() => {
    button.textContent = originalText;
  }, 1600);
}

function openIdeaDetail(trigger) {
  const sheet = document.querySelector('[data-idea-detail-sheet]');
  if (!(sheet instanceof HTMLElement)) {
    return;
  }

  setDetailText('[data-idea-detail-title]', trigger.dataset.title);
  setDetailText('[data-idea-detail-kind]', trigger.dataset.kind);
  setDetailText('[data-idea-detail-body]', trigger.dataset.body);
  setDetailText('[data-idea-detail-author]', trigger.dataset.author);
  setDetailText('[data-idea-detail-status]', trigger.dataset.status);
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

  sheet.setAttribute('aria-hidden', 'true');
  sheet.classList.remove('open');
  document.body.classList.remove('sheet-open');
}

function setDetailText(selector, value) {
  document.querySelectorAll(selector).forEach((element) => {
    element.textContent = value || '';
  });
}

function isAgentMentionSubmit(form) {
  if (!form.classList.contains('composer')) {
    return false;
  }

  const input = form.querySelector('input[name="body"]');
  return input instanceof HTMLInputElement && isAgentMentionValue(input.value);
}

function isAgentMentionValue(value) {
  const cleanValue = value.trim().toLowerCase();
  return cleanValue.startsWith('@旅行助手') || cleanValue.startsWith('@agent') || cleanValue.startsWith('@ai');
}

function appendAgentThinkingMessage() {
  const chatList = document.querySelector('[data-chat-list]');
  if (!(chatList instanceof HTMLElement) || chatList.querySelector('[data-agent-thinking-message]')) {
    return;
  }

  const article = document.createElement('article');
  article.className = 'message agent thinking-message';
  article.dataset.status = 'thinking';
  article.dataset.agentThinkingMessage = 'true';
  article.setAttribute('aria-live', 'polite');
  article.innerHTML = `
    <span class="avatar">AI</span>
    <div class="message-body">
      <div class="message-meta">
        <strong>旅行规划 Agent</strong>
        <time>刚刚</time>
      </div>
      <p>思考中 <span class="typing-dots"><i></i><i></i><i></i></span></p>
    </div>
  `;
  chatList.append(article);
}

function appendUserMessage(body, member = { initials: '林', name: '林夏' }) {
  const chatList = document.querySelector('.chat-list');
  if (!(chatList instanceof HTMLElement)) {
    return;
  }

  const article = document.createElement('article');
  article.className = 'message user pending-message';
  article.dataset.status = 'sending';
  article.innerHTML = `
    <span class="avatar"></span>
    <div class="message-body">
      <div class="message-meta">
        <strong></strong>
        <time>刚刚</time>
      </div>
      <p></p>
    </div>
  `;
  const avatar = article.querySelector('.avatar');
  const name = article.querySelector('.message-meta strong');
  if (avatar) {
    avatar.textContent = member.initials || '我';
  }
  if (name) {
    name.textContent = member.name || '我';
  }
  const bubble = article.querySelector('.message-body > p');
  if (bubble) {
    bubble.textContent = body;
  }
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
  const input = form?.querySelector('input[name="body"]');
  if (!(messageElement instanceof HTMLElement) || !(form instanceof HTMLFormElement) || !(input instanceof HTMLInputElement)) {
    return;
  }
  input.value = messageElement.dataset.failedBody || '';
  messageElement.remove();
  form.requestSubmit();
}

function selectedMember(form) {
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

    if (!(svg instanceof SVGSVGElement) || !(path instanceof SVGPathElement) || cards.length < 2) {
      return;
    }

    const boardRect = board.getBoundingClientRect();
    layoutIdeaCards(board, cards, boardRect);
    svg.setAttribute('viewBox', `0 0 ${boardRect.width} ${boardRect.height}`);

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
    window.scrollTo({ top: document.documentElement.scrollHeight, behavior: 'auto' });
  }
}

let chatPollingTimer = null;
let chatPollingInFlight = false;

function startChatPolling() {
  const chatList = document.querySelector('[data-chat-list]');
  if (!(chatList instanceof HTMLElement) || !chatList.dataset.messagesUrl) {
    return;
  }

  chatPollingTimer = window.setInterval(() => refreshChatMessages(chatList), 3000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      refreshChatMessages(chatList);
    }
  });
}

async function refreshChatMessages(chatList, options = {}) {
  const composer = document.querySelector('.composer');
  const input = composer?.querySelector('input[name="body"]');
  const force = options.force === true;
  if (chatPollingInFlight || (!force && document.hidden)) {
    return;
  }
  if (!force && composer instanceof HTMLFormElement && composer.dataset.submitting === 'true') {
    return;
  }
  if (!force && input instanceof HTMLInputElement && input.value.trim()) {
    return;
  }

  chatPollingInFlight = true;
  try {
    const response = await fetch(chatList.dataset.messagesUrl, {
      headers: { 'X-Requested-With': 'fetch' },
      cache: 'no-store',
    });
    if (!response.ok || response.redirected) {
      return;
    }
    const html = await response.text();
    if (html.trim() && html !== chatList.innerHTML) {
      const wasNearBottom = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 140;
      chatList.innerHTML = html;
      if (wasNearBottom) {
        scrollChatToLatest();
      }
    }
  } catch (error) {
    // Keep the current messages if polling fails.
  } finally {
    chatPollingInFlight = false;
  }
}

window.addEventListener('load', startChatPolling);
