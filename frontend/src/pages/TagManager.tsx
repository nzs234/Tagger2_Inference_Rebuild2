import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChartColumn, Images, ListChecks, LoaderCircle, Tags, X } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { BatchBar } from '../components/tagManager/BatchBar'
import { EditorDrawer } from '../components/tagManager/EditorDrawer'
import { FilterBar } from '../components/tagManager/FilterBar'
import { ImageGrid } from '../components/tagManager/ImageGrid'
import { SessionBar } from '../components/tagManager/SessionBar'
import { StatsPanel } from '../components/tagManager/StatsPanel'
import { Button, ConfirmDialog, DialogLayer, EmptyState, Notice, Panel } from '../components/ui'
import { api, ApiError } from '../lib/api'
import {
  emptyImageFilter,
  tagManagerApi,
  tagManagerThumbnailUrl,
  type ImageFilterState,
  type TagManagerBatchRequest,
  type TagManagerEditableContent,
  type TagManagerSession,
  type TagManagerSort,
} from '../lib/tagManager'

const PAGE_SIZE = 60

type PageNotice = { tone: 'info' | 'warning' | 'danger' | 'success'; text: string }

export function TagManager() {
  const queryClient = useQueryClient()
  const [activeId, setActiveId] = useState<string>()
  const [notice, setNotice] = useState<PageNotice | null>(null)
  const [filter, setFilterState] = useState<ImageFilterState>(emptyImageFilter)
  const [sort, setSort] = useState<TagManagerSort>('name')
  const [page, setPage] = useState(0)
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const selectionAnchor = useRef<number | null>(null)
  const [editingId, setEditingId] = useState<number>()
  const [saveConflict, setSaveConflict] = useState(false)
  const [statsOpen, setStatsOpen] = useState(true)
  const [confirmDelete, setConfirmDelete] = useState(false)

  const roots = useQuery({ queryKey: ['roots'], queryFn: api.roots, staleTime: 60_000, retry: false })
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

  useEffect(() => {
    if (!activeId && sessions.length > 0) setActiveId(sessions[0]?.id)
  }, [activeId, sessions])

  const previousStatus = useRef<string | undefined>(undefined)
  useEffect(() => {
    if (previousStatus.current === 'indexing' && session?.status === 'ready') {
      void queryClient.invalidateQueries({ queryKey: ['tag-manager'] })
    }
    previousStatus.current = session?.status
  }, [session?.status, queryClient])

  const imagesQuery = useQuery({
    queryKey: ['tag-manager', 'images', activeId, page, sort, filter],
    queryFn: () => tagManagerApi.images(activeId as string, { offset: page * PAGE_SIZE, limit: PAGE_SIZE, sort, filter }),
    enabled: Boolean(activeId) && sessionReady,
    placeholderData: (previous) => previous,
  })
  const images = useMemo(() => imagesQuery.data?.items ?? [], [imagesQuery.data])
  const total = imagesQuery.data?.total ?? 0
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  const detailQuery = useQuery({
    queryKey: ['tag-manager', 'image', activeId, editingId],
    queryFn: () => tagManagerApi.imageDetail(activeId as string, editingId as number),
    enabled: Boolean(activeId) && editingId != null,
  })

  const fail = (error: unknown, fallback: string) =>
    setNotice({ tone: 'danger', text: error instanceof ApiError ? error.message : fallback })

  const createMutation = useMutation({
    mutationFn: tagManagerApi.createDataset,
    onSuccess: (created) => {
      setActiveId(created.id)
      setNotice({ tone: 'info', text: `会话「${created.name}」已创建，正在索引图片…` })
      void queryClient.invalidateQueries({ queryKey: ['tag-manager', 'datasets'] })
    },
    onError: (error) => fail(error, '会话创建失败'),
  })
  const refreshMutation = useMutation({
    mutationFn: (id: string) => tagManagerApi.refreshDataset(id),
    onSuccess: (refreshed) => {
      setNotice({ tone: 'info', text: '正在重新扫描数据集…' })
      void queryClient.invalidateQueries({ queryKey: ['tag-manager', 'datasets'] })
      void queryClient.invalidateQueries({ queryKey: ['tag-manager', 'dataset', refreshed.id] })
    },
    onError: (error) => fail(error, '重新扫描失败'),
  })
  const deleteMutation = useMutation({
    mutationFn: (id: string) => tagManagerApi.deleteDataset(id),
    onSuccess: () => {
      setActiveId(undefined)
      setSelectedIds(new Set())
      setEditingId(undefined)
      setNotice({ tone: 'success', text: '会话已删除' })
      void queryClient.invalidateQueries({ queryKey: ['tag-manager', 'datasets'] })
    },
    onError: (error) => fail(error, '会话删除失败'),
  })
  const saveMutation = useMutation({
    mutationFn: ({ imageId, content, expectedSidecarMtime }: {
      imageId: number
      content: TagManagerEditableContent
      expectedSidecarMtime?: number | string
    }) => tagManagerApi.updateImage(activeId as string, imageId, {
      content,
      expected_sidecar_mtime: expectedSidecarMtime ?? undefined,
    }),
    onSuccess: () => {
      setSaveConflict(false)
      setNotice({ tone: 'success', text: '标签已保存' })
      void queryClient.invalidateQueries({ queryKey: ['tag-manager'] })
    },
    onError: (error) => {
      if (error instanceof ApiError && error.code === 'sidecar_conflict') {
        setSaveConflict(true)
        return
      }
      fail(error, '标签保存失败')
    },
  })
  const batchMutation = useMutation({
    mutationFn: (body: TagManagerBatchRequest) => tagManagerApi.batch(activeId as string, body),
    onSuccess: (result) => {
      setSelectedIds(new Set())
      setNotice({ tone: 'success', text: `批量操作完成，影响 ${result.affected} 张图片` })
      void queryClient.invalidateQueries({ queryKey: ['tag-manager'] })
    },
    onError: (error) => fail(error, '批量操作失败'),
  })
  const undoMutation = useMutation({
    mutationFn: (id: string) => tagManagerApi.undo(id),
    onSuccess: () => {
      setNotice({ tone: 'success', text: '已撤销上一次操作' })
      void queryClient.invalidateQueries({ queryKey: ['tag-manager'] })
    },
    onError: (error) => fail(error, '撤销失败'),
  })
  const redoMutation = useMutation({
    mutationFn: (id: string) => tagManagerApi.redo(id),
    onSuccess: () => {
      setNotice({ tone: 'success', text: '已重做操作' })
      void queryClient.invalidateQueries({ queryKey: ['tag-manager'] })
    },
    onError: (error) => fail(error, '重做失败'),
  })

  const actionsDisabled = !sessionReady
  const inputRoots = (roots.data?.items ?? []).filter((root) => root.kind === 'input')

  const setFilter = (next: ImageFilterState) => {
    setFilterState(next)
    setPage(0)
  }
  const changeSort = (next: TagManagerSort) => {
    setSort(next)
    setPage(0)
  }

  const toggleSelect = (imageId: number, index: number, modifiers: { shift: boolean; ctrl: boolean }) => {
    setSelectedIds((current) => {
      const next = new Set(current)
      const anchor = selectionAnchor.current
      if (modifiers.shift && anchor != null) {
        const from = Math.min(anchor, index)
        const to = Math.max(anchor, index)
        for (let candidate = from; candidate <= to; candidate += 1) {
          const item = images[candidate]
          if (item) next.add(item.id)
        }
      } else if (next.has(imageId)) {
        next.delete(imageId)
      } else {
        next.add(imageId)
      }
      return next
    })
    selectionAnchor.current = index
  }

  const selectAllPage = () => {
    setSelectedIds((current) => {
      const next = new Set(current)
      images.forEach((image) => next.add(image.id))
      return next
    })
  }

  const addToIncludeFilter = (tag: string) => {
    if (filter.includeTags.includes(tag)) return
    setFilter({ ...filter, includeTags: [...filter.includeTags, tag] })
  }

  const selectedIdList = useMemo(() => [...selectedIds], [selectedIds])
  const closeEditor = () => {
    setEditingId(undefined)
    setSaveConflict(false)
  }

  return <div className="page page-tag-manager">
    <div className="page-heading">
      <div>
        <p className="eyebrow">TAG MANAGER</p>
        <h1>标签管理</h1>
        <p className="page-subtitle">以数据集为单位浏览图片、编辑 sidecar 标签，并批量增删改标签，全部改动可撤销。</p>
      </div>
      <div className="heading-stats">
        <span><strong>{session?.image_count ?? total}</strong> 图片</span>
        <span className="heading-divider" />
        <span><strong>{selectedIds.size}</strong> 已选</span>
      </div>
    </div>

    {notice && <Notice tone={notice.tone}>
      {notice.text}
      <button type="button" className="icon-button icon-button-quiet" aria-label="关闭提示" onClick={() => setNotice(null)}><X size={15} /></button>
    </Notice>}

    <SessionBar
      sessions={sessions}
      activeSession={session}
      inputRoots={inputRoots}
      active={activeId}
      creating={createMutation.isPending}
      refreshing={refreshMutation.isPending}
      deleting={deleteMutation.isPending}
      undoPending={undoMutation.isPending}
      redoPending={redoMutation.isPending}
      actionsDisabled={actionsDisabled}
      onSelect={(id) => {
        setActiveId(id)
        setSelectedIds(new Set())
        setPage(0)
        setEditingId(undefined)
        setSaveConflict(false)
      }}
      onCreate={(body) => createMutation.mutate(body)}
      onRefresh={() => session && refreshMutation.mutate(session.id)}
      onDelete={() => setConfirmDelete(true)}
      onUndo={() => session && undoMutation.mutate(session.id)}
      onRedo={() => session && redoMutation.mutate(session.id)}
    />

    <div className="tm-layout">
      <div className="tm-main">
        <Panel title="筛选与排序" eyebrow="FILTER">
          <FilterBar filter={filter} sort={sort} disabled={!sessionReady} onChange={setFilter} onSortChange={changeSort} />
        </Panel>
        <Panel
          title="图片"
          eyebrow="IMAGES"
          actions={<>
            <Button size="sm" variant="quiet" icon={<ListChecks size={14} />} disabled={images.length === 0} onClick={selectAllPage}>全选本页</Button>
            <span className="panel-count">{images.length > 0 ? `${images.length} / ${total}` : '0'}</span>
          </>}
        >
          <ImageGrid
            images={images}
            thumbnailUrl={(image) => tagManagerThumbnailUrl(activeId ?? '', image.id)}
            selectedIds={selectedIds}
            editingId={editingId}
            onToggleSelect={(image, index, modifiers) => toggleSelect(image.id, index, modifiers)}
            onOpen={(image) => {
              setEditingId(image.id)
              setSaveConflict(false)
            }}
            empty={session && !sessionReady
              ? <div className="tm-grid-loading"><LoaderCircle className="spin" size={18} aria-hidden="true" /><span>{session.status === 'indexing' ? '正在索引图片，请稍候…' : session.error || '会话不可用'}</span></div>
              : <EmptyState icon={<Images size={22} />} title="没有匹配的图片" detail="调整筛选条件，或先创建并打开一个会话。" />}
          />
          <div className="tm-pagination">
            <Button size="sm" variant="secondary" disabled={page === 0} onClick={() => setPage(page - 1)}>上一页</Button>
            <span className="muted">第 {page + 1} / {totalPages} 页 · 共 {total} 张</span>
            <Button size="sm" variant="secondary" disabled={page + 1 >= totalPages} onClick={() => setPage(page + 1)}>下一页</Button>
          </div>
        </Panel>
      </div>

      <div className="tm-side">
        {selectedIds.size > 0 && <BatchBar
          profile={session?.profile ?? 'e621'}
          filter={filter}
          selectedIds={selectedIdList}
          filteredTotal={total}
          submitting={batchMutation.isPending}
          disabled={!sessionReady}
          onSubmit={(body) => batchMutation.mutate(body)}
        />}
        <Panel
          title="高频标签"
          eyebrow="STATS"
          actions={<Button size="sm" variant="quiet" icon={statsOpen ? <X size={14} /> : <ChartColumn size={14} />} aria-expanded={statsOpen} onClick={() => setStatsOpen(!statsOpen)}>{statsOpen ? '收起' : '展开'}</Button>}
        >
          {sessionReady && activeId
            ? <StatsPanel sessionId={activeId} enabled={statsOpen} onTagClick={addToIncludeFilter} />
            : <EmptyState icon={<Tags size={20} />} title="等待会话就绪" detail="会话索引完成后可查看标签统计。" />}
        </Panel>
      </div>
    </div>

    {editingId != null && activeId && (detailQuery.data
      ? <EditorDrawer
          key={`${editingId}:${detailQuery.data.sidecar_mtime ?? 'none'}`}
          detail={detailQuery.data}
          profile={session?.profile ?? 'e621'}
          saving={saveMutation.isPending}
          conflict={saveConflict}
          onClose={closeEditor}
          onSave={(content) => saveMutation.mutate({
            imageId: editingId,
            content,
            expectedSidecarMtime: detailQuery.data.sidecar_mtime ?? undefined,
          })}
          onReload={() => {
            setSaveConflict(false)
            void queryClient.invalidateQueries({ queryKey: ['tag-manager', 'image', activeId, editingId] })
          }}
        />
      : <DialogLayer onClose={closeEditor}>
          <div className="tm-drawer drawer" role="dialog" aria-modal="true" aria-label="正在加载图片">
            <div className="tm-drawer-loading"><LoaderCircle className="spin" size={20} aria-hidden="true" /><span>正在加载图片内容…</span></div>
          </div>
        </DialogLayer>)}

    {confirmDelete && session && <ConfirmDialog
      title={`删除会话「${session.name}」？`}
      detail={<span>将删除会话索引（包含 {session.image_count} 张图片的记录）。磁盘上的图片与 sidecar 文件不会被删除。</span>}
      confirmLabel="删除会话"
      busy={deleteMutation.isPending}
      onConfirm={() => deleteMutation.mutate(session.id)}
      onClose={() => setConfirmDelete(false)}
    />}
  </div>
}
