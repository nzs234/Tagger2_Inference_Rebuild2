import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useMemo, useRef } from 'react'
import type { TagManagerBatchRequest, TagManagerBatchResult, TagManagerSession } from '../../lib/tagManager'
import { tagManagerApi } from '../../lib/tagManager'
import { useTagManagerView } from '../../store/tagManagerView'
import type { NoticeTone } from './useNoticeQueue'

/**
 * Success copy for a finished batch.  Zero counters are omitted so the common
 * all-written case stays short; read-only skips and no-change targets are
 * only mentioned when the backend actually reported them.
 */
export function formatBatchResultNotice(result: TagManagerBatchResult): string {
  const parts = [`已修改 ${result.affected} 张`]
  const skipped = result.skipped_read_only ?? 0
  const noChange = result.no_change ?? 0
  if (skipped > 0) parts.push(`跳过 ${skipped} 张（只读）`)
  if (noChange > 0) parts.push(`${noChange} 张无变化`)
  return parts.join('，')
}

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
   * the session is dropped; roots and the dataset list keep their cache.  The
   * session detail is included too: its can_undo/can_redo flags flip with every
   * journal write and the toolbar renders directly from them. */
  const invalidateSessionData = () => {
    const sessionId = activeId as string
    void queryClient.invalidateQueries({ queryKey: ['tag-manager', 'images', sessionId] })
    void queryClient.invalidateQueries({ queryKey: ['tag-manager', 'image', sessionId] })
    void queryClient.invalidateQueries({ queryKey: ['tag-manager', 'stats', sessionId] })
    void queryClient.invalidateQueries({ queryKey: ['tag-manager', 'dataset', sessionId] })
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
  const cancelScanMutation = useMutation({
    mutationFn: (id: string) => tagManagerApi.cancelScan(id),
    // The backend settles the session (typically to `ready` with whatever the
    // scan had indexed so far); refetching both queries repaints that state.
    onSuccess: () => {
      notify('success', '已取消扫描')
      void queryClient.invalidateQueries({ queryKey: ['tag-manager', 'datasets'] })
      void queryClient.invalidateQueries({ queryKey: ['tag-manager', 'dataset', activeId] })
    },
    onError: (error) => fail(error, '取消扫描失败'),
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
      notify('success', formatBatchResultNotice(result))
      invalidateSessionData()
    },
    onError: (error) => fail(error, '批量操作失败'),
  })
  const undoMutation = useMutation({
    mutationFn: (id: string) => tagManagerApi.undo(id),
    onSuccess: (result) => {
      onSessionDataChanged()
      // `reverted` is the number of journal entries/change rows rolled back;
      // older backends may omit it, so the count is only shown when present.
      const reverted = typeof result.reverted === 'number' ? result.reverted : undefined
      notify('success', reverted != null ? `已撤销上一步（恢复 ${reverted} 张图片）` : '已撤销上一次操作')
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
    cancelScanMutation,
    deleteMutation,
    batchMutation,
    undoMutation,
    redoMutation,
  }
}
