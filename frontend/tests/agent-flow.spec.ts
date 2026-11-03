import { expect, test, type Page } from '@playwright/test'

async function setup(page: Page) {
  const script = { chapter_id: 1, revision: 1, kind: 'reviewed', entries: [{ speaker: 'NARRATOR', text: '他望向窗外。', instruct: '平静' }] }
  const runs: any[] = []
  const messages: any[] = []
  const decisions: any[] = []
  const conversations = [{ id: 'default', title: '历史对话', created_at: new Date().toISOString() }]
  const chapters = [{ id: 1, included: true, included_position: 1, position: 1, title: '第一章 · 窗外', status: 'reviewed', active_revision_id: 'revision-1' }]
  let pending = false
  let disconnect = false
  const settings = { agent_auto_apply_scripts: false }
  const automations: any[] = []
  let batch = false
  const proposal = () => ({ id: 'proposal-1', kind: 'script', status: 'pending', args: { chapter_id: 1, expected_revision: 1, summary: '将旁白语气调整得更沉稳', before: structuredClone(script.entries), entries: [{ speaker: 'NARRATOR', text: '他望向窗外。', instruct: '沉稳、舒缓' }], text_changed: false }, result: {} })
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/projects/book/automations') return route.fulfill({ json: { automations } })
    if (path.startsWith('/api/v1/projects/book/automations/')) {
      const id = path.split('/').at(-1)
      const index = automations.findIndex(plan => plan.id === id)
      if (route.request().method() === 'DELETE') { automations.splice(index, 1); return route.fulfill({ status: 204 }) }
      Object.assign(automations[index], route.request().postDataJSON())
      return route.fulfill({ json: automations[index] })
    }
    if (path.endsWith('/agent/messages')) {
      const body = route.request().postDataJSON()
      messages.push(body)
      let run = runs.find(item => item.request_id === body.request_id)
      if (!run) {
        const action = batch ? { id: 'batch-1', kind: 'script_batch', status: 'applied', args: { summary: '删除重复开头', changes: [{ chapter_id: 1, expected_revision: script.revision, before: structuredClone(script.entries), entries: [{ speaker: 'NARRATOR', text: '正文保留。', instruct: '平静' }] }], skipped_count: 2 }, result: { modified_count: 1, skipped_count: 2 } } : pending ? proposal() : { id: 'op-1', kind: 'task', status: 'completed', args: {}, result: { id: 'task-1' } }
        run = { id: `run-${runs.length}`, request_id: body.request_id, conversation_id: body.conversation_id, message: body.message, status: 'completed', auto_apply_scripts: settings.agent_auto_apply_scripts, output: batch ? '批量修改已保存。' : pending ? '已生成修改提案，请确认后保存。' : '已提交任务，进度请查看项目面板。', created_at: new Date().toISOString(), action }
        runs.push(run)
        const conversation = conversations.find(item => item.id === body.conversation_id)
        if (conversation?.title === '新对话') conversation.title = body.message.slice(0, 40)
        if (batch) { script.revision++; script.entries = structuredClone(action.args.changes![0]!.entries) }
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
    if (path.endsWith('/agent/conversations')) {
      if (route.request().method() === 'POST') {
        const conversation = { id: `conversation-${conversations.length}`, title: '新对话', created_at: new Date().toISOString() }
        conversations.push(conversation)
        return route.fulfill({ status: 201, json: conversation })
      }
      return route.fulfill({ json: { conversations } })
    }
    if (path.includes('/agent/conversations/')) {
      const conversation = conversations.find(item => item.id === path.split('/').at(-1))!
      conversation.title = route.request().postDataJSON().title
      return route.fulfill({ json: conversation })
    }
    if (path.endsWith('/agent')) {
      const id = new URL(route.request().url()).searchParams.get('conversation_id') || 'default'
      if (!conversations.some(item => item.id === id)) return route.fulfill({ status: 404, json: { detail: 'Resource not found' } })
      return route.fulfill({ json: { runs: runs.filter(run => (run.conversation_id || 'default') === id), conversations,
        conversation_id: id, project_busy: runs.some(run => run.status === 'running') } })
    }
    if (path === '/api/v1/projects/book' && route.request().method() === 'PATCH') Object.assign(settings, route.request().postDataJSON())
    if (path === '/api/v1/chapters/1/script') {
      if (route.request().method() === 'PATCH') { script.revision++; script.entries = route.request().postDataJSON().entries }
      return route.fulfill({ json: script })
    }
    const responses: Record<string, unknown> = {
      '/api/v1/projects/book': { id: 'book', title: 'Agent 测试书', source_filename: '测试书.txt', status: 'ready_for_tts', latest_job: { id: 'completed-job', type: 'preprocess', status: 'completed', payload: {} }, settings, chapter_count: 1 },
      '/api/v1/projects/book/chapters': chapters.map(chapter => ({ ...chapter, active_revision_id: `revision-${script.revision}` })),
      '/api/v1/projects/book/voice-pool': { excluded_voice_ids: [], narrator_voice_profile_id: null },
      '/api/v1/projects/book/bilibili': { chapters: [], form: {}, continuous_audio_count: 0 },
    }
    return route.fulfill({ json: responses[path] || [] })
  })
  await page.goto('/projects/book')
  await expect(page.getByLabel('文本', { exact: true })).toHaveValue('他望向窗外。')
  return { runs, messages, decisions, conversations, script, chapters, settings, automations, batch: () => { batch = true }, propose: () => { pending = true }, disconnect: () => { disconnect = true } }
}

test('submits a task and restores its receipt after reload', async ({ page }) => {
  const state = await setup(page)
  await page.getByRole('button', { name: 'Agent 助手' }).click()
  await page.getByLabel('给 Agent 的指令').fill('预处理第1章')
  await page.getByRole('button', { name: '发送指令', exact: true }).click()
  await expect(page.getByText('任务编号：task-1')).toBeVisible()
  expect(state.messages[0].selected_chapter_id).toBe(1)
  expect(state.messages[0].allow_text_rewrite).toBeUndefined()
  await page.reload()
  await page.getByRole('button', { name: 'Agent 助手' }).click()
  await expect(page.getByText('任务编号：task-1')).toBeVisible()
  expect(state.messages).toHaveLength(1)
})

test('new conversations isolate history, preserve drafts, and restore selection', async ({ page }) => {
  const state = await setup(page)
  await page.getByRole('button', { name: 'Agent 助手', exact: true }).click()
  await page.getByLabel('给 Agent 的指令').fill('旧对话的请求')
  await page.getByRole('button', { name: '发送指令', exact: true }).click()
  await expect(page.getByText('任务编号：task-1')).toBeVisible()
  await page.getByLabel('给 Agent 的指令').fill('旧对话的草稿')
  await page.getByRole('button', { name: '新建对话', exact: true }).click()
  await expect(page.getByLabel('会话列表')).toHaveValue('conversation-1')
  await expect(page.getByText('想从哪里开始？')).toBeVisible()
  await expect(page.getByLabel('给 Agent 的指令')).toHaveValue('')
  await expect(page.getByText('旧对话的请求', { exact: true })).toHaveCount(0)
  await page.getByLabel('给 Agent 的指令').fill('新对话草稿')
  await page.getByLabel('会话列表').selectOption('default')
  await expect(page.getByLabel('给 Agent 的指令')).toHaveValue('旧对话的草稿')
  await expect(page.getByText('旧对话的请求', { exact: true })).toBeVisible()
  await page.getByLabel('会话列表').selectOption('conversation-1')
  await expect(page.getByLabel('给 Agent 的指令')).toHaveValue('新对话草稿')
  await page.getByRole('button', { name: '发送指令', exact: true }).click()
  await expect(page.getByLabel('会话列表')).toContainText('新对话草稿')
  expect(state.messages[1].conversation_id).toBe('conversation-1')
  await page.getByRole('button', { name: '重命名对话' }).click()
  await page.getByLabel('对话名称').fill('B站上传安排')
  await page.locator('.agent-rename').getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByLabel('会话列表')).toContainText('B站上传安排')
  await page.screenshot({ path: 'test-results/agent-conversations.png' })
  await page.reload()
  await page.getByRole('button', { name: 'Agent 助手', exact: true }).click()
  await expect(page.getByLabel('会话列表')).toHaveValue('conversation-1')
  await expect(page.getByText('新对话草稿', { exact: true })).toBeVisible()
  await expect(page.getByText('旧对话的请求', { exact: true })).toHaveCount(0)
})

test('another running conversation keeps new conversation read-only until idle', async ({ page }) => {
  const state = await setup(page)
  state.runs.push({ id: 'running', conversation_id: 'default', message: '执行中', status: 'running', action: null })
  await page.getByRole('button', { name: 'Agent 助手', exact: true }).click()
  await page.getByRole('button', { name: '新建对话', exact: true }).click()
  await expect(page.getByLabel('会话列表')).toHaveValue('conversation-1')
  await expect(page.getByLabel('给 Agent 的指令')).toBeDisabled()
  state.runs[0].status = 'completed'
  await page.getByRole('button', { name: '刷新对话' }).click()
  await expect(page.getByLabel('给 Agent 的指令')).toBeEnabled()
})

test('stale saved conversation falls back to project history', async ({ page }) => {
  await setup(page)
  await page.evaluate(() => localStorage.setItem('alexandria-agent-conversation:book', 'missing'))
  await page.reload()
  await page.getByRole('button', { name: 'Agent 助手', exact: true }).click()
  await expect(page.getByLabel('会话列表')).toHaveValue('default')
  await expect(page.getByText('想从哪里开始？')).toBeVisible()
  await expect(page).toHaveURL('/projects/book')
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
  await page.getByRole('dialog', { name: '项目 Agent' }).getByRole('button', { name: '放弃人工编辑' }).click()
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


test('floating agent keeps drafts, context, and pending proposals while switching tabs', async ({ page }) => {
  const state = await setup(page)
  state.propose()
  const launcher = page.getByRole('button', { name: 'Agent 助手', exact: true })
  const dialog = page.getByRole('dialog', { name: '项目 Agent' })
  await expect(dialog).toBeHidden()
  await expect(page.getByRole('navigation', { name: '项目内容' }).getByRole('button', { name: 'Agent 助手' })).toHaveCount(0)
  await launcher.click()
  await page.getByLabel('给 Agent 的指令').fill('请修改语气')
  await page.getByRole('button', { name: '收起 Agent' }).click()
  await expect(launcher).toBeFocused()
  await page.getByRole('button', { name: '角色池 0' }).click()
  await launcher.click()
  await expect(page.getByLabel('给 Agent 的指令')).toHaveValue('请修改语气')
  await expect(page.getByText('当前章：第一章 · 窗外')).toBeVisible()
  await page.getByLabel('给 Agent 的指令').press('Enter')
  await expect(page.getByText('脚本修改提案 · 待确认')).toBeVisible()
  await page.getByRole('button', { name: '收起 Agent' }).click()
  await launcher.click()
  await expect(page.getByText('脚本修改提案 · 待确认')).toBeVisible()
  await page.getByLabel('给 Agent 的指令').press('Escape')
  await expect(dialog).toBeHidden()
  expect(state.messages).toHaveLength(1)
})

test('floating agent moves, resizes, and stays within desktop and narrow viewports', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 })
  await setup(page)
  await page.getByRole('button', { name: 'Agent 助手', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '项目 Agent' })
  const initial = (await dialog.boundingBox())!
  const header = (await page.locator('.agent-title').boundingBox())!
  await page.mouse.move(header.x + 25, header.y + 15)
  await page.mouse.down()
  await page.mouse.move(header.x - 155, header.y + 45, { steps: 5 })
  await page.mouse.up()
  const moved = (await dialog.boundingBox())!
  expect(moved.x).toBeLessThan(initial.x - 100)
  const handle = (await page.getByRole('button', { name: '调整 Agent 窗口大小，使用方向键' }).boundingBox())!
  await page.mouse.move(handle.x + 8, handle.y + 8)
  await page.mouse.down()
  await page.mouse.move(handle.x + 88, handle.y + 58, { steps: 5 })
  await page.mouse.up()
  expect((await dialog.boundingBox())!.width).toBeGreaterThan(moved.width + 60)
  await page.screenshot({ path: 'test-results/agent-floating-desktop.png' })
  await page.setViewportSize({ width: 390, height: 740 })
  const narrow = (await dialog.boundingBox())!
  expect(narrow.x).toBeGreaterThanOrEqual(0)
  expect(narrow.x + narrow.width).toBeLessThanOrEqual(390)
  expect(narrow.y + narrow.height).toBeLessThanOrEqual(740)
  await expect(page.getByLabel('给 Agent 的指令')).toBeVisible()
  await expect(page.getByRole('button', { name: '收起 Agent' })).toBeVisible()
  await page.screenshot({ path: 'test-results/agent-floating-narrow.png' })
})

test('composer uses Enter to send and preserves IME and Shift+Enter input', async ({ page }) => {
  const state = await setup(page)
  await page.getByRole('button', { name: 'Agent 助手', exact: true }).click()
  const input = page.getByLabel('给 Agent 的指令')
  await input.fill('查询进度')
  await input.dispatchEvent('keydown', { key: 'Enter', isComposing: true })
  expect(state.messages).toHaveLength(0)
  await input.press('Shift+Enter')
  await expect(input).toHaveValue('查询进度\n')
  await input.press('Enter')
  await expect.poll(() => state.messages.length).toBe(1)
})

test('script and chapter scrolling are independent and switching chapters resets only the script', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  const state = await setup(page)
  for (let id = 2; id <= 60; id++) state.chapters.push({ ...state.chapters[0]!, id, position: id, included_position: id, title: `第${id}章` })
  for (let i = 1; i < 70; i++) state.script.entries.push({ speaker: '青年', text: `台词 ${i}`, instruct: '平静' })
  await page.route('**/api/v1/chapters/2/script', route => route.fulfill({ json: { ...state.script, chapter_id: 2 } }))
  await page.reload()
  const entries = page.getByLabel('脚本正文', { exact: true })
  const chapters = page.getByLabel('章节列表', { exact: true })
  await expect(entries).toBeVisible()
  await entries.evaluate(element => { element.scrollTop = element.scrollHeight })
  const scrollY = await page.evaluate(() => window.scrollY)
  const listTop = await chapters.evaluate(element => element.scrollTop)
  const entryBox = (await entries.boundingBox())!
  await page.mouse.move(entryBox.x + entryBox.width - 12, entryBox.y + 60)
  await page.mouse.wheel(0, 1500)
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(scrollY)
  expect(await chapters.evaluate(element => element.scrollTop)).toBe(listTop)
  await expect(page.getByRole('button', { name: '保存脚本', exact: true })).toBeInViewport()
  await chapters.evaluate(element => { element.scrollTop = 30 })
  await page.getByRole('button', { name: /02 第2章/ }).click()
  await expect.poll(() => entries.evaluate(element => element.scrollTop)).toBe(0)
  expect(await chapters.evaluate(element => element.scrollTop)).toBe(30)
  await page.screenshot({ path: 'test-results/script-independent-scroll.png' })
  await page.setViewportSize({ width: 390, height: 740 })
  expect(await entries.evaluate(element => element.scrollHeight > element.clientHeight)).toBe(true)
  expect((await chapters.boundingBox())!.height).toBeLessThan(240)
})


test('new messages follow the bottom without pulling the reader away from older messages', async ({ page }) => {
  const state = await setup(page)
  for (let i = 0; i < 12; i++) state.runs.push({ id: `history-${i}`, message: `问题 ${i}`, output: '一段较长的答复。'.repeat(20), status: 'completed', created_at: new Date().toISOString() })
  await page.getByRole('button', { name: 'Agent 助手', exact: true }).click()
  const history = page.getByRole('log', { name: 'Agent 对话记录' })
  await expect.poll(() => history.evaluate(element => element.scrollTop)).toBeGreaterThan(0)
  await history.evaluate(element => { element.scrollTop = 0; element.dispatchEvent(new Event('scroll')) })
  state.runs.push({ ...state.runs[0], id: 'new-1', message: '新增消息一' })
  await page.getByRole('button', { name: '刷新对话', exact: true }).click()
  await expect(page.getByText('新增消息一', { exact: true })).toHaveCount(1)
  expect(await history.evaluate(element => element.scrollTop)).toBe(0)
  await history.evaluate(element => { element.scrollTop = element.scrollHeight; element.dispatchEvent(new Event('scroll')) })
  state.runs.push({ ...state.runs[0], id: 'new-2', message: '新增消息二', output: '底部回复' })
  await page.getByRole('button', { name: '刷新对话', exact: true }).click()
  await expect(page.getByText('底部回复', { exact: true })).toBeInViewport()
})

test('closing the window keeps polling the active request until completion', async ({ page }) => {
  const state = await setup(page)
  const run = { id: 'running-1', message: '查询进度', output: '', status: 'running', created_at: new Date().toISOString() }
  state.runs.push(run)
  await page.getByRole('button', { name: 'Agent 助手', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Agent 处理中', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: '收起 Agent' }).click()
  run.output = '后台处理已完成'
  run.status = 'completed'
  await expect(page.locator('.agent-live-dot')).toHaveCount(0)
  await page.getByRole('button', { name: 'Agent 助手', exact: true }).click()
  await expect(page.getByText('后台处理已完成')).toBeVisible()
})

test('batch saves need no permission flags and survive a lost response', async ({ page }) => {
  const state = await setup(page)
  state.batch()
  await page.getByRole('button', { name: 'Agent 助手', exact: true }).click()
  await expect(page.getByLabel('允许直接修改本项目脚本')).toHaveCount(0)
  await expect(page.getByLabel('本条指令允许改写正文')).toHaveCount(0)
  state.disconnect()
  await page.getByLabel('给 Agent 的指令').fill('批量删除章节开头的重复书名')
  await page.getByRole('button', { name: '发送指令', exact: true }).click()
  await expect(page.locator('.agent-error')).toBeVisible()
  await page.getByRole('button', { name: '发送指令', exact: true }).click()
  await expect(page.getByText('批量脚本修改 · 已保存')).toBeVisible()
  await expect(page.getByText('修改 1 章 · 跳过 2 章')).toBeVisible()
  await expect(page.getByRole('button', { name: '确认保存脚本' })).toHaveCount(0)
  await page.locator('.agent-diff summary').click()
  await expect(page.locator('.agent-diff pre').last()).toContainText('正文保留。')
  await expect(page.getByLabel('文本', { exact: true })).toHaveValue('正文保留。')
  expect(state.messages).toHaveLength(2)
  expect(state.messages[0].request_id).toBe(state.messages[1].request_id)
  expect(state.script.revision).toBe(2)
  expect(state.decisions).toHaveLength(0)
  await page.reload()
  await page.getByRole('button', { name: 'Agent 助手', exact: true }).click()
  await expect(page.getByText('批量脚本修改 · 已保存')).toBeVisible()
})

test('conversation and draft do not leak into another project', async ({ page }) => {
  const state = await setup(page)
  state.settings.agent_auto_apply_scripts = true
  await page.reload()
  await page.getByRole('button', { name: 'Agent 助手', exact: true }).click()
  await expect(page.getByLabel('允许直接修改本项目脚本')).toHaveCount(0)
  await page.getByLabel('给 Agent 的指令').fill('上一个项目草稿')
  await page.route('**/api/v1/projects/other**', async route => {
    const path = new URL(route.request().url()).pathname
    const json = path.endsWith('/other') ? { id: 'other', title: '另一项目', settings: { agent_auto_apply_scripts: false }, latest_job: null, chapter_count: 0 }
      : path.endsWith('/agent') ? { runs: [] } : path.endsWith('/voice-pool') ? {} : []
    await route.fulfill({ json })
  })
  await page.goto('/projects/other')
  await page.getByRole('button', { name: 'Agent 助手', exact: true }).click()
  await expect(page.getByLabel('允许直接修改本项目脚本')).toHaveCount(0)
  await expect(page.getByLabel('给 Agent 的指令')).toHaveValue('')
})

test('batch completion preserves unsaved local edits', async ({ page }) => {
  const state = await setup(page)
  state.settings.agent_auto_apply_scripts = true
  await page.reload()
  const run: any = { id: 'running-batch', message: '删除重复书名', output: '', status: 'running', created_at: new Date().toISOString(), action: null }
  state.runs.push(run)
  await page.getByRole('button', { name: 'Agent 助手', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Agent 处理中', exact: true })).toBeDisabled()
  await page.getByLabel('文本', { exact: true }).fill('未保存人工修改')
  state.script.revision = 2
  state.script.entries = [{ speaker: 'NARRATOR', text: '服务器已清理。', instruct: '平静' }]
  run.status = 'completed'
  run.output = '批量完成'
  run.action = { id: 'batch-done', kind: 'script_batch', status: 'applied', args: { changes: [], skipped_count: 0 }, result: { modified_count: 1 } }
  await expect(page.getByText('批量脚本修改 · 已保存')).toBeVisible()
  await expect(page.getByLabel('文本', { exact: true })).toHaveValue('未保存人工修改')
  await expect(page.getByText('章节有未保存的人工编辑', { exact: false })).toBeVisible()
})


test('all operations are shown in order and restored after reload', async ({ page }) => {
  const state = await setup(page)
  const actions = [
    { id: 'a-1', kind: 'project', status: 'completed', args: { title: '新标题' }, result: {} },
    { id: 'a-2', kind: 'task', status: 'completed', args: {}, result: { id: 'upload-10' } },
  ]
  state.runs.push({ id: 'multi-run', message: '改标题并追加10个分P', status: 'completed', output: '已修改标题并提交追加任务。', actions, action: actions[0] })
  await page.getByRole('button', { name: 'Agent 助手', exact: true }).click()
  await expect(page.locator('.agent-action')).toHaveCount(2)
  await expect(page.locator('.agent-action').first()).toContainText('操作已完成')
  await expect(page.locator('.agent-action').last()).toContainText('任务编号：upload-10')
  await page.reload()
  await page.getByRole('button', { name: 'Agent 助手', exact: true }).click()
  await expect(page.locator('.agent-action')).toHaveCount(2)
})

test('project schedules show results and support pause resume and delete', async ({ page }) => {
  const state = await setup(page)
  state.automations.push({ id: 'plan-1', instruction: '给当前稿件追加最多10个已准备的分P', interval_seconds: 600, enabled: true,
    next_run_at: '2026-10-29T02:10:00Z', selected_chapter_id: 1, paused_reason: '', last_run_id: 'scheduled-run',
    last_run: { id: 'scheduled-run', status: 'completed', output: '已提交10个分P的追加任务。', created_at: '2026-10-29T02:00:00Z' } })
  await page.getByRole('button', { name: '定时任务', exact: true }).click()
  await expect(page.getByText('已启用 · 每 10 分钟')).toBeVisible()
  await expect(page.getByText('已提交10个分P的追加任务。', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '暂停计划 plan-1', exact: true }).click()
  await expect(page.getByText('已暂停 · 每 10 分钟')).toBeVisible()
  state.automations[0].paused_reason = 'B站账号已过期，请重新登录'
  await page.getByRole('button', { name: '刷新定时任务' }).click()
  await expect(page.getByText('B站账号已过期，请重新登录')).toBeVisible()
  await page.screenshot({ path: 'test-results/project-automations.png', fullPage: true })
  await page.getByRole('button', { name: '恢复计划 plan-1', exact: true }).click()
  await expect(page.getByText('已启用 · 每 10 分钟')).toBeVisible()
  await page.getByRole('button', { name: '删除计划 plan-1', exact: true }).click()
  await expect(page.getByText('暂无定时任务。')).toBeVisible()
})

test('project deletion stops agent polling and returns to project list', async ({ page }) => {
  await setup(page)
  await page.route('**/api/v1/projects', route => route.fulfill({ json: [] }))
  let deleted = false
  await page.route('**/api/v1/projects/book/agent**', async route => {
    if (route.request().method() === 'POST') {
      deleted = true
      return route.fulfill({ status: 202, json: { id: 'delete-run', status: 'running', message: '删除项目', action: null, actions: [] } })
    }
    return route.fulfill({ status: deleted ? 404 : 200, json: deleted ? { detail: 'Not found' } : { runs: [] } })
  })
  await page.getByRole('button', { name: 'Agent 助手', exact: true }).click()
  await page.getByLabel('给 Agent 的指令').fill('删除当前项目')
  await page.getByRole('button', { name: '发送指令', exact: true }).click()
  await expect(page).toHaveURL('/')
  await expect(page.getByRole('dialog', { name: '项目 Agent' })).toHaveCount(0)
})
