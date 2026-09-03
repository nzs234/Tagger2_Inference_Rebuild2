import { request } from './api'

// --- Tag Wiki Types (mirrors backend/tagger2/tag_wiki/contracts.py) ---

export type TagWikiProfile = 'e621'

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
}

export interface TagWikiStatus {
  database: WikiDatabaseStatus
  index: WikiIndexStatus
  build: BuildStatus
  translate: TranslateStatus
}

export interface BuildRequest {
  download_dump?: boolean
  reindex?: boolean
  force_reembed?: boolean
}

export interface TranslateRequest {
  scope: 'model_vocab' | 'popular' | 'all'
  min_post_count?: number
  max_pages?: number
  provider_id?: string
  model?: string
}

export interface SearchRequest {
  query: string
  top_k?: number
}

export interface AskRequest {
  query: string
  top_k?: number
  provider_id?: string
  model?: string
}

// --- Tag Wiki API Client ---

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

  page: (title: string) => request<WikiPageInfo>(`/tag-wiki/page/${encodeURIComponent(title)}`),
}
