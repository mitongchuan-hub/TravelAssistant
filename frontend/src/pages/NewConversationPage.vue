<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { apiRequest } from '@/api/client'
import type { Trip } from '@/types'
const router = useRouter()
const error = ref(''), busy = ref(false)
async function start() {
  if (busy.value) return
  busy.value = true; error.value = ''
  try {
    const { trip } = await apiRequest<{ trip: Trip }>('/api/trips/conversations', { method: 'POST' })
    await router.replace(`/workspace/${encodeURIComponent(trip.id)}`)
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '创建对话失败，请重试。' }
  finally { busy.value = false }
}
onMounted(start)
</script>
<template><section class="page-stack"><p role="status">{{ error || '正在打开新的对话…' }}</p><button v-if="error" type="button" :disabled="busy" @click="start">重试</button><RouterLink to="/trips">返回我的旅行</RouterLink></section></template>
