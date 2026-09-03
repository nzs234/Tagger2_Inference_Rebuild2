import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { BookOpen, LoaderCircle, X } from 'lucide-react'
import { ApiError } from '../../lib/api'
import { tagWikiApi, type LookupResult } from '../../lib/tagWiki'
import { DialogLayer, IconButton, Notice } from '../ui'
import { LookupResultCard } from './ResultCards'

export function WikiDrawer({
  tag,
  onClose,
}: {
  tag: string | null
  onClose: () => void
}) {
  const [currentTag, setCurrentTag] = useState<string | null>(tag)
  const activeTag = currentTag ?? tag

  // Follow the parent-provided tag whenever it changes while mounted; clicking
  // a related tag inside the drawer overrides it via currentTag.
  useEffect(() => {
    setCurrentTag(tag)
  }, [tag])

  const lookupQuery = useQuery<LookupResult>({
    queryKey: ['tag-wiki', 'lookup', activeTag],
    queryFn: () => tagWikiApi.lookup(activeTag!),
    enabled: Boolean(activeTag),
    retry: 1,
  })

  if (!tag && !currentTag) return null

  const isPending = lookupQuery.isPending
  const error = lookupQuery.error
  const result = lookupQuery.data

  let errorMessage: string | null = null
  if (error) {
    if (error instanceof ApiError && error.code === 'wiki_not_built') {
      errorMessage = 'Wiki 数据库尚未构建。请前往「Tag Wiki」页面先点击「下载/更新 Wiki 数据」。'
    } else {
      errorMessage = error instanceof ApiError ? error.message : '查询 Wiki 失败'
    }
  }

  return (
    <DialogLayer onClose={onClose}>
      <div
        className="tm-drawer drawer tw-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="tw-drawer-title"
      >
        <header className="drawer-header">
          <div className="tm-drawer-heading">
            <p className="eyebrow">TAG WIKI</p>
            <h2 id="tw-drawer-title" className="tw-drawer-title">
              <BookOpen size={16} aria-hidden="true" />
              <span>{activeTag}</span>
            </h2>
          </div>
          <IconButton label="关闭" onClick={onClose}>
            <X size={17} />
          </IconButton>
        </header>

        <div className="drawer-body tm-drawer-body tw-drawer-body">
          {isPending && (
            <div className="tw-loading-state">
              <LoaderCircle size={20} className="spin" />
              <span>正在查询 Wiki 条目…</span>
            </div>
          )}

          {errorMessage && (
            <Notice tone="danger">
              <span>{errorMessage}</span>
            </Notice>
          )}

          {result && (
            <LookupResultCard
              result={result}
              compact
              onTagClick={(newTag) => setCurrentTag(newTag)}
            />
          )}
        </div>
      </div>
    </DialogLayer>
  )
}
