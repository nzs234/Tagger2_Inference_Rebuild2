import { useState } from 'react'
import { LoaderCircle, Wand2 } from 'lucide-react'
import { Button, ConfirmDialog, Field, Panel } from '../ui'
import {
  formatTagForDisplay,
  tagManagerApi,
  toBatchFilter,
  toWriteStyle,
  type BatchPreviewResponse,
  type ImageFilterState,
  type TagManagerBatchRequest,
  type TagManagerProfile,
} from '../../lib/tagManager'
import { usePreferences } from '../../store/app'
import { BatchPreviewDialog } from './BatchPreviewDialog'
import { TagPillEditor, type PillEntry } from './TagPillEditor'
import type { NoticeTone } from './useNoticeQueue'

type BatchOp = TagManagerBatchRequest['op']
type BatchScope = 'selected' | 'filtered'

const OP_LABELS: Record<BatchOp, string> = { add: '添加', remove: '删除', replace: '替换' }

// Mirrors the backend's MAX_BATCH_IMAGES; blocked before submit so the user
// never discovers the cap through a 413 after filling in the whole form.
const MAX_BATCH_IMAGES = 2000

/**
 * Multi-image batch operations bar.
 *
 * Clicking 执行 first asks the backend for a read-only preview.  The resulting
 * dialog states the exact scope and the would-be outcome before the write
 * fires; when the preview call itself fails the bar falls back to the plain
 * confirmation so a flaky preview never blocks the operation.  The payload is
 * built exactly once per click and handed to both calls, so the preview can
 * never describe a different request than the one that is submitted.
 */
export function BatchBar({ sessionId, profile, filter, selectedIds, filteredTotal, submitting, disabled, notify, onSubmit }: {
  sessionId: string
  profile: TagManagerProfile
  filter: ImageFilterState
  selectedIds: number[]
  filteredTotal: number
  submitting: boolean
  disabled?: boolean
  notify: (tone: NoticeTone, text: string) => void
  onSubmit: (body: TagManagerBatchRequest) => void
}) {
  const [op, setOp] = useState<BatchOp>('add')
  const [tags, setTags] = useState<PillEntry[]>([])
  const [replacement, setReplacement] = useState('')
  const [useRegex, setUseRegex] = useState(false)
  const [scopeChoice, setScopeChoice] = useState<BatchScope | null>(null)
  const [previewing, setPreviewing] = useState(false)
  // The frozen request plus its preview result; both are cleared together.
  const [preview, setPreview] = useState<{ body: TagManagerBatchRequest; result: BatchPreviewResponse } | null>(null)
  // Preview-failure fallback: the same frozen payload, confirmed by the plain dialog.
  const [fallbackBody, setFallbackBody] = useState<TagManagerBatchRequest | null>(null)
  const tagStyle = usePreferences((state) => state.tagStyle)

  // The scope follows the selection until the user picks one explicitly:
  // selecting images targets them, dropping back to zero targets the filtered
  // result — the batch bar stays usable without ever selecting anything.
  const scope: BatchScope = scopeChoice
    ?? (selectedIds.length > 0 ? 'selected' : 'filtered')

  const scopeCount = scope === 'selected' ? selectedIds.length : filteredTotal
  const overLimit = scopeCount > MAX_BATCH_IMAGES
  const canSubmit = tags.length > 0 && scopeCount > 0 && !overLimit && !submitting && !disabled && !previewing

  const tagsLabel = tags.map((entry) => formatTagForDisplay(entry.text, tagStyle)).join(', ')

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

  const startPreview = async () => {
    const body = buildBody()
    setPreviewing(true)
    try {
      const result = await tagManagerApi.batchPreview(sessionId, body)
      setPreview({ body, result })
    } catch {
      // Preview is advisory: a failure must not block the write, so the exact
      // payload already built goes straight to the normal confirmation.
      notify('warning', '预览加载失败，将直接确认执行')
      setFallbackBody(body)
    } finally {
      setPreviewing(false)
    }
  }

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
        {scope === 'selected'
          ? `已选中 ${selectedIds.length} 张图片，超过单批 ${MAX_BATCH_IMAGES} 张的上限。请减少选中数量（或取消部分勾选）后再执行。`
          : `当前过滤结果有 ${filteredTotal} 张，超过单批 ${MAX_BATCH_IMAGES} 张的上限。请先缩小过滤范围（例如排除部分标签）再执行。`}
      </p>}
      <label className="toggle standalone">
        <input aria-label="使用正则表达式" type="checkbox" checked={useRegex} disabled={disabled} onChange={(event) => setUseRegex(event.target.checked)} />
        <span />使用正则表达式
      </label>
      <div className="form-actions">
        <Button
          icon={(submitting || previewing) ? <LoaderCircle className="spin" size={15} /> : <Wand2 size={15} />}
          disabled={!canSubmit}
          onClick={() => { void startPreview() }}
        >执行</Button>
        <Button variant="quiet" disabled={tags.length === 0} onClick={() => setTags([])}>清空标签</Button>
      </div>
    </div>
    {preview && <BatchPreviewDialog
      scopeCount={scopeCount}
      scopeLabel={scope === 'selected'
        ? `选中的 ${selectedIds.length} 张图片（可能包含当前筛选结果之外的图片）`
        : `当前过滤结果的全部 ${filteredTotal} 张图片`}
      opLabel={OP_LABELS[op]}
      tagsLabel={tagsLabel}
      preview={preview.result}
      busy={submitting}
      onConfirm={() => {
        onSubmit(preview.body)
        // Close on confirmation: the mutation's progress is visible on the
        // 执行 button and in the notice queue, and leaving the dialog mounted
        // would show a stale scope count once the batch clears the selection.
        setPreview(null)
      }}
      onClose={() => setPreview(null)}
    />}
    {fallbackBody && <ConfirmDialog
      title={`对 ${scopeCount} 张图片执行「${OP_LABELS[op]}」？`}
      detail={<span>
        范围：<strong>{scope === 'selected'
          ? `选中的 ${selectedIds.length} 张图片（可能包含当前筛选结果之外的图片）`
          : `当前过滤结果的全部 ${filteredTotal} 张图片`}</strong>。
        标签：<strong>{tagsLabel}</strong>
        {op === 'replace' && <> → <strong>{replacement ? formatTagForDisplay(replacement, tagStyle) : '（空）'}</strong></>}
        {useRegex && <>（正则模式）</>}。此操作会写入撤销日志，可撤销。
      </span>}
      confirmLabel="确认执行"
      busy={submitting}
      onConfirm={() => {
        onSubmit(fallbackBody)
        setFallbackBody(null)
      }}
      onClose={() => setFallbackBody(null)}
    />}
  </Panel>
}
