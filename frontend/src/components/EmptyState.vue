<script setup lang="ts">
import { Library } from 'lucide-vue-next'
defineProps<{ title: string; detail: string }>()
</script>

<template>
  <div class="empty-state">
    <Library :size="32" stroke-width="1.5" />
    <strong>{{ title }}</strong>
    <span>{{ detail }}</span>
    <slot />
  </div>
</template>
