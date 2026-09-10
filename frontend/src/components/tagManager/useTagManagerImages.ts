import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import {
  tagManagerApi,
  translationKey,
  type ImageFilterState,
  type TagManagerImageSummary,
  type TagManagerSort,
} from '../../lib/tagManager'
import { useTagManagerView } from '../../store/tagManagerView'
import { useTagTranslationMemory } from '../../store/tagTranslationMemory'

const PAGE_SIZE = 60

/** Identity of one fetched page: session, page, sort and filter. */
function pageKey(activeId: string | undefined, page: number, sort: TagManagerSort, filter: ImageFilterState): string {
  return `${activeId ?? ''}|${page}|${sort}|${JSON.stringify(filter)}`
}

export interface UseTagManagerImagesOptions {
  activeId?: string
  /** The active session must be ready before the images query runs. */
  sessionReady: boolean
}

/**
 * Paginated image list for the active session.  Page/sort/filter come from the
 * persisted view store; a local mirror of the fetched page keeps consumers
 * (grid selection) stable between refetches, and a restored page pointing past
 * the last page is clamped as soon as a real result arrives.
 *
 * The fetched payload carries the key it was requested for, so a placeholder
 * window (previous session/page still shown while the next page loads) is
 * distinguishable from real data for the current key: `pageImages`/`imagesReady`
 * only expose data that genuinely belongs to the current key.
 */
export function useTagManagerImages({ activeId, sessionReady }: UseTagManagerImagesOptions) {
  const page = useTagManagerView((state) => state.page)
  const filter = useTagManagerView((state) => state.filter)
  const sort = useTagManagerView((state) => state.sort)
  const setPage = useTagManagerView((state) => state.setPage)
  const setViewFilter = useTagManagerView((state) => state.setFilter)
  const setSort = useTagManagerView((state) => state.setSort)

  const currentKey = pageKey(activeId, page, sort, filter)
  const [images, setImages] = useState<TagManagerImageSummary[]>([])
  const imagesQuery = useQuery({
    queryKey: ['tag-manager', 'images', activeId, page, sort, filter],
    queryFn: async () => {
      const result = await tagManagerApi.images(activeId as string, { offset: page * PAGE_SIZE, limit: PAGE_SIZE, sort, filter })
      return { key: pageKey(activeId, page, sort, filter), result }
    },
    enabled: Boolean(activeId) && sessionReady,
    placeholderData: (previous) => previous,
  })
  // Only a payload fetched for the current key counts; the placeholder (the
  // previous key's data, kept visible in the grid meanwhile) does not.
  const payload = imagesQuery.data
  const currentPayload = payload && payload.key === currentKey ? payload.result : undefined
  const fetchedImages = currentPayload?.items
  // Keep a local mirror so the selection hook and the grid always see the
  // latest fetched page even between refetches and across placeholder windows.
  useEffect(() => {
    if (fetchedImages) setImages(fetchedImages)
  }, [fetchedImages])
  const total = (currentPayload ?? payload?.result)?.total ?? 0
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  // A restored page may point past the last page after the data changed; clamp
  // it as soon as a real page result (not a disabled query) is available.
  useEffect(() => {
    if (imagesQuery.isSuccess && page > totalPages - 1) setPage(totalPages - 1)
  }, [imagesQuery.isSuccess, page, totalPages, setPage])

  // Tags on this page the offline dictionary does not cover and no on-demand
  // translation has been saved for yet; feeding the 在线翻译 button.
  const translationMemory = useTagTranslationMemory((state) => state.map)
  const missingTags = useMemo(() => {
    const seen = new Set<string>()
    for (const image of images) {
      for (const entry of image.tags) {
        if (entry.translation) continue
        const key = translationKey(entry.tag)
        if (key && !seen.has(key) && !translationMemory[key]) seen.add(key)
      }
    }
    return [...seen]
  }, [images, translationMemory])

  return {
    /** Mirrored page for grid/selection: stays visible across placeholder windows. */
    images,
    /** Images fetched for the current session+page key; empty while a placeholder window is open. */
    pageImages: fetchedImages ?? [],
    /** True when `pageImages` really belongs to the current session+page key. */
    imagesReady: currentPayload != null,
    total,
    totalPages,
    page,
    setPage,
    filter,
    sort,
    setViewFilter,
    setSort,
    missingTags,
    imagesError: imagesQuery.isError,
    retryImages: () => { void imagesQuery.refetch() },
  }
}
