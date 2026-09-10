import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import {
  tagManagerApi,
  translationKey,
  type TagManagerImageSummary,
} from '../../lib/tagManager'
import { useTagManagerView } from '../../store/tagManagerView'
import { useTagTranslationMemory } from '../../store/tagTranslationMemory'

const PAGE_SIZE = 60

export interface UseTagManagerImagesOptions {
  activeId?: string
  /** The active session must be ready before the images query runs. */
  sessionReady: boolean
}

/**
 * Paginated image list for the active session.  Page/sort/filter come from the
 * persisted view store; a local mirror of the fetched page keeps consumers
 * (grid selection, editor) stable between refetches, and a restored page
 * pointing past the last page is clamped as soon as a real result arrives.
 */
export function useTagManagerImages({ activeId, sessionReady }: UseTagManagerImagesOptions) {
  const page = useTagManagerView((state) => state.page)
  const filter = useTagManagerView((state) => state.filter)
  const sort = useTagManagerView((state) => state.sort)
  const setPage = useTagManagerView((state) => state.setPage)
  const setViewFilter = useTagManagerView((state) => state.setFilter)
  const setSort = useTagManagerView((state) => state.setSort)

  const [images, setImages] = useState<TagManagerImageSummary[]>([])
  const imagesQuery = useQuery({
    queryKey: ['tag-manager', 'images', activeId, page, sort, filter],
    queryFn: () => tagManagerApi.images(activeId as string, { offset: page * PAGE_SIZE, limit: PAGE_SIZE, sort, filter }),
    enabled: Boolean(activeId) && sessionReady,
    placeholderData: (previous) => previous,
  })
  // Keep a local mirror so the selection hook and the editor always see the
  // latest page even between refetches; derived memos stay for translations.
  const fetchedImages = useMemo(() => imagesQuery.data?.items ?? [], [imagesQuery.data])
  useEffect(() => setImages(fetchedImages), [fetchedImages])
  const total = imagesQuery.data?.total ?? 0
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
    images,
    total,
    totalPages,
    page,
    setPage,
    filter,
    sort,
    setViewFilter,
    setSort,
    missingTags,
    /** True while the current page key has data (placeholder data included). */
    imagesReady: imagesQuery.isSuccess,
    imagesError: imagesQuery.isError,
    retryImages: () => { void imagesQuery.refetch() },
  }
}
