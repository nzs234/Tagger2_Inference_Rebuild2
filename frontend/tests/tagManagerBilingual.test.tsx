import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { TagManager } from '../src/pages/TagManager'
import {
  formatTagForDisplay,
  toWriteStyle,
  translationFor,
  translationKey,
} from '../src/lib/tagManager'
import type { TagManagerImageDetail, TagManagerImageSummary, TagManagerSession } from '../src/lib/tagManager'
import { usePreferences } from '../src/store/app'

const session: TagManagerSession = {
  id: 'ds-1',
  name: 'cats',
  root_id: 'in',
  relative_path: 'cats',
  profile: 'e621',
  recursive: true,
  status: 'ready',
  error: null,
  image_count: 2,
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-01T00:00:00Z',
}

const imageItems: TagManagerImageSummary[] = [
  {
    id: 1,
    relative_path: 'a.png',
    file_name: 'a.png',
    image_format: 'png',
    sidecar_kind: 'tag_txt',
    mtime: 1_001,
    width: 64,
    height: 64,
    tag_count: 2,
    tags: [
      { tag: 'blue_eyes', category: 'general', translation: '蓝瞳' },
      { tag: 'unmapped_tag', category: 'general', translation: null },
    ],
  },
  {
    id: 2,
    relative_path: 'b.png',
    file_name: 'b.png',
    image_format: 'png',
    sidecar_kind: 'standard_json',
    mtime: 1_002,
    width: 64,
    height: 64,
    tag_count: 1,
    tags: [{ tag: 'wolf', category: 'general', translation: '狼' }],
  },
]

const txtDetail: TagManagerImageDetail = {
  ...(imageItems[0] as TagManagerImageSummary),
  content: { kind: 'tag_txt', tags: ['blue_eyes', 'unmapped_tag'] },
  sidecar_mtime: 1_725_148_800,
  translations: { blue_eyes: '蓝瞳' },
}

const jsonDetail: TagManagerImageDetail = {
  ...(imageItems[1] as TagManagerImageSummary),
  content: {
    kind: 'standard_json',
    fields: {
      quality: [],
      count: 'solo',
      character: '',
      series: '',
      artist: '',
      appearance: ['blue_eyes'],
      tags: ['wolf'],
      environment: ['forest'],
      nl: 'A wolf stands in a forest.',
    },
  },
  sidecar_mtime: 1_725_148_900,
  translations: { blue_eyes: '蓝瞳', wolf: '狼', forest: '森林' },
}

interface HarnessState {
  patchBodies: Array<Record<string, unknown>>
  batchBodies: Array<Record<string, unknown>>
  translateBodies: Array<Record<string, unknown>>
  translateStatus: number
  dictionaryEntries: number
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
    if (path.endsWith('/roots')) return json({ items: [{ id: 'in', name: '训练图片', kind: 'input', writable: true }] })
    if (path.endsWith('/providers')) {
      return json({
        items: [
          { id: 'p-1', name: 'Gemini', enabled: true, configured: true },
          { id: 'p-off', name: '停用的', enabled: false, configured: true },
          { id: 'p-nokey', name: '未配置密钥', enabled: true, configured: false },
        ],
      })
    }
    if (path.endsWith('/tag-db/info')) {
      return json({
        available: { e621: ['classify-e621-test-v1'], danbooru: [] },
        loaded: { e621: true, danbooru: false },
        translations: {
          e621: {
            entries: state.dictionaryEntries,
            loaded: true,
            source: state.dictionaryEntries > 0 ? 'e621-zh.csv.gz' : null,
            updated: state.dictionaryEntries > 0 ? '2026-09-02T00:00:00Z' : null,
          },
          danbooru: { entries: 0, loaded: true, source: null, updated: null },
        },
      })
    }
    if (path.endsWith('/tag-db')) {
      const query = (url.searchParams.get('query') ?? '').toLowerCase()
      const matches = [
        { name: 'long_hair', category: 'general', post_count: 1_200_000, alias_of: null, translation: '长发' },
        { name: 'longcat', category: 'general', post_count: 12, alias_of: null, translation: null },
      ].filter((entry) => entry.name.includes(query))
      return json({ profile: 'e621', items: matches })
    }
    if (path.endsWith('/nl/translate')) {
      state.translateBodies.push(JSON.parse(init?.body as string) as Record<string, unknown>)
      if (state.translateStatus !== 200) {
        // The server's error middleware flattens the envelope and adds a request id.
        return json(
          {
            code: 'nl_translate_unavailable',
            message: '没有可用的在线模型：请先在「Provider 配置」中添加并启用一个在线模型',
            request_id: 'req-1',
            retryable: false,
          },
          state.translateStatus,
        )
      }
      return json({ text: '一只狼站在森林里。', target: 'zh', provider_id: 'p-1', model: 'gemini-flash' })
    }
    if (path === '/api/v1/tag-manager/datasets') return json({ items: [session] })
    if (/\/tag-manager\/datasets\/ds-1$/.test(path)) return json(session)
    if (/\/tag-manager\/datasets\/ds-1\/batch$/.test(path)) {
      state.batchBodies.push(JSON.parse(init?.body as string) as Record<string, unknown>)
      return json({ affected: 1, journal_id: 'j-batch' })
    }
    if (/\/tag-manager\/datasets\/ds-1\/tags\/stats$/.test(path)) {
      return json({ items: [{ tag: 'blue_eyes', category: 'general', count: 2, translation: '蓝瞳' }] })
    }
    if (/\/tag-manager\/datasets\/ds-1\/images\/1$/.test(path)) {
      if (method === 'PATCH') {
        state.patchBodies.push(JSON.parse(init?.body as string) as Record<string, unknown>)
        return json({ image_id: 1, journal_id: 'j-1', sidecar_kind: 'tag_txt' })
      }
      return json(txtDetail)
    }
    if (/\/tag-manager\/datasets\/ds-1\/images\/2$/.test(path)) {
      if (method === 'PATCH') {
        state.patchBodies.push(JSON.parse(init?.body as string) as Record<string, unknown>)
        return json({ image_id: 2, journal_id: 'j-2', sidecar_kind: 'standard_json' })
      }
      return json(jsonDetail)
    }
    if (/\/tag-manager\/datasets\/ds-1\/images$/.test(path)) {
      return json({ items: imageItems, total: imageItems.length })
    }
    return json({})
  })
}

describe('tag style helpers', () => {
  it('formats a tag in either separator style', () => {
    expect(formatTagForDisplay('blue_eyes', 'space')).toBe('blue eyes')
    expect(formatTagForDisplay('blue eyes', 'underscore')).toBe('blue_eyes')
    expect(formatTagForDisplay('blue_eyes', 'underscore')).toBe('blue_eyes')
    expect(formatTagForDisplay('hatsune_miku_(append)', 'space')).toBe('hatsune miku (append)')
  })

  it('writes back the displayed spelling', () => {
    expect(toWriteStyle('blue eyes', 'underscore')).toBe('blue_eyes')
    expect(toWriteStyle('blue_eyes', 'space')).toBe('blue eyes')
  })

  it('normalises translation keys the way the backend does', () => {
    expect(translationKey('  Blue Eyes ')).toBe('blue_eyes')
    expect(translationFor({ blue_eyes: '蓝瞳' }, 'Blue Eyes')).toBe('蓝瞳')
    expect(translationFor({ blue_eyes: '蓝瞳' }, 'green_eyes')).toBeNull()
    expect(translationFor(undefined, 'blue_eyes')).toBeNull()
  })
})

describe('TagManager bilingual display', () => {
  let state: HarnessState

  beforeEach(() => {
    state = {
      patchBodies: [],
      batchBodies: [],
      translateBodies: [],
      translateStatus: 200,
      dictionaryEntries: 68_399,
    }
    // The preference store persists to localStorage, so reset it per test.
    window.localStorage.clear()
    usePreferences.setState({ bilingualTags: true, tagStyle: 'underscore' })
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('shows the Chinese name beside the English tag and omits it when absent', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    const translated = await screen.findByTitle('blue_eyes · 蓝瞳')
    expect(translated).toHaveTextContent('blue_eyes')
    expect(translated).toHaveTextContent('蓝瞳')

    const untranslated = screen.getByTitle('unmapped_tag')
    expect(untranslated).toHaveTextContent('unmapped_tag')
    expect(untranslated.querySelector('.tm-pill-zh')).toBeNull()
  })

  it('hides the Chinese name when 双语显示 is switched off', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')
    expect(await screen.findByTitle('blue_eyes · 蓝瞳')).toBeInTheDocument()

    fireEvent.click(screen.getByLabelText('双语显示'))

    await waitFor(() => expect(screen.queryByTitle('blue_eyes · 蓝瞳')).not.toBeInTheDocument())
    expect(screen.getByTitle('blue_eyes')).toBeInTheDocument()
  })

  it('reports the offline dictionary size and warns when it is missing', async () => {
    setupFetch(state)
    const first = renderPage()
    expect(await screen.findByText(/e621 离线词库：68,399 条/)).toBeInTheDocument()
    first.unmount()
    cleanup()

    state.dictionaryEntries = 0
    renderPage()
    expect(await screen.findByText(/未找到 e621 的离线中文词库/)).toBeInTheDocument()
  })

  it('renders stats and autocomplete bilingually', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    expect(await screen.findByTitle('筛选包含 blue_eyes · 蓝瞳')).toBeInTheDocument()

    fireEvent.click(screen.getByTitle('a.png'))
    await screen.findByRole('dialog', { name: 'a.png' })
    fireEvent.change(screen.getByRole('combobox', { name: '添加标签' }), { target: { value: 'long' } })

    const suggestion = await screen.findByRole('option', { name: /long_hair/ })
    expect(suggestion).toHaveTextContent('长发')
    expect(screen.getByRole('option', { name: /longcat/ })).not.toHaveTextContent('（')
  })
})

describe('TagManager separator style', () => {
  let state: HarnessState

  beforeEach(() => {
    state = {
      patchBodies: [],
      batchBodies: [],
      translateBodies: [],
      translateStatus: 200,
      dictionaryEntries: 68_399,
    }
    window.localStorage.clear()
    usePreferences.setState({ bilingualTags: true, tagStyle: 'underscore' })
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('renders tags with spaces and saves them that way in 空格 mode', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    fireEvent.click(screen.getByRole('button', { name: '空格' }))
    await waitFor(() => expect(screen.getByTitle('blue eyes · 蓝瞳')).toBeInTheDocument())

    fireEvent.click(screen.getByTitle('a.png'))
    const dialog = await screen.findByRole('dialog', { name: 'a.png' })
    expect(within(dialog).getByRole('button', { name: '移除 blue eyes' })).toBeInTheDocument()

    fireEvent.click(within(dialog).getByRole('button', { name: '保存' }))

    await waitFor(() => expect(state.patchBodies).toHaveLength(1))
    expect(state.patchBodies[0]).toEqual({
      content: { kind: 'tag_txt', tags: ['blue eyes', 'unmapped tag'] },
      expected_sidecar_mtime: 1_725_148_800,
    })
  })

  it('keeps the underscore spelling in 下划线 mode', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    fireEvent.click(screen.getByTitle('a.png'))
    const dialog = await screen.findByRole('dialog', { name: 'a.png' })
    fireEvent.click(within(dialog).getByRole('button', { name: '保存' }))

    await waitFor(() => expect(state.patchBodies).toHaveLength(1))
    expect(state.patchBodies[0]).toEqual({
      content: { kind: 'tag_txt', tags: ['blue_eyes', 'unmapped_tag'] },
      expected_sidecar_mtime: 1_725_148_800,
    })
  })

  it('restyles nine-field list values on save', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('b.png')

    fireEvent.click(screen.getByRole('button', { name: '空格' }))
    fireEvent.click(screen.getByTitle('b.png'))
    const dialog = await screen.findByRole('dialog', { name: 'b.png' })
    fireEvent.click(within(dialog).getByRole('button', { name: '保存' }))

    await waitFor(() => expect(state.patchBodies).toHaveLength(1))
    const content = state.patchBodies[0]?.content as { kind: string; fields: Record<string, unknown> }
    expect(content.kind).toBe('standard_json')
    expect(content.fields.tags).toEqual(['wolf'])
    expect(content.fields.appearance).toEqual(['blue eyes'])
    expect(content.fields.environment).toEqual(['forest'])
    // Free-form fields are never restyled.
    expect(content.fields.nl).toBe('A wolf stands in a forest.')
    expect(content.fields.count).toBe('solo')
  })

  it('sends batch tags in the active style but leaves regex patterns alone', async () => {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('a.png')

    fireEvent.click(screen.getByRole('button', { name: '空格' }))
    fireEvent.click(screen.getByRole('checkbox', { name: '选择 a.png' }))

    const tagInput = screen.getByRole('combobox', { name: '批量标签' })
    fireEvent.change(tagInput, { target: { value: 'long_hair' } })
    // In 空格 mode the suggestion is rendered with a space even though the
    // query goes out in underscore form.
    await screen.findByRole('option', { name: /long hair/ })
    fireEvent.keyDown(tagInput, { key: 'Enter' })

    fireEvent.click(screen.getByRole('button', { name: '执行' }))
    fireEvent.click(screen.getByRole('button', { name: '确认执行' }))

    await waitFor(() => expect(state.batchBodies).toHaveLength(1))
    expect(state.batchBodies[0]).toEqual({
      op: 'add',
      tags: ['long hair'],
      use_regex: false,
      image_ids: [1],
    })
  })
})

describe('TagManager NL translation', () => {
  let state: HarnessState

  beforeEach(() => {
    state = {
      patchBodies: [],
      batchBodies: [],
      translateBodies: [],
      translateStatus: 200,
      dictionaryEntries: 68_399,
    }
    window.localStorage.clear()
    usePreferences.setState({
      bilingualTags: true,
      tagStyle: 'underscore',
      tagManagerTranslateProviderId: '',
      tagManagerTranslateModel: '',
    })
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  async function openNineFieldEditor() {
    setupFetch(state)
    renderPage()
    await screen.findByAltText('b.png')
    fireEvent.click(screen.getByTitle('b.png'))
    return screen.findByRole('dialog', { name: 'b.png' })
  }

  it('translates the nl field and only applies it on demand', async () => {
    const dialog = await openNineFieldEditor()
    const nl = within(dialog).getByLabelText('自然语言描述') as HTMLTextAreaElement

    fireEvent.click(within(dialog).getByRole('button', { name: '翻译' }))

    await waitFor(() => expect(state.translateBodies).toHaveLength(1))
    expect(state.translateBodies[0]).toEqual({ text: 'A wolf stands in a forest.', target: 'zh' })
    expect(await within(dialog).findByText('一只狼站在森林里。')).toBeInTheDocument()
    // Nothing is written until the user asks for it.
    expect(nl.value).toBe('A wolf stands in a forest.')

    fireEvent.click(within(dialog).getByRole('button', { name: '替换 NL' }))
    await waitFor(() => expect(nl.value).toBe('一只狼站在森林里。'))
  })

  it('passes the chosen provider, model and direction', async () => {
    const dialog = await openNineFieldEditor()

    fireEvent.change(within(dialog).getByLabelText('翻译方向'), { target: { value: 'en' } })
    await waitFor(() => expect(within(dialog).getByRole('option', { name: 'Gemini' })).toBeInTheDocument())
    fireEvent.change(within(dialog).getByLabelText('翻译使用的在线模型'), { target: { value: 'p-1' } })
    fireEvent.change(within(dialog).getByLabelText('翻译模型 ID'), { target: { value: 'gemini-pro' } })
    fireEvent.click(within(dialog).getByRole('button', { name: '翻译' }))

    await waitFor(() => expect(state.translateBodies).toHaveLength(1))
    expect(state.translateBodies[0]).toEqual({
      text: 'A wolf stands in a forest.',
      target: 'en',
      provider_id: 'p-1',
      model: 'gemini-pro',
    })
  })

  it('omits disabled and unkeyed providers from the picker', async () => {
    const dialog = await openNineFieldEditor()

    await waitFor(() => expect(within(dialog).getByRole('option', { name: 'Gemini' })).toBeInTheDocument())
    expect(within(dialog).queryByRole('option', { name: '停用的' })).not.toBeInTheDocument()
    expect(within(dialog).queryByRole('option', { name: '未配置密钥' })).not.toBeInTheDocument()
  })

  it('explains how to fix a missing online provider', async () => {
    state.translateStatus = 409
    const dialog = await openNineFieldEditor()

    fireEvent.click(within(dialog).getByRole('button', { name: '翻译' }))

    expect(await within(dialog).findByText(/请先在「Provider 配置」页添加并启用一个在线模型/)).toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: '替换 NL' })).not.toBeInTheDocument()
  })
})
