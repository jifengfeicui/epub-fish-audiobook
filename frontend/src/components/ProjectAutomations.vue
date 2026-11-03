<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { Pause, Play, RefreshCw, Trash2 } from 'lucide-vue-next'
import { api } from '../api/client'
import type { Chapter, ProjectAutomation } from '../types'

const props = defineProps<{ projectId: string; chapters: Chapter[] }>()
const plans = ref<ProjectAutomation[]>([])
const busy = ref(false)
const error = ref('')
const base = `/api/v1/projects/${props.projectId}/automations`
let timer = 0
let stopped = false

async function load() {
  window.clearTimeout(timer)
  try {
    const response = await api.get<{ automations: ProjectAutomation[] }>(base)
    if (stopped) return
    plans.value = response.automations
    error.value = ''
  } catch (reason) { error.value = (reason as Error).message }
  finally { if (!stopped) timer = window.setTimeout(() => { void load() }, 5000) }
}

async function control(plan: ProjectAutomation, remove = false) {
  busy.value = true
  try {
    if (remove) await api.delete(`${base}/${plan.id}`)
    else await api.patch(`${base}/${plan.id}`, { enabled: !plan.enabled })
    await load()
  } catch (reason) { error.value = (reason as Error).message }
  finally { busy.value = false }
}

function time(value: string) { return new Date(value).toLocaleString() }
const statuses: Record<string, string> = { running: '处理中', completed: '本轮已处理', failed: '失败', interrupted: '已中断' }
onMounted(load)
onBeforeUnmount(() => { stopped = true; window.clearTimeout(timer) })
</script>

<template>
  <section class="panel automation-panel" aria-label="项目定时任务">
    <div class="panel-heading"><div><h2>定时任务</h2><span class="muted">告诉 Agent 要做什么、多久执行一次，即可创建计划。</span></div><button class="icon-button" aria-label="刷新定时任务" @click="load"><RefreshCw :size="17" /></button></div>
    <p class="muted">到点会调用模型并记录在项目对话中。关闭网页仍会执行，后端服务需保持运行。</p>
    <p v-if="error" class="form-error" role="alert">{{ error }}</p>
    <div v-if="!plans.length" class="tab-empty muted">暂无定时任务。</div>
    <article v-for="plan in plans" :key="plan.id" class="automation-card">
      <div class="automation-heading"><strong>{{ plan.enabled ? '已启用' : '已暂停' }} · 每 {{ plan.interval_seconds / 60 }} 分钟</strong><div class="heading-actions"><button class="button secondary" :disabled="busy" :aria-label="`${plan.enabled ? '暂停' : '恢复'}计划 ${plan.id}`" @click="control(plan)"><Pause v-if="plan.enabled" :size="15" /><Play v-else :size="15" />{{ plan.enabled ? '暂停' : '恢复' }}</button><button class="icon-button" :disabled="busy" :aria-label="`删除计划 ${plan.id}`" @click="control(plan, true)"><Trash2 :size="16" /></button></div></div>
      <p class="automation-instruction">{{ plan.instruction }}</p>
      <p v-if="plan.enabled" class="muted">下次执行：{{ time(plan.next_run_at) }}</p>
      <p v-if="plan.selected_chapter_id" class="muted">目标章节：{{ chapters.find(chapter => chapter.id === plan.selected_chapter_id)?.title || '章节已变化' }}</p>
      <p v-if="plan.paused_reason" class="form-error">{{ plan.paused_reason }}</p>
      <div v-if="plan.last_run" class="automation-result"><small>最近运行 · {{ statuses[plan.last_run.status] }} · {{ time(plan.last_run.created_at) }}</small><p>{{ plan.last_run.output || '正在查询项目状态…' }}</p></div>
    </article>
    <small v-if="plans.length" class="muted">暂停或删除计划不取消已经提交的业务任务。</small>
  </section>
</template>

<style scoped>
.automation-panel > p { margin: 16px 20px; line-height: 1.7; }
.automation-panel > small { display: block; margin: 16px 20px; }
.automation-card { border: 1px solid var(--border, #e5e5e5); border-radius: 10px; padding: 16px; margin: 14px 20px; }
.automation-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.automation-instruction, .automation-result p { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.7; }
.automation-result { background: #f7f7f7; border-radius: 8px; padding: 12px; }
.automation-result p { margin-bottom: 0; }
</style>
