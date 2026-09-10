import { ImageIcon, Pencil } from 'lucide-react'
import { useEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import { tagCategoryClass } from '../../lib/tagCategories'
import { formatTagForDisplay, translationKey, type TagManagerImageSummary } from '../../lib/tagManager'
import { usePreferences } from '../../store/app'
import { useTagTranslationMemory } from '../../store/tagTranslationMemory'

const CARD_HEIGHT = 224
const CARD_MIN_WIDTH = 150
const GRID_GAP = 10

// Cards are fixed height, so only the first few tags fit; the drawer shows all.
const CARD_TAG_LIMIT = 4

function sidecarLabel(kind: TagManagerImageSummary['sidecar_kind']): string {
  if (kind === 'tag_txt') return 'TXT'
  if (kind === 'tags_json') return 'JSON'
  if (kind === 'standard_json') return '标准 JSON'
  if (kind === 'raw_e621_json') return 'E621 JSON'
  return '无 sidecar'
}

function useElementSize(ref: RefObject<HTMLElement | null>): { width: number; height: number; measured: boolean } {
  const [size, setSize] = useState({ width: 0, height: 0, measured: false })
  useEffect(() => {
    const element = ref.current
    if (!element || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect
      if (rect) setSize({ width: rect.width, height: rect.height, measured: true })
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [ref])
  return size
}

/**
 * Authorized thumbnail fetch: LAN+token deployments reject plain <img src>
 * URLs (the authorize dependency only reads the Authorization header), so the
 * blob is fetched through the shared client and exposed as an object URL.
 * Each blob URL is revoked when the card unmounts or the fetch key changes.
 */
function GridThumb({ image, loadImage }: {
  image: TagManagerImageSummary
  loadImage: (imageId: number) => Promise<Blob>
}) {
  const [url, setUrl] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    let revoked = false
    let objectUrl: string | null = null
    setFailed(false)
    setUrl(null)
    loadImage(image.id).then((blob) => {
      if (revoked) return
      objectUrl = URL.createObjectURL(blob)
      setUrl(objectUrl)
    }).catch(() => {
      if (!revoked) setFailed(true)
    })
    return () => {
      revoked = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
    // Key the fetch on the image id plus the stable loader: the `image` object
    // itself is recreated on every parent render (scroll, selection, refetch),
    // and depending on it would refetch every visible thumbnail each time.
  }, [image.id, loadImage])
  if (failed || url === null) {
    if (failed) {
      return <div className="tm-thumb tm-thumb-failed" role="img" aria-label={image.file_name}><ImageIcon size={26} aria-hidden="true" /></div>
    }
    return <div className="tm-thumb tm-thumb-loading" role="img" aria-label={image.file_name} />
  }
  return <img className="tm-thumb" src={url} alt={image.file_name} loading="lazy" />
}

/** Helpers handed to every card so keyboard navigation can cross grid cells. */
export interface GridNavHelpers {
  columns: number
  /** Moves keyboard focus to the card at `index`, scrolling it into view. */
  focusItem: (index: number) => void
}

/**
 * Windowed grid: only the rows around the scroll viewport are mounted.
 * The container height follows the layout (CSS `tm-grid-wrap` uses the
 * available panel height), so large screens no longer waste space behind a
 * hardcoded 560px window.  When the container cannot be measured (no
 * ResizeObserver, hidden layout) every row renders so tests and degraded
 * environments stay functional.
 *
 * Accessibility: `role="grid"` with real `role="row"` wrappers (laid out with
 * `display: contents`, so the flat CSS grid is untouched) and `role="gridcell"`
 * cards.  Grid is the right primitive here because cells legitimately contain
 * interactive widgets (checkbox, edit button) — a listbox would forbid that.
 */
export function VirtualGrid({ count, empty, ariaLabel, renderItem }: {
  count: number
  empty?: ReactNode
  ariaLabel?: string
  renderItem: (index: number, helpers: GridNavHelpers) => ReactNode
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  const size = useElementSize(containerRef)
  const [scrollTop, setScrollTop] = useState(0)
  // Keyboard focus deferred to the effect below when the target row is
  // currently windowed out of the DOM.
  const pendingFocus = useRef<number | null>(null)
  const columns = size.width > 0 ? Math.max(2, Math.floor((size.width + GRID_GAP) / (CARD_MIN_WIDTH + GRID_GAP))) : 6
  const rowCount = Math.ceil(count / columns)
  const rowStride = CARD_HEIGHT + GRID_GAP
  const canWindow = size.height > 0
  const firstRow = canWindow ? Math.max(0, Math.floor(scrollTop / rowStride) - 2) : 0
  const lastRow = canWindow ? Math.min(rowCount, firstRow + Math.ceil(size.height / rowStride) + 4) : rowCount
  const rows: number[] = []
  for (let row = firstRow; row < lastRow; row += 1) rows.push(row)

  const focusItem = (index: number) => {
    const container = containerRef.current
    if (!container) return
    const target = container.querySelector<HTMLElement>(`[data-vm-index="${index}"]`)
    if (target) {
      if (typeof target.scrollIntoView === 'function') target.scrollIntoView({ block: 'nearest' })
      target.focus()
      return
    }
    // The row is windowed out: scroll it into the viewport, then let the
    // effect below focus the card once the re-render mounts it.  If the row
    // still fails to mount (measurement unavailable), the user simply presses
    // the arrow again — an accepted trade-off of the simple windowing.
    const row = Math.floor(index / columns)
    const top = Math.max(0, row * rowStride)
    pendingFocus.current = index
    container.scrollTop = top
    setScrollTop(top)
  }

  // Deferred keyboard focus: runs after every render, so a card scrolled into
  // the window by `focusItem` is focused as soon as it appears.
  useEffect(() => {
    if (pendingFocus.current == null) return
    const target = containerRef.current?.querySelector<HTMLElement>(`[data-vm-index="${pendingFocus.current}"]`)
    if (!target) return
    pendingFocus.current = null
    if (typeof target.scrollIntoView === 'function') target.scrollIntoView({ block: 'nearest' })
    target.focus()
  })

  if (count === 0) return <div className="tm-grid-wrap" style={{ minHeight: 200 }}>{empty}</div>
  return (
    <div
      ref={containerRef}
      className="tm-grid-wrap"
      role="grid"
      aria-label={ariaLabel}
      onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}
    >
      <div
        className="tm-grid"
        style={{
          gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))`,
          paddingTop: firstRow * rowStride,
          paddingBottom: Math.max(0, (rowCount - lastRow) * rowStride),
        }}
      >
        {rows.map((row) => (
          // `display: contents` keeps the flat CSS grid layout while exposing
          // the ARIA grid > row > gridcell structure to assistive tech.
          <div key={row} role="row" style={{ display: 'contents' }}>
            {Array.from({ length: columns }, (_, column) => {
              const index = row * columns + column
              return index < count
                ? <GridCell key={index} index={index} helpers={{ columns, focusItem }} renderItem={renderItem} />
                : <div key={`spacer-${index}`} aria-hidden="true" />
            })}
          </div>
        ))}
      </div>
    </div>
  )
}

function GridCell({ index, helpers, renderItem }: {
  index: number
  helpers: GridNavHelpers
  renderItem: (index: number, helpers: GridNavHelpers) => ReactNode
}) {
  return <>{renderItem(index, helpers)}</>
}

function CardTags({ tags }: { tags: TagManagerImageSummary['tags'] }) {
  const bilingual = usePreferences((state) => state.bilingualTags)
  const tagStyle = usePreferences((state) => state.tagStyle)
  // On-demand translations saved this session live in the memory store until
  // the next server fetch annotates them natively.
  const memory = useTagTranslationMemory((state) => state.map)
  if (tags.length === 0) return null
  return <div className="tm-card-tags">
    {tags.slice(0, CARD_TAG_LIMIT).map((entry) => {
      const display = formatTagForDisplay(entry.tag, tagStyle)
      const translation = bilingual
        ? entry.translation ?? memory[translationKey(entry.tag)] ?? null
        : null
      return <span
        key={entry.tag}
        className={`tm-pill ${tagCategoryClass(entry.category)}`}
        title={translation ? `${display} · ${translation}` : display}
      >
        <span>{display}</span>
        {translation && <span className="tm-pill-zh">{translation}</span>}
      </span>
    })}
    {tags.length > CARD_TAG_LIMIT && <span className="tm-card-tags-more">+{tags.length - CARD_TAG_LIMIT}</span>}
  </div>
}

/**
 * Interaction semantics: a plain click toggles selection (shift/ctrl+click
 * extends it as a range), a double click or the corner 编辑 button opens the
 * editor, and the card body is the roving-tabindex keyboard hub — arrows move
 * focus between cards, Space toggles selection, Enter opens the editor.
 */
export function ImageGrid({ images, loadThumbnail, selectedIds, editingId, empty, onToggleSelect, onOpen }: {
  images: TagManagerImageSummary[]
  loadThumbnail: (imageId: number) => Promise<Blob>
  selectedIds: ReadonlySet<number>
  editingId?: number
  empty?: ReactNode
  onToggleSelect: (image: TagManagerImageSummary, index: number, modifiers: { shift: boolean; ctrl: boolean }) => void
  onOpen: (image: TagManagerImageSummary) => void
}) {
  // Roving tabindex: exactly one card is the grid's tab stop.  The clamp keeps
  // a tab stop alive when the page shrinks (paging, filtering) under the focus.
  const [focusIndex, setFocusIndex] = useState(0)
  const activeIndex = Math.min(focusIndex, Math.max(0, images.length - 1))
  return <VirtualGrid
    count={images.length}
    empty={empty}
    ariaLabel="图片网格"
    renderItem={(index, { columns, focusItem }) => {
      const image = images[index]
      if (!image) return null
      const selected = selectedIds.has(image.id)
      return (
        <div
          key={image.id}
          role="gridcell"
          aria-selected={selected}
          className={`tm-card ${selected ? 'tm-card-selected' : ''} ${editingId === image.id ? 'tm-card-editing' : ''}`}
        >
          <label className="tm-card-check">
            <input
              type="checkbox"
              aria-label={`选择 ${image.file_name}`}
              checked={selected}
              onClick={(event) => event.stopPropagation()}
              onChange={() => onToggleSelect(image, index, { shift: false, ctrl: false })}
            />
          </label>
          <button
            type="button"
            className="tm-card-body"
            title={image.relative_path}
            data-vm-index={index}
            tabIndex={index === activeIndex ? 0 : -1}
            onFocus={() => setFocusIndex(index)}
            onClick={(event) => {
              if (event.shiftKey || event.ctrlKey || event.metaKey) {
                onToggleSelect(image, index, { shift: event.shiftKey, ctrl: event.ctrlKey || event.metaKey })
              } else {
                onToggleSelect(image, index, { shift: false, ctrl: false })
              }
            }}
            onDoubleClick={() => onOpen(image)}
            onKeyDown={(event) => {
              if (event.shiftKey || event.ctrlKey || event.metaKey || event.altKey) return
              if (event.key === 'ArrowRight' || event.key === 'ArrowLeft' || event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                event.preventDefault()
                const delta = event.key === 'ArrowRight'
                  ? 1
                  : event.key === 'ArrowLeft'
                    ? -1
                    : event.key === 'ArrowDown' ? columns : -columns
                const target = index + delta
                if (target >= 0 && target < images.length) focusItem(target)
                return
              }
              if (event.key === ' ') {
                // preventDefault stops the button's synthetic click so Space
                // toggles the selection exactly once.
                event.preventDefault()
                onToggleSelect(image, index, { shift: false, ctrl: false })
                return
              }
              if (event.key === 'Enter') {
                event.preventDefault()
                onOpen(image)
              }
            }}
          >
            <GridThumb image={image} loadImage={loadThumbnail} />
            <span className="tm-card-name">{image.file_name}</span>
          </button>
          {/* Explicit editor entry point: a sibling of the card body (never
              nested inside it) so interactive elements stay un-nested. */}
          <button
            type="button"
            className="tm-card-edit"
            title={`编辑 ${image.file_name}`}
            aria-label={`编辑 ${image.file_name}`}
            onClick={() => onOpen(image)}
          >
            <Pencil size={13} aria-hidden="true" />
          </button>
          <div className="tm-card-badges">
            <span className="tm-badge">{image.tag_count} 标签</span>
            <span className={`tm-badge ${image.sidecar_kind === 'none' ? 'tm-badge-missing' : ''}`}>{sidecarLabel(image.sidecar_kind)}</span>
          </div>
          <CardTags tags={image.tags} />
        </div>
      )
    }}
  />
}
