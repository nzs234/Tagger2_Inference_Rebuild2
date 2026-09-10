import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { ApiError } from '../../lib/api'
import {
  tagManagerApi,
  type TagManagerEditableContent,
  type TagManagerImageDetail,
  type TagManagerImageSummary,
} from '../../lib/tagManager'
import type { NoticeTone } from './useNoticeQueue'

export interface UseTagEditorOptions {
  activeId?: string
  /** Current page images (the mirror kept by useTagManagerImages). */
  images: TagManagerImageSummary[]
  /** True once the images query has data for the current page key (placeholder included). */
  imagesReady: boolean
  page: number
  totalPages: number
  setPage: (page: number) => void
  /** Push a page notice (queue entry). */
  notify: (tone: NoticeTone, text: string) => void
  /** Report a failure; the page maps backend error codes to Chinese copy. */
  fail: (error: unknown, fallback: string) => void
}

/**
 * Editor state for one image: which image is open, its detail query, the save
 * mutation (including the save-then-navigate flow across page edges), the
 * conflict flag and the explicit-reload resync token.  The page image list and
 * pagination flow in through the options; nothing is shared implicitly.
 */
export function useTagEditor({ activeId, images, imagesReady, page, totalPages, setPage, notify, fail }: UseTagEditorOptions) {
  const queryClient = useQueryClient()
  const [editingId, setEditingId] = useState<number>()
  const [saveConflict, setSaveConflict] = useState(false)
  const [saveRevision, setSaveRevision] = useState(0)
  // Bumped on an explicit reload after a conflict; the drawer resyncs its
  // draft from the fresh detail once (normal refetches never clobber it).
  const [editorSync, setEditorSync] = useState(0)
  // Cross-page editor navigation: flipping past the page edge stores which end
  // of the new page to open ('first'/'last'); the effect below consumes it as
  // soon as the flipped-to page's images arrive.
  const pendingNavigate = useRef<'first' | 'last' | null>(null)

  // Position of the editing image on the current page; -1 when not open.
  const editingIndex = editingId == null ? -1 : images.findIndex((image) => image.id === editingId)

  const detailQuery = useQuery({
    queryKey: ['tag-manager', 'image', activeId, editingId],
    queryFn: () => tagManagerApi.imageDetail(activeId as string, editingId as number),
    enabled: Boolean(activeId) && editingId != null,
  })

  // Consume a cross-page navigation request.  Gated on a successful query for
  // the flipped-to page: while it loads, `images` still mirrors the previous
  // page (or is briefly empty when the new key has no cache yet), which must
  // neither consume nor cancel the pending target.  The drawer tolerates the
  // transient editingIndex === -1 state by simply disabling its nav buttons.
  useEffect(() => {
    if (pendingNavigate.current == null || !imagesReady || images.length === 0) return
    const target = pendingNavigate.current === 'first' ? images[0] : images[images.length - 1]
    pendingNavigate.current = null
    if (!target) return
    setEditingId(target.id)
    setSaveConflict(false)
  }, [images, imagesReady])

  const openImage = (imageId: number) => {
    setEditingId(imageId)
    setSaveConflict(false)
  }
  const closeEditor = () => {
    pendingNavigate.current = null
    setEditingId(undefined)
    setSaveConflict(false)
  }
  /** Cancel a pending cross-page navigation (session switch, filter/sort change). */
  const cancelPendingNavigate = () => {
    pendingNavigate.current = null
  }

  const saveMutation = useMutation({
    mutationFn: ({ imageId, content, expectedSidecarMtime, nextAction }: {
      imageId: number
      content: TagManagerEditableContent
      expectedSidecarMtime?: number | string
      nextAction?: 'close' | 'next' | 'prev'
    }) => tagManagerApi.updateImage(activeId as string, imageId, {
      content,
      expected_sidecar_mtime: expectedSidecarMtime ?? undefined,
    }).then((result) => ({ result, nextAction })),
    onSuccess: ({ result, nextAction }) => {
      setSaveConflict(false)
      setSaveRevision((revision) => revision + 1)
      queryClient.setQueryData(
        ['tag-manager', 'image', activeId, result.image_id],
        (current: TagManagerImageDetail | undefined) => current
          ? { ...current, sidecar_mtime: result.sidecar_mtime }
          : current,
      )
      notify('success', '标签已保存')
      // Precise invalidation: only this image's detail and the images/stats
      // aggregates depend on a single-image save; unrelated queries (roots,
      // dataset list) keep their cache.
      const sessionId = activeId as string
      void queryClient.invalidateQueries({ queryKey: ['tag-manager', 'image', sessionId, result.image_id] })
      void queryClient.invalidateQueries({ queryKey: ['tag-manager', 'images', sessionId] })
      void queryClient.invalidateQueries({ queryKey: ['tag-manager', 'stats', sessionId] })
      if (nextAction === 'close') {
        closeEditor()
      } else if (nextAction) {
        const delta = nextAction === 'next' ? 1 : -1
        const adjacent = images[editingIndex + delta]
        if (adjacent) {
          setEditingId(adjacent.id)
        } else {
          // Page edge: flip the page and open its far end once the new page's
          // images arrive (consumed by the pendingNavigate effect).
          const nextPage = page + delta
          if (nextPage >= 0 && nextPage < totalPages) {
            setPage(nextPage)
            pendingNavigate.current = delta === 1 ? 'first' : 'last'
          }
        }
      }
    },
    onError: (error) => {
      if (error instanceof ApiError && error.code === 'sidecar_conflict') {
        setSaveConflict(true)
        return
      }
      fail(error, '标签保存失败')
    },
  })

  // Navigate to the adjacent image; a page edge chains into the adjacent page
  // via pendingNavigate instead of opening a transient empty editor.
  const navigate = (delta: -1 | 1) => {
    if (saveMutation.isPending) return
    const adjacent = images[editingIndex + delta]
    if (adjacent) {
      setEditingId(adjacent.id)
      setSaveConflict(false)
      return
    }
    const nextPage = page + delta
    if (nextPage < 0 || nextPage >= totalPages) return
    setPage(nextPage)
    pendingNavigate.current = delta === 1 ? 'first' : 'last'
  }

  /** Save the drawer's draft; the expected mtime comes from the loaded detail. */
  const save = (content: TagManagerEditableContent, action?: 'close' | 'next' | 'prev') => {
    if (editingId == null || !detailQuery.data) return
    saveMutation.mutate({
      imageId: editingId,
      content,
      expectedSidecarMtime: detailQuery.data.sidecar_mtime ?? undefined,
      nextAction: action,
    })
  }

  // Conflict recovery: refetch the detail, then bump the sync token so the
  // drawer resets its draft from the fresh server content.  The bump must wait
  // for the refetch: bumping before would resync from the stale cached detail
  // and let an old draft pair with the new sidecar_mtime on the next save.
  const reload = () => {
    setSaveConflict(false)
    void queryClient
      .invalidateQueries({ queryKey: ['tag-manager', 'image', activeId, editingId] })
      .then(() => setEditorSync((value) => value + 1))
  }

  /** Draft resync token for the drawer: resets the draft when it changes. */
  const syncToken = `${editingId}:${editorSync}`

  return {
    editingId,
    editingIndex,
    saveConflict,
    syncToken,
    saveRevision,
    detail: detailQuery.data,
    detailError: detailQuery.isError,
    retryDetail: () => { void detailQuery.refetch() },
    saving: saveMutation.isPending,
    openImage,
    closeEditor,
    cancelPendingNavigate,
    navigate,
    save,
    reload,
  }
}
