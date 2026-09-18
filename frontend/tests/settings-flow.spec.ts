import { expect, test } from '@playwright/test'

const settings = {
  llm_base_url: 'http://localhost:1234/v1', llm_api_key: '', llm_model: 'local-model',
  fish_base_url: 'https://api.fish.audio', fish_api_key: '', fish_model: 's1', fish_workers: 5,
  generation: { chunk_size: 3000, max_tokens: 4096, review_batch_size: 25 },
  prompts: { system_prompt: 'generate system', user_prompt: 'generate user', review_system_prompt: 'review system', review_user_prompt: 'review user' },
  fish_tts: { format: 'mp3', sample_rate: 44100, mp3_bitrate: 128, temperature: 0.7, top_p: 0.7, latency: 'normal', condition_on_previous_chunks: true, timeout_seconds: 300 },
}

test('paginates settings, retains edits, and saves voice gender radio', async ({ page }) => {
  let savedSettings: typeof settings | undefined
  let savedVoices: unknown
  const voices = [{ id: 'voice-1', reference_id: 'ref-1', name: '音色一', pool_order: 0, gender: '', traits: '沉稳' }]

  await page.route('**/api/v1/settings', async route => {
    if (route.request().method() === 'PUT') savedSettings = route.request().postDataJSON()
    await route.fulfill({ json: settings })
  })
  await page.route('**/api/v1/voices', async route => {
    if (route.request().method() === 'PUT') {
      savedVoices = route.request().postDataJSON()
      await route.fulfill({ json: (savedVoices as { voices: unknown[] }).voices })
      return
    }
    await route.fulfill({ json: voices })
  })

  await page.goto('/settings')
  const nav = page.getByRole('navigation', { name: '配置分类' })
  await expect(nav).toBeVisible()
  await page.getByLabel('服务地址').first().fill('https://fish.example.test')
  await nav.getByRole('button', { name: 'Prompt' }).click()
  await expect(page.getByLabel('Generate System')).toHaveValue('generate system')
  await nav.getByRole('button', { name: '服务连接' }).click()
  await expect(page.getByLabel('服务地址').first()).toHaveValue('https://fish.example.test')

  await nav.getByRole('button', { name: '音色池' }).click()
  await page.getByRole('radio', { name: '女' }).check()
  await page.getByRole('button', { name: '保存设置与音色' }).click()
  await expect(page.getByText('设置已保存')).toBeVisible()
  expect(savedSettings?.fish_base_url).toBe('https://fish.example.test')
  expect(savedVoices).toMatchObject({ voices: [{ gender: '女' }] })

  const savebarPosition = await page.locator('.settings-savebar').evaluate(element => getComputedStyle(element).position)
  expect(savebarPosition).toBe('sticky')
  await page.setViewportSize({ width: 390, height: 844 })
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
  await expect(nav).toHaveCSS('display', 'flex')
})

test('adds a voice from only its Fish id without rendering sample text', async ({ page }) => {
  const voices = [{ id: 'voice-1', reference_id: 'existing', name: '已有音色', pool_order: 0, gender: '', traits: '', enabled: true }]
  let validationCalls = 0

  await page.route('**/api/v1/settings', route => route.fulfill({ json: settings }))
  await page.route('**/api/v1/voices', route => route.fulfill({ json: voices }))
  await page.route('**/api/v1/voices/validate', async route => {
    validationCalls += 1
    await route.fulfill({ json: {
      reference_id: 'new-reference', name: '自动名称', gender: '女', traits: 'young、温柔',
      sample_available: true, sample_reference_id: 'new-reference', sample_title: '不应显示的标题',
      sample_text: '不应显示的示例文字', sample_url: '/sample.wav',
    } })
  })

  await page.goto('/settings')
  await page.getByRole('navigation', { name: '配置分类' }).getByRole('button', { name: '音色池' }).click()
  await page.getByRole('button', { name: '添加音色' }).click()
  await page.getByLabel('Fish Reference ID').fill('existing')
  await page.getByRole('button', { name: '校验并添加' }).click()
  await expect(page.getByText('该音色已在音色池中')).toBeVisible()
  expect(validationCalls).toBe(0)

  await page.getByLabel('Fish Reference ID').fill('new-reference')
  await page.getByRole('button', { name: '校验并添加' }).click()
  await expect(page.getByRole('dialog')).toBeHidden()
  const addedRow = page.locator('.voice-row').last()
  await expect(addedRow.locator(':scope > input').nth(0)).toHaveValue('自动名称')
  await expect(addedRow.locator(':scope > input').nth(1)).toHaveValue('young、温柔')
  await expect(page.getByText('不应显示的示例文字')).toHaveCount(0)
  await expect(page.locator('button[title="播放示例"]:not([disabled])')).toHaveCount(1)
})

test('shows Bilibili account state and completes QR login polling', async ({ page }) => {
  await page.route('**/api/v1/settings', route => route.fulfill({ json: settings }))
  await page.route('**/api/v1/voices', route => route.fulfill({ json: [] }))
  await page.route('**/api/v1/bilibili/account', route => route.fulfill({ json: { logged_in: false, name: null } }))
  await page.route('**/api/v1/bilibili/login', route => route.fulfill({ json: { session_id: 'qr-1', url: 'https://example.test/qr', expires_at: '2099-01-01T00:00:00Z' } }))
  await page.route('**/api/v1/bilibili/login/qr-1', route => route.fulfill({ json: { status: 'success', name: '测试账号' } }))

  await page.goto('/settings')
  await page.getByRole('navigation', { name: '配置分类' }).getByRole('button', { name: 'B站账号' }).click()
  await expect(page.getByText('未登录', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '扫码登录' }).click()
  await expect(page.locator('canvas')).toBeVisible()
  await expect(page.getByText('测试账号')).toBeVisible({ timeout: 4000 })
})
