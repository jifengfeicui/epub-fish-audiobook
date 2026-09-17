<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { CircleCheck, KeyRound, LoaderCircle, MessageSquareText, Mic2, Play, Plus, Save, Server, SlidersHorizontal, Trash2, X } from 'lucide-vue-next'
import { api } from '../api/client'

interface VoiceProfile {
  id?: string
  reference_id: string
  name: string
  pool_order: number
  gender: string
  traits: string
  enabled: boolean
  sample_available?: boolean
  sample_reference_id?: string | null
  sample_title?: string | null
  sample_text?: string | null
  sample_url?: string | null
  validating?: boolean
}

interface AppSettings {
  llm_base_url: string
  llm_api_key: string
  llm_model: string
  fish_base_url: string
  fish_api_key: string
  fish_model: string
  fish_workers: number
  generation: { chunk_size: number; max_tokens: number; review_batch_size: number }
  prompts: { system_prompt: string; user_prompt: string; review_system_prompt: string; review_user_prompt: string }
  fish_tts: {
    format: string
    sample_rate: number
    mp3_bitrate: number
    temperature: number
    top_p: number
    latency: string
    condition_on_previous_chunks: boolean
    timeout_seconds: number
  }
}

const form = ref<AppSettings>({
  llm_base_url: '', llm_api_key: '', llm_model: '',
  fish_base_url: '', fish_api_key: '', fish_model: '',
  fish_workers: 5,
  generation: { chunk_size: 3000, max_tokens: 4096, review_batch_size: 25 },
  prompts: { system_prompt: '', user_prompt: '', review_system_prompt: '', review_user_prompt: '' },
  fish_tts: { format: 'mp3', sample_rate: 44100, mp3_bitrate: 128, temperature: 0.7, top_p: 0.7, latency: 'normal', condition_on_previous_chunks: true, timeout_seconds: 300 },
})
const voices = ref<VoiceProfile[]>([])
const saved = ref(false)
const error = ref('')
const activeSection = ref<'connections' | 'rendering' | 'prompts' | 'voices'>('connections')
const showAddVoice = ref(false)
const newReferenceId = ref('')
const addVoiceError = ref('')
const addingVoice = ref(false)
let previewAudio: HTMLAudioElement | null = null

onMounted(async () => {
  const [settings, profiles] = await Promise.all([
    api.get<AppSettings>('/api/v1/settings'),
    api.get<VoiceProfile[]>('/api/v1/voices'),
  ])
  form.value = settings
  voices.value = profiles.map(profile => {
    profile.enabled ??= true
    return profile
  })
})

function openAddVoice() {
  newReferenceId.value = ''
  addVoiceError.value = ''
  showAddVoice.value = true
}

function removeVoice(index: number) { voices.value.splice(index, 1) }

function hasCurrentSample(voice: VoiceProfile) {
  return voice.sample_available && voice.sample_reference_id === voice.reference_id && Boolean(voice.sample_url)
}

async function validateVoice(voice: VoiceProfile) {
  error.value = ''
  voice.validating = true
  try {
    const result = await api.post<Partial<VoiceProfile>>('/api/v1/voices/validate', { reference_id: voice.reference_id })
    voice.name ||= result.name || ''
    voice.sample_available = true
    voice.sample_reference_id = voice.reference_id
    voice.sample_title = result.sample_title
    voice.sample_text = result.sample_text
    voice.sample_url = result.sample_url
  } catch (reason) { error.value = (reason as Error).message }
  finally { voice.validating = false }
}

async function addVoice() {
  const referenceId = newReferenceId.value.trim()
  addVoiceError.value = ''
  if (!referenceId) { addVoiceError.value = '请输入 Fish Reference ID'; return }
  if (voices.value.some(voice => voice.reference_id === referenceId)) { addVoiceError.value = '该音色已在音色池中'; return }
  addingVoice.value = true
  try {
    const result = await api.post<Partial<VoiceProfile>>('/api/v1/voices/validate', { reference_id: referenceId })
    voices.value.push({
      reference_id: referenceId,
      name: result.name || referenceId,
      pool_order: voices.value.length,
      gender: result.gender || '',
      traits: result.traits || '',
      enabled: true,
      sample_available: true,
      sample_reference_id: referenceId,
      sample_title: result.sample_title,
      sample_text: result.sample_text,
      sample_url: result.sample_url,
    })
    showAddVoice.value = false
  } catch (reason) { addVoiceError.value = (reason as Error).message }
  finally { addingVoice.value = false }
}

function playSample(voice: VoiceProfile) {
  if (!hasCurrentSample(voice) || !voice.sample_url) return
  previewAudio?.pause()
  previewAudio = new Audio(voice.sample_url)
  void previewAudio.play()
}

onBeforeUnmount(() => previewAudio?.pause())

async function save() {
  error.value = ''
  try {
    await api.put('/api/v1/settings', form.value)
    voices.value = await api.put<VoiceProfile[]>('/api/v1/voices', {
      voices: voices.value.map((voice, index) => ({ ...voice, pool_order: index })),
    })
    saved.value = true
    window.setTimeout(() => { saved.value = false }, 2200)
  } catch (reason) {
    error.value = (reason as Error).message
  }
}
</script>

<template>
  <section class="page settings-page">
    <div class="page-heading"><div><span class="eyebrow">运行配置</span><h1>服务设置</h1><p>API Key 会保存到本机 SQLite，界面读取时始终只显示掩码。</p></div></div>
    <div class="section-layout settings-layout">
      <nav class="section-nav" aria-label="配置分类">
        <button :class="{ active: activeSection === 'connections' }" @click="activeSection = 'connections'"><Server :size="17" /><span>服务连接</span></button>
        <button :class="{ active: activeSection === 'rendering' }" @click="activeSection = 'rendering'"><SlidersHorizontal :size="17" /><span>生成参数</span></button>
        <button :class="{ active: activeSection === 'prompts' }" @click="activeSection = 'prompts'"><MessageSquareText :size="17" /><span>Prompt</span></button>
        <button :class="{ active: activeSection === 'voices' }" @click="activeSection = 'voices'"><Mic2 :size="17" /><span>音色池</span></button>
      </nav>
      <div class="section-content settings-content">
        <div class="settings-savebar">
          <span v-if="saved" class="success-text">设置已保存</span>
          <button class="button primary" @click="save"><Save :size="17" />保存设置与音色</button>
        </div>
        <p v-if="error" class="form-error settings-error">{{ error }}</p>
        <div v-if="activeSection === 'connections'" class="settings-grid">
          <section class="panel settings-card">
            <div class="panel-heading"><div><h2>Fish Audio</h2><span class="muted">整书语音渲染服务</span></div><KeyRound :size="19" /></div>
            <label>服务地址<input v-model="form.fish_base_url" /></label>
            <label>API Key<input v-model="form.fish_api_key" type="password" placeholder="********" /></label>
            <label>模型<input v-model="form.fish_model" /></label>
            <label>API 并发数量<input v-model.number="form.fish_workers" type="number" min="1" max="16" /></label>
          </section>
          <section class="panel settings-card">
            <div class="panel-heading"><div><h2>LLM Generate / Review</h2><span class="muted">OpenAI 兼容接口</span></div><KeyRound :size="19" /></div>
            <label>服务地址<input v-model="form.llm_base_url" /></label>
            <label>API Key<input v-model="form.llm_api_key" type="password" placeholder="********" /></label>
            <label>模型<input v-model="form.llm_model" /></label>
          </section>
        </div>
        <div v-else-if="activeSection === 'rendering'" class="settings-grid">
          <section class="panel settings-card compact-settings">
            <div class="panel-heading"><div><h2>LLM 参数</h2><span class="muted">分块与 Review 批次</span></div></div>
            <label>文本分块字符数<input v-model.number="form.generation.chunk_size" type="number" min="1" /></label>
            <label>最大输出 Token<input v-model.number="form.generation.max_tokens" type="number" min="1" /></label>
            <label>Review 条目批次<input v-model.number="form.generation.review_batch_size" type="number" min="1" /></label>
          </section>
          <section class="panel settings-card compact-settings">
            <div class="panel-heading"><div><h2>Fish 渲染参数</h2><span class="muted">影响音频缓存指纹</span></div></div>
            <div class="inline-fields"><label>采样率<input v-model.number="form.fish_tts.sample_rate" type="number" min="8000" /></label><label>MP3 kbps<input v-model.number="form.fish_tts.mp3_bitrate" type="number" min="32" /></label></div>
            <div class="inline-fields"><label>Temperature<input v-model.number="form.fish_tts.temperature" type="number" min="0" max="1" step="0.05" /></label><label>Top P<input v-model.number="form.fish_tts.top_p" type="number" min="0" max="1" step="0.05" /></label></div>
            <div class="inline-fields"><label>延迟模式<select v-model="form.fish_tts.latency"><option value="normal">normal</option><option value="balanced">balanced</option><option value="low">low</option></select></label><label>超时秒数<input v-model.number="form.fish_tts.timeout_seconds" type="number" min="1" /></label></div>
            <label class="check-field"><input v-model="form.fish_tts.condition_on_previous_chunks" type="checkbox" />保持前文条件</label>
          </section>
        </div>
        <section v-else-if="activeSection === 'prompts'" class="panel prompt-card">
          <div class="panel-heading"><div><h2>Prompt</h2><span class="muted">Generate 与 Review 的系统、用户模板</span></div></div>
          <div class="prompt-grid">
            <label>Generate System<textarea v-model="form.prompts.system_prompt" rows="8" /></label>
            <label>Generate User<textarea v-model="form.prompts.user_prompt" rows="8" /></label>
            <label>Review System<textarea v-model="form.prompts.review_system_prompt" rows="8" /></label>
            <label>Review User<textarea v-model="form.prompts.review_user_prompt" rows="8" /></label>
          </div>
        </section>
        <section v-else class="panel voices-card">
          <div class="panel-heading"><div><h2>Fish 音色池</h2><span class="muted">管理全局可用音色；旁白与角色音色均在项目中选择</span></div><button class="button secondary" @click="openAddVoice"><Plus :size="15" />添加音色</button></div>
          <div class="voice-table">
            <div class="voice-row voice-head"><span>显示名称</span><span>Fish Reference ID</span><span>性别</span><span>备注</span><span>状态</span><span>操作</span></div>
            <div v-for="(voice, index) in voices" :key="voice.id || index" class="voice-row">
              <input v-model="voice.name" placeholder="音色名称" />
              <div class="voice-reference"><input v-model.trim="voice.reference_id" placeholder="reference id" /><button class="icon-button" :title="hasCurrentSample(voice) ? '重新检查并下载示例' : '检查并下载示例'" :disabled="!voice.reference_id || voice.validating" @click="validateVoice(voice)"><LoaderCircle v-if="voice.validating" class="spin" :size="15" /><CircleCheck v-else :size="15" /></button></div>
              <fieldset class="voice-gender" :aria-label="`${voice.name || `音色 ${index + 1}`}性别`">
                <label v-for="option in [{ label: '未指定', value: '' }, { label: '男', value: '男' }, { label: '女', value: '女' }, { label: '中性', value: '中性' }]" :key="option.label"><input v-model="voice.gender" type="radio" :name="`voice-gender-${index}`" :value="option.value" />{{ option.label }}</label>
              </fieldset>
              <input v-model="voice.traits" placeholder="沉稳、清亮、低沉…" />
              <label class="enabled-check"><input v-model="voice.enabled" type="checkbox" /><span>{{ voice.enabled ? '启用' : '禁用' }}<small>{{ hasCurrentSample(voice) ? '示例已缓存' : '待校验' }}</small></span></label>
              <div class="voice-actions"><button class="icon-button" title="播放示例" :disabled="!hasCurrentSample(voice)" @click="playSample(voice)"><Play :size="15" /></button><button class="icon-button" title="移除音色" @click="removeVoice(index)"><Trash2 :size="15" /></button></div>
            </div>
            <p v-if="!voices.length" class="empty-voices">还没有配置音色，开始渲染前至少添加一个。</p>
          </div>
        </section>
      </div>
    </div>
    <div v-if="showAddVoice" class="modal-backdrop" @click.self="showAddVoice = false">
      <form class="dialog voice-dialog" role="dialog" aria-modal="true" aria-labelledby="add-voice-title" @submit.prevent="addVoice">
        <header><div><span class="eyebrow">Fish Audio</span><h2 id="add-voice-title">添加音色</h2></div><button class="icon-button" type="button" title="关闭" @click="showAddVoice = false"><X :size="19" /></button></header>
        <label>Fish Reference ID<input v-model.trim="newReferenceId" autofocus placeholder="输入模型 ID" /></label>
        <p v-if="addVoiceError" class="form-error">{{ addVoiceError }}</p>
        <footer><button class="button secondary" type="button" @click="showAddVoice = false">取消</button><button class="button primary" type="submit" :disabled="addingVoice || !newReferenceId.trim()"><LoaderCircle v-if="addingVoice" class="spin" :size="16" />{{ addingVoice ? '正在校验...' : '校验并添加' }}</button></footer>
      </form>
    </div>
  </section>
</template>
