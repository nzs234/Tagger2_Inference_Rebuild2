import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { TagCloud } from '../src/components/TagCloud'
import { TagWiki } from '../src/pages/TagWiki'
import type {
  CatalogBrowseResponse,
  CatalogCategoriesResponse,
  CatalogTagDetail,
  LookupResult,
  TagWikiStatus,
} from '../src/lib/tagWiki'
import { clampInt, describeWikiError } from '../src/lib/tagWiki'
import { ApiError } from '../src/lib/api'
import { usePreferences } from '../src/store/app'

const mockStatus: TagWikiStatus = {
  profiles: {
    e621: {
      database: {
        exists: true,
        pages: 1250,
        chunks: 3500,
        embedded_chunks: 3400,
        translated_pages: 500,
        dump_date: '2026-09-01',
      },
      index: {
        embedding_model: 'intfloat/multilingual-e5-small',
        embedding_model_ready: true,
        dimension: 384,
        fts_enabled: true,
        search_ready: true,
      },
    },
  },
  database: {
    exists: true,
    pages: 1250,
    chunks: 3500,
    embedded_chunks: 3400,
    translated_pages: 500,
    dump_date: '2026-09-01',
  },
  index: {
    embedding_model: 'intfloat/multilingual-e5-small',
    embedding_model_ready: true,
    dimension: 384,
    fts_enabled: true,
    search_ready: true,
  },
  build: { state: 'idle', phase: 'idle', message: '就绪', started_at: null, updated_at: null, error: null },
  translate: {
    state: 'idle',
    done: 500,
    failed: 0,
    total: 1000,
    provider_id: 'gemini',
    model: 'gemini-flash',
    message: '空闲',
    started_at: null,
    updated_at: null,
    error: null,
  },
}

const mockCategories: CatalogCategoriesResponse = {
  profile: 'e621',
  built: true,
  generated_at: '2026-09-07T00:00:00+00:00',
  taxonomy_version: 1,
  min_post_count: 100,
  tag_count: 3,
  relation_count: 2,
  categories: [
    {
      category: 'general',
      label: '通用',
      tag_count: 2,
      groups: [
        { key: 'action_pose', label: '动作与姿势', tag_count: 2 },
        { key: 'body_part', label: '身体部位', tag_count: 1 },
      ],
    },
    { category: 'species', label: '物种', tag_count: 1, groups: [{ key: 'species', label: '物种', tag_count: 1 }] },
  ],
}

const hugItem = {
  name: 'hug',
  translation: '拥抱',
  category: 'general',
  group_key: 'action_pose',
  group_label: '动作与姿势',
  post_count: 500,
  has_wiki: true,
  alias_of: null,
}

const mockBrowse: CatalogBrowseResponse = {
  profile: 'e621',
  category: null,
  group: null,
  q: null,
  total: 3,
  offset: 0,
  limit: 60,
  items: [
    { ...hugItem },
    {
      name: 'solo',
      translation: null,
      category: 'general',
      group_key: 'action_pose',
      group_label: '动作与姿势',
      post_count: 2000,
      has_wiki: true,
      alias_of: null,
    },
    {
      name: 'blue_eyes',
      translation: '蓝眼睛',
      category: 'general',
      group_key: 'body_part',
      group_label: '身体部位',
      post_count: 800,
      has_wiki: false,
      alias_of: null,
    },
  ],
}

function aliasBrowse(q: string): CatalogBrowseResponse {
  return {
    ...mockBrowse,
    q,
    total: 1,
    items: [
      {
        name: 'kiss',
        translation: '亲吻',
        category: 'general',
        group_key: 'action_pose',
        group_label: '动作与姿势',
        post_count: 300,
        has_wiki: true,
        alias_of: 'smooch',
        match: 'alias',
      },
    ],
  }
}

const hugDetail: CatalogTagDetail = {
  tag: { ...hugItem },
  page: {
    title: 'hug',
    wiki_id: 1,
    updated_at: '2026-09-01T00:00:00Z',
    url: 'https://e621.net/wiki_pages/1',
    summary: {
      meaning: '拥抱动作。',
      usage: '用于两人相拥的场景。',
      pairing: '常与 kiss 搭配。',
      notes: '',
      tags: ['kiss'],
      provider_id: 'gemini',
      model: 'gemini-flash',
      updated_at: '2026-09-01T00:00:00Z',
    },
    sections: [{ heading: 'Usage', text: 'Use for hugging.' }],
    related_tags: [],
  },
  implications: [
    {
      name: 'kiss',
      relation_type: 'implication',
      direction: 'forward',
      score: 300,
      tag: { name: 'kiss', category: 'general', post_count: 300, translation: '亲吻' },
    },
  ],
  wiki_links: [
    {
      name: 'kiss',
      relation_type: 'wiki_link',
      direction: 'forward',
      score: 300,
      tag: { name: 'kiss', category: 'general', post_count: 300, translation: '亲吻' },
    },
  ],
  cooccurrences: [],
}

const kissDetail: CatalogTagDetail = {
  tag: {
    name: 'kiss',
    translation: '亲吻',
    category: 'general',
    group_key: 'action_pose',
    group_label: '动作与姿势',
    post_count: 300,
    has_wiki: true,
    alias_of: null,
  },
  page: { title: 'kiss', sections: [{ heading: '', text: 'A kiss.' }], related_tags: [], summary: null },
  implications: [],
  wiki_links: [],
  cooccurrences: [],
}

// WikiDrawer (Tag Manager integration) still speaks the legacy lookup API.
const mockLookupResult: LookupResult = {
  query: 'solo',
  resolved: true,
  tag: { name: 'solo', category: 'general', post_count: 2_500_000, alias_of: null, translation: '单人' },
  implications: [],
  page: {
    title: 'solo',
    summary: { meaning: '画面中仅包含一个独立主体。' },
    sections: [{ heading: 'Overview', text: 'Only one character is present.' }],
    related_tags: ['duo'],
  },
}

interface HarnessState {
  browseUrls: string[]
  categoryUrls: string[]
  detailTitles: string[]
  categoriesStatus: number
  totalOverride: number | null
}

function renderTagWikiPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <TagWiki />
    </QueryClientProvider>,
  )
}

function setupFetch(state: HarnessState) {
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), 'http://localhost')
    const path = url.pathname

    if (path.endsWith('/tag-wiki/status')) return json(mockStatus)

    if (path.endsWith('/catalog/categories')) {
      state.categoryUrls.push(url.searchParams.get('profile') ?? '')
      if (state.categoriesStatus !== 200) {
        return json(
          { code: 'wiki_catalog_missing', message: '标签目录尚未生成', request_id: 'r', retryable: false },
          state.categoriesStatus,
        )
      }
      return json({ ...mockCategories, profile: url.searchParams.get('profile') ?? 'e621' })
    }

    if (path.includes('/catalog/tags/')) {
      const title = decodeURIComponent(path.split('/catalog/tags/')[1] ?? '')
      state.detailTitles.push(title)
      if (title === 'kiss') return json(kissDetail)
      if (title === 'hug') return json(hugDetail)
      return json({ ...hugDetail, tag: { ...hugDetail.tag, name: title } })
    }

    if (path.endsWith('/catalog/tags')) {
      state.browseUrls.push(url.searchParams.toString())
      const q = url.searchParams.get('q')
      const offset = Number(url.searchParams.get('offset') ?? 0)
      if (q) return json(aliasBrowse(q))
      const total = state.totalOverride ?? mockBrowse.total
      return json({ ...mockBrowse, total, offset })
    }

    if (path.endsWith('/tag-wiki/lookup')) return json(mockLookupResult)

    return json({})
  })
}

describe('TagWiki catalog page', () => {
  let state: HarnessState

  beforeEach(() => {
    state = { browseUrls: [], categoryUrls: [], detailTitles: [], categoriesStatus: 200, totalOverride: null }
    usePreferences.setState({ page: 'tag-wiki', bilingualTags: true })
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('renders the directory: sidebar categories, tag list and threshold note', async () => {
    setupFetch(state)
    renderTagWikiPage()

    expect(screen.getByRole('heading', { level: 1, name: 'Tag Wiki' })).toBeInTheDocument()
    expect(await screen.findByText('全部标签')).toBeInTheDocument()
    expect(await screen.findByText('hug')).toBeInTheDocument()
    expect(screen.getByText('solo')).toBeInTheDocument()
    expect(screen.getByText(/post_count ≥ 100/)).toBeInTheDocument()
    const groupLabels = screen.getAllByText('动作与姿势', { selector: '.tw-catalog-item-group' })
    expect(groupLabels.length).toBeGreaterThanOrEqual(2)
  })

  it('retired flows must not appear: no tabs, no semantic search, no AI ask, no maintenance', async () => {
    setupFetch(state)
    renderTagWikiPage()

    expect(await screen.findByText('hug')).toBeInTheDocument()
    expect(screen.queryByRole('tab')).not.toBeInTheDocument()
    expect(screen.queryByText(/语义搜索/)).not.toBeInTheDocument()
    expect(screen.queryByText(/AI 问答/)).not.toBeInTheDocument()
    expect(screen.queryByLabelText('语义搜索内容')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('AI 问答内容')).not.toBeInTheDocument()
    expect(screen.queryByText('下载/更新 Wiki 数据')).not.toBeInTheDocument()
  })

  it('shows a second-level group filter row for the selected category', async () => {
    setupFetch(state)
    renderTagWikiPage()

    // Sidebar button accessible name joins the label and count ("通用2").
    fireEvent.click(await screen.findByRole('button', { name: '通用2' }))
    expect(await screen.findByRole('button', { name: /全部 通用/ })).toBeInTheDocument()
    // Selecting the body_part group threads it into the browse request.
    fireEvent.click(screen.getByRole('button', { name: /身体部位/ }))
    await waitFor(() =>
      expect(state.browseUrls.some((u) => u.includes('group=body_part'))).toBe(true),
    )
  })

  it('debounces the search input and requests ranked results', async () => {
    setupFetch(state)
    renderTagWikiPage()
    await screen.findByText('hug')

    const input = screen.getByLabelText('搜索标签')
    fireEvent.change(input, { target: { value: 'smooch' } })
    // No request until the debounce window elapses.
    expect(state.browseUrls.some((u) => u.includes('q=smooch'))).toBe(false)

    await waitFor(() => expect(state.browseUrls.some((u) => u.includes('q=smooch'))).toBe(true), {
      timeout: 1500,
    })
    expect(await screen.findByText('kiss')).toBeInTheDocument()
    expect(screen.getByText('别名')).toBeInTheDocument()
  })

  it('opens the same-page detail view from a list item and navigates via related pills', async () => {
    setupFetch(state)
    renderTagWikiPage()

    fireEvent.click(await screen.findByText('hug'))
    expect(state.detailTitles).toContain('hug')
    expect(await screen.findByText('拥抱动作。')).toBeInTheDocument()
    expect(screen.getByText('隐含标签（需要搭配）')).toBeInTheDocument()
    expect(screen.getByText('Wiki 页面关联')).toBeInTheDocument()

    // Related implication pill navigates within the catalog detail view.
    // (kiss appears as implication pill, wiki-link pill and summary chip.)
    const kissPill = screen.getAllByRole('button', { name: /亲吻/ })[0]
    fireEvent.click(kissPill!)
    await waitFor(() => expect(state.detailTitles).toContain('kiss'))
    expect(await screen.findByText('A kiss.')).toBeInTheDocument()

    // Back to the list: the directory is shown again.
    fireEvent.click(screen.getByRole('button', { name: /返回列表/ }))
    expect(await screen.findByText('solo')).toBeInTheDocument()
  })

  it('supports keyboard navigation: ArrowDown then Enter opens the active tag', async () => {
    setupFetch(state)
    renderTagWikiPage()

    // Wait for the list before navigating it.
    await screen.findByText('hug')
    const input = screen.getByLabelText('搜索标签')
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    const active = document.querySelector('.tw-catalog-item-active')
    expect(active).not.toBeNull()
    expect(active!.getAttribute('data-catalog-index')).toBe('0')

    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(state.detailTitles).toContain('hug'))
    expect(await screen.findByText('拥抱动作。')).toBeInTheDocument()
  })

  it('paginates the directory with the offset parameter', async () => {
    state.totalOverride = 120
    setupFetch(state)
    renderTagWikiPage()

    fireEvent.click(await screen.findByRole('button', { name: /下一页/ }))
    await waitFor(() => expect(state.browseUrls.some((u) => u.includes('offset=60'))).toBe(true))
    expect(await screen.findByText(/第 61–120 个/)).toBeInTheDocument()
  })

  it('switches the wiki profile and threads it into catalog queries', async () => {
    setupFetch(state)
    renderTagWikiPage()
    await screen.findByText('hug')

    fireEvent.click(screen.getByRole('button', { name: 'Danbooru' }))
    await waitFor(() => expect(state.categoryUrls).toContain('danbooru'))
    await waitFor(() => expect(state.browseUrls.some((u) => u.includes('profile=danbooru'))).toBe(true))
  })

  it('surfaces actionable guidance when the catalog has not been generated', async () => {
    state.categoriesStatus = 409
    setupFetch(state)
    renderTagWikiPage()

    // react-query retries once (default 1s backoff) before surfacing the error.
    expect(await screen.findByText(/标签目录尚未生成/, {}, { timeout: 4000 })).toBeInTheDocument()
    expect(screen.getByText(/build_tag_wiki_catalog\.py/)).toBeInTheDocument()
  })
})

describe('TagWiki WikiDrawer (legacy lookup consumers keep working)', () => {
  beforeEach(() => {
    usePreferences.setState({ page: 'tag-wiki', bilingualTags: true })
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('opens WikiDrawer from a TagCloud pill and fetches content', async () => {
    const json = (body: unknown, status = 200) =>
      new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input: RequestInfo | URL) => {
      const path = new URL(String(input), 'http://localhost').pathname
      if (path.endsWith('/tag-wiki/status')) return json(mockStatus)
      if (path.endsWith('/tag-wiki/lookup')) return json(mockLookupResult)
      if (path.endsWith('/catalog/categories')) return json(mockCategories)
      if (path.endsWith('/catalog/tags')) return json(mockBrowse)
      return json({})
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    render(
      <QueryClientProvider client={client}>
        <TagCloud tags={[{ text: 'solo', category: 'general', source: 'test', model_id: 'm1' }]} />
      </QueryClientProvider>,
    )

    const wikiBtn = screen.getByRole('button', { name: '查看 solo 的 Wiki' })
    fireEvent.click(wikiBtn)

    const drawer = await screen.findByRole('dialog', { name: /solo/ })
    expect(drawer).toBeInTheDocument()
    expect(await screen.findByText('画面中仅包含一个独立主体。')).toBeInTheDocument()
  })
})

describe('TagWiki shared helpers', () => {
  it('maps shared wiki error codes onto Chinese guidance', () => {
    expect(
      describeWikiError(new ApiError('busy', 409, 'wiki_busy'), 'fallback'),
    ).toBe('已有构建或翻译任务正在进行中，请等待其完成后再试。')
    expect(
      describeWikiError(new ApiError('missing', 409, 'wiki_catalog_missing'), 'fallback'),
    ).toContain('标签目录尚未生成')
    expect(
      describeWikiError(new ApiError('nf', 404, 'wiki_catalog_tag_not_found'), 'fallback'),
    ).toContain('100 posts')
    // Unknown codes surface the backend message; non-Api errors use the fallback.
    expect(describeWikiError(new ApiError('boom', 500, 'wiki_build_failed'), 'fallback')).toBe('boom')
    expect(describeWikiError(new Error('x'), 'fallback')).toBe('fallback')
  })

  it('clamps panel numeric inputs into the API-accepted range', () => {
    expect(clampInt(Number(''), 1, 50_000)).toBe(1) // cleared input reads as 0
    expect(clampInt(0, 1, 50_000)).toBe(1)
    expect(clampInt(99_999, 1, 50_000)).toBe(50_000)
    expect(clampInt(2.6, 0, 1_000_000)).toBe(3)
    expect(clampInt(Number.NaN, 0, 1_000_000)).toBe(0)
  })
})
