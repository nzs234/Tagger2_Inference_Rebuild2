import { useState } from 'react'
import { BookOpen } from 'lucide-react'
import { mergeTags } from '../lib/anima'
import type { TagItem } from '../types'
import { WikiDrawer } from './tagWiki/WikiDrawer'

const categoryNames: Record<string, string> = {
  quality: '质量',
  character: '角色',
  artist: '作者',
  appearance: '外观',
  environment: '环境',
  general: '通用',
}

const categoryOrder: Record<string, number> = {
  character: 0,
  species: 1,
  copyright: 2,
  artist: 3,
  meta: 4,
  rating: 5,
  general: 99,
}

export function TagCloud({ tags }: { tags: TagItem[] }) {
  const [wikiTag, setWikiTag] = useState<string | null>(null)
  const groups = new Map<string, TagItem[]>()
  for (const tag of mergeTags(tags)) groups.set(tag.category, [...(groups.get(tag.category) ?? []), tag])
  if (!groups.size) return <p className="muted">暂无标签</p>
  return <div className="tag-groups">
    {[...groups.entries()].sort(([left], [right]) => (categoryOrder[left] ?? 50) - (categoryOrder[right] ?? 50) || left.localeCompare(right)).map(([category, values]) => (
      <div className="tag-group" key={category}>
        <div className="tag-group-title"><span>{categoryNames[category] ?? category}</span><small>{values.length}</small></div>
        <div className="tag-pills">
          {values.map((tag) => <span className="tag-pill" title={tag.score == null ? tag.source : `${tag.source} · ${Math.round(tag.score * 100)}%`} key={`${category}:${tag.text}`}>
            <span>{tag.text}</span>
            {tag.score != null && <small>{Math.round(tag.score * 100)}%</small>}
            <button
              type="button"
              className="tag-pill-wiki-btn"
              title={`查看 ${tag.text} 的 Wiki`}
              aria-label={`查看 ${tag.text} 的 Wiki`}
              onClick={(e) => {
                e.stopPropagation()
                setWikiTag(tag.text)
              }}
            >
              <BookOpen size={12} aria-hidden="true" />
            </button>
          </span>)}
        </div>
      </div>
    ))}
    {wikiTag && <WikiDrawer tag={wikiTag} onClose={() => setWikiTag(null)} />}
  </div>
}

