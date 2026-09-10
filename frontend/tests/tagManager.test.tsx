import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { TagManager } from '../src/pages/TagManager'
import type { TagManagerImageDetail, TagManagerImageSummary, TagManagerSession } from '../src/lib/tagManager'

const session: TagManagerSession = {
  id: 'ds-1',
  name: 'cats',
  root_id: 'in',
  relative_path: 'cats',
  profile: 'e621',
  recursive: true,
  status: 'ready',
  error: null,
  image_count: 3,
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-01T00:00:00Z',
}

function summary(id: number, fileName: string, sidecarKind: TagManagerImageSummary['sidecar_kind'], tagCount: number): TagManagerImageSummary {
  return {
    id,
    relative_path: fileName,
    file_name: fileName,
    image_format: 'png',
    sidecar_kind: sidecarKind,
    mtime: 1_000 + id,
    width: 64,
    height: 64,
    tag_count: tagCount,
    tags: [],
  }
}

const imageItems: TagManagerImageSummary[] = [
  summary(1, 'a.png', 'tag_txt', 2),
  summary(2, 'b.png', 'none', 0),
  summary(3, 'c.png', 'tags_json', 4),
]

const detail: TagManagerImageDetail = {
  ...imageItems[0] as TagManagerImageSummary,
  tags: [{ tag: 'solo', category: 'general' }],
  content: { kind: 'tag_txt', tags: ['solo', 'long_hair'] },
  sidecar_mtime: 1_725_148_800,
}

interface HarnessState {
  sessions: TagManagerSession[]
  createBodies: Array<Record<string, unknown>>
  patchBodies: Array<Record<string, unknown>>
  batchBodies: Array<Record<string, unknown>>
  undoCalls: number
  redoCalls: number
  deleteCalls: number
  tagDbQueries: string[]
  imageQueries: string[]
  detail: TagManagerImageDetail
  patchConflict: boolean
  /** When set, every PATCH fails with this envelope instead of succeeding. */
  patchError: { code: string; message: string } | null
  /** When set, PATCH responses hold until `release` is called (edit-during-save). */
  patchGate: { release: () => void; hold: Promise<void> } | null
  /** When set, the images list request fails. */
  imagesError: boolean
  /** When set, image detail GETs fail. */
  detailError: boolean
  /** Current sidecar mtime served by GET detail; a successful PATCH rewrites it. */
  sidecarMtime: number
  thumbnailCalls: number[]
  /** When set, the next undo request fails with this error envelope. */
  undoError: { code: string; message: string } | null
  /** When set, the images endpoint slices by offset: page one = `first`,
   * page two = `second`; `total` drives the page count (PAGE_SIZE is 60). */
  pagedPages: { first: TagManagerImageSummary[]; second: TagManagerImageSummary[]; total: number } | null
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <TagManager />
    </QueryClientProvider>,
  )
}

/** Manual release gate for holding a mutation response open mid-test. */
function deferred(): { release: () => void; hold: Promise<void> } {
  let release: () => void = () => {}
  const hold = new Promise<void>((resolve) => {
    release = resolve
  })
  return { release, hold }
}

/** Text of the heading stats ("<n> 图片 · <n> 已选"); the numbers sit inside
 * <strong> children, so text-level queries cannot match them. */
function headingStatsText(): string {
  return (document.querySelector('.heading-stats') as HTMLElement | null)?.textContent ?? ''
}

function setupFetch(state: HarnessState) {
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

  const patchResult = async (imageId: number) => {
    if (state.patchError) return json({ code: state.patchError.code, message: state.patchError.message }, 500)
    if (state.patchConflict) return json({ code: 'sidecar_conflict', message: 'sidecar 在编辑期间被外部修改' }, 409)
    // A real backend rewrites the sidecar and reports the new mtime; the next
    // consecutive save must send exactly this value.
    state.sidecarMtime += 5
    return json({ image_id: imageId, journal_id: `j-${imageId}`, sidecar_kind: 'tag_txt', sidecar_mtime: state.sidecarMtime })
  }

  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost')
    const method = init?.method ?? 'GET'
    const path = url.pathname
    if (path.endsWith('/health')) return json({ status: 'ok' })
    if (path.endsWith('/roots')) return json({ items: [{ id: 'in', name: '训练图片', kind: 'input', writable: true }] })
    if (path.endsWith('/tag-manager/tag-db') || path.endsWith('/tag-db')) {
      state.tagDbQueries.push(url.searchParams.get('query') ?? '')
      const query = (url.searchParams.get('query') ?? '').toLowerCase()
      const matches = [
        { name: '1girl', category: 'general', post_count: 4_000_000, alias_of: null },
        { name: 'long_hair', category: 'general', post_count: 1_200_000, alias_of: null },
        { name: 'hakurei_reimu', category: 'character', post_count: 90_000, alias_of: null },
      ].filter((entry) => entry.name.includes(query))
      return json({ profile: 'e621', items: matches })
    }
    if (path === '/api/v1/tag-manager/datasets' || path === '/tag-manager/datasets') {
      if (method === 'POST') {
        const body = JSON.parse(init?.body as string) as Record<string, unknown>
        state.createBodies.push(body)
        const created: TagManagerSession = {
          ...session,
          id: 'ds-2',
          name: String(body.name ?? 'cats_v2'),
          relative_path: String(body.relative_path ?? 'cats_v2'),
          status: 'indexing',
          image_count: 0,
        }
        state.sessions = [...state.sessions, created]
        return json(created, 202)
      }
      return json({ items: state.sessions })
    }
    if (/\/tag-manager\/datasets\/ds-1\/refresh$/.test(path)) return json({ ...session, status: 'indexing' }, 202)
    if (/\/tag-manager\/datasets\/ds-1\/batch$/.test(path)) {
      state.batchBodies.push(JSON.parse(init?.body as string) as Record<string, unknown>)
      return json({ affected: 2, journal_id: 'j-batch' })
    }
    if (/\/tag-manager\/datasets\/ds-1\/undo$/.test(path)) {
      if (state.undoError) return json(state.undoError, 409)
      state.undoCalls += 1
      return json({ journal_id: 'j-undo' })
    }
    if (/\/tag-manager\/datasets\/ds-1\/redo$/.test(path)) {
      state.redoCalls += 1
      return json({ journal_id: 'j-redo' })
    }
    if (/\/tag-manager\/datasets\/ds-1\/tags\/stats$/.test(path)) {
      return json({ items: [{ tag: 'solo', category: 'general', count: 3 }] })
    }
    const datasetMatch = /\/tag-manager\/datasets\/([^/]+)$/.exec(path)
    if (datasetMatch) {
      if (method === 'DELETE') {
        state.deleteCalls += 1
        state.sessions = state.sessions.filter((item) => item.id !== datasetMatch[1])
        return json({ ok: true })
      }
      const found = state.sessions.find((item) => item.id === datasetMatch[1])
      return json(found ?? session)
    }
    const thumbnailMatch = /\/tag-manager\/datasets\/[^/]+\/images\/(\d+)\/thumbnail$/.exec(path)
    if (thumbnailMatch) {
      state.thumbnailCalls.push(Number(thumbnailMatch[1]))
      return json({})
    }
    const detailMatch = /\/tag-manager\/datasets\/[^/]+\/images\/(\d+)$/.exec(path)
    if (detailMatch) {
      const imageId = Number(detailMatch[1])
      if (method === 'PATCH') {
        state.patchBodies.push(JSON.parse(init?.body as string) as Record<string, unknown>)
        if (state.patchGate) {
          const gate = state.patchGate
          await gate.hold
          return patchResult(imageId)
        }
        return patchResult(imageId)
      }
      if (state.detailError) return json({ code: 'image_not_found', message: 'image missing' }, 500)
      // Every image shares the tag_txt detail shape; the drawer is keyed by
      // image id so navigating refetches the detail under a new key.
      const knownSummaries = [
        ...imageItems,
        ...(state.pagedPages ? [...state.pagedPages.first, ...state.pagedPages.second] : []),
      ]
      const summary = knownSummaries.find((item) => item.id === imageId) ?? imageItems[0]
      return json({ ...state.detail, ...summary, tags: [{ tag: 'solo', category: 'general' }], sidecar_mtime: state.sidecarMtime })
    }
    if (/\/tag-manager\/datasets\/[^/]+\/images$/.test(path)) {
      state.imageQueries.push(url.search)
      if (state.imagesError) return json({ code: 'request_failed', message: 'list failed' }, 500)
      if (state.pagedPages) {
        const offset = Number(url.searchParams.get('offset') ?? '0')
        const items = offset === 0 ? state.pagedPages.first : state.pagedPages.second
        return json({ items, total: state.pagedPages.total })
      }
      return json({ items: imageItems, total: imageItems.length })
    }
    return json({})
  })
}

describe('TagManager page', () => {
  let state: HarnessState

  beforeEach(() => {
    state = {
      sessions: [session],
      createBodies: [],
      patchBodies: [],
      batchBodies: [],
      undoCalls: 0,
      redoCalls: 0,
      deleteCalls: 0,
      tagDbQueries: [],
      imageQueries: [],
      detail: { ...detail },
      patchConflict: false,
      patchError: null,
      patchGate: null,
      imagesError: false,
      detailError: false,
      sidecarMtime: 1_725_148_800,
      thumbnailCalls: [],
      undoError: null,
      pagedPages: null,
    }
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('renders the page heading and the image grid for a ready session', async () => {
    setupFetch(state)
    renderPage()

    expect(screen.getByRole('heading', { level: 1, name: '标签管理' })).toBeInTheDocument()
    expect(await screen.findByAltText('a.png')).toBeInTheDocument()
    expect(screen.getByAltText('b.png')).toBeInTheDocument()
    expect(screen.getByAltText('c.png')).toBeInTheDocument()
    expect(screen.getByText('TXT')).toBeInTheDocument()
    expect(document.querySelector('.tm-badge-missing')).toHaveTextContent('无 sidecar')
  })

  it('creates a session from the form and selects it', async () => {
    state.sessions = []
    setupFetch(state)
    renderPage()

    const pathInput = await screen.findByLabelText('相对路径')
    fireEvent.change(pathInput, { target: { value: 'cats_v2' } })
    const openButton = screen.getByRole('button', { name: '打开' })
    await waitFor(() => expect(openButton).toBeEnabled())
    fireEvent.click(openButton)

    await waitFor(() => expect(state.createBodies).toHaveLength(1))
    expect(state.createBodies[0]).toMatchObject({
      root_id: 'in',
      relative_path: 'cats_v2',
      profile: 'e621',
      recursive: true,
      name: 'cats_v2',
    })
    // The created session gets its own id (ds-2) and becomes the active one.
    await waitFor(() => {
      expect(screen.getByRole('combobox', { name: '现有会话' })).toHaveValue('ds-2')
    })
  })

  it('selects images through checkboxes and 全选本页', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    fireEvent.click(screen.getByRole('checkbox', { name: '选择 a.png' }))
    expect(screen.getByText('选中图片（1）')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('checkbox', { name: '选择 b.png' }))
    expect(screen.getByText('选中图片（2）')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '全选本页' }))
    expect(screen.getByText('选中图片（3）')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: '选择 c.png' })).toBeChecked()
  })

  it('edits tag_txt content: removes a pill, adds one via autocomplete, and saves', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    fireEvent.dblClick(screen.getByTitle('a.png'))
    const dialog = await screen.findByRole('dialog', { name: 'a.png' })
    expect(screen.getByRole('button', { name: '移除 solo' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '移除 long_hair' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '移除 solo' }))
    expect(screen.queryByRole('button', { name: '移除 solo' })).not.toBeInTheDocument()

    const addInput = screen.getByRole('combobox', { name: '添加标签' })
    fireEvent.change(addInput, { target: { value: 'hakurei' } })
    const suggestion = await screen.findByRole('option', { name: /hakurei_reimu/ })
    expect(state.tagDbQueries.at(-1)).toBe('hakurei')
    // The suggestion carries the category colour from the tag database.
    expect(suggestion.querySelector('.tm-cat-character')).not.toBeNull()
    fireEvent.mouseDown(within(suggestion).getByRole('button'))

    await waitFor(() => expect(screen.getByRole('button', { name: '移除 hakurei_reimu' })).toBeInTheDocument())

    fireEvent.click(within(dialog).getByRole('button', { name: '保存' }))
    await waitFor(() => expect(state.patchBodies).toHaveLength(1))
    expect(state.patchBodies[0]).toEqual({
      content: { kind: 'tag_txt', tags: ['long_hair', 'hakurei_reimu'] },
      expected_sidecar_mtime: 1_725_148_800,
    })
  })

  it('submits a replace batch for the selected images after confirmation', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    fireEvent.click(screen.getByRole('checkbox', { name: '选择 a.png' }))
    fireEvent.click(screen.getByRole('checkbox', { name: '选择 b.png' }))

    fireEvent.change(screen.getByRole('combobox', { name: '批量操作类型' }), { target: { value: 'replace' } })
    const tagInput = screen.getByRole('combobox', { name: '批量标签' })
    fireEvent.change(tagInput, { target: { value: '1g' } })
    await screen.findByRole('option', { name: /1girl/ })
    fireEvent.keyDown(tagInput, { key: 'Enter' })
    expect(screen.getByRole('button', { name: '移除 1girl' })).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('替换为'), { target: { value: 'dog' } })
    fireEvent.click(screen.getByLabelText('使用正则表达式'))

    fireEvent.click(screen.getByRole('button', { name: '执行' }))
    const confirmation = screen.getByRole('alertdialog', { name: '对 2 张图片执行「替换」？' })
    expect(confirmation.textContent).toContain('选中的 2 张图片')
    fireEvent.click(screen.getByRole('button', { name: '确认执行' }))

    await waitFor(() => expect(state.batchBodies).toHaveLength(1))
    expect(state.batchBodies[0]).toEqual({
      op: 'replace',
      tags: ['1girl'],
      replacement: 'dog',
      use_regex: true,
      image_ids: [1, 2],
    })
  })

  it('calls undo and redo endpoints from the session bar', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    fireEvent.click(screen.getByRole('button', { name: '撤销' }))
    await waitFor(() => expect(state.undoCalls).toBe(1))
    fireEvent.click(screen.getByRole('button', { name: '重做' }))
    await waitFor(() => expect(state.redoCalls).toBe(1))
  })

  it('disables session actions while the session is still indexing', async () => {
    state.sessions = [{ ...session, status: 'indexing' }]
    setupFetch(state)
    renderPage()
    await screen.findByText('正在索引图片，请稍候…')

    expect(screen.getByRole('button', { name: '撤销' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '重做' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '刷新' })).toBeDisabled()
  })

  it('adds a high-frequency tag to the include filter from the stats panel', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    fireEvent.click(screen.getByTitle('筛选包含 solo'))
    await waitFor(() => expect(screen.getByRole('button', { name: '移除筛选 solo' })).toBeInTheDocument())
    await waitFor(() => expect(state.imageQueries.at(-1)).toContain('include_tags=solo'))
  })

  it('adds a filter chip from the include autocomplete and queries include_tags', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    const includeInput = screen.getByLabelText('包含标签')
    fireEvent.change(includeInput, { target: { value: 'long' } })
    const suggestion = await screen.findByRole('option', { name: /long_hair/ })
    expect(state.tagDbQueries.at(-1)).toBe('long')
    fireEvent.mouseDown(within(suggestion).getByRole('button'))

    await waitFor(() => expect(screen.getByRole('button', { name: '移除筛选 long_hair' })).toBeInTheDocument())
    await waitFor(() => expect(state.imageQueries.at(-1)).toContain('include_tags=long_hair'))
  })

  it('removes a filter chip and drops include_tags from the query', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    const includeInput = screen.getByLabelText('包含标签')
    fireEvent.change(includeInput, { target: { value: 'long' } })
    const suggestion = await screen.findByRole('option', { name: /long_hair/ })
    fireEvent.mouseDown(within(suggestion).getByRole('button'))
    await waitFor(() => expect(state.imageQueries.at(-1)).toContain('include_tags=long_hair'))

    fireEvent.click(screen.getByRole('button', { name: '移除筛选 long_hair' }))
    expect(screen.queryByRole('button', { name: '移除筛选 long_hair' })).not.toBeInTheDocument()
    await waitFor(() => expect(state.imageQueries.at(-1)).not.toContain('include_tags'))
  })

  it('excludes a tag from the stats panel into the exclude filter', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    fireEvent.click(screen.getByRole('button', { name: '排除 solo' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '移除排除 solo' })).toBeInTheDocument())
    await waitFor(() => expect(state.imageQueries.at(-1)).toContain('exclude_tags=solo'))

    // The row click still includes the tag, so both chips coexist.
    fireEvent.click(screen.getByTitle('筛选包含 solo'))
    await waitFor(() => expect(screen.getByRole('button', { name: '移除筛选 solo' })).toBeInTheDocument())
    await waitFor(() => {
      expect(state.imageQueries.at(-1)).toContain('include_tags=solo')
      expect(state.imageQueries.at(-1)).toContain('exclude_tags=solo')
    })
  })

  it('restores filter and sort from the persisted view after remount', async () => {
    setupFetch(state)
    const first = renderPage()
    await screen.findByAltText('a.png')

    fireEvent.click(screen.getByTitle('筛选包含 solo'))
    fireEvent.change(screen.getByLabelText('排序'), { target: { value: 'mtime' } })
    await waitFor(() => {
      expect(state.imageQueries.at(-1)).toContain('include_tags=solo')
      expect(state.imageQueries.at(-1)).toContain('sort=mtime')
    })

    // The persist middleware has already mirrored the view into storage.
    const stored = JSON.parse(window.localStorage.getItem('tagger2-tm-view') ?? '{}') as {
      state?: { filter?: { includeTags?: string[] }; sort?: string }
    }
    expect(stored.state?.filter?.includeTags).toContain('solo')
    expect(stored.state?.sort).toBe('mtime')

    first.unmount()
    cleanup()
    // The fetch spy from the first mount is still active; just remount.
    renderPage()
    await screen.findByAltText('a.png')

    expect(screen.getByRole('button', { name: '移除筛选 solo' })).toBeInTheDocument()
    expect(screen.getByLabelText('排序')).toHaveValue('mtime')
  })

  it('warns before closing the editor with unsaved changes', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    // Clean draft: closing goes straight through, no confirm dialog.
    fireEvent.dblClick(screen.getByTitle('a.png'))
    let dialog = await screen.findByRole('dialog', { name: 'a.png' })
    let footer = dialog.querySelector('.tm-drawer-footer') as HTMLElement
    fireEvent.click(within(footer).getByRole('button', { name: '关闭' }))
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()

    // Dirty draft: closing asks before discarding.
    fireEvent.dblClick(screen.getByTitle('a.png'))
    dialog = await screen.findByRole('dialog', { name: 'a.png' })
    footer = dialog.querySelector('.tm-drawer-footer') as HTMLElement
    fireEvent.click(screen.getByRole('button', { name: '移除 solo' }))
    fireEvent.click(within(footer).getByRole('button', { name: '关闭' }))
    const confirm = screen.getByRole('alertdialog')
    expect(confirm.textContent).toContain('未保存')

    // Cancel keeps the editor and the draft intact.
    fireEvent.click(within(confirm).getByRole('button', { name: '取消' }))
    expect(screen.getByRole('dialog', { name: 'a.png' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '移除 long_hair' })).toBeInTheDocument()

    // Discarding closes the editor without a save request.
    fireEvent.click(within(footer).getByRole('button', { name: '关闭' }))
    fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: '丢弃更改' }))
    expect(screen.queryByRole('dialog', { name: 'a.png' })).not.toBeInTheDocument()
    expect(state.patchBodies).toHaveLength(0)
  })
  it('keeps the draft after a save instead of remounting the editor', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')
    fireEvent.dblClick(screen.getByTitle('a.png'))
    const dialog = await screen.findByRole('dialog', { name: 'a.png' })
    fireEvent.click(screen.getByRole('button', { name: '移除 solo' }))
    fireEvent.click(within(dialog).getByRole('button', { name: '保存' }))
    await waitFor(() => expect(state.patchBodies).toHaveLength(1))
    // The editor stays open with the saved state — a remount would flash the
    // loading drawer and drop the pill row the user was looking at.
    expect(screen.getByRole('dialog', { name: 'a.png' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '移除 long_hair' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '移除 solo' })).not.toBeInTheDocument()
    expect(dialog.textContent).not.toContain('未保存更改')
  })
  it('saves and moves to the next image in the current page', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')
    fireEvent.dblClick(screen.getByTitle('a.png'))
    const dialog = await screen.findByRole('dialog', { name: 'a.png' })
    fireEvent.click(screen.getByRole('button', { name: '移除 solo' }))
    fireEvent.click(within(dialog).getByRole('button', { name: '保存并下一张' }))
    await waitFor(() => expect(state.patchBodies).toHaveLength(1))
    // The editor switches to b.png without an intermediate loading state.
    await screen.findByRole('dialog', { name: 'b.png' })
    expect(state.patchBodies[0]).toEqual({
      content: { kind: 'tag_txt', tags: ['long_hair'] },
      expected_sidecar_mtime: 1_725_148_800,
    })
  })
  it('runs a batch against the filtered result without selecting anything', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')
    // No checkbox touched: the batch bar is already mounted and the scope
    // follows the (empty) selection to the filtered result.
    expect(screen.getByRole('button', { name: '当前过滤结果（3）' })).toHaveAttribute('aria-pressed', 'true')
    const tagInput = screen.getByRole('combobox', { name: '批量标签' })
    fireEvent.change(tagInput, { target: { value: '1g' } })
    await screen.findByRole('option', { name: /1girl/ })
    fireEvent.keyDown(tagInput, { key: 'Enter' })
    fireEvent.click(screen.getByRole('button', { name: '执行' }))
    const confirmation = screen.getByRole('alertdialog', { name: '对 3 张图片执行「添加」？' })
    expect(confirmation.textContent).toContain('当前过滤结果的全部 3 张图片')
    fireEvent.click(screen.getByRole('button', { name: '确认执行' }))
    await waitFor(() => expect(state.batchBodies).toHaveLength(1))
    expect(state.batchBodies[0]).toEqual({
      op: 'add',
      tags: ['1girl'],
      filter: {
        include_tags: [],
        exclude_tags: [],
        include_mode: 'all',
        kind: 'any',
        sidecar: 'any',
      },
      use_regex: false,
    })
  })

  it('fetches each visible thumbnail once across unrelated re-renders', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')
    await waitFor(() => expect(state.thumbnailCalls).toHaveLength(3))

    // Selecting a card and toggling the stats panel re-render the whole grid;
    // neither may refetch the thumbnails that are already on screen.
    fireEvent.click(screen.getByRole('checkbox', { name: '选择 a.png' }))
    await waitFor(() => expect(screen.getByText('选中图片（1）')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: '收起' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '展开' })).toBeInTheDocument())

    expect(state.thumbnailCalls).toHaveLength(3)
    expect(state.thumbnailCalls).toEqual([1, 2, 3])
  })

  it('reloads the fresh server content after a save conflict', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    // Draft an edit, then the server rejects the save: the sidecar changed
    // externally while the drawer was open.
    fireEvent.dblClick(screen.getByTitle('a.png'))
    const dialog = await screen.findByRole('dialog', { name: 'a.png' })
    fireEvent.click(screen.getByRole('button', { name: '移除 solo' }))
    state.patchConflict = true
    state.sidecarMtime = 1_999_999_999
    state.detail = {
      ...detail,
      content: { kind: 'tag_txt', tags: ['solo', 'long_hair', '1girl'] },
      sidecar_mtime: 1_999_999_999,
    }
    fireEvent.click(within(dialog).getByRole('button', { name: '保存' }))
    await waitFor(() => expect(screen.getByText(/sidecar 在编辑期间被外部修改/)).toBeInTheDocument())

    // 重新加载 discards the stale draft and resyncs from the refetched detail:
    // the new server tag appears and the removed pill is back.
    fireEvent.click(screen.getByRole('button', { name: '重新加载' }))
    await screen.findByRole('button', { name: '移除 1girl' })
    expect(screen.getByRole('button', { name: '移除 solo' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '移除 long_hair' })).toBeInTheDocument()
    const reloaded = screen.getByRole('dialog', { name: 'a.png' })
    expect(reloaded.textContent).not.toContain('未保存更改')
    expect(screen.queryByText(/sidecar 在编辑期间被外部修改/)).not.toBeInTheDocument()

    // The next save carries the fresh mtime, so the stale-draft loophole
    // (old draft + new mtime) is closed.
    state.patchConflict = false
    fireEvent.click(within(reloaded).getByRole('button', { name: '保存' }))
    await waitFor(() => expect(state.patchBodies).toHaveLength(2))
    expect(state.patchBodies[1]).toEqual({
      content: { kind: 'tag_txt', tags: ['solo', 'long_hair', '1girl'] },
      expected_sidecar_mtime: 1_999_999_999,
    })
  })

  it('toggles selection with a single card click instead of opening the editor', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    fireEvent.click(screen.getByTitle('a.png'))
    expect(screen.getByText('选中图片（1）')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: '选择 a.png' })).toBeChecked()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()

    // A second plain click deselects; the editor only opens on double click.
    fireEvent.click(screen.getByTitle('a.png'))
    expect(screen.getByText('选中图片（0）')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: '选择 a.png' })).not.toBeChecked()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('opens the editor from the card corner edit button', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    fireEvent.click(screen.getByRole('button', { name: '编辑 a.png' }))
    expect(await screen.findByRole('dialog', { name: 'a.png' })).toBeInTheDocument()
  })

  it('navigates the grid with the keyboard: arrows move focus, space selects, enter opens', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    // The first card is the grid's roving tab stop; everything else is -1.
    const first = screen.getByTitle('a.png')
    expect(first).toHaveAttribute('tabindex', '0')
    expect(screen.getByTitle('b.png')).toHaveAttribute('tabindex', '-1')
    expect(first.closest('[role="grid"]')).toHaveAttribute('aria-label', '图片网格')

    fireEvent.keyDown(first, { key: 'ArrowRight' })
    const second = screen.getByTitle('b.png')
    expect(second).toHaveAttribute('tabindex', '0')
    expect(first).toHaveAttribute('tabindex', '-1')
    expect(document.activeElement).toBe(second)

    // Space toggles the focused card's selection without opening the editor.
    fireEvent.keyDown(second, { key: ' ' })
    expect(screen.getByRole('checkbox', { name: '选择 b.png' })).toBeChecked()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()

    // Enter opens the editor for the focused card.
    fireEvent.keyDown(second, { key: 'Enter' })
    expect(await screen.findByRole('dialog', { name: 'b.png' })).toBeInTheDocument()
  })

  it('removes a tag only through the explicit remove button, not the pill body', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    fireEvent.dblClick(screen.getByTitle('a.png'))
    await screen.findByRole('dialog', { name: 'a.png' })

    // The pill body is presentational: clicking it must not remove anything.
    const pill = screen.getByRole('button', { name: '移除 solo' }).closest('.tm-pill')
    expect(pill).not.toBeNull()
    expect(pill).not.toHaveAttribute('role', 'button')
    fireEvent.click(pill as HTMLElement)
    expect(screen.getByRole('button', { name: '移除 solo' })).toBeInTheDocument()

    // Wiki lookup and removal are sibling buttons of the pill.
    expect(screen.getByRole('button', { name: '查看 solo 的 Wiki' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '移除 solo' }))
    expect(screen.queryByRole('button', { name: '移除 solo' })).not.toBeInTheDocument()
  })

  it('creates a sidecar for an image without one and saves it as tag_txt', async () => {
    state.detail = { ...detail, content: { kind: 'none' } }
    setupFetch(state)
    renderPage()
    await screen.findByAltText('b.png')

    fireEvent.dblClick(screen.getByTitle('b.png'))
    const dialog = await screen.findByRole('dialog', { name: 'b.png' })
    expect(screen.getByText('暂无 sidecar')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('radio', { name: /tag_txt/ }))
    fireEvent.click(within(dialog).getByRole('button', { name: '创建' }))

    const addInput = screen.getByRole('combobox', { name: '添加标签' })
    fireEvent.change(addInput, { target: { value: 'hakurei' } })
    const suggestion = await screen.findByRole('option', { name: /hakurei_reimu/ })
    fireEvent.mouseDown(within(suggestion).getByRole('button'))
    await waitFor(() => expect(screen.getByRole('button', { name: '移除 hakurei_reimu' })).toBeInTheDocument())

    fireEvent.click(within(dialog).getByRole('button', { name: '保存' }))
    await waitFor(() => expect(state.patchBodies).toHaveLength(1))
    expect(state.patchBodies[0]).toEqual({
      content: { kind: 'tag_txt', tags: ['hakurei_reimu'] },
      expected_sidecar_mtime: 1_725_148_800,
    })
  })

  it('saves and moves to the previous image', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('b.png')

    fireEvent.dblClick(screen.getByTitle('b.png'))
    const dialog = await screen.findByRole('dialog', { name: 'b.png' })
    fireEvent.click(screen.getByRole('button', { name: '移除 solo' }))
    fireEvent.click(within(dialog).getByRole('button', { name: '保存并上一张' }))

    await waitFor(() => expect(state.patchBodies).toHaveLength(1))
    await screen.findByRole('dialog', { name: 'a.png' })
    expect(state.patchBodies[0]).toEqual({
      content: { kind: 'tag_txt', tags: ['long_hair'] },
      expected_sidecar_mtime: 1_725_148_800,
    })
  })

  it('crosses page boundaries when saving and navigating', async () => {
    const pageTwo: TagManagerImageSummary[] = [
      summary(101, 'd.png', 'tag_txt', 2),
      summary(102, 'e.png', 'tags_json', 3),
      summary(103, 'f.png', 'standard_json', 1),
    ]
    // PAGE_SIZE is 60, so a total above 60 produces two pages; the mock slices
    // its items by offset instead of returning full pages.
    state.pagedPages = { first: imageItems, second: pageTwo, total: 61 }
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    // Last image of page 1 → 保存并下一张 flips to page 2 and opens its first.
    fireEvent.dblClick(screen.getByTitle('c.png'))
    const firstDialog = await screen.findByRole('dialog', { name: 'c.png' })
    fireEvent.click(screen.getByRole('button', { name: '移除 solo' }))
    fireEvent.click(within(firstDialog).getByRole('button', { name: '保存并下一张' }))

    await waitFor(() => expect(state.patchBodies).toHaveLength(1))
    await screen.findByRole('dialog', { name: 'd.png' })
    expect(state.imageQueries.some((query) => query.includes('offset=60'))).toBe(true)
    expect(screen.getByText(/第 2 \/ 2 页/)).toBeInTheDocument()

    // First image of page 2 → 保存并上一张 returns to page 1's last image.
    fireEvent.click(screen.getByRole('button', { name: '移除 solo' }))
    fireEvent.click(within(screen.getByRole('dialog', { name: 'd.png' })).getByRole('button', { name: '保存并上一张' }))
    await waitFor(() => expect(state.patchBodies).toHaveLength(2))
    await screen.findByRole('dialog', { name: 'c.png' })
    expect(state.imageQueries.some((query) => query.includes('offset=0'))).toBe(true)
    expect(screen.getByText(/第 1 \/ 2 页/)).toBeInTheDocument()
    expect(screen.getByAltText('a.png')).toBeInTheDocument()
  })

  it('copies the unsaved draft to the clipboard from the conflict notice', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    fireEvent.dblClick(screen.getByTitle('a.png'))
    const dialog = await screen.findByRole('dialog', { name: 'a.png' })
    fireEvent.click(screen.getByRole('button', { name: '移除 solo' }))
    state.patchConflict = true
    fireEvent.click(within(dialog).getByRole('button', { name: '保存' }))
    expect(await screen.findByText(/sidecar 在编辑期间被外部修改/)).toBeInTheDocument()

    const clipboard = { writeText: vi.fn().mockResolvedValue(undefined) }
    Object.defineProperty(window.navigator, 'clipboard', { value: clipboard, configurable: true })
    fireEvent.click(screen.getByRole('button', { name: '复制草稿' }))
    expect(await screen.findByRole('button', { name: '已复制' })).toBeInTheDocument()
    expect(clipboard.writeText).toHaveBeenCalledWith(JSON.stringify({ kind: 'tag_txt', tags: ['long_hair'] }, null, 2))
    Object.defineProperty(window.navigator, 'clipboard', { value: undefined, configurable: true })
  })

  it('stacks page notices in a queue and closes each independently', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    fireEvent.click(screen.getByRole('button', { name: '撤销' }))
    await waitFor(() => expect(state.undoCalls).toBe(1))
    fireEvent.click(screen.getByRole('button', { name: '重做' }))
    await waitFor(() => expect(state.redoCalls).toBe(1))

    // Both notices stay visible instead of the second overwriting the first.
    expect(await screen.findByText('已撤销上一次操作')).toBeInTheDocument()
    expect(screen.getByText('已重做操作')).toBeInTheDocument()
    const closeButtons = screen.getAllByRole('button', { name: '关闭提示' })
    expect(closeButtons).toHaveLength(2)

    // Each entry carries its own close button; closing one keeps the other.
    fireEvent.click(closeButtons[0] as HTMLElement)
    expect(screen.queryByText('已撤销上一次操作')).not.toBeInTheDocument()
    expect(screen.getByText('已重做操作')).toBeInTheDocument()
  })

  it('maps known backend error codes to the Chinese notice copy', async () => {
    state.undoError = { code: 'session_busy', message: 'Session is busy with another write' }
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    fireEvent.click(screen.getByRole('button', { name: '撤销' }))
    expect(await screen.findByText('会话正在执行其它操作，请稍后重试')).toBeInTheDocument()
  })

  it('falls back to the backend message for unknown error codes', async () => {
    state.undoError = { code: 'mystery_code', message: 'Unknown backend failure' }
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    fireEvent.click(screen.getByRole('button', { name: '撤销' }))
    expect(await screen.findByText('Unknown backend failure')).toBeInTheDocument()
  })

  it('extends the selection with shift-click and toggles with ctrl-click', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    // Plain click sets the range anchor, shift-click extends to the range.
    fireEvent.click(screen.getByTitle('a.png'))
    fireEvent.click(screen.getByTitle('c.png'), { shiftKey: true })
    expect(screen.getByText('选中图片（3）')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: '选择 b.png' })).toBeChecked()

    // ctrl-click toggles one card out of the range without touching the rest.
    fireEvent.click(screen.getByTitle('b.png'), { ctrlKey: true })
    expect(screen.getByText('选中图片（2）')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: '选择 b.png' })).not.toBeChecked()
    expect(screen.getByRole('checkbox', { name: '选择 a.png' })).toBeChecked()
  })

  it('keeps the selection across pages and shift-clicks plainly after a page flip', async () => {
    const pageTwo: TagManagerImageSummary[] = [
      summary(101, 'd.png', 'tag_txt', 2),
      summary(102, 'e.png', 'tags_json', 3),
      summary(103, 'f.png', 'standard_json', 1),
    ]
    state.pagedPages = { first: imageItems, second: pageTwo, total: 61 }
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    // The anchor is a.png; the page flip drops it (page composition changed)
    // but the selection itself persists across pages.
    fireEvent.click(screen.getByTitle('a.png'))
    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    await screen.findByAltText('d.png')

    // A stale-anchor shift-click must be a plain toggle, never a wrong range.
    fireEvent.click(screen.getByTitle('f.png'), { shiftKey: true })
    expect(screen.getByText('选中图片（2）')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: '选择 d.png' })).not.toBeChecked()
    expect(screen.getByRole('checkbox', { name: '选择 e.png' })).not.toBeChecked()
    expect(screen.getByRole('checkbox', { name: '选择 f.png' })).toBeChecked()
  })

  it('clears the grid selection after a batch operation completes', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    fireEvent.click(screen.getByRole('checkbox', { name: '选择 a.png' }))
    expect(screen.getByText('选中图片（1）')).toBeInTheDocument()
    const tagInput = screen.getByRole('combobox', { name: '批量标签' })
    fireEvent.change(tagInput, { target: { value: '1g' } })
    await screen.findByRole('option', { name: /1girl/ })
    fireEvent.keyDown(tagInput, { key: 'Enter' })
    fireEvent.click(screen.getByRole('button', { name: '执行' }))
    fireEvent.click(screen.getByRole('button', { name: '确认执行' }))

    await waitFor(() => expect(state.batchBodies).toHaveLength(1))
    await waitFor(() => expect(screen.getByText('选中图片（0）')).toBeInTheDocument())
    expect(screen.getByRole('checkbox', { name: '选择 a.png' })).not.toBeChecked()
  })

  it('clears the grid selection after undo rewrites session data', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    fireEvent.click(screen.getByRole('checkbox', { name: '选择 a.png' }))
    expect(screen.getByText('选中图片（1）')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '撤销' }))

    await waitFor(() => expect(state.undoCalls).toBe(1))
    await waitFor(() => expect(screen.getByText('选中图片（0）')).toBeInTheDocument())
  })

  it('clears the selection and the editor when a newly created session takes over', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    fireEvent.click(screen.getByRole('checkbox', { name: '选择 a.png' }))
    fireEvent.click(screen.getByRole('button', { name: '编辑 a.png' }))
    await screen.findByRole('dialog', { name: 'a.png' })

    const pathInput = screen.getByLabelText('相对路径')
    fireEvent.change(pathInput, { target: { value: 'cats_v2' } })
    const openButton = screen.getByRole('button', { name: '打开' })
    await waitFor(() => expect(openButton).toBeEnabled())
    fireEvent.click(openButton)

    await waitFor(() => expect(state.createBodies).toHaveLength(1))
    // The editor closes and the selection is dropped: session-scoped state
    // must not leak into the created (auto-selected) session.
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'a.png' })).not.toBeInTheDocument())
    await waitFor(() => expect(headingStatsText()).toContain('0 已选'))
    await waitFor(() => expect(screen.getByRole('combobox', { name: '现有会话' })).toHaveValue('ds-2'))
  })

  it('falls back to the next session after a deletion and drops editor and selection', async () => {
    state.sessions = [session, { ...session, id: 'ds-2', name: 'dogs', relative_path: 'dogs' }]
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    fireEvent.click(screen.getByRole('checkbox', { name: '选择 a.png' }))
    fireEvent.click(screen.getByRole('button', { name: '编辑 a.png' }))
    await screen.findByRole('dialog', { name: 'a.png' })

    fireEvent.click(screen.getByRole('button', { name: '删除会话' }))
    const confirm = screen.getByRole('alertdialog')
    fireEvent.click(within(confirm).getByRole('button', { name: '删除会话' }))

    await waitFor(() => expect(state.deleteCalls).toBe(1))
    // Auto-switch: the first remaining session becomes active with clean state.
    await waitFor(() => expect(screen.getByRole('combobox', { name: '现有会话' })).toHaveValue('ds-2'))
    expect(screen.queryByRole('dialog', { name: 'a.png' })).not.toBeInTheDocument()
    expect(headingStatsText()).toContain('0 已选')
  })

  it('retries the image list after a load failure', async () => {
    state.imagesError = true
    setupFetch(state)
    renderPage()

    expect(await screen.findByText('图片列表加载失败')).toBeInTheDocument()
    state.imagesError = false
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    await screen.findByAltText('a.png')
    expect(screen.getByAltText('b.png')).toBeInTheDocument()
  })

  it('retries the image detail after a load failure', async () => {
    state.detailError = true
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    fireEvent.click(screen.getByRole('button', { name: '编辑 a.png' }))
    const failed = await screen.findByRole('dialog', { name: '图片内容加载失败' })
    expect(failed).toBeInTheDocument()

    state.detailError = false
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    await screen.findByRole('dialog', { name: 'a.png' })
  })

  it('renders raw_e621_json strictly read-only: no removal, save disabled', async () => {
    state.detail = { ...detail, content: { kind: 'raw_e621_json', tags: ['solo', 'long_hair'], read_only: true } }
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    fireEvent.dblClick(screen.getByTitle('a.png'))
    const dialog = await screen.findByRole('dialog', { name: 'a.png' })
    expect(within(dialog).getByText(/只能查看/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '移除 solo' })).not.toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: '添加标签' })).not.toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: '保存' })).toBeDisabled()
    expect(within(dialog).getByRole('button', { name: '保存并下一张' })).toBeDisabled()
    expect(state.patchBodies).toHaveLength(0)
  })

  it('keeps save disabled for a sidecar-less image until a draft is created', async () => {
    state.detail = { ...detail, content: { kind: 'none' } }
    setupFetch(state)
    renderPage()
    await screen.findByAltText('b.png')

    fireEvent.dblClick(screen.getByTitle('b.png'))
    const dialog = await screen.findByRole('dialog', { name: 'b.png' })
    expect(within(dialog).getByText('暂无 sidecar')).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: '保存' })).toBeDisabled()

    fireEvent.click(within(dialog).getByRole('radio', { name: /tag_txt/ }))
    fireEvent.click(within(dialog).getByRole('button', { name: '创建' }))
    expect(within(dialog).getByRole('button', { name: '保存' })).toBeEnabled()
  })

  it('keeps the draft dirty after a failed save and retries successfully', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    fireEvent.dblClick(screen.getByTitle('a.png'))
    const dialog = await screen.findByRole('dialog', { name: 'a.png' })
    fireEvent.click(screen.getByRole('button', { name: '移除 solo' }))
    state.patchError = { code: 'storage_failed', message: 'disk on fire' }
    fireEvent.click(within(dialog).getByRole('button', { name: '保存' }))
    expect(await screen.findByText('disk on fire')).toBeInTheDocument()

    // The draft and the dirty marker survive the failure untouched.
    expect(screen.getByRole('button', { name: '移除 long_hair' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '移除 solo' })).not.toBeInTheDocument()
    expect(within(dialog).getByText(/有未保存更改/)).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: '保存' })).toBeEnabled()

    // The retry sends the full draft with the still-unconsumed mtime.
    state.patchError = null
    fireEvent.click(within(dialog).getByRole('button', { name: '保存' }))
    await waitFor(() => expect(state.patchBodies).toHaveLength(2))
    expect(state.patchBodies[1]).toEqual({
      content: { kind: 'tag_txt', tags: ['long_hair'] },
      expected_sidecar_mtime: 1_725_148_800,
    })
    expect(within(dialog).queryByText(/有未保存更改/)).not.toBeInTheDocument()
  })

  it('keeps edits made during a pending save dirty and saves them on the next attempt', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    fireEvent.dblClick(screen.getByTitle('a.png'))
    const dialog = await screen.findByRole('dialog', { name: 'a.png' })
    fireEvent.click(screen.getByRole('button', { name: '移除 solo' }))
    const gate = deferred()
    state.patchGate = gate
    fireEvent.click(within(dialog).getByRole('button', { name: '保存' }))
    await waitFor(() => expect(state.patchBodies).toHaveLength(1))

    // The user keeps editing while the first save is in flight.
    fireEvent.click(screen.getByRole('button', { name: '移除 long_hair' }))
    expect(within(dialog).getByRole('button', { name: '保存' })).toBeDisabled()
    gate.release()
    await waitFor(() => expect(within(dialog).getByRole('button', { name: '保存' })).toBeEnabled())

    // The baseline is the saved draft, so the mid-flight edit stays dirty…
    expect(within(dialog).getByText(/有未保存更改/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '移除 solo' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '移除 long_hair' })).not.toBeInTheDocument()

    // …and the follow-up save writes exactly the continued draft with the
    // mtime reported by the first save (no stale-mtime regression).
    fireEvent.click(within(dialog).getByRole('button', { name: '保存' }))
    await waitFor(() => expect(state.patchBodies).toHaveLength(2))
    expect(state.patchBodies[0]).toEqual({
      content: { kind: 'tag_txt', tags: ['long_hair'] },
      expected_sidecar_mtime: 1_725_148_800,
    })
    expect(state.patchBodies[1]).toEqual({
      content: { kind: 'tag_txt', tags: [] },
      expected_sidecar_mtime: 1_725_148_805,
    })
    expect(within(dialog).queryByText(/有未保存更改/)).not.toBeInTheDocument()
  })

  it('sends the updated sidecar_mtime on consecutive saves of the same image', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    fireEvent.dblClick(screen.getByTitle('a.png'))
    const dialog = await screen.findByRole('dialog', { name: 'a.png' })
    fireEvent.click(screen.getByRole('button', { name: '移除 solo' }))
    fireEvent.click(within(dialog).getByRole('button', { name: '保存' }))
    await waitFor(() => expect(state.patchBodies).toHaveLength(1))
    expect(state.patchBodies[0]).toMatchObject({ expected_sidecar_mtime: 1_725_148_800 })

    // Continue editing after the refetch and save again: the second request
    // must carry the mtime from the first PATCH response, not the original.
    const addInput = screen.getByRole('combobox', { name: '添加标签' })
    fireEvent.change(addInput, { target: { value: 'hakurei' } })
    const suggestion = await screen.findByRole('option', { name: /hakurei_reimu/ })
    fireEvent.mouseDown(within(suggestion).getByRole('button'))
    await waitFor(() => expect(screen.getByRole('button', { name: '移除 hakurei_reimu' })).toBeInTheDocument())

    fireEvent.click(within(dialog).getByRole('button', { name: '保存' }))
    await waitFor(() => expect(state.patchBodies).toHaveLength(2))
    expect(state.patchBodies[1]).toEqual({
      content: { kind: 'tag_txt', tags: ['long_hair', 'hakurei_reimu'] },
      expected_sidecar_mtime: 1_725_148_805,
    })
    expect(within(dialog).queryByText(/有未保存更改/)).not.toBeInTheDocument()
  })
})