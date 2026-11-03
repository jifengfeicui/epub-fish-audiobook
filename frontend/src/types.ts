export type JobStatus = 'queued' | 'running' | 'pausing' | 'paused' | 'interrupted' | 'completed' | 'cancelled' | 'failed'

export interface Job {
  id: string
  project_id: string
  type: 'preprocess' | 'render' | 'merge' | 'bilibili' | 'character_analysis'
  payload: Record<string, unknown>
  progress?: { completed: number; total: number }
  status: JobStatus
  render_start_mode: 'after_review_batch' | 'after_all_reviews'
  release_batch_size: number
  from_chapter: number | null
  to_chapter: number | null
  error: string | null
}

export interface ProjectSettings {
  agent_auto_apply_scripts: boolean
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
  included_chapter_count: number
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
  included_position: number | null
  title: string
  included: boolean
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

export interface ScriptChange {
  chapter_id?: number
  expected_revision?: number
  before?: ScriptEntry[]
  entries?: ScriptEntry[]
  text_changed?: boolean
}

export interface AgentAction {
  id: string
  kind: string
  status: string
  args: ScriptChange & { summary?: string; changes?: ScriptChange[]; skipped_count?: number; [key: string]: unknown }
  result: { id?: string; chapter_id?: number; revision?: number; message?: string; modified_count?: number; skipped_count?: number; chapters?: Array<{ chapter_id: number; revision: number }>; [key: string]: unknown }
}

export interface AgentConversation {
  id: string
  title: string
  created_at: string
}

export interface AgentRun {
  id: string
  request_id: string
  conversation_id?: string | null
  message: string
  status: 'running' | 'completed' | 'failed' | 'interrupted'
  output: string
  created_at: string
  auto_apply_scripts?: boolean
  automation_id?: string | null
  actions?: AgentAction[]
  action: AgentAction | null
}

export interface ProjectAutomation {
  id: string
  project_id: string
  instruction: string
  interval_seconds: number
  enabled: boolean
  next_run_at: string
  selected_chapter_id: number | null
  last_run_id: string | null
  last_run?: Pick<AgentRun, 'id' | 'status' | 'output' | 'created_at'> | null
  paused_reason: string
  created_at: string
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

export type BilibiliChapterState = 'missing' | 'ready' | 'published' | 'outdated' | 'remote_mismatch'

export interface BilibiliChapter {
  id: number
  position: number
  included_position: number
  title: string
  bilibili_state: BilibiliChapterState
  image_url: string | null
  video_url: string | null
}

export interface BilibiliForm {
  author: string
  publisher: string
  source: string
  title: string
  tid: number
  tags: string
  desc: string
  visibility: 'only_self' | 'public'
  line: string
}

export interface BilibiliStatus {
  continuous_audio_count: number
  prepared_count: number
  published_count: number
  appendable_count: number
  form: BilibiliForm
  publication: null | {
    bvid: string
    aid?: string | null
    last_sync_at?: string
    remote_count?: number
    sync_error?: string
    remote_mismatch?: boolean
  }
  chapters: BilibiliChapter[]
}
