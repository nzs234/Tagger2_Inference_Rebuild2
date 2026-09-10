import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { TagManagerImageSummary } from '../../lib/tagManager'

/**
 * Grid selection state with a stable-image-id anchor.
 *
 * The anchor is an image id, not a page index: paging, resorting or refiltering
 * changes indices, so a shift-click after navigation must resolve both range
 * ends through the *current* page by id.  The anchor is cleared whenever the
 * page composition changes underneath it (new session, filter, sort or page),
 * which turns a stale shift-click into a plain toggle instead of a wrong range.
 */
export function useImageSelection(images: TagManagerImageSummary[]) {
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const anchorRef = useRef<number | null>(null)

  // Identity of the current page: any change resets the range anchor.  Page
  // number is not part of it — the visible ids already capture the page.
  const pageKey = useMemo(() => images.map((image) => image.id).join('|'), [images])
  const lastPageKey = useRef<string | null>(null)
  useEffect(() => {
    if (lastPageKey.current !== pageKey) {
      lastPageKey.current = pageKey
      anchorRef.current = null
    }
  }, [pageKey])

  const indexOfId = useCallback(
    (imageId: number) => images.findIndex((image) => image.id === imageId),
    [images],
  )

  const toggle = useCallback((
    image: TagManagerImageSummary,
    modifiers: { shift: boolean; ctrl: boolean },
  ) => {
    // The anchor must be read synchronously, BEFORE this click overwrites it:
    // React defers state updaters, so an anchor read inside the updater would
    // already see the just-clicked id and every shift-range would degenerate
    // into a plain toggle of the clicked card.
    const anchor = anchorRef.current
    if (modifiers.shift && anchor != null) {
      const anchorIndex = indexOfId(anchor)
      const targetIndex = indexOfId(image.id)
      if (anchorIndex !== -1 && targetIndex !== -1) {
        const from = Math.min(anchorIndex, targetIndex)
        const to = Math.max(anchorIndex, targetIndex)
        setSelectedIds((current) => {
          const next = new Set(current)
          for (let candidate = from; candidate <= to; candidate += 1) {
            const item = images[candidate]
            if (item) next.add(item.id)
          }
          return next
        })
        anchorRef.current = image.id
        return
      }
      // Unknown anchor (page changed): fall through to a plain toggle.
    }
    setSelectedIds((current) => {
      const next = new Set(current)
      if (next.has(image.id)) next.delete(image.id)
      else next.add(image.id)
      return next
    })
    anchorRef.current = image.id
  }, [images, indexOfId])

  const selectAll = useCallback(() => {
    setSelectedIds((current) => {
      const next = new Set(current)
      images.forEach((image) => next.add(image.id))
      return next
    })
  }, [images])

  const clear = useCallback(() => {
    setSelectedIds(new Set())
    anchorRef.current = null
  }, [])

  const selectedIdList = useMemo(() => [...selectedIds], [selectedIds])

  return { selectedIds, selectedIdList, toggle, selectAll, clear, setSelectedIds }
}
