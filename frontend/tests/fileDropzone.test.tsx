import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { FileDropzone } from '../src/components/FileDropzone'

function image(name: string) {
  return new File([name], name, { type: 'image/png' })
}

describe('FileDropzone', () => {
  it('accepts only the first image when single-image mode is enabled', () => {
    const onFiles = vi.fn()
    const { container } = render(<FileDropzone multiple={false} onFiles={onFiles} />)
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    const first = image('first.png')
    const second = new File(['second'], 'second.jpg', { type: 'image/jpeg' })

    Object.defineProperty(input, 'files', { configurable: true, value: [first, second] })
    fireEvent.change(input)

    expect(input.multiple).toBe(false)
    expect(onFiles).toHaveBeenCalledWith([first])
  })

  it('uses the latest callback and maxFiles value after rerender', () => {
    const initialCallback = vi.fn()
    const latestCallback = vi.fn()
    const { container, rerender } = render(
      <FileDropzone maxFiles={3} onFiles={initialCallback} />,
    )
    rerender(<FileDropzone maxFiles={1} onFiles={latestCallback} />)
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    const first = image('first.png')
    const second = image('second.png')

    Object.defineProperty(input, 'files', { configurable: true, value: [first, second] })
    fireEvent.change(input)

    expect(initialCallback).not.toHaveBeenCalled()
    expect(latestCallback).toHaveBeenCalledWith([first])
  })

  it('does not intercept paste events from editable controls', () => {
    const onFiles = vi.fn()
    render(<><input aria-label="说明" /><textarea aria-label="备注" /><FileDropzone onFiles={onFiles} /></>)
    const pasted = image('pasted.png')

    fireEvent.paste(screen.getByRole('textbox', { name: '说明' }), {
      clipboardData: { files: [pasted] },
    })
    fireEvent.paste(screen.getByRole('textbox', { name: '备注' }), {
      clipboardData: { files: [pasted] },
    })

    expect(onFiles).not.toHaveBeenCalled()

    fireEvent.paste(window, { clipboardData: { files: [pasted] } })
    expect(onFiles).toHaveBeenCalledWith([pasted])
  })

  it.each(['Enter', ' '])('opens the file picker with the %s key', (key) => {
    const { container } = render(<FileDropzone label="上传图片" onFiles={vi.fn()} />)
    const dropzone = screen.getByRole('button', { name: '上传图片' })
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    const click = vi.spyOn(input, 'click').mockImplementation(() => undefined)

    const dispatched = fireEvent.keyDown(dropzone, { key })

    expect(dispatched).toBe(false)
    expect(click).toHaveBeenCalledOnce()
  })

  it('keeps every interaction inactive while disabled', () => {
    const onFiles = vi.fn()
    const { container } = render(
      <FileDropzone disabled label="上传图片" onFiles={onFiles} />,
    )
    const dropzone = screen.getByRole('button', { name: '上传图片' })
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    const click = vi.spyOn(input, 'click').mockImplementation(() => undefined)
    const pasted = image('pasted.png')

    fireEvent.click(dropzone)
    fireEvent.keyDown(dropzone, { key: 'Enter' })
    fireEvent.paste(window, { clipboardData: { files: [pasted] } })
    fireEvent.drop(dropzone, { dataTransfer: { files: [pasted] } })

    expect(dropzone).toHaveAttribute('aria-disabled', 'true')
    expect(dropzone).toHaveAttribute('tabindex', '-1')
    expect(input).toBeDisabled()
    expect(click).not.toHaveBeenCalled()
    expect(onFiles).not.toHaveBeenCalled()
  })
})
