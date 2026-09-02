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
  tagDbQueries: string[]
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <TagManager />
    </QueryClientProvider>,
  )
}

function setupFetch(state: HarnessState) {
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost')
    const method = init?.method ?? 'GET'
    const path = url.pathname
    if (path.endsWith('/health')) return json({ status: 'ok' })
    if (path.endsWith('/roots')) return json({ items: [{ id: 'in', name: '训练图片', kind: 'input', writable: false }] })
    if (path === '/api/v1/tag-db' || path === '/tag-db') {
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
        state.sessions = [{ ...session, name: String(body.name ?? 'cats') }]
        return json(state.sessions[0], 202)
      }
      return json({ items: state.sessions })
    }
    if (/\/tag-manager\/datasets\/ds-1$/.test(path)) return json(state.sessions[0] ?? session)
    if (/\/tag-manager\/datasets\/ds-1\/refresh$/.test(path)) return json({ ...session, status: 'indexing' }, 202)
    if (/\/tag-manager\/datasets\/ds-1\/batch$/.test(path)) {
      state.batchBodies.push(JSON.parse(init?.body as string) as Record<string, unknown>)
      return json({ affected: 2, journal_id: 'j-batch' })
    }
    if (/\/tag-manager\/datasets\/ds-1\/undo$/.test(path)) {
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
    if (/\/tag-manager\/datasets\/ds-1\/images\/1$/.test(path)) {
      if (method === 'PATCH') {
        state.patchBodies.push(JSON.parse(init?.body as string) as Record<string, unknown>)
        return json({ image_id: 1, journal_id: 'j-1', sidecar_kind: 'tag_txt' })
      }
      return json(detail)
    }
    if (/\/tag-manager\/datasets\/ds-1\/images$/.test(path)) return json({ items: imageItems, total: imageItems.length })
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
      tagDbQueries: [],
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
    await waitFor(() => {
      expect(screen.getByRole('combobox', { name: '现有会话' })).toHaveValue('ds-1')
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

    fireEvent.click(screen.getByTitle('a.png'))
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
    const includeInput = screen.getByLabelText('包含标签') as HTMLInputElement
    await waitFor(() => expect(includeInput.value).toBe('solo'))
  })
})
