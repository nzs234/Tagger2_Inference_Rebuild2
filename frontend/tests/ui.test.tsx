import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { ConfirmDialog, DialogLayer } from '../src/components/ui'

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

function ConfirmHarness() {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  return (
    <div className="app-shell">
      <button type="button" onClick={() => setOpen(true)}>删除资源</button>
      {open && <ConfirmDialog
        title="删除资源？"
        detail="删除后无法撤销。"
        confirmLabel="删除"
        busy={busy}
        onClose={() => setOpen(false)}
        onConfirm={() => setBusy(true)}
      />}
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

  it('treats alertdialog confirmations like dialogs and focuses cancel by default', async () => {
    render(<ConfirmHarness />)
    const trigger = screen.getByRole('button', { name: '删除资源' })
    trigger.focus()
    fireEvent.click(trigger)

    await screen.findByRole('alertdialog', { name: '删除资源？' })
    const cancel = screen.getByRole('button', { name: '取消' })
    const confirm = screen.getByRole('button', { name: '删除' })
    await waitFor(() => expect(cancel).toHaveFocus())

    confirm.focus()
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(cancel).toHaveFocus()
    cancel.focus()
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true })
    expect(confirm).toHaveFocus()

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('alertdialog', { name: '删除资源？' })).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
  })

  it('does not close a busy confirmation from Escape or the backdrop', async () => {
    render(<ConfirmHarness />)
    fireEvent.click(screen.getByRole('button', { name: '删除资源' }))
    await screen.findByRole('alertdialog', { name: '删除资源？' })
    fireEvent.click(screen.getByRole('button', { name: '删除' }))

    expect(screen.getByRole('button', { name: '删除' })).toBeDisabled()
    fireEvent.keyDown(document, { key: 'Escape' })
    const backdrop = document.querySelector('.drawer-backdrop')
    expect(backdrop).toBeInTheDocument()
    fireEvent.mouseDown(backdrop as HTMLElement)
    expect(screen.getByRole('alertdialog', { name: '删除资源？' })).toBeInTheDocument()
  })
})
