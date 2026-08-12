import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { AlertTriangle, Check, CircleHelp, LoaderCircle, X } from 'lucide-react'
import { useVirtualizer } from '@tanstack/react-virtual'
import { useRef } from 'react'
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

export function Panel({ title, eyebrow, actions, children, className = '' }: {
  title?: string
  eyebrow?: string
  actions?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <section className={`panel ${className}`}>
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
    pending: 'Pending', waiting_count_review: 'Count review', waiting_token_review: 'Token review',
    committing: 'Committing', pausing: 'Pausing', rollback_required: 'Rollback required', restoring: 'Restoring',
    ready: '待处理', uploading: '上传中', queued: '排队中', processing: '处理中', done: '完成', error: '失败',
    running: '运行中', paused: '已暂停', cancelling: '取消中', cancelled: '已取消', succeeded: '成功', failed: '失败', interrupted: '已中断',
  }
  const icon = ['running', 'uploading', 'processing', 'queued', 'cancelling', 'committing', 'pausing', 'restoring'].includes(state)
    ? <LoaderCircle size={13} className="spin" aria-hidden="true" />
    : ['done', 'succeeded'].includes(state)
      ? <Check size={13} aria-hidden="true" />
      : ['error', 'failed'].includes(state)
        ? <AlertTriangle size={13} aria-hidden="true" />
        : null
  return <span className={`status status-${state}`}>{icon}{label[state] ?? state}</span>
}

export function ProgressBar({ value, label }: { value: number; label?: string }) {
  const safe = Math.max(0, Math.min(100, value))
  return (
    <div className="progress-wrap" aria-label={label ?? `${Math.round(safe)}%`}>
      <div className="progress-track"><div className="progress-value" style={{ width: `${safe}%` }} /></div>
      <span className="progress-label">{Math.round(safe)}%</span>
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

export function Field({ label, hint, error, children }: { label: string; hint?: string; error?: string; children: ReactNode }) {
  return <label className="field"><span className="field-label">{label}</span>{children}{hint && <span className="field-hint">{hint}</span>}{error && <span className="field-error">{error}</span>}</label>
}

export function ConfirmButton({ label, onConfirm, children, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { label: string; onConfirm: () => void }) {
  return <Button {...props} onClick={() => { if (window.confirm(label)) onConfirm() }}>{children}</Button>
}

export function CloseIcon() { return <X size={16} aria-hidden="true" /> }
