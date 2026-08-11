import { Pause, Play, RefreshCw, Square } from 'lucide-react'
import { Button, StatusBadge } from './ui'
import type { JobState } from '../types'

export function JobControls({ state, onAction, compact = false }: {
  state: JobState
  onAction: (action: 'pause' | 'resume' | 'cancel' | 'retry-failed') => void
  compact?: boolean
}) {
  const busy = ['queued', 'running', 'paused', 'cancelling'].includes(state)
  return <div className={`job-controls ${compact ? 'job-controls-compact' : ''}`}>
    <StatusBadge state={state} />
    {state === 'running' && <Button size="sm" variant="secondary" icon={<Pause size={14} />} onClick={() => onAction('pause')}>{compact ? undefined : '暂停'}</Button>}
    {state === 'paused' && <Button size="sm" variant="secondary" icon={<Play size={14} />} onClick={() => onAction('resume')}>{compact ? undefined : '继续'}</Button>}
    {busy && state !== 'cancelling' && <Button size="sm" variant="danger" icon={<Square size={13} />} onClick={() => onAction('cancel')}>{compact ? undefined : '取消'}</Button>}
    {['failed', 'cancelled', 'interrupted'].includes(state) && <Button size="sm" variant="secondary" icon={<RefreshCw size={14} />} onClick={() => onAction('retry-failed')}>{compact ? undefined : '重试失败项'}</Button>}
  </div>
}
