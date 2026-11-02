<script setup lang="ts">
import { computed, ref } from 'vue'
import type { ChatMessage } from '@/stores/workspace'
import SafeMarkdown, { replyPreview } from './SafeMarkdown'
import ChatProgress from './ChatProgress.vue'
const props = defineProps<{ message: ChatMessage; previous?: ChatMessage; busy: boolean }>()
const emit = defineEmits<{ draft: [body: string]; recover: []; guide: []; board: []; generate: [] }>()
const disclosure = ref<HTMLDetailsElement | null>(null)
const reply = computed(() => replyPreview(props.message.body, props.message.agent_card?.kind))
const timeBreak = computed(() => {
  const current = props.message.created_at, previous = props.previous?.created_at
  if (!previous) return current
  const pattern = /^(\d{1,2}):(\d{2})$/; const a = current.match(pattern), b = previous.match(pattern)
  if (a && b) return ((Number(a[1]) * 60 + Number(a[2]) - Number(b[1]) * 60 - Number(b[2]) + 1440) % 1440 >= 15) ? current : ''
  return current !== previous ? current : ''
})
const actions = computed(() => {
  const result = (props.message.agent_card?.actions ?? []).filter((a, i, all) => ['itinerary'].includes(a.target) && all.findIndex(b => a.target === b.target) === i)
  if (['itinerary-draft', 'revision'].includes(props.message.agent_card?.kind || '') && !result.some(a => a.target === 'itinerary')) result.unshift({ target: 'itinerary', label: '查看当前行程' })
  return result
})
const supplemental = computed(() => { const normalize = (s: string) => s.replace(/\s+/g, '').replace(/[。.!！]+$/, ''); const seen = new Set<string>(); return (props.message.agent_card?.bullets ?? []).filter(b => { const k = normalize(b); if (!k || normalize(props.message.body).includes(k) || seen.has(k)) return false; seen.add(k); return true }) })
function collapse() { if (disclosure.value) { disclosure.value.open = false; disclosure.value.querySelector('summary')?.focus({ preventScroll: true }); disclosure.value.closest('article')?.scrollIntoView({ block: 'nearest' }) } }
</script>
<template>
  <article :class="['message', message.sender_type, { 'message--continued': previous?.sender_type === message.sender_type && !timeBreak, 'thinking-message': message.status === 'thinking' }]" :data-message-id="message.id" :data-status="message.status" :data-time-break="timeBreak || undefined" :aria-label="message.sender_type === 'user' ? '你的消息' : '旅行助手的消息'">
    <span v-if="message.sender_type !== 'user'" class="avatar" aria-hidden="true">✦</span>
    <div class="message-body">
      <ChatProgress v-if="message.sender_type !== 'user' && message.execution_progress?.state" :progress="message.execution_progress" />
      <div v-if="message.status === 'thinking' && !message.execution_progress?.state" class="chat-bubble chat-thinking" role="status">正在整理回复</div>
      <div v-else-if="message.sender_type === 'user'" class="chat-bubble chat-user-text">{{ message.body }}</div>
      <div v-else-if="message.status !== 'thinking'" :class="['chat-bubble', 'chat-reply', { 'chat-reply--failed': message.status === 'failed' }]">
        <details v-if="reply.long" ref="disclosure" class="chat-full-reply">
          <summary><SafeMarkdown class="chat-reply-preview" :text="reply.preview"/><span class="chat-expand-label">{{ reply.overview ? '展开完整行程' : '展开完整回复' }} <span aria-hidden="true">⌄</span></span></summary>
          <SafeMarkdown class="chat-reply-content" :text="message.body"/><button class="chat-collapse" type="button" @click="collapse">收起完整回复 ↑</button>
        </details>
        <SafeMarkdown v-else class="chat-reply-content" :text="message.body"/>
      </div>
      <details v-if="supplemental.length" class="chat-supplemental"><summary>补充信息</summary><ul><li v-for="bullet in supplemental" :key="bullet">{{ bullet }}</li></ul></details>
      <button v-if="message.status === 'send_failed'" class="chat-message-action" type="button" :disabled="busy" @click="emit('recover')">移回草稿</button>
      <button v-else-if="message.status === 'failed' && previous?.sender_type === 'user'" class="chat-message-action" type="button" @click="emit('draft', previous.body)">移回草稿</button>
      <div v-else-if="message.status !== 'thinking' && actions.length" class="chat-message-actions"><button v-for="action in actions" :key="action.target" class="chat-message-action" type="button" :disabled="action.target === 'generate' && busy" @click="action.target === 'itinerary' ? emit('guide') : action.target === 'board' ? emit('board') : emit('generate')">{{ action.target === 'itinerary' ? '查看当前行程' : action.label }}</button></div>
      <span v-if="message.status === 'sending'" class="chat-message-state">正在确认是否送达…</span>
    </div>
  </article>
</template>
