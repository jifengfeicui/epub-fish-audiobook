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
  const voices = [{ id: 'voice-1', reference_id: 'ref-1', name: '旁白音色', bound_speaker: 'NARRATOR', pool_order: 0, gender: '', traits: '沉稳' }]

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
