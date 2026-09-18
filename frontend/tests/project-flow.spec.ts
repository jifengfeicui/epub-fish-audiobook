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

test('selects a temporary TTS range and starts a merge job when every chapter has audio', async ({ page }) => {
  const project = {
    id: 'book-1', title: '测试有声书', source_filename: 'book.epub', status: 'completed',
    chapter_count: 2, included_chapter_count: 2, chapter_status_counts: { done: 2 }, created_at: '', updated_at: '', archived_at: null,
    settings: { render_start_mode: 'after_review_batch', release_batch_size: 3, from_chapter: null, to_chapter: null, first_person_speaker: null, context_window: 0, single_speaker: false, speaker_name: 'NARRATOR', instruct: '', workers: 2 },
    latest_job: null,
  }
  const chapters = [1, 2].map(position => ({ id: position, project_id: 'book-1', position, included_position: position, included: true, title: `第${position}章`, status: 'done', active_revision_id: `revision-${position}` }))
  const requests: Record<string, unknown>[] = []
  await page.route('**/api/v1/projects?*', route => route.fulfill({ json: [project] }))
  await page.route('**/api/v1/projects/book-1', route => route.fulfill({ json: project }))
  await page.route('**/api/v1/projects/book-1/chapters', route => route.fulfill({ json: chapters }))
  await page.route('**/api/v1/projects/book-1/artifacts', route => route.fulfill({ json: chapters.map(chapter => ({ id: `chapter-${chapter.id}`, chapter_id: chapter.id, kind: 'chapter_mp3', path: `${chapter.id}.mp3`, size: 3 })) }))
  await page.route('**/api/v1/projects/book-1/events', route => route.fulfill({ json: [] }))
  await page.route('**/api/v1/projects/book-1/speaker-assignments', route => route.fulfill({ json: [] }))
  await page.route('**/api/v1/projects/book-1/characters', route => route.fulfill({ json: [] }))
  await page.route('**/api/v1/voices', route => route.fulfill({ json: [{ id: 'voice-1', name: '旁白', gender: '女', traits: '', enabled: true, sample_available: false, sample_url: null }] }))
  await page.route('**/api/v1/projects/book-1/voice-pool', route => route.fulfill({ json: { excluded_voice_ids: [], narrator_voice_profile_id: 'voice-1' } }))
  await page.route('**/api/v1/chapters/1/script', route => route.fulfill({ json: { chapter_id: 1, revision: 1, kind: 'reviewed', entries: [] } }))
  await page.route('**/api/v1/projects/book-1/jobs', async route => {
    requests.push(await route.request().postDataJSON())
    await route.fulfill({ json: { id: 'merge-1', project_id: 'book-1', type: 'merge', status: 'queued' } })
  })

  await page.goto('/projects/book-1')
  await page.getByRole('button', { name: '开始 TTS' }).click()
  await expect(page.getByRole('heading', { name: '本次 TTS 范围' })).toBeVisible()
  await page.getByLabel('从第几章').fill('2')
  await page.getByLabel('到第几章').fill('2')
  await page.getByRole('dialog').getByRole('button', { name: '开始 TTS' }).click()
  await page.getByRole('button', { name: '合成整书' }).click()

  expect(requests[0]).toEqual({ type: 'render', payload: { from_included_position: 2, to_included_position: 2 } })
  expect(requests[1]).toEqual({ type: 'merge' })
})

test('excludes a chapter while keeping it visible', async ({ page }) => {
  let chapters = [1, 2].map(position => ({
    id: position, project_id: 'book-exclusion', position, title: position === 1 ? '引言' : '正文',
    included: true, status: 'reviewed', active_revision_id: `revision-${position}`,
  }))
  let savedIds: number[] = []
  const project = () => ({
    id: 'book-exclusion', title: '章节排除测试', source_filename: 'book.epub', status: 'ready_for_tts',
    chapter_count: 2, included_chapter_count: chapters.filter(chapter => chapter.included).length,
    chapter_status_counts: { reviewed: chapters.filter(chapter => chapter.included).length }, created_at: '', updated_at: '', archived_at: null,
    settings: { render_start_mode: 'after_review_batch', release_batch_size: 3, from_chapter: null, to_chapter: null, first_person_speaker: null, context_window: 0, single_speaker: false, speaker_name: 'NARRATOR', instruct: '', workers: 2 },
    latest_job: null,
  })
  await page.route('**/api/v1/projects/book-exclusion', route => route.fulfill({ json: project() }))
  await page.route('**/api/v1/projects/book-exclusion/chapters', async route => {
    if (route.request().method() === 'PUT') {
      savedIds = (await route.request().postDataJSON()).included_chapter_ids
      chapters = chapters.map(chapter => ({ ...chapter, included: savedIds.includes(chapter.id) }))
    }
    await route.fulfill({ json: chapters })
  })
  await page.route('**/api/v1/projects/book-exclusion/artifacts', route => route.fulfill({ json: [] }))
  await page.route('**/api/v1/projects/book-exclusion/events', route => route.fulfill({ json: [] }))
  await page.route('**/api/v1/projects/book-exclusion/speaker-assignments', route => route.fulfill({ json: [] }))
  await page.route('**/api/v1/projects/book-exclusion/characters', route => route.fulfill({ json: [] }))
  await page.route('**/api/v1/projects/book-exclusion/voice-pool', route => route.fulfill({ json: { excluded_voice_ids: [], narrator_voice_profile_id: null } }))
  await page.route('**/api/v1/voices', route => route.fulfill({ json: [] }))
  await page.route('**/api/v1/chapters/*/script', route => route.fulfill({ json: { chapter_id: 1, revision: 1, kind: 'reviewed', entries: [] } }))

  await page.goto('/projects/book-exclusion')
  const introRow = page.locator('.chapter-item-row').filter({ hasText: '引言' })
  const toggle = introRow.getByRole('button', { name: '禁用原第 1 章' })
  await expect(toggle).toHaveCSS('opacity', '0')
  await introRow.hover()
  await expect(toggle).toHaveCSS('opacity', '1')
  await toggle.click()
  await expect(page.getByText('已禁用')).toBeVisible()
  await page.getByRole('button', { name: '保存', exact: true }).click()

  expect(savedIds).toEqual([2])
  await expect(page.getByText('章节启用状态已保存，整书产物已标记为待重新合成')).toBeVisible()
  await expect(page.getByRole('button', { name: /引言.*已禁用/ })).toBeVisible()
  await expect(page.getByText('已启用 1 / 2')).toBeVisible()
})

test('publishes a private-ready prefix with public confirmation and validates append count', async ({ page }) => {
  const project = {
    id: 'book-1', title: '测试有声书', source_filename: 'book.epub', status: 'completed', chapter_count: 3,
    chapter_status_counts: { done: 2, pending: 1 }, created_at: '', updated_at: '', archived_at: null,
    settings: { render_start_mode: 'after_review_batch', release_batch_size: 3, from_chapter: null, to_chapter: null, first_person_speaker: null, context_window: 0, single_speaker: false, speaker_name: 'NARRATOR', instruct: '', workers: 2 }, latest_job: null,
  }
  const chapters = [1, 2, 3].map(position => ({ id: position, project_id: 'book-1', position, title: `第${position}章`, status: position < 3 ? 'done' : 'pending', active_revision_id: null }))
  let published = false
  const jobs: Record<string, unknown>[] = []
  await page.route('**/api/v1/projects/book-1', route => route.fulfill({ json: project }))
  await page.route('**/api/v1/projects/book-1/chapters', route => route.fulfill({ json: chapters }))
  await page.route('**/api/v1/projects/book-1/artifacts', route => route.fulfill({ json: [] }))
  await page.route('**/api/v1/projects/book-1/events', route => route.fulfill({ json: [] }))
  await page.route('**/api/v1/projects/book-1/speaker-assignments', route => route.fulfill({ json: [] }))
  await page.route('**/api/v1/projects/book-1/characters', route => route.fulfill({ json: [] }))
  await page.route('**/api/v1/projects/book-1/voice-pool', route => route.fulfill({ json: { excluded_voice_ids: [], narrator_voice_profile_id: null } }))
  await page.route('**/api/v1/voices', route => route.fulfill({ json: [] }))
  await page.route('**/api/v1/chapters/1/script', route => route.fulfill({ json: { chapter_id: 1, revision: 0, kind: null, entries: [] } }))
  await page.route('**/api/v1/projects/book-1/bilibili', route => route.fulfill({ json: {
    continuous_audio_count: 2, prepared_count: 2, published_count: published ? 1 : 0, appendable_count: published ? 1 : 2,
    form: { author: '', publisher: '', source: '测试来源', title: '《测试有声书》AI有声书', tid: 201, tags: '有声书,读书', desc: '按章节分P', visibility: 'only_self', line: 'cnbldsa' },
    publication: published ? { bvid: 'BV1234567890' } : null,
    chapters: chapters.map(chapter => ({ ...chapter, bilibili_state: chapter.position === 1 && published ? 'published' : chapter.position < 3 ? 'ready' : 'missing', image_url: null, video_url: null })),
  } }))
  await page.route('**/api/v1/projects/book-1/bilibili/jobs', async route => {
    const body = await route.request().postDataJSON()
    jobs.push(body)
    if (body.action === 'publish') published = true
    await route.fulfill({ json: { id: 'bili-job', type: 'bilibili', status: 'queued', payload: body } })
  })

  await page.goto('/projects/book-1')
  await page.getByRole('button', { name: 'B站投稿' }).click()
  await expect(page.getByText('连续音频')).toBeVisible()
  await page.getByLabel('可见性').selectOption('public')
  page.once('dialog', dialog => dialog.accept())
  await page.getByRole('button', { name: '首发' }).click()
  expect(jobs[0]).toMatchObject({ action: 'publish', parts: 1, visibility: 'public', confirm_public: true })
  await page.getByLabel('本次追加').fill('2')
  await expect(page.getByRole('button', { name: '追加分P' })).toBeDisabled()
  await page.getByLabel('本次追加').fill('1')
  await page.getByRole('button', { name: '追加分P' }).click()
  expect(jobs[1]).toMatchObject({ action: 'append', parts: 1, line: 'cnbldsa' })
})
