import { expect, test } from '@playwright/test'

test('opens a project and keeps preprocessing separate from TTS', async ({ page }) => {
  let status = 'ready'
  let savedPool: { excluded_voice_ids: string[]; narrator_voice_profile_id: string | null } | undefined
  const project = () => ({
    id: 'book-1', title: '测试有声书', source_filename: 'book.epub', status,
    chapter_count: 1, chapter_status_counts: {}, created_at: '', updated_at: '', archived_at: null,
    settings: { render_start_mode: 'after_review_batch', release_batch_size: 3, from_chapter: null, to_chapter: null, first_person_speaker: null, context_window: 0, single_speaker: false, speaker_name: 'NARRATOR', instruct: '', workers: 2 },
    latest_job: status === 'ready' ? null : { id: 'job-1', project_id: 'book-1', type: 'preprocess', status, render_start_mode: 'after_review_batch', release_batch_size: 3, error: null },
  })
  await page.route('**/api/v1/projects?*', route => route.fulfill({ json: [project()] }))
  await page.route('**/api/v1/projects/book-1', route => route.fulfill({ json: project() }))
  await page.route('**/api/v1/projects/book-1/chapters', route => route.fulfill({ json: [{ id: 1, project_id: 'book-1', position: 1, title: '第一章', status: 'pending', active_revision_id: null }] }))
  await page.route('**/api/v1/projects/book-1/artifacts', route => route.fulfill({ json: [] }))
  await page.route('**/api/v1/projects/book-1/events', route => route.fulfill({ json: [] }))
  await page.route('**/api/v1/projects/book-1/speaker-assignments', route => route.fulfill({ json: [] }))
  await page.route('**/api/v1/projects/book-1/characters', route => route.fulfill({ json: [] }))
  await page.route('**/api/v1/voices', route => route.fulfill({ json: [
    { id: 'voice-1', name: '旁白一', gender: '女', traits: '沉稳', enabled: true, sample_available: true, sample_url: '/sample.wav' },
    { id: 'voice-2', name: '旁白二', gender: '男', traits: '清晰', enabled: true, sample_available: false, sample_url: null },
  ] }))
  await page.route('**/api/v1/projects/book-1/voice-pool', async route => {
    if (route.request().method() === 'PUT') savedPool = route.request().postDataJSON()
    await route.fulfill({ json: savedPool || { excluded_voice_ids: [], narrator_voice_profile_id: 'voice-1' } })
  })
  await page.route('**/api/v1/chapters/1/script', route => route.fulfill({ json: { chapter_id: 1, revision: 0, kind: null, entries: [] } }))
  await page.route('**/api/v1/projects/book-1/jobs', async route => {
    expect((await route.request().postDataJSON()).type).toBe('preprocess')
    status = 'running'
    await route.fulfill({ json: project().latest_job })
  })

  await page.goto('/')
  await page.getByText('测试有声书').click()
  await expect(page.getByRole('button', { name: '开始 TTS' })).toBeDisabled()
  await expect(page.getByRole('button', { name: '合成整书' })).toBeDisabled()
  const navBox = await page.getByRole('navigation', { name: '项目内容' }).boundingBox()
  const workspaceBox = await page.locator('.workspace-grid').boundingBox()
  expect(navBox!.x).toBeLessThan(workspaceBox!.x)
  await page.getByRole('button', { name: /角色池/ }).click()
  await expect(page.getByRole('heading', { name: '项目角色池' })).toBeVisible()
  await page.getByRole('button', { name: '项目设置' }).click()
  await page.getByLabel('旁白音色').selectOption('voice-2')
  await page.getByRole('button', { name: '保存项目音色' }).click()
  expect(savedPool?.narrator_voice_profile_id).toBe('voice-2')
  await page.getByRole('button', { name: /产物/ }).click()
  await expect(page.getByText('还没有可下载的音频产物。')).toBeVisible()
  await page.getByRole('button', { name: /日志/ }).click()
  await expect(page.getByRole('heading', { name: '活动日志' })).toBeVisible()
  await page.getByRole('button', { name: '章节 1', exact: true }).click()
  await page.getByRole('button', { name: '开始预解析' }).click()
  await expect(page.getByRole('button', { name: '暂停' })).toBeVisible()
})

test('starts a merge job when every chapter has audio', async ({ page }) => {
  const project = {
    id: 'book-1', title: '测试有声书', source_filename: 'book.epub', status: 'completed',
    chapter_count: 1, chapter_status_counts: { done: 1 }, created_at: '', updated_at: '', archived_at: null,
    settings: { render_start_mode: 'after_review_batch', release_batch_size: 3, from_chapter: null, to_chapter: null, first_person_speaker: null, context_window: 0, single_speaker: false, speaker_name: 'NARRATOR', instruct: '', workers: 2 },
    latest_job: null,
  }
  const chapter = { id: 1, project_id: 'book-1', position: 1, title: '第一章', status: 'done', active_revision_id: 'revision-1' }
  let requestedType = ''
  await page.route('**/api/v1/projects?*', route => route.fulfill({ json: [project] }))
  await page.route('**/api/v1/projects/book-1', route => route.fulfill({ json: project }))
  await page.route('**/api/v1/projects/book-1/chapters', route => route.fulfill({ json: [chapter] }))
  await page.route('**/api/v1/projects/book-1/artifacts', route => route.fulfill({ json: [{ id: 'chapter-1', chapter_id: 1, kind: 'chapter_mp3', path: 'chapter.mp3', size: 3 }] }))
  await page.route('**/api/v1/projects/book-1/events', route => route.fulfill({ json: [] }))
  await page.route('**/api/v1/projects/book-1/speaker-assignments', route => route.fulfill({ json: [] }))
  await page.route('**/api/v1/projects/book-1/characters', route => route.fulfill({ json: [] }))
  await page.route('**/api/v1/voices', route => route.fulfill({ json: [] }))
  await page.route('**/api/v1/chapters/1/script', route => route.fulfill({ json: { chapter_id: 1, revision: 1, kind: 'reviewed', entries: [] } }))
  await page.route('**/api/v1/projects/book-1/jobs', async route => {
    requestedType = (await route.request().postDataJSON()).type
    await route.fulfill({ json: { id: 'merge-1', project_id: 'book-1', type: 'merge', status: 'queued' } })
  })

  await page.goto('/projects/book-1')
  await page.getByRole('button', { name: '合成整书' }).click()

  expect(requestedType).toBe('merge')
})
