<script setup lang="ts">
import { ref } from 'vue'
import { Upload, X } from 'lucide-vue-next'
import { api } from '../api/client'
import type { Project } from '../types'

const emit = defineEmits<{ close: []; created: [project: Project] }>()
const title = ref('')
const file = ref<File | null>(null)
const busy = ref(false)
const error = ref('')

async function submit() {
  if (!file.value) { error.value = '请选择 EPUB 或 TXT 文件'; return }
  busy.value = true
  error.value = ''
  const form = new FormData()
  form.append('file', file.value)
  form.append('title', title.value)
  try { emit('created', await api.upload<Project>('/api/v1/projects', form)) }
  catch (reason) { error.value = (reason as Error).message }
  finally { busy.value = false }
}
</script>

<template>
  <div class="modal-backdrop" @click.self="emit('close')">
    <section class="dialog" role="dialog" aria-modal="true" aria-labelledby="new-project-title">
      <header><div><span class="eyebrow">新建项目</span><h2 id="new-project-title">导入书籍</h2></div><button class="icon-button" title="关闭" @click="emit('close')"><X :size="19" /></button></header>
      <label class="file-drop">
        <Upload :size="24" />
        <strong>{{ file?.name || '选择 EPUB 或 TXT' }}</strong>
        <span>源文件保存在项目目录，章节正文写入 SQLite</span>
        <input type="file" accept=".epub,.txt,application/epub+zip,text/plain" @change="file = ($event.target as HTMLInputElement).files?.[0] || null" />
      </label>
      <div class="form-grid">
        <label class="span-2">项目名称<input v-model="title" placeholder="默认使用文件名" /></label>
      </div>
      <p v-if="error" class="form-error">{{ error }}</p>
      <footer><button class="button secondary" @click="emit('close')">取消</button><button class="button primary" :disabled="busy" @click="submit">{{ busy ? '正在导入...' : '创建项目' }}</button></footer>
    </section>
  </div>
</template>
