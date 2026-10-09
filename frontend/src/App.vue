<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { BookOpen, Library, Settings, X } from 'lucide-vue-next'

const toast = ref('')
let toastTimer = 0

function closeToast() {
  window.clearTimeout(toastTimer)
  toast.value = ''
}

function showError(event: Event) {
  const message = (event as CustomEvent<string>).detail
  if (!message || message === toast.value) return
  toast.value = message
  window.clearTimeout(toastTimer)
  toastTimer = window.setTimeout(closeToast, 5000)
}

onMounted(() => window.addEventListener('app-error', showError))
onBeforeUnmount(() => {
  window.removeEventListener('app-error', showError)
  window.clearTimeout(toastTimer)
})
</script>

<template>
  <div class="shell">
    <div v-if="toast" class="error-toast" role="alert" aria-live="assertive">
      <span>{{ toast }}</span>
      <button type="button" aria-label="关闭错误提示" @click="closeToast"><X :size="17" /></button>
    </div>
    <header class="topbar">
      <router-link class="brand" to="/" aria-label="Alexandria 项目">
        <span class="brand-mark"><BookOpen :size="20" /></span>
        <span>Alexandria</span>
      </router-link>
      <nav aria-label="主导航">
        <router-link to="/"><Library :size="17" />项目</router-link>
        <router-link to="/settings"><Settings :size="17" />设置</router-link>
      </nav>
    </header>
    <main><router-view /></main>
  </div>
</template>
