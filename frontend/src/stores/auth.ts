import { defineStore } from 'pinia'
import { ref } from 'vue'
import { acknowledgeSession, apiRequest, onUnauthorized } from '@/api/client'
import type { Session, User } from '@/types'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<User | null>(null)
  const ready = ref(false)
  onUnauthorized(() => { user.value = null; ready.value = false })
  let initialization: Promise<void> | null = null
  async function initialize(): Promise<void> {
    if (ready.value) return
    initialization ??= apiRequest<Session>('/api/auth/session').then(session => {
      user.value = session.user
      ready.value = true
      acknowledgeSession()
    }).finally(() => { initialization = null })
    await initialization
  }
  async function authenticate(mode: 'login' | 'register', email: string, password: string): Promise<void> {
    const session = await apiRequest<Session>(`/api/auth/${mode}`, { method: 'POST', body: JSON.stringify({ email, password }) })
    user.value = session.user
    ready.value = true
  }
  async function logout(): Promise<void> {
    const session = await apiRequest<Session>('/api/auth/logout', { method: 'POST', body: '{}' })
    user.value = session.user
    ready.value = true
  }
  return { user, ready, initialize, authenticate, logout }
})
