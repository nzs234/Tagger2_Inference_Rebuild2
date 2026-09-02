import { useQuery } from '@tanstack/react-query'
import { LoaderCircle } from 'lucide-react'
import { EmptyState, Notice } from '../ui'
import { tagCategoryClass, tagCategoryLabel } from '../../lib/tagCategories'
import { formatTagForDisplay, tagManagerApi } from '../../lib/tagManager'
import { usePreferences } from '../../store/app'

/**
 * Top-tags leaderboard. Clicking a tag appends it to the include filter,
 * which is how users narrow the grid down to a specific tag.
 */
export function StatsPanel({ sessionId, enabled, onTagClick }: {
  sessionId: string
  enabled: boolean
  onTagClick: (tag: string) => void
}) {
  const bilingual = usePreferences((state) => state.bilingualTags)
  const tagStyle = usePreferences((state) => state.tagStyle)
  const stats = useQuery({
    queryKey: ['tag-manager', 'stats', sessionId],
    queryFn: () => tagManagerApi.stats(sessionId, { limit: 50 }),
    enabled,
  })

  if (!enabled) return null
  if (stats.isPending) {
    return <div className="tm-stats-state"><LoaderCircle className="spin" size={15} aria-hidden="true" /><span className="muted">正在统计标签…</span></div>
  }
  if (stats.isError) {
    return <Notice tone="danger">标签统计加载失败{stats.error instanceof Error ? `：${stats.error.message}` : ''}</Notice>
  }
  const items = stats.data?.items ?? []
  if (items.length === 0) {
    return <EmptyState title="暂无标签统计" detail="索引完成后这里会展示出现频率最高的标签。" />
  }
  return <ol className="tm-stats-list">
    {items.map((item) => {
      const display = formatTagForDisplay(item.tag, tagStyle)
      const translation = bilingual ? item.translation : null
      return <li key={item.tag}>
        <button
          type="button"
          className="tm-stats-row"
          title={translation ? `筛选包含 ${display} · ${translation}` : `筛选包含 ${display}`}
          onClick={() => onTagClick(item.tag)}
        >
          <span className={`tm-pill ${tagCategoryClass(item.category)}`}>{tagCategoryLabel(item.category)}</span>
          <span className="tm-stats-name">
            {display}
            {translation && <span className="tm-stats-zh">{translation}</span>}
          </span>
          <small>{item.count}</small>
        </button>
      </li>
    })}
  </ol>
}
