import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import ProjectCreateDialog from './ProjectCreateDialog.vue'

const { upload } = vi.hoisted(() => ({ upload: vi.fn() }))

vi.mock('../api/client', () => ({
  api: { upload },
}))

describe('ProjectCreateDialog', () => {
  beforeEach(() => upload.mockReset())

  it('creates projects without automatic render controls', () => {
    const wrapper = mount(ProjectCreateDialog)
    expect(wrapper.find('select').exists()).toBe(false)
    expect(wrapper.find('input[type="number"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('渲染开始')
    expect(wrapper.text()).not.toContain('起始章节')
  })

  it('omits legacy project chapter bounds', async () => {
    upload.mockResolvedValue({ id: 'project-1' })
    const wrapper = mount(ProjectCreateDialog)
    const fileInput = wrapper.get('input[type="file"]')
    Object.defineProperty(fileInput.element, 'files', { value: [new File(['epub'], 'book.epub')] })
    await fileInput.trigger('change')
    await wrapper.get('button.primary').trigger('click')

    expect(upload).toHaveBeenCalledOnce()
    const form = upload.mock.calls[0][1] as FormData
    expect(form.has('from_chapter')).toBe(false)
    expect(form.has('to_chapter')).toBe(false)
  })
})
