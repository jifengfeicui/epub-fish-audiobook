import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '../api/client'
import type { Artifact, Chapter, JobEvent, Project } from '../types'

export const useProjectsStore = defineStore('projects', () => {
  const projects = ref<Project[]>([])
  const current = ref<Project | null>(null)
  const chapters = ref<Chapter[]>([])
  const artifacts = ref<Artifact[]>([])
  const speakerAssignments = ref<Array<{ speaker: string; voice_name: string; reference_id: string }>>([])
  const events = ref<JobEvent[]>([])
  const loading = ref(false)
  const error = ref('')
  const latestEventId = computed(() => events.value.at(-1)?.event_id || 0)

  async function loadProjects() {
    loading.value = true
    error.value = ''
    try { projects.value = await api.get<Project[]>('/api/v1/projects') }
    catch (reason) { error.value = (reason as Error).message }
    finally { loading.value = false }
  }

  async function loadProject(id: string) {
    const previousEvents = current.value?.id === id ? events.value : []
    const [project, chapterRows, artifactRows, eventRows, assignmentRows] = await Promise.all([
      api.get<Project>(`/api/v1/projects/${id}`),
      api.get<Chapter[]>(`/api/v1/projects/${id}/chapters`),
      api.get<Artifact[]>(`/api/v1/projects/${id}/artifacts`),
      api.get<JobEvent[]>(`/api/v1/projects/${id}/events`),
      api.get<Array<{ speaker: string; voice_name: string; reference_id: string }>>(`/api/v1/projects/${id}/speaker-assignments`),
    ])
    current.value = project
    chapters.value = chapterRows
    artifacts.value = artifactRows
    const mergedEvents = [...previousEvents, ...eventRows]
    events.value = Array.from(new Map(mergedEvents.map(event => [event.event_id, event])).values())
      .sort((left, right) => left.event_id - right.event_id)
      .slice(-500)
    speakerAssignments.value = assignmentRows
  }

  function receiveEvent(event: JobEvent) {
    if (events.value.some(item => item.event_id === event.event_id)) return
    events.value.push(event)
    events.value.sort((left, right) => left.event_id - right.event_id)
    if (events.value.length > 500) events.value.shift()
  }

  return { projects, current, chapters, artifacts, speakerAssignments, events, loading, error, latestEventId, loadProjects, loadProject, receiveEvent }
})
