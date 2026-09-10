import { LoaderCircle } from 'lucide-react'
import { useEffect, useId, useRef, useState } from 'react'
import {
  formatPostCount,
  formatTagForDisplay,
  tagManagerApi,
  toWriteStyle,
  type TagDbEntry,
  type TagManagerProfile,
} from '../../lib/tagManager'
import { tagCategoryClass, tagCategoryLabel } from '../../lib/tagCategories'
import { usePreferences } from '../../store/app'

/**
 * Debounced tag-database autocomplete input. Enter commits the first
 * suggestion (Alt/Option+Enter behaves the same); clicking a suggestion
 * commits that entry. When the lookup has no matches the raw text is committed
 * without a category.
 */
export function TagInput({ profile, label, placeholder, disabled, onAdd }: {
  profile: TagManagerProfile
  label: string
  placeholder?: string
  disabled?: boolean
  onAdd: (tag: string, category?: string) => void
}) {
  const bilingual = usePreferences((state) => state.bilingualTags)
  const tagStyle = usePreferences((state) => state.tagStyle)
  const [text, setText] = useState('')
  const [suggestions, setSuggestions] = useState<TagDbEntry[]>([])
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [activeIndex, setActiveIndex] = useState(-1)
  const requestId = useRef(0)
  // Stable combobox/listbox ids: deriving them from the (Chinese, potentially
  // duplicated) label made the aria wiring fragile; useId is always unique.
  const listId = useId()
  const optionId = (index: number) => `${listId}-suggestion-${index}`

  useEffect(() => {
    const rawQuery = text.trim()
    const query = toWriteStyle(rawQuery, 'underscore')
    if (!query || disabled) {
      // Bump the request id so a lookup that is already in flight for the
      // previous text cannot land after the reset and reopen the dropdown.
      requestId.current += 1
      setSuggestions([])
      setOpen(false)
      setLoading(false)
      return
    }
    const id = requestId.current + 1
    requestId.current = id
    setLoading(true)
    const timer = window.setTimeout(async () => {
      try {
        const result = await tagManagerApi.tagDb(profile, query, 20)
        if (requestId.current !== id) return
        setSuggestions(result.items)
        setActiveIndex(result.items.length > 0 ? 0 : -1)
        setOpen(result.items.length > 0)
      } catch {
        if (requestId.current === id) {
          setSuggestions([])
          setOpen(false)
        }
      } finally {
        if (requestId.current === id) setLoading(false)
      }
    }, 220)
    return () => window.clearTimeout(timer)
  }, [text, profile, disabled])

  const commit = (raw: string, category?: string) => {
    const tag = toWriteStyle(raw.trim(), tagStyle)
    if (!tag) return
    onAdd(tag, category)
    setText('')
    setSuggestions([])
    setOpen(false)
  }

  return <div className="tm-autocomplete">
    <input
      value={text}
      aria-label={label}
      placeholder={placeholder ?? '输入标签，回车添加'}
      disabled={disabled}
      spellCheck={false}
      autoComplete="off"
      role="combobox"
      aria-expanded={open}
      aria-controls={listId}
      onChange={(event) => setText(event.target.value)}
      aria-activedescendant={open && activeIndex >= 0 ? optionId(activeIndex) : undefined}
      onKeyDown={(event) => {
        // An IME confirm Enter (zh-CN input) must not commit the draft tag.
        if (event.key === 'Enter' && event.nativeEvent.isComposing) return
        if (event.key === 'ArrowDown' && open && suggestions.length > 0) {
          event.preventDefault()
          setActiveIndex((index) => (index + 1) % suggestions.length)
        } else if (event.key === 'ArrowUp' && open && suggestions.length > 0) {
          event.preventDefault()
          setActiveIndex((index) => (index - 1 + suggestions.length) % suggestions.length)
        } else if (event.key === 'Enter') {
          // Alt/Option+Enter must stay usable for keyboard users (and macOS
          // muscle memory), so it commits exactly like plain Enter.
          event.preventDefault()
          const selected = open && activeIndex >= 0 ? suggestions[activeIndex] : undefined
          commit(selected ? selected.name : text, selected?.category)
        } else if (event.key === 'Escape' && open) {
          // Escape closes only the suggestion dropdown.  Without swallowing the
          // event it bubbles to the drawer's DialogLayer and closes the whole
          // editor; stopPropagation also keeps that from depending solely on
          // the layer's defaultPrevented check.
          event.preventDefault()
          event.stopPropagation()
          setOpen(false)
          setActiveIndex(-1)
        }
      }}
    />
    {loading && <LoaderCircle className="spin tm-autocomplete-spinner" size={13} aria-hidden="true" />}
    {open && suggestions.length > 0 && <ul id={listId} className="tm-suggest-list" role="listbox" aria-label={`${label}建议`}>
      {suggestions.map((entry, index) => {
        const displayTag = formatTagForDisplay(entry.name, tagStyle)
        const showTranslation = bilingual && Boolean(entry.translation)
        const titleText = entry.alias_of
          ? `别名 → ${formatTagForDisplay(entry.alias_of, tagStyle)}`
          : showTranslation && entry.translation
            ? `${displayTag} · ${entry.translation}`
            : displayTag

        return (
          <li
            key={entry.name}
            id={optionId(index)}
            role="option"
            aria-selected={index === activeIndex}
            className={index === activeIndex ? 'tm-suggest-active' : undefined}
          >
            <button
              type="button"
              className="tm-suggest-item"
              title={titleText}
              onMouseDown={(event) => {
                event.preventDefault()
                commit(entry.name, entry.category)
              }}
            >
              <span className={`tm-pill ${tagCategoryClass(entry.category)}`}>{tagCategoryLabel(entry.category)}</span>
              <span className="tm-suggest-name">
                {displayTag}
                {showTranslation && entry.translation && <span className="tm-suggest-zh">({entry.translation})</span>}
              </span>
              <small>{formatPostCount(entry.post_count)}</small>
            </button>
          </li>
        )
      })}
    </ul>}
  </div>
}
