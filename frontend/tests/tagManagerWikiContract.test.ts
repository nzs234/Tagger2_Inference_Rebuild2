import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../src/lib/api'
import { tagManagerApi } from '../src/lib/tagManager'
import { describeWikiError, tagWikiApi, type LookupResult, type SearchResult, type TagWikiStatus } from '../src/lib/tagWiki'

/**
 * Contract tests for the tag-manager / tag-wiki HTTP boundary.
 *
 * The JSON payloads below mirror exactly what the backend produces (see
 * backend/tests/test_tag_manager_wiki_integration.py): the app-wide flat
 * error envelope from create_app, the nested {detail} envelope from bare
 * router mounts, and the response shapes the typed clients rely on.
 */

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

// Flat envelope: what the production app's exception handler emits.
const FLAT_409 = {
  code: 'wiki_not_built',
  message: '本地 Wiki 还没有数据：请先在构建面板下载并构建',
  fields: null,
  request_id: 'req-flat-1',
  retryable: false,
}

// Nested envelope: what a bare router mount (FastAPI default) emits.
const NESTED_404 = {
  detail: { code: 'dataset_not_found', message: 'dataset session not found', retryable: false },
}

const BACKEND_LOOKUP: LookupResult = {
  query: '1girl',
  resolved: true,
  tag: { name: 'solo', category: 'general', post_count: 100, alias_of: '1girl', translation: '单人' },
  implications: [
    { name: 'hug', category: 'general', post_count: 500, alias_of: null, translation: '拥抱' },
  ],
  page: {
    title: 'solo',
    wiki_id: 7,
    updated_at: '2026-09-01T00:00:00Z',
    url: 'https://e621.net/wiki_pages/7',
    summary: {
      meaning: '画面中只有一个主体。',
      usage: '单角色登场时使用。',
      pairing: '常与 1boy 搭配。',
      notes: '',
      tags: ['single'],
      provider_id: 'fake-provider',
      model: 'fake-model',
      updated_at: '2026-09-01T00:00:00Z',
    },
    sections: [{ heading: 'Usage', text: 'Only one character is present.' }],
    related_tags: ['duo'],
  },
}

const BACKEND_SEARCH: SearchResult = {
  query: 'hugging',
  items: [
    {
      page_title: 'hug',
      heading: 'Usage',
      text: 'Use for hugging.',
      score: 1,
      matched_by: ['keyword'],
      summary: null,
      tag: { name: 'hug', category: 'general', post_count: 500, alias_of: null, translation: '拥抱' },
    },
  ],
  suggested_tags: [
    { name: 'hug', category: 'general', post_count: 500, alias_of: null, translation: '拥抱' },
  ],
}

const BACKEND_STATUS: TagWikiStatus = {
  profiles: {
    e621: {
      database: { exists: true, pages: 2, chunks: 2, embedded_chunks: 0, translated_pages: 0, dump_date: null },
      index: {
        embedding_model: 'intfloat/multilingual-e5-small',
        embedding_model_ready: false,
        dimension: null,
        fts_enabled: true,
        search_ready: true,
        min_post_count: 1000,
      },
    },
    danbooru: {
      database: { exists: true, pages: 1, chunks: 1, embedded_chunks: 0, translated_pages: 0, dump_date: null },
      index: {
        embedding_model: 'intfloat/multilingual-e5-small',
        embedding_model_ready: false,
        dimension: null,
        fts_enabled: true,
        search_ready: true,
        min_post_count: 1000,
      },
    },
  },
  database: { exists: true, pages: 2, chunks: 2, embedded_chunks: 0, translated_pages: 0, dump_date: null },
  index: {
    embedding_model: 'intfloat/multilingual-e5-small',
    embedding_model_ready: false,
    dimension: null,
    fts_enabled: true,
    search_ready: true,
    min_post_count: 1000,
  },
  build: { state: 'idle', phase: 'done', message: '构建完成', started_at: null, updated_at: null, error: null },
  translate: {
    state: 'idle', done: 0, failed: 0, total: 0, provider_id: '', model: '',
    message: '', started_at: null, updated_at: null, error: null,
  },
}

afterEach(() => {
  vi.unstubAllGlobals()
  sessionStorage.clear()
})

describe('shared error envelope handling', () => {
  it('parses the flat app-wide envelope into a typed ApiError', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(FLAT_409, 409))
    vi.stubGlobal('fetch', fetchMock)

    const error = await tagWikiApi.lookup('hug').catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(ApiError)
    const apiError = error as ApiError
    expect(apiError.status).toBe(409)
    expect(apiError.code).toBe('wiki_not_built')
    expect(apiError.requestId).toBe('req-flat-1')
    expect(apiError.retryable).toBe(false)
    expect(apiError.message).toContain('本地 Wiki 还没有数据')
  })

  it('parses the nested detail envelope produced by bare router mounts', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(NESTED_404, 404))
    vi.stubGlobal('fetch', fetchMock)

    const error = await tagManagerApi.dataset('missing').catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 404, code: 'dataset_not_found' })
  })

  it('maps every shared wiki error code onto actionable guidance', () => {
    const codes = [
      'wiki_not_built',
      'wiki_busy',
      'wiki_ask_unavailable',
      'wiki_search_unavailable',
      'wiki_embed_model_unavailable',
      'wiki_tag_db_unavailable',
      'wiki_ask_failed',
      'wiki_search_failed',
      'wiki_lookup_failed',
    ] as const
    for (const code of codes) {
      const text = describeWikiError(new ApiError('backend message', 409, code), 'fallback')
      expect(text, code).not.toBe('fallback')
      expect(text.length).toBeGreaterThan(0)
    }
    // The manager-side setup codes surface the backend message instead.
    expect(describeWikiError(new ApiError('标签库未就绪', 409, 'tag_db_unavailable'), 'fallback')).toBe('标签库未就绪')
  })
})

describe('backend response shapes satisfy the typed clients', () => {
  it('consumes the wiki lookup contract (alias resolution + translations)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), 'http://localhost')
      expect(url.pathname).toBe('/api/v1/tag-wiki/lookup')
      expect(url.searchParams.get('tag')).toBe('1girl')
      expect(url.searchParams.get('profile')).toBe('e621')
      return jsonResponse(BACKEND_LOOKUP)
    }))

    const lookup = await tagWikiApi.lookup('1girl')

    // Alias + learned translation fields the WikiDrawer renders.
    expect(lookup.resolved).toBe(true)
    expect(lookup.tag?.alias_of).toBe('1girl')
    expect(lookup.tag?.translation).toBe('单人')
    expect(lookup.implications[0]?.translation).toBe('拥抱')
    expect(lookup.page?.sections).toHaveLength(1)
    expect(lookup.page?.summary?.meaning).toContain('一个主体')
  })

  it('consumes the wiki search contract (ChunkHit + suggested TagRefs)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(init?.method).toBe('POST')
      // Defaults (top_k/profile) are applied server-side and stay off the wire.
      expect(JSON.parse(init?.body as string)).toEqual({ query: 'hugging' })
      return jsonResponse(BACKEND_SEARCH)
    }))

    const search = await tagWikiApi.search({ query: 'hugging' })

    // Explicit length guard + non-null assertion (noUncheckedIndexedAccess).
    expect(search.items).toHaveLength(1)
    const hit = search.items[0]!
    expect(hit.matched_by).toContain('keyword')
    expect(hit.tag?.translation).toBe('拥抱')
    expect(search.suggested_tags[0]?.name).toBe('hug')
  })

  it('consumes the wiki status contract with per-profile documents', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(BACKEND_STATUS)))

    const status = await tagWikiApi.status()

    expect(Object.keys(status.profiles ?? {}).sort()).toEqual(['danbooru', 'e621'])
    // Backward-compatible top-level e621 view stays aligned with profiles.e621.
    expect(status.database).toEqual(status.profiles?.e621?.database)
    expect(status.index.search_ready).toBe(true)
    expect(status.build.phase).toBe('done')
    expect(status.translate.state).toBe('idle')
  })

  it('consumes the tag-manager tag-db and translation contracts', async () => {
    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/tag-db/info')) {
        return jsonResponse({
          available: { e621: ['classify-e621-integration-v1'], danbooru: [] },
          loaded: { e621: true, danbooru: false },
          translations: {
            e621: { entries: 320_000, loaded: true, source: 'e621-zh.csv.gz', updated: '2026-09-02T00:00:00Z', user_entries: 1 },
            danbooru: { entries: 0, loaded: true, source: null, updated: null, user_entries: 0 },
          },
        })
      }
      if (url.includes('/tag-db?')) {
        return jsonResponse({
          profile: 'e621',
          items: [
            { name: 'solo', category: 'general', post_count: 100, alias_of: null, translation: '单人' },
            { name: 'kitty', category: 'general', post_count: 700, alias_of: null, translation: null },
          ],
        })
      }
      if (url.endsWith('/translations/lookup')) {
        return jsonResponse({ profile: 'e621', translations: { hug: '拥抱' } })
      }
      if (url.endsWith('/translations/translate')) {
        return jsonResponse({
          profile: 'e621',
          translations: { hug: '拥抱', blue_eyes: '蓝瞳' },
          translated_now: 1,
          from_dictionary: 1,
          provider_id: 'fake-provider',
          model: 'fake-model',
        })
      }
      throw new Error(`unexpected fetch: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const info = await tagManagerApi.tagDbInfo()
    // TagDbProfileTranslationInfo declares entries/loaded/source/updated; the
    // backend additionally sends user_entries, which stays outside the
    // asserted (typed) surface here.
    expect(info.translations?.e621.source).toBe('e621-zh.csv.gz')
    expect(info.translations?.e621.updated).toBe('2026-09-02T00:00:00Z')
    expect(info.translations?.danbooru.source).toBeNull()
    expect(info.available.e621).toEqual(['classify-e621-integration-v1'])

    const tagDb = await tagManagerApi.tagDb('e621', 's', 20)
    expect(tagDb.items.every((item) => 'translation' in item)).toBe(true)
    expect(tagDb.items[0]?.alias_of).toBeNull()

    const learned = await tagManagerApi.translateTags({ profile: 'e621', tags: ['hug', 'blue_eyes'] })
    expect(learned.translated_now).toBe(1)
    expect(learned.from_dictionary).toBe(1)
    expect(learned.provider_id).toBe('fake-provider')
  })
})
