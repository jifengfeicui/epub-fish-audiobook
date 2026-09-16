import { expect, test } from '@playwright/test'

test('opens a project and keeps preprocessing separate from TTS', async ({ page }) => {
  let status = 'ready'
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
  await page.route('**/api/v1/voices', route => route.fulfill({ json: [] }))
  await page.route('**/api/v1/chapters/1/script', route => route.fulfill({ json: { chapter_id: 1, revision: 0, kind: null, entries: [] } }))
  await page.route('**/api/v1/projects/book-1/jobs', async route => {
    expect((await route.request().postDataJSON()).type).toBe('preprocess')
    status = 'running'
    await route.fulfill({ json: project().latest_job })
  })

  await page.goto('/')
  await page.getByText('测试有声书').click()
  await expect(page.getByRole('button', { name: '开始 TTS' })).toBeDisabled()
  const navBox = await page.getByRole('navigation', { name: '项目内容' }).boundingBox()
  const workspaceBox = await page.locator('.workspace-grid').boundingBox()
  expect(navBox!.x).toBeLessThan(workspaceBox!.x)
  await page.getByRole('button', { name: /角色池/ }).click()
  await expect(page.getByRole('heading', { name: '项目角色池' })).toBeVisible()
  await page.getByRole('button', { name: /产物/ }).click()
  await expect(page.getByText('还没有可下载的音频产物。')).toBeVisible()
  await page.getByRole('button', { name: /日志/ }).click()
  await expect(page.getByRole('heading', { name: '活动日志' })).toBeVisible()
  await page.getByRole('button', { name: '章节 1', exact: true }).click()
  await page.getByRole('button', { name: '开始预解析' }).click()
  await expect(page.getByRole('button', { name: '暂停' })).toBeVisible()
})
