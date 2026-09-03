import { useState } from 'react'
import { BookOpen, X } from 'lucide-react'
import { tagCategoryClass, tagCategoryLabel } from '../../lib/tagCategories'
import { formatTagForDisplay, type TagManagerProfile } from '../../lib/tagManager'
import { usePreferences } from '../../store/app'
import { WikiDrawer } from '../tagWiki/WikiDrawer'
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
  const [wikiTag, setWikiTag] = useState<string | null>(null)
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
          <span
            key={`${entry.text}:${index}`}
            role="button"
            tabIndex={disabled ? undefined : 0}
            className={`tm-pill ${tagCategoryClass(entry.category)}`}
            aria-label={`移除 ${displayTag}`}
            aria-disabled={disabled || undefined}
            title={titleParts.join(' · ')}
            onClick={() => {
              if (!disabled) onRemove(index)
            }}
            onKeyDown={(event) => {
              // Only react when the pill itself is focused; the nested wiki
              // button must not trigger removal via bubbling key events.
              if (event.target !== event.currentTarget) return
              if (disabled || (event.key !== 'Enter' && event.key !== ' ')) return
              event.preventDefault()
              onRemove(index)
            }}
          >
            <span>{displayTag}</span>
            {showTranslation && entry.translation && <span className="tm-pill-zh">{entry.translation}</span>}
            {entry.score != null && <small>{Math.round(entry.score * 100)}%</small>}
            <button
              type="button"
              className="tm-pill-wiki-btn"
              title={`查看 ${entry.text} 的 Wiki`}
              aria-label={`查看 ${entry.text} 的 Wiki`}
              onClick={(e) => {
                e.stopPropagation()
                setWikiTag(entry.text)
              }}
            >
              <BookOpen size={12} aria-hidden="true" />
            </button>
            {!disabled && <X size={11} aria-hidden="true" />}
          </span>
        )
      })}
    </div> : <p className="tm-pill-empty">暂无标签</p>}
    <TagInput profile={profile} label={addLabel} onAdd={onAdd} disabled={disabled} />
    {wikiTag && <WikiDrawer tag={wikiTag} onClose={() => setWikiTag(null)} profile={profile} />}
  </div>
}
