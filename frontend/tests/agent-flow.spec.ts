import { expect, test, type Page } from '@playwright/test'

async function setup(page: Page) {
  const script = { chapter_id: 1, revision: 1, kind: 'reviewed', entries: [{ speaker: 'NARRATOR', text: '他望向窗外。', instruct: '平静' }] }
  const runs: any[] = []
  const messages: any[] = []
  const decisions: any[] = []
  let pending = false
  let disconnect = false
  const proposal = () => ({ id: 'proposal-1', kind: 'script', status: 'pending', args: { chapter_id: 1, expected_revision: 1, summary: '将旁白语气调整得更沉稳', before: structuredClone(script.entries), entries: [{ speaker: 'NARRATOR', text: '他望向窗外。', instruct: '沉稳、舒缓' }], text_changed: false }, result: {} })
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/agent/messages')) {
      const body = route.request().postDataJSON()
      messages.push(body)
      let run = runs.find(item => item.request_id === body.request_id)
      if (!run) {
        run = { id: `run-${runs.length}`, request_id: body.request_id, message: body.message, status: 'completed', output: pending ? '已生成修改提案，请确认后保存。' : '已提交任务，进度请查看项目面板。', created_at: new Date().toISOString(), action: pending ? proposal() : { id: 'op-1', kind: 'task', status: 'completed', args: {}, result: { id: 'task-1' } } }
        runs.push(run)
      }
      if (disconnect) { disconnect = false; return route.abort('failed') }
      return route.fulfill({ status: 202, json: run })
    }
    if (path.endsWith('/agent/proposals/proposal-1')) {
      const body = route.request().postDataJSON()
      decisions.push(body)
      const action = runs.at(-1).action
      action.status = body.approve ? 'applied' : 'rejected'
      if (body.approve) { script.revision++; script.kind = 'manual'; script.entries = structuredClone(action.args.entries) }
      return route.fulfill({ json: action })
    }
    if (path.endsWith('/agent')) return route.fulfill({ json: { runs } })
    if (path === '/api/v1/chapters/1/script') {
      if (route.request().method() === 'PATCH') { script.revision++; script.entries = route.request().postDataJSON().entries }
      return route.fulfill({ json: script })
    }
    const responses: Record<string, unknown> = {
      '/api/v1/projects/book': { id: 'book', title: 'Agent 测试书', source_filename: '测试书.txt', status: 'ready_for_tts', latest_job: { id: 'completed-job', type: 'preprocess', status: 'completed', payload: {} }, settings: {}, chapter_count: 1 },
      '/api/v1/projects/book/chapters': [{ id: 1, included: true, included_position: 1, position: 1, title: '第一章 · 窗外', status: 'reviewed', active_revision_id: `revision-${script.revision}` }],
      '/api/v1/projects/book/voice-pool': { excluded_voice_ids: [], narrator_voice_profile_id: null },
      '/api/v1/projects/book/bilibili': { chapters: [], form: {}, continuous_audio_count: 0 },
    }
    return route.fulfill({ json: responses[path] || [] })
  })
  await page.goto('/projects/book')
  await expect(page.getByLabel('文本', { exact: true })).toHaveValue('他望向窗外。')
  return { runs, messages, decisions, script, propose: () => { pending = true }, disconnect: () => { disconnect = true } }
}

test('submits a task and restores its receipt after reload', async ({ page }) => {
  const state = await setup(page)
  await page.getByRole('button', { name: 'Agent 助手' }).click()
  await page.getByLabel('给 Agent 的指令').fill('预处理第1章')
  await page.getByRole('button', { name: '发送指令', exact: true }).click()
  await expect(page.getByText('任务编号：task-1')).toBeVisible()
  expect(state.messages[0].selected_chapter_id).toBe(1)
  expect(state.messages[0].allow_text_rewrite).toBe(false)
  await page.reload()
  await page.getByRole('button', { name: 'Agent 助手' }).click()
  await expect(page.getByText('任务编号：task-1')).toBeVisible()
  expect(state.messages).toHaveLength(1)
})

test('previews a script change and applies it only after confirmation', async ({ page }) => {
  const state = await setup(page)
  state.propose()
  await page.getByRole('button', { name: 'Agent 助手' }).click()
  await page.getByLabel('给 Agent 的指令').fill('把当前章旁白的语气改得沉稳')
  await page.getByRole('button', { name: '发送指令', exact: true }).click()
  await expect(page.getByText('脚本修改提案 · 待确认')).toBeVisible()
  await expect(page.getByText('修改前', { exact: true })).toBeVisible()
  await expect(page.getByText('修改后', { exact: true })).toBeVisible()
  expect(state.script.revision).toBe(1)
  expect(state.decisions).toHaveLength(0)
  await page.locator('.agent-history').evaluate(element => { element.scrollTop = element.scrollHeight })
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({ path: 'test-results/agent-proposal.png', fullPage: true })
  await page.getByRole('button', { name: '确认保存脚本' }).click()
  await expect(page.getByText('脚本修改提案 · 已保存')).toBeVisible()
  expect(state.script.revision).toBe(2)
  await page.getByRole('navigation', { name: '项目内容' }).getByRole('button', { name: '章节 1', exact: true }).click()
  await expect(page.getByLabel('语气指令')).toHaveValue('沉稳、舒缓')
})

test('protects unsaved human edits and supports rejecting a proposal', async ({ page }) => {
  const state = await setup(page)
  state.propose()
  await page.getByLabel('文本', { exact: true }).fill('人工改稿')
  await page.getByRole('button', { name: 'Agent 助手' }).click()
  await page.getByLabel('给 Agent 的指令').fill('改语气')
  await expect(page.getByRole('button', { name: '发送指令', exact: true })).toBeDisabled()
  await expect(page.getByText('章节有未保存的人工编辑', { exact: false })).toBeVisible()
  await page.getByRole('button', { name: '放弃人工编辑' }).click()
  await page.getByRole('button', { name: '发送指令', exact: true }).click()
  await page.getByRole('button', { name: '拒绝修改' }).click()
  await expect(page.getByText('脚本修改提案 · 已拒绝')).toBeVisible()
  expect(state.script.revision).toBe(1)
})

test('reuses the submission id after a lost response', async ({ page }) => {
  const state = await setup(page)
  state.disconnect()
  await page.getByRole('button', { name: 'Agent 助手' }).click()
  await page.getByLabel('给 Agent 的指令').fill('预处理第1章')
  await page.getByRole('button', { name: '发送指令', exact: true }).click()
  await expect(page.locator('.agent-error')).toBeVisible()
  await page.getByRole('button', { name: '发送指令', exact: true }).click()
  await expect(page.getByText('任务编号：task-1')).toBeVisible()
  expect(state.messages).toHaveLength(2)
  expect(state.messages[0].request_id).toBe(state.messages[1].request_id)
  expect(state.runs).toHaveLength(1)
})
