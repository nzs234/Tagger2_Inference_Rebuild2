import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import {
  AlertCircle,
  BookOpen,
  Bot,
  HelpCircle,
  LoaderCircle,
  Search,
  Send,
  Sparkles,
} from 'lucide-react'
import { BuildPanel } from '../components/tagWiki/BuildPanel'
import { ChunkHitCard, LookupResultCard, WikiTagPill } from '../components/tagWiki/ResultCards'
import { Button, Notice, Panel } from '../components/ui'
import { ApiError } from '../lib/api'
import {
  tagWikiApi,
  type AskResult,
  type LookupResult,
  type SearchResult,
} from '../lib/tagWiki'
import { usePreferences } from '../store/app'

type WikiMode = 'lookup' | 'search' | 'ask'

export function TagWiki() {
  const [mode, setMode] = useState<WikiMode>('lookup')

  // Lookup state
  const [lookupInput, setLookupInput] = useState('')
  const [lookupResult, setLookupResult] = useState<LookupResult | null>(null)

  // Search state
  const [searchQuery, setSearchQuery] = useState('')
  const [searchTopK, setSearchTopK] = useState(8)
  const [searchResult, setSearchResult] = useState<SearchResult | null>(null)

  // Ask state
  const [askQuery, setAskQuery] = useState('')
  const [askTopK, setAskTopK] = useState(8)
  const [askResult, setAskResult] = useState<AskResult | null>(null)

  const [generalError, setGeneralError] = useState<{ message: string; code?: string } | null>(null)

  const setPage = usePreferences((state) => state.setPage)

  /** Surface the shared error envelope: show the backend's Chinese message and
   * special-case the 409 wiki codes with actionable guidance. */
  const handleError = (err: unknown, defaultMsg: string) => {
    if (err instanceof ApiError) {
      switch (err.code) {
        case 'wiki_not_built':
          setGeneralError({
            code: 'wiki_not_built',
            message: 'Wiki 数据库尚未构建。请先在上方「构建面板」中点击「下载/更新 Wiki 数据」。',
          })
          return
        case 'wiki_busy':
          setGeneralError({
            code: 'wiki_busy',
            message: '已有构建或翻译任务正在进行中，请等待其完成后再试。',
          })
          return
        case 'wiki_ask_unavailable':
          setGeneralError({
            code: 'wiki_ask_unavailable',
            message: '未配置或启用在线模型：AI 问答需要在线 LLM Provider。请前往「在线模型」页面配置。',
          })
          return
        case 'wiki_search_unavailable':
          setGeneralError({
            code: 'wiki_search_unavailable',
            message: '检索未就绪：尚未生成向量索引。请在上方构建面板重新构建索引。',
          })
          return
        case 'wiki_embed_model_unavailable':
          setGeneralError({
            code: 'wiki_embed_model_unavailable',
            message: 'Embedding 向量模型不可用，请检查本地模型缓存或网络连接。',
          })
          return
        case 'wiki_tag_db_unavailable':
          setGeneralError({
            code: 'wiki_tag_db_unavailable',
            message: '本地标签数据库缺失，无法解析标签分类。请先完成标签库构建后再试。',
          })
          return
        default:
          setGeneralError({ code: err.code, message: err.message })
      }
    } else {
      setGeneralError({ message: defaultMsg })
    }
  }

  // Lookup mutation
  const lookupMutation = useMutation({
    mutationFn: (tag: string) => tagWikiApi.lookup(tag),
    onSuccess: (data) => {
      setLookupResult(data)
      setGeneralError(null)
    },
    onError: (err) => {
      setLookupResult(null)
      handleError(err, '查询标签 Wiki 失败')
    },
  })

  // Search mutation
  const searchMutation = useMutation({
    mutationFn: () => tagWikiApi.search({ query: searchQuery.trim(), top_k: searchTopK }),
    onSuccess: (data) => {
      setSearchResult(data)
      setGeneralError(null)
    },
    onError: (err) => {
      setSearchResult(null)
      handleError(err, '语义搜索失败')
    },
  })

  // Ask mutation
  const askMutation = useMutation({
    mutationFn: () => tagWikiApi.ask({ query: askQuery.trim(), top_k: askTopK }),
    onSuccess: (data) => {
      setAskResult(data)
      setGeneralError(null)
    },
    onError: (err) => {
      setAskResult(null)
      handleError(err, 'AI 问答失败')
    },
  })

  const runLookup = (tag: string) => {
    const trimmed = tag.trim()
    if (!trimmed) return
    setLookupInput(trimmed)
    setMode('lookup')
    setGeneralError(null)
    lookupMutation.mutate(trimmed)
  }

  const maxScore = searchResult?.items.length
    ? Math.max(...searchResult.items.map((it) => it.score), 0.001)
    : 1

  return (
    <div className="tag-wiki-page">
      <header className="page-header">
        <div className="page-title-group">
          <h1 className="page-title">Tag Wiki</h1>
          <p className="page-subtitle">本地 E621 标签百科与语义检索 · 查含义 / 语义检索 / AI 问答</p>
        </div>
      </header>

      {/* Top collapsible Build Panel */}
      <BuildPanel />

      {/* Main Mode Tabs */}
      <Panel className="tw-main-panel">
        <div className="tw-tab-nav" role="tablist" aria-label="Wiki 查询模式">
          <button
            type="button"
            role="tab"
            aria-selected={mode === 'lookup'}
            className={`tw-tab-btn ${mode === 'lookup' ? 'tw-tab-active' : ''}`}
            onClick={() => {
              setMode('lookup')
              setGeneralError(null)
            }}
          >
            <BookOpen size={16} aria-hidden="true" />
            <span>查含义</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={mode === 'search'}
            className={`tw-tab-btn ${mode === 'search' ? 'tw-tab-active' : ''}`}
            onClick={() => {
              setMode('search')
              setGeneralError(null)
            }}
          >
            <Search size={16} aria-hidden="true" />
            <span>语义搜索</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={mode === 'ask'}
            className={`tw-tab-btn ${mode === 'ask' ? 'tw-tab-active' : ''}`}
            onClick={() => {
              setMode('ask')
              setGeneralError(null)
            }}
          >
            <Bot size={16} aria-hidden="true" />
            <span>AI 问答</span>
          </button>
        </div>

        {/* Global Error Banner / Guidance */}
        {generalError && (
          <div className="tw-error-container">
            <Notice tone={generalError.code === 'wiki_ask_unavailable' ? 'warning' : 'danger'}>
              <div className="tw-error-row">
                <AlertCircle size={16} />
                <span>{generalError.message}</span>
                {generalError.code === 'wiki_ask_unavailable' && (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => setPage('providers')}
                  >
                    前往「在线模型」页
                  </Button>
                )}
              </div>
            </Notice>
          </div>
        )}

        {/* Tab 1: 查含义 */}
        {mode === 'lookup' && (
          <div className="tw-tab-pane">
            <form
              className="tw-lookup-form"
              onSubmit={(e) => {
                e.preventDefault()
                runLookup(lookupInput)
              }}
            >
              <div className="tw-search-bar">
                <input
                  type="text"
                  placeholder="输入标签英文名 (如 solo, anthro, rating:explicit)…"
                  value={lookupInput}
                  onChange={(e) => setLookupInput(e.target.value)}
                  className="tw-query-input"
                  aria-label="标签名称"
                />
                <Button
                  type="submit"
                  disabled={!lookupInput.trim() || lookupMutation.isPending}
                  icon={lookupMutation.isPending ? <LoaderCircle size={14} className="spin" /> : <Search size={14} />}
                >
                  {lookupMutation.isPending ? '查询中…' : '查询'}
                </Button>
              </div>
            </form>

            {lookupMutation.isPending && (
              <div className="tw-loading-state">
                <LoaderCircle size={24} className="spin" />
                <span>正在查询标签词条与摘要…</span>
              </div>
            )}

            {lookupResult && !lookupMutation.isPending && (
              <LookupResultCard
                result={lookupResult}
                onTagClick={(tag) => runLookup(tag)}
              />
            )}

            {!lookupResult && !lookupMutation.isPending && !generalError && (
              <div className="tw-empty-pane">
                <BookOpen size={36} className="muted" />
                <p>输入任意 E621 标签名称，即刻查询其官方百科条目、隐含关联以及 AI 提炼的中文用法指南。</p>
              </div>
            )}
          </div>
        )}

        {/* Tab 2: 语义搜索 */}
        {mode === 'search' && (
          <div className="tw-tab-pane">
            <form
              className="tw-search-form"
              onSubmit={(e) => {
                e.preventDefault()
                if (searchQuery.trim()) searchMutation.mutate()
              }}
            >
              <div className="tw-textarea-wrap">
                <textarea
                  placeholder="输入自然语言描述或画风/特征意图 (例如: 带有发光符文的暗黑魔法背景，或者某种姿势描写)…"
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  rows={3}
                  className="tw-query-textarea"
                  aria-label="语义搜索内容"
                />
              </div>

              <div className="tw-search-actions">
                <div className="tw-slider-group">
                  <label htmlFor="tw-topk-range">返回结果数量: <strong>{searchTopK}</strong></label>
                  <input
                    id="tw-topk-range"
                    type="range"
                    min={2}
                    max={30}
                    step={1}
                    value={searchTopK}
                    onChange={(e) => setSearchTopK(Number(e.target.value))}
                  />
                </div>

                <Button
                  type="submit"
                  disabled={!searchQuery.trim() || searchMutation.isPending}
                  icon={searchMutation.isPending ? <LoaderCircle size={14} className="spin" /> : <Search size={14} />}
                >
                  {searchMutation.isPending ? '搜索中…' : '检索 Wiki 章节'}
                </Button>
              </div>
            </form>

            {searchMutation.isPending && (
              <div className="tw-loading-state">
                <LoaderCircle size={24} className="spin" />
                <span>正在执行向量 + 关键字混合检索…</span>
              </div>
            )}

            {searchResult && !searchMutation.isPending && (
              <div className="tw-search-results">
                {/* Suggested Tags row */}
                {searchResult.suggested_tags && searchResult.suggested_tags.length > 0 && (
                  <div className="tw-suggested-tags-box">
                    <div className="tw-box-title">
                      <Sparkles size={14} />
                      <span>推荐候选标签</span>
                    </div>
                    <div className="tw-pill-row">
                      {searchResult.suggested_tags.map((st) => (
                        <WikiTagPill
                          key={st.name}
                          tag={st}
                          onClick={(tagName) => runLookup(tagName)}
                        />
                      ))}
                    </div>
                  </div>
                )}

                {/* Chunk hits */}
                <div className="tw-chunk-list">
                  <div className="tw-box-title">
                    命中章节 ({searchResult.items.length})
                  </div>
                  {searchResult.items.length === 0 ? (
                    <p className="muted">未检索到匹配的 Wiki 章节，请尝试更简短或更具特征的描述。</p>
                  ) : (
                    searchResult.items.map((hit, idx) => (
                      <ChunkHitCard
                        key={`${hit.page_title}-${idx}`}
                        hit={hit}
                        maxScore={maxScore}
                        onTagClick={(t) => runLookup(t)}
                      />
                    ))
                  )}
                </div>
              </div>
            )}

            {!searchResult && !searchMutation.isPending && !generalError && (
              <div className="tw-empty-pane">
                <Search size={36} className="muted" />
                <p>使用自然语言检索整库 Wiki 章节，自动融合向量相似度与 SQLite FTS5 关键词匹配，并推荐相关标签。</p>
              </div>
            )}
          </div>
        )}

        {/* Tab 3: AI 问答 */}
        {mode === 'ask' && (
          <div className="tw-tab-pane">
            <form
              className="tw-ask-form"
              onSubmit={(e) => {
                e.preventDefault()
                if (askQuery.trim()) askMutation.mutate()
              }}
            >
              <div className="tw-textarea-wrap">
                <textarea
                  placeholder="向 AI 提问关于标签规则、分类含义或打标规范的问题 (例如: 怎样正确区分 feral 和 anthro 标签？)…"
                  value={askQuery}
                  onChange={(e) => setAskQuery(e.target.value)}
                  rows={3}
                  className="tw-query-textarea"
                  aria-label="AI 问答内容"
                />
              </div>

              <div className="tw-search-actions">
                <div className="tw-slider-group">
                  <label htmlFor="tw-ask-topk">参考章节数: <strong>{askTopK}</strong></label>
                  <input
                    id="tw-ask-topk"
                    type="range"
                    min={2}
                    max={15}
                    step={1}
                    value={askTopK}
                    onChange={(e) => setAskTopK(Number(e.target.value))}
                  />
                </div>

                <Button
                  type="submit"
                  disabled={!askQuery.trim() || askMutation.isPending}
                  icon={askMutation.isPending ? <LoaderCircle size={14} className="spin" /> : <Send size={14} />}
                >
                  {askMutation.isPending ? '思考与检索中…' : '提问'}
                </Button>
              </div>
            </form>

            {askMutation.isPending && (
              <div className="tw-loading-state">
                <LoaderCircle size={24} className="spin" />
                <span>正在检索相关 Wiki 知识并生成回答…</span>
              </div>
            )}

            {askResult && !askMutation.isPending && (
              <div className="tw-ask-result-card">
                <div className="tw-ask-answer-section">
                  <div className="tw-ask-answer-header">
                    <Bot size={16} aria-hidden="true" />
                    <strong>AI 答复</strong>
                  </div>
                  <div className="tw-ask-answer-text">
                    {askResult.answer.split('\n').map((line, idx) => (
                      <p key={idx}>{line}</p>
                    ))}
                  </div>
                </div>

                {askResult.tags && askResult.tags.length > 0 && (
                  <div className="tw-ask-meta-row">
                    <span className="tw-meta-label">提及标签</span>
                    <div className="tw-chip-row">
                      {askResult.tags.map((t) => (
                        <button
                          type="button"
                          key={t}
                          className="tm-chip"
                          onClick={() => runLookup(t)}
                        >
                          {t}
                        </button>
                      ))}
                    </div>
                  </div>
                )}

                {askResult.sources && askResult.sources.length > 0 && (
                  <div className="tw-ask-meta-row">
                    <span className="tw-meta-label">参考来源</span>
                    <div className="tw-chip-row">
                      {askResult.sources.map((s) => (
                        <button
                          type="button"
                          key={s}
                          className="tm-chip tw-chip-source"
                          onClick={() => runLookup(s)}
                          title={`查看来源词条 ${s}`}
                        >
                          § {s}
                        </button>
                      ))}
                    </div>
                  </div>
                )}

                {(askResult.provider_id || askResult.model) && (
                  <div className="tw-ask-footnote">
                    <small className="muted">
                      由在线模型提供: {askResult.provider_id ? `${askResult.provider_id} / ` : ''}
                      {askResult.model}
                    </small>
                  </div>
                )}
              </div>
            )}

            {!askResult && !askMutation.isPending && !generalError && (
              <div className="tw-empty-pane">
                <HelpCircle size={36} className="muted" />
                <p>基于本地 E621 Wiki 数据库的 RAG 知识问答。解答标签含义对比、搭配规则与打标建议。</p>
              </div>
            )}
          </div>
        )}
      </Panel>
    </div>
  )
}

export default TagWiki
