import { useQuery } from '@tanstack/react-query'
import { ChartColumn, Images, ListChecks, LoaderCircle, Tags, X, XCircle } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { BatchBar } from '../components/tagManager/BatchBar'
import { EditorDrawer } from '../components/tagManager/EditorDrawer'
import { FilterBar } from '../components/tagManager/FilterBar'
import { ImageGrid } from '../components/tagManager/ImageGrid'
import { SessionBar } from '../components/tagManager/SessionBar'
import { StatsPanel } from '../components/tagManager/StatsPanel'
import { TagDisplayBar } from '../components/tagManager/TagDisplayBar'
import { useImageSelection } from '../components/tagManager/useImageSelection'
import { useNoticeQueue } from '../components/tagManager/useNoticeQueue'
import { useTagEditor } from '../components/tagManager/useTagEditor'
import { useTagManagerImages } from '../components/tagManager/useTagManagerImages'
import { useTagManagerSessions } from '../components/tagManager/useTagManagerSessions'
import { Button, ConfirmDialog, DialogLayer, EmptyState, Notice, Panel } from '../components/ui'
import { api } from '../lib/api'
import { describeTagManagerError, tagManagerErrorTone } from '../lib/tagManagerErrors'
import {
  tagManagerApi,
  type ImageFilterState,
  type TagManagerImageSummary,
  type TagManagerSort,
} from '../lib/tagManager'
import { useTagManagerView } from '../store/tagManagerView'

export function TagManager() {
  // View state (stats panel) lives in a persisted store so a reload restores
  // the working context; see store/tagManagerView.ts.  Filter/sort/page are
  // owned by useTagManagerImages, the active session by useTagManagerSessions.
  const statsOpen = useTagManagerView((state) => state.statsOpen)
  const setStatsOpen = useTagManagerView((state) => state.setStatsOpen)
  const [confirmDelete, setConfirmDelete] = useState(false)

  // Page notices: a queue rendered as a vertical stack (info/success auto-
  // dismiss, warning/danger stay).  Known backend error codes map to Chinese
  // copy; see lib/tagManagerErrors.ts.
  const { notices, push: pushNotice, dismiss: dismissNotice } = useNoticeQueue()
  const fail = useCallback((error: unknown, fallback: string) => {
    // Empty-history undo/redo is a benign no-op, so it renders as a warning
    // instead of the danger tone used for real failures.
    pushNotice(tagManagerErrorTone(error), describeTagManagerError(error, fallback))
  }, [pushNotice])

  // Session layer.  The cleanup callbacks only run on mutation success — long
  // after render — so referring to `selection`/`editor`, which are initialised
  // further down, is safe here.
  const {
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
  } = useTagManagerSessions({
    notify: pushNotice,
    fail,
    onSessionRemoved: () => {
      selection.clear()
      editor.closeEditor()
    },
    onSessionDataChanged: () => selection.clear(),
  })

  const {
    images,
    pageImages,
    total,
    totalPages,
    page,
    setPage,
    filter,
    sort,
    setViewFilter,
    setSort,
    missingTags,
    imagesReady,
    imagesError,
    retryImages,
  } = useTagManagerImages({ activeId, sessionReady })
  const selection = useImageSelection(images)
  const { selectedIds, selectedIdList, toggle, selectAll, clear } = selection

  const editor = useTagEditor({ activeId, images: pageImages, imagesReady, page, totalPages, setPage, notify: pushNotice, fail })
  const {
    editingId,
    editingIndex,
    saveConflict,
    syncToken,
    saveRevision,
    saveErrorToken,
    detail,
    detailError,
    retryDetail,
    saving,
    openImage,
    closeEditor,
    cancelPendingNavigate,
    confirmLeaveIfDirty,
    registerLeaveGuard,
    navigate,
    save,
    reload,
  } = editor

  // Any change of the active session — manual switch, create, the automatic
  // fallback after a deletion or a stale restored id — starts a new working
  // context.  Grid selection, the editor and the page are session-scoped
  // state and must never leak into the next session.  The user-facing switches
  // (select/create/delete) run through `confirmLeaveIfDirty`, which owns the
  // discard/saving confirmation and closes the editor before the switch, so
  // this effect is a no-op for them.  It stays as the safety net for a session
  // change we did not originate (e.g. a stale persisted id falling back).
  const lastActiveIdRef = useRef(activeId)
  useEffect(() => {
    if (lastActiveIdRef.current === activeId) return
    lastActiveIdRef.current = activeId
    selection.clear()
    editor.closeEditor()
    setPage(0)
  }, [activeId, selection, editor, setPage])

  const roots = useQuery({ queryKey: ['roots'], queryFn: api.roots, staleTime: 60_000, retry: false })

  const actionsDisabled = !sessionReady
  // Editing writes sidecars in place, so only writable roots can host a dataset.
  const writableRoots = (roots.data?.items ?? []).filter((root) => root.writable)

  // Filter/sort changes reset the page and cancel a pending cross-page
  // navigation: the pending target belongs to the page being left.
  const setFilter = (next: ImageFilterState) => {
    cancelPendingNavigate()
    setViewFilter(next)
    setPage(0)
  }
  const changeSort = (next: TagManagerSort) => {
    cancelPendingNavigate()
    setSort(next)
    setPage(0)
  }

  const toggleSelect = (image: TagManagerImageSummary, _index: number, modifiers: { shift: boolean; ctrl: boolean }) => {
    toggle(image, modifiers)
  }

  const addToIncludeFilter = (tag: string) => {
    if (filter.includeTags.includes(tag)) return
    setFilter({ ...filter, includeTags: [...filter.includeTags, tag] })
  }
  const addToExcludeFilter = (tag: string) => {
    if (filter.excludeTags.includes(tag)) return
    setFilter({ ...filter, excludeTags: [...filter.excludeTags, tag] })
  }

  // Stable per-session loader: `useCallback` keyed on the session id keeps the
  // grid's thumbnail effects from rerunning on unrelated parent renders.
  const loadThumbnail = useCallback(
    (imageId: number) => tagManagerApi.thumbnailBlob(activeId as string, imageId),
    [activeId],
  )

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
        {/* Selection persists across pages and filter changes by design, so an
            explicit clear action is the only way to drop it deliberately. */}
        <Button size="sm" variant="quiet" icon={<XCircle size={13} />} disabled={selectedIds.size === 0} onClick={clear}>清除选择</Button>
      </div>
    </div>

    {notices.length > 0 && <div className="tm-notices">
      {notices.map((entry) => <Notice key={entry.id} tone={entry.tone}>
        {entry.text}
        <button type="button" className="icon-button icon-button-quiet" aria-label="关闭提示" onClick={() => dismissNotice(entry.id)}><X size={15} /></button>
      </Notice>)}
    </div>}

    <SessionBar
      sessions={sessions}
      activeSession={session}
      writableRoots={writableRoots}
      active={activeId}
      creating={createMutation.isPending}
      refreshing={refreshMutation.isPending}
      deleting={deleteMutation.isPending}
      undoPending={undoMutation.isPending}
      redoPending={redoMutation.isPending}
      actionsDisabled={actionsDisabled}
      canUndo={Boolean(session?.can_undo)}
      canRedo={Boolean(session?.can_redo)}
      onSelect={(id) => {
        // Switching sessions discards the current draft; the editor confirms
        // first when it is dirty or a save is in flight.
        confirmLeaveIfDirty(() => {
          selectSession(id)
          clear()
          setPage(0)
          closeEditor()
        })
      }}
      onCreate={(body) => confirmLeaveIfDirty(() => {
        closeEditor()
        createMutation.mutate(body)
      })}
      onRefresh={() => session && refreshMutation.mutate(session.id)}
      onDelete={() => confirmLeaveIfDirty(() => {
        closeEditor()
        setConfirmDelete(true)
      })}
      onUndo={() => session && undoMutation.mutate(session.id)}
      onRedo={() => session && redoMutation.mutate(session.id)}
    />

    <div className="tm-layout">
      <div className="tm-main">
        <Panel title="筛选与排序" eyebrow="FILTER">
          <FilterBar
            filter={filter}
            sort={sort}
            profile={session?.profile ?? 'e621'}
            disabled={!sessionReady}
            onChange={setFilter}
            onSortChange={changeSort}
          />
          <TagDisplayBar profile={session?.profile ?? 'e621'} missingTags={missingTags} />
        </Panel>
        <Panel
          title="图片"
          eyebrow="IMAGES"
          actions={<>
            <Button size="sm" variant="quiet" icon={<ListChecks size={14} />} disabled={images.length === 0} onClick={selectAll}>全选本页</Button>
            <span className="panel-count">{images.length > 0 ? `${images.length} / ${total}` : '0'}</span>
          </>}
        >
          <ImageGrid
            images={images}
            loadThumbnail={loadThumbnail}
            selectedIds={selectedIds}
            editingId={editingId}
            // Different result set (session/page/filter/sort) starts at the top.
            resetKey={`${activeId}:${page}:${JSON.stringify(filter)}:${sort}`}
            onToggleSelect={toggleSelect}
            onOpen={(image) => openImage(image.id)}
            empty={imagesError
              ? <EmptyState icon={<Images size={22} />} title="图片列表加载失败" detail="请重试，或检查当前会话。" action={<Button variant="secondary" onClick={retryImages}>重试</Button>} />
              : session && !sessionReady
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
        {/* Always mounted once the session is ready: the whole point of the
            filter scope is running a batch without selecting anything first. */}
        {sessionReady && activeId && <BatchBar
          sessionId={activeId}
          profile={session?.profile ?? 'e621'}
          filter={filter}
          selectedIds={selectedIdList}
          filteredTotal={total}
          submitting={batchMutation.isPending}
          disabled={!sessionReady}
          notify={pushNotice}
          onSubmit={(body) => batchMutation.mutate(body)}
        />}
        <Panel
          title="高频标签"
          eyebrow="STATS"
          actions={<Button size="sm" variant="quiet" icon={statsOpen ? <X size={14} /> : <ChartColumn size={14} />} aria-expanded={statsOpen} onClick={() => setStatsOpen(!statsOpen)}>{statsOpen ? '收起' : '展开'}</Button>}
        >
          {sessionReady && activeId
            ? <StatsPanel
                sessionId={activeId}
                enabled={statsOpen}
                onTagClick={addToIncludeFilter}
                onTagExclude={addToExcludeFilter}
              />
            : <EmptyState icon={<Tags size={20} />} title="等待会话就绪" detail="会话索引完成后可查看标签统计。" />}
        </Panel>
      </div>
    </div>

    {editingId != null && activeId && (detail
      ? <EditorDrawer
          // Deliberately NOT keyed by sidecar_mtime: remounting after each
          // save reset the draft, scroll and focus mid-review. The draft syncs
          // from the detail query only when the image id changes.
          key={`image:${editingId}`}
          detail={detail}
          profile={session?.profile ?? 'e621'}
          saving={saving}
          conflict={saveConflict}
          // Page edges chain into the adjacent page (pendingNavigate): the
          // first image of a page has a "previous" when page > 0, the last one
          // has a "next" when further pages exist.  When the editing id is
          // temporarily absent from the page (filter change, cross-page load)
          // both directions stay disabled until the state settles.
          hasPrev={editingIndex > 0 || (editingIndex === 0 && page > 0)}
          hasNext={editingIndex >= 0 && (editingIndex < pageImages.length - 1 || page < totalPages - 1)}
          onClose={closeEditor}
          onNavigate={navigate}
          onSave={save}
          onReload={reload}
          syncToken={syncToken}
          saveRevision={saveRevision}
          saveErrorToken={saveErrorToken}
          registerLeaveGuard={registerLeaveGuard}
        />
      : <DialogLayer onClose={closeEditor}>
          <div className="tm-drawer drawer" role="dialog" aria-modal="true" aria-label={detailError ? '图片内容加载失败' : '正在加载图片'}>
            {detailError
              ? <EmptyState title="图片内容加载失败" detail="请重试或关闭编辑器。" action={<><Button variant="secondary" onClick={retryDetail}>重试</Button><Button variant="quiet" onClick={closeEditor}>关闭</Button></>} />
              : <div className="tm-drawer-loading"><LoaderCircle className="spin" size={20} aria-hidden="true" /><span>正在加载图片内容…</span></div>}
          </div>
        </DialogLayer>)}

    {confirmDelete && session && <ConfirmDialog
      title={`删除会话「${session.name || session.relative_path || '数据集'}」？`}
      detail={<span>将删除会话索引（包含 {session.image_count} 张图片的记录）。磁盘上的图片与 sidecar 文件不会被删除。</span>}
      confirmLabel="删除会话"
      busy={deleteMutation.isPending}
      onConfirm={() => deleteMutation.mutate(session.id)}
      onClose={() => setConfirmDelete(false)}
    />}
  </div>
}
