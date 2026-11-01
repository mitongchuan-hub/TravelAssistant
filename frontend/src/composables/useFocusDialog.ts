import { nextTick, onBeforeUnmount, watch, type Ref } from 'vue'

export function useFocusDialog(open: Ref<boolean>, panel: Ref<HTMLElement | null>, close: () => void) {
  let previous: HTMLElement | null = null
  const focusable = () => Array.from(panel.value?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], input:not(:disabled), textarea:not(:disabled), summary, iframe, [tabindex="0"]') ?? []).filter(el => el.getClientRects().length > 0 && !el.closest('details:not([open]) > :not(summary)'))
  function keydown(event: KeyboardEvent) {
    if (!open.value) return
    if (event.key === 'Escape') { event.preventDefault(); close() }
    if (event.key !== 'Tab') return
    const list = focusable(), first = list[0], last = list.at(-1)
    if (!first) { event.preventDefault(); panel.value?.focus(); return }
    if (event.shiftKey && (document.activeElement === first || !panel.value?.contains(document.activeElement))) { event.preventDefault(); last?.focus() }
    else if (!event.shiftKey && (document.activeElement === last || !panel.value?.contains(document.activeElement))) { event.preventDefault(); first.focus() }
  }
  watch(open, async value => {
    if (value) { previous = document.activeElement instanceof HTMLElement ? document.activeElement : null; document.addEventListener('keydown', keydown); await nextTick(); if (open.value) (focusable()[0] ?? panel.value)?.focus({ preventScroll: true }) }
    else { document.removeEventListener('keydown', keydown); await nextTick(); if (previous?.isConnected) previous.focus({ preventScroll: true }) }
  }, { immediate: true })
  onBeforeUnmount(() => document.removeEventListener('keydown', keydown))
}
