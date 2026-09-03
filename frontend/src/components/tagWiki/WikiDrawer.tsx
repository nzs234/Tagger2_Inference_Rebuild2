import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { BookOpen, LoaderCircle, X } from 'lucide-react'
import {
  describeWikiError,
  tagWikiApi,
  type LookupResult,
  type TagWikiProfile,
} from '../../lib/tagWiki'
import { DialogLayer, IconButton, Notice } from '../ui'
import { LookupResultCard } from './ResultCards'

export function WikiDrawer({
  tag,
  onClose,
  profile = 'e621',
}: {
  tag: string | null
  onClose: () => void
  profile?: TagWikiProfile
}) {
  const [currentTag, setCurrentTag] = useState<string | null>(tag)
  const activeTag = currentTag ?? tag

  // Follow the parent-provided tag whenever it changes while mounted; clicking
  // a related tag inside the drawer overrides it via currentTag.
  useEffect(() => {
    setCurrentTag(tag)
  }, [tag])

  const lookupQuery = useQuery<LookupResult>({
    queryKey: ['tag-wiki', 'lookup', profile, activeTag],
    queryFn: () => tagWikiApi.lookup(activeTag!, profile),
    enabled: Boolean(activeTag),
    retry: 1,
  })

  if (!tag && !currentTag) return null

  const isPending = lookupQuery.isPending
  const error = lookupQuery.error
  const result = lookupQuery.data

  const errorMessage: string | null = error
    ? describeWikiError(error, '查询 Wiki 失败')
    : null

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
              key={result.query}
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
