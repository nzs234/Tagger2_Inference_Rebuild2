import { LoaderCircle } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { formatPostCount, tagManagerApi, type TagDbEntry, type TagManagerProfile } from '../../lib/tagManager'
import { tagCategoryClass, tagCategoryLabel } from '../../lib/tagCategories'

/**
 * Debounced tag-database autocomplete input. Enter commits the first
 * suggestion; clicking a suggestion commits that entry. When the lookup has
 * no matches the raw text is committed without a category.
 */
export function TagInput({ profile, label, placeholder, disabled, onAdd }: {
  profile: TagManagerProfile
  label: string
  placeholder?: string
  disabled?: boolean
  onAdd: (tag: string, category?: string) => void
}) {
  const [text, setText] = useState('')
  const [suggestions, setSuggestions] = useState<TagDbEntry[]>([])
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const requestId = useRef(0)

  useEffect(() => {
    const query = text.trim()
    if (!query || disabled) {
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
    const tag = raw.trim().replace(/\s+/g, '_')
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
      aria-controls={`${label}-suggestions`}
      onChange={(event) => setText(event.target.value)}
      onKeyDown={(event) => {
        if (event.key === 'Enter') {
          event.preventDefault()
          const first = open ? suggestions[0] : undefined
          commit(first ? first.name : text, first?.category)
        }
        if (event.key === 'Escape') setOpen(false)
      }}
    />
    {loading && <LoaderCircle className="spin tm-autocomplete-spinner" size={13} aria-hidden="true" />}
    {open && suggestions.length > 0 && <ul id={`${label}-suggestions`} className="tm-suggest-list" role="listbox" aria-label={`${label}建议`}>
      {suggestions.map((entry) => <li key={entry.name} role="option" aria-selected="false">
        <button type="button" className="tm-suggest-item" title={entry.alias_of ? `别名 → ${entry.alias_of}` : entry.name} onMouseDown={(event) => { event.preventDefault(); commit(entry.name, entry.category) }}>
          <span className={`tm-pill ${tagCategoryClass(entry.category)}`}>{tagCategoryLabel(entry.category)}</span>
          <span className="tm-suggest-name">{entry.name}</span>
          <small>{formatPostCount(entry.post_count)}</small>
        </button>
      </li>)}
    </ul>}
  </div>
}
