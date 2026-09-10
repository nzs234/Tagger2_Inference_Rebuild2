import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useMemo, useRef } from 'react'
import type { TagManagerBatchRequest, TagManagerSession } from '../../lib/tagManager'
import { tagManagerApi } from '../../lib/tagManager'
import { useTagManagerView } from '../../store/tagManagerView'
import type { NoticeTone } from './useNoticeQueue'

/** Page-injected callbacks so this hook stays free of notice/editor state. */
export interface UseTagManagerSessionsOptions {
  /** Push a page notice (queue entry). */
  notify: (tone: NoticeTone, text: string) => void
  /** Report a failure; the page maps backend error codes to Chinese copy. */
  fail: (error: unknown, fallback: string) => void
  /** Runs after the active session was deleted: the page drops grid selection and editor state. */
  onSessionRemoved: () => void
  /** Runs after a batch/undo/redo rewrote session data: the page drops the grid selection. */
  onSessionDataChanged: () => void
}

/**
 * Session layer of the Tag Manager page: the datasets list query (polling
 * while any session indexes), the active-session query, the session-scoped
 * write mutations (create/refresh/delete plus batch/undo/redo) and the
 * selection logic.  Persistence of `activeId` lives in
 * store/tagManagerView.ts; page-level state (grid selection, editor) flows in
 * through the options and is never read from shared refs here.
 */
export function useTagManagerSessions({ notify, fail, onSessionRemoved, onSessionDataChanged }: UseTagManagerSessionsOptions) {
  const queryClient = useQueryClient()
  const activeId = useTagManagerView((state) => state.activeId)
  const setActiveId = useTagManagerView((state) => state.setActiveId)

  const datasets = useQuery({
    queryKey: ['tag-manager', 'datasets'],
    queryFn: tagManagerApi.datasets,
    refetchInterval: (query) => (query.state.data?.items.some((item) => item.status === 'indexing') ? 1500 : false),
    retry: false,
  })

  const sessions = useMemo(() => datasets.data?.items ?? [], [datasets.data])
  const sessionFromList = useMemo(
    () => sessions.find((item) => item.id === activeId),
    [sessions, activeId],
  )
  const activeQuery = useQuery({
    queryKey: ['tag-manager', 'dataset', activeId],
    queryFn: () => tagManagerApi.dataset(activeId as string),
    enabled: Boolean(activeId),
    refetchInterval: (query) => (query.state.data?.status === 'indexing' ? 1500 : false),
  })
  const session: TagManagerSession | undefined = activeQuery.data ?? sessionFromList
  const sessionReady = session?.status === 'ready'

  // Sessions picked before the datasets list has caught up (e.g. just created)
  // must survive the list validation below.
  const pendingSessionIds = useRef(new Set<string>())
  const selectSession = useCallback((id?: string) => {
    if (id) pendingSessionIds.current.add(id)
    setActiveId(id)
  }, [setActiveId])

  // Validate the restored/selected session against the loaded list: a stale id
  // (persisted from an older index, or left over after a deletion) falls back
  // to the first session.  An explicitly picked id that the still-stale list
  // does not know yet is honoured via `pendingSessionIds`.
  useEffect(() => {
    if (sessions.length === 0) return
    if (!activeId) {
      selectSession(sessions[0]?.id)
      return
    }
    if (sessions.some((item) => item.id === activeId)) {
      pendingSessionIds.current.delete(activeId)
      return
    }
    if (!pendingSessionIds.current.has(activeId)) selectSession(sessions[0]?.id)
  }, [activeId, sessions, selectSession])

  const previousStatus = useRef<string | undefined>(undefined)
  useEffect(() => {
    if (previousStatus.current === 'indexing' && session?.status === 'ready') {
      void queryClient.invalidateQueries({ queryKey: ['tag-manager'] })
    }
    previousStatus.current = session?.status
  }, [session?.status, queryClient])

  /** Invalidate the session-scoped queries after a write.
   * Batch/undo/redo touch an unbounded set of images, so every detail under
   * the session is dropped; roots and the dataset list keep their cache. */
  const invalidateSessionData = () => {
    const sessionId = activeId as string
    void queryClient.invalidateQueries({ queryKey: ['tag-manager', 'images', sessionId] })
    void queryClient.invalidateQueries({ queryKey: ['tag-manager', 'image', sessionId] })
    void queryClient.invalidateQueries({ queryKey: ['tag-manager', 'stats', sessionId] })
  }

  const createMutation = useMutation({
    mutationFn: tagManagerApi.createDataset,
    onSuccess: (created) => {
      selectSession(created.id)
      const label = created.name || created.relative_path || '数据集'
      notify('info', `会话「${label}」已创建，正在索引图片…`)
      void queryClient.invalidateQueries({ queryKey: ['tag-manager', 'datasets'] })
    },
    onError: (error) => fail(error, '会话创建失败'),
  })
  const refreshMutation = useMutation({
    mutationFn: (id: string) => tagManagerApi.refreshDataset(id),
    onSuccess: (refreshed) => {
      notify('info', '正在重新扫描数据集…')
      void queryClient.invalidateQueries({ queryKey: ['tag-manager', 'datasets'] })
      void queryClient.invalidateQueries({ queryKey: ['tag-manager', 'dataset', refreshed.id] })
    },
    onError: (error) => fail(error, '重新扫描失败'),
  })
  const deleteMutation = useMutation({
    mutationFn: (id: string) => tagManagerApi.deleteDataset(id),
    onSuccess: () => {
      setActiveId(undefined)
      onSessionRemoved()
      notify('success', '会话已删除')
      void queryClient.invalidateQueries({ queryKey: ['tag-manager', 'datasets'] })
    },
    onError: (error) => fail(error, '会话删除失败'),
  })
  const batchMutation = useMutation({
    mutationFn: (body: TagManagerBatchRequest) => tagManagerApi.batch(activeId as string, body),
    onSuccess: (result) => {
      onSessionDataChanged()
      notify('success', `批量操作完成，影响 ${result.affected} 张图片`)
      invalidateSessionData()
    },
    onError: (error) => fail(error, '批量操作失败'),
  })
  const undoMutation = useMutation({
    mutationFn: (id: string) => tagManagerApi.undo(id),
    onSuccess: () => {
      onSessionDataChanged()
      notify('success', '已撤销上一次操作')
      invalidateSessionData()
    },
    onError: (error) => fail(error, '撤销失败'),
  })
  const redoMutation = useMutation({
    mutationFn: (id: string) => tagManagerApi.redo(id),
    onSuccess: () => {
      onSessionDataChanged()
      notify('success', '已重做操作')
      invalidateSessionData()
    },
    onError: (error) => fail(error, '重做失败'),
  })

  return {
    sessions,
    session,
    sessionReady,
    activeId,
    selectSession,
    createMutation,
    refreshMutation,
    deleteMutation,
    batchMutation,
    undoMutation,
    redoMutation,
  }
}
