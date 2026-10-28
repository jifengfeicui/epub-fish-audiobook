<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../api/client'
import type { AgentAction, AgentRun, Chapter, ScriptEntry } from '../types'

const props = defineProps<{ projectId: string; chapters: Chapter[]; selectedChapterId: number | null; chapterTitle?: string; dirty: boolean; editable: boolean; saving: boolean }>()
const emit = defineEmits<{ changed: []; save: []; discard: [] }>()
const runs = ref<AgentRun[]>([])
const draft = ref('')
const allowRewrite = ref(false)
const sending = ref(false)
const deciding = ref(false)
const error = ref('')
const loading = ref(true)
const busy = computed(() => runs.value.some(run => run.status === 'running'))
const base = `/api/v1/projects/${props.projectId}/agent`
let timer = 0
let stopped = false
let submission: { signature: string; id: string } | undefined
const statusNames: Record<string, string> = { running: '处理中', completed: '本轮已处理', failed: '失败', interrupted: '已中断', pending: '待确认', applied: '已保存', rejected: '已拒绝', outdated: '已过期', executing: '正在提交' }

async function load() {
  window.clearTimeout(timer)
  try {
    const result = await api.get<{ runs: AgentRun[] }>(base)
    if (stopped) return
    const wasBusy = busy.value
    runs.value = result.runs
    error.value = ''
    if (wasBusy && !busy.value) emit('changed')
  } catch (reason) { error.value = (reason as Error).message }
  finally {
    loading.value = false
    if (!stopped && (busy.value || error.value)) timer = window.setTimeout(() => { void load() }, 1500)
  }
}

async function send() {
  if (!draft.value.trim() || props.dirty || busy.value || sending.value || loading.value) return
  const body = { message: draft.value.trim(), selected_chapter_id: props.selectedChapterId, allow_text_rewrite: allowRewrite.value }
  const signature = JSON.stringify(body)
  if (submission?.signature !== signature) submission = { signature, id: crypto.randomUUID() }
  sending.value = true
  error.value = ''
  try {
    const run = await api.post<AgentRun>(`${base}/messages`, { request_id: submission.id, ...body })
    if (stopped) return
    if (!runs.value.some(item => item.id === run.id)) runs.value.push(run)
    draft.value = ''
    allowRewrite.value = false
    submission = undefined
    await load()
  } catch (reason) { error.value = (reason as Error).message }
  finally { sending.value = false }
}

async function decide(action: AgentAction, approve: boolean) {
  if (deciding.value || (approve && (props.dirty || !props.editable))) return
  deciding.value = true
  try {
    await api.post(`${base}/proposals/${action.id}`, { approve })
    emit('changed')
    await load()
  } catch (reason) { await load(); error.value = (reason as Error).message }
  finally { deciding.value = false }
}

function differences(action: AgentAction) {
  const before = action.args.before || []
  const after = action.args.entries || []
  return Array.from({ length: Math.max(before.length, after.length) }, (_, index) => ({ index, before: before[index], after: after[index] }))
    .filter(row => JSON.stringify(row.before) !== JSON.stringify(row.after))
}
function content(entry?: ScriptEntry) {
  return entry ? `${entry.speaker}\n${entry.text}\n语气：${entry.instruct || '无'}` : '（无条目）'
}
onMounted(load)
onBeforeUnmount(() => { stopped = true; window.clearTimeout(timer) })
</script>

<template>
  <section class="agent-panel panel" aria-label="项目 Agent">
    <div class="panel-heading"><div><h2>项目 Agent</h2><span class="muted">任务直接提交 · 改稿预览后确认</span></div><button class="button secondary" :disabled="sending || deciding" @click="load">刷新对话</button></div>
    <div class="agent-body">
      <div v-if="dirty" class="agent-notice" role="alert">
        章节有未保存的人工编辑，请先保存或放弃，再发送指令或应用提案。
        <div class="heading-actions"><button class="button secondary" :disabled="saving || !editable" @click="emit('save')">保存人工编辑</button><button class="button secondary" :disabled="saving" @click="emit('discard')">放弃人工编辑</button></div>
      </div>
      <p v-if="loading" class="muted">正在读取对话…</p>
      <p v-else-if="!runs.length" class="muted">例如：“查询任务进度”“预处理第 1 到 3 章”“把当前章旁白的语气改得沉稳”。</p>
      <div class="agent-history" role="log" aria-label="Agent 对话记录" aria-live="polite">
        <article v-for="run in runs" :key="run.id" class="agent-turn">
          <p class="agent-user">{{ run.message }}</p>
          <span class="muted">{{ statusNames[run.status] || run.status }} · {{ new Date(run.created_at).toLocaleTimeString() }}</span>
          <p class="agent-answer">{{ run.output || '正在处理，请稍候…' }}</p>
          <div v-if="run.action" class="agent-action">
            <template v-if="run.action.kind === 'script'">
              <strong>脚本修改提案 · {{ statusNames[run.action.status] || run.action.status }}</strong>
              <p>{{ run.action.args.summary }}</p>
              <p class="muted">{{ chapters.find(chapter => chapter.id === run.action?.args.chapter_id)?.title || '章节已移除' }} · 基于修订 {{ run.action.args.expected_revision }} · {{ run.action.args.text_changed ? '包含正文修改' : '正文保持不变' }}</p>
              <div v-for="row in differences(run.action)" :key="row.index" class="agent-diff">
                <strong>条目 {{ row.index + 1 }}</strong><div class="agent-diff-columns"><div><small>修改前</small><pre>{{ content(row.before) }}</pre></div><div><small>修改后</small><pre>{{ content(row.after) }}</pre></div></div>
              </div>
              <template v-if="run.action.status === 'pending'">
                <p v-if="!editable" class="muted">请先暂停任务，等待暂停完成后再应用提案。</p>
                <div class="heading-actions"><button class="button secondary" :disabled="deciding || run.status === 'running'" @click="decide(run.action, false)">拒绝修改</button><button class="button primary" :disabled="dirty || !editable || deciding || run.status === 'running'" @click="decide(run.action, true)">确认保存脚本</button></div>
              </template>
            </template>
            <template v-else><strong>{{ run.action.status === 'completed' ? '操作已提交，实际进度见项目面板' : statusNames[run.action.status] || run.action.status }}</strong><p v-if="run.action.result.id">任务编号：{{ run.action.result.id }}</p><p v-if="run.action.result.message">{{ run.action.result.message }}</p></template>
          </div>
        </article>
      </div>
      <form class="agent-compose" @submit.prevent="send">
        <label><span>给 Agent 的指令</span><textarea v-model="draft" aria-label="给 Agent 的指令" maxlength="8000" rows="3" placeholder="输入自然语言指令…" :disabled="sending || busy" /></label>
        <div class="agent-options"><span class="muted">当前章：{{ chapterTitle || '未选择' }}</span><label><input v-model="allowRewrite" type="checkbox" :disabled="sending || busy" />本条指令允许改写正文</label></div>
        <p v-if="error" role="alert" class="agent-error">{{ error }}</p>
        <button class="button primary" :disabled="loading || dirty || sending || busy || !draft.trim()">{{ busy ? 'Agent 处理中…' : sending ? '提交中…' : '发送指令' }}</button>
      </form>
    </div>
  </section>
</template>

<style scoped>
.agent-body { padding: 18px; }.agent-history { max-height: 65vh; overflow-y: auto; }.agent-turn { border-bottom: 1px solid #dce6df; padding: 12px 0 20px; }.agent-user { font-weight: 700; color: #294438; white-space: pre-wrap; }.agent-answer { white-space: pre-wrap; line-height: 1.7; }.agent-action { margin-top: 12px; padding: 14px; background: #f5f8f5; border: 1px solid #dce6df; border-radius: 6px; overflow-wrap: anywhere; }.agent-diff { margin: 12px 0; }.agent-diff-columns { display: grid; grid-template-columns: minmax(0,1fr) minmax(0,1fr); gap: 12px; margin-top: 7px; }.agent-diff-columns > div { min-width: 0; padding: 10px; background: white; border: 1px solid #dce6df; border-radius: 4px; }pre { white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; line-height: 1.6; }.agent-compose { display: grid; gap: 12px; margin-top: 20px; }.agent-compose textarea { width: 100%; min-height: 90px; resize: vertical; padding: 12px; border: 1px solid #afc5b5; border-radius: 5px; font: inherit; }.agent-options { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 12px; }.agent-options label { display: flex; align-items: center; gap: 7px; font-size: 12px; }.agent-notice { padding: 14px; background: #fff3dc; border: 1px solid #dec28c; border-radius: 5px; line-height: 1.7; }.agent-notice .heading-actions { margin-top: 10px; }.agent-error { color: #a04c4c; white-space: pre-wrap; }@media(max-width: 650px) { .agent-diff-columns { grid-template-columns: 1fr; }.agent-body { padding: 12px; } }
</style>
