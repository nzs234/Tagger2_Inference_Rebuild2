import type { ButtonHTMLAttributes, ReactElement, ReactNode } from 'react'
import { AlertTriangle, Check, CircleHelp, LoaderCircle, X } from 'lucide-react'
import { useVirtualizer } from '@tanstack/react-virtual'
import { createPortal } from 'react-dom'
import { cloneElement, isValidElement, useCallback, useEffect, useId, useRef, useState } from 'react'
import type { JobState, QueueState } from '../types'

export function Button({
  variant = 'primary',
  size = 'md',
  icon,
  children,
  className,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary' | 'quiet' | 'danger' | 'outline'
  size?: 'sm' | 'md' | 'lg'
  icon?: ReactNode
}) {
  return (
    <button className={`button button-${variant} button-${size}${className ? ` ${className}` : ''}`} {...props}>
      {icon}
      {children && <span>{children}</span>}
    </button>
  )
}

export function IconButton({
  label,
  children,
  variant = 'quiet',
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { label: string; variant?: 'primary' | 'secondary' | 'quiet' | 'danger' }) {
  return (
    <button className={`icon-button icon-button-${variant}`} aria-label={label} title={label} {...props}>
      {children}
    </button>
  )
}

export function Panel({ title, eyebrow, actions, children, className = '', id }: {
  title?: string
  eyebrow?: string
  actions?: ReactNode
  children: ReactNode
  className?: string
  id?: string
}) {
  return (
    <section id={id} className={`panel ${className}`}>
      {(title || eyebrow || actions) && (
        <header className="panel-header">
          <div>
            {eyebrow && <p className="eyebrow">{eyebrow}</p>}
            {title && <h2>{title}</h2>}
          </div>
          {actions && <div className="panel-actions">{actions}</div>}
        </header>
      )}
      {children}
    </section>
  )
}

export function StatusBadge({ state }: { state: JobState | QueueState | string }) {
  const label: Record<string, string> = {
    pending: '等待中',
    waiting_count_review: '等待数量审核',
    waiting_token_review: '等待 Token 审核',
    committing: '提交中',
    pausing: '暂停中',
    rollback_required: '需要回滚',
    restoring: '恢复中',
    completed: '已完成',
    ready: '待处理',
    uploading: '上传中',
    queued: '排队中',
    processing: '处理中',
    done: '完成',
    error: '失败',
    running: '运行中',
    paused: '已暂停',
    cancelling: '取消中',
    cancelled: '已取消',
    succeeded: '成功',
    partial: '部分完成',
    failed: '失败',
    interrupted: '已中断',
    deleting: '清理中',
  }
  const icon = ['running', 'uploading', 'processing', 'queued', 'cancelling', 'committing', 'pausing', 'restoring', 'deleting'].includes(state)
    ? <LoaderCircle size={13} className="spin" aria-hidden="true" />
    : ['done', 'succeeded', 'completed'].includes(state)
      ? <Check size={13} aria-hidden="true" />
      : ['error', 'failed', 'rollback_required'].includes(state)
        ? <AlertTriangle size={13} aria-hidden="true" />
        : null
  return <span className={`status status-${state}`}>{icon}{label[state] ?? state}</span>
}

export function ProgressBar({ value, label }: { value: number; label?: string }) {
  const safe = Math.max(0, Math.min(100, value))
  const rounded = Math.round(safe)
  return (
    <div
      className="progress-wrap"
      role="progressbar"
      aria-label={label ?? '任务进度'}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={rounded}
      aria-valuetext={`${rounded}%`}
    >
      <div className="progress-track"><div className="progress-value" style={{ width: `${safe}%` }} /></div>
      <span className="progress-label" aria-hidden="true">{rounded}%</span>
    </div>
  )
}

export function EmptyState({ icon = <CircleHelp size={20} />, title, detail, action }: {
  icon?: ReactNode
  title: string
  detail?: string
  action?: ReactNode
}) {
  return <div className="empty-state">{icon}<strong>{title}</strong>{detail && <p>{detail}</p>}{action}</div>
}

export function Notice({ tone = 'info', children }: { tone?: 'info' | 'warning' | 'danger' | 'success'; children: ReactNode }) {
  return <div className={`notice notice-${tone}`} role={tone === 'danger' ? 'alert' : 'status'}>{children}</div>
}

export function TagPills({ tags, limit, className = '' }: { tags: string[]; limit?: number; className?: string }) {
  const shown = limit ? tags.slice(0, limit) : tags
  return (
    <div className={`tag-pills ${className}`}>
      {shown.map((tag) => <span className="tag-pill" key={tag}>{tag}</span>)}
      {limit && tags.length > limit && <span className="tag-more">+{tags.length - limit}</span>}
    </div>
  )
}

export function VirtualList<T>({
  items,
  rowHeight = 64,
  height = 360,
  getKey,
  renderRow,
  empty,
}: {
  items: T[]
  rowHeight?: number
  height?: number
  getKey: (item: T, index: number) => string
  renderRow: (item: T, index: number) => ReactNode
  empty?: ReactNode
}) {
  const parentRef = useRef<HTMLDivElement>(null)
  const virtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => rowHeight,
    getItemKey: (index) => getKey(items[index] as T, index),
    overscan: 8,
  })
  if (!items.length && empty) return <>{empty}</>
  return (
    <div ref={parentRef} className="virtual-list" style={{ height }} tabIndex={0} aria-label="项目列表">
      <div style={{ height: virtualizer.getTotalSize(), position: 'relative', width: '100%' }}>
        {virtualizer.getVirtualItems().map((virtual) => {
          const item = items[virtual.index] as T
          return (
            <div key={virtual.key} className="virtual-row" style={{ transform: `translateY(${virtual.start}px)`, height: virtual.size }}>
              {renderRow(item, virtual.index)}
            </div>
          )
        })}
      </div>
    </div>
  )
}

export interface FieldHelp {
  purpose: string
  recommended: string
  note: string
}

export interface FieldHelpLabels {
  button: string
  purpose: string
  recommended: string
  note: string
  close: string
}

type HelpPopoverProps = {
  label: string
  help: FieldHelp
  labels: FieldHelpLabels
}

export function HelpPopover({ label, help, labels }: HelpPopoverProps) {
  const id = useId()
  const triggerRef = useRef<HTMLButtonElement>(null)
  const openTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const closeTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const pointerActivation = useRef(false)
  const [open, setOpen] = useState(false)
  const [position, setPosition] = useState({ top: 0, left: 0 })

  const updatePosition = useCallback(() => {
    const trigger = triggerRef.current
    if (!trigger) return
    const rect = trigger.getBoundingClientRect()
    const width = Math.min(360, Math.max(260, window.innerWidth - 32))
    const left = Math.min(
      Math.max(16, rect.left - 18),
      Math.max(16, window.innerWidth - width - 16),
    )
    const below = rect.bottom + 10
    const estimatedHeight = 360
    const fitsBelow = below + estimatedHeight + 16 <= window.innerHeight
    const fitsAbove = rect.top - estimatedHeight - 10 >= 16
    const top = fitsBelow
      ? below
      : fitsAbove
        ? Math.max(16, rect.top - estimatedHeight - 10)
        : Math.max(16, Math.min(below, window.innerHeight - estimatedHeight - 16))
    setPosition({ top, left })
  }, [])

  const cancelOpen = useCallback(() => {
    if (openTimer.current) clearTimeout(openTimer.current)
    openTimer.current = undefined
  }, [])

  const cancelClose = useCallback(() => {
    if (closeTimer.current) clearTimeout(closeTimer.current)
    closeTimer.current = undefined
  }, [])

  const close = useCallback(() => {
    cancelClose()
    setOpen(false)
  }, [cancelClose])

  const scheduleClose = useCallback(() => {
    cancelClose()
    closeTimer.current = setTimeout(close, 180)
  }, [cancelClose, close])

  const openPopover = useCallback(() => {
    cancelOpen()
    cancelClose()
    updatePosition()
    setOpen(true)
    document.dispatchEvent(new CustomEvent('tagger2-help-open', { detail: id }))
  }, [cancelClose, cancelOpen, id, updatePosition])

  const scheduleOpen = useCallback(() => {
    cancelOpen()
    openTimer.current = setTimeout(openPopover, 250)
  }, [cancelOpen, openPopover])

  useEffect(() => {
    const onOtherHelp = (event: Event) => {
      if ((event as CustomEvent<string>).detail !== id) close()
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close()
    }
    const onViewportChange = () => {
      if (open) updatePosition()
    }
    document.addEventListener('tagger2-help-open', onOtherHelp)
    document.addEventListener('keydown', onKeyDown)
    window.addEventListener('resize', onViewportChange)
    window.addEventListener('scroll', onViewportChange, true)
    return () => {
      document.removeEventListener('tagger2-help-open', onOtherHelp)
      document.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('resize', onViewportChange)
      window.removeEventListener('scroll', onViewportChange, true)
      cancelOpen()
      cancelClose()
    }
  }, [cancelClose, cancelOpen, close, id, open, updatePosition])

  useEffect(() => {
    if (!open) return
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node
      if (triggerRef.current?.contains(target)) return
      const popover = document.getElementById(`${id}-popover`)
      if (!popover?.contains(target)) close()
    }
    document.addEventListener('pointerdown', onPointerDown)
    return () => document.removeEventListener('pointerdown', onPointerDown)
  }, [close, id, open])

  return <>
    <button
      ref={triggerRef}
      type="button"
      className="field-help-button"
      aria-label={labels.button}
      aria-haspopup="dialog"
      aria-expanded={open}
      aria-controls={`${id}-popover`}
      onMouseEnter={scheduleOpen}
      onMouseLeave={() => { cancelOpen(); scheduleClose() }}
      onPointerDown={() => { pointerActivation.current = true }}
      onFocus={() => {
        if (!pointerActivation.current) openPopover()
      }}
      onBlur={scheduleClose}
      onClick={(event) => {
        event.preventDefault()
        pointerActivation.current = false
        if (event.detail === 0) openPopover()
        else if (open) close()
        else openPopover()
      }}
    >
      <CircleHelp size={14} aria-hidden="true" />
    </button>
    {open && createPortal(
      <div
        id={`${id}-popover`}
        className="field-help-popover"
        role="dialog"
        aria-label={`${label} ${labels.button}`}
        style={{ top: position.top, left: position.left }}
        onMouseEnter={cancelClose}
        onMouseLeave={scheduleClose}
      >
        <div className="field-help-popover-header">
          <strong id={`${id}-help-label`}>{label}</strong>
          <button type="button" className="field-help-close" aria-label={labels.close} onClick={close}>
            <X size={14} aria-hidden="true" />
          </button>
        </div>
        <dl>
          <div><dt>{labels.purpose}</dt><dd>{help.purpose}</dd></div>
          <div><dt>{labels.recommended}</dt><dd>{help.recommended}</dd></div>
          <div><dt>{labels.note}</dt><dd>{help.note}</dd></div>
        </dl>
      </div>,
      document.body,
    )}
  </>
}

export function Field({ label, hint, error, help, helpLabels, children }: { label: string; hint?: string; error?: string; help?: FieldHelp; helpLabels?: FieldHelpLabels; children: ReactNode }) {
  const id = useId()
  const hintId = hint ? `${id}-hint` : undefined
  const errorId = error ? `${id}-error` : undefined
  const directControl = isValidElement(children)
    && typeof children.type === 'string'
    && ['input', 'select', 'textarea'].includes(children.type)
  const control = directControl ? children as ReactElement<Record<string, unknown>> : undefined
  const existingId = control?.props.id
  const controlId = typeof existingId === 'string' ? existingId : `${id}-control`
  const existingDescription = control?.props['aria-describedby']
  const describedBy = [typeof existingDescription === 'string' ? existingDescription : undefined, hintId, errorId].filter(Boolean).join(' ') || undefined
  const content = control
    ? cloneElement(control, {
      id: controlId,
      'aria-describedby': describedBy,
      'aria-invalid': error ? true : control.props['aria-invalid'],
      'aria-errormessage': error ? errorId : control.props['aria-errormessage'],
    })
    : children
  const labelId = `${id}-label`

  return <div className="field" role={directControl ? undefined : 'group'} aria-labelledby={directControl ? undefined : labelId}>
    <div className="field-label-row">
      {directControl
        ? <label id={labelId} className="field-label" htmlFor={controlId}>{label}</label>
        : <span id={labelId} className="field-label">{label}</span>}
      {help && helpLabels && <HelpPopover label={label} help={help} labels={helpLabels} />}
    </div>
    {content}
    {hint && <span id={hintId} className="field-hint">{hint}</span>}
    {error && <span id={errorId} className="field-error">{error}</span>}
  </div>
}

type DialogLayerProps = {
  children: ReactNode
  onClose: () => void
  className?: string
  closeOnBackdrop?: boolean
}

const dialogFocusableSelector = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')
const dialogRoleSelector = '[role="dialog"], [role="alertdialog"]'

export function DialogLayer({ children, onClose, className = 'drawer-backdrop', closeOnBackdrop = true }: DialogLayerProps) {
  const backdropRef = useRef<HTMLDivElement>(null)
  const closeRef = useRef(onClose)
  closeRef.current = onClose

  useEffect(() => {
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const appShell = document.querySelector<HTMLElement>('.app-shell')
    const alreadyInert = appShell?.hasAttribute('inert') ?? false
    const previousOverflow = document.body.style.overflow
    if (appShell && !alreadyInert) appShell.setAttribute('inert', '')
    document.body.style.overflow = 'hidden'

    const focusDialog = window.setTimeout(() => {
      const backdrop = backdropRef.current
      const dialog = backdrop?.querySelector<HTMLElement>(dialogRoleSelector)
      const preferred = dialog?.querySelector<HTMLElement>('[data-autofocus]')
        ?? dialog?.querySelector<HTMLElement>('input:not([disabled]), select:not([disabled]), textarea:not([disabled])')
        ?? dialog?.querySelector<HTMLElement>(dialogFocusableSelector)
      if (preferred) preferred.focus()
      else if (dialog) {
        dialog.tabIndex = -1
        dialog.focus()
      }
    }, 0)

    const onKeyDown = (event: KeyboardEvent) => {
      const backdrop = backdropRef.current
      const dialog = backdrop?.querySelector<HTMLElement>(dialogRoleSelector)
      if (!dialog) return
      if (event.key === 'Escape') {
        event.preventDefault()
        closeRef.current()
        return
      }
      if (event.key !== 'Tab') return
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(dialogFocusableSelector))
        .filter((element) => {
          const style = window.getComputedStyle(element)
          return !element.hidden
            && element.getAttribute('aria-hidden') !== 'true'
            && style.display !== 'none'
            && style.visibility !== 'hidden'
        })
      if (!focusable.length) {
        event.preventDefault()
        dialog.focus()
        return
      }
      const first = focusable[0]!
      const last = focusable[focusable.length - 1]!
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)

    return () => {
      window.clearTimeout(focusDialog)
      document.removeEventListener('keydown', onKeyDown)
      if (appShell && !alreadyInert) appShell.removeAttribute('inert')
      document.body.style.overflow = previousOverflow
      previousFocus?.focus()
    }
  }, [])

  return createPortal(
    <div
      ref={backdropRef}
      className={className}
      role="presentation"
      onMouseDown={(event) => {
        if (closeOnBackdrop && event.currentTarget === event.target) closeRef.current()
      }}
    >
      {children}
    </div>,
    document.body,
  )
}

export function ConfirmDialog({
  title,
  detail,
  confirmLabel = '确认',
  cancelLabel = '取消',
  tone = 'danger',
  busy = false,
  onConfirm,
  onClose,
}: {
  title: string
  detail: ReactNode
  confirmLabel?: string
  cancelLabel?: string
  tone?: 'primary' | 'danger'
  busy?: boolean
  onConfirm: () => void
  onClose: () => void
}) {
  const titleId = useId()
  const detailId = useId()
  return <DialogLayer onClose={() => { if (!busy) onClose() }} closeOnBackdrop={!busy}>
    <div className="confirm-dialog" role="alertdialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={detailId}>
      <header>
        <span className="confirm-dialog-icon"><AlertTriangle size={20} aria-hidden="true" /></span>
        <div><p className="eyebrow">CONFIRM ACTION</p><h2 id={titleId}>{title}</h2></div>
      </header>
      <div className="confirm-dialog-body" id={detailId}>{detail}</div>
      <footer>
        <Button type="button" variant="secondary" data-autofocus disabled={busy} onClick={onClose}>{cancelLabel}</Button>
        <Button type="button" variant={tone} disabled={busy} icon={busy ? <LoaderCircle className="spin" size={15} /> : undefined} onClick={onConfirm}>{confirmLabel}</Button>
      </footer>
    </div>
  </DialogLayer>
}

export function CloseIcon() { return <X size={16} aria-hidden="true" /> }
