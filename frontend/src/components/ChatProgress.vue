<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { Progress } from '@/stores/workspace'
const props = defineProps<{ progress: Progress }>()
const active = computed(() => typeof props.progress.state === 'string' && ['queued', 'running'].includes(props.progress.state))
const expanded = ref(active.value)
const now = ref(Date.now())
let timer: ReturnType<typeof setInterval> | undefined
onMounted(() => { timer = setInterval(() => { if (!document.hidden && active.value) now.value = Date.now() }, 1000) })
onBeforeUnmount(() => clearInterval(timer))
watch(active, value => { expanded.value = value })
const heading = computed(() => {
  if (props.progress.state === 'queued') return '等待处理'
  const step = [...(props.progress.steps ?? [])].reverse().find(s => s.status === 'running')
  if (active.value) return step ? `正在${step.label}` : '正在整理回复'
  if (props.progress.state === 'superseded') {
    if (props.progress.reason === 'cancelled') return '已终止'
    if (props.progress.reason === 'superseded') return '已由新消息替代'
    return '处理已停止'
  }
  return ({ succeeded: '处理完成', failed: '本次处理未完成', needs_input: '等待你的补充' } as Record<string, string>)[props.progress.state ?? ''] || '执行过程'
})
function duration(ms = 0) { const seconds = Math.max(0, Math.floor(ms / 1000)); return seconds >= 60 ? `${Math.floor(seconds / 60)}分${String(seconds % 60).padStart(2, '0')}秒` : `${seconds}秒` }
const elapsed = computed(() => duration(active.value && props.progress.started_at ? now.value - props.progress.started_at : props.progress.duration_ms))
const statusLabel: Record<string, string> = { running: '进行中', succeeded: '完成', failed: '失败', unavailable: '暂无结果', stopped: '已停止' }
</script>
<template>
  <details class="chat-progress" :data-progress-state="progress.state" :open="expanded" @toggle="expanded = ($event.target as HTMLDetailsElement).open">
    <summary :aria-label="`${heading}，展开或收起执行过程`"><svg class="chat-progress-chevron" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="m5 3 5 5-5 5" /></svg><span class="chat-progress-heading">{{ heading }}</span><span class="chat-progress-time">{{ elapsed }}</span></summary>
    <ol v-if="progress.steps?.length" class="chat-progress-steps" aria-label="执行步骤">
      <li v-for="(step, index) in progress.steps" :key="step.id || index" class="chat-progress-step" :data-step-state="step.status">
        <svg class="chat-progress-icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true"><circle v-if="step.status === 'running'" class="chat-progress-spinner" cx="10" cy="10" r="6" stroke-dasharray="24 14"/><path v-else-if="step.status === 'succeeded'" d="m4 10 4 4 8-8"/><path v-else d="M10 4v7m0 4h.01"/></svg>
        <div class="chat-progress-copy"><span>{{ step.label }}</span><small v-if="step.detail">{{ step.detail }}</small></div><span class="chat-progress-step-status">{{ statusLabel[step.status ?? ''] || '已结束' }}<small v-if="step.status !== 'running'">{{ duration(step.duration_ms) }}</small></span>
      </li>
    </ol>
    <p class="chat-progress-footer">{{ active ? (progress.state === 'queued' ? '请求已收到，等待开始' : '执行中，步骤持续更新') : progress.state === 'failed' ? '执行未完成，可查看下方说明' : progress.state === 'superseded' ? '旧请求已停止更新' : '本轮执行过程' }}<span v-if="progress.omitted"> · 显示最近 {{ progress.steps?.length }} 个步骤</span></p>
  </details>
</template>
