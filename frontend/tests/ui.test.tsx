import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { DialogLayer } from '../src/components/ui'

function DialogHarness({ onClose }: { onClose?: () => void }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="app-shell">
      <button type="button" onClick={() => setOpen(true)}>打开编辑器</button>
      {open && (
        <DialogLayer onClose={() => { setOpen(false); onClose?.() }}>
          <section role="dialog" aria-label="配置编辑器">
            <button type="button" data-autofocus>第一个操作</button>
            <input aria-label="配置名称" />
            <button type="button">最后一个操作</button>
          </section>
        </DialogLayer>
      )}
    </div>
  )
}

describe('DialogLayer', () => {
  it('isolates the app, traps focus, closes with Escape, and restores focus', async () => {
    const onClose = vi.fn()
    render(<DialogHarness onClose={onClose} />)
    const trigger = screen.getByRole('button', { name: '打开编辑器' })

    trigger.focus()
    fireEvent.click(trigger)

    const appShell = document.querySelector('.app-shell') as HTMLElement
    const first = screen.getByRole('button', { name: '第一个操作' })
    const last = screen.getByRole('button', { name: '最后一个操作' })
    expect(appShell).toHaveAttribute('inert')
    expect(document.body.style.overflow).toBe('hidden')
    await waitFor(() => expect(first).toHaveFocus())

    last.focus()
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(first).toHaveFocus()

    first.focus()
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true })
    expect(last).toHaveFocus()

    fireEvent.keyDown(document, { key: 'Escape' })

    expect(onClose).toHaveBeenCalledOnce()
    expect(screen.queryByRole('dialog', { name: '配置编辑器' })).not.toBeInTheDocument()
    expect(appShell).not.toHaveAttribute('inert')
    expect(document.body.style.overflow).toBe('')
    expect(trigger).toHaveFocus()
  })

  it('closes only when the backdrop itself is pressed', async () => {
    const onClose = vi.fn()
    render(<DialogHarness onClose={onClose} />)
    fireEvent.click(screen.getByRole('button', { name: '打开编辑器' }))
    await screen.findByRole('dialog', { name: '配置编辑器' })

    fireEvent.mouseDown(screen.getByRole('dialog', { name: '配置编辑器' }))
    expect(onClose).not.toHaveBeenCalled()

    const backdrop = document.querySelector('.drawer-backdrop') as HTMLElement
    fireEvent.mouseDown(backdrop)
    expect(onClose).toHaveBeenCalledOnce()
    expect(screen.queryByRole('dialog', { name: '配置编辑器' })).not.toBeInTheDocument()
  })
})
