import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  ChevronLeft,
  ChevronRight,
  Database,
  FileText,
  LayoutGrid,
  LoaderCircle,
  Search,
} from 'lucide-react'
import { BuildPanel } from '../components/tagWiki/BuildPanel'
import { WikiSummaryCard, WikiTagPill } from '../components/tagWiki/ResultCards'
import { Button, Notice, Panel } from '../components/ui'
import { tagCategoryClass } from '../lib/tagCategories'
import { formatTagForDisplay } from '../lib/tagManager'
import {
  describeWikiError,
  tagWikiApi,
  WIKI_PROFILES,
  WIKI_PROFILE_LABELS,
  type CatalogRelationItem,
  type TagRef,
  type TagWikiProfile,
} from '../lib/tagWiki'
import { usePreferences } from '../store/app'

const CATALOG_PAGE_SIZE = 60
const SEARCH_DEBOUNCE_MS = 300

const MATCH_LABELS: Record<string, string> = {
  exact: '精确',
  alias: '别名',
  prefix: '前缀',
  token: '词匹配',
  contained: '包含',
}

/**
 * Tag Wiki front page: a local booru-style tag directory. Users browse
 * high-frequency tags (post_count >= threshold, maintained by the CLI) by
 * category / semantic group or search them by name and alias; clicking a tag
 * opens its wiki page, Chinese summary and related tags in the same view.
 *
 * The legacy semantic-search / AI-ask flows are retired from this page; the
 * read-only build/status panel stays for per-profile database state.
 */
export function TagWiki() {
  const [profile, setProfile] = useState<TagWikiProfile>('e621')
  const [category, setCategory] = useState<string | null>(null)
  const [group, setGroup] = useState<string | null>(null)
  const [queryInput, setQueryInput] = useState('')
  const [query, setQuery] = useState('')
  const [offset, setOffset] = useState(0)
  const [detailTitle, setDetailTitle] = useState<string | null>(null)
  const [activeIndex, setActiveIndex] = useState(-1)

  const listRef = useRef<HTMLDivElement | null>(null)
  const tagStyle = usePreferences((state) => state.tagStyle)

  // -- data ---------------------------------------------------------------

  const categoriesQuery = useQuery({
    queryKey: ['tag-wiki', 'catalog-categories', profile],
    queryFn: () => tagWikiApi.catalogCategories(profile),
    retry: 1,
  })

  const browseQuery = useQuery({
    queryKey: ['tag-wiki', 'catalog-browse', profile, category, group, query, offset],
    queryFn: () =>
      tagWikiApi.catalogTags({ profile, category, group, q: query || null, offset, limit: CATALOG_PAGE_SIZE }),
    retry: 1,
  })

  const detailQuery = useQuery({
    queryKey: ['tag-wiki', 'catalog-detail', profile, detailTitle],
    queryFn: () => tagWikiApi.catalogTag(detailTitle!, profile),
    enabled: Boolean(detailTitle),
    retry: 1,
  })

  // -- derived ------------------------------------------------------------

  const categories = categoriesQuery.data
  const browse = browseQuery.data
  const items = useMemo(() => browse?.items ?? [], [browse])
  const activeCategory = categories?.categories.find((c) => c.category === category) ?? null
  const searching = query.trim().length > 0

  // -- effects ------------------------------------------------------------

  // Debounced search: typing never fires a request per keystroke.
  useEffect(() => {
    const handle = window.setTimeout(() => setQuery(queryInput.trim()), SEARCH_DEBOUNCE_MS)
    return () => window.clearTimeout(handle)
  }, [queryInput])

  const resetPaging = () => {
    setOffset(0)
    setActiveIndex(-1)
  }

  const switchProfile = (next: TagWikiProfile) => {
    if (next === profile) return
    setProfile(next)
    setCategory(null)
    setGroup(null)
    setQueryInput('')
    setQuery('')
    setDetailTitle(null)
    resetPaging()
  }

  const selectCategory = (next: string | null) => {
    setCategory(next)
    setGroup(null)
    setDetailTitle(null)
    resetPaging()
  }

  const selectGroup = (next: string | null) => {
    setGroup(next)
    setDetailTitle(null)
    resetPaging()
  }

  const openTag = (name: string) => {
    setActiveIndex(-1)
    setDetailTitle(name)
  }

  const closeDetail = () => {
    setDetailTitle(null)
    setActiveIndex(-1)
  }

  const submitSearch = () => {
    setQuery(queryInput.trim())
    resetPaging()
  }

  const clearSearch = () => {
    setQueryInput('')
    setQuery('')
    resetPaging()
  }

  // Keyboard navigation over the tag list: ArrowDown/ArrowUp move the active
  // row, Enter opens the active tag, Escape jumps back to the list from the
  // detail view.
  const moveActive = (delta: number) => {
    if (!items.length) return
    setActiveIndex((prev) => {
      const next = prev + delta
      if (next < 0) return items.length - 1
      if (next >= items.length) return 0
      return next
    })
  }

  useEffect(() => {
    if (activeIndex < 0) return
    const node = listRef.current?.querySelector<HTMLElement>(`[data-catalog-index="${activeIndex}"]`)
    node?.focus()
  }, [activeIndex, items])

  const onSearchKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      moveActive(1)
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      moveActive(-1)
    } else if (event.key === 'Enter') {
      event.preventDefault()
      if (activeIndex >= 0 && items[activeIndex]) {
        openTag(items[activeIndex].name)
      } else {
        submitSearch()
      }
    } else if (event.key === 'Escape') {
      if (detailTitle) {
        event.preventDefault()
        closeDetail()
      }
    }
  }

  // -- render helpers -----------------------------------------------------

  const renderPillRow = (
    title: string,
    relations: CatalogRelationItem[],
    reverse?: Set<string>,
  ) => {
    if (!relations.length) return null
    return (
      <div className="tw-implications-box">
        <div className="tw-box-title">{title}</div>
        <div className="tw-pill-row">
          {relations.map((rel) =>
            rel.tag ? (
              <span key={`${title}-${rel.name}`} className="tw-catalog-relation">
                <WikiTagPill tag={rel.tag as TagRef} onClick={openTag} />
                {reverse?.has(rel.name) && <em className="tw-catalog-reverse">反向</em>}
              </span>
            ) : (
              <button type="button" key={`${title}-${rel.name}`} className="tm-chip" onClick={() => openTag(rel.name)}>
                {rel.name}
              </button>
            ),
          )}
        </div>
      </div>
    )
  }

  const detail = detailQuery.data
  const detailTag = detail?.tag
  const detailPage = detail?.page

  const total = browse?.total ?? 0
  const rangeStart = total === 0 ? 0 : offset + 1
  const rangeEnd = Math.min(offset + CATALOG_PAGE_SIZE, total)
  const rangeLabel = `第 ${rangeStart.toLocaleString('zh-CN')}–${rangeEnd.toLocaleString('zh-CN')} 个，共 ${total.toLocaleString('zh-CN')} 个`
  const hasPrev = offset > 0
  const hasNext = offset + CATALOG_PAGE_SIZE < total

  return (
    <div className="tag-wiki-page">
      <header className="page-header">
        <div className="page-title-group">
          <h1 className="page-title">Tag Wiki</h1>
          <p className="page-subtitle">本地标签目录 · 按分类浏览 / 按名称与别名搜索 / 查看词条与关联</p>
        </div>
        <div className="tw-profile-switch" role="group" aria-label="Wiki 语料库">
          {WIKI_PROFILES.map((name) => (
            <button
              key={name}
              type="button"
              className={`tw-profile-btn ${profile === name ? 'tw-profile-active' : ''}`}
              aria-pressed={profile === name}
              onClick={() => switchProfile(name)}
            >
              {WIKI_PROFILE_LABELS[name]}
            </button>
          ))}
        </div>
      </header>

      {/* Read-only per-profile database status; maintenance is CLI-only. */}
      <BuildPanel profile={profile} />

      <Panel className="tw-main-panel tw-catalog-panel">
        <div className="tw-catalog-layout">
          {/* Sidebar: first-level official categories */}
          <aside className="tw-catalog-sidebar" aria-label="分类浏览">
            <div className="tw-catalog-sidebar-title">
              <LayoutGrid size={14} aria-hidden="true" />
              <span>分类</span>
            </div>
            <button
              type="button"
              className={`tw-catalog-cat ${category === null ? 'tw-catalog-cat-active' : ''}`}
              onClick={() => selectCategory(null)}
            >
              <span>全部标签</span>
              <strong>{categories?.tag_count ?? '…'}</strong>
            </button>
            {(categories?.categories ?? []).map((cat) => (
              <button
                key={cat.category}
                type="button"
                className={`tw-catalog-cat ${category === cat.category ? 'tw-catalog-cat-active' : ''}`}
                onClick={() => selectCategory(cat.category)}
              >
                <span>{cat.label}</span>
                <strong>{cat.tag_count.toLocaleString('zh-CN')}</strong>
              </button>
            ))}
            {categoriesQuery.error && (
              <p className="tw-catalog-sidebar-error">
                {describeWikiError(categoriesQuery.error, '目录数据加载失败')}
              </p>
            )}
          </aside>

          {/* Main column: search + groups + list/detail */}
          <div className="tw-catalog-main">
            <form
              className="tw-search-bar tw-catalog-search"
              role="search"
              onSubmit={(e) => {
                e.preventDefault()
                submitSearch()
              }}
            >
              <input
                type="text"
                className="tw-query-input"
                placeholder="搜索标签名称或别名（如 solo、anthro、smooch）…"
                value={queryInput}
                onChange={(e) => setQueryInput(e.target.value)}
                onKeyDown={onSearchKeyDown}
                aria-label="搜索标签"
              />
              <Button
                type="submit"
                disabled={browseQuery.isFetching}
                icon={browseQuery.isFetching ? <LoaderCircle size={14} className="spin" /> : <Search size={14} />}
              >
                搜索
              </Button>
              {searching && (
                <Button type="button" variant="outline" onClick={clearSearch}>
                  清除
                </Button>
              )}
            </form>

            {/* Second-level semantic groups for the selected category */}
            {activeCategory && !detailTitle && (
              <div className="tw-catalog-groups" aria-label="语义分组">
                <button
                  type="button"
                  className={`tm-chip ${group === null ? 'tw-chip-active' : ''}`}
                  onClick={() => selectGroup(null)}
                >
                  全部 {activeCategory.label} ({activeCategory.tag_count.toLocaleString('zh-CN')})
                </button>
                {activeCategory.groups.map((grp) => (
                  <button
                    key={grp.key}
                    type="button"
                    className={`tm-chip ${group === grp.key ? 'tw-chip-active' : ''}`}
                    onClick={() => selectGroup(grp.key)}
                  >
                    {grp.label} ({grp.tag_count.toLocaleString('zh-CN')})
                  </button>
                ))}
              </div>
            )}

            {detailTitle ? (
              /* -- Detail view (same page; filters stay untouched) -- */
              <div className="tw-catalog-detail">
                <div className="tw-catalog-detail-header">
                  <Button variant="outline" icon={<ChevronLeft size={14} />} onClick={closeDetail}>
                    返回列表
                  </Button>
                  {searching && (
                    <span className="muted tw-catalog-filter-note">搜索词「{query}」与筛选已保留</span>
                  )}
                </div>

                {detailQuery.isPending && (
                  <div className="tw-loading-state">
                    <LoaderCircle size={20} className="spin" />
                    <span>正在加载标签词条…</span>
                  </div>
                )}
                {detailQuery.error && (
                  <Notice tone="danger">
                    <span>{describeWikiError(detailQuery.error, '加载标签详情失败')}</span>
                  </Notice>
                )}

                {detailTag && (
                  <div className="tw-catalog-detail-body">
                    <div className="tw-lookup-header">
                      <div className="tw-lookup-identity">
                        <WikiTagPill
                          tag={{
                            name: detailTag.name,
                            category: detailTag.category,
                            post_count: detailTag.post_count,
                            translation: detailTag.translation ?? null,
                            alias_of: detailTag.alias_of ?? null,
                          }}
                          clickable={false}
                        />
                        <span className={`tm-pill ${tagCategoryClass(detailTag.category)} tw-catalog-group-badge`}>
                          {detailTag.group_label}
                        </span>
                        {!detailTag.has_wiki && (
                          <span className="muted tw-catalog-filter-note">
                            <Database size={12} aria-hidden="true" /> 暂无 Wiki 词条
                          </span>
                        )}
                      </div>
                    </div>

                    {detailTag.alias_of && (
                      <p className="tw-alias-notice">
                        别名，标准标签为{' '}
                        <button type="button" className="tw-link-button" onClick={() => openTag(detailTag.alias_of!)}>
                          {formatTagForDisplay(detailTag.alias_of, tagStyle)}
                        </button>
                      </p>
                    )}

                    {detailPage?.summary && <WikiSummaryCard summary={detailPage.summary} onTagClick={openTag} />}

                    {renderPillRow('隐含标签（需要搭配）', detail?.implications ?? [], new Set(
                      (detail?.implications ?? []).filter((rel) => rel.direction === 'reverse').map((rel) => rel.name),
                    ))}
                    {renderPillRow('Wiki 页面关联', detail?.wiki_links ?? [])}

                    {detailPage?.sections && detailPage.sections.length > 0 && (
                      <div className="tw-sections">
                        <div className="tw-box-title">
                          <FileText size={12} aria-hidden="true" /> Wiki 原文摘要
                        </div>
                        {detailPage.sections.slice(0, 4).map((sec, idx) => (
                          <div className="tw-section" key={`${sec.heading}-${idx}`}>
                            {sec.heading && <strong>{sec.heading}</strong>}
                            <p className="tw-catalog-section-text">{sec.text}</p>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            ) : (
              /* -- Directory list -- */
              <div className="tw-catalog-list-wrap">
                <div className="tw-catalog-list-meta">
                  <span>
                    {searching ? `搜索「${query}」` : '全部标签'}
                    {group ? ` · ${items[0]?.group_label ?? ''}` : ''} · 共 {total.toLocaleString('zh-CN')} 个
                    {categories?.min_post_count ? `（post_count ≥ ${categories.min_post_count}）` : ''}
                  </span>
                  <span className="tw-catalog-hint">↑↓ 选择 · Enter 打开</span>
                </div>

                {browseQuery.isPending && (
                  <div className="tw-loading-state">
                    <LoaderCircle size={20} className="spin" />
                    <span>正在加载标签目录…</span>
                  </div>
                )}

                {!browseQuery.isPending && items.length === 0 && (
                  <div className="tw-empty-pane">
                    <Search size={32} className="muted" />
                    <p>
                      {searching
                        ? '没有匹配的标签。目录只收录高频标签（post_count ≥ 100），请尝试更短的名称或官方写法。'
                        : '这个分类下还没有标签。'}
                    </p>
                  </div>
                )}

                <div className="tw-catalog-list" ref={listRef} role="listbox" aria-label="标签列表">
                  {items.map((item, index) => (
                    <button
                      type="button"
                      role="option"
                      aria-selected={index === activeIndex}
                      data-catalog-index={index}
                      key={item.name}
                      className={`tw-catalog-item ${index === activeIndex ? 'tw-catalog-item-active' : ''}`}
                      onClick={() => openTag(item.name)}
                    >
                      <WikiTagPill
                        tag={{
                          name: item.name,
                          category: item.category,
                          post_count: item.post_count,
                          translation: item.translation ?? null,
                          alias_of: item.alias_of ?? null,
                        }}
                        clickable={false}
                      />
                      <span className="tw-catalog-item-group">{item.group_label}</span>
                      {item.match && <em className="tw-catalog-match">{MATCH_LABELS[item.match] ?? item.match}</em>}
                      {item.has_wiki && (
                        <span className="tw-catalog-wiki-badge" title="已有本地 Wiki 词条">
                          <FileText size={12} aria-hidden="true" /> Wiki
                        </span>
                      )}
                    </button>
                  ))}
                </div>

                {total > CATALOG_PAGE_SIZE && (
                  <div className="tw-catalog-pagination">
                    <Button
                      variant="outline"
                      disabled={!hasPrev || browseQuery.isFetching}
                      icon={<ChevronLeft size={14} />}
                      onClick={() => {
                        setOffset(Math.max(0, offset - CATALOG_PAGE_SIZE))
                        setActiveIndex(-1)
                      }}
                    >
                      上一页
                    </Button>
                    <span className="muted">{rangeLabel}</span>
                    <Button
                      variant="outline"
                      disabled={!hasNext || browseQuery.isFetching}
                      onClick={() => {
                        setOffset(offset + CATALOG_PAGE_SIZE)
                        setActiveIndex(-1)
                      }}
                    >
                      下一页 <ChevronRight size={14} />
                    </Button>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      </Panel>
    </div>
  )
}

export default TagWiki
