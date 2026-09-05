import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronUp, Database, RefreshCw } from 'lucide-react'
import {
  tagWikiApi,
  WIKI_PROFILE_LABELS,
  type TagWikiProfile,
  type TagWikiStatus,
} from '../../lib/tagWiki'
import { Notice } from '../ui'

// Data maintenance (dump download, reindex, re-embedding, translation) is a
// maintainer/CLI job; the UI is intentionally read-only status + query.
export function BuildPanel({ profile }: { profile: TagWikiProfile }) {
  const [collapsed, setCollapsed] = useState(false)

  const statusQuery = useQuery<TagWikiStatus>({
    queryKey: ['tag-wiki', 'status'],
    queryFn: tagWikiApi.status,
    refetchInterval: 30_000,
    retry: 1,
  })

  const status = statusQuery.data
  const frozen = status?.frozen === true

  // Per-mirror database/index view; the top-level keys mirror e621 for
  // older backends.
  const profileStatus = status?.profiles?.[profile]
  const db = profileStatus?.database ?? status?.database
  const idx = profileStatus?.index ?? status?.index

  return (
    <div className="tw-build-panel">
      <div className="tw-build-panel-header">
          <div className="tw-build-title-row">
          <Database size={16} aria-hidden="true" />
          <h2 className="tw-build-title">
            {WIKI_PROFILE_LABELS[profile]} Wiki 数据库状态
          </h2>
        </div>
        <div className="tw-build-header-actions">
          <button
            type="button"
            className="icon-button icon-button-quiet"
            title="刷新状态"
            aria-label="刷新状态"
            onClick={() => statusQuery.refetch()}
            disabled={statusQuery.isFetching}
          >
            <RefreshCw size={14} className={statusQuery.isFetching ? 'spin' : ''} />
          </button>
          <button
            type="button"
            className="tw-collapse-btn"
            onClick={() => setCollapsed((v) => !v)}
            aria-expanded={!collapsed}
            title={collapsed ? '展开面板' : '收起面板'}
          >
            {collapsed ? <ChevronDown size={16} /> : <ChevronUp size={16} />}
          </button>
        </div>
      </div>

      {!collapsed && (
        <div className="tw-build-panel-body">
          {/* Status Chips */}
          <div className="tw-status-chips">
            <div className="tw-chip-item">
              <span className="tw-chip-label">Wiki 页数</span>
              <strong>{db?.pages ? db.pages.toLocaleString('zh-CN') : 0}</strong>
            </div>
            <div className="tw-chip-item">
              <span className="tw-chip-label">章节数</span>
              <strong>{db?.chunks ? db.chunks.toLocaleString('zh-CN') : 0}</strong>
            </div>
            <div className="tw-chip-item">
              <span className="tw-chip-label">已向量化</span>
              <strong>
                {db?.embedded_chunks ? db.embedded_chunks.toLocaleString('zh-CN') : 0}
              </strong>
            </div>
            <div className="tw-chip-item">
              <span className="tw-chip-label">已翻译摘要</span>
              <strong>
                {db?.translated_pages ? db.translated_pages.toLocaleString('zh-CN') : 0}
              </strong>
            </div>
            <div className="tw-chip-item">
              <span className="tw-chip-label">Dump 日期</span>
              <strong>{db?.dump_date ?? '未同步'}</strong>
            </div>
            <div className="tw-chip-item">
              <span className="tw-chip-label">检索状态</span>
              <strong className={idx?.search_ready ? 'tw-text-success' : 'tw-text-warning'}>
                {idx?.search_ready ? '就绪' : '未就绪'}
              </strong>
            </div>
          </div>

          {frozen && (
            <Notice tone="info">
              <Database size={15} />
              <span>Wiki 数据与中文翻译已内置，可直接使用；数据维护由发布者完成。</span>
            </Notice>
          )}
        </div>
      )}
    </div>
  )
}
