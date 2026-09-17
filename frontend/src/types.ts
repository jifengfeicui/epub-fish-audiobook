export type JobStatus = 'queued' | 'running' | 'pausing' | 'paused' | 'interrupted' | 'completed' | 'cancelled' | 'failed'

export interface Job {
  id: string
  project_id: string
  type: 'preprocess' | 'render' | 'merge'
  status: JobStatus
  render_start_mode: 'after_review_batch' | 'after_all_reviews'
  release_batch_size: number
  from_chapter: number | null
  to_chapter: number | null
  error: string | null
}

export interface ProjectSettings {
  render_start_mode: 'after_review_batch' | 'after_all_reviews'
  release_batch_size: number
  from_chapter: number | null
  to_chapter: number | null
  first_person_speaker: string | null
  context_window: number
  single_speaker: boolean
  speaker_name: string
  instruct: string
}

export interface Project {
  id: string
  title: string
  source_filename: string
  status: string
  chapter_count: number
  chapter_status_counts: Record<string, number>
  created_at: string
  updated_at: string
  archived_at: string | null
  settings: ProjectSettings
  latest_job: Job | null
}

export interface Character {
  speaker: string
  gender: string
  personality: string
  line_count: number
  importance: number
  voice_profile_id: string | null
  voice_name: string
  user_edited: boolean
}

export interface Chapter {
  id: number
  project_id: string
  position: number
  title: string
  status: string
  active_revision_id: string | null
}

export interface ScriptEntry {
  id?: number
  position?: number
  speaker: string
  text: string
  instruct: string
}

export interface Script {
  chapter_id: number
  revision: number
  kind: string | null
  entries: ScriptEntry[]
}

export interface JobEvent {
  event_id: number
  job_id: string
  type: string
  timestamp: string
  payload: Record<string, unknown>
}

export interface Artifact {
  id: string
  chapter_id: number | null
  kind: 'chapter_mp3' | 'book_mp3'
  path: string
  size: number
}
