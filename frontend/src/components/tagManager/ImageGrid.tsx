import { ImageIcon } from 'lucide-react'
import { useEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import { tagCategoryClass } from '../../lib/tagCategories'
import { formatTagForDisplay, type TagManagerImageSummary } from '../../lib/tagManager'
import { usePreferences } from '../../store/app'

const CARD_HEIGHT = 224
const CARD_MIN_WIDTH = 150
const GRID_GAP = 10
const SCROLL_CONTAINER_HEIGHT = 560

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
 * Windowed grid: only the rows around the scroll viewport are mounted.
 * When the container cannot be measured (no ResizeObserver, hidden layout)
 * every row renders so tests and degraded environments stay functional.
 */
export function VirtualGrid({ count, empty, renderItem }: {
  count: number
  empty?: ReactNode
  renderItem: (index: number) => ReactNode
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  const size = useElementSize(containerRef)
  const [scrollTop, setScrollTop] = useState(0)
  const columns = size.width > 0 ? Math.max(2, Math.floor((size.width + GRID_GAP) / (CARD_MIN_WIDTH + GRID_GAP))) : 6
  const rowCount = Math.ceil(count / columns)
  const rowStride = CARD_HEIGHT + GRID_GAP
  const canWindow = size.height > 0
  const firstRow = canWindow ? Math.max(0, Math.floor(scrollTop / rowStride) - 2) : 0
  const lastRow = canWindow ? Math.min(rowCount, firstRow + Math.ceil(size.height / rowStride) + 4) : rowCount
  const rows: number[] = []
  for (let row = firstRow; row < lastRow; row += 1) rows.push(row)

  if (count === 0) return <div className="tm-grid-wrap" style={{ height: SCROLL_CONTAINER_HEIGHT }}>{empty}</div>
  return (
    <div
      ref={containerRef}
      className="tm-grid-wrap"
      style={{ height: SCROLL_CONTAINER_HEIGHT }}
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
        {rows.flatMap((row) => Array.from({ length: columns }, (_, column) => {
          const index = row * columns + column
          return index < count
            ? <GridCell key={index} index={index} renderItem={renderItem} />
            : <div key={`spacer-${index}`} aria-hidden="true" />
        }))}
      </div>
    </div>
  )
}

function GridCell({ index, renderItem }: { index: number; renderItem: (index: number) => ReactNode }) {
  return <>{renderItem(index)}</>
}

function GridThumb({ url, name }: { url: string; name: string }) {
  const [failed, setFailed] = useState(false)
  if (failed) {
    return <div className="tm-thumb tm-thumb-failed" role="img" aria-label={name}><ImageIcon size={26} aria-hidden="true" /></div>
  }
  return <img className="tm-thumb" src={url} alt={name} loading="lazy" onError={() => setFailed(true)} />
}

function CardTags({ tags }: { tags: TagManagerImageSummary['tags'] }) {
  const bilingual = usePreferences((state) => state.bilingualTags)
  const tagStyle = usePreferences((state) => state.tagStyle)
  if (tags.length === 0) return null
  return <div className="tm-card-tags">
    {tags.slice(0, CARD_TAG_LIMIT).map((entry) => {
      const display = formatTagForDisplay(entry.tag, tagStyle)
      const translation = bilingual ? entry.translation : null
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

export function ImageGrid({ images, thumbnailUrl, selectedIds, editingId, empty, onToggleSelect, onOpen }: {
  images: TagManagerImageSummary[]
  thumbnailUrl: (image: TagManagerImageSummary) => string
  selectedIds: ReadonlySet<number>
  editingId?: number
  empty?: ReactNode
  onToggleSelect: (image: TagManagerImageSummary, index: number, modifiers: { shift: boolean; ctrl: boolean }) => void
  onOpen: (image: TagManagerImageSummary) => void
}) {
  return <VirtualGrid
    count={images.length}
    empty={empty}
    renderItem={(index) => {
      const image = images[index]
      if (!image) return null
      const selected = selectedIds.has(image.id)
      return (
        <div key={image.id} className={`tm-card ${selected ? 'tm-card-selected' : ''} ${editingId === image.id ? 'tm-card-editing' : ''}`}>
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
            onClick={(event) => {
              if (event.shiftKey || event.ctrlKey || event.metaKey) {
                onToggleSelect(image, index, { shift: event.shiftKey, ctrl: event.ctrlKey || event.metaKey })
              } else {
                onOpen(image)
              }
            }}
          >
            <GridThumb url={thumbnailUrl(image)} name={image.file_name} />
            <span className="tm-card-name">{image.file_name}</span>
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
