import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'auth', component: () => import('@/pages/AuthPage.vue'), meta: { title: '登录' } },
    { path: '/login', redirect: { name: 'auth' } },
    { path: '/register', redirect: { name: 'auth', query: { mode: 'register' } } },
    { path: '/trips', name: 'trips', component: () => import('@/pages/TripsPage.vue'), meta: { requiresAuth: true, title: '我的旅行' } },
    { path: '/trips/new', name: 'trip-new', component: () => import('@/pages/NewConversationPage.vue'), meta: { requiresAuth: true, title: '开始聊天' } },
    { path: '/workspace/:id/requirements', name: 'requirements', redirect: to => ({ name: 'workspace', params: { id: to.params.id } }) },
    { path: '/workspace/:id', name: 'workspace', component: () => import('@/pages/WorkspacePage.vue'), meta: { requiresAuth: true, title: '旅行' } },
    { path: '/error', name: 'error', component: () => import('@/pages/ErrorPage.vue'), meta: { title: '暂时无法打开' } },
    { path: '/:pathMatch(.*)*', name: 'not-found', component: () => import('@/pages/ErrorPage.vue'), meta: { title: '页面不存在' } },
  ],
  scrollBehavior(to, from, savedPosition) {
    if (to.name === 'workspace' && from.name === 'workspace' && to.params.id === from.params.id) return false
    return savedPosition ?? { top: 0 }
  },
})

router.beforeEach(async to => {
  const auth = useAuthStore()
  try { await auth.initialize() } catch {
    if (to.name !== 'error') return { name: 'error', query: { reason: 'network' } }
    return true
  }
  if (to.meta.requiresAuth && !auth.user) return { name: 'auth' }
  if (to.name === 'auth' && auth.user) return { name: 'trips' }
  return true
})
router.afterEach(to => { document.title = `${String(to.meta.title ?? 'TravelAssistant')} · TravelAssistant` })
