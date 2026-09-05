import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../src/lib/api'
import {
  emptyImageFilter,
  imageFilterQuery,
  tagManagerApi,
  tagManagerThumbnailUrl,
  type TagManagerSession,
} from '../src/lib/tagManager'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

const session: TagManagerSession = {
  id: 'ds-1',
  name: 'cats',
  root_id: 'input-root',
  relative_path: 'cats',
  profile: 'e621',
  recursive: true,
  status: 'ready',
  error: null,
  image_count: 12,
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-01T00:00:00Z',
}

afterEach(() => {
  vi.unstubAllGlobals()
  sessionStorage.clear()
})

describe('tag manager API client', () => {
  it('creates a dataset session with the exact POST contract', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(session, 202))
    vi.stubGlobal('fetch', fetchMock)

    const result = await tagManagerApi.createDataset({
      root_id: 'input-root',
      relative_path: 'cats',
      profile: 'e621',
      recursive: true,
      name: 'cats',
    })

    expect(result).toEqual(session)
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/tag-manager/datasets')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body as string)).toEqual({
      root_id: 'input-root',
      relative_path: 'cats',
      profile: 'e621',
      recursive: true,
      name: 'cats',
    })
  })

  it('lists, reads, deletes, and refreshes dataset sessions', async () => {
    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/tag-manager/datasets') && (!init?.method || init.method === 'GET')) return jsonResponse({ items: [session] })
      if (url.endsWith('/tag-manager/datasets/ds-1/refresh')) return jsonResponse({ ...session, status: 'indexing' }, 202)
      if (url.endsWith('/tag-manager/datasets/ds-1') && init?.method === 'DELETE') return new Response(null, { status: 204 })
      if (url.endsWith('/tag-manager/datasets/ds-1')) return jsonResponse(session)
      throw new Error(`unexpected fetch: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(tagManagerApi.datasets()).resolves.toEqual({ items: [session] })
    await expect(tagManagerApi.dataset('ds-1')).resolves.toEqual(session)
    await expect(tagManagerApi.refreshDataset('ds-1')).resolves.toMatchObject({ status: 'indexing' })
    await expect(tagManagerApi.deleteDataset('ds-1')).resolves.toBeUndefined()
    const [deleteUrl, deleteInit] = fetchMock.mock.calls[3] as [string, RequestInit]
    expect(deleteUrl).toBe('/api/v1/tag-manager/datasets/ds-1')
    expect(deleteInit.method).toBe('DELETE')
  })

  it('serialises image filters, sorting, and pagination into query parameters', () => {
    const query = imageFilterQuery({
      offset: 120,
      limit: 60,
      sort: 'tags',
      filter: {
        includeTags: ['solo', 'long_hair'],
        excludeTags: ['comic'],
        includeMode: 'any',
        kind: 'tag_txt',
        sidecar: 'missing',
      },
    })
    expect(Object.fromEntries(query)).toEqual({
      offset: '120',
      limit: '60',
      sort: 'tags',
      include_tags: 'long_hair', // tags are repeated parameters now; the last one shows here
      exclude_tags: 'comic',
      include_mode: 'any',
      kind: 'tag_txt',
      sidecar: 'missing',
    })
    expect(query.getAll('include_tags')).toEqual(['solo', 'long_hair'])
    expect(query.getAll('exclude_tags')).toEqual(['comic'])
    // Neutral filters must not be sent at all.
    const neutral = imageFilterQuery({ offset: 0, limit: 60, filter: emptyImageFilter })
    expect(Object.fromEntries(neutral)).toEqual({ offset: '0', limit: '60' })
    // A tag containing a comma is escaped, so it survives the round trip.
    const comma = imageFilterQuery({
      offset: 0,
      limit: 60,
      filter: { ...emptyImageFilter, includeTags: ['1girl, smile'], excludeTags: ['a\\b'] },
    })
    expect(comma.getAll('include_tags')).toEqual(['1girl\\, smile'])
    expect(comma.getAll('exclude_tags')).toEqual(['a\\\\b'])
    expect(comma.get('include_tags')).not.toContain('1girl,')
  })

  it('fetches the image page and a single image detail', async () => {
    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/images/1?') || url.endsWith('/images/1')) return jsonResponse({ id: 1, content: { kind: 'none' }, sidecar_mtime: null })
      if (url.includes('/images?')) return jsonResponse({ items: [], total: 0 })
      throw new Error(`unexpected fetch: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const page = await tagManagerApi.images('ds-1', {
      offset: 60,
      limit: 60,
      sort: 'mtime',
      filter: { ...emptyImageFilter, includeTags: ['solo'], includeMode: 'all' },
    })
    expect(page).toEqual({ items: [], total: 0 })
    const [listUrl] = fetchMock.mock.calls[0] as [string]
    expect(listUrl).toBe('/api/v1/tag-manager/datasets/ds-1/images?offset=60&limit=60&sort=mtime&include_tags=solo')

    await expect(tagManagerApi.imageDetail('ds-1', 1)).resolves.toMatchObject({ id: 1, content: { kind: 'none' } })
    const [detailUrl] = fetchMock.mock.calls[1] as [string]
    expect(detailUrl).toBe('/api/v1/tag-manager/datasets/ds-1/images/1')
  })

  it('patches image content with the expected sidecar mtime', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ image_id: 1, journal_id: 'j-1', sidecar_kind: 'tag_txt' }))
    vi.stubGlobal('fetch', fetchMock)

    const result = await tagManagerApi.updateImage('ds-1', 1, {
      content: { kind: 'tag_txt', tags: ['solo', 'long_hair'] },
      expected_sidecar_mtime: 1725148800,
    })

    expect(result).toMatchObject({ image_id: 1, journal_id: 'j-1' })
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/v1/tag-manager/datasets/ds-1/images/1')
    expect(init.method).toBe('PATCH')
    expect(JSON.parse(init.body as string)).toEqual({
      content: { kind: 'tag_txt', tags: ['solo', 'long_hair'] },
      expected_sidecar_mtime: 1725148800,
    })
  })

  it('submits batch operations and journal undo/redo', async () => {
    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/batch')) {
        return jsonResponse({ affected: 3, journal_id: 'j-2' })
      }
      if (url.endsWith('/undo') || url.endsWith('/redo')) return jsonResponse({ journal_id: 'j-3' })
      throw new Error(`unexpected fetch: ${url} ${init?.method ?? 'GET'}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const batch = await tagManagerApi.batch('ds-1', {
      op: 'replace',
      tags: ['cat'],
      replacement: 'dog',
      use_regex: true,
      image_ids: [1, 2],
      filter: { ...emptyImageFilter, sidecar: 'present' },
    })
    expect(batch).toEqual({ affected: 3, journal_id: 'j-2' })
    const [batchUrl, batchInit] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(batchUrl).toBe('/api/v1/tag-manager/datasets/ds-1/batch')
    expect(batchInit.method).toBe('POST')
    expect(JSON.parse(batchInit.body as string)).toEqual({
      op: 'replace',
      tags: ['cat'],
      replacement: 'dog',
      use_regex: true,
      image_ids: [1, 2],
      filter: { includeTags: [], excludeTags: [], includeMode: 'all', kind: 'any', sidecar: 'present' },
    })

    await expect(tagManagerApi.undo('ds-1')).resolves.toEqual({ journal_id: 'j-3' })
    const [undoUrl, undoInit] = fetchMock.mock.calls[1] as [string, RequestInit]
    expect(undoUrl).toBe('/api/v1/tag-manager/datasets/ds-1/undo')
    expect(undoInit.method).toBe('POST')
    await expect(tagManagerApi.redo('ds-1')).resolves.toEqual({ journal_id: 'j-3' })
  })

  it('reads tag stats and the tag database endpoints', async () => {
    const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/tag-db/info')) {
        return jsonResponse({ available: { e621: ['e621-full'], danbooru: [] }, loaded: { e621: true, danbooru: false } })
      }
      if (url.includes('/tag-db?')) return jsonResponse({ profile: 'e621', items: [{ name: 'solo', category: 'general', post_count: 5_000_000, alias_of: null }] })
      if (url.includes('/tags/stats')) return jsonResponse({ items: [{ tag: 'solo', category: 'general', count: 9 }] })
      throw new Error(`unexpected fetch: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const stats = await tagManagerApi.stats('ds-1', { limit: 30, min_count: 2 })
    expect(stats.items).toHaveLength(1)
    const [statsUrl] = fetchMock.mock.calls[0] as [string]
    expect(statsUrl).toBe('/api/v1/tag-manager/datasets/ds-1/tags/stats?limit=30&min_count=2')

    const tagDb = await tagManagerApi.tagDb('e621', 'solo', 20)
    expect(tagDb.items[0]?.name).toBe('solo')
    const [tagDbUrl] = fetchMock.mock.calls[1] as [string]
    expect(tagDbUrl).toBe('/api/v1/tag-manager/tag-db?profile=e621&query=solo&limit=20')

    const info = await tagManagerApi.tagDbInfo()
    expect(info.loaded).toEqual({ e621: true, danbooru: false })
    const [infoUrl] = fetchMock.mock.calls[2] as [string]
    expect(infoUrl).toBe('/api/v1/tag-manager/tag-db/info')
  })

  it('surfaces sidecar conflicts as typed ApiErrors from the detail envelope', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(
      { detail: { code: 'sidecar_conflict', message: 'sidecar 已被外部修改', retryable: false } },
      409,
    )))

    const error = await tagManagerApi.updateImage('ds-1', 1, {
      content: { kind: 'tag_txt', tags: [] },
    }).catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 409, code: 'sidecar_conflict', message: 'sidecar 已被外部修改' })
  })

  it('builds the direct thumbnail URL used by <img> elements', () => {
    expect(tagManagerThumbnailUrl('ds-1', 7)).toBe('/api/v1/tag-manager/datasets/ds-1/images/7/thumbnail?size=256')
    expect(tagManagerThumbnailUrl('ds-1', 7, 512)).toBe('/api/v1/tag-manager/datasets/ds-1/images/7/thumbnail?size=512')
  })
})
