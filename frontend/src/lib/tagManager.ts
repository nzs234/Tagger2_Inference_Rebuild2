import { API_BASE, request } from './api'

// --- Tag Manager types ---

export type TagManagerProfile = 'e621' | 'danbooru'
export type TagManagerSessionStatus = 'indexing' | 'ready' | 'error'
export type TagStyle = 'underscore' | 'space'

export interface TagManagerSession {
  id: string
  name: string
  root_id: string
  relative_path: string
  profile: TagManagerProfile
  recursive: boolean
  status: TagManagerSessionStatus
  error?: string | null
  image_count: number
  created_at: string
  updated_at: string
}

export interface TagManagerCreateRequest {
  root_id: string
  relative_path: string
  profile: TagManagerProfile
  recursive: boolean
  name?: string
}

/** Sidecar flavours the backend recognises. `none` means no sidecar yet. */
export type TagManagerSidecarKind = 'none' | 'tag_txt' | 'tags_json' | 'standard_json' | 'raw_e621_json'

export interface TagManagerImageTag {
  tag: string
  category: string
  translation?: string | null
}

export interface TagManagerImageSummary {
  id: number
  relative_path: string
  file_name: string
  image_format?: string | null
  sidecar_kind: TagManagerSidecarKind
  mtime: number | string
  width?: number | null
  height?: number | null
  tag_count: number
  tags: TagManagerImageTag[]
}

export interface TagManagerImagePage {
  items: TagManagerImageSummary[]
  total: number
}

export type TagManagerSort = 'name' | 'mtime' | 'tags'
export type TagManagerIncludeMode = 'all' | 'any'
export type TagManagerKindFilter = 'any' | TagManagerSidecarKind
export type TagManagerSidecarFilter = 'any' | 'present' | 'missing'

/** UI-facing filter state; the client turns this into query parameters. */
export interface ImageFilterState {
  includeTags: string[]
  excludeTags: string[]
  includeMode: TagManagerIncludeMode
  kind: TagManagerKindFilter
  sidecar: TagManagerSidecarFilter
}

export const emptyImageFilter: ImageFilterState = {
  includeTags: [],
  excludeTags: [],
  includeMode: 'all',
  kind: 'any',
  sidecar: 'any',
}

export interface TagManagerImageQuery {
  offset?: number
  limit?: number
  sort?: TagManagerSort
  filter?: ImageFilterState
}

/** Payload for PATCH image content; discriminated by `kind`. */
export interface TagTxtContent { kind: 'tag_txt'; tags: string[] }
export interface TagsJsonEntry { text: string; category?: string; score?: number }
export interface TagsJsonContent { kind: 'tags_json'; tags: TagsJsonEntry[] }
export interface StandardJsonFields {
  quality: string[]
  count: '' | 'solo' | 'duo' | 'trio' | 'group'
  character: string
  series: string
  artist: string
  appearance: string[]
  tags: string[]
  environment: string[]
  nl: string
}
export interface StandardJsonContent { kind: 'standard_json'; fields: StandardJsonFields }
export interface RawE621JsonContent { kind: 'raw_e621_json'; tags: string[]; read_only: true }
export interface TagManagerContentNone { kind: 'none' }

export type TagManagerEditableContent = TagTxtContent | TagsJsonContent | StandardJsonContent
export type TagManagerImageContent = TagManagerEditableContent | RawE621JsonContent | TagManagerContentNone

export interface TagManagerImageDetail extends TagManagerImageSummary {
  content: TagManagerImageContent
  sidecar_mtime: number | string | null
  translations?: Record<string, string>
}

export interface TagManagerUpdateResult {
  image_id: number
  journal_id: string
  sidecar_kind: TagManagerSidecarKind
}

export interface TagManagerBatchRequest {
  op: 'add' | 'remove' | 'replace'
  tags: string[]
  replacement?: string
  use_regex?: boolean
  image_ids?: number[]
  filter?: ImageFilterState
}

export interface TagManagerBatchResult {
  affected: number
  journal_id: string
}

export interface TagManagerJournalResult {
  journal_id: string
  reverted?: number | boolean
  reapplied?: number | boolean
}

export interface TagManagerTagStat {
  tag: string
  category: string
  count: number
  translation?: string | null
}

export interface TagManagerStatsPage {
  items: TagManagerTagStat[]
}

export interface TagDbEntry {
  name: string
  category: string
  post_count: number
  alias_of?: string | null
  translation?: string | null
}

export interface TagDbQueryResult {
  profile: TagManagerProfile
  items: TagDbEntry[]
}

export interface TagDbProfileTranslationInfo {
  entries: number
  loaded: boolean
  source: string | null
  updated: string | null
}

export interface TagDbInfo {
  available: { e621: string[]; danbooru: string[] }
  loaded: { e621: boolean; danbooru: boolean }
  translations?: {
    e621: TagDbProfileTranslationInfo
    danbooru: TagDbProfileTranslationInfo
  }
}

export interface TagTranslationLookupRequest {
  profile: TagManagerProfile
  tags: string[]
}

export interface TagTranslationLookupResult {
  profile: TagManagerProfile
  translations: Record<string, string>
}

export interface TagTranslateRequest {
  profile: TagManagerProfile
  tags: string[]
  provider_id?: string
  model?: string
}

export interface TagTranslateResult {
  profile: TagManagerProfile
  translations: Record<string, string>
  translated_now: number
  from_dictionary: number
  provider_id: string
  model: string
}

export interface NlTranslateRequest {
  text: string
  target?: 'zh' | 'en'
  provider_id?: string
  model?: string
}

export interface NlTranslateResult {
  text: string
  target: 'zh' | 'en'
  provider_id: string
  model: string
}

// --- Client ---

function datasetPath(sessionId: string): string {
  return `/tag-manager/datasets/${encodeURIComponent(sessionId)}`
}

export const tagManagerApi = {
  createDataset: (body: TagManagerCreateRequest) =>
    request<TagManagerSession>('/tag-manager/datasets', { method: 'POST', body: JSON.stringify(body) }),
  datasets: () => request<{ items: TagManagerSession[] }>('/tag-manager/datasets'),
  dataset: (sessionId: string) => request<TagManagerSession>(datasetPath(sessionId)),
  deleteDataset: (sessionId: string) => request<void>(datasetPath(sessionId), { method: 'DELETE' }),
  refreshDataset: (sessionId: string) =>
    request<TagManagerSession>(`${datasetPath(sessionId)}/refresh`, { method: 'POST', body: '{}' }),

  images: (sessionId: string, params: TagManagerImageQuery = {}) =>
    request<TagManagerImagePage>(`${datasetPath(sessionId)}/images?${imageFilterQuery(params).toString()}`),
  imageDetail: (sessionId: string, imageId: number) =>
    request<TagManagerImageDetail>(`${datasetPath(sessionId)}/images/${imageId}`),
  updateImage: (
    sessionId: string,
    imageId: number,
    body: { content: TagManagerEditableContent; expected_sidecar_mtime?: number | string },
  ) =>
    request<TagManagerUpdateResult>(`${datasetPath(sessionId)}/images/${imageId}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),

  batch: (sessionId: string, body: TagManagerBatchRequest) =>
    request<TagManagerBatchResult>(`${datasetPath(sessionId)}/batch`, { method: 'POST', body: JSON.stringify(body) }),
  undo: (sessionId: string) =>
    request<TagManagerJournalResult>(`${datasetPath(sessionId)}/undo`, { method: 'POST', body: '{}' }),
  redo: (sessionId: string) =>
    request<TagManagerJournalResult>(`${datasetPath(sessionId)}/redo`, { method: 'POST', body: '{}' }),

  stats: (sessionId: string, params: { limit?: number; min_count?: number } = {}) => {
    const query = new URLSearchParams()
    if (params.limit !== undefined) query.set('limit', String(params.limit))
    if (params.min_count !== undefined) query.set('min_count', String(params.min_count))
    const suffix = query.toString() ? `?${query.toString()}` : ''
    return request<TagManagerStatsPage>(`${datasetPath(sessionId)}/tags/stats${suffix}`)
  },

  /** Tag database queries under /api/v1/tag-manager/tag-db. */
  tagDb: (profile: TagManagerProfile, query?: string, limit = 20) => {
    const search = new URLSearchParams()
    search.set('profile', profile)
    if (query) search.set('query', query)
    search.set('limit', String(limit))
    return request<TagDbQueryResult>(`/tag-manager/tag-db?${search.toString()}`)
  },
  tagDbInfo: () => request<TagDbInfo>('/tag-manager/tag-db/info'),

  lookupTranslations: (body: TagTranslationLookupRequest) =>
    request<TagTranslationLookupResult>('/tag-manager/translations/lookup', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /** Translate dictionary-missing tags with the online model; results are persisted server-side. */
  translateTags: (body: TagTranslateRequest) =>
    request<TagTranslateResult>('/tag-manager/translations/translate', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  nlTranslate: (body: NlTranslateRequest) =>
    request<NlTranslateResult>('/tag-manager/nl/translate', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
}

/**
 * Escape one tag for the tag query parameters: a literal backslash or comma
 * is backslash-escaped so a tag may itself contain commas.  Every other
 * character is passed through unchanged, matching the backend parser, so
 * legacy requests (no backslashes) keep their exact old behaviour.
 */
export function escapeTagForQuery(tag: string): string {
  return tag.replace(/\\/g, '\\\\').replace(/,/g, '\\,')
}

/** Split one tag query value on unescaped commas and unescape each part. */
function splitTagQueryValue(value: string): string[] {
  const parts: string[] = []
  let current = ''
  let escaped = false
  for (const char of value) {
    if (escaped) {
      current += char
      escaped = false
    } else if (char === '\\') {
      current += char
      escaped = true
    } else if (char === ',') {
      parts.push(current)
      current = ''
    } else {
      current += char
    }
  }
  parts.push(current)
  return parts
}

/** Undo the escape written by `escapeTagForQuery`; other `\x` pairs stay verbatim. */
function unescapeTagQueryValue(value: string): string {
  let out = ''
  let escaped = false
  for (const char of value) {
    if (escaped) {
      out += char === ',' || char === '\\' ? char : `\\${char}`
      escaped = false
    } else if (char === '\\') {
      escaped = true
    } else {
      out += char
    }
  }
  return escaped ? `${out}\\` : out
}

/**
 * Parse the comma-separated filter input into tags, honouring backslash
 * escapes so a tag may itself contain a comma.  Mirrors the backend's
 * `_split_tag_query`; plain legacy input behaves exactly as before.
 */
export function parseTagFilterInput(value: string): string[] {
  return splitTagQueryValue(value)
    .map((part) => unescapeTagQueryValue(part.trim()))
    .filter(Boolean)
}

/**
 * Render the filter-bar input value for a tag list.  Tags are shown in the
 * preferred separator style; literal commas and backslashes are escaped so
 * editing the box never silently splits a tag that contains a comma.
 */
export function formatTagFilterInput(tags: string[], style: TagStyle): string {
  return tags.map((tag) => escapeTagForQuery(formatTagForDisplay(tag, style))).join(', ')
}

/** Serialises image-list query parameters exactly as the backend expects.
 * Tags are sent as repeated query parameters with commas/backslashes
 * escaped, so a tag containing a comma survives the round trip; the backend
 * also still accepts the legacy single comma-joined parameter. */
export function imageFilterQuery(params: TagManagerImageQuery): URLSearchParams {
  const query = new URLSearchParams()
  query.set('offset', String(params.offset ?? 0))
  query.set('limit', String(params.limit ?? 60))
  if (params.sort) query.set('sort', params.sort)
  const filter = params.filter
  if (filter) {
    for (const tag of filter.includeTags) query.append('include_tags', escapeTagForQuery(tag))
    for (const tag of filter.excludeTags) query.append('exclude_tags', escapeTagForQuery(tag))
    // `all` is the backend default and stays off the wire.
    if (filter.includeMode && filter.includeMode !== 'all') query.set('include_mode', filter.includeMode)
    if (filter.kind && filter.kind !== 'any') query.set('kind', filter.kind)
    if (filter.sidecar && filter.sidecar !== 'any') query.set('sidecar', filter.sidecar)
  }
  return query
}

/** Thumbnails are public JWT-less image responses, consumed via <img src>. */
export function tagManagerThumbnailUrl(sessionId: string, imageId: number, size = 256): string {
  return `${API_BASE}/tag-manager/datasets/${encodeURIComponent(sessionId)}/images/${imageId}/thumbnail?size=${size}`
}

export function formatPostCount(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1).replace(/\.0$/, '')}m`
  if (value >= 1_000) return `${(value / 1_000).toFixed(1).replace(/\.0$/, '')}k`
  return String(value)
}

/** Render a tag in the user's preferred separator style. */
export function formatTagForDisplay(tag: string, style: TagStyle): string {
  return style === 'space' ? tag.replace(/_/g, ' ') : tag.replace(/\s+/g, '_')
}

/**
 * The spelling written back to the sidecar. Identical to the display form on
 * purpose: the toggle governs what is stored, not only what is shown.
 */
export function toWriteStyle(tag: string, style: TagStyle): string {
  return formatTagForDisplay(tag, style)
}

/** Dictionary key for translation lookups, mirroring the backend's rule. */
export function translationKey(tag: string): string {
  return tag.trim().replace(/\s+/g, '_').toLowerCase()
}

/** Resolve one tag's Chinese name from an image detail's translation map. */
export function translationFor(
  translations: Record<string, string> | undefined,
  tag: string,
): string | null {
  if (!translations) return null
  const direct = translations[tag]
  if (direct) return direct
  const key = translationKey(tag)
  for (const [candidate, value] of Object.entries(translations)) {
    if (translationKey(candidate) === key) return value
  }
  return null
}