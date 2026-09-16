<script setup lang="ts">
import { BookOpen, Library, Settings } from 'lucide-vue-next'
</script>

<template>
  <div class="shell">
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
