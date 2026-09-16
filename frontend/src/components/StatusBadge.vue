<script setup lang="ts">
defineProps<{ status: string }>()

const labels: Record<string, string> = {
  ready: '待预解析', ready_for_tts: '可开始 TTS', queued: '排队中', running: '处理中', pausing: '正在暂停', paused: '已暂停',
  interrupted: '已中断', completed: '已完成', cancelled: '已取消', failed: '失败', pending: '待处理',
  generating: '生成脚本', generated: '待审校', reviewing: '审校中', reviewed: '已审校', rendering: '渲染中', done: '已完成',
}
</script>

<template><span class="status" :class="`status-${status}`"><i />{{ labels[status] || status }}</span></template>
