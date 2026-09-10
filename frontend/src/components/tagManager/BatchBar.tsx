import { useState } from 'react'
import { LoaderCircle, Wand2 } from 'lucide-react'
import { Button, ConfirmDialog, Field, Panel } from '../ui'
import {
  formatTagForDisplay,
  toBatchFilter,
  toWriteStyle,
  type ImageFilterState,
  type TagManagerBatchRequest,
  type TagManagerProfile,
} from '../../lib/tagManager'
import { usePreferences } from '../../store/app'
import { TagPillEditor, type PillEntry } from './TagPillEditor'

type BatchOp = TagManagerBatchRequest['op']
type BatchScope = 'selected' | 'filtered'

const OP_LABELS: Record<BatchOp, string> = { add: '添加', remove: '删除', replace: '替换' }

// Mirrors the backend's MAX_BATCH_IMAGES; blocked before submit so the user
// never discovers the cap through a 413 after filling in the whole form.
const MAX_BATCH_IMAGES = 2000

/**
 * Multi-image batch operations bar. The actual submission is confirmed with a
 * dialog that states the exact scope (selected images vs. the whole filtered
 * result set) before `onSubmit` fires.
 */
export function BatchBar({ profile, filter, selectedIds, filteredTotal, submitting, disabled, onSubmit }: {
  profile: TagManagerProfile
  filter: ImageFilterState
  selectedIds: number[]
  filteredTotal: number
  submitting: boolean
  disabled?: boolean
  onSubmit: (body: TagManagerBatchRequest) => void
}) {
  const [op, setOp] = useState<BatchOp>('add')
  const [tags, setTags] = useState<PillEntry[]>([])
  const [replacement, setReplacement] = useState('')
  const [useRegex, setUseRegex] = useState(false)
  const [scopeChoice, setScopeChoice] = useState<BatchScope | null>(null)
  const [confirming, setConfirming] = useState(false)
  const tagStyle = usePreferences((state) => state.tagStyle)

  // The scope follows the selection until the user picks one explicitly:
  // selecting images targets them, dropping back to zero targets the filtered
  // result — the batch bar stays usable without ever selecting anything.
  const scope: BatchScope = scopeChoice
    ?? (selectedIds.length > 0 ? 'selected' : 'filtered')

  const scopeCount = scope === 'selected' ? selectedIds.length : filteredTotal
  const overLimit = scopeCount > MAX_BATCH_IMAGES
  const canSubmit = tags.length > 0 && scopeCount > 0 && !overLimit && !submitting && !disabled

  const buildBody = (): TagManagerBatchRequest => ({
    op,
    // A regex pattern is not a tag name, so it is never restyled.
    tags: tags.map((entry) => (useRegex ? entry.text : toWriteStyle(entry.text, tagStyle))),
    replacement: op === 'replace'
      ? (useRegex ? replacement : toWriteStyle(replacement, tagStyle))
      : undefined,
    use_regex: useRegex,
    image_ids: scope === 'selected' ? [...selectedIds].sort((left, right) => left - right) : undefined,
    filter: scope === 'filtered' ? toBatchFilter(filter) : undefined,
  })

  return <Panel
    title="批量操作"
    eyebrow="BATCH"
    className="tm-batch-panel"
  >
    <div className="tm-batch-body">
      <div className="tm-scope-switch" role="group" aria-label="操作范围">
        <button type="button" className={scope === 'selected' ? 'mode-active' : ''} aria-pressed={scope === 'selected'} onClick={() => setScopeChoice('selected')}>选中图片（{selectedIds.length}）</button>
        <button type="button" className={scope === 'filtered' ? 'mode-active' : ''} aria-pressed={scope === 'filtered'} onClick={() => setScopeChoice('filtered')}>当前过滤结果（{filteredTotal}）</button>
      </div>
      <Field label="操作">
        <select aria-label="批量操作类型" value={op} disabled={disabled} onChange={(event) => setOp(event.target.value as BatchOp)}>
          <option value="add">添加标签</option>
          <option value="remove">删除标签</option>
          <option value="replace">替换标签</option>
        </select>
      </Field>
      <TagPillEditor
        entries={tags}
        profile={profile}
        addLabel="批量标签"
        disabled={disabled}
        onAdd={(tag, category) => { if (!tags.some((entry) => entry.text === tag)) setTags([...tags, category ? { text: tag, category } : { text: tag }]) }}
        onRemove={(index) => setTags(tags.filter((_, candidate) => candidate !== index))}
      />
      {op === 'replace' && <Field label="替换为" hint={useRegex ? '正则替换：单个模式匹配每个标签，可用 $1 引用分组' : '匹配到的每个标签都会统一替换为该值'}>
        <input aria-label="替换为" value={replacement} disabled={disabled} spellCheck={false} onChange={(event) => setReplacement(event.target.value)} placeholder="replacement" />
      </Field>}
      {overLimit && <p className="tm-batch-warning" role="alert">
        当前过滤结果有 {filteredTotal} 张，超过单批 {MAX_BATCH_IMAGES} 张的上限。请先缩小过滤范围（例如排除部分标签）再执行。
      </p>}
      <label className="toggle standalone">
        <input aria-label="使用正则表达式" type="checkbox" checked={useRegex} disabled={disabled} onChange={(event) => setUseRegex(event.target.checked)} />
        <span />使用正则表达式
      </label>
      <div className="form-actions">
        <Button
          icon={submitting ? <LoaderCircle className="spin" size={15} /> : <Wand2 size={15} />}
          disabled={!canSubmit}
          onClick={() => setConfirming(true)}
        >执行</Button>
        <Button variant="quiet" disabled={tags.length === 0} onClick={() => setTags([])}>清空标签</Button>
      </div>
    </div>
    {confirming && <ConfirmDialog
      title={`对 ${scopeCount} 张图片执行「${OP_LABELS[op]}」？`}
      detail={<span>
        范围：<strong>{scope === 'selected' ? `选中的 ${selectedIds.length} 张图片` : `当前过滤结果的全部 ${filteredTotal} 张图片`}</strong>。
        标签：<strong>{tags.map((entry) => formatTagForDisplay(entry.text, tagStyle)).join(', ')}</strong>
        {op === 'replace' && <> → <strong>{replacement ? formatTagForDisplay(replacement, tagStyle) : '（空）'}</strong></>}
        {useRegex && <>（正则模式）</>}。此操作会写入撤销日志，可撤销。
      </span>}
      confirmLabel="确认执行"
      busy={submitting}
      onConfirm={() => {
        onSubmit(buildBody())
        // Close on confirmation: the mutation's progress is visible on the
        // 执行 button and in the notice queue, and leaving the dialog mounted
        // would show a stale scope count once the batch clears the selection.
        setConfirming(false)
      }}
      onClose={() => setConfirming(false)}
    />}
  </Panel>
}
