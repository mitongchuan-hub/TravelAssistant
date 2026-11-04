<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import workflowCss from './assets/workflow.css?url'
import guideCss from './assets/guide.css?url'

const route = useRoute()
const workspace = computed(() => route.name === 'workspace')
const chat = computed(() => workspace.value && route.query.tab !== 'board')
const settings = computed(() => ['trip-new', 'requirements'].includes(String(route.name)))
</script>

<template>
  <Teleport to="head">
    <link v-if="settings" rel="stylesheet" :href="workflowCss" />
    <link v-if="workspace" rel="stylesheet" :href="guideCss" />
  </Teleport>
  <main class="phone-shell" :class="{ 'phone-shell--chat': chat }">
    <RouterView :key="`${String(route.name)}:${String(route.params.id ?? '')}`" />
  </main>
</template>
