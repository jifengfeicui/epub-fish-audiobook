<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ArrowLeft, Download, Pause, Play, RefreshCw, Save, Wifi, WifiOff, XCircle } from 'lucide-vue-next'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api/client'
import { connectProjectEvents } from '../api/events'
import { useProjectsStore } from '../stores/projects'
import StatusBadge from '../components/StatusBadge.vue'
import type { JobEvent, Script } from '../types'

const route = useRoute()
const router = useRouter()
const store = useProjectsStore()
const selectedId = ref<number | null>(null)
const script = ref<Script | null>(null)
const saving = ref(false)
const message = ref('')
const connected = ref(false)
const mode = ref<'after_review_batch' | 'after_all_reviews'>('after_review_batch')
const batchSize = ref(3)
const fromChapter = ref<number | '' | null>(null)
const toChapter = ref<number | '' | null>(null)
let disconnect: (() => void) | undefined
let refreshTimer = 0
const projectId = computed(() => String(route.params.id))
const job = computed(() => store.current?.latest_job)
const activeChapter = computed(() => store.chapters.find(chapter => chapter.id === selectedId.value))
const running = computed(() => ['queued', 'running', 'pausing'].includes(job.value?.status || ''))
const scriptEditable = computed(() => ['paused', 'completed'].includes(job.value?.status || ''))
const pipelineSettingsValid = computed(() => {
  const start = fromChapter.value === '' || fromChapter.value === null ? 1 : fromChapter.value
  const end = toChapter.value === '' || toChapter.value === null ? store.chapters.length : toChapter.value
  return batchSize.value >= 1 && batchSize.value <= 20 && start >= 1 && end >= start && end <= store.chapters.length
})
const selectedChapters = computed(() => {
  const start = store.current?.settings.from_chapter || 1
  const end = store.current?.settings.to_chapter || store.chapters.length
  return store.chapters.filter(chapter => chapter.position >= start && chapter.position <= end)
})
const progress = computed(() => {
  const total = selectedChapters.value.length || 1
  const complete = selectedChapters.value.filter(chapter => chapter.status === 'done').length
  return Math.round((complete / total) * 100)
})

async function refresh() {
  await store.loadProject(projectId.value)
  if (store.current) {
    mode.value = store.current.settings.render_start_mode
    batchSize.value = store.current.settings.release_batch_size
    fromChapter.value = store.current.settings.from_chapter
    toChapter.value = store.current.settings.to_chapter
  }
  if (selectedId.value === null && store.chapters[0]) await selectChapter(store.chapters[0].id)
}
async function selectChapter(id: number) {
  selectedId.value = id
  script.value = await api.get<Script>(`/api/v1/chapters/${id}/script`)
}
async function startJob() { await api.post(`/api/v1/projects/${projectId.value}/jobs`); await refresh() }
async function control(action: 'pause' | 'resume' | 'retry' | 'cancel') { if (job.value) { await api.post(`/api/v1/jobs/${job.value.id}/${action}`); await refresh() } }
async function savePipelineSettings() {
  message.value = ''
  try {
    await api.patch(`/api/v1/projects/${projectId.value}`, {
      render_start_mode: mode.value,
      release_batch_size: batchSize.value,
      from_chapter: fromChapter.value === '' ? null : fromChapter.value,
      to_chapter: toChapter.value === '' ? null : toChapter.value,
    })
    message.value = mode.value === 'after_review_batch'
      ? `已设置为每 ${batchSize.value} 章 Review 后开始渲染`
      : '已设置为整本 Review 完成后开始渲染'
    await refresh()
  } catch (reason) {
    message.value = (reason as Error).message
  }
}
async function saveScript() {
  if (!script.value || !activeChapter.value) return
  saving.value = true; message.value = ''
  try { script.value = await api.patch<Script>(`/api/v1/chapters/${activeChapter.value.id}/script`, { expected_revision: script.value.revision, entries: script.value.entries.map(({ speaker, text, instruct }) => ({ speaker, text, instruct })) }); message.value = '脚本已保存，章节音频已标记为待重渲染'; await refresh() }
  catch (reason) { message.value = (reason as Error).message }
  finally { saving.value = false }
}
function addEntry() { script.value?.entries.push({ speaker: 'NARRATOR', text: '', instruct: '' }) }
function removeEntry(index: number) { script.value?.entries.splice(index, 1) }
function eventReceived(event: JobEvent) {
  store.receiveEvent(event)
  window.clearTimeout(refreshTimer)
  refreshTimer = window.setTimeout(() => { void refresh() }, 180)
}
onMounted(async () => { await refresh(); disconnect = connectProjectEvents(projectId.value, store.latestEventId, eventReceived, value => { connected.value = value }) })
onBeforeUnmount(() => { window.clearTimeout(refreshTimer); disconnect?.() })
</script>

<template>
  <section v-if="store.current" class="page project-page">
    <button class="back-link" @click="router.push('/')"><ArrowLeft :size="17" />返回项目</button>
    <div class="project-heading"><div><span class="eyebrow">{{ store.current.source_filename }}</span><h1>{{ store.current.title }}</h1><div class="connection"><component :is="connected ? Wifi : WifiOff" :size="14" />{{ connected ? '实时进度已连接' : '正在重连进度' }}</div></div><div class="heading-actions"><button v-if="job?.status === 'paused' && job.error" class="button secondary" @click="control('retry')"><RefreshCw :size="17" />重试</button><button v-else-if="job?.status === 'paused' || job?.status === 'interrupted'" class="button secondary" @click="control('resume')"><Play :size="17" />继续</button><button v-else-if="running" class="button secondary" @click="control('pause')"><Pause :size="17" />暂停</button><button v-else class="button primary" @click="startJob"><Play :size="17" />开始流水线</button><button v-if="job && !['completed', 'cancelled'].includes(job.status)" class="button danger" @click="control('cancel')"><XCircle :size="17" />取消</button></div></div>
    <div class="progress-panel"><div class="progress-copy"><div><strong>{{ progress }}%</strong><span>所选章节进度</span></div><StatusBadge :status="job?.status || store.current.status" /></div><div class="progress-track"><span :style="{ width: `${progress}%` }" /></div><div class="pipeline-legend"><span><i class="dot review" />审校：{{ selectedChapters.filter(c => ['reviewed', 'rendering', 'done'].includes(c.status)).length }}/{{ selectedChapters.length }}</span><span><i class="dot audio" />音频：{{ selectedChapters.filter(c => c.status === 'done').length }}/{{ selectedChapters.length }}</span><span>{{ store.current.settings.render_start_mode === 'after_review_batch' ? `每 ${store.current.settings.release_batch_size} 章释放` : '整本审校后释放' }}</span></div><div class="pipeline-settings"><label>渲染开始<select v-model="mode" :disabled="running"><option value="after_review_batch">每批 Review 后开始</option><option value="after_all_reviews">整本 Review 后开始</option></select></label><label v-if="mode === 'after_review_batch'">每批章节数<input v-model.number="batchSize" type="number" min="1" max="20" :disabled="running" /></label><label>起始章节<input v-model.number="fromChapter" type="number" min="1" :max="store.chapters.length" placeholder="第 1 章" :disabled="running" /></label><label>结束章节<input v-model.number="toChapter" type="number" min="1" :max="store.chapters.length" placeholder="最后一章" :disabled="running" /></label><button class="button secondary" :disabled="running || !pipelineSettingsValid" @click="savePipelineSettings"><Save :size="15" />保存流水线设置</button></div></div>
    <p v-if="job?.error" class="form-error">{{ job.error }}</p><p v-if="message" class="inline-message">{{ message }}</p>
    <div class="workspace-grid">
      <aside class="chapter-list panel"><div class="panel-heading"><h2>章节</h2><span>{{ store.chapters.length }}</span></div><button v-for="chapter in store.chapters" :key="chapter.id" class="chapter-item" :class="{ selected: selectedId === chapter.id }" @click="selectChapter(chapter.id)"><span class="chapter-number">{{ String(chapter.position).padStart(2, '0') }}</span><span class="chapter-title">{{ chapter.title }}</span><StatusBadge :status="chapter.status" /></button></aside>
      <section class="script-panel panel"><div class="panel-heading"><div><h2>{{ activeChapter?.title || '选择章节' }}</h2><span v-if="script" class="muted">{{ script.kind || '尚未生成' }} · 修订 {{ script.revision }}</span></div><div v-if="script" class="heading-actions"><button class="icon-button" title="添加条目" :disabled="!scriptEditable" @click="addEntry">＋</button><button class="button secondary" :disabled="saving || !scriptEditable" @click="saveScript"><Save :size="16" />{{ saving ? '保存中' : '保存脚本' }}</button></div></div><div v-if="script" class="entry-list"><div v-for="(entry, index) in script.entries" :key="entry.id || index" class="entry-row"><span class="entry-index">{{ String(index + 1).padStart(3, '0') }}</span><div class="entry-fields"><input v-model="entry.speaker" aria-label="说话人" :disabled="!scriptEditable" /><textarea v-model="entry.text" rows="2" aria-label="文本" :disabled="!scriptEditable" /><input v-model="entry.instruct" placeholder="Fish voice direction" aria-label="语气指令" :disabled="!scriptEditable" /></div><button class="remove-entry" title="删除条目" :disabled="!scriptEditable" @click="removeEntry(index)">×</button></div></div><div v-else class="empty-script">选择章节查看脚本</div></section>
    </div>
    <section class="assignment-panel panel"><div class="panel-heading"><h2>角色音色绑定</h2><span class="muted">新角色首次渲染时自动固定</span></div><div v-if="store.speakerAssignments.length" class="assignment-list"><span v-for="assignment in store.speakerAssignments" :key="assignment.speaker"><strong>{{ assignment.speaker }}</strong><small>{{ assignment.voice_name }}</small></span></div><div v-else class="muted assignment-empty">尚未分配角色音色。</div></section>
    <section class="artifact-panel panel"><div class="panel-heading"><h2>产物</h2><span class="muted">完成的章节可立即下载</span></div><div v-if="store.artifacts.length" class="artifact-list"><a v-for="artifact in store.artifacts" :key="artifact.id" class="artifact" :href="`/api/v1/artifacts/${artifact.id}/download`"><span><strong>{{ artifact.kind === 'book_mp3' ? '整书 MP3' : `第 ${store.chapters.find(c => c.id === artifact.chapter_id)?.position || ''} 章` }}</strong><small>{{ (artifact.size / 1024 / 1024).toFixed(1) }} MB</small></span><Download :size="17" /></a></div><div v-else class="muted">还没有可下载的音频产物。</div></section>
    <section class="log-panel panel"><div class="panel-heading"><h2>活动日志</h2><button class="icon-button" title="刷新" @click="refresh"><RefreshCw :size="16" /></button></div><div class="log-list"><p v-for="event in store.events.slice(-80).reverse()" :key="event.event_id"><time>{{ new Date(event.timestamp).toLocaleTimeString() }}</time><strong>{{ event.type }}</strong><span>{{ String(event.payload.message || event.payload.position || '') }}</span></p><p v-if="!store.events.length" class="muted">任务启动后，阶段日志会显示在这里。</p></div></section>
  </section>
</template>
