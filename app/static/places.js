document.addEventListener('click', async event => {
  const button = event.target.closest('[data-copy-address]');
  if (!button) return;
  const status = document.querySelector('[data-copy-status]');
  try {
    await navigator.clipboard.writeText(button.dataset.copyAddress);
    if (status) { status.hidden = false; status.textContent = '地址已复制'; }
    else button.textContent = '已复制';
  } catch {
    if (status) { status.hidden = false; status.textContent = '未能自动复制，请长按地址文字复制。'; }
    else button.textContent = '请长按地址复制';
  }
});
