import { X } from 'lucide-react'
import { tagCategoryClass, tagCategoryLabel } from '../../lib/tagCategories'
import { formatTagForDisplay, type TagManagerProfile } from '../../lib/tagManager'
import { usePreferences } from '../../store/app'
import { TagInput } from './TagInput'

export interface PillEntry {
  text: string
  category?: string
  score?: number
  translation?: string | null
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
  const bilingual = usePreferences((state) => state.bilingualTags)
  const tagStyle = usePreferences((state) => state.tagStyle)

  return <div className="tm-pill-editor">
    {entries.length > 0 ? <div className="tm-pill-row">
      {entries.map((entry, index) => {
        const displayTag = formatTagForDisplay(entry.text, tagStyle)
        const showTranslation = bilingual && Boolean(entry.translation)
        const titleParts = [tagCategoryLabel(entry.category)]
        if (showTranslation && entry.translation) {
          titleParts.push(`${displayTag} · ${entry.translation}`)
        } else {
          titleParts.push(displayTag)
        }
        titleParts.push('点击移除')

        return (
          <button
            type="button"
            key={`${entry.text}:${index}`}
            className={`tm-pill ${tagCategoryClass(entry.category)}`}
            disabled={disabled}
            title={titleParts.join(' · ')}
            aria-label={`移除 ${displayTag}`}
            onClick={() => onRemove(index)}
          >
            <span>{displayTag}</span>
            {showTranslation && entry.translation && <span className="tm-pill-zh">{entry.translation}</span>}
            {entry.score != null && <small>{Math.round(entry.score * 100)}%</small>}
            {!disabled && <X size={11} aria-hidden="true" />}
          </button>
        )
      })}
    </div> : <p className="tm-pill-empty">暂无标签</p>}
    <TagInput profile={profile} label={addLabel} onAdd={onAdd} disabled={disabled} />
  </div>
}
