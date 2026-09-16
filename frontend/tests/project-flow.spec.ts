import { expect, test } from '@playwright/test'

test('opens a project, starts it, and exposes pipeline controls', async ({ page }) => {
  let status = 'ready'
  const project = () => ({
    id: 'book-1', title: '测试有声书', source_filename: 'book.epub', status,
    chapter_count: 1, chapter_status_counts: {}, created_at: '', updated_at: '',
    settings: { render_start_mode: 'after_review_batch', release_batch_size: 3, from_chapter: null, to_chapter: null, first_person_speaker: null, context_window: 0, single_speaker: false, speaker_name: 'NARRATOR', instruct: '', workers: 2 },
    latest_job: status === 'ready' ? null : { id: 'job-1', project_id: 'book-1', status, render_start_mode: 'after_review_batch', release_batch_size: 3, error: null },
  })
  await page.route('**/api/v1/projects', route => route.fulfill({ json: [project()] }))
  await page.route('**/api/v1/projects/book-1', route => route.fulfill({ json: project() }))
  await page.route('**/api/v1/projects/book-1/chapters', route => route.fulfill({ json: [{ id: 1, project_id: 'book-1', position: 1, title: '第一章', status: 'pending', active_revision_id: null }] }))
  await page.route('**/api/v1/projects/book-1/artifacts', route => route.fulfill({ json: [] }))
  await page.route('**/api/v1/projects/book-1/events', route => route.fulfill({ json: [] }))
  await page.route('**/api/v1/projects/book-1/speaker-assignments', route => route.fulfill({ json: [] }))
  await page.route('**/api/v1/chapters/1/script', route => route.fulfill({ json: { chapter_id: 1, revision: 0, kind: null, entries: [] } }))
  await page.route('**/api/v1/projects/book-1/jobs', async route => { status = 'running'; await route.fulfill({ json: project().latest_job }) })

  await page.goto('/')
  await page.getByText('测试有声书').click()
  await expect(page.getByLabel('渲染开始')).toHaveValue('after_review_batch')
  await expect(page.getByLabel('每批章节数')).toHaveValue('3')
  await page.getByRole('button', { name: '开始流水线' }).click()
  await expect(page.getByRole('button', { name: '暂停' })).toBeVisible()
})
