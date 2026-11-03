<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { ArrowUp, BookOpenText, Grip, MessageSquare, Pencil, Plus, RefreshCw, Sparkles, X } from 'lucide-vue-next'
import { api } from '../api/client'
import type { AgentAction, AgentConversation, AgentRun, Chapter, ScriptChange, ScriptEntry } from '../types'

const props = defineProps<{ projectId: string; chapters: Chapter[]; selectedChapterId: number | null; chapterTitle?: string; dirty: boolean; editable: boolean; saving: boolean }>()
const emit = defineEmits<{ changed: []; save: []; discard: []; deleted: [] }>()
const runs = ref<AgentRun[]>([])
const conversations = ref<AgentConversation[]>([])
const selectionKey = `alexandria-agent-conversation:${props.projectId}`
const conversationId = ref(localStorage.getItem(selectionKey) || '')
const projectBusy = ref(false)
const conversationSaving = ref(false)
const renaming = ref(false)
const conversationTitle = ref('')
const drafts = new Map<string, string>()
const draft = ref('')
const sending = ref(false)
const deciding = ref(false)
const error = ref('')
const loading = ref(true)
const opened = ref(false)
const launcher = ref<HTMLButtonElement | null>(null)
const composer = ref<HTMLTextAreaElement | null>(null)
const history = ref<HTMLElement | null>(null)
const followLatest = ref(true)
const bounds = ref({ x: 0, y: 0, width: 460, height: 640 })
const windowStyle = computed(() => ({ left: `${bounds.value.x}px`, top: `${bounds.value.y}px`, width: `${bounds.value.width}px`, height: `${bounds.value.height}px` }))
let initialized = false
let gesture: { x: number; y: number; bounds: typeof bounds.value; resize: boolean } | undefined
const busy = computed(() => projectBusy.value || runs.value.some(run => run.status === 'running'))
const base = `/api/v1/projects/${props.projectId}/agent`
let timer = 0
let stopped = false
let loadVersion = 0
let submission: { signature: string; id: string } | undefined
const statusNames: Record<string, string> = { running: '处理中', completed: '本轮已处理', failed: '失败', interrupted: '已中断', pending: '待确认', applied: '已保存', rejected: '已拒绝', outdated: '已过期', executing: '正在提交' }
const operationNames: Record<string, string> = { task: '创建任务', control: '控制任务', project: '修改项目', chapters: '选择章节', characters: '修改角色', characters_refresh: '更新角色池', voice_pool: '修改项目音色池', automation_create: '创建定时任务', automation_update: '修改定时任务', automation_pause: '暂停定时任务', automation_delete: '删除定时任务', reparse: '重新识别章节' }

function fitWindow() {
  const b = bounds.value
  b.width = Math.min(Math.max(360, b.width), window.innerWidth - 24)
  b.height = Math.min(Math.max(360, b.height), window.innerHeight - 24)
  b.x = Math.max(12, Math.min(b.x, window.innerWidth - b.width - 12))
  b.y = Math.max(12, Math.min(b.y, window.innerHeight - b.height - 12))
}

async function toggle() {
  opened.value = !opened.value
  if (!opened.value) { launcher.value?.focus(); return }
  if (!initialized) {
    initialized = true
    bounds.value.x = window.innerWidth - 484
    bounds.value.y = window.innerHeight - 720
    fitWindow()
  }
  void load()
  await nextTick()
  if (followLatest.value && history.value) history.value.scrollTop = history.value.scrollHeight
  composer.value?.focus({ preventScroll: true })
}

function startGesture(event: PointerEvent, resize = false) {
  if (event.button !== 0 || window.innerWidth <= 600 || (!resize && (event.target as HTMLElement).closest('button'))) return
  gesture = { x: event.clientX, y: event.clientY, bounds: { ...bounds.value }, resize }
  ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
  event.preventDefault()
}

function moveGesture(event: PointerEvent) {
  if (!gesture) return
  const dx = event.clientX - gesture.x, dy = event.clientY - gesture.y
  const b = gesture.bounds
  bounds.value = gesture.resize ? { ...b, width: Math.min(b.width + dx, window.innerWidth - b.x - 12), height: Math.min(b.height + dy, window.innerHeight - b.y - 12) }
    : { ...b, x: b.x + dx, y: b.y + dy }
  fitWindow()
}

function adjustWithKeys(event: KeyboardEvent, resize = false) {
  if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return
  event.preventDefault()
  const dx = event.key === 'ArrowLeft' ? -20 : event.key === 'ArrowRight' ? 20 : 0
  const dy = event.key === 'ArrowUp' ? -20 : event.key === 'ArrowDown' ? 20 : 0
  if (resize) { bounds.value.width += dx; bounds.value.height += dy }
  else { bounds.value.x += dx; bounds.value.y += dy }
  fitWindow()
}

function trackScroll() {
  if (history.value) followLatest.value = history.value.scrollHeight - history.value.scrollTop - history.value.clientHeight < 48
}

function composeKey(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault()
    void send()
  }
}

async function load() {
  window.clearTimeout(timer)
  const version = ++loadVersion
  const selected = conversationId.value
  try {
    const result = await api.get<{ runs: AgentRun[]; conversations: AgentConversation[]; conversation_id: string; project_busy: boolean }>(selected ? `${base}?conversation_id=${encodeURIComponent(selected)}` : base, { silentNotFound: true })
    if (stopped || version !== loadVersion) return
    const changed = JSON.stringify(result.runs) !== JSON.stringify(runs.value)
    runs.value = result.runs
    conversations.value = result.conversations
    conversationId.value = result.conversation_id
    projectBusy.value = result.project_busy
    localStorage.setItem(selectionKey, result.conversation_id)
    error.value = ''
    await nextTick()
    if (opened.value && followLatest.value && history.value) history.value.scrollTop = history.value.scrollHeight
    if (changed && runs.value.some(run => run.status !== 'running')) emit('changed')
  } catch (reason) {
    if (stopped || version !== loadVersion) return
    if ((reason as Error & { status?: number }).status === 404) {
      if (selected) { conversationId.value = ''; await load(); return }
      stopped = true; emit('deleted'); return
    }
    error.value = (reason as Error).message
  }
  finally {
    if (version === loadVersion) {
      loading.value = false
      if (!stopped && (opened.value || busy.value || error.value)) timer = window.setTimeout(() => { void load() }, 1500)
    }
  }
}

async function switchConversation(id: string) {
  drafts.set(conversationId.value, draft.value)
  conversationId.value = id
  draft.value = drafts.get(id) || ''
  runs.value = []
  loading.value = true
  followLatest.value = true
  renaming.value = false
  submission = undefined
  await load()
}

async function newConversation() {
  if (sending.value || deciding.value || conversationSaving.value) return
  conversationSaving.value = true
  try {
    const conversation = await api.post<AgentConversation>(`${base}/conversations`, { title: '新对话' })
    await switchConversation(conversation.id)
    composer.value?.focus()
  } catch (reason) { error.value = (reason as Error).message }
  finally { conversationSaving.value = false }
}

async function renameConversation() {
  if (!conversationTitle.value.trim() || conversationSaving.value) return
  conversationSaving.value = true
  try {
    await api.patch(`${base}/conversations/${conversationId.value}`, { title: conversationTitle.value.trim() })
    renaming.value = false
    await load()
  } catch (reason) { error.value = (reason as Error).message }
  finally { conversationSaving.value = false }
}

async function send() {
  if (!draft.value.trim() || props.dirty || busy.value || sending.value || loading.value) return
  const body = { conversation_id: conversationId.value, message: draft.value.trim(), selected_chapter_id: props.selectedChapterId }
  const signature = JSON.stringify(body)
  if (submission?.signature !== signature) submission = { signature, id: crypto.randomUUID() }
  sending.value = true
  error.value = ''
  try {
    const run = await api.post<AgentRun>(`${base}/messages`, { request_id: submission.id, ...body })
    if (stopped) return
    if (!runs.value.some(item => item.id === run.id)) runs.value.push(run)
    draft.value = ''
    drafts.delete(conversationId.value)
    submission = undefined
    if (run.status !== 'running') emit('changed')
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

function differences(change: ScriptChange) {
  const before = change.before || []
  const after = change.entries || []
  return Array.from({ length: Math.max(before.length, after.length) }, (_, index) => ({ index, before: before[index], after: after[index] }))
    .filter(row => JSON.stringify(row.before) !== JSON.stringify(row.after))
}
function content(entry?: ScriptEntry) {
  return entry ? `${entry.speaker}\n${entry.text}\n语气：${entry.instruct || '无'}` : '（无条目）'
}
onMounted(() => window.addEventListener('resize', fitWindow))
onBeforeUnmount(() => { stopped = true; window.clearTimeout(timer); window.removeEventListener('resize', fitWindow) })
</script>

<template>
  <Teleport to="body">
    <button ref="launcher" class="agent-launcher" :aria-expanded="opened" aria-controls="project-agent-window" aria-label="Agent 助手" @click="toggle">
      <MessageSquare :size="19" /><span>Agent 助手</span><i v-if="busy" class="agent-live-dot" /><span v-else-if="runs.some(run => run.action?.status === 'pending')" class="pending-dot" />
    </button>
    <section v-show="opened" id="project-agent-window" class="agent-window" :style="windowStyle" role="dialog" aria-label="项目 Agent" @keydown.esc.stop="opened && toggle()">
      <header class="agent-header" @pointerdown="startGesture($event)" @pointermove="moveGesture" @pointerup="gesture = undefined" @lostpointercapture="gesture = undefined">
        <div class="agent-title" tabindex="0" aria-label="移动 Agent 窗口，使用方向键" @keydown="adjustWithKeys($event)"><span class="agent-symbol"><Sparkles :size="18" /></span><div><h2>Agent 助手</h2><small>{{ busy ? '正在处理你的请求' : '一起完成这本有声书' }}</small></div></div>
        <div class="agent-header-actions"><button class="agent-icon" aria-label="刷新对话" :disabled="sending || deciding" @click="load"><RefreshCw :size="16" /></button><button class="agent-icon" aria-label="收起 Agent" @click="toggle"><X :size="19" /></button></div>
      </header>
      <div class="agent-conversations">
        <select :value="conversationId" aria-label="会话列表" :disabled="sending || deciding || conversationSaving" @change="switchConversation(($event.target as HTMLSelectElement).value)">
          <option v-for="conversation in conversations" :key="conversation.id" :value="conversation.id">{{ conversation.title }}</option>
        </select>
        <button class="agent-icon" aria-label="重命名对话" :disabled="!conversationId || conversationSaving" @click="renaming = !renaming; conversationTitle = conversations.find(item => item.id === conversationId)?.title || ''"><Pencil :size="14" /></button>
        <button class="agent-new-conversation" :disabled="sending || deciding || conversationSaving" @click="newConversation"><Plus :size="14" />新建对话</button>
      </div>
      <form v-if="renaming" class="agent-rename" @submit.prevent="renameConversation">
        <input v-model="conversationTitle" aria-label="对话名称" maxlength="100" autofocus />
        <button :disabled="conversationSaving || !conversationTitle.trim()">保存</button><button type="button" @click="renaming = false">取消</button>
      </form>
      <div ref="history" class="agent-history" role="log" aria-label="Agent 对话记录" aria-live="polite" @scroll="trackScroll">
        <p v-if="loading" class="agent-loading">正在读取对话…</p>
        <div v-else-if="!runs.length" class="agent-welcome"><Sparkles :size="28" /><h3>想从哪里开始？</h3><p>查看进度、安排任务，或一起打磨当前章节。</p><div class="agent-suggestions"><button v-for="suggestion in ['查询任务进度', '把当前章旁白的语气改得沉稳']" :key="suggestion" @click="draft = suggestion; composer?.focus()">{{ suggestion }}<ArrowUp :size="14" /></button></div></div>
        <article v-for="run in runs" :key="run.id" class="agent-turn">
          <small v-if="run.automation_id" class="agent-permission-note">定时唤醒</small><p class="agent-user">{{ run.message }}</p>
          <div class="agent-answer-label"><Sparkles :size="14" /><span>Alexandria</span><small>{{ statusNames[run.status] || run.status }}</small></div>
          <p class="agent-answer">{{ run.output || '正在思考并检查项目状态…' }}</p>
          <div v-for="action in (run.actions ?? (run.action ? [run.action] : []))" :key="action.id" class="agent-action">
            <template v-if="action.kind === 'script' || action.kind === 'script_batch'">
              <strong>{{ action.kind === 'script_batch' ? '批量脚本修改' : action.status === 'pending' || !run.auto_apply_scripts ? '脚本修改提案' : '脚本修改' }} · {{ statusNames[action.status] || action.status }}</strong>
              <p>{{ action.args.summary }}</p>
              <template v-if="action.kind === 'script_batch'">
                <p>修改 {{ action.result.modified_count ?? action.args.changes?.length ?? 0 }} 章 · 跳过 {{ action.args.skipped_count ?? 0 }} 章</p>
                <details v-for="change in action.args.changes" :key="change.chapter_id" class="agent-diff">
                  <summary>{{ chapters.find(chapter => chapter.id === change.chapter_id)?.title || `章节 ${change.chapter_id}` }} · 基于修订 {{ change.expected_revision }}</summary>
                  <div v-for="row in differences(change)" :key="row.index" class="agent-diff-columns"><div><small>修改前</small><pre>{{ content(row.before) }}</pre></div><div><small>修改后</small><pre>{{ content(row.after) }}</pre></div></div>
                </details>
              </template>
              <template v-else>
              <p class="muted">{{ chapters.find(chapter => chapter.id === action?.args.chapter_id)?.title || '章节已移除' }} · 基于修订 {{ action.args.expected_revision }} · {{ action.args.text_changed ? '包含正文修改' : '正文保持不变' }}</p>
              <div v-for="row in differences(action.args)" :key="row.index" class="agent-diff">
                <strong>条目 {{ row.index + 1 }}</strong><div class="agent-diff-columns"><div><small>修改前</small><pre>{{ content(row.before) }}</pre></div><div><small>修改后</small><pre>{{ content(row.after) }}</pre></div></div>
              </div>
              </template>
              <p v-if="action.result.message" class="agent-error">{{ action.result.message }}</p>
              <template v-if="action.status === 'pending'">
                <p v-if="!editable" class="muted">请先暂停任务，等待暂停完成后再应用提案。</p>
                <div class="heading-actions"><button class="button secondary" :disabled="deciding || run.status === 'running'" @click="decide(action, false)">拒绝修改</button><button class="button primary" :disabled="dirty || !editable || deciding || run.status === 'running'" @click="decide(action, true)">确认保存脚本</button></div>
              </template>
            </template>
            <template v-else><strong>{{ action.status === 'completed' ? (action.kind === 'task' || action.kind === 'control' ? '操作已提交，实际进度见项目面板' : '操作已完成') : statusNames[action.status] || action.status }}</strong><p>{{ operationNames[action.kind] || '项目操作' }}</p><p v-if="action.result.id">{{ action.kind === 'task' || action.kind === 'control' ? '任务编号' : '记录编号' }}：{{ action.result.id }}</p><p v-if="action.result.message">{{ action.result.message }}</p></template>
          </div>
        </article>
      </div>
      <form class="agent-compose" @submit.prevent="send">
        <div v-if="dirty" class="agent-notice" role="alert">章节有未保存的人工编辑，请先保存或放弃，再发送指令或应用提案。<div class="agent-notice-actions"><button type="button" :disabled="saving || !editable" @click="emit('save')">保存人工编辑</button><button type="button" :disabled="saving" @click="emit('discard')">放弃人工编辑</button></div></div>
        <div class="agent-context"><BookOpenText :size="13" /><span>当前章：{{ chapterTitle || '未选择' }}</span></div>
        <div class="agent-input-box"><textarea ref="composer" v-model="draft" aria-label="给 Agent 的指令" maxlength="8000" rows="2" placeholder="查询进度、修改项目，或安排定时任务…" :disabled="sending || busy" @keydown="composeKey" /><div class="agent-input-footer"><span class="agent-permission-note">按明确指令直接执行</span><button class="agent-send" :aria-label="busy ? 'Agent 处理中' : sending ? '提交中' : '发送指令'" :disabled="loading || dirty || sending || busy || !draft.trim()"><ArrowUp :size="19" /></button></div></div>
        <p v-if="error" role="alert" class="agent-error">{{ error }}</p>
        <small class="agent-footnote">{{ busy ? '收起窗口后仍会继续处理' : 'Enter 发送 · Shift+Enter 换行 · 支持项目操作与定时任务' }}</small>
      </form>
      <button type="button" class="agent-resize" aria-label="调整 Agent 窗口大小，使用方向键" @pointerdown="startGesture($event, true)" @pointermove="moveGesture" @pointerup="gesture = undefined" @lostpointercapture="gesture = undefined" @keydown="adjustWithKeys($event, true)"><Grip :size="14" /></button>
    </section>
  </Teleport>
</template>

<style scoped>
.agent-conversations { display: flex; align-items: center; gap: 6px; padding: 9px 18px; border-bottom: 1px solid #eee; flex: none; }.agent-conversations select { flex: 1; min-width: 0; padding: 7px; border: 1px solid #e5e5e5; border-radius: 8px; background: white; color: #555; font-size: 12px; text-overflow: ellipsis; }.agent-new-conversation { display: flex; align-items: center; gap: 4px; flex: none; padding: 8px; border: 0; border-radius: 8px; background: #f3f3f3; color: #444; font-size: 12px; }.agent-rename { display: flex; gap: 6px; padding: 8px 18px; }.agent-rename input { flex: 1; min-width: 0; padding: 6px; border: 1px solid #ddd; border-radius: 6px; }.agent-rename button { border: 0; border-radius: 6px; background: #eee; font-size: 12px; }
.agent-permission { display: flex; align-items: center; gap: 6px; font-size: 11px; color: #555; }.agent-permission input { accent-color: #333; }.agent-permission-note { color: #888; font-size: 10px; line-height: 1.6; }.agent-diff summary { cursor: pointer; font-size: 12px; }
.agent-launcher { position: fixed; right: 24px; bottom: 20px; z-index: 15; display: flex; align-items: center; gap: 9px; padding: 13px 18px; border: 1px solid #333; border-radius: 999px; background: #242424; color: white; font-size: 13px; font-weight: 600; box-shadow: 0 5px 20px #0002; }
.agent-live-dot, .pending-dot { width: 7px; height: 7px; border-radius: 50%; background: #8bd8b2; }.pending-dot { background: #f0c56e; }
.agent-window { position: fixed; z-index: 16; display: flex; flex-direction: column; background: #fff; color: #262626; border: 1px solid #dedede; border-radius: 18px; box-shadow: 0 18px 70px #0002, 0 2px 8px #0001; overflow: hidden; font-family: "Segoe UI", "Microsoft YaHei", sans-serif; }
.agent-header { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 17px 18px; border-bottom: 1px solid #eeeeee; cursor: grab; touch-action: none; user-select: none; flex: none; }.agent-header:active { cursor: grabbing; }.agent-title { display: flex; align-items: center; gap: 10px; min-width: 0; }.agent-symbol { display: grid; place-items: center; width: 36px; height: 36px; background: #f3f3f3; border-radius: 10px; }.agent-title h2 { font-size: 15px; margin: 0 0 3px; font-weight: 650; }.agent-title small { color: #848484; font-size: 11px; }.agent-header-actions { display: flex; gap: 4px; }.agent-icon { display: grid; place-items: center; width: 30px; height: 30px; border: 0; border-radius: 8px; background: transparent; color: #777; }.agent-icon:hover { background: #f4f4f4; color: #222; }
.agent-history { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; padding: 20px; scrollbar-gutter: stable; }.agent-loading { font-size: 13px; color: #888; }.agent-welcome { display: flex; flex-direction: column; justify-content: center; align-items: flex-start; min-height: 220px; padding: 12px 4px; }.agent-welcome > svg { color: #555; }.agent-welcome h3 { font-size: 22px; letter-spacing: -.5px; margin: 18px 0 9px; font-weight: 600; }.agent-welcome p { color: #858585; font-size: 13px; line-height: 1.7; margin: 0; }.agent-suggestions { display: grid; gap: 8px; margin-top: 22px; width: 100%; }.agent-suggestions button { display: flex; align-items: center; justify-content: space-between; padding: 12px; background: white; border: 1px solid #e8e8e8; border-radius: 10px; color: #555; font-size: 12px; text-align: left; }.agent-suggestions button:hover { background: #fafafa; }
.agent-turn { margin-bottom: 28px; font-size: 13px; }.agent-user { width: fit-content; max-width: 90%; margin: 0 0 20px auto; padding: 11px 15px; border-radius: 16px 16px 4px 16px; background: #f1f1f1; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.7; }.agent-answer-label { display: flex; align-items: center; gap: 7px; font-size: 11px; font-weight: 600; }.agent-answer-label small { margin-left: auto; color: #999; font-weight: 400; }.agent-answer { margin: 11px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.85; }.agent-action { margin-top: 14px; padding: 14px; background: #fcfcfc; border: 1px solid #e5e5e5; border-radius: 12px; overflow-wrap: anywhere; line-height: 1.7; }.agent-action strong { font-size: 12px; }.agent-action p { font-size: 12px; margin: 8px 0; }.agent-action .muted { color: #888; font-size: 11px; }.agent-diff { margin: 12px 0; }.agent-diff-columns { display: grid; gap: 8px; margin-top: 7px; }.agent-diff-columns > div { min-width: 0; padding: 10px; background: #fff; border: 1px solid #e9e9e9; border-radius: 8px; }.agent-diff-columns > div:last-child { background: #f3f9f5; border-color: #dcebe1; }.agent-diff small { color: #888; font-size: 10px; }pre { margin: 5px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; line-height: 1.7; }.agent-action .heading-actions { gap: 7px; margin-top: 14px; }.agent-action .button { padding: 8px 11px; font-size: 11px; }.agent-action .button.primary { background: #292929; box-shadow: none; }.agent-action .button.secondary { background: #efefef; color: #555; }
.agent-compose { flex: none; display: grid; gap: 9px; padding: 12px 18px 15px; border-top: 1px solid #f0f0f0; max-height: 55%; overflow-y: auto; overscroll-behavior: contain; }.agent-context { display: flex; align-items: center; gap: 6px; font-size: 11px; color: #888; }.agent-context span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.agent-input-box { border: 1px solid #e5e5e5; background: #f7f7f7; border-radius: 14px; padding: 11px 12px 9px; }.agent-input-box:focus-within { border-color: #b5b5b5; }.agent-compose textarea { display: block; width: 100%; min-height: 48px; max-height: 140px; resize: vertical; border: 0; padding: 0; outline: 0; color: #333; background: transparent; font: inherit; font-size: 13px; line-height: 1.6; }.agent-compose textarea::placeholder { color: #aaa; }.agent-input-footer { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: 7px; }.agent-input-footer label { display: flex; align-items: center; gap: 4px; color: #888; font-size: 10px; }.agent-input-footer input { accent-color: #333; }.agent-send { display: grid; place-items: center; width: 30px; height: 30px; flex: none; border: 0; border-radius: 50%; background: #242424; color: white; }.agent-send:disabled { background: #dfdfdf; color: #aaa; cursor: not-allowed; }.agent-footnote { text-align: center; color: #aaa; font-size: 10px; }.agent-notice { padding: 10px 12px; background: #fff9ed; border: 1px solid #f0e2c6; border-radius: 10px; font-size: 11px; line-height: 1.7; }.agent-notice-actions { display: flex; gap: 12px; margin-top: 6px; }.agent-notice-actions button { border: 0; padding: 0; background: transparent; color: #825f2b; font-size: 11px; text-decoration: underline; }.agent-error { margin: 0; color: #ad4343; font-size: 12px; white-space: pre-wrap; }.agent-resize { position: absolute; right: 2px; bottom: 2px; display: grid; place-items: center; width: 18px; height: 18px; padding: 0; border: 0; background: transparent; color: #aaa; cursor: nwse-resize; touch-action: none; }.agent-window :focus-visible:not(textarea), .agent-launcher:focus-visible { outline: 2px solid #767676; outline-offset: 2px; }
@media (max-width: 600px) { .agent-window { inset: 12px !important; width: auto !important; height: auto !important; border-radius: 14px; }.agent-resize { display: none; }.agent-header { cursor: default; }.agent-history { padding: 16px; }.agent-launcher { right: 16px; bottom: 16px; }.agent-compose { padding: 12px 14px; } }
</style>
