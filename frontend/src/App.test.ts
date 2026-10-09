import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import App from './App.vue'
import { notifyError } from './api/client'

describe('error toast', () => {
  afterEach(() => vi.useRealTimers())

  it('can be closed and disappears after five seconds', async () => {
    vi.useFakeTimers()
    const wrapper = mount(App, { global: { stubs: { RouterLink: { template: '<a><slot /></a>' }, RouterView: true } } })

    notifyError('上传失败')
    await nextTick()
    expect(wrapper.get('[role="alert"]').text()).toContain('上传失败')

    await wrapper.get('[aria-label="关闭错误提示"]').trigger('click')
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)

    notifyError('再次失败')
    await nextTick()
    vi.advanceTimersByTime(5000)
    await nextTick()
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    wrapper.unmount()
  })
})
