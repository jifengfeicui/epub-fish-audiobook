import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import ProjectCreateDialog from './ProjectCreateDialog.vue'

const { upload } = vi.hoisted(() => ({ upload: vi.fn() }))

vi.mock('../api/client', () => ({
  api: { upload },
}))

describe('ProjectCreateDialog', () => {
  beforeEach(() => upload.mockReset())

  it('defaults new projects to three-chapter review batches', () => {
    const wrapper = mount(ProjectCreateDialog)
    expect((wrapper.get('select').element as HTMLSelectElement).value).toBe('after_review_batch')
    expect((wrapper.get('input[type="number"]').element as HTMLInputElement).value).toBe('3')
    expect(wrapper.text()).toContain('分批审校后开始')
  })

  it('omits chapter bounds after a number input is cleared', async () => {
    upload.mockResolvedValue({ id: 'project-1' })
    const wrapper = mount(ProjectCreateDialog)
    const fileInput = wrapper.get('input[type="file"]')
    Object.defineProperty(fileInput.element, 'files', { value: [new File(['epub'], 'book.epub')] })
    await fileInput.trigger('change')
    const numberInputs = wrapper.findAll('input[type="number"]')
    await numberInputs[1].setValue('2')
    await numberInputs[1].setValue('')
    await numberInputs[2].setValue('5')
    await numberInputs[2].setValue('')
    await wrapper.get('button.primary').trigger('click')

    expect(upload).toHaveBeenCalledOnce()
    const form = upload.mock.calls[0][1] as FormData
    expect(form.has('from_chapter')).toBe(false)
    expect(form.has('to_chapter')).toBe(false)
  })
})
