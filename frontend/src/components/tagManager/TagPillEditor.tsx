import { X } from 'lucide-react'
import { tagCategoryClass, tagCategoryLabel } from '../../lib/tagCategories'
import type { TagManagerProfile } from '../../lib/tagManager'
import { TagInput } from './TagInput'

export interface PillEntry {
  text: string
  category?: string
  score?: number
}

/** Removable category-coloured tag pills plus an autocomplete add input. */
export function TagPillEditor({ entries, profile, addLabel, onAdd, onRemove, disabled }: {
  entries: PillEntry[]
  profile: TagManagerProfile
  addLabel: string
  onAdd: (tag: string, category?: string) => void
  onRemove: (index: number) => void
  disabled?: boolean
}) {
  return <div className="tm-pill-editor">
    {entries.length > 0 ? <div className="tm-pill-row">
      {entries.map((entry, index) => (
        <button
          type="button"
          key={`${entry.text}:${index}`}
          className={`tm-pill ${tagCategoryClass(entry.category)}`}
          disabled={disabled}
          title={`${tagCategoryLabel(entry.category)} · 点击移除`}
          aria-label={`移除 ${entry.text}`}
          onClick={() => onRemove(index)}
        >
          {entry.text}
          {entry.score != null && <small>{Math.round(entry.score * 100)}%</small>}
          {!disabled && <X size={11} aria-hidden="true" />}
        </button>
      ))}
    </div> : <p className="tm-pill-empty">暂无标签</p>}
    <TagInput profile={profile} label={addLabel} onAdd={onAdd} disabled={disabled} />
  </div>
}
