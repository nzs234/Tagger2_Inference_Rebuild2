import { useState } from 'react'
import { ChevronDown, ChevronRight, ExternalLink, Sparkles } from 'lucide-react'
import { tagCategoryClass, tagCategoryLabel } from '../../lib/tagCategories'
import { formatPostCount, formatTagForDisplay } from '../../lib/tagManager'
import type { ChunkHit, LookupResult, TagRef, WikiSummaryInfo } from '../../lib/tagWiki'
import { usePreferences } from '../../store/app'

/** Render a single TagRef as a colored pill */
export function WikiTagPill({
  tag,
  onClick,
  clickable = true,
}: {
  tag: TagRef
  onClick?: (name: string) => void
  clickable?: boolean
}) {
  const bilingual = usePreferences((state) => state.bilingualTags)
  const tagStyle = usePreferences((state) => state.tagStyle)
  const displayName = formatTagForDisplay(tag.name, tagStyle)
  const showZh = bilingual && Boolean(tag.translation)

  const content = (
    <>
      <span className="tw-pill-name">{displayName}</span>
      {showZh && <span className="tw-pill-zh">{tag.translation}</span>}
      {tag.post_count != null && (
        <span className="tw-pill-count">{formatPostCount(tag.post_count)}</span>
      )}
      {tag.alias_of && (
        <small className="tw-pill-alias">→ {formatTagForDisplay(tag.alias_of, tagStyle)}</small>
      )}
    </>
  )

  const titleParts = [tagCategoryLabel(tag.category)]
  if (tag.translation) titleParts.push(`${displayName} · ${tag.translation}`)
  else titleParts.push(displayName)
  if (tag.post_count != null) titleParts.push(`${tag.post_count.toLocaleString('zh-CN')} 篇帖子`)
  if (tag.alias_of) titleParts.push(`别名，重定向至: ${tag.alias_of}`)

  if (!clickable || !onClick) {
    return (
      <span
        className={`tm-pill ${tagCategoryClass(tag.category)}`}
        title={titleParts.join(' · ')}
      >
        {content}
      </span>
    )
  }

  return (
    <button
      type="button"
      className={`tm-pill ${tagCategoryClass(tag.category)} tw-clickable-pill`}
      title={titleParts.join(' · ')}
      onClick={() => onClick(tag.name)}
    >
      {content}
    </button>
  )
}

/** Structured Chinese summary card (含义/用法/搭配建议/注意事项) */
export function WikiSummaryCard({
  summary,
  onTagClick,
}: {
  summary: WikiSummaryInfo
  onTagClick?: (tag: string) => void
}) {
  const tagStyle = usePreferences((state) => state.tagStyle)
  const hasFields = Boolean(summary.meaning || summary.usage || summary.pairing || summary.notes)
  const hasTags = Boolean(summary.tags && summary.tags.length > 0)

  if (!hasFields && !hasTags) return null

  return (
    <div className="tw-summary-card">
      <div className="tw-summary-header">
        <Sparkles size={14} aria-hidden="true" />
        <strong>中文摘要</strong>
        {(summary.provider_id || summary.model) && (
          <small className="muted">
            by {summary.provider_id ? `${summary.provider_id} / ` : ''}
            {summary.model ?? 'AI'}
          </small>
        )}
      </div>

      <div className="tw-summary-fields">
        {summary.meaning && (
          <div className="tw-summary-field">
            <span className="tw-summary-label">含义</span>
            <span className="tw-summary-value">{summary.meaning}</span>
          </div>
        )}
        {summary.usage && (
          <div className="tw-summary-field">
            <span className="tw-summary-label">用法</span>
            <span className="tw-summary-value">{summary.usage}</span>
          </div>
        )}
        {summary.pairing && (
          <div className="tw-summary-field">
            <span className="tw-summary-label">搭配建议</span>
            <span className="tw-summary-value">{summary.pairing}</span>
          </div>
        )}
        {summary.notes && (
          <div className="tw-summary-field">
            <span className="tw-summary-label">注意事项</span>
            <span className="tw-summary-value">{summary.notes}</span>
          </div>
        )}
      </div>

      {hasTags && (
        <div className="tw-summary-tags">
          <span className="tw-summary-tags-label">相关词</span>
          <div className="tw-chip-row">
            {summary.tags!.map((t) => (
              <button
                type="button"
                key={t}
                className="tm-chip"
                onClick={() => onTagClick?.(t)}
                title={`查询 ${t}`}
              >
                {formatTagForDisplay(t, tagStyle)}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

/** Full or compact lookup result card */
export function LookupResultCard({
  result,
  compact = false,
  onTagClick,
}: {
  result: LookupResult
  compact?: boolean
  onTagClick?: (tag: string) => void
}) {
  const tagStyle = usePreferences((state) => state.tagStyle)
  const [openSections, setOpenSections] = useState<Record<number, boolean>>({ 0: true })

  if (!result.resolved) {
    return (
      <div className="tw-lookup-empty">
        <p>未找到标签 <strong>{result.query}</strong> 的 Wiki 条目。</p>
        <p className="muted">请检查拼写是否正确，或尝试在「语义搜索」中输入自然语言查找。</p>
      </div>
    )
  }

  const toggleSection = (idx: number) => {
    setOpenSections((prev) => ({ ...prev, [idx]: !prev[idx] }))
  }

  const { tag, implications, page } = result

  return (
    <div className={`tw-lookup-card ${compact ? 'tw-compact' : ''}`}>
      {/* Header: Tag pill, category, alias, and e621 link */}
      <div className="tw-lookup-header">
        <div className="tw-lookup-identity">
          {tag ? (
            <WikiTagPill tag={tag} clickable={false} />
          ) : (
            <h3 className="tw-lookup-title">{page?.title ?? result.query}</h3>
          )}
          {tag?.alias_of && (
            <span className="tw-alias-notice">
              （别名，标准标签为{' '}
              <button
                type="button"
                className="tw-link-button"
                onClick={() => onTagClick?.(tag.alias_of!)}
              >
                {formatTagForDisplay(tag.alias_of, tagStyle)}
              </button>
              ）
            </span>
          )}
        </div>

        {page?.url && (
          <a
            href={page.url}
            target="_blank"
            rel="noreferrer noopener"
            className="tw-external-link"
            title="在 e621 官方查看原条目"
          >
            <span>在 e621 查看</span>
            <ExternalLink size={13} aria-hidden="true" />
          </a>
        )}
      </div>

      {/* Implications */}
      {implications && implications.length > 0 && (
        <div className="tw-implications-box">
          <div className="tw-box-title">隐含标签（需要搭配）</div>
          <div className="tw-pill-row">
            {implications.map((imp) => (
              <WikiTagPill key={imp.name} tag={imp} onClick={onTagClick} />
            ))}
          </div>
        </div>
      )}

      {/* Chinese Summary */}
      {page?.summary && (
        <WikiSummaryCard summary={page.summary} onTagClick={onTagClick} />
      )}

      {/* Sections */}
      {page?.sections && page.sections.length > 0 && (
        <div className="tw-sections">
          <div className="tw-box-title">Wiki 章节内容</div>
          {page.sections.map((sec, idx) => {
            const isOpen = Boolean(openSections[idx])
            return (
              <div className="tw-section" key={`${sec.heading}-${idx}`}>
                <button
                  type="button"
                  className="tw-section-trigger"
                  onClick={() => toggleSection(idx)}
                  aria-expanded={isOpen}
                >
                  {isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                  <strong>{sec.heading || '正文'}</strong>
                </button>
                {isOpen && (
                  <div className="tw-section-content">
                    <p>{sec.text}</p>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

      {/* Related tags */}
      {page?.related_tags && page.related_tags.length > 0 && (
        <div className="tw-related-box">
          <div className="tw-box-title">相关标签</div>
          <div className="tw-chip-row">
            {page.related_tags.map((rt) => (
              <button
                type="button"
                key={rt}
                className="tm-chip"
                onClick={() => onTagClick?.(rt)}
                title={`查询 ${rt}`}
              >
                {formatTagForDisplay(rt, tagStyle)}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

/** Search chunk hit card */
export function ChunkHitCard({
  hit,
  maxScore = 1,
  onTagClick,
}: {
  hit: ChunkHit
  maxScore?: number
  onTagClick?: (tag: string) => void
}) {
  const normScore = maxScore > 0 ? Math.min(100, Math.round((hit.score / maxScore) * 100)) : 0

  return (
    <div className="tw-chunk-card">
      <div className="tw-chunk-header">
        <div className="tw-chunk-title-group">
          {hit.tag ? (
            <WikiTagPill tag={hit.tag} onClick={onTagClick} />
          ) : (
            <button
              type="button"
              className="tw-page-title-button"
              onClick={() => onTagClick?.(hit.page_title)}
            >
              {hit.page_title}
            </button>
          )}
          {hit.heading && <span className="tw-chunk-heading">§ {hit.heading}</span>}
        </div>

        <div className="tw-chunk-meta">
          <div className="tw-matched-badges">
            {hit.matched_by.map((m) => (
              <span key={m} className={`tw-match-badge tw-match-${m}`}>
                {m === 'vector' ? '向量' : m === 'keyword' ? '关键词' : m}
              </span>
            ))}
          </div>
          <div className="tw-score-indicator" title={`相关度分值: ${hit.score.toFixed(3)}`}>
            <div className="tw-score-bar-bg">
              <div className="tw-score-bar-fill" style={{ width: `${normScore}%` }} />
            </div>
            <span className="tw-score-number">{normScore}%</span>
          </div>
        </div>
      </div>

      <div className="tw-chunk-text">
        <p>{hit.text}</p>
      </div>

      {hit.summary && (
        <div className="tw-chunk-summary-brief">
          {hit.summary.meaning && (
            <p>
              <strong>含义：</strong>
              {hit.summary.meaning}
            </p>
          )}
          {hit.summary.usage && (
            <p>
              <strong>用法：</strong>
              {hit.summary.usage}
            </p>
          )}
        </div>
      )}
    </div>
  )
}
