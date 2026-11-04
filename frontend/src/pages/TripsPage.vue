<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useTripsStore } from '@/stores/trips'
import type { Trip } from '@/types'

const auth = useAuthStore()
const store = useTripsStore()
const router = useRouter()
const error = ref('')
const notice = ref('')
const deleting = ref('')
const loggingOut = ref(false)
async function load() {
  error.value = ''
  try { await store.load() } catch (cause) { error.value = cause instanceof Error ? cause.message : '旅行列表加载失败，请重试。' }
}
onMounted(load)
async function remove(trip: Trip) {
  if (deleting.value || !window.confirm(`确定删除「${trip.destination}」吗？聊天记录、想法和行程都会一并删除，无法恢复。`)) return
  deleting.value = trip.id
  error.value = ''
  try { await store.remove(trip.id); notice.value = '旅行及相关聊天、想法和行程已删除。' }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '删除失败，请重试。' }
  finally { deleting.value = '' }
}
async function logout() {
  loggingOut.value = true
  try { await auth.logout(); store.clear(); await router.replace('/') }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '退出失败，请重试。' }
  finally { loggingOut.value = false }
}
</script>

<template>
  <section class="page-stack">
    <header class="page-header">
      <div><p class="muted">我的旅行</p><h1>从住处出发，逛一逛这座城</h1></div>
      <div v-if="auth.user" class="user-chip"><span>{{ auth.user.initials }}</span><strong>{{ auth.user.name }}</strong><form @submit.prevent="logout"><button type="submit" :disabled="loggingOut">退出</button></form></div>
      <RouterLink class="icon-action" to="/trips/new" aria-label="新建旅行">+</RouterLink>
    </header>
    <p v-if="error" class="trip-notice trip-notice-error" role="alert">{{ error }} <button class="text-link" type="button" @click="load">重试</button></p>
    <p v-if="notice" class="trip-notice" role="status">{{ notice }}</p>
    <p v-if="store.loading" class="muted" role="status">正在加载旅行…</p>
    <div v-else-if="store.trips.length" class="trip-list">
      <div v-for="trip in store.trips" :key="trip.id" class="trip-card">
        <RouterLink class="trip-card-link" :to="`/workspace/${trip.id}`"><div class="trip-card-main"><strong>{{ trip.destination }}</strong><div class="trip-meta-row" aria-label="旅行概况"><span v-if="trip.mode === 'city'">同城游玩</span><span>{{ trip.date_range }}</span></div><small>{{ trip.last_activity }}</small></div></RouterLink>
        <div class="trip-card-side"><em>{{ trip.status }}</em><form @submit.prevent="remove(trip)"><button type="submit" class="delete-trip-button" :aria-label="`删除${trip.destination}`" :disabled="Boolean(deleting)">{{ deleting === trip.id ? '删除中…' : '删除' }}</button></form></div>
      </div>
    </div>
    <div v-else-if="!error" class="empty-state"><h2>还没有旅行</h2><p>先填写住宿、日期和出行方式，再和助手一起安排这趟旅行。</p><RouterLink class="primary-action compact" to="/trips/new">新建旅行</RouterLink></div>
  </section>
</template>
