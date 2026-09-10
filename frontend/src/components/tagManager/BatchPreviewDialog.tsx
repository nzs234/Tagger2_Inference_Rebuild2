import { useId } from 'react'
import { Wand2 } from 'lucide-react'
import { Button, DialogLayer } from '../ui'
import {
  formatTagForDisplay,
  type BatchPreviewResponse,
  type TagStyle,
} from '../../lib/tagManager'
import { usePreferences } from '../../store/app'

/** Human labels for the editable sidecar kinds shown on each sample badge. */
const KIND_LABELS: Record<BatchPreviewResponse['samples'][number]['kind'], string> = {
  tag_txt: 'TXT',
  tags_json: '本地 JSON',
  standard_json: '九字段 JSON',
}

/** Format tallies in display order; `none` is contract symmetry only. */
const FORMAT_LABELS: Array<[keyof BatchPreviewResponse['formats'], string]> = [
  ['tag_txt', 'TXT'],
  ['tags_json', '本地 JSON'],
  ['standard_json', '九字段 JSON'],
]

/** Per-line cap so a target with hundreds of tags cannot blow up the dialog. */
const MAX_DISPLAY_TAGS = 12

function TagLine({ tags, changed, kind, style }: {
  tags: string[]
  /** `changed` is the tail of the diff that gets the added/removed highlight. */
  changed: (tag: string) => string
  kind: string
  style: TagStyle
}) {
  const shown = tags.slice(0, MAX_DISPLAY_TAGS)
  const hidden = tags.length - shown.length
  return <div className={`tm-preview-line tm-preview-line-${kind}`}>
    <span className="tm-preview-line-label">{kind === 'before' ? '原' : '后'}</span>
    <span className="tm-preview-tags">
      {shown.length === 0
        ? <span className="tm-preview-tag tm-preview-tag-muted">（无标签）</span>
        : shown.map((tag, index) => {
          const state = changed(tag)
          return <span key={`${tag}:${index}`} className={`tm-preview-tag${state ? ` ${state}` : ''}`}>{formatTagForDisplay(tag, style)}</span>
        })}
      {hidden > 0 && <span className="tm-preview-tag tm-preview-tag-muted">… 还有 {hidden} 个</span>}
    </span>
  </div>
}

function SampleDiff({ sample, style }: { sample: BatchPreviewResponse['samples'][number]; style: TagStyle }) {
  const beforeSet = new Set(sample.before_tags)
  const afterSet = new Set(sample.after_tags)
  return <div className="tm-preview-sample">
    <div className="tm-preview-sample-head">
      <span className="tm-preview-file" title={sample.file_name}>{sample.file_name}</span>
      <span className="tm-preview-kind">{KIND_LABELS[sample.kind]}</span>
    </div>
    <TagLine
      kind="before"
      tags={sample.before_tags}
      style={style}
      // Removed tags only: tags that survive into the "after" set stay plain.
      changed={(tag) => (afterSet.has(tag) ? '' : 'tm-preview-tag-removed')}
    />
    <TagLine
      kind="after"
      tags={sample.after_tags}
      style={style}
      changed={(tag) => (beforeSet.has(tag) ? '' : 'tm-preview-tag-added')}
    />
  </div>
}

/**
 * Read-only batch preview confirmation.  Renders the would-be tallies, format
 * distribution and up to five before/after samples produced by
 * `POST .../batch/preview`; the real write only fires from the confirm button.
 *
 * A preview with nothing to change blocks the confirm: a no-op batch writes no
 * journal entry, so running it would only produce a "已修改 0 张" notice.
 */
export function BatchPreviewDialog({ scopeCount, scopeLabel, opLabel, tagsLabel, preview, busy, onConfirm, onClose }: {
  scopeCount: number
  scopeLabel: string
  opLabel: string
  tagsLabel: string
  preview: BatchPreviewResponse
  busy: boolean
  onConfirm: () => void
  onClose: () => void
}) {
  const titleId = useId()
  const detailId = useId()
  const tagStyle = usePreferences((state) => state.tagStyle)

  const willCreate = preview.will_create ?? 0
  const noChange = preview.no_change ?? 0
  const skipped = preview.skipped_read_only ?? 0
  const targets = preview.targets ?? 0
  const affected = preview.affected ?? 0
  const formatEntries = FORMAT_LABELS.filter(([key]) => (preview.formats?.[key] ?? 0) > 0)

  return <DialogLayer onClose={() => { if (!busy) onClose() }} closeOnBackdrop={!busy}>
    <div className="confirm-dialog tm-preview-dialog" role="alertdialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={detailId}>
      <header>
        <span className="confirm-dialog-icon"><Wand2 size={20} aria-hidden="true" /></span>
        <div><p className="eyebrow">BATCH PREVIEW</p><h2 id={titleId}>批量操作预览</h2></div>
      </header>
      <div className="confirm-dialog-body tm-preview-body" id={detailId}>
        <p className="tm-preview-headline">
          对 <strong>{scopeCount}</strong> 张图片执行「{opLabel}」：<strong>{tagsLabel}</strong>
        </p>
        <p className="tm-preview-scope">范围：<strong>{scopeLabel}</strong>。此操作会写入撤销日志，可撤销。</p>
        <ul className="tm-preview-summary">
          <li>将修改 <strong>{affected}</strong> 张</li>
          <li>无变化 <strong>{noChange}</strong> 张</li>
          <li>跳过 <strong>{skipped}</strong> 张（只读）</li>
          <li>目标总数 <strong>{targets}</strong> 张</li>
        </ul>
        {willCreate > 0 && <p className="tm-preview-create">
          其中 <strong>{willCreate}</strong> 张没有 sidecar，将以 tag_txt 格式新建
        </p>}
        {formatEntries.length > 0 && <div className="tm-preview-formats" aria-label="格式分布">
          {formatEntries.map(([key, label]) => <span key={key} className="tm-preview-format">
            {label} <strong>{preview.formats[key]}</strong> 张
          </span>)}
        </div>}
        {affected === 0 && <p className="tm-preview-empty" role="status">没有会产生修改的目标</p>}
        {preview.samples.length > 0 && <section className="tm-preview-samples" aria-label="变更抽样">
          <p className="tm-preview-samples-title">抽样预览（最多 5 张）</p>
          {preview.samples.map((sample) => <SampleDiff key={sample.image_id} sample={sample} style={tagStyle} />)}
        </section>}
      </div>
      <footer>
        <Button type="button" variant="secondary" data-autofocus disabled={busy} onClick={onClose}>取消</Button>
        <Button type="button" variant="primary" disabled={busy || affected === 0} onClick={onConfirm}>确认执行</Button>
      </footer>
    </div>
  </DialogLayer>
}
