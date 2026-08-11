import { fireEvent, render } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { FileDropzone } from '../src/components/FileDropzone'

describe('FileDropzone', () => {
  it('accepts only the first image when single-image mode is enabled', () => {
    const onFiles = vi.fn()
    const { container } = render(<FileDropzone multiple={false} onFiles={onFiles} />)
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    const first = new File(['first'], 'first.png', { type: 'image/png' })
    const second = new File(['second'], 'second.jpg', { type: 'image/jpeg' })

    Object.defineProperty(input, 'files', { configurable: true, value: [first, second] })
    fireEvent.change(input)

    expect(input.multiple).toBe(false)
    expect(onFiles).toHaveBeenCalledWith([first])
  })
})
