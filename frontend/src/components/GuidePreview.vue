<script setup lang="ts">
import { computed, ref, toRef } from 'vue'
import type { WorkspaceSnapshot } from '@/stores/workspace'
import { useFocusDialog } from '@/composables/useFocusDialog'
import SafeMarkdown from './SafeMarkdown'
const props = defineProps<{ open: boolean; snapshot: WorkspaceSnapshot; busy: boolean; feedback: string }>()
const emit = defineEmits<{ close: [] }>()
const panel = ref<HTMLElement | null>(null)
useFocusDialog(toRef(props, 'open'), panel, () => emit('close'))
const plan = computed(() => props.snapshot.plan), state = computed(() => props.snapshot.display_state)
const guideUrl = computed(() => {
  if (!state.value.guide_available) return ''
  const query = new URLSearchParams({ plan_id: state.value.plan_id, source_hash: state.value.source_hash, version: String(state.value.guide_version) })
  return `/api/trips/${encodeURIComponent(props.snapshot.trip.id)}/guide?${query}`
})
</script>
<template>
  <aside id="guide-preview" ref="panel" class="guide-preview" :data-open="String(open)" :role="open ? 'dialog' : undefined" :aria-modal="open ? true : undefined" aria-labelledby="guide-title" tabindex="-1">
    <header class="guide-heading"><button class="guide-close" type="button" aria-label="关闭攻略预览，返回聊天" @click="emit('close')">返回聊天</button><h2 id="guide-title">旅行攻略</h2><span class="guide-download-slot"><a v-if="guideUrl" class="guide-download" :href="`${guideUrl}&download=true`" download aria-label="下载当前攻略 HTML"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12m-5-5 5 5 5-5M4 15v5h16v-5"/></svg><span>下载</span></a></span></header>
    <section class="guide-content"><div class="guide-panel">
      <template v-if="plan.text">
        <div class="guide-toolbar"><span class="guide-plan-status">{{ plan.status }}</span><a v-if="guideUrl" :href="guideUrl" target="_blank" rel="noopener noreferrer">打开网页</a></div>
        <p v-if="state.basis_status === 'stale'" class="guide-notice" role="status">住宿或需求已更新，请在聊天中更新行程。</p>
        <p v-else-if="state.basis_status === 'unknown'" class="guide-notice" role="status"><span>住宿依据尚未确认，请在聊天中说明住宿全名和地址。</span></p>
        <p v-if="feedback" class="guide-notice" role="status">{{ feedback }}</p>
        <p v-if="state.display_status === 'pending'" class="guide-notice" role="status">正在生成攻略网页。{{ guideUrl ? '上一版仍可查看。' : '可以先查看行程文字。' }}</p>
        <div v-else-if="state.display_status === 'fallback' || !guideUrl" class="guide-notice" role="status"><span>{{ state.display_status === 'fallback' ? `新版网页未生成，${guideUrl ? '上一版仍可查看。' : '完整行程已保留。'}` : '行程已保存，可以生成攻略网页。' }}</span><p>需要生成或重试时，请在聊天中告诉助手。</p></div>
        <!-- Stable sibling: unchanged URL keeps the live browsing context during polling. -->
        <iframe v-if="guideUrl" :key="guideUrl" class="guide-frame" title="旅行攻略网页" :src="guideUrl" sandbox="allow-popups allow-popups-to-escape-sandbox" referrerpolicy="no-referrer" />
        <details :key="`${plan.id}:${state.source_hash}`" class="guide-source" :open="!guideUrl"><summary>查看完整行程文字</summary><article data-itinerary-text><SafeMarkdown :text="plan.text" /></article></details>
      </template>
      <template v-else-if="plan.days?.length">
        <div class="guide-notice">这是此前保存的行程，完整安排仍可阅读。可在聊天中说明需要如何调整。</div>
        <article class="guide-legacy" data-itinerary-text><h2>{{ plan.title }}</h2><p v-if="plan.overview">{{ plan.overview }}</p>
          <section v-for="(day, index) in plan.days" :key="day.id || index"><h3>{{ day.date || day.label }}<template v-if="day.theme"> · {{ day.theme }}</template></h3>
            <template v-for="(item, itemIndex) in day.items" :key="item.id || itemIndex"><h4>{{ item.time }} {{ item.title }}</h4><p>{{ item.location }}<template v-if="item.duration"> · {{ item.duration }}</template></p><p v-if="item.reason">{{ item.reason }}</p><p v-if="item.notes">{{ item.notes }}</p><p v-if="item.meal">{{ item.meal.meal_type }}：{{ item.meal.recommendation }} · {{ item.meal.cuisine }} · {{ item.meal.budget }} · {{ item.meal.reservation }}</p><template v-for="(route, routeIndex) in [item.incoming_route, item.departure_route]" :key="routeIndex"><p v-if="route">{{ route.origin_name }} → {{ route.destination_name }}<template v-if="route.duration_minutes != null"> · 查询估算 {{ route.duration_minutes }} 分钟</template> · {{ route.reason }}</p></template></template>
          </section>
          <template v-for="(group, label) in { clothing: '衣物与装备', accommodation: '住宿' }" :key="label"><h3 v-if="plan.preparation?.[label]?.length">{{ group }}</h3><p v-for="(item, index) in plan.preparation?.[label]" :key="index">{{ item.title }} · {{ item.body }} · {{ item.status }}</p></template>
          <template v-for="(label, key) in { pending_items: '待核实', risks: '出行提醒' }" :key="key"><h3 v-if="plan[key]?.length">{{ label }}</h3><ul v-if="plan[key]?.length"><li v-for="(text, index) in plan[key]" :key="index">{{ text }}</li></ul></template>
        </article>
      </template>
      <div v-else class="guide-empty"><svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M14 10h27l9 9v35H14zM40 10v11h10M22 30h20M22 37h16M22 44h11"/></svg><h3>下一段旅途，从这里开始</h3><p>告诉助手想去哪、怎么游玩。行程安排好后，攻略网页会出现在这里。</p><p class="guide-empty-hint">也可以在聊天中说：帮我生成行程。</p></div>
    </div></section>
  </aside>
</template>
