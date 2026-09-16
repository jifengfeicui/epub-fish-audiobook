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
    expect(wrapper.findAll('input[type="number"]')).toHaveLength(2)
    expect(wrapper.text()).not.toContain('渲染开始')
  })

  it('omits chapter bounds after a number input is cleared', async () => {
    upload.mockResolvedValue({ id: 'project-1' })
    const wrapper = mount(ProjectCreateDialog)
    const fileInput = wrapper.get('input[type="file"]')
    Object.defineProperty(fileInput.element, 'files', { value: [new File(['epub'], 'book.epub')] })
    await fileInput.trigger('change')
    const numberInputs = wrapper.findAll('input[type="number"]')
    await numberInputs[0].setValue('2')
    await numberInputs[0].setValue('')
    await numberInputs[1].setValue('5')
    await numberInputs[1].setValue('')
    await wrapper.get('button.primary').trigger('click')

    expect(upload).toHaveBeenCalledOnce()
    const form = upload.mock.calls[0][1] as FormData
    expect(form.has('from_chapter')).toBe(false)
    expect(form.has('to_chapter')).toBe(false)
  })
})
