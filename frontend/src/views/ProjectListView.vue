<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ArrowRight, Plus, RefreshCw, Search } from 'lucide-vue-next'
import { useRouter } from 'vue-router'
import { useProjectsStore } from '../stores/projects'
import StatusBadge from '../components/StatusBadge.vue'
import EmptyState from '../components/EmptyState.vue'
import ProjectCreateDialog from '../components/ProjectCreateDialog.vue'
import type { Project } from '../types'

const store = useProjectsStore()
const router = useRouter()
const showDialog = ref(false)
const query = ref('')
const filtered = computed(() => store.projects.filter(project => project.title.toLowerCase().includes(query.value.toLowerCase())))
onMounted(() => store.loadProjects())

function created(project: Project) {
  showDialog.value = false
  store.loadProjects()
  router.push(`/projects/${project.id}`)
}
</script>

<template>
  <section class="page projects-page">
    <div class="page-heading">
      <div><span class="eyebrow">工作台</span><h1>你的有声书项目</h1><p>从 EPUB 到 Fish Audio 成品，章节会在审校完成后按批次进入渲染。</p></div>
      <button class="button primary" @click="showDialog = true"><Plus :size="18" />新建项目</button>
    </div>
    <div class="toolbar"><label class="search"><Search :size="17" /><input v-model="query" placeholder="搜索项目" /></label><button class="icon-button" title="刷新项目" @click="store.loadProjects()"><RefreshCw :size="18" :class="{ spin: store.loading }" /></button></div>
    <p v-if="store.error" class="form-error">{{ store.error }}</p>
    <div v-if="filtered.length" class="project-grid">
      <article v-for="project in filtered" :key="project.id" class="project-card" @click="router.push(`/projects/${project.id}`)">
        <div class="project-card-top"><span class="book-spine">{{ project.title.slice(0, 1) }}</span><StatusBadge :status="project.latest_job?.status || project.status" /></div>
        <h2>{{ project.title }}</h2><span class="muted">{{ project.source_filename }}</span>
        <div class="card-meta"><span>{{ project.chapter_count }} 章</span><span>{{ project.chapter_status_counts.done || 0 }} 已完成</span><ArrowRight :size="17" /></div>
      </article>
    </div>
    <EmptyState v-else title="还没有项目" detail="导入一本 EPUB，开始建立你的第一本有声书。"><button class="button secondary" @click="showDialog = true"><Plus :size="17" />导入 EPUB</button></EmptyState>
    <ProjectCreateDialog v-if="showDialog" @close="showDialog = false" @created="created" />
  </section>
</template>
