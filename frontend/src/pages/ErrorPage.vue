<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
const route = useRoute()
const missing = computed(() => route.name === 'not-found')
</script>

<template>
  <section class="page-stack error-screen">
    <header class="page-header"><RouterLink class="text-link" to="/trips">返回</RouterLink><div><p class="muted">{{ missing ? '404' : '连接失败' }}</p><h1>{{ missing ? '页面不存在' : '暂时无法打开' }}</h1></div></header>
    <section class="empty-state error-card"><h2>{{ missing ? '没有找到这个页面' : '暂时无法连接服务' }}</h2><p>{{ missing ? '链接可能已失效，请返回旅行列表继续。' : '请检查网络连接，稍后重试。' }}</p><RouterLink class="primary-action compact" to="/trips">返回我的旅行</RouterLink></section>
  </section>
</template>
