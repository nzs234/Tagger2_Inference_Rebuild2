import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useRef, useState } from 'react'
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
  /** Images of the current page key; empty while the fetched page still belongs to another key (placeholder window). */
  images: TagManagerImageSummary[]
  /** True only when `images` was fetched for the current session+page key (placeholder data excluded). */
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
  // Bumped on every failed save attempt (conflict included); the drawer uses
  // it to drop the pending baseline of the failed attempt.
  const [saveErrorToken, setSaveErrorToken] = useState(0)
  // Bumped on an explicit reload after a conflict; the drawer resyncs its
  // draft from the fresh detail once (normal refetches never clobber it).
  const [editorSync, setEditorSync] = useState(0)
  // Cross-page editor navigation: flipping past the page edge stores which end
  // of the new page to open ('first'/'last') plus the session and page it was
  // created for; the effect below consumes it as soon as that page's images
  // arrive.  The stamp keeps a placeholder window (previous session/page still
  // mirrored) from resolving the target against the wrong image list.
  const pendingNavigate = useRef<{ end: 'first' | 'last'; sessionId: string; page: number } | null>(null)
  // The open drawer owns the draft, so only it knows whether a session switch
  // would discard unsaved work.  It publishes an imperative guard here; the
  // page's session-switch entry points run their switch through
  // `confirmLeaveIfDirty` so a dirty draft (or an in-flight save) is confirmed
  // before the active session changes under the editor.
  const leaveGuard = useRef<((action: () => void) => void) | null>(null)
  const registerLeaveGuard = useCallback((guard: ((action: () => void) => void) | null) => {
    leaveGuard.current = guard
  }, [])
  /** Run `action`, first asking the open editor to confirm when it has a dirty
   * draft or a save in flight.  With no editor open the action runs at once. */
  const confirmLeaveIfDirty = useCallback((action: () => void) => {
    const guard = leaveGuard.current
    if (guard) guard(action)
    else action()
  }, [])

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
    const pending = pendingNavigate.current
    if (!pending || !imagesReady || images.length === 0) return
    pendingNavigate.current = null
    // The target belongs to the session/page it was created for: a session
    // switch (create, auto-fallback, delete) or manual pagination in the
    // meantime invalidates it instead of opening a surprise image.
    if (pending.sessionId !== activeId || pending.page !== page) return
    const target = pending.end === 'first' ? images[0] : images[images.length - 1]
    if (!target) return
    setEditingId(target.id)
    setSaveConflict(false)
  }, [images, imagesReady, activeId, page])

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
          // A response without a usable mtime must never regress the cached
          // expectation: the next consecutive save would then send the stale
          // (or a wiped) mtime and either falsely conflict or skip the check.
          ? { ...current, sidecar_mtime: result.sidecar_mtime ?? current.sidecar_mtime }
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
      // A save appends a journal entry, so the session detail's can_undo flag
      // must refetch; otherwise the 撤销 button would stay disabled until an
      // unrelated refetch happens.
      void queryClient.invalidateQueries({ queryKey: ['tag-manager', 'dataset', sessionId] })
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
            pendingNavigate.current = { end: delta === 1 ? 'first' : 'last', sessionId, page: nextPage }
          }
        }
      }
    },
    onError: (error) => {
      setSaveErrorToken((token) => token + 1)
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
    pendingNavigate.current = { end: delta === 1 ? 'first' : 'last', sessionId: activeId as string, page: nextPage }
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
    saveErrorToken,
    detail: detailQuery.data,
    detailError: detailQuery.isError,
    retryDetail: () => { void detailQuery.refetch() },
    saving: saveMutation.isPending,
    openImage,
    closeEditor,
    cancelPendingNavigate,
    confirmLeaveIfDirty,
    registerLeaveGuard,
    navigate,
    save,
    reload,
  }
}
