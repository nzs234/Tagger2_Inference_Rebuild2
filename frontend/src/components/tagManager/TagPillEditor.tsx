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

/**
 * Category-coloured tag pills plus an autocomplete add input.  The pill body
 * is presentational; the two sibling buttons (wiki lookup and explicit remove)
 * carry all interaction, so no interactive element is ever nested inside
 * another and the accessible names stay stable for tests and screen readers.
 */
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

        return (
          <span
            key={`${entry.text}:${index}`}
            className={`tm-pill ${tagCategoryClass(entry.category)}`}
            title={titleParts.join(' · ')}
          >
            <span>{displayTag}</span>
            {showTranslation && entry.translation && <span className="tm-pill-zh">{entry.translation}</span>}
            {entry.score != null && <small>{Math.round(entry.score * 100)}%</small>}
            <button
              type="button"
              className="tm-pill-wiki-btn"
              title={`查看 ${entry.text} 的 Wiki`}
              aria-label={`查看 ${entry.text} 的 Wiki`}
              onClick={() => setWikiTag(entry.text)}
            >
              <BookOpen size={12} aria-hidden="true" />
            </button>
            {!disabled && <button
              type="button"
              className="tm-pill-remove"
              title={`移除 ${displayTag}`}
              aria-label={`移除 ${displayTag}`}
              onClick={() => onRemove(index)}
            >
              <X size={11} aria-hidden="true" />
            </button>}
          </span>
        )
      })}
    </div> : <p className="tm-pill-empty">暂无标签</p>}
    <TagInput profile={profile} label={addLabel} onAdd={onAdd} disabled={disabled} />
    {wikiTag && <WikiDrawer tag={wikiTag} onClose={() => setWikiTag(null)} profile={profile} />}
  </div>
}
