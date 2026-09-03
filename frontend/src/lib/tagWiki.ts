import { ApiError, request } from './api'

// --- Tag Wiki Types (mirrors backend/tagger2/tag_wiki/contracts.py) ---

export type TagWikiProfile = 'e621' | 'danbooru'

/** Every wiki mirror the backend serves, in UI order. */
export const WIKI_PROFILES: TagWikiProfile[] = ['e621', 'danbooru']

export const WIKI_PROFILE_LABELS: Record<TagWikiProfile, string> = {
  e621: 'e621',
  danbooru: 'Danbooru',
}

export interface TagRef {
  name: string
  category: string
  post_count?: number | null
  alias_of?: string | null
  translation?: string | null
}

export interface WikiSummaryInfo {
  meaning?: string
  usage?: string
  pairing?: string
  notes?: string
  tags?: string[]
  provider_id?: string
  model?: string
  updated_at?: string
}

export interface PageSection {
  heading: string
  text: string
}

export interface WikiPageInfo {
  title: string
  wiki_id?: number | null
  updated_at?: string | null
  url?: string | null
  summary?: WikiSummaryInfo | null
  sections: PageSection[]
  related_tags: string[]
}

export interface LookupResult {
  query: string
  resolved: boolean
  tag: TagRef | null
  implications: TagRef[]
  page: WikiPageInfo | null
}

export interface ChunkHit {
  page_title: string
  heading: string
  text: string
  score: number
  matched_by: ('vector' | 'keyword' | string)[]
  summary: WikiSummaryInfo | null
  tag: TagRef | null
}

export interface SearchResult {
  query: string
  items: ChunkHit[]
  suggested_tags: TagRef[]
}

export interface AskResult {
  query: string
  answer: string
  tags: string[]
  provider_id: string
  model: string
  sources: string[]
}

export type BuildState = 'idle' | 'running' | 'error'
export type BuildPhase = 'idle' | 'download' | 'parse' | 'model' | 'embed' | 'done'

export interface BuildStatus {
  state: BuildState
  phase: BuildPhase
  message: string
  started_at?: string | null
  updated_at?: string | null
  error?: string | null
  profile?: TagWikiProfile
}

export type TranslateState = 'idle' | 'running' | 'error'

export interface TranslateStatus {
  state: TranslateState
  done: number
  failed: number
  total: number
  provider_id: string
  model: string
  message: string
  started_at?: string | null
  updated_at?: string | null
  error?: string | null
  profile?: TagWikiProfile
}

export interface WikiDatabaseStatus {
  exists: boolean
  pages: number
  chunks: number
  embedded_chunks: number
  translated_pages: number
  dump_date: string | null
}

export interface WikiIndexStatus {
  embedding_model: string
  embedding_model_ready: boolean
  dimension: number | null
  fts_enabled: boolean
  search_ready: boolean
  min_post_count?: number
}

export interface TagWikiProfileStatus {
  database: WikiDatabaseStatus
  index: WikiIndexStatus
}

export interface TagWikiStatus {
  /** Per-mirror database/index documents; keys follow TagWikiProfile. */
  profiles?: Partial<Record<TagWikiProfile, TagWikiProfileStatus>>
  /** Backward-compatible top-level view of the e621 profile. */
  database: WikiDatabaseStatus
  index: WikiIndexStatus
  build: BuildStatus
  translate: TranslateStatus
}

export interface BuildRequest {
  profile?: TagWikiProfile
  download_dump?: boolean
  reindex?: boolean
  force_reembed?: boolean
}

export interface TranslateRequest {
  profile?: TagWikiProfile
  scope: 'model_vocab' | 'popular' | 'all'
  min_post_count?: number
  max_pages?: number
  provider_id?: string
  model?: string
}

export interface SearchRequest {
  query: string
  top_k?: number
  profile?: TagWikiProfile
}

export interface AskRequest {
  query: string
  top_k?: number
  provider_id?: string
  model?: string
  profile?: TagWikiProfile
}

// --- Tag Wiki API Client ---

/** Human-readable Chinese guidance per shared wiki error code. */
const WIKI_ERROR_GUIDANCE: Record<string, string> = {
  wiki_not_built: 'Wiki 数据库尚未构建。请前往「Tag Wiki」页面在构建面板中点击「下载/更新 Wiki 数据」。',
  wiki_busy: '已有构建或翻译任务正在进行中，请等待其完成后再试。',
  wiki_ask_unavailable: '未配置或启用在线模型：AI 问答需要在线 LLM Provider。请前往「在线模型」页面配置。',
  wiki_search_unavailable: '检索未就绪：尚未生成向量索引。请在构建面板重新构建索引。',
  wiki_embed_model_unavailable: 'Embedding 向量模型不可用，请检查本地模型缓存或网络连接。',
  wiki_tag_db_unavailable: '本地标签数据库缺失，无法解析标签分类。请先完成标签库构建后再试。',
  wiki_ask_failed: 'AI 生成失败，请稍后重试或更换在线模型。',
  wiki_search_failed: '检索失败，请稍后重试。',
  wiki_lookup_failed: '查询失败，请稍后重试。',
}

/** Map the shared error envelope's wiki codes onto actionable Chinese guidance. */
export function describeWikiError(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    return WIKI_ERROR_GUIDANCE[err.code] ?? err.message
  }
  return fallback
}

export const clampInt = (value: number, min: number, max: number) => {
  const base = Number.isFinite(value) ? value : min
  return Math.min(max, Math.max(min, Math.round(base)))
}

/** The backend error code, for callers that branch on it (e.g. setup hints). */
export function wikiErrorCode(err: unknown): string | null {
  return err instanceof ApiError ? err.code : null
}

export const tagWikiApi = {
  status: () => request<TagWikiStatus>('/tag-wiki/status'),

  build: (body: BuildRequest = {}) =>
    request<TagWikiStatus>('/tag-wiki/build', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  translate: (body: TranslateRequest) =>
    request<TranslateStatus>('/tag-wiki/translate', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  translateProgress: () => request<TranslateStatus>('/tag-wiki/translate/progress'),

  lookup: (tag: string, profile: TagWikiProfile = 'e621') => {
    const search = new URLSearchParams()
    search.set('tag', tag)
    search.set('profile', profile)
    return request<LookupResult>(`/tag-wiki/lookup?${search.toString()}`)
  },

  search: (body: SearchRequest) =>
    request<SearchResult>('/tag-wiki/search', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  ask: (body: AskRequest) =>
    request<AskResult>('/tag-wiki/ask', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  page: (title: string, profile: TagWikiProfile = 'e621') => {
    const search = new URLSearchParams()
    search.set('profile', profile)
    return request<WikiPageInfo>(`/tag-wiki/page/${encodeURIComponent(title)}?${search.toString()}`)
  },
}
