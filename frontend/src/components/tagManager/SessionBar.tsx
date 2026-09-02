import { AlertTriangle, CheckCircle2, FolderOpen, LoaderCircle, Redo2, RefreshCw, Trash2, Undo2 } from 'lucide-react'
import { useState } from 'react'
import type { RootInfo } from '../../types'
import { Button, Field, Panel } from '../ui'
import type { TagManagerCreateRequest, TagManagerProfile, TagManagerSession } from '../../lib/tagManager'

function sessionStatusLabel(status: TagManagerSession['status']): string {
  if (status === 'indexing') return '索引中'
  if (status === 'error') return '错误'
  return '就绪'
}

function SessionStatusBadge({ session }: { session: TagManagerSession }) {
  const tone = session.status === 'ready' ? 'success' : session.status === 'error' ? 'danger' : 'processing'
  return <span className={`status status-${tone}`} role="status">
    {session.status === 'indexing' && <LoaderCircle className="spin" size={13} aria-hidden="true" />}
    {session.status === 'ready' && <CheckCircle2 size={13} aria-hidden="true" />}
    {session.status === 'error' && <AlertTriangle size={13} aria-hidden="true" />}
    {sessionStatusLabel(session.status)} · {session.image_count} 张
  </span>
}

export function SessionBar({ sessions, activeSession, inputRoots, active, creating, refreshing, deleting, undoPending, redoPending, actionsDisabled, onSelect, onCreate, onRefresh, onDelete, onUndo, onRedo }: {
  sessions: TagManagerSession[]
  activeSession?: TagManagerSession
  inputRoots: RootInfo[]
  active?: string
  creating: boolean
  refreshing: boolean
  deleting: boolean
  undoPending: boolean
  redoPending: boolean
  /** True while the session is missing or still indexing. */
  actionsDisabled: boolean
  onSelect: (id: string) => void
  onCreate: (body: TagManagerCreateRequest) => void
  onRefresh: () => void
  onDelete: () => void
  onUndo: () => void
  onRedo: () => void
}) {
  const [rootId, setRootId] = useState('')
  const effectiveRootId = inputRoots.some((root) => root.id === rootId) ? rootId : inputRoots[0]?.id ?? ''
  const [relativePath, setRelativePath] = useState('')
  const [profile, setProfile] = useState<TagManagerProfile>('e621')
  const [recursive, setRecursive] = useState(true)

  return <Panel
    title="会话"
    eyebrow="SESSION"
    actions={<div className="tm-session-actions">
      <Button size="sm" variant="secondary" icon={refreshing ? <LoaderCircle className="spin" size={14} /> : <RefreshCw size={14} />} disabled={actionsDisabled || refreshing} onClick={onRefresh}>刷新</Button>
      <Button size="sm" variant="secondary" icon={<Undo2 size={14} />} disabled={actionsDisabled || undoPending} onClick={onUndo}>撤销</Button>
      <Button size="sm" variant="secondary" icon={<Redo2 size={14} />} disabled={actionsDisabled || redoPending} onClick={onRedo}>重做</Button>
      <Button size="sm" variant="danger" icon={<Trash2 size={14} />} disabled={!activeSession || deleting} onClick={onDelete}>删除会话</Button>
    </div>}
  >
    <div className="tm-session-body">
      <Field label="现有会话">
        <select value={active ?? ''} aria-label="现有会话" onChange={(event) => onSelect(event.target.value)}>
          {sessions.length === 0 && <option value="">暂无会话</option>}
          {sessions.map((session) => (
            <option key={session.id} value={session.id}>
              {session.name} · {sessionStatusLabel(session.status)} · {session.image_count} 张
            </option>
          ))}
        </select>
      </Field>
      {activeSession && <div className="tm-session-meta">
        <SessionStatusBadge session={activeSession} />
        <small className="mono" title={activeSession.relative_path}>{activeSession.root_id}/{activeSession.relative_path || '(根目录)'}</small>
        <small>{activeSession.profile}</small>
      </div>}
      {activeSession?.error && <p className="tm-session-error">{activeSession.error}</p>}
      <div className="tm-session-create">
        <Field label="数据目录">
          <select value={effectiveRootId} onChange={(event) => setRootId(event.target.value)}>
            <option value="">选择输入目录</option>
            {inputRoots.map((root) => <option key={root.id} value={root.id}>{root.name}</option>)}
          </select>
        </Field>
        <Field label="相对路径" hint="数据集在所选目录下的子路径，留空表示整个目录">
          <input value={relativePath} aria-label="相对路径" onChange={(event) => setRelativePath(event.target.value)} placeholder="例如 cats_v2" spellCheck={false} />
        </Field>
        <Field label="标签体系">
          <select value={profile} onChange={(event) => setProfile(event.target.value as TagManagerProfile)}>
            <option value="e621">e621</option>
            <option value="danbooru">danbooru</option>
          </select>
        </Field>
        <Field label="扫描范围">
          <label className="toggle standalone">
            <input aria-label="包含子目录" type="checkbox" checked={recursive} onChange={(event) => setRecursive(event.target.checked)} />
            <span />包含子目录
          </label>
        </Field>
        <div className="tm-session-open">
          <Button
            icon={creating ? <LoaderCircle className="spin" size={16} /> : <FolderOpen size={15} />}
            disabled={!effectiveRootId || creating}
            onClick={() => onCreate({
              root_id: effectiveRootId,
              relative_path: relativePath.trim(),
              profile,
              recursive,
              name: relativePath.trim().split(/[\\/]/).filter(Boolean).at(-1) || undefined,
            })}
          >打开</Button>
          {inputRoots.length === 0 && <small className="tm-session-hint">没有可用的输入目录，请先在设置中登记。</small>}
        </div>
      </div>
    </div>
  </Panel>
}
