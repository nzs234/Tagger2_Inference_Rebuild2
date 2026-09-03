import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { TagCloud } from '../src/components/TagCloud'
import { TagWiki } from '../src/pages/TagWiki'
import type {
  AskResult,
  LookupResult,
  SearchResult,
  TagWikiStatus,
} from '../src/lib/tagWiki'
import { usePreferences } from '../src/store/app'

const mockStatus: TagWikiStatus = {
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
  build: {
    state: 'idle',
    phase: 'idle',
    message: '就绪',
    started_at: null,
    updated_at: null,
    error: null,
  },
  translate: {
    state: 'idle',
    done: 500,
    failed: 0,
    total: 1000,
    provider_id: 'gemini',
    model: 'gemini-1.5-flash',
    message: '空闲',
    started_at: null,
    updated_at: null,
    error: null,
  },
}

const mockLookupResult: LookupResult = {
  query: 'solo',
  resolved: true,
  tag: {
    name: 'solo',
    category: 'general',
    post_count: 2_500_000,
    alias_of: null,
    translation: '单人',
  },
  implications: [
    {
      name: '1girl',
      category: 'general',
      post_count: 1_800_000,
      alias_of: null,
      translation: '单人女性',
    },
  ],
  page: {
    title: 'solo',
    wiki_id: 101,
    updated_at: '2026-09-01T12:00:00Z',
    url: 'https://e621.net/wiki_pages/solo',
    summary: {
      meaning: '画面中仅包含一个独立主体。',
      usage: '用于标记单个角色登场的场景。',
      pairing: '通常与 1girl 或 1boy 搭配。',
      notes: '若背景有微小杂兵则视情况而定。',
      tags: ['solo', 'single'],
      provider_id: 'gemini',
      model: 'gemini-flash',
      updated_at: '2026-09-01T12:00:00Z',
    },
    sections: [
      {
        heading: 'Overview',
        text: 'The solo tag is applied when only one character is present in the image.',
      },
      {
        heading: 'Usage Guidelines',
        text: 'Do not use this tag if there are multiple characters.',
      },
    ],
    related_tags: ['duo', 'group'],
  },
}

const mockSearchResult: SearchResult = {
  query: 'solo character',
  items: [
    {
      page_title: 'solo',
      heading: 'Overview',
      text: 'The solo tag is applied when only one character is present in the image.',
      score: 0.95,
      matched_by: ['vector', 'keyword'],
      summary: mockLookupResult.page!.summary ?? null,
      tag: mockLookupResult.tag,
    },
  ],
  suggested_tags: [
    {
      name: 'solo',
      category: 'general',
      post_count: 2_500_000,
      alias_of: null,
      translation: '单人',
    },
  ],
}

const mockAskResult: AskResult = {
  query: '如何使用 solo 标签？',
  answer: 'solo 标签用于表示画面中只有一名角色。请注意与 duo/group 互斥。',
  tags: ['solo', 'duo', 'group'],
  provider_id: 'gemini',
  model: 'gemini-1.5-flash',
  sources: ['solo', 'duo'],
}

interface HarnessState {
  status: TagWikiStatus
  searchBodies: Array<Record<string, unknown>>
  askBodies: Array<Record<string, unknown>>
  askStatus: number
}

function renderTagWikiPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <TagWiki />
    </QueryClientProvider>,
  )
}

function setupFetch(state: HarnessState) {
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    })

  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost')
    const path = url.pathname

    if (path.endsWith('/tag-wiki/status')) {
      return json(state.status)
    }

    if (path.endsWith('/tag-wiki/lookup')) {
      const tag = url.searchParams.get('tag')
      if (tag === 'solo') return json(mockLookupResult)
      return json({
        query: tag ?? '',
        resolved: false,
        tag: null,
        implications: [],
        page: null,
      })
    }

    if (path.endsWith('/tag-wiki/search')) {
      state.searchBodies.push(JSON.parse(init?.body as string) as Record<string, unknown>)
      return json(mockSearchResult)
    }

    if (path.endsWith('/tag-wiki/ask')) {
      state.askBodies.push(JSON.parse(init?.body as string) as Record<string, unknown>)
      if (state.askStatus !== 200) {
        return json(
          {
            code: 'wiki_ask_unavailable',
            message: '未配置或启用在线模型：AI 问答需要在线 LLM Provider。请前往「在线模型」页面配置。',
            request_id: 'req-ask',
            retryable: false,
          },
          state.askStatus,
        )
      }
      return json(mockAskResult)
    }

    return json({})
  })
}

describe('TagWiki Page & WikiDrawer', () => {
  let state: HarnessState

  beforeEach(() => {
    state = {
      status: mockStatus,
      searchBodies: [],
      askBodies: [],
      askStatus: 200,
    }
    usePreferences.setState({ page: 'tag-wiki', bilingualTags: true })
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('renders the page heading and status chips in BuildPanel', async () => {
    setupFetch(state)
    renderTagWikiPage()

    expect(screen.getByRole('heading', { level: 1, name: 'Tag Wiki' })).toBeInTheDocument()
    expect(await screen.findByText(/1,250/)).toBeInTheDocument()
    expect(screen.getByText(/3,500/)).toBeInTheDocument()
    expect(screen.getByText('2026-09-01')).toBeInTheDocument()
  })

  it('looks up a tag and displays its details and summary', async () => {
    setupFetch(state)
    renderTagWikiPage()

    const input = screen.getByLabelText('标签名称')
    fireEvent.change(input, { target: { value: 'solo' } })
    fireEvent.click(screen.getByRole('button', { name: '查询' }))

    expect(await screen.findByText('画面中仅包含一个独立主体。')).toBeInTheDocument()
    expect(screen.getByText('用法')).toBeInTheDocument()
    expect(screen.getByText('搭配建议')).toBeInTheDocument()
    expect(screen.getByText('注意事项')).toBeInTheDocument()
    expect(screen.getByText('隐含标签（需要搭配）')).toBeInTheDocument()
    expect(screen.getByText('单人女性')).toBeInTheDocument()
  })

  it('performs semantic search and renders hits and suggested tags', async () => {
    setupFetch(state)
    renderTagWikiPage()

    // Switch to search tab
    fireEvent.click(screen.getByRole('tab', { name: /语义搜索/ }))

    const textarea = screen.getByLabelText('语义搜索内容')
    fireEvent.change(textarea, { target: { value: 'solo character' } })
    fireEvent.click(screen.getByRole('button', { name: '检索 Wiki 章节' }))

    await waitFor(() => expect(state.searchBodies).toHaveLength(1))
    expect(state.searchBodies[0]).toEqual({ query: 'solo character', top_k: 8 })

    expect(await screen.findByText('推荐候选标签')).toBeInTheDocument()
    expect(screen.getByText(/The solo tag is applied when only one character is present/)).toBeInTheDocument()
  })

  it('performs AI ask and renders answer and sources', async () => {
    setupFetch(state)
    renderTagWikiPage()

    // Switch to ask tab
    fireEvent.click(screen.getByRole('tab', { name: /AI 问答/ }))

    const textarea = screen.getByLabelText('AI 问答内容')
    fireEvent.change(textarea, { target: { value: '如何使用 solo 标签？' } })
    fireEvent.click(screen.getByRole('button', { name: '提问' }))

    await waitFor(() => expect(state.askBodies).toHaveLength(1))
    expect(await screen.findByText(/solo 标签用于表示画面中只有一名角色。/)).toBeInTheDocument()
    expect(screen.getByText('提及标签')).toBeInTheDocument()
    expect(screen.getByText('参考来源')).toBeInTheDocument()
  })

  it('shows guidance and navigation button when 409 wiki_ask_unavailable occurs', async () => {
    state.askStatus = 409
    setupFetch(state)
    renderTagWikiPage()

    fireEvent.click(screen.getByRole('tab', { name: /AI 问答/ }))
    const textarea = screen.getByLabelText('AI 问答内容')
    fireEvent.change(textarea, { target: { value: 'test ask' } })
    fireEvent.click(screen.getByRole('button', { name: '提问' }))

    expect(await screen.findByText(/未配置或启用在线模型/)).toBeInTheDocument()
    const navBtn = screen.getByRole('button', { name: '前往「在线模型」页' })
    expect(navBtn).toBeInTheDocument()

    fireEvent.click(navBtn)
    expect(usePreferences.getState().page).toBe('providers')
  })

  it('opens WikiDrawer from a TagCloud pill and fetches content', async () => {
    setupFetch(state)
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
