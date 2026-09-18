<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ExternalLink, Film, LoaderCircle, Play, RefreshCw, Replace, Upload } from 'lucide-vue-next'
import { api } from '../api/client'
import type { BilibiliStatus } from '../types'

const props = defineProps<{ projectId: string; running: boolean }>()
const emit = defineEmits<{ refresh: [] }>()
const state = ref<BilibiliStatus | null>(null)
const error = ref('')
const busy = ref(false)
const publishParts = ref(1)
const appendParts = ref(1)
const previewChapter = ref<number | null>(null)
const stateLabels = { missing: '未生成', ready: '可发布', published: '已发布', outdated: '音频已变化', remote_mismatch: '远端不一致' }
const availablePublish = computed(() => state.value?.appendable_count || 0)

async function load() {
  error.value = ''
  try {
    state.value = await api.get<BilibiliStatus>(`/api/v1/projects/${props.projectId}/bilibili`)
    publishParts.value = Math.max(1, Math.min(publishParts.value, availablePublish.value || 1))
    appendParts.value = Math.max(1, Math.min(appendParts.value, availablePublish.value || 1))
  } catch (reason) { error.value = (reason as Error).message }
}

async function start(payload: Record<string, unknown>) {
  error.value = ''
  if (payload.action === 'publish' && state.value?.form.visibility === 'public') {
    if (!window.confirm('公开投稿将立即对外可见，并可能触发审核。确认继续吗？')) return
    payload.confirm_public = true
  }
  busy.value = true
  try {
    await api.post(`/api/v1/projects/${props.projectId}/bilibili/jobs`, payload)
    emit('refresh')
    await load()
  } catch (reason) { error.value = (reason as Error).message }
  finally { busy.value = false }
}

function publish() {
  if (!state.value) return
  start({ action: 'publish', parts: publishParts.value, ...state.value.form })
}

function append() {
  start({ action: 'append', parts: appendParts.value, line: state.value?.form.line || 'cnbldsa' })
}

onMounted(load)
</script>

<template>
  <section class="panel bilibili-panel">
    <div class="panel-heading">
      <div><h2>B站分章投稿</h2><span class="muted">一个项目对应一个稿件，章节按顺序成为分P</span></div>
      <button class="icon-button" title="检查本地与远端状态" :disabled="busy" @click="load"><RefreshCw :size="17" /></button>
    </div>
    <p v-if="error" class="form-error">{{ error }}</p>
    <template v-if="state">
      <div class="bilibili-metrics">
        <div><strong>{{ state.continuous_audio_count }}</strong><span>连续音频</span></div>
        <div><strong>{{ state.prepared_count }}</strong><span>已准备</span></div>
        <div><strong>{{ state.published_count }}</strong><span>已发布</span></div>
        <div><strong>{{ state.appendable_count }}</strong><span>可追加</span></div>
      </div>
      <div class="bilibili-toolbar">
        <button class="button secondary" :disabled="running || busy || !state.continuous_audio_count" @click="start({ action: 'prepare' })"><Film :size="16" />准备视频</button>
        <a v-if="state.publication?.bvid" class="button secondary" :href="`https://www.bilibili.com/video/${state.publication.bvid}`" target="_blank" rel="noreferrer"><ExternalLink :size="16" />{{ state.publication.bvid }}</a>
        <span v-if="state.publication?.sync_error" class="warning-text">远端同步失败，保留本地状态</span>
      </div>

      <div v-if="!state.publication" class="bilibili-form">
        <label>稿件标题<input v-model.trim="state.form.title" maxlength="80" /></label>
        <div class="inline-fields"><label>作者<input v-model.trim="state.form.author" /></label><label>出版社<input v-model.trim="state.form.publisher" /></label></div>
        <label>转载来源<input v-model.trim="state.form.source" required /></label>
        <div class="inline-fields"><label>分区<input v-model.number="state.form.tid" type="number" min="1" /></label><label>上传线路<select v-model="state.form.line"><option value="cnbldsa">cnbldsa</option><option value="bda2">bda2</option><option value="txa">txa</option><option value="alia">alia</option></select></label></div>
        <label>标签<input v-model.trim="state.form.tags" /></label>
        <label>简介<textarea v-model="state.form.desc" rows="4" /></label>
        <div class="publish-row"><label>可见性<select v-model="state.form.visibility"><option value="only_self">仅自己可见</option><option value="public">公开</option></select></label><label>首发分P数<input v-model.number="publishParts" type="number" min="1" :max="availablePublish" /></label><button class="button primary" :disabled="running || busy || !state.form.source.trim() || publishParts < 1 || publishParts > availablePublish" @click="publish"><Upload :size="16" />首发</button></div>
      </div>
      <div v-else class="publish-row append-row">
        <label>本次追加<input v-model.number="appendParts" type="number" min="1" :max="state.appendable_count" /></label>
        <label>上传线路<select v-model="state.form.line"><option value="cnbldsa">cnbldsa</option><option value="bda2">bda2</option><option value="txa">txa</option><option value="alia">alia</option></select></label>
        <button class="button primary" :disabled="running || busy || appendParts < 1 || appendParts > state.appendable_count" @click="append"><Upload :size="16" />追加分P</button>
      </div>

      <div class="bilibili-chapters">
        <div v-for="chapter in state.chapters" :key="chapter.id" class="bilibili-chapter-row">
          <span class="chapter-number">{{ String(chapter.position).padStart(3, '0') }}</span>
          <strong>{{ chapter.title }}</strong>
          <span :class="['bilibili-state', chapter.bilibili_state]">{{ stateLabels[chapter.bilibili_state] }}</span>
          <button v-if="chapter.video_url" class="icon-button" title="预览章节视频" @click="previewChapter = previewChapter === chapter.id ? null : chapter.id"><Play :size="15" /></button>
          <button v-if="chapter.bilibili_state === 'outdated'" class="button secondary" :disabled="running || busy" @click="start({ action: 'replace', chapter_id: chapter.id, line: state?.form.line || 'cnbldsa' })"><Replace :size="15" />替换</button>
          <div v-if="previewChapter === chapter.id" class="bilibili-preview"><img v-if="chapter.image_url" :src="chapter.image_url" alt="章节封面" /><video controls preload="metadata" :src="chapter.video_url || undefined" /></div>
        </div>
      </div>
    </template>
    <div v-else class="tab-empty"><LoaderCircle class="spin" :size="18" />正在读取投稿状态</div>
  </section>
</template>
