<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const mode = computed(() => route.query.mode === 'register' ? 'register' : 'login')
const email = ref('')
const password = ref('')
const error = ref('')
const busy = ref(false)
async function submit() {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await auth.authenticate(mode.value, email.value, password.value)
    password.value = ''
    await router.replace('/trips')
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '操作失败，请重试。' }
  finally { busy.value = false }
}
</script>

<template>
  <section class="login-screen auth-screen">
    <header class="brand-lockup">
      <div class="brand-mark">TA</div>
      <div><h1>TravelAssistant</h1><p>从住处出发，慢慢发现这座城。</p></div>
    </header>
    <section class="auth-idea-board" aria-hidden="true">
      <div class="auth-board-title"><span>同城灵感</span><strong>住处周边，走走看看</strong></div>
      <svg class="auth-route" viewBox="0 0 360 210"><path d="M18 162 C72 198, 92 46, 158 82 S248 192, 342 38" /><circle cx="18" cy="162" r="5" /><circle cx="342" cy="38" r="5" /></svg>
      <div class="auth-idea-note auth-idea-note--place"><strong>海边日落</strong><span>地点</span></div>
      <div class="auth-idea-note auth-idea-note--pace"><strong>慢慢逛</strong><span>节奏</span></div>
      <div class="auth-idea-note auth-idea-note--budget"><strong>住处出发</strong><span>起点</span></div>
    </section>
    <form class="login-card" :aria-busy="busy" @submit.prevent="submit">
      <nav class="auth-mode-switch" aria-label="账号操作">
        <RouterLink :class="{ active: mode === 'login' }" to="/">登录</RouterLink>
        <RouterLink :class="{ active: mode === 'register' }" to="/?mode=register">注册</RouterLink>
      </nav>
      <div class="auth-heading"><span>{{ mode === 'register' ? '新的旅程' : '继续旅程' }}</span><h2>{{ mode === 'register' ? '创建你的账号' : '欢迎回来' }}</h2><p>{{ mode === 'register' ? '用邮箱保存每一次出发。' : '你的旅行想法还在这里。' }}</p></div>
      <p v-if="error" class="auth-error" role="alert">{{ error }}</p>
      <label><span>邮箱</span><input v-model="email" type="email" name="email" placeholder="name@example.com" autocomplete="email" inputmode="email" maxlength="254" autofocus required /></label>
      <label><span>密码</span><input v-model="password" type="password" name="password" placeholder="至少 8 个字符" :autocomplete="mode === 'register' ? 'new-password' : 'current-password'" minlength="8" maxlength="72" required /></label>
      <button class="primary-action button-reset" type="submit" :disabled="busy">{{ busy ? '正在提交…' : mode === 'register' ? '创建账号' : '登录' }}</button>
    </form>
  </section>
</template>
